"""The pages a person actually looks at: the email log and the settings.

Everything is rendered on the server into one HTML response. The portal owns the
watcher thread and restarts it whenever a connection or a preference changes. It binds
localhost by default and has no authentication, so never publish its port beyond the
machine you trust.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from zoneinfo import available_timezones

from flask import Flask, Response, abort, jsonify, redirect, render_template, request
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response as WerkzeugResponse

from . import prefs, updates
from .app import run
from .cal import CalendarClient, mail_link
from .checks import check_calendar, check_service
from .config import CREDENTIALS, SERVICES, STATE_FILE, Settings, write_secrets
from .mailbox import FLAG_COLOURS
from .prefs import LOG_LEVELS, Category, Prefs
from .store import Store

log = logging.getLogger(__name__)

# The settings page's tabs, in the order they are drawn. Every save comes back through a
# redirect, and this is what carries the tab it was made on across it.
SETTINGS_TABS = ("connections", "preferences")

# The watcher heartbeats once per poll (a minute); well past that means it is wedged.
HEARTBEAT_MAX_AGE_SECONDS = 900.0

# The preferences a tickbox turns on, which is what makes an absent one meaningful.
PREFERENCE_FLAGS = ("notifications_enabled", "notify_events", "notify_failures")

# What each account is running, for when it is removed. Switched back on by hand: an
# account connected again is not somebody asking for the job that used to run on it.
PUT_DOWN_WITH = {"pushover": ("notifications_enabled",)}


def missing_for_start(settings: Settings, current: Prefs) -> list[str]:
    """What still has to be set up before the watcher can run, in the order to do it."""
    missing = []
    if not settings.icloud_configured:
        missing.append("iCloud")
    if not settings.anthropic_configured:
        missing.append("Anthropic")
    if not current.calendar_configured:
        missing.append("a calendar")
    return missing


class Supervisor:
    """Owns the watcher thread; the portal starts, stops, and restarts it."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.missing: list[str] = []
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def restart(self) -> None:
        self.stop()
        settings = Settings()
        current = prefs.current()
        self.missing = missing_for_start(settings, current)
        if self.missing:
            log.info("watcher waiting for setup: %s", ", ".join(self.missing))
            return
        self.error = None
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._watch,
            args=(settings, current, self._stopping),
            name="watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stopping.set()
        # The loop waits on the stop event between polls, so it notices immediately
        # unless it is mid-email.
        self._thread.join(timeout=30)
        if self._thread.is_alive():
            log.warning("watcher thread did not stop in time; abandoning it")
        self._thread = None

    def _watch(self, settings: Settings, current: Prefs, stopping: threading.Event) -> None:
        try:
            run(settings, current, stopping)
        except Exception as exc:
            log.exception("watcher stopped on an error")
            self.error = str(exc) or type(exc).__name__


def ago(then: float, now: float | None = None) -> str:
    """`4m ago`, `3h ago`, `3d ago`: one unit, always the largest that fits."""
    minutes = int(max((now or time.time()) - then, 0) // 60)
    days, rest = divmod(minutes, 1440)
    hours = rest // 60
    if days:
        return f"{days}d ago"
    if hours:
        return f"{hours}h ago"
    return f"{minutes}m ago"


def starts(value: str) -> str:
    """When an event starts, as the calendar would say it: the day, and the time if any."""
    if len(value) == 10:
        return f"{datetime.fromisoformat(value):%a %-d %b}"
    return f"{datetime.fromisoformat(value):%a %-d %b %H:%M}"


def _build_id() -> str:
    """A fingerprint of the code being served, so the page can tell a new process from
    the one it asked to update - stamped commit or not."""
    digest = sha256()
    root = Path(__file__).parent
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".css", ".html", ".jinja", ".js", ".py", ".svg"}:
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _first_validation_message(exc: ValidationError) -> str:
    """One field, one sentence. A wall of pydantic is not an error message."""
    error = exc.errors()[0]
    field = " ".join(str(part) for part in error["loc"] if not isinstance(part, int)) or "value"
    message = str(error["msg"]).removeprefix("Value error, ")
    return f"{field.replace('_', ' ')}: {message}"


