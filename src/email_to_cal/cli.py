"""Command line entry points."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

from . import prefs
from .app import run
from .cal import CalendarClient, build_ical
from .checks import run_checks
from .config import STATE_FILE, Settings
from .llm import Extractor
from .mime import parse_email
from .store import Store

log = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _cmd_run(settings: Settings, _args: argparse.Namespace) -> int:
    stopping = threading.Event()

    def _stop(signum: int, _frame: types.FrameType | None) -> None:
        log.info("received signal %d; shutting down", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run(settings, prefs.current(), stopping)
    return 0


def _cmd_serve(_settings: Settings, args: argparse.Namespace) -> int:
    """The portal plus a supervised watcher thread; how the container runs."""
    from .web import Supervisor, create_app

    supervisor = Supervisor()
    # Under the debug reloader the process runs twice (a file watcher and the actual
    # server, marked by WERKZEUG_RUN_MAIN); only the server may own a mail watcher.
    if not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        supervisor.restart()

    def _stop(signum: int, _frame: types.FrameType | None) -> None:
        log.info("received signal %d; shutting down", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("listening on http://%s:%d", args.host, args.port)
    try:
        create_app(supervisor).run(host=args.host, port=args.port, threaded=True, debug=args.debug)
    finally:
        supervisor.stop()
    return 0


def _cmd_check(settings: Settings, _args: argparse.Namespace) -> int:
    """Exercise every external dependency, then exit. Safe to run against production."""
    current = prefs.current()
    print(f"flag colour: {current.flag_colour}")
    print(f"calendar: {current.calendar or '(not picked)'}")
    for category in current.categories:
        print(f"  {category.name} -> {category.calendar}")
    print(f"time zone: {current.timezone}")

    ok = True
    for result in run_checks(settings, current):
        if result.ok:
            print(f"{result.name}: {result.detail}")
        else:
            print(f"{result.name}: FAILED - {result.detail}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


def _cmd_replay(settings: Settings, args: argparse.Namespace) -> int:
    """Run one .eml file through extraction, as if it had been flagged."""
    current = prefs.current()
    doc = parse_email(Path(args.path).read_bytes())
    events = Extractor(settings, current).extract(doc).events
    if not events:
        print("no events found", file=sys.stderr)
        return 1

    calendar = CalendarClient(settings) if args.write else None
    calendar_urls = calendar.resolve(current.calendars) if calendar else {}

    for event in events:
        name = current.calendar_for(event.category)
        built = build_ical(event, current, message_id=doc.message_id, calendar=name)
        print(f"# {built.describe()} -> {name}")
        print(built.ics.decode())
        if calendar is not None:
            calendar.put(calendar_urls[name], built.uid, built.ics)
            print(f"# written to {name}")
    return 0


def _cmd_healthcheck(_settings: Settings, args: argparse.Namespace) -> int:
    """Used by the container HEALTHCHECK.

    `serve` mode answers via the portal's /healthz, which also covers the
    waiting-for-configuration state. When no portal is listening (headless `run`
    deployments), fall back to reading the heartbeat directly.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/healthz", timeout=5) as reply:
            print(reply.read().decode().strip())
            return 0
    except urllib.error.HTTPError as exc:
        print(exc.read().decode().strip() or str(exc), file=sys.stderr)
        return 1
    except OSError:
        pass

    with Store(STATE_FILE) as store:
        beat = store.last_beat()
    if beat is None:
        print("no heartbeat yet", file=sys.stderr)
        return 1
    age = time.time() - beat
    if age > args.max_age:
        print(f"heartbeat is {age:.0f}s old", file=sys.stderr)
        return 1
    print(f"ok ({age:.0f}s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="email-to-cal")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="watch for flagged mail and create events").set_defaults(
        func=_cmd_run
    )

    serve = sub.add_parser("serve", help="run the web portal plus the watcher")
    serve.add_argument("--host", default="127.0.0.1", help="bind address")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--debug",
        action="store_true",
        help="Flask debug mode with template/code auto-reload; development only",
    )
    serve.set_defaults(func=_cmd_serve)

    sub.add_parser("check", help="exercise every external dependency").set_defaults(func=_cmd_check)

    replay = sub.add_parser("replay", help="run a single .eml through extraction")
    replay.add_argument("path")
    replay.add_argument(
        "--write", action="store_true", help="also write the events to the calendar"
    )
    replay.set_defaults(func=_cmd_replay)

    health = sub.add_parser("healthcheck", help="check the portal, falling back to the heartbeat")
    health.add_argument("--port", type=int, default=8080, help="portal port to probe")
    health.add_argument("--max-age", type=float, default=900.0)
    health.set_defaults(func=_cmd_healthcheck)

    args = parser.parse_args(argv)
    with Store(STATE_FILE) as store:
        current = prefs.load(store)
    settings = Settings()
    _configure_logging(current.log_level)
    exit_code: int = args.func(settings, args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
