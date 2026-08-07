"""The portal: configure the service, connect Google, and watch it work.

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
from urllib.parse import urlsplit
from zoneinfo import available_timezones

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from google_auth_oauthlib.flow import Flow
from pydantic import ValidationError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response as WerkzeugResponse

from .app import run
from .checks import run_checks
from .config import CONFIG_FILE, Settings
from .gcal import SCOPES, client_config, save_token
from .store import Store

log = logging.getLogger(__name__)

# The settings the form manages; exactly these end up in config.json.
FORM_FIELDS = [
    "imap_host",
    "imap_port",
    "imap_username",
    "imap_password",
    "imap_folder",
    "imap_idle_seconds",
    "first_run_lookback_days",
    "sweep_folders",
    "sweep_interval_minutes",
    "anthropic_api_key",
    "anthropic_model",
    "anthropic_effort",
    "enable_vision",
    "max_attachment_mb",
    "min_confidence",
    "google_client_id",
    "google_client_secret",
    "default_calendar",
    "default_timezone",
    "categories",
    "dry_run",
    "log_level",
]

# The watcher heartbeats at least once per IDLE cycle (default 300s); well past that
# means it is wedged, not slow.
HEARTBEAT_MAX_AGE_SECONDS = 900.0


def missing_for_start(settings: Settings) -> list[str]:
    """What still has to be configured before the watcher can run."""
    missing = []
    if not settings.imap_username or not settings.imap_password:
        missing.append("iCloud credentials")
    if not settings.anthropic_api_key:
        missing.append("Anthropic API key")
    if not settings.dry_run and not settings.google_token_file.exists():
        missing.append("Google authorisation")
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
        # IDLE is sliced at 10s, so a healthy watcher notices well inside this window.
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
    values = settings.model_dump(mode="json", include=set(FORM_FIELDS))
    values["sweep_folders"] = ", ".join(settings.sweep_folders)
    return values


def parse_form(form: Any) -> dict[str, Any]:
    """The submitted form as Settings keyword arguments, still unvalidated."""
    values: dict[str, Any] = {}
    for name in FORM_FIELDS:
        if name in ("enable_vision", "dry_run"):
            values[name] = name in form
        elif name == "categories":
            values[name] = [
                {"name": n, "description": d, "calendar": c}
                for n, d, c in zip(
                    form.getlist("category_name"),
                    form.getlist("category_description"),
                    form.getlist("category_calendar"),
                    strict=True,
                )
                if n.strip() or d.strip() or c.strip()
            ]
        else:
            values[name] = form.get(name, "").strip()
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


def _oauth_redirect_uri() -> str:
    return url_for("google_callback", _external=True)


def create_app(supervisor: Supervisor) -> Flask:
    app = Flask(__name__)
    # Honour X-Forwarded-Proto/Host from a fronting proxy (tailscale serve, caddy), so
    # the Google redirect URI comes out as the https address the browser is really on.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1)  # type: ignore[method-assign]
    # Sessions only carry flashes and the OAuth state token, so a key that rotates on
    # restart costs nothing.
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
        settings = Settings()
        if missing_for_start(settings):
            return redirect(url_for("settings_page"))
        return redirect(url_for("status_page"))

    @app.get("/settings")
    def settings_page() -> str:
        settings = Settings()
        return render_template(
            "settings.html",
            values=form_values(settings),
            google_connected=settings.google_token_file.exists(),
            missing=missing_for_start(settings),
            timezones=sorted(available_timezones()),
        )

    @app.post("/settings")
    def settings_save() -> Any:
        values = parse_form(request.form)
        try:
            save_config(values)
        except ValidationError as exc:
            errors = [_readable_error(err) for err in exc.errors()]
            return render_template(
                "settings.html",
                values=values,
                google_connected=Settings().google_token_file.exists(),
                missing=[],
                errors=errors,
                timezones=sorted(available_timezones()),
            )
        supervisor.restart()
        settings = Settings()

        if request.form.get("action") == "connect":
            if not settings.google_client_id or not settings.google_client_secret:
                flash("Enter the Google client id and secret to connect.")
                return redirect(url_for("settings_page"))
            parts = urlsplit(request.url)
            local = parts.hostname in ("localhost", "127.0.0.1", "::1")
            if not local and parts.scheme != "https":
                # Google accepts loopback redirects (Desktop clients) and registered
                # https redirects (Web application clients); a plain-http remote host
                # is neither, and letting it through ends on a cryptic policy page.
                port = parts.port or 80
                flash(
                    "Your settings were saved, but Google cannot connect from "
                    f"http://{parts.hostname}. Either run "
                    f"'ssh -L {port}:localhost:{port} {parts.hostname}' on your "
                    f"computer and connect from http://localhost:{port}/settings "
                    "(one time), or serve this page over HTTPS and use a Web "
                    "application client; see the Google section's walkthrough."
                )
                return redirect(url_for("settings_page"))
            flow = Flow.from_client_config(
                client_config(settings), scopes=SCOPES, redirect_uri=_oauth_redirect_uri()
            )
            auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
            session["oauth_state"] = state
            return redirect(auth_url)

        flash("Settings saved.")
        if missing_for_start(settings):
            return redirect(url_for("settings_page"))
        return redirect(url_for("status_page"))

    @app.get("/google/callback")
    def google_callback() -> WerkzeugResponse:
        settings = Settings()
        flow = Flow.from_client_config(
            client_config(settings),
            scopes=SCOPES,
            redirect_uri=_oauth_redirect_uri(),
            state=session.pop("oauth_state", None),
        )
        try:
            # The redirect arrives over plain http because this is a loopback flow;
            # oauthlib insists on https, so present it as such (the token exchange
            # itself really is https). Same trick InstalledAppFlow uses.
            flow.fetch_token(authorization_response=request.url.replace("http://", "https://", 1))
        except Exception as exc:
            log.warning("Google authorisation failed", exc_info=True)
            flash(f"Google authorisation failed: {exc}")
            return redirect(url_for("settings_page"))
        save_token(settings, flow.credentials)
        supervisor.restart()
        flash("Google Calendar connected.")
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
            dry_run=settings.dry_run,
            beat_age=None if beat is None else time.time() - beat,
            events=[
                (summary, calendar_id, time.time() - created)
                for summary, calendar_id, created in events
            ],
            failures=failures,
        )

    @app.post("/restart")
    def restart() -> WerkzeugResponse:
        supervisor.restart()
        flash("Watcher restarted.")
        return redirect(url_for("status_page"))

    @app.post("/check")
    def check() -> str:
        settings = Settings()
        return render_template("checks.html", results=run_checks(settings))

    @app.get("/healthz")
    def healthz() -> Response:
        if not supervisor.running:
            # Waiting for configuration is healthy; a watcher that died is not.
            if supervisor.error:
                return Response(f"watcher stopped: {supervisor.error}\n", status=500)
            return Response("ok (waiting for configuration)\n")
        settings = Settings()
        with Store(settings.state_db) as store:
            beat = store.last_beat()
        age = None if beat is None else time.time() - beat
        if age is not None and age > HEARTBEAT_MAX_AGE_SECONDS:
            return Response(f"heartbeat is {age:.0f}s old\n", status=500)
        return Response("ok\n")

    return app