def _wants_json() -> bool:
    """Whether the answer is going to a script that will draw it, rather than to a page."""
    return "application/json" in request.headers.get("Accept", "")


def _merged(names: tuple[str, ...], form: Any, *, forget: bool) -> dict[str, str]:
    """What to write for one service: what was typed, or blanks to clear it.

    An empty box means "leave this one alone", because the page never shows a stored
    credential back for it to have been left in. Forget is what clears.
    """
    if forget:
        return dict.fromkeys(names, "")
    return {name: form.get(name, "").strip() for name in names if form.get(name, "").strip()}


def create_app(supervisor: Supervisor) -> Flask:
    app = Flask(__name__)
    # Honour X-Forwarded-Proto/Host from a fronting proxy (tailscale serve, caddy), so
    # generated links come out as the address the browser is really on.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1)  # type: ignore[method-assign]
    app.secret_key = secrets.token_hex(32)
    app.jinja_env.globals.update(email_url=mail_link)
    app.jinja_env.filters.update(ago=ago, starts=starts)
    # Once for the process rather than per use: it also tells the poll after an update
    # that the process answering is a new one, stamped commit or not.
    build = _build_id()

    def saved(tab: str) -> WerkzeugResponse:
        """Back to the settings page, on the tab the form was on."""
        if tab not in SETTINGS_TABS:
            tab = SETTINGS_TABS[0]
        return redirect(f"/settings?tab={tab}", code=303)

    def refused(message: str, tab: str) -> Any:
        """A refusal, beside the button that asked or on the page with the values on it."""
        if _wants_json():
            return jsonify({"error": message}), 400
        return render_template("settings.html", **settings_context(tab=tab, error=message)), 400

    def settings_context(tab: str | None, error: str | None = None) -> dict[str, Any]:
        settings = Settings()
        current = prefs.current()
        return {
            "prefs": current,
            "posted": current.model_dump(mode="json"),
            # Booleans, never values: a stored credential is never rendered back into the
            # page. The Apple ID is the exception, because it names the account rather
            # than proving anything about it.
            "connected": {name: bool(getattr(settings, name)) for name in CREDENTIALS},
            "icloud_email": settings.icloud_email,
            # The Watchtower address is the other value shown back: a name on the
            # compose network, not a secret.
            "watchtower_url": settings.watchtower_url,
            "running_build": updates.RUNNING_SHA[:7],
            "build_id": build,
            "flag_colours": tuple(FLAG_COLOURS),
            "timezones": sorted(available_timezones()),
            "log_levels": LOG_LEVELS,
            "tab": tab,
            "error": error,
        }

    @app.errorhandler(HTTPException)
    def on_http_error(exc: HTTPException) -> Any:
        if request.path == "/healthz":
            return exc
        code = exc.code or 500
        return render_template("error.html", code=code, detail=exc.description), code

    @app.errorhandler(Exception)
    def on_unhandled_error(exc: Exception) -> Any:
        log.exception("unhandled error serving %s", request.path)
        return render_template("error.html", code=500, detail="Something went wrong."), 500

    @app.get("/")
    def mail() -> str:
        """What the service has made of the mailbox: what waits on a person, and what
        reached the calendar."""
        settings = Settings()
        current = prefs.current()
        with Store(STATE_FILE) as store:
            beat = store.last_beat()
            events = store.recent_events()
            failures = store.list_failures()
        return render_template(
            "mail.html",
            failures=failures,
            events=events,
            flag_colour=current.flag_colour,
            icloud_ready=settings.icloud_configured,
            running=supervisor.running,
            missing=missing_for_start(settings, current),
            error=supervisor.error,
            beat=beat,
        )

    @app.post("/retry")
    def retry() -> WerkzeugResponse:
        """Hand one set-aside email back to the watcher.

        Nothing is reprocessed here: the message is still flagged in Mail, so all this
        does is clear the record of having given up. The next pass finds it.
        """
        with Store(STATE_FILE) as store:
            store.clear_failure(request.form.get("message_id", ""))
        return redirect("/", code=303)

    @app.post("/ignore")
    def ignore() -> WerkzeugResponse:
        """Decide the email holds no event, which is what takes its flag off in Mail."""
        with Store(STATE_FILE) as store:
            store.dismiss(request.form.get("message_id", ""))
        return redirect("/", code=303)

    @app.post("/restart")
    def restart() -> WerkzeugResponse:
        supervisor.restart()
        return redirect("/", code=303)

    @app.get("/settings")
    def settings_page() -> str:
        return render_template("settings.html", **settings_context(request.args.get("tab")))

    @app.post("/settings")
    def save_settings() -> Any:
        """Save whichever preferences were posted.

        What arrives is a slice rather than the lot, and a field nobody sent is one
        nobody touched.
        """
        tab = request.form.get("tab", SETTINGS_TABS[0])
        posted = {
            name: request.form[name].strip()
            for name in ("flag_colour", "calendar", "timezone", "log_level")
            if name in request.form
        }
        # A cleared checkbox posts nothing at all, which everywhere else on this route
        # means "leave it alone". The preferences form carries every switch on it each
        # time, so on that form alone an absent one is a box somebody unticked.
        if tab == "preferences":
            posted |= {name: request.form.get(name, "false") for name in PREFERENCE_FLAGS}
        settings = Settings()
        previous = prefs.current()
        # A calendar is proved before it is stored rather than after: a name the account
        # does not offer would only fail on the first email that routes to it.
        chosen = posted.get("calendar")
        if chosen and chosen != previous.calendar:
            result = check_calendar(settings, {chosen})
            if not result.ok:
                return refused(f"Calendar: {result.detail}", tab)
        try:
            with Store(STATE_FILE) as store:
                updated = prefs.save(store, posted)
        except ValidationError as exc:
            return refused(_first_validation_message(exc), tab)
        # Applied here rather than only at boot, so turning the logs up to find out what
        # is going wrong does not need the restart that would clear the evidence.
        logging.getLogger().setLevel(updated.log_level)
        # The watcher holds the preferences it started with, so a change to any of them
        # is a restart. The log level is the exception: it was applied just above.
        if updated.model_dump(exclude={"log_level"}) != previous.model_dump(exclude={"log_level"}):
            supervisor.restart()
        return jsonify({"ok": True}) if _wants_json() else saved(tab)

    @app.post("/settings/categories")
    def save_category() -> Any:
        """Add, change, or remove one category.

        One at a time, like a connection: each row is its own form, so what arrives is
        one category and the name it had before, and nothing else on the page is
        touched by a save that is refused.
        """
        original = request.form.get("original", "").strip().lower()
        kept = [category for category in prefs.current().categories if category.name != original]
        if not request.form.get("remove"):
            try:
                category = Category(
                    name=request.form.get("name", ""),
                    description=request.form.get("description", ""),
                    calendar=request.form.get("calendar", ""),
                )
            except ValidationError as exc:
                return refused(_first_validation_message(exc), "preferences")
            result = check_calendar(Settings(), {category.calendar})
            if not result.ok:
                return refused(f"Calendar: {result.detail}", "preferences")
            kept.append(category)
        try:
            with Store(STATE_FILE) as store:
                prefs.save(store, {"categories": [c.model_dump() for c in kept]})
        except ValidationError as exc:
            return refused(_first_validation_message(exc), "preferences")
        supervisor.restart()
        return jsonify({"ok": True}) if _wants_json() else saved("preferences")

    @app.post("/settings/credentials")
    def save_credentials() -> Any:
        """Store one service's credentials, or forget them.

        Saved one service at a time so that the boxes on the page and the values on file
        can never disagree: nothing is shown back, so a form covering all of them would
        have no way to say which blank boxes were meant.

        What was typed has to work before it is kept. The credentials are the only thing
        the check can read, so they are written, exercised, and put back as they were if
        the service will not have them.
        """
        service = request.form.get("service", "")
        found = next((candidate for candidate in SERVICES if candidate.key == service), None)
        if found is None:
            abort(404, "No such connection.")
        forget = bool(request.form.get("forget"))
        changed = _merged(found.fields, request.form, forget=forget)
        if not changed:
            return jsonify({"ok": True}) if _wants_json() else saved("connections")
        before = Settings()
        restore = {name: getattr(before, name) for name in found.fields}
        settings = write_secrets(changed)
        # The notifications run on the Pushover account, so throwing it away puts them
        # down rather than leaving a switch on for work that has nothing left to do it.
        if forget and service in PUT_DOWN_WITH:
            with Store(STATE_FILE) as store:
                prefs.save(store, dict.fromkeys(PUT_DOWN_WITH[service], False))
        # Forgetting is never refused: a credential you are throwing away does not have
        # to work first.
        if not forget:
            result = check_service(settings, prefs.current(), service)
            if not result.ok:
                write_secrets(restore)
                return refused(f"{found.name}: {result.detail}", "connections")
        supervisor.restart()
        return jsonify({"ok": True}) if _wants_json() else saved("connections")

    @app.get("/settings/calendars")
    def list_calendars() -> str:
        """The pickers' options, fetched after the page rather than during it.

        Discovery is three round trips to iCloud, and putting them in front of the
        render would make every visit to settings wait on them.
        """
        settings = Settings()
        if not settings.icloud_configured:
            return render_template("calendars.html", calendars=[], error=None)
        try:
            calendars = sorted(CalendarClient(settings).calendars())
        except Exception:
            log.warning("could not list the iCloud calendars", exc_info=True)
            return render_template(
                "calendars.html",
                calendars=[],
                error="iCloud could not be reached. Check the connection under Connections.",
            )
        return render_template("calendars.html", calendars=calendars, error=None)

    @app.get("/settings/update/check")
    def update_check() -> Any:
        """What is running and what is published, for the row that offers the buttons.

        Asked three ways: as the settings page opens, to say whether anything newer
        exists; with ?force=1 when the refresh icon is pressed, which goes to the
        registry however fresh the cache is; and every couple of seconds while an
        update runs, to notice the process has been replaced. The poll passes ?poll=1
        and stays off the registry - the answer it needs is in `build`, which any new
        image changes.
        """
        poll = bool(request.args.get("poll"))
        force = bool(request.args.get("force"))
        state = updates.status(refresh=not poll, force=force)
        return jsonify(
            {
                "running": state.running,
                "latest": state.latest,
                "available": state.available,
                "error": state.error,
                "build": build,
            }
        )

    @app.post("/settings/update")
    def run_update() -> Any:
        """Hand the update to Watchtower.

        The likeliest way for this to go well is for this process to be stopped before
        it can say so, which is why the page's script treats a connection that dies here
        as progress rather than failure and goes on to watch for the restart.
        """
        settings = Settings()
        if not settings.watchtower_configured:
            refusal = "Connect Watchtower under Connections first."
            if _wants_json():
                return jsonify({"ok": False, "detail": refusal}), 400
            abort(400, refusal)
        outcome = updates.trigger(settings)
        if _wants_json():
            return jsonify(
                {"ok": outcome.ok, "restarting": outcome.restarting, "detail": outcome.detail}
            ), (200 if outcome.ok else 502)
        if not outcome.ok:
            context = settings_context(tab="preferences", error=f"Watchtower: {outcome.detail}")
            return render_template("settings.html", **context), 502
        return saved("preferences")

    @app.get("/healthz")
    def healthz() -> Response:
        if not supervisor.running:
            # Waiting for setup is healthy; a watcher that died is not.
            if supervisor.error:
                return Response(f"watcher stopped: {supervisor.error}\n", status=500)
            return Response("ok (waiting for setup)\n")
        with Store(STATE_FILE) as store:
            beat = store.last_beat()
        age = None if beat is None else time.time() - beat
        if age is not None and age > HEARTBEAT_MAX_AGE_SECONDS:
            return Response(f"heartbeat is {age:.0f}s old\n", status=500)
        return Response("ok\n")

    return app
