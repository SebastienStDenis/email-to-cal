"""Command line entry points."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .app import Pipeline, run
from .config import Settings
from .gcal import CalendarClient, CredentialsExpired, run_consent_flow
from .llm import Extractor
from .mailbox import Mailbox
from .store import Store


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _cmd_run(settings: Settings, _args: argparse.Namespace) -> int:
    run(settings)
    return 0


def _cmd_auth_google(settings: Settings, args: argparse.Namespace) -> int:
    credentials = Path(args.credentials or settings.google_credentials_file)
    token = Path(args.token or settings.google_token_file)
    if not credentials.exists():
        print(f"missing OAuth client file: {credentials}", file=sys.stderr)
        print(
            "Download it from the Google Cloud console (APIs & Services > Credentials >\n"
            "OAuth client ID > Desktop app), and make sure the app's publishing status is\n"
            "'In production' or the refresh token will expire after 7 days.",
            file=sys.stderr,
        )
        return 1
    run_consent_flow(credentials, token, port=args.port)
    print(f"wrote {token}")
    return 0


def _cmd_check(settings: Settings, _args: argparse.Namespace) -> int:
    """Validate every external dependency, then exit. Safe to run against production."""
    ok = True

    print(f"categories: {len(settings.categories)} configured")
    for rule in settings.categories:
        print(f"  {rule.name} -> {rule.calendar}")
    print(f"default calendar: {settings.default_calendar}")
    print(f"default timezone: {settings.default_timezone}")

    with Store(settings.state_db) as store:
        print(f"state db: {settings.state_db} ok")

        failures = store.list_failures()
        if failures:
            print(f"failed messages: {len(failures)} (retry with 'replay', or investigate)")
            for folder, _, uid, attempts, error in failures[:10]:
                print(f"  {folder} UID {uid}: {attempts} attempts, {error[:120]}")

        try:
            mailbox = Mailbox(settings, store)
            box = mailbox.connect()
            print(f"imap: connected, {len(box.uids())} messages in {settings.imap_folder}")
            mailbox.close()
        except Exception as exc:
            print(f"imap: FAILED - {exc}", file=sys.stderr)
            ok = False

        try:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            client.models.retrieve(settings.anthropic_model)
            print(f"anthropic: {settings.anthropic_model} reachable")
        except Exception as exc:
            print(f"anthropic: FAILED - {exc}", file=sys.stderr)
            ok = False

        try:
            calendar = CalendarClient(settings, store)
            wanted = {settings.default_calendar} | {r.calendar for r in settings.categories}
            for name in sorted(wanted):
                print(f"calendar {name!r} -> {calendar.resolve_calendar(name)}")
        except CredentialsExpired as exc:
            print(f"google: FAILED - {exc}", file=sys.stderr)
            ok = False
        except Exception as exc:
            print(f"google: FAILED - {exc}", file=sys.stderr)
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
    """Used by the container HEALTHCHECK: has the loop beaten recently?"""
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

    auth = sub.add_parser("auth-google", help="run the Google OAuth consent flow")
    auth.add_argument("--credentials", help="path to the OAuth client JSON")
    auth.add_argument("--token", help="where to write the token JSON")
    auth.add_argument("--port", type=int, default=0, help="loopback port (0 picks one)")
    auth.set_defaults(func=_cmd_auth_google)

    sub.add_parser("check", help="validate config and every external dependency").set_defaults(
        func=_cmd_check
    )

    replay = sub.add_parser("replay", help="run a single .eml through the pipeline")
    replay.add_argument("path")
    replay.add_argument("--dry-run", action="store_true", help="never write to Google Calendar")
    replay.set_defaults(func=_cmd_replay)

    health = sub.add_parser("healthcheck", help="check the loop heartbeat")
    health.add_argument("--max-age", type=float, default=900.0)
    health.set_defaults(func=_cmd_healthcheck)

    args = parser.parse_args(argv)
    settings = Settings()
    _configure_logging(settings.log_level)
    exit_code: int = args.func(settings, args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
