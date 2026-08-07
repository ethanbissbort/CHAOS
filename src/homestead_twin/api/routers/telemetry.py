"""Telemetry read API and the hardware-free simulate hook (SDD 41).

Point IDs are ``<asset_id>/<point_name>`` (SDD 26.2) and therefore contain a
slash, so every point route declares ``{point_id:path}``.

**Two addresses per point resource.** ``GET /points/{point_id:path}/current`` is
the canonical shape from SDD 41, and it is what clients should use. It is also
served under ``/telemetry/points/{point_id:path}/...``. The ``/points/`` prefix
is shared with the registry router, which is registered ahead of this module
(routers are discovered alphabetically); Starlette matches in declaration order
with no most-specific-route rule, so any catch-all or three-segment pattern the
registry declares there silently swallows these sub-resources -- which has
already happened once during development. The alias lives in the namespace this
module owns outright and therefore cannot be shadowed by another subsystem.

Everything here is read-only except ``POST /telemetry/simulate``, which exists so
the twin can be exercised, demonstrated and commissioned before a single sensor
is wired (SDD 19, 20 phase A). Simulated samples are written with
``source="api.simulate"`` and are never attributed to a device: an operator
looking at a value must always be able to tell whether the property actually
reported it.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from homestead_twin.api.deps import AppSettings, DbSession, OperatorPrincipal
from homestead_twin.envelope import TelemetryEnvelope
from homestead_twin.ingest.writer import TelemetryWriter, as_utc
from homestead_twin.models.base import utcnow
from homestead_twin.models.registry import Asset, Point, PointSampleIndex
from homestead_twin.models.telemetry import CurrentState, IngestDeadLetter, TelemetrySample

router = APIRouter(tags=["telemetry"])

#: Provenance stamped on every simulated value.
SIMULATED_SOURCE = "api.simulate"

#: Qualities the operator UI treats as "this reading cannot be trusted".
UNTRUSTED_QUALITIES = ("stale", "bad")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CurrentStateOut(BaseModel):
    point_id: str
    asset_id: str
    point_name: str
    value: Any = None
    unit: str | None = None
    quality: str
    source: str | None = None
    sequence: int | None = None
    ts: dt.datetime | None = None
    received_at: dt.datetime | None = None
    stale_after_s: int | None = None
    age_s: float | None = None
    alarm_state: str | None = None

    @classmethod
    def from_row(cls, row: CurrentState, now: dt.datetime) -> "CurrentStateOut":
        ts = as_utc(row.ts)
        return cls(
            point_id=row.point_id,
            asset_id=row.asset_id,
            point_name=row.point_name,
            value=row.value,
            unit=row.unit,
            quality=row.quality,
            source=row.source,
            sequence=row.sequence,
            ts=ts,
            received_at=as_utc(row.received_at),
            stale_after_s=row.stale_after_s,
            age_s=round((now - ts).total_seconds(), 3) if ts else None,
            alarm_state=row.alarm_state,
        )


class SampleOut(BaseModel):
    point_id: str
    ts: dt.datetime
    value: Any = None
    unit: str | None = None
    quality: str
    source: str | None = None

    @classmethod
    def from_row(cls, row: TelemetrySample) -> "SampleOut":
        value: Any = row.value_numeric
        if value is None:
            value = row.value_bool if row.value_bool is not None else row.value_text
        return cls(
            point_id=row.point_id,
            ts=as_utc(row.ts),
            value=value,
            unit=row.unit,
            quality=row.quality,
            source=row.source,
        )


class HistoryOut(BaseModel):
    point_id: str
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    count: int
    truncated: bool = Field(
        default=False, description="True when older samples exist beyond `limit`."
    )
    samples: list[SampleOut]


class DeadLetterOut(BaseModel):
    id: str
    topic: str
    reason: str
    payload: str | None = None
    created_at: dt.datetime | None = None


class SimulateOut(BaseModel):
    point_id: str
    accepted: bool
    quality: str
    current_state_updated: bool
    historised: bool
    out_of_order: bool
    sequence_gap: int | None = None
    problems: list[str] = Field(default_factory=list)
    source: str = SIMULATED_SOURCE


# ---------------------------------------------------------------------------
# Current state
# ---------------------------------------------------------------------------


@router.get("/telemetry/current", response_model=list[CurrentStateOut], summary="Bulk current state")
def list_current_state(
    session: DbSession,
    asset_id: Annotated[str | None, Query(description="Exact asset ID")] = None,
    domain: Annotated[str | None, Query(description="Asset domain, e.g. energy")] = None,
    point_name: Annotated[str | None, Query(description="Exact canonical point name")] = None,
    quality: Annotated[str | None, Query(description="Filter by quality code")] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> list[CurrentStateOut]:
    """Current value of every matching point (SDD 40.2)."""
    stmt = select(CurrentState)
    if asset_id:
        stmt = stmt.where(CurrentState.asset_id == asset_id)
    if point_name:
        stmt = stmt.where(CurrentState.point_name == point_name)
    if quality:
        stmt = stmt.where(CurrentState.quality == quality)
    if domain:
        stmt = stmt.join(Asset, Asset.asset_id == CurrentState.asset_id).where(Asset.domain == domain)
    stmt = stmt.order_by(CurrentState.point_id).limit(limit)
    now = utcnow()
    return [CurrentStateOut.from_row(row, now) for row in session.execute(stmt).scalars().all()]


@router.get(
    "/telemetry/stale",
    response_model=list[CurrentStateOut],
    summary="Points whose value cannot be trusted",
)
def list_stale(
    session: DbSession,
    quality: Annotated[
        list[str] | None, Query(description="Quality codes to include; defaults to stale + bad")
    ] = None,
    asset_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> list[CurrentStateOut]:
    """The operator's "what is not reporting" list (SDD 5.5, 26.6).

    A dead sensor is an operational condition, not a blank space on a
    dashboard, so this endpoint is the UI's source for it.
    """
    wanted = [q for q in (quality or UNTRUSTED_QUALITIES) if q]
    stmt = select(CurrentState).where(CurrentState.quality.in_(wanted))
    if asset_id:
        stmt = stmt.where(CurrentState.asset_id == asset_id)
    stmt = stmt.order_by(CurrentState.ts.desc().nullsfirst()).limit(limit)
    now = utcnow()
    return [CurrentStateOut.from_row(row, now) for row in session.execute(stmt).scalars().all()]


@router.get(
    "/points/{point_id:path}/current",
    response_model=CurrentStateOut,
    summary="Current value of one point",
)
@router.get(
    "/telemetry/points/{point_id:path}/current",
    response_model=CurrentStateOut,
    summary="Current value of one point (unshadowed alias)",
)
def get_current_state(point_id: str, session: DbSession) -> CurrentStateOut:
    row = session.get(CurrentState, point_id)
    if row is None:
        # Distinguish "never reported" from "not a point at all"; during
        # commissioning that difference is the whole diagnosis.
        if session.get(Point, point_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown point: {point_id}")
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Point {point_id} is registered but has never reported"
        )
    return CurrentStateOut.from_row(row, utcnow())


@router.get(
    "/points/{point_id:path}/history",
    response_model=HistoryOut,
    summary="Historian samples for one point",
)
@router.get(
    "/telemetry/points/{point_id:path}/history",
    response_model=HistoryOut,
    summary="Historian samples for one point (unshadowed alias)",
)
def get_history(
    point_id: str,
    session: DbSession,
    start: Annotated[dt.datetime | None, Query(description="Inclusive lower bound (UTC)")] = None,
    end: Annotated[dt.datetime | None, Query(description="Exclusive upper bound (UTC)")] = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
    source: Annotated[
        str | None, Query(description='Filter by source, e.g. "downsample:1min"')
    ] = None,
) -> HistoryOut:
    """The most recent ``limit`` samples in range, returned oldest-first.

    Newest-first is the right thing to *select* (a chart wants the latest data
    when the range is wider than the limit) and oldest-first is the right thing
    to *return* (callers plot it directly), so the window is taken from the end
    and then reversed.
    """
    if session.get(Point, point_id) is None and session.get(CurrentState, point_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown point: {point_id}")

    stmt = select(TelemetrySample).where(TelemetrySample.point_id == point_id)
    if start:
        stmt = stmt.where(TelemetrySample.ts >= as_utc(start))
    if end:
        stmt = stmt.where(TelemetrySample.ts < as_utc(end))
    if source:
        stmt = stmt.where(TelemetrySample.source == source)

    rows = (
        session.execute(stmt.order_by(TelemetrySample.ts.desc()).limit(limit + 1)).scalars().all()
    )
    truncated = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    return HistoryOut(
        point_id=point_id,
        start=as_utc(start),
        end=as_utc(end),
        count=len(rows),
        truncated=truncated,
        samples=[SampleOut.from_row(row) for row in rows],
    )


# ---------------------------------------------------------------------------
# Commissioning aids
# ---------------------------------------------------------------------------


@router.get(
    "/telemetry/dead-letters",
    response_model=list[DeadLetterOut],
    summary="Recent ingest failures",
)
def list_dead_letters(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    topic: Annotated[str | None, Query(description="Substring match on topic")] = None,
    reason: Annotated[str | None, Query(description="Substring match on reason")] = None,
) -> list[DeadLetterOut]:
    """Why messages were rejected -- the first thing to check when a point is silent."""
    stmt = select(IngestDeadLetter)
    if topic:
        stmt = stmt.where(IngestDeadLetter.topic.contains(topic))
    if reason:
        stmt = stmt.where(IngestDeadLetter.reason.contains(reason))
    rows = (
        session.execute(stmt.order_by(IngestDeadLetter.created_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [
        DeadLetterOut(
            id=row.id,
            topic=row.topic,
            reason=row.reason,
            payload=row.payload,
            created_at=as_utc(row.created_at),
        )
        for row in rows
    ]


@router.get("/telemetry/stats", summary="Ingest counters and historian volume")
def get_stats(request: Request, session: DbSession) -> dict[str, Any]:
    """Ingest health for the operator UI and for ``/health`` style checks."""
    service = _ingest_service(request)
    quality_counts = dict(
        session.execute(
            select(CurrentState.quality, func.count()).group_by(CurrentState.quality)
        ).all()
    )
    return {
        "ingest": service.stats() if service is not None else None,
        "ingest_running": bool(service and service.running),
        "database": {
            "points": session.execute(select(func.count()).select_from(Point)).scalar_one(),
            "current_state_rows": session.execute(
                select(func.count()).select_from(CurrentState)
            ).scalar_one(),
            "samples": session.execute(
                select(func.count()).select_from(TelemetrySample)
            ).scalar_one(),
            "indexed_series": session.execute(
                select(func.count()).select_from(PointSampleIndex)
            ).scalar_one(),
            "dead_letters": session.execute(
                select(func.count()).select_from(IngestDeadLetter)
            ).scalar_one(),
            "quality": quality_counts,
        },
        "generated_at": utcnow(),
    }


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


@router.post(
    "/telemetry/simulate",
    response_model=SimulateOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Inject a telemetry value without hardware",
)
def simulate(
    envelope: TelemetryEnvelope,
    principal: OperatorPrincipal,
    session: DbSession,
    settings: AppSettings,
) -> SimulateOut:
    """Push one envelope through the real ingest writer.

    Same validation, same quality model, same historian policy as a message off
    the broker -- only the provenance differs. ``source`` is forced to
    ``api.simulate`` regardless of what the caller sent, so no simulated value
    can ever be mistaken for something the property measured.
    """
    writer = TelemetryWriter(settings, cache_metadata=False)
    result = writer.apply(session, envelope, source=SIMULATED_SOURCE)
    if not result.known_point:
        session.rollback()
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Unknown point: {envelope.point_id}"
        )
    _record_problems(session, envelope, result, principal.name)
    session.commit()
    return SimulateOut(
        point_id=result.point_id,
        accepted=result.ok,
        quality=result.quality,
        current_state_updated=result.current_state_updated,
        historised=result.historised,
        out_of_order=result.out_of_order,
        sequence_gap=result.sequence_gap,
        problems=[p.reason for p in result.problems],
    )


def _record_problems(session: Session, envelope: TelemetryEnvelope, result, actor: str) -> None:
    """Simulated faults are dead-lettered too, so the aid works on itself."""
    for reason in result.dead_letter_reasons:
        session.add(
            IngestDeadLetter(
                topic=f"{SIMULATED_SOURCE}/{envelope.point_id}"[:400],
                payload=envelope.to_json()[:2000],
                reason=f"{reason} (simulated by {actor})"[:200],
            )
        )


def _ingest_service(request: Request):
    manager = getattr(request.app.state, "services", None)
    if manager is None:
        return None
    for service in manager.services:
        if getattr(service, "name", None) == "ingest":
            return service
    return None
