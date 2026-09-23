from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone

import structlog
from pydantic import ValidationError

from emt_collector.analysis.stats import default_window, history_stats
from emt_collector.api.client import EMTClient, EMTError
from emt_collector.collector import Collector
from emt_collector.config import ConfigError, Settings
from emt_collector.db.repository import Repository, init_schema, make_engine
from emt_collector.logging_setup import configure_logging
from emt_collector.scheduler import run_forever

log = structlog.get_logger("emt_collector")


def build_client(settings: Settings) -> EMTClient:
    return EMTClient(
        settings.emt_base_url,
        email=settings.emt_email,
        password=settings.emt_password,
        client_id=settings.emt_client_id,
        pass_key=settings.emt_pass_key,
        max_requests_per_minute=settings.emt_max_requests_per_minute,
        timeout=settings.emt_request_timeout_seconds,
        max_retries=settings.emt_max_retries,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="emt-collector", description="EMT Madrid data collector")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="long-running process: collect every COLLECT_INTERVAL_SECONDS")
    sub.add_parser("once", help="run a single collection cycle and exit (useful for cron)")
    sub.add_parser("init-db", help="create tables (and Timescale hypertables) and exit")
    sub.add_parser("check", help="login, print quota info and resolved targets, no DB writes")
    sub.add_parser("lines", help="print the EMT line catalogue as JSON")
    stats = sub.add_parser(
        "stats", help="coverage and quality of the stored history (no API calls)"
    )
    stats.add_argument("--days", type=int, default=30, help="window ending now (default 30)")
    return parser


NEEDS_API = {"lines", "check", "once", "run"}
NEEDS_TARGETS = {"check", "once", "run"}
NEEDS_DB = {"init-db", "once", "run", "stats"}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except EMTError as exc:
        log.error("emt.fatal", command=args.command, error=str(exc))
        print(f"EMT API error: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    try:
        settings = Settings()
        if args.command in NEEDS_API:
            settings.require_credentials()
        if args.command in NEEDS_TARGETS:
            settings.require_targets()
        if args.command == "run":
            settings.require_interval()
        database_url = settings.resolved_database_url() if args.command in NEEDS_DB else ""
    except (ValidationError, ConfigError) as exc:
        print(f"Invalid configuration:\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, settings.log_format)

    if args.command == "lines":
        with build_client(settings) as client:
            print(
                json.dumps(
                    [m.model_dump() for m in client.list_lines()], ensure_ascii=False, indent=1
                )
            )
        return 0

    if args.command == "check":
        with build_client(settings) as client:
            token = client.login()
            print(
                json.dumps(
                    {
                        "login": "ok",
                        "daily_quota": token.daily_quota,
                        "used_today": token.used_today,
                        "lines": settings.emt_lines,
                        "explicit_stops": settings.emt_stops,
                        "interval_seconds": settings.collect_interval_seconds,
                    },
                    indent=1,
                )
            )
        return 0

    engine = make_engine(database_url)
    timescale = init_schema(engine, use_timescale=settings.db_use_timescale)
    log.info("db.ready", url=_redact(database_url), timescale=timescale)
    if args.command == "init-db":
        return 0
    if args.command == "stats":
        start, end = default_window(args.days, datetime.now(timezone.utc))
        report = history_stats(
            engine,
            start,
            end,
            settings.collect_interval_seconds,
            settings.emt_daily_request_budget,
        )
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if not report["hints"] else 3

    repo = Repository(engine)
    with build_client(settings) as client:
        collector = Collector(settings, client, repo)
        if args.command == "once":
            result = collector.run_cycle()
            print(json.dumps(asdict(result), default=str, indent=1))
            return 0 if result.status in {"ok", "partial", "empty"} else 1
        run_forever(collector, repo, settings.collect_interval_seconds)
    return 0


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


if __name__ == "__main__":
    sys.exit(main())
