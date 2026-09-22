from __future__ import annotations

import signal
from datetime import datetime, timezone
from types import FrameType

import structlog
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from emt_collector.collector import Collector
from emt_collector.db.repository import Repository

log = structlog.get_logger(__name__)

JOB_ID = "emt_collect_cycle"


def build_scheduler(
    collector: Collector, repo: Repository, interval_seconds: int
) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        collector.run_cycle,
        trigger=IntervalTrigger(seconds=interval_seconds, timezone="UTC"),
        id=JOB_ID,
        name="EMT collection cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=max(5, interval_seconds // 2),
        next_run_time=datetime.now(timezone.utc),
    )

    def _on_event(event: JobExecutionEvent) -> None:
        if event.code == EVENT_JOB_MISSED:
            log.warning("scheduler.job_missed", scheduled=str(event.scheduled_run_time))
            _safe_gap(repo, "job_missed", f"scheduled={event.scheduled_run_time}")
        elif event.code == EVENT_JOB_ERROR:
            log.error("scheduler.job_error", error=str(event.exception))
            _safe_gap(repo, "job_error", str(event.exception))

    scheduler.add_listener(_on_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    return scheduler


def _safe_gap(repo: Repository, kind: str, detail: str) -> None:
    try:
        repo.record_gap(scope="scheduler", kind=kind, detail=detail)
    except Exception as exc:  # noqa: BLE001
        log.error("scheduler.gap_record_failed", error=str(exc))


def run_forever(collector: Collector, repo: Repository, interval_seconds: int) -> None:
    scheduler = build_scheduler(collector, repo, interval_seconds)

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("scheduler.stopping", signal=signal.Signals(signum).name)
        scheduler.shutdown(wait=True)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("scheduler.start", interval_seconds=interval_seconds)
    scheduler.start()
    log.info("scheduler.stopped")
