from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from emt_collector.api.client import (
    EMTAuthError,
    EMTClient,
    EMTError,
    EMTResponseError,
    EMTTransientError,
)
from emt_collector.api.models import Arrive, LineInfo
from emt_collector.config import Settings
from emt_collector.db.repository import Repository, utcnow

log = structlog.get_logger(__name__)

MADRID_TZ = ZoneInfo("Europe/Madrid")


def normalize_line(value: str) -> str:
    """'001' -> '1', 'c1' -> 'C1'. Used to match user config against API ids/labels."""
    value = value.strip().upper()
    stripped = value.lstrip("0")
    return stripped or "0"


def to_utc(sample: datetime | None, fallback: datetime) -> datetime:
    """API timestamps are naive Europe/Madrid local time; store everything in UTC."""
    if sample is None:
        return fallback
    if sample.tzinfo is None:
        sample = sample.replace(tzinfo=MADRID_TZ)
    return sample.astimezone(timezone.utc)


def classify_error(exc: Exception) -> str:
    if isinstance(exc, EMTAuthError):
        return "auth"
    if isinstance(exc, EMTTransientError):
        return "network"
    if isinstance(exc, EMTResponseError):
        return f"api_error_{exc.code}"
    if isinstance(exc, EMTError):
        return "api_error"
    return "unexpected"


@dataclass
class CycleResult:
    cycle_id: int | None
    started_at: datetime
    finished_at: datetime
    status: str
    stops_requested: int
    stops_ok: int
    stops_failed: int
    positions_inserted: int
    arrivals_inserted: int
    positions_seen: int
    arrivals_seen: int
    gaps: int
    error: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


