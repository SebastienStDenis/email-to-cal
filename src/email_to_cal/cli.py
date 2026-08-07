"""Command line entry points."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

from .app import Pipeline, run
from .checks import run_checks
from .config import Settings
from .gcal import CalendarClient
from .llm import Extractor
from .store import Store

log = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _stop_on_signals(stopping: threading.Event) -> None:
    def _stop(signum: int, _frame: types.FrameType | None) -> None:
        log.info("received signal %d; shutting down", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)


def _cmd_run(settings: Settings, _args: argparse.Namespace) -> int:
    stopping = threading.Event()
    _stop_on_signals(stopping)
    run(settings, stopping)
    return 0


def _cmd_serve(_settings: Settings, args: argparse.Namespace) -> int:
    """The portal plus a supervised watcher thread; how the container runs."""
    from .web import Supervisor, create_app

    supervisor = Supervisor()
    supervisor.restart()

    def _stop(signum: int, _frame: types.FrameType | None) -> None:
        log.info("received signal %d; shutting down", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("portal listening on http://%s:%d", args.host, args.port)
    try:
        create_app(supervisor).run(host=args.host, port=args.port, threaded=True)
    finally:
        supervisor.stop()
    return 0


def _cmd_check(settings: Settings, _args: argparse.Namespace) -> int:
    """Validate every external dependency, then exit. Safe to run against production."""
    print(f"categories: {len(settings.categories)} configured")
    for rule in settings.categories:
        print(f"  {rule.name} -> {rule.calendar}")
    print(f"default calendar: {settings.default_calendar}")
    print(f"default timezone: {settings.default_timezone}")

    ok = True
    for result in run_checks(settings):
        if result.ok:
            print(f"{result.name}: {result.detail}")
        else:
            print(f"{result.name}: FAILED - {result.detail}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


def _cmd_replay(settings: Settings, args: argparse.Namespace) -> int:
    """Run one .eml file through the real pipeline. Honours --dry-run."""
    raw = Path(args.path).read_bytes()
    if args.dry_run:
        settings.dry_run = True

    with Store(settings.state_db) as store:
        calendar = None if settings.dry_run else CalendarClient(settings, store)
        pipeline = Pipeline(settings, store, Extractor(settings), calendar)
        # Replay is a debugging tool: always re-run, even for mail already handled.
        outcome = pipeline.process(raw, skip_seen=False)

    print(
        json.dumps(
            {
                "subject": outcome.subject,
                "committed": outcome.committed,
                "reason": outcome.reason,
                "created": outcome.created,
                "skipped": outcome.skipped,
            },
            indent=2,
        )
    )
    return 0


def _cmd_healthcheck(settings: Settings, args: argparse.Namespace) -> int:
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

    with Store(settings.state_db) as store:
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

    sub.add_parser("run", help="watch the mailbox and create events").set_defaults(func=_cmd_run)

    serve = sub.add_parser("serve", help="run the web portal plus the watcher")
    serve.add_argument("--host", default="127.0.0.1", help="bind address")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=_cmd_serve)

    sub.add_parser("check", help="validate config and every external dependency").set_defaults(
        func=_cmd_check
    )

    replay = sub.add_parser("replay", help="run a single .eml through the pipeline")
    replay.add_argument("path")
    replay.add_argument("--dry-run", action="store_true", help="never write to Google Calendar")
    replay.set_defaults(func=_cmd_replay)

    health = sub.add_parser("healthcheck", help="check the portal, falling back to the heartbeat")
    health.add_argument("--port", type=int, default=8080, help="portal port to probe")
    health.add_argument("--max-age", type=float, default=900.0)
    health.set_defaults(func=_cmd_healthcheck)

    args = parser.parse_args(argv)
    settings = Settings()
    _configure_logging(settings.log_level)
    exit_code: int = args.func(settings, args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
