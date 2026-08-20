"""The portal: configure the service and watch it work.

The portal writes data/config.json (the configuration source of truth, overridable by
environment variables), owns the watcher thread, and restarts it whenever the
configuration changes. It binds localhost by default and has no authentication, so
never publish its port beyond the machine you trust.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any
from zoneinfo import available_timezones

from flask import Flask, Response, flash, redirect, render_template, request, url_for
from pydantic import ValidationError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response as WerkzeugResponse

from .app import run
from .checks import run_checks
from .config import CONFIG_FILE, Settings
from .store import Store

log = logging.getLogger(__name__)

# The settings the form manages; exactly these end up in config.json.
FORM_FIELDS = [
    "apple_id",
    "apple_password",
    "imap_host",
    "imap_port",
    "poll_interval_seconds",
    "provider",
    "anthropic_api_key",
    "anthropic_model",
    "anthropic_effort",
    "ollama_url",
    "ollama_model",
    "enable_vision",
    "max_attachment_mb",
    "caldav_url",
    "calendar_name",
    "default_timezone",
    "pushover_user",
    "pushover_token",
    "log_level",
]

CHECKBOX_FIELDS = {"enable_vision"}

# The watcher heartbeats once per poll (default 60s); well past that means it is wedged.
HEARTBEAT_MAX_AGE_SECONDS = 900.0


def missing_for_start(settings: Settings) -> list[str]:
    """What still has to be configured before the watcher can run."""
    missing = []
    if not settings.apple_id or not settings.apple_password:
        missing.append("iCloud credentials")
    if settings.provider == "anthropic" and not settings.anthropic_api_key:
        missing.append("Anthropic API key")
    if not settings.calendar_name:
        missing.append("calendar name")
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
        try:
            settings = Settings()
        except ValidationError as exc:
            self.error = str(exc)
            return
        self.missing = missing_for_start(settings)
        if self.missing:
            log.info("watcher waiting for configuration: %s", ", ".join(self.missing))
            return
        self.error = None
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._watch, args=(settings, self._stopping), name="watcher", daemon=True
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

    def _watch(self, settings: Settings, stopping: threading.Event) -> None:
        try:
            run(settings, stopping)
        except Exception as exc:
            log.exception("watcher stopped on an error")
            self.error = str(exc) or type(exc).__name__


def form_values(settings: Settings) -> dict[str, Any]:
    """Settings as the template renders them."""
    return settings.model_dump(mode="json", include=set(FORM_FIELDS))


def parse_form(form: Any) -> dict[str, Any]:
    """The submitted form as Settings keyword arguments, still unvalidated."""
    values: dict[str, Any] = {
        name: name in form if name in CHECKBOX_FIELDS else form.get(name, "").strip()
        for name in FORM_FIELDS
    }
    # A cleared field means "back to the default", not "the empty string is my host".
    return {name: value for name, value in values.items() if value != ""}


def save_config(values: dict[str, Any]) -> None:
    """Validate, then atomically replace config.json. Secrets live here: keep it 0600."""
    validated = Settings(**values)
    payload = validated.model_dump(mode="json", include=set(FORM_FIELDS))
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    scratch = CONFIG_FILE.with_suffix(".json.tmp")
    scratch.write_text(json.dumps(payload, indent=2) + "\n")
    scratch.chmod(0o600)
    scratch.replace(CONFIG_FILE)


def _readable_error(err: Mapping[str, Any]) -> str:
    """One pydantic error as a sentence a person can act on."""
    field = " ".join(str(loc) for loc in err["loc"]).replace("_", " ")
    message = str(err["msg"]).removeprefix("Value error, ")
    return f"{field}: {message}" if field else message


def create_app(supervisor: Supervisor) -> Flask:
    app = Flask(__name__)
    # Honour X-Forwarded-Proto/Host from a fronting proxy (tailscale serve, caddy), so
    # generated links come out as the address the browser is really on.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1)  # type: ignore[method-assign]
    # Sessions only carry flashes, so a key that rotates on restart costs nothing.
    app.secret_key = secrets.token_hex(32)

    @app.template_filter("ago")
    def ago(seconds: float | None) -> str:
        if seconds is None:
            return "never"
        if seconds < 90:
            return f"{seconds:.0f}s ago"
        if seconds < 90 * 60:
            return f"{seconds / 60:.0f}m ago"
        if seconds < 36 * 3600:
            return f"{seconds / 3600:.1f}h ago"
        return f"{seconds / 86400:.1f}d ago"

    @app.get("/")
    def index() -> WerkzeugResponse:
        if missing_for_start(Settings()):
            return redirect(url_for("settings_page"))
        return redirect(url_for("status_page"))

    @app.get("/settings")
    def settings_page() -> str:
        settings = Settings()
        return render_template(
            "settings.html",
            values=form_values(settings),
            missing=missing_for_start(settings),
            timezones=sorted(available_timezones()),
        )

    @app.post("/settings")
    def settings_save() -> Any:
        values = parse_form(request.form)
        try:
            save_config(values)
        except ValidationError as exc:
            return render_template(
                "settings.html",
                values=values,
                missing=[],
                errors=[_readable_error(err) for err in exc.errors()],
                timezones=sorted(available_timezones()),
            )
        supervisor.restart()
        flash("Settings saved.")
        if missing_for_start(Settings()):
            return redirect(url_for("settings_page"))
        return redirect(url_for("status_page"))

    @app.get("/status")
    def status_page() -> str:
        settings = Settings()
        with Store(settings.state_db) as store:
            beat = store.last_beat()
            events = store.recent_events()
            failures = store.list_failures()
        return render_template(
            "status.html",
            running=supervisor.running,
            missing=supervisor.missing,
            error=supervisor.error,
            calendar=settings.calendar_name,
            beat_age=None if beat is None else time.time() - beat,
            events=[
                (summary, starts, time.time() - created) for summary, starts, created in events
            ],
            failures=failures,
        )

    @app.post("/retry")
    def retry() -> WerkzeugResponse:
        """Forget a failure so the message - still flagged - is tried again."""
        settings = Settings()
        with Store(settings.state_db) as store:
            message_id = request.form.get("message_id", "")
            if message_id:
                store.clear_failure(message_id)
                flash("That email will be tried again on the next pass.")
            else:
                for failure in store.list_failures():
                    store.clear_failure(failure.message_id)
                flash("All set-aside emails will be tried again on the next pass.")
        return redirect(url_for("status_page"))

    @app.post("/restart")
    def restart() -> WerkzeugResponse:
        supervisor.restart()
        flash("Watcher restarted.")
        return redirect(url_for("status_page"))

    @app.post("/check")
    def check() -> str:
        return render_template("checks.html", results=run_checks(Settings()))

    @app.get("/healthz")
    def healthz() -> Response:
        if not supervisor.running:
            # Waiting for configuration is healthy; a watcher that died is not.
            if supervisor.error:
                return Response(f"watcher stopped: {supervisor.error}\n", status=500)
            return Response("ok (waiting for configuration)\n")
        with Store(Settings().state_db) as store:
            beat = store.last_beat()
        age = None if beat is None else time.time() - beat
        if age is not None and age > HEARTBEAT_MAX_AGE_SECONDS:
            return Response(f"heartbeat is {age:.0f}s old\n", status=500)
        return Response("ok\n")

    return app