class Collector:
    """Runs one collection cycle: poll every target stop, persist positions and arrivals."""

    def __init__(self, settings: Settings, client: EMTClient, repo: Repository) -> None:
        self._settings = settings
        self._client = client
        self._repo = repo
        self._target_stops: list[str] | None = None
        self._line_filter: set[str] | None = None
        self._stops_resolved_at: datetime | None = None

    # -- target resolution -----------------------------------------------------------------

    def _resolve_line_ids(self) -> tuple[list[LineInfo], set[str]]:
        wanted = {normalize_line(x) for x in self._settings.emt_lines}
        if not wanted:
            return [], set()
        lines = self._client.list_lines()
        matched = [
            info
            for info in lines
            if normalize_line(info.line) in wanted or normalize_line(info.label) in wanted
        ]
        found = {normalize_line(i.label) for i in matched} | {
            normalize_line(i.line) for i in matched
        }
        missing = sorted(wanted - found)
        if missing:
            log.warning("collector.lines_not_found", lines=missing)
        labels = {normalize_line(i.label) for i in matched}
        return matched, labels

    def resolve_targets(self, force: bool = False) -> list[str]:
        """Return the stop ids to poll, refreshing the cache when it is stale."""
        max_age = self._settings.emt_stops_refresh_hours * 3600
        now = utcnow()
        if (
            not force
            and self._target_stops is not None
            and self._stops_resolved_at is not None
            and (now - self._stops_resolved_at).total_seconds() < max_age
        ):
            return self._target_stops

        stops: dict[str, dict[str, Any]] = {}
        for stop_id in self._settings.emt_stops:
            stops[stop_id] = {"stop_id": stop_id, "lines": None}

        line_filter: set[str] | None = None
        if self._settings.emt_lines:
            try:
                matched, labels = self._resolve_line_ids()
                line_filter = labels or {normalize_line(x) for x in self._settings.emt_lines}
                for info in matched:
                    for direction in (1, 2):
                        result = self._client.line_stops(info.line, direction)
                        for s in result.stops:
                            row = stops.setdefault(
                                s.stop, {"stop_id": s.stop, "name": s.name, "lines": set()}
                            )
                            row.setdefault("name", s.name)
                            if s.geometry is not None:
                                row["lat"], row["lon"] = s.geometry.lat, s.geometry.lon
                            lines_set = row.get("lines")
                            if isinstance(lines_set, set):
                                lines_set.add(info.label)
            except EMTError as exc:
                cached = self._repo.cached_stops()
                log.error(
                    "collector.stops_refresh_failed",
                    error=str(exc),
                    cached_stops=len(cached),
                )
                self._repo.record_gap(
                    scope="scheduler", kind="stops_refresh_failed", detail=str(exc)
                )
                if self._target_stops:
                    return self._target_stops
                if cached:
                    self._target_stops = sorted(s.stop_id for s in cached)
                    self._line_filter = {normalize_line(x) for x in self._settings.emt_lines}
                    self._stops_resolved_at = now
                    return self._target_stops
                raise

        rows = []
        for row in stops.values():
            lines_val = row.get("lines")
            rows.append(
                {
                    "stop_id": row["stop_id"],
                    "name": row.get("name"),
                    "lat": row.get("lat"),
                    "lon": row.get("lon"),
                    "lines": ",".join(sorted(lines_val)) if isinstance(lines_val, set) else None,
                    "updated_at": now,
                }
            )
        self._repo.upsert_stops(rows)
        self._target_stops = sorted(stops)
        self._line_filter = line_filter
        self._stops_resolved_at = now
        self._warn_budget(len(self._target_stops))
        log.info(
            "collector.targets_resolved",
            stops=len(self._target_stops),
            lines=sorted(line_filter) if line_filter else "all",
        )
        return self._target_stops

    def _warn_budget(self, stops: int) -> None:
        per_day = stops * self._settings.cycles_per_day
        budget = self._settings.emt_daily_request_budget
        token = self._client.token
        if token is not None and token.daily_quota:
            budget = min(budget, token.daily_quota)
        if per_day > budget:
            log.warning(
                "collector.daily_budget_exceeded",
                projected_requests_per_day=int(per_day),
                budget=budget,
                hint="reduce EMT_LINES/EMT_STOPS or raise COLLECT_INTERVAL_SECONDS",
            )
        per_minute = stops * 60 / self._settings.collect_interval_seconds
        if per_minute > self._settings.emt_max_requests_per_minute:
            log.warning(
                "collector.cycle_slower_than_rate_limit",
                stops=stops,
                requests_per_minute_needed=int(per_minute),
                max_requests_per_minute=self._settings.emt_max_requests_per_minute,
            )

    # -- cycle -----------------------------------------------------------------------------

    def _keep(self, arrive: Arrive) -> bool:
        return self._line_filter is None or normalize_line(arrive.line) in self._line_filter

    def run_cycle(self) -> CycleResult:
        started_at = utcnow()
        stats_before = self._client.stats.snapshot()
        cycle_log = log.bind(cycle_started_at=started_at.isoformat())
        cycle_log.info("cycle.start")

        try:
            stops = self.resolve_targets()
        except Exception as exc:  # noqa: BLE001 - recorded as a gap, scheduler keeps running
            finished = utcnow()
            self._repo.record_gap(
                scope="cycle", kind="targets_unresolved", detail=str(exc), occurred_at=started_at
            )
            cycle_log.error(
                "cycle.failed", error=str(exc), duration=(finished - started_at).total_seconds()
            )
            return CycleResult(
                cycle_id=None,
                started_at=started_at,
                finished_at=finished,
                status="failed",
                stops_requested=0,
                stops_ok=0,
                stops_failed=0,
                positions_inserted=0,
                arrivals_inserted=0,
                positions_seen=0,
                arrivals_seen=0,
                gaps=1,
                error=str(exc),
            )

        cycle_id = self._repo.start_cycle(started_at, len(stops))
        cycle_log = cycle_log.bind(cycle_id=cycle_id)

        positions: dict[tuple[str, int], dict[str, Any]] = {}
        arrivals: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        stops_ok = 0
        aborted: str | None = None

        for stop_id in stops:
            try:
                resp = self._client.stop_arrivals(stop_id)
            except EMTAuthError as exc:
                aborted = f"auth failure after re-login: {exc}"
                gaps.append(_gap(cycle_id, "stop", "auth", str(exc), stop_id=stop_id))
                cycle_log.error("cycle.aborted_auth", stop=stop_id, error=str(exc))
                break
            except Exception as exc:  # noqa: BLE001
                kind = classify_error(exc)
                gaps.append(_gap(cycle_id, "stop", kind, str(exc), stop_id=stop_id))
                cycle_log.warning("stop.failed", stop=stop_id, kind=kind, error=str(exc))
                continue

            stops_ok += 1
            ingested_at = utcnow()
            sample_ts = to_utc(resp.server_time, ingested_at)
            for a in resp.arrivals:
                if not self._keep(a):
                    continue
                arrivals.append(
                    {
                        "stop_id": a.stop or stop_id,
                        "line": a.line,
                        "bus_id": a.bus,
                        "sample_ts": sample_ts,
                        "ingested_at": ingested_at,
                        "estimate_seconds": a.estimate_arrive if a.has_estimate else None,
                        "distance_m": a.distance_bus,
                        "destination": a.destination,
                        "is_head": a.is_head,
                        "deviation": a.deviation,
                        "cycle_id": cycle_id,
                    }
                )
                if a.has_position and a.geometry is not None:
                    key = (a.line, a.bus)
                    if key not in positions:
                        positions[key] = {
                            "line": a.line,
                            "bus_id": a.bus,
                            "sample_ts": sample_ts,
                            "ingested_at": ingested_at,
                            "lat": a.geometry.lat,
                            "lon": a.geometry.lon,
                            "destination": a.destination,
                            "position_type": a.position_type_bus,
                            "observed_from_stop": a.stop or stop_id,
                            "cycle_id": cycle_id,
                        }

        positions_inserted = arrivals_inserted = 0
        error: str | None = aborted
        try:
            positions_inserted = self._repo.insert_positions(list(positions.values()))
            arrivals_inserted = self._repo.insert_arrivals(arrivals)
        except Exception as exc:  # noqa: BLE001
            error = f"db insert failed: {exc}"
            gaps.append(_gap(cycle_id, "cycle", "db_error", str(exc)))
            cycle_log.error("cycle.db_insert_failed", error=str(exc))

        stops_failed = len(stops) - stops_ok
        db_failed = error is not None and error.startswith("db insert failed")
        if aborted or db_failed or stops_ok == 0:
            status = "failed"
        elif stops_failed > 0:
            status = "partial"
        elif not arrivals:
            status = "empty"
        else:
            status = "ok"

        if status == "failed" and not aborted and not any(g["scope"] == "cycle" for g in gaps):
            gaps.append(_gap(cycle_id, "cycle", "all_stops_failed", None))
        elif status == "empty":
            gaps.append(_gap(cycle_id, "cycle", "empty", "all stops answered but no arrivals"))

        finished_at = utcnow()
        stats_after = self._client.stats.snapshot()
        delta = {k: stats_after[k] - stats_before[k] for k in stats_after}

        try:
            self._repo.record_gaps(gaps)
            self._repo.finish_cycle(
                cycle_id,
                finished_at=finished_at,
                status=status,
                stops_ok=stops_ok,
                stops_failed=stops_failed,
                api_requests=int(delta["requests"]),
                api_retries=int(delta["retries"]),
                reauths=int(delta["reauths"]),
                positions_inserted=positions_inserted,
                arrivals_inserted=arrivals_inserted,
                error=error,
            )
        except Exception as exc:  # noqa: BLE001
            cycle_log.error("cycle.bookkeeping_failed", error=str(exc))

        result = CycleResult(
            cycle_id=cycle_id,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            stops_requested=len(stops),
            stops_ok=stops_ok,
            stops_failed=stops_failed,
            positions_inserted=positions_inserted,
            arrivals_inserted=arrivals_inserted,
            positions_seen=len(positions),
            arrivals_seen=len(arrivals),
            gaps=len(gaps),
            error=error,
            stats=delta,
        )
        cycle_log.info(
            "cycle.end",
            status=status,
            duration_seconds=round(result.duration_seconds, 2),
            stops_requested=len(stops),
            stops_ok=stops_ok,
            stops_failed=stops_failed,
            positions_seen=len(positions),
            positions_inserted=positions_inserted,
            arrivals_seen=len(arrivals),
            arrivals_inserted=arrivals_inserted,
            gaps=len(gaps),
            **delta,
        )
        if result.duration_seconds > self._settings.collect_interval_seconds:
            cycle_log.warning(
                "cycle.overrun",
                duration_seconds=round(result.duration_seconds, 2),
                interval_seconds=self._settings.collect_interval_seconds,
            )
        return result


def _gap(
    cycle_id: int,
    scope: str,
    kind: str,
    detail: str | None,
    *,
    stop_id: str | None = None,
    line: str | None = None,
) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "occurred_at": utcnow(),
        "scope": scope,
        "stop_id": stop_id,
        "line": line,
        "kind": kind,
        "detail": (detail or "")[:2000] or None,
    }
