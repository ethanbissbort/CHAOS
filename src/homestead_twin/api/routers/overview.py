"""FR-001 property overview aggregate.

SDD FR-001 asks for *one* property overview showing subsystem health, active
alarms, energy reserve, water reserve, communications status and the current
operating mode. SDD 5.1 says the operator interface must work on the local
network with no internet, which in practice means the wall display should not
need fifteen round trips to paint a screen. So the whole home screen is served
from a single aggregate here.

Three rules shape every response in this module:

1. **Nothing is fabricated.** The homestead does not exist yet. A subsystem with
   no assets is ``not_deployed``; assets that exist only on paper are
   ``design_only``; a registered point that has never reported is ``no_data``.
   None of those are ``0``, and none of them are errors.
2. **Every number carries its provenance.** Each metric reports which points it
   was summed from, their quality and their age, because SDD 38 requires
   measured values to be distinguishable from calculated ones.
3. **Empty tables are the normal early state.** Every query degrades to an
   explicit "unavailable" marker rather than raising.

The endpoints read the SQLAlchemy models directly rather than calling other
routers' services, so the overview keeps working while the rest of the API is
still being built out.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from homestead_twin import __version__
from homestead_twin.api.deps import AppSettings, CurrentPrincipal, DbSession
from homestead_twin.config import Settings
from homestead_twin.models import (
    Alarm,
    Asset,
    Command,
    CurrentState,
    EnergyStateSnapshot,
    EnergyStateTransition,
    Incident,
    Location,
    OperatingMode,
    Point,
    PointBinding,
    PowerBudgetLease,
    PowerLoadProfile,
    utcnow,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["overview"])

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: SDD 25.4 domain vocabulary, in the order an operator scans a wall display.
DOMAINS: tuple[tuple[str, str], ...] = (
    ("site", "Site"),
    ("energy", "Energy"),
    ("water", "Water"),
    ("agriculture", "Agriculture"),
    ("structure", "Structures"),
    ("it", "IT and communications"),
    ("security", "Security"),
    ("safety", "Safety"),
    ("storage", "Storage"),
    ("spa", "Spa"),
    ("workshop", "Workshop"),
    ("fuel", "Fuel"),
)
DOMAIN_LABELS = dict(DOMAINS)

#: Lifecycle statuses that mean physical hardware exists and can report.
DEPLOYED_STATUSES = frozenset({"installed", "commissioned", "active", "degraded", "maintenance"})
#: Statuses that mean the asset is a design record only.
DESIGN_STATUSES = frozenset({"concept", "planned", "procured", "reserve"})
FAILED_STATUSES = frozenset({"failed"})

#: An alarm is "active" for the home screen until it is cleared or reviewed.
UNCLEARED_ALARM_STATES = ("detected", "active", "acknowledged", "mitigated")
SEVERITY_ORDER = ("emergency", "critical", "major", "warning", "info")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITY_ORDER)}

# Availability status vocabulary shared by every metric and roll-up. The UI
# renders each of these differently; collapsing them would be a lie.
STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_NO_DATA = "no_data"
STATUS_NO_POINTS = "no_points"
STATUS_DESIGN_ONLY = "design_only"
STATUS_NOT_DEPLOYED = "not_deployed"

STATUS_EXPLANATIONS = {
    STATUS_OK: "Live measured data.",
    STATUS_STALE: "Last value is older than its stale window; treat as unknown.",
    STATUS_NO_DATA: "Points are registered but have never reported a value.",
    STATUS_NO_POINTS: "Assets exist in the registry but no matching points are registered.",
    STATUS_DESIGN_ONLY: "Assets exist as design records only; nothing is installed yet.",
    STATUS_NOT_DEPLOYED: "No assets of this kind exist in the registry yet.",
}

#: Worst-first ordering used when rolling several statuses into one.
_STATUS_SEVERITY = {
    STATUS_OK: 0,
    STATUS_STALE: 1,
    STATUS_NO_DATA: 2,
    STATUS_NO_POINTS: 3,
    STATUS_DESIGN_ONLY: 4,
    STATUS_NOT_DEPLOYED: 5,
}

QUALITY_ORDER = ("good", "uncertain", "substituted", "stale", "invalid", "unknown")
_QUALITY_RANK = {name: index for index, name in enumerate(QUALITY_ORDER)}


# ---------------------------------------------------------------------------
# Metric declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricSource:
    """One candidate way of measuring a quantity.

    Sources are tried in order and the first one that has *any* live data wins,
    so a purpose-built meter beats a sum of sub-loads, and the answer always
    says which one it used.
    """

    label: str
    point_names: tuple[str, ...]
    asset_classes: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    criticalities: tuple[str, ...] = ()
    aggregate: str = "sum"  # sum | mean | max | min
    note: str | None = None


@dataclass(frozen=True)
class MetricSpec:
    role: str
    unit: str | None
    description: str
    sources: tuple[MetricSource, ...]
    #: Domains that must contain at least one asset for this metric to be
    #: meaningful. Used to tell "not deployed" from "no data".
    required_domains: tuple[str, ...] = ()
    required_classes: tuple[str, ...] = ()


METRICS: dict[str, MetricSpec] = {
    "pv_production_kw": MetricSpec(
        role="pv_production_kw",
        unit="kW",
        description="Photovoltaic production measured at the array.",
        required_domains=("energy",),
        required_classes=("pv_array", "pv_row", "combiner", "inverter"),
        sources=(
            MetricSource(
                label="PV array DC power",
                point_names=("power_dc_kw",),
                asset_classes=("pv_array",),
            ),
            MetricSource(
                label="PV row DC power (summed)",
                point_names=("power_dc_kw",),
                asset_classes=("pv_row", "combiner"),
            ),
            MetricSource(
                label="Inverter DC input (summed)",
                point_names=("power_dc_input_kw",),
                asset_classes=("inverter",),
                note="Inverter DC input is a proxy for array output, not a direct array measurement.",
            ),
        ),
    ),
    "site_load_kw": MetricSpec(
        role="site_load_kw",
        unit="kW",
        description="Total site electrical demand.",
        required_domains=("energy",),
        sources=(
            MetricSource(
                label="Site meter total",
                point_names=("power_total_kw",),
                domains=("energy",),
            ),
            MetricSource(
                label="Distribution panel AC power (summed)",
                point_names=("power_ac_kw",),
                asset_classes=("panel", "ats", "pdu"),
            ),
            MetricSource(
                label="Instrumented loads (summed)",
                point_names=("power_kw",),
                asset_classes=("load",),
                note="Sum of instrumented loads only; uninstrumented demand is not included.",
            ),
        ),
    ),
    "critical_load_kw": MetricSpec(
        role="critical_load_kw",
        unit="kW",
        description="Demand of loads classified life-safety or critical.",
        required_domains=("energy",),
        sources=(
            MetricSource(
                label="Critical loads (summed)",
                point_names=("power_kw",),
                criticalities=("life_safety", "critical"),
            ),
        ),
    ),
    "battery_soc_pct": MetricSpec(
        role="battery_soc_pct",
        unit="%",
        description="Battery state of charge.",
        required_domains=("energy",),
        required_classes=("battery_bank", "bms"),
        sources=(
            MetricSource(
                label="Battery bank SOC",
                point_names=("soc_pct",),
                asset_classes=("battery_bank", "bms"),
                aggregate="mean",
            ),
        ),
    ),
    "generator_fuel_pct": MetricSpec(
        role="generator_fuel_pct",
        unit="%",
        description="Generator fuel level.",
        required_domains=("energy", "fuel"),
        required_classes=("generator", "fuel_tank"),
        sources=(
            MetricSource(
                label="Generator fuel level",
                point_names=("fuel_level_pct",),
                asset_classes=("generator", "fuel_tank"),
                aggregate="min",
            ),
        ),
    ),
    "generator_output_kw": MetricSpec(
        role="generator_output_kw",
        unit="kW",
        description="Generator electrical output.",
        required_domains=("energy",),
        required_classes=("generator",),
        sources=(
            MetricSource(
                label="Generator output",
                point_names=("power_output_kw",),
                asset_classes=("generator",),
            ),
        ),
    ),
    "water_reserve_pct": MetricSpec(
        role="water_reserve_pct",
        unit="%",
        description="Stored water as a percentage of usable capacity.",
        required_domains=("water",),
        sources=(
            MetricSource(
                label="Tank level",
                point_names=("level_pct",),
                domains=("water",),
                aggregate="mean",
            ),
        ),
    ),
    "water_level_m": MetricSpec(
        role="water_level_m",
        unit="m",
        description="Measured water level.",
        required_domains=("water",),
        sources=(
            MetricSource(
                label="Tank/cistern level",
                point_names=("water_level_m",),
                domains=("water",),
                aggregate="mean",
            ),
        ),
    ),
    "outside_temperature_c": MetricSpec(
        role="outside_temperature_c",
        unit="degC",
        description="Outside air temperature used for freeze and heat risk.",
        required_domains=("site",),
        sources=(
            MetricSource(
                label="Site weather station",
                point_names=("temperature_air_c",),
                domains=("site",),
                aggregate="mean",
            ),
        ),
    ),
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class Reader:
    """A session wrapper that turns a broken table into a *reported* gap.

    The overview is the screen an operator opens when something is wrong, which
    includes "this node came up before its schema was created" and "the registry
    volume did not mount". Raising a 500 there hides the one fact that matters.
    Each failed read is recorded in :attr:`problems` and surfaced in the payload
    as ``data_sources.unavailable`` -- degraded, visibly, rather than absent.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.problems: list[dict] = []

    def _fail(self, source: str, exc: Exception) -> None:
        detail = str(getattr(exc, "orig", exc))
        self.problems.append({"source": source, "error": type(exc).__name__, "detail": detail[:240]})
        try:
            self.session.rollback()
        except Exception:  # pragma: no cover - defensive
            logger.exception("rollback failed after read error on %s", source)

    def scalars(self, stmt, source: str) -> list:
        try:
            return list(self.session.execute(stmt).scalars())
        except SQLAlchemyError as exc:
            self._fail(source, exc)
            return []

    def first(self, stmt, source: str):
        try:
            return self.session.execute(stmt).scalars().first()
        except SQLAlchemyError as exc:
            self._fail(source, exc)
            return None

    def rows(self, stmt, source: str) -> list:
        try:
            return list(self.session.execute(stmt).all())
        except SQLAlchemyError as exc:
            self._fail(source, exc)
            return []

    def count(self, stmt, source: str) -> int:
        try:
            return int(self.session.execute(stmt).scalar_one() or 0)
        except SQLAlchemyError as exc:
            self._fail(source, exc)
            return 0

    def get(self, model, primary_key, source: str):
        try:
            return self.session.get(model, primary_key)
        except SQLAlchemyError as exc:
            self._fail(source, exc)
            return None

    def report(self) -> dict:
        return {
            "unavailable": self.problems,
            "degraded": bool(self.problems),
            "note": (
                None
                if not self.problems
                else "Some tables could not be read. The values below are incomplete; "
                "this is usually an uninitialised or unmounted database, not empty data."
            ),
        }


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite hands back naive datetimes; every platform timestamp is UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    aware = _as_utc(value)
    return aware.isoformat() if aware else None


def _worst_status(statuses: Iterable[str]) -> str:
    worst = STATUS_OK
    seen = False
    for candidate in statuses:
        seen = True
        if _STATUS_SEVERITY.get(candidate, 9) > _STATUS_SEVERITY.get(worst, 0):
            worst = candidate
    return worst if seen else STATUS_NOT_DEPLOYED


def _worst_quality(qualities: Iterable[str | None]) -> str | None:
    worst: str | None = None
    for quality in qualities:
        if quality is None:
            continue
        if worst is None or _QUALITY_RANK.get(quality, 9) > _QUALITY_RANK.get(worst, 0):
            worst = quality
    return worst


def _current_value(state: CurrentState) -> Any:
    for candidate in (state.value_numeric, state.value_bool, state.value_text, state.value_json):
        if candidate is not None:
            return candidate
    return None


def _is_stale(state: CurrentState, point: Point | None, settings: Settings, now: dt.datetime) -> bool:
    ts = _as_utc(state.ts) or _as_utc(state.received_at)
    if ts is None:
        return True
    window = state.stale_after_s
    if window is None and point is not None:
        window = point.stale_after_s
    if window is None:
        window = settings.default_stale_after_s
    if not window or window <= 0:
        return False
    return (now - ts).total_seconds() > window


def _unavailable(spec: MetricSpec, status_name: str, note: str | None = None) -> dict:
    return {
        "role": spec.role,
        "unit": spec.unit,
        "description": spec.description,
        "status": status_name,
        "available": False,
        "value": None,
        "quality": None,
        "as_of": None,
        "source": None,
        "point_count": 0,
        "reporting_point_count": 0,
        "contributors": [],
        "note": note or STATUS_EXPLANATIONS[status_name],
    }


# ---------------------------------------------------------------------------
# Registry snapshot -- loaded once per request
# ---------------------------------------------------------------------------


@dataclass
class RegistrySnapshot:
    """Everything the aggregate needs, read in a handful of queries."""

    now: dt.datetime
    settings: Settings
    assets: dict[str, Asset] = field(default_factory=dict)
    points: list[Point] = field(default_factory=list)
    states: dict[str, CurrentState] = field(default_factory=dict)
    points_by_name: dict[str, list[Point]] = field(default_factory=lambda: defaultdict(list))

    @property
    def any_assets(self) -> bool:
        return bool(self.assets)

    def domain_assets(self, domains: Sequence[str]) -> list[Asset]:
        wanted = set(domains)
        return [a for a in self.assets.values() if a.domain in wanted]

    def class_assets(self, classes: Sequence[str]) -> list[Asset]:
        wanted = set(classes)
        return [a for a in self.assets.values() if a.asset_class in wanted]


def _load_snapshot(reader: Reader, settings: Settings, point_names: Sequence[str]) -> RegistrySnapshot:
    snapshot = RegistrySnapshot(now=utcnow(), settings=settings)

    for asset in reader.scalars(select(Asset), "assets"):
        snapshot.assets[asset.asset_id] = asset

    if point_names:
        stmt = select(Point).where(Point.point_name.in_(list(point_names)))
        snapshot.points = reader.scalars(stmt, "points")
        for point in snapshot.points:
            snapshot.points_by_name[point.point_name].append(point)

        point_ids = [p.point_id for p in snapshot.points]
        # Chunked so a very large registry never blows the SQLite variable limit.
        for start in range(0, len(point_ids), 400):
            chunk = point_ids[start : start + 400]
            stmt = select(CurrentState).where(CurrentState.point_id.in_(chunk))
            for state in reader.scalars(stmt, "current_state"):
                snapshot.states[state.point_id] = state
    return snapshot


def _matching_points(snapshot: RegistrySnapshot, source: MetricSource) -> list[tuple[Point, Asset]]:
    matches: list[tuple[Point, Asset]] = []
    for name in source.point_names:
        for point in snapshot.points_by_name.get(name, []):
            asset = snapshot.assets.get(point.asset_id)
            if asset is None:
                continue
            if source.asset_classes and asset.asset_class not in source.asset_classes:
                continue
            if source.domains and asset.domain not in source.domains:
                continue
            if source.criticalities and asset.criticality not in source.criticalities:
                continue
            matches.append((point, asset))
    return matches


def _evaluate_metric(snapshot: RegistrySnapshot, spec: MetricSpec) -> dict:
    """Resolve one metric, preserving why it is unavailable when it is."""
    relevant: list[Asset] = []
    if spec.required_classes:
        relevant.extend(snapshot.class_assets(spec.required_classes))
    if spec.required_domains and not spec.required_classes:
        relevant.extend(snapshot.domain_assets(spec.required_domains))

    if (spec.required_classes or spec.required_domains) and not relevant:
        scope = ", ".join(spec.required_classes or spec.required_domains)
        return _unavailable(
            spec,
            STATUS_NOT_DEPLOYED,
            f"No assets of type/domain [{scope}] exist in the registry yet.",
        )

    candidate_points = 0
    fallback_status = STATUS_NO_POINTS
    saw_stale_value = False
    for source in spec.sources:
        matches = _matching_points(snapshot, source)
        candidate_points += len(matches)
        if not matches:
            continue
        fallback_status = STATUS_NO_DATA

        contributors: list[dict] = []
        numeric: list[float] = []
        timestamps: list[dt.datetime] = []
        qualities: list[str | None] = []
        stale_count = 0

        for point, asset in matches:
            state = snapshot.states.get(point.point_id)
            if state is None:
                contributors.append(
                    {
                        "point_id": point.point_id,
                        "asset_id": asset.asset_id,
                        "asset_name": asset.name,
                        "value": None,
                        "unit": point.unit,
                        "quality": None,
                        "ts": None,
                        "stale": True,
                        "status": STATUS_NO_DATA,
                    }
                )
                continue
            stale = _is_stale(state, point, snapshot.settings, snapshot.now)
            value = _current_value(state)
            contributors.append(
                {
                    "point_id": point.point_id,
                    "asset_id": asset.asset_id,
                    "asset_name": asset.name,
                    "value": value,
                    "unit": state.unit or point.unit,
                    "quality": state.quality,
                    "ts": _iso(state.ts),
                    "stale": stale,
                    "status": STATUS_STALE if stale else STATUS_OK,
                }
            )
            qualities.append(state.quality)
            if stale:
                stale_count += 1
            if state.value_numeric is not None and not stale:
                numeric.append(float(state.value_numeric))
            ts = _as_utc(state.ts)
            if ts is not None:
                timestamps.append(ts)

        if not numeric:
            # Points matched but nothing usable right now. A point that reported
            # and went quiet is a different fact from one that never reported --
            # the first means an instrument or link has failed.
            if stale_count:
                saw_stale_value = True
            continue

        if source.aggregate == "mean":
            value = sum(numeric) / len(numeric)
        elif source.aggregate == "max":
            value = max(numeric)
        elif source.aggregate == "min":
            value = min(numeric)
        else:
            value = sum(numeric)

        return {
            "role": spec.role,
            "unit": spec.unit,
            "description": spec.description,
            "status": STATUS_STALE if stale_count else STATUS_OK,
            "available": True,
            "value": round(value, 4),
            "quality": _worst_quality(qualities),
            "as_of": _iso(max(timestamps)) if timestamps else None,
            "source": source.label,
            "aggregate": source.aggregate,
            "point_count": len(matches),
            "reporting_point_count": len(numeric),
            "stale_point_count": stale_count,
            "contributors": contributors,
            "note": source.note,
        }

    if candidate_points == 0:
        # Assets exist but carry no matching points at all.
        if relevant and not any(a.status in DEPLOYED_STATUSES for a in relevant):
            return _unavailable(
                spec,
                STATUS_DESIGN_ONLY,
                "Assets exist as design records only; no instrumentation is installed yet.",
            )
        return _unavailable(spec, STATUS_NO_POINTS)

    if saw_stale_value:
        return _unavailable(
            spec,
            STATUS_STALE,
            "The last reported value is older than its stale window. Treat this as unknown: "
            "the instrument or its link has stopped reporting, it is not reading zero.",
        )
    return _unavailable(spec, fallback_status)


# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------


def _active_alarms(reader: Reader) -> list[Alarm]:
    stmt = select(Alarm).where(Alarm.state.in_(UNCLEARED_ALARM_STATES))
    return reader.scalars(stmt, "alarms")


def _alarm_brief(alarm: Alarm, asset: Asset | None, incident: Incident | None) -> dict:
    return {
        "id": alarm.id,
        "alarm_key": alarm.alarm_key,
        "severity": alarm.severity,
        "state": alarm.state,
        "asset_id": alarm.asset_id,
        "asset_name": asset.name if asset else None,
        "domain": asset.domain if asset else None,
        "point_id": alarm.point_id,
        "message": alarm.message,
        "detected_at": _iso(alarm.detected_at),
        "acknowledged_at": _iso(alarm.acknowledged_at),
        "acknowledged_by": alarm.acknowledged_by,
        "suppressed": bool(alarm.suppressed),
        "incident_id": alarm.incident_id,
        "incident_title": incident.title if incident else None,
    }


def _alarm_summary(reader: Reader, snapshot: RegistrySnapshot, limit: int) -> tuple[dict, list[Alarm]]:
    alarms = _active_alarms(reader)
    by_severity = Counter(a.severity for a in alarms)
    by_state = Counter(a.state for a in alarms)

    incident_ids = {a.incident_id for a in alarms if a.incident_id}
    incidents: dict[str, Incident] = {}
    if incident_ids:
        stmt = select(Incident).where(Incident.id.in_(list(incident_ids)))
        incidents = {i.id: i for i in reader.scalars(stmt, "incidents")}

    open_incidents = reader.count(
        select(func.count()).select_from(Incident).where(Incident.state == "open"),
        "incidents",
    )

    ordered = sorted(
        alarms,
        key=lambda a: (
            SEVERITY_RANK.get(a.severity, len(SEVERITY_ORDER)),
            -(_as_utc(a.detected_at) or dt.datetime.min.replace(tzinfo=dt.UTC)).timestamp(),
        ),
    )
    top = [
        _alarm_brief(a, snapshot.assets.get(a.asset_id or ""), incidents.get(a.incident_id or ""))
        for a in ordered[:limit]
    ]

    summary = {
        "status": STATUS_OK,
        "active_total": len(alarms),
        "emergency_active": by_severity.get("emergency", 0),
        "critical_active": by_severity.get("critical", 0),
        "major_active": by_severity.get("major", 0),
        "warning_active": by_severity.get("warning", 0),
        "info_active": by_severity.get("info", 0),
        "unacknowledged": sum(1 for a in alarms if a.state in ("detected", "active")),
        "by_severity": {s: by_severity.get(s, 0) for s in SEVERITY_ORDER},
        "by_state": dict(by_state),
        "open_incidents": int(open_incidents or 0),
        "top": top,
        "note": (
            "No alarms have ever been raised."
            if not alarms
            else "Active means detected, active, acknowledged or mitigated but not cleared."
        ),
    }
    return summary, alarms


# ---------------------------------------------------------------------------
# Subsystem roll-up
# ---------------------------------------------------------------------------


def _domain_point_stats(reader: Reader, settings: Settings) -> dict[str, dict]:
    """Point counts per domain, including how many have ever reported."""
    stats: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "control_capable": 0, "bound": 0, "with_data": 0, "stale": 0}
    )

    rows = reader.rows(
        select(
            Asset.domain,
            func.count(Point.point_id),
            func.sum(case((Point.control_capable.is_(True), 1), else_=0)),
        )
        .select_from(Point)
        .join(Asset, Asset.asset_id == Point.asset_id)
        .group_by(Asset.domain),
        "points",
    )
    for domain, total, controllable in rows:
        stats[domain]["total"] = int(total or 0)
        stats[domain]["control_capable"] = int(controllable or 0)

    bound_rows = reader.rows(
        select(Asset.domain, func.count(PointBinding.point_id))
        .select_from(PointBinding)
        .join(Asset, Asset.asset_id == PointBinding.asset_id)
        .where(PointBinding.binding_status.notin_(("tbd", "unbound")))
        .group_by(Asset.domain),
        "point_bindings",
    )
    for domain, bound in bound_rows:
        stats[domain]["bound"] = int(bound or 0)

    now = utcnow()
    state_rows = reader.rows(
        select(Asset.domain, CurrentState.ts, CurrentState.stale_after_s)
        .select_from(CurrentState)
        .join(Asset, Asset.asset_id == CurrentState.asset_id),
        "current_state",
    )
    for domain, ts, stale_after in state_rows:
        entry = stats[domain]
        entry["with_data"] += 1
        ts = _as_utc(ts)
        window = stale_after or settings.default_stale_after_s
        if ts is None or (window and (now - ts).total_seconds() > window):
            entry["stale"] += 1
    return stats


def _subsystem_rollup(reader: Reader, settings: Settings, detailed: bool = False) -> list[dict]:
    assets = reader.scalars(select(Asset), "assets")
    by_domain: dict[str, list[Asset]] = defaultdict(list)
    for asset in assets:
        by_domain[asset.domain].append(asset)

    alarms = _active_alarms(reader)
    asset_domains = {a.asset_id: a.domain for a in assets}
    alarms_by_domain: dict[str, list[Alarm]] = defaultdict(list)
    for alarm in alarms:
        # An alarm with no asset, or one naming an asset the registry no longer
        # holds, still has to be counted somewhere: it belongs to the site.
        domain = asset_domains.get(alarm.asset_id or "", "site")
        alarms_by_domain[domain].append(alarm)

    point_stats = _domain_point_stats(reader, settings)

    known_domains = [d for d, _ in DOMAINS]
    for domain in by_domain:
        if domain not in known_domains:  # a domain outside SDD 25.4 must still be visible
            known_domains.append(domain)

    result: list[dict] = []
    for domain in known_domains:
        domain_assets = by_domain.get(domain, [])
        status_counts = Counter(a.status for a in domain_assets)
        deployed = sum(1 for a in domain_assets if a.status in DEPLOYED_STATUSES)
        failed = sum(1 for a in domain_assets if a.status in FAILED_STATUSES)
        domain_alarms = alarms_by_domain.get(domain, [])
        severities = Counter(a.severity for a in domain_alarms)
        points = point_stats.get(
            domain, {"total": 0, "control_capable": 0, "bound": 0, "with_data": 0, "stale": 0}
        )

        open_field_assets = [a for a in domain_assets if a.open_fields]
        open_field_count = sum(len(a.open_fields or []) for a in domain_assets)

        if not domain_assets:
            health = STATUS_NOT_DEPLOYED
        elif severities.get("emergency") or severities.get("critical"):
            health = "alarm"
        elif deployed == 0:
            health = STATUS_DESIGN_ONLY
        elif failed or severities.get("major"):
            health = "degraded"
        elif points["with_data"] == 0:
            health = STATUS_NO_DATA
        elif points["stale"] and points["stale"] == points["with_data"]:
            health = STATUS_STALE
        else:
            health = STATUS_OK

        entry: dict[str, Any] = {
            "domain": domain,
            "label": DOMAIN_LABELS.get(domain, domain.replace("_", " ").title()),
            "health": health,
            "health_note": STATUS_EXPLANATIONS.get(health)
            or ("Active critical alarm." if health == "alarm" else "Asset failed or major alarm active."),
            "assets": {
                "total": len(domain_assets),
                "deployed": deployed,
                "design_only": sum(1 for a in domain_assets if a.status in DESIGN_STATUSES),
                "failed": failed,
                "by_status": dict(sorted(status_counts.items())),
            },
            "alarms": {
                "active_total": len(domain_alarms),
                "by_severity": {s: severities.get(s, 0) for s in SEVERITY_ORDER},
            },
            "points": dict(points),
            "open_fields": {
                "unresolved_count": open_field_count,
                "assets_with_open_fields": len(open_field_assets),
            },
        }
        if detailed:
            entry["open_fields"]["items"] = [
                {"asset_id": a.asset_id, "name": a.name, "fields": list(a.open_fields or [])}
                for a in sorted(open_field_assets, key=lambda a: a.asset_id)
            ]
            entry["alarms"]["items"] = [
                _alarm_brief(a, next((x for x in domain_assets if x.asset_id == a.asset_id), None), None)
                for a in sorted(domain_alarms, key=lambda a: SEVERITY_RANK.get(a.severity, 9))
            ]
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Operating mode / site state
# ---------------------------------------------------------------------------


def _normalise_interlock(
    entry: Any, *, source: str, evaluated_at: str | None, command_id: str | None
) -> dict:
    """Accept either interlock record shape without losing information.

    The command service records ``{code, allowed, reason, detail, blocks_dispatch}``;
    older/simpler producers record ``{name, passed, detail}``. An operator needs
    both facts kept apart: whether the check *passed*, and whether it would stop
    a real dispatch even so (a dry run passes checks it would fail for real).
    """
    if not isinstance(entry, dict):
        return {
            "name": str(entry),
            "passed": None,
            "blocking": None,
            "blocks_dispatch": None,
            "detail": None,
            "context": None,
            "source": source,
            "evaluated_at": evaluated_at,
            "command_id": command_id,
        }

    passed = entry.get("passed")
    if passed is None:
        passed = entry.get("allowed")
    if passed is None and entry.get("result") is not None:
        passed = entry["result"] in ("pass", "passed", "ok", True)
    if passed is not None:
        passed = bool(passed)

    detail = entry.get("reason") if isinstance(entry.get("reason"), str) else None
    context = entry.get("detail")
    if detail is None and isinstance(context, str):
        detail, context = context, None

    return {
        "name": entry.get("name") or entry.get("code") or entry.get("interlock") or "unnamed",
        "passed": passed,
        "blocking": None if passed is None else (not passed),
        "blocks_dispatch": entry.get("blocks_dispatch"),
        "detail": detail,
        "context": context,
        "override_by": entry.get("override_by"),
        "source": source,
        "evaluated_at": evaluated_at,
        "command_id": command_id,
    }


def _mode_for(reader: Reader, scope_type: str, scope_id: str) -> OperatingMode | None:
    stmt = select(OperatingMode).where(
        OperatingMode.scope_type == scope_type, OperatingMode.scope_id == scope_id
    )
    return reader.first(stmt, "operating_modes")


def _site_mode(reader: Reader, settings: Settings) -> dict:
    mode = _mode_for(reader, "site", settings.site_id)
    if mode is None:
        mode = reader.first(
            select(OperatingMode).where(OperatingMode.scope_type == "site"), "operating_modes"
        )
    if mode is None:
        return {
            "available": False,
            "status": STATUS_NO_DATA,
            "mode": None,
            "scope_id": settings.site_id,
            "note": "No site operating mode has been set. The platform makes no assumption.",
        }
    return {
        "available": True,
        "status": STATUS_OK,
        "mode": mode.mode,
        "previous_mode": mode.previous_mode,
        "scope_id": mode.scope_id,
        "changed_by": mode.changed_by,
        "changed_at": _iso(mode.changed_at),
        "reason": mode.reason,
        "expires_at": _iso(mode.expires_at),
        "auto_clear_allowed": bool(mode.auto_clear_allowed),
    }


def _site_operating_state(
    snapshot: RegistrySnapshot,
    alarm_summary: dict,
    energy: dict,
    subsystems: list[dict],
) -> dict:
    reasons: list[str] = []
    assets = list(snapshot.assets.values())
    deployed = sum(1 for a in assets if a.status in DEPLOYED_STATUSES)

    if not assets:
        return {
            "state": "unknown",
            "label": "No registry loaded",
            "basis": ["The asset registry is empty; the platform knows nothing about this site yet."],
            "assets_total": 0,
            "assets_deployed": 0,
            "lifecycle_phase": "empty_registry",
        }

    if alarm_summary["emergency_active"]:
        state, label = "emergency", "Emergency"
        reasons.append(f"{alarm_summary['emergency_active']} active emergency alarm(s).")
    elif alarm_summary["critical_active"]:
        state, label = "alarm", "Critical alarm active"
        reasons.append(f"{alarm_summary['critical_active']} active critical alarm(s).")
    elif alarm_summary["major_active"]:
        state, label = "degraded", "Degraded"
        reasons.append(f"{alarm_summary['major_active']} active major alarm(s).")
    elif deployed == 0:
        state, label = "pre_deployment", "Pre-deployment"
        reasons.append(f"{len(assets)} assets are registered but none are installed or commissioned yet.")
    else:
        degraded = [s for s in subsystems if s["health"] in ("degraded", "alarm")]
        if degraded:
            state, label = "degraded", "Degraded"
            reasons.append("Degraded subsystems: " + ", ".join(s["label"] for s in degraded))
        else:
            state, label = "nominal", "Nominal"
            reasons.append("No active major or critical alarms.")

    energy_state = energy.get("state")
    if energy_state and energy_state not in ("NORMAL", "SURPLUS"):
        reasons.append(f"Energy state is {energy_state}.")

    return {
        "state": state,
        "label": label,
        "basis": reasons,
        "assets_total": len(assets),
        "assets_deployed": deployed,
        "lifecycle_phase": "design" if deployed == 0 else "operational",
    }


# ---------------------------------------------------------------------------
# Energy
# ---------------------------------------------------------------------------


#: SDD 13.2 load tiers, so the dashboard can label a tier even before any
#: profile exists for it.
TIER_LABELS = {
    0: "Tier 0 — Physical protection",
    1: "Tier 1 — Essential services",
    2: "Tier 2 — Important operations",
    3: "Tier 3 — Deferrable operations",
    4: "Tier 4 — Opportunistic surplus",
}


def _load_budget_block(
    reader: Reader, snapshot: RegistrySnapshot, energy_row: EnergyStateSnapshot | None
) -> dict:
    """Tier roll-up of the load schedule (SDD 31) for the energy dashboard."""
    profiles = reader.scalars(select(PowerLoadProfile), "power_load_profiles")
    shed_active = set(energy_row.shed_groups_active or []) if energy_row else set()

    tiers: dict[int, dict] = {}
    for tier, label in TIER_LABELS.items():
        tiers[tier] = {
            "tier": tier,
            "label": label,
            "load_count": 0,
            "estimated_kw": 0.0,
            "estimated_kw_known": 0,
            "measured_kw": None,
            "measured_point_count": 0,
            "shed_groups": [],
            "shed_now": 0,
            "controllable": 0,
            "data_status": {},
        }

    for profile in profiles:
        tier = profile.effective_tier if profile.effective_tier is not None else profile.base_tier
        entry = tiers.setdefault(
            tier,
            {
                "tier": tier,
                "label": TIER_LABELS.get(tier, f"Tier {tier}"),
                "load_count": 0,
                "estimated_kw": 0.0,
                "estimated_kw_known": 0,
                "measured_kw": None,
                "measured_point_count": 0,
                "shed_groups": [],
                "shed_now": 0,
                "controllable": 0,
                "data_status": {},
            },
        )
        entry["load_count"] += 1
        estimate = profile.estimated_power_kw
        if estimate is None:
            estimate = profile.rated_power_kw
        if estimate is not None:
            entry["estimated_kw"] += float(estimate)
            entry["estimated_kw_known"] += 1
        if profile.shed_group and profile.shed_group not in entry["shed_groups"]:
            entry["shed_groups"].append(profile.shed_group)
        if profile.shed_group in shed_active:
            entry["shed_now"] += 1
        if profile.control_method and profile.control_method != "not_controllable":
            entry["controllable"] += 1
        entry["data_status"][profile.data_status] = entry["data_status"].get(profile.data_status, 0) + 1

        # Measured power, where the load actually has a live point.
        point_id = profile.measured_power_point
        state = snapshot.states.get(point_id) if point_id else None
        if state is None:
            state = snapshot.states.get(f"{profile.asset_id}/power_kw")
        if state is not None and state.value_numeric is not None:
            entry["measured_kw"] = (entry["measured_kw"] or 0.0) + float(state.value_numeric)
            entry["measured_point_count"] += 1

    for entry in tiers.values():
        entry["estimated_kw"] = round(entry["estimated_kw"], 3) if entry["estimated_kw_known"] else None
        if entry["measured_kw"] is not None:
            entry["measured_kw"] = round(entry["measured_kw"], 3)

    leases = reader.scalars(
        select(PowerBudgetLease).where(PowerBudgetLease.state == "active"), "power_budget_leases"
    )

    return {
        "status": STATUS_OK if profiles else STATUS_NO_DATA,
        "profile_count": len(profiles),
        "note": (
            None
            if profiles
            else "No load schedule has been recorded yet, so tier budgets cannot be computed."
        ),
        "tiers": [tiers[key] for key in sorted(tiers)],
        "active_leases": [
            {
                "lease_id": lease.lease_id,
                "asset_id": lease.asset_id,
                "granted_kw": lease.granted_kw,
                "expires_at": _iso(lease.expires_at),
                "priority": lease.priority,
                "reason": lease.reason,
                "requested_by": lease.requested_by,
                "revocable": bool(lease.revocable),
            }
            for lease in sorted(leases, key=lambda x: x.priority)
        ],
        "granted_kw_total": round(sum(lease.granted_kw for lease in leases), 3) if leases else 0.0,
    }


def _energy_block(reader: Reader, snapshot: RegistrySnapshot) -> dict:
    row = reader.first(select(EnergyStateSnapshot).order_by(EnergyStateSnapshot.id), "energy_state_snapshot")

    reserve = _evaluate_metric(snapshot, METRICS["battery_soc_pct"])
    pv = _evaluate_metric(snapshot, METRICS["pv_production_kw"])
    load = _evaluate_metric(snapshot, METRICS["site_load_kw"])
    critical_load = _evaluate_metric(snapshot, METRICS["critical_load_kw"])
    generator_output = _evaluate_metric(snapshot, METRICS["generator_output_kw"])
    generator_fuel = _evaluate_metric(snapshot, METRICS["generator_fuel_pct"])

    derived = dict(row.derived or {}) if row else {}
    inputs = dict(row.inputs or {}) if row else {}

    def _derived(name: str) -> dict:
        if name in derived and derived[name] is not None:
            return {"available": True, "value": derived[name], "status": STATUS_OK, "source": "ems_derived"}
        return {
            "available": False,
            "value": None,
            "status": STATUS_NO_DATA,
            "source": None,
            "note": (
                "The EMS has not published this derived value yet."
                if row
                else "The EMS has never published a state snapshot."
            ),
        }

    # Reserve percentage: the EMS derived value wins, measured SOC is the fallback.
    reserve_pct = reserve
    if "reserve_pct" in derived and derived.get("reserve_pct") is not None:
        reserve_pct = {
            **reserve,
            "status": STATUS_OK,
            "available": True,
            "value": derived["reserve_pct"],
            "source": "EMS derived reserve_pct",
        }

    net_power = {"available": False, "value": None, "status": STATUS_NO_DATA, "unit": "kW"}
    if pv["available"] and load["available"]:
        net_power = {
            "available": True,
            "value": round(pv["value"] - load["value"], 4),
            "status": _worst_status([pv["status"], load["status"]]),
            "unit": "kW",
            "note": "Calculated: PV production minus site load. Not a measured value.",
        }

    transitions = reader.scalars(
        select(EnergyStateTransition).order_by(EnergyStateTransition.occurred_at.desc()).limit(10),
        "energy_state_transitions",
    )

    load_budget = _load_budget_block(reader, snapshot, row)

    if row is None:
        ems_status = STATUS_NO_DATA
        note = "The energy management service has not published a state snapshot yet."
    else:
        ems_status = STATUS_OK
        note = None

    return {
        "status": ems_status,
        "note": note,
        "state": row.state if row else None,
        "state_entered_at": _iso(row.entered_at) if row else None,
        "candidate_state": row.candidate_state if row else None,
        "candidate_since": _iso(row.candidate_since) if row else None,
        "frozen_until": _iso(row.frozen_until) if row else None,
        "frozen_by": row.frozen_by if row else None,
        "data_quality": row.data_quality if row else "unknown",
        "last_evaluated_at": _iso(row.last_evaluated_at) if row else None,
        "shed_groups_active": list(row.shed_groups_active or []) if row else [],
        "generator_request": row.generator_request if row else None,
        "inputs": inputs,
        "derived": derived,
        "reserve_pct": reserve_pct,
        "battery_soc_pct": reserve,
        "usable_energy_kwh": _derived("energy_above_emergency_reserve_kwh"),
        "autonomy_current_h": _derived("autonomy_current_h"),
        "autonomy_critical_h": _derived("autonomy_critical_h"),
        "forecast_energy_margin_kwh": _derived("forecast_energy_margin_kwh"),
        "pv_production_kw": pv,
        "site_load_kw": load,
        "critical_load_kw": critical_load,
        "net_power_kw": net_power,
        "load_budget": load_budget,
        "generator": {
            "output_kw": generator_output,
            "fuel_pct": generator_fuel,
            "request": row.generator_request if row else None,
        },
        "recent_transitions": [
            {
                "from_state": t.from_state,
                "to_state": t.to_state,
                "trigger": t.trigger,
                "reason": t.reason,
                "actor": t.actor,
                "occurred_at": _iso(t.occurred_at),
            }
            for t in transitions
        ],
    }


# ---------------------------------------------------------------------------
# Availability roll-ups for enum points (comms, security, agriculture)
# ---------------------------------------------------------------------------


def _state_rollup(
    snapshot: RegistrySnapshot,
    *,
    domains: Sequence[str],
    point_names: Sequence[str],
    asset_classes: Sequence[str] = (),
) -> dict:
    """Distribution of an enumerated state point across a set of assets."""
    scope_assets = [
        a
        for a in snapshot.assets.values()
        if a.domain in set(domains) and (not asset_classes or a.asset_class in set(asset_classes))
    ]
    if not scope_assets:
        return {
            "status": STATUS_NOT_DEPLOYED,
            "available": False,
            "asset_count": 0,
            "reporting": 0,
            "by_value": {},
            "items": [],
            "note": STATUS_EXPLANATIONS[STATUS_NOT_DEPLOYED],
        }

    scope_ids = {a.asset_id for a in scope_assets}
    items: list[dict] = []
    counts: Counter[str] = Counter()
    stale_count = 0
    for name in point_names:
        for point in snapshot.points_by_name.get(name, []):
            if point.asset_id not in scope_ids:
                continue
            asset = snapshot.assets[point.asset_id]
            state = snapshot.states.get(point.point_id)
            if state is None:
                items.append(
                    {
                        "asset_id": asset.asset_id,
                        "asset_name": asset.name,
                        "point_id": point.point_id,
                        "value": None,
                        "status": STATUS_NO_DATA,
                        "ts": None,
                    }
                )
                continue
            stale = _is_stale(state, point, snapshot.settings, snapshot.now)
            stale_count += 1 if stale else 0
            value = _current_value(state)
            counts[str(value)] += 1
            items.append(
                {
                    "asset_id": asset.asset_id,
                    "asset_name": asset.name,
                    "point_id": point.point_id,
                    "value": value,
                    "quality": state.quality,
                    "status": STATUS_STALE if stale else STATUS_OK,
                    "ts": _iso(state.ts),
                }
            )

    deployed = sum(1 for a in scope_assets if a.status in DEPLOYED_STATUSES)
    if not items:
        status_name = STATUS_DESIGN_ONLY if deployed == 0 else STATUS_NO_POINTS
    elif not counts:
        status_name = STATUS_NO_DATA
    elif stale_count == len(counts):
        status_name = STATUS_STALE
    else:
        status_name = STATUS_OK

    return {
        "status": status_name,
        "available": status_name in (STATUS_OK, STATUS_STALE),
        "asset_count": len(scope_assets),
        "deployed_asset_count": deployed,
        "reporting": sum(counts.values()),
        "by_value": dict(counts),
        "items": items[:60],
        "note": STATUS_EXPLANATIONS.get(status_name),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


ALL_ROLE_POINT_NAMES: tuple[str, ...] = tuple(
    sorted(
        {name for spec in METRICS.values() for source in spec.sources for name in source.point_names}
        | {
            "availability_state",
            "wan_state",
            "vpn_state",
            "voice_gateway_state",
            "door_state",
            "occupancy_state",
            "accessibility_state",
            "smoke_active",
            "leak_active",
            "fault_active",
            "state_operating",
            "mode_actual",
            "mode_requested",
            "manual_override_active",
            "interlock_permissive",
            "interlock_block_reason",
            "control_owner",
            "shed_state",
            "enabled_actual",
            "enabled_requested",
            "humidity_relative_pct",
            "soil_moisture_pct",
            "temperature_air_c",
            "load_tier",
            "power_kw",
        }
    )
)


@router.get(
    "/overview",
    summary="FR-001 single-request property overview",
    description=(
        "Everything the home screen needs in one request: site state and mode, energy reserve, "
        "active alarms, water reserve, communications, security and per-subsystem health. "
        "Unavailable data is reported as an explicit status, never as zero."
    ),
)
def get_overview(
    session: DbSession,
    settings: AppSettings,
    principal: CurrentPrincipal,
    alarm_limit: int = Query(8, ge=1, le=100, description="How many top alarms to inline."),
) -> dict:
    reader = Reader(session)
    snapshot = _load_snapshot(reader, settings, ALL_ROLE_POINT_NAMES)

    alarm_summary, _ = _alarm_summary(reader, snapshot, alarm_limit)
    energy = _energy_block(reader, snapshot)
    subsystems = _subsystem_rollup(reader, settings)
    site_state = _site_operating_state(snapshot, alarm_summary, energy, subsystems)

    water_pct = _evaluate_metric(snapshot, METRICS["water_reserve_pct"])
    water_level = _evaluate_metric(snapshot, METRICS["water_level_m"])
    water_assets = snapshot.domain_assets(["water"])
    water = {
        "status": _worst_status([water_pct["status"], water_level["status"]]),
        "available": water_pct["available"] or water_level["available"],
        "asset_count": len(water_assets),
        "reserve_pct": water_pct,
        "level_m": water_level,
        "note": (
            "No water assets exist in the register yet (SDD 12.3 water systems are a later phase). "
            "Water reserve is unavailable, not zero."
            if not water_assets
            else None
        ),
    }

    comms = _state_rollup(
        snapshot,
        domains=("it",),
        point_names=("availability_state", "wan_state", "vpn_state", "voice_gateway_state"),
    )
    comms["platform"] = {
        "node_role": settings.node_role,
        "site_id": settings.site_id,
        "version": __version__,
        "mqtt_enabled": settings.mqtt_enabled,
        "mqtt_host": settings.mqtt_host,
        "historian_backend": settings.historian_backend,
        "physical_control_enabled": settings.allow_physical_control,
        "ems_enabled": settings.ems_enabled,
        "alarm_engine_enabled": settings.alarm_engine_enabled,
        "server_time": _iso(snapshot.now),
    }

    security = _state_rollup(
        snapshot,
        domains=("security",),
        point_names=("availability_state", "door_state", "occupancy_state", "accessibility_state"),
    )
    safety = _state_rollup(
        snapshot,
        domains=("safety",),
        point_names=("smoke_active", "leak_active", "fault_active", "availability_state"),
    )

    outside_temp = _evaluate_metric(snapshot, METRICS["outside_temperature_c"])
    freeze_risk: dict[str, Any] = {"status": outside_temp["status"], "available": False, "risk": None}
    if outside_temp["available"] and outside_temp["value"] is not None:
        value = outside_temp["value"]
        freeze_risk = {
            "status": outside_temp["status"],
            "available": True,
            "risk": "freeze" if value <= 2 else ("heat" if value >= 32 else "none"),
            "temperature_c": value,
            "note": "Derived from the site air temperature point.",
        }
    else:
        freeze_risk["note"] = (
            "No site weather station exists in the register yet; freeze/heat risk cannot be assessed."
        )

    agriculture_assets = snapshot.domain_assets(["agriculture"])
    agriculture = {
        "status": STATUS_NOT_DEPLOYED if not agriculture_assets else STATUS_NO_DATA,
        "asset_count": len(agriculture_assets),
        "greenhouse": _state_rollup(
            snapshot,
            domains=("agriculture",),
            point_names=("availability_state", "temperature_air_c", "humidity_relative_pct"),
            asset_classes=("greenhouse", "greenhouse_zone", "environmental_monitor"),
        ),
        "orchard": _state_rollup(
            snapshot,
            domains=("agriculture",),
            point_names=("soil_moisture_pct", "availability_state"),
            asset_classes=("orchard_block", "irrigation_zone", "tree", "bed"),
        ),
        "note": (
            "Agriculture assets are a later deployment phase (SDD 20 phase D)."
            if not agriculture_assets
            else None
        ),
    }

    return {
        "generated_at": _iso(snapshot.now),
        "requested_by": {"name": principal.name, "role": principal.role},
        "status_vocabulary": STATUS_EXPLANATIONS,
        "data_sources": reader.report(),
        "site": {
            "site_id": settings.site_id,
            "timezone": settings.timezone,
            "operating_state": site_state,
            "operating_mode": _site_mode(reader, settings),
        },
        "energy": energy,
        "water": water,
        "alarms": alarm_summary,
        "communications": comms,
        "security": security,
        "safety": safety,
        "weather": {"outside_temperature_c": outside_temp, "risk": freeze_risk},
        "agriculture": agriculture,
        "subsystems": subsystems,
    }


@router.get(
    "/overview/subsystems",
    summary="Per-domain health roll-up",
    description=(
        "Asset counts by lifecycle status, active alarms, point/telemetry coverage and the "
        "unresolved open_fields that still block the design, for every SDD 25.4 domain."
    ),
)
def get_subsystems(session: DbSession, settings: AppSettings) -> dict:
    reader = Reader(session)
    domains = _subsystem_rollup(reader, settings, detailed=True)
    totals = {
        "assets": sum(d["assets"]["total"] for d in domains),
        "deployed": sum(d["assets"]["deployed"] for d in domains),
        "active_alarms": sum(d["alarms"]["active_total"] for d in domains),
        "unresolved_open_fields": sum(d["open_fields"]["unresolved_count"] for d in domains),
        "domains_with_assets": sum(1 for d in domains if d["assets"]["total"]),
        "domains_not_deployed": sum(1 for d in domains if d["health"] == STATUS_NOT_DEPLOYED),
    }
    return {
        "generated_at": _iso(utcnow()),
        "status_vocabulary": STATUS_EXPLANATIONS,
        "data_sources": reader.report(),
        "totals": totals,
        "domains": domains,
    }


@router.get(
    "/overview/map",
    summary="Property map as GeoJSON",
    description=(
        "GeoJSON FeatureCollection built from the locations table (SDD 17.2). Only assets that "
        "genuinely have coordinates are emitted; everything still awaiting survey is listed in "
        "pending_placement. No position is ever invented."
    ),
)
def get_map(session: DbSession, settings: AppSettings) -> dict:
    reader = Reader(session)
    assets = {a.asset_id: a for a in reader.scalars(select(Asset), "assets")}
    locations = {loc.asset_id: loc for loc in reader.scalars(select(Location), "locations")}

    alarms = _active_alarms(reader)
    alarms_by_asset: dict[str, list[Alarm]] = defaultdict(list)
    for alarm in alarms:
        if alarm.asset_id:
            alarms_by_asset[alarm.asset_id].append(alarm)

    features: list[dict] = []
    pending: list[dict] = []
    lons: list[float] = []
    lats: list[float] = []

    for asset_id, asset in sorted(assets.items()):
        location = locations.get(asset_id)
        geometry: dict | None = None
        if location is not None:
            if isinstance(location.geometry, dict) and location.geometry.get("type"):
                geometry = location.geometry
            elif location.latitude is not None and location.longitude is not None:
                geometry = {
                    "type": "Point",
                    "coordinates": [location.longitude, location.latitude],
                }

        if geometry is None:
            if location is None:
                reason = "no_location_record"
            else:
                reason = "location_record_without_coordinates"
            pending.append(
                {
                    "asset_id": asset_id,
                    "name": asset.name,
                    "domain": asset.domain,
                    "asset_class": asset.asset_class,
                    "status": asset.status,
                    "criticality": asset.criticality,
                    "reason": reason,
                    "structure_id": location.structure_id if location else None,
                    "room_id": location.room_id if location else None,
                    "description": location.description if location else None,
                    "open_fields": list(asset.open_fields or []),
                }
            )
            continue

        domain_alarms = alarms_by_asset.get(asset_id, [])
        worst = min(
            (SEVERITY_RANK.get(a.severity, 99) for a in domain_alarms),
            default=None,
        )
        features.append(
            {
                "type": "Feature",
                "id": asset_id,
                "geometry": geometry,
                "properties": {
                    "asset_id": asset_id,
                    "name": asset.name,
                    "domain": asset.domain,
                    "asset_class": asset.asset_class,
                    "status": asset.status,
                    "criticality": asset.criticality,
                    "control_authority": asset.control_authority,
                    "elevation_m": location.elevation_m if location else None,
                    "description": location.description if location else None,
                    "active_alarms": len(domain_alarms),
                    "worst_alarm_severity": SEVERITY_ORDER[worst] if worst is not None else None,
                    "open_fields": list(asset.open_fields or []),
                },
            }
        )
        if geometry.get("type") == "Point":
            coords = geometry.get("coordinates") or []
            if len(coords) >= 2:
                lons.append(float(coords[0]))
                lats.append(float(coords[1]))

    pending_by_domain = Counter(item["domain"] for item in pending)

    payload: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": features,
        "pending_placement": pending,
        "metadata": {
            "generated_at": _iso(utcnow()),
            "site_id": settings.site_id,
            "data_sources": reader.report(),
            "crs": "urn:ogc:def:crs:OGC:1.3:CRS84",
            "placed_count": len(features),
            "pending_count": len(pending),
            "pending_by_domain": dict(sorted(pending_by_domain.items())),
            "survey_status": "not_surveyed" if not features else "partial" if pending else "complete",
            "note": (
                "The property has not been surveyed. Asset coordinates are deliberately TBD in the "
                "v0.3 design package (SDD 22 open decision 7), so no positions are shown."
                if not features
                else "Only assets with recorded coordinates are drawn. The rest await survey."
            ),
        },
    }
    if lons and lats:
        payload["bbox"] = [min(lons), min(lats), max(lons), max(lats)]
    return payload


@router.get(
    "/overview/control/{asset_id:path}",
    summary="SDD 17.4 control presentation for one asset",
    description=(
        "Actual state, requested state, local/remote authority, interlocks preventing operation, "
        "last command and issuer, manual override status, and the impact on energy and resource "
        "budgets -- the seven things SDD 17.4 requires of every control."
    ),
)
def get_control(asset_id: str, session: DbSession, settings: AppSettings) -> dict:
    reader = Reader(session)
    asset = reader.get(Asset, asset_id, "assets")
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown asset: {asset_id}")

    points = reader.scalars(select(Point).where(Point.asset_id == asset_id), "points")
    point_ids = [p.point_id for p in points]
    states: dict[str, CurrentState] = {}
    bindings: dict[str, PointBinding] = {}
    if point_ids:
        stmt = select(CurrentState).where(CurrentState.point_id.in_(point_ids))
        states = {s.point_id: s for s in reader.scalars(stmt, "current_state")}
        stmt = select(PointBinding).where(PointBinding.point_id.in_(point_ids))
        bindings = {b.point_id: b for b in reader.scalars(stmt, "point_bindings")}

    now = utcnow()

    def _point_view(name: str) -> dict | None:
        point = next((p for p in points if p.point_name == name), None)
        if point is None:
            return None
        state = states.get(point.point_id)
        binding = bindings.get(point.point_id)
        view: dict[str, Any] = {
            "point_id": point.point_id,
            "point_name": name,
            "point_class": point.point_class,
            "unit": point.unit,
            "data_type": point.data_type,
            "enum_values": point.enum_values,
            "control_capable": bool(point.control_capable),
            "automatic_control_allowed": bool(point.automatic_control_allowed),
            "binding_status": binding.binding_status if binding else "unbound",
            "value": None,
            "quality": None,
            "ts": None,
            "status": STATUS_NO_DATA,
            "requested_value": None,
            "requested_at": None,
        }
        if state is not None:
            stale = _is_stale(state, point, settings, now)
            view.update(
                {
                    "value": _current_value(state),
                    "quality": state.quality,
                    "source": state.source,
                    "ts": _iso(state.ts),
                    "status": STATUS_STALE if stale else STATUS_OK,
                    "stale": stale,
                    "requested_value": state.requested_value,
                    "requested_at": _iso(state.requested_at),
                    "alarm_state": state.alarm_state,
                }
            )
        return view

    actual_names = ("state_operating", "enabled_actual", "mode_actual", "availability_state", "shed_state")
    requested_names = ("enabled_requested", "mode_requested", "power_request_kw")

    actual_state = {name: v for name in actual_names if (v := _point_view(name)) is not None}
    requested_state = {name: v for name in requested_names if (v := _point_view(name)) is not None}

    # Any control-capable point can also carry a pending request on its own row.
    pending_requests = []
    for point in points:
        state = states.get(point.point_id)
        if state is not None and state.requested_value is not None:
            pending_requests.append(
                {
                    "point_id": point.point_id,
                    "point_name": point.point_name,
                    "requested_value": state.requested_value,
                    "requested_at": _iso(state.requested_at),
                    "actual_value": _current_value(state),
                    "matches_actual": _current_value(state) == state.requested_value,
                }
            )

    # --- commands ------------------------------------------------------
    commands = reader.scalars(
        select(Command).where(Command.asset_id == asset_id).order_by(Command.issued_at.desc()).limit(10),
        "commands",
    )
    last_command = commands[0] if commands else None

    def _command_view(cmd: Command) -> dict:
        return {
            "command_id": cmd.command_id,
            "command": cmd.command,
            "point_id": cmd.point_id,
            "value": cmd.value,
            "issued_by": cmd.issued_by,
            "issued_by_kind": cmd.issued_by_kind,
            "reason": cmd.reason,
            "operating_mode": cmd.operating_mode,
            "issued_at": _iso(cmd.issued_at),
            "completed_at": _iso(cmd.completed_at),
            "state": cmd.state,
            "state_reason": cmd.state_reason,
            "interlocks_evaluated": list(cmd.interlocks_evaluated or []),
        }

    # --- interlocks ----------------------------------------------------
    interlocks: list[dict] = []
    interlock_source = None
    for cmd in commands:
        if cmd.interlocks_evaluated:
            interlock_source = cmd
            break
    if interlock_source is not None:
        for entry in interlock_source.interlocks_evaluated or []:
            interlocks.append(
                _normalise_interlock(
                    entry,
                    source="command",
                    evaluated_at=_iso(interlock_source.issued_at),
                    command_id=interlock_source.command_id,
                )
            )

    permissive = _point_view("interlock_permissive")
    block_reason = _point_view("interlock_block_reason")
    if permissive is not None and permissive.get("value") is not None:
        interlocks.append(
            {
                "name": "interlock_permissive",
                "passed": bool(permissive["value"]),
                "blocking": not bool(permissive["value"]),
                "blocks_dispatch": not bool(permissive["value"]),
                "detail": (block_reason or {}).get("value"),
                "context": None,
                "override_by": None,
                "source": "live_point",
                "evaluated_at": permissive.get("ts"),
                "command_id": None,
            }
        )

    blocking = [i for i in interlocks if i.get("blocking")]
    dispatch_blockers = [i for i in interlocks if i.get("blocks_dispatch")]

    # --- authority and mode -------------------------------------------
    override_point = _point_view("manual_override_active")
    control_owner = _point_view("control_owner")
    manual_override_record = dict(asset.manual_override or {})
    override_active = None
    if override_point is not None and override_point.get("value") is not None:
        override_active = bool(override_point["value"])
    elif manual_override_record.get("active") is not None:
        override_active = bool(manual_override_record["active"])

    asset_mode = _mode_for(reader, "asset", asset_id)
    domain_mode = _mode_for(reader, "domain", asset.domain)
    site_mode_row = _mode_for(reader, "site", settings.site_id)
    effective_mode_row = asset_mode or domain_mode or site_mode_row
    effective_mode = {
        "mode": effective_mode_row.mode if effective_mode_row else None,
        "scope": ("asset" if asset_mode else "domain" if domain_mode else "site" if site_mode_row else None),
        "changed_by": effective_mode_row.changed_by if effective_mode_row else None,
        "changed_at": _iso(effective_mode_row.changed_at) if effective_mode_row else None,
        "reason": effective_mode_row.reason if effective_mode_row else None,
        "status": STATUS_OK if effective_mode_row else STATUS_NO_DATA,
        "note": None if effective_mode_row else "No operating mode has been set at any scope.",
    }

    local_or_remote = "unknown"
    if override_active:
        local_or_remote = "local"
    elif control_owner is not None and control_owner.get("value"):
        local_or_remote = str(control_owner["value"])
    elif asset.control_authority in ("local_only", "manual", "safety_interlock"):
        local_or_remote = "local"
    elif asset.control_authority in ("supervisory", "local_controller_with_supervisory_setpoints"):
        local_or_remote = "remote_supervisory"
    elif asset.control_authority == "vendor_native":
        local_or_remote = "vendor_native"
    elif asset.control_authority == "none":
        local_or_remote = "not_controllable"

    # --- budget impact -------------------------------------------------
    profile = reader.get(PowerLoadProfile, asset_id, "power_load_profiles")
    leases = reader.scalars(
        select(PowerBudgetLease)
        .where(PowerBudgetLease.asset_id == asset_id)
        .order_by(PowerBudgetLease.starts_at.desc())
        .limit(10),
        "power_budget_leases",
    )
    energy_snapshot = reader.first(
        select(EnergyStateSnapshot).order_by(EnergyStateSnapshot.id), "energy_state_snapshot"
    )
    measured_power = _point_view("power_kw")

    shed_groups_active = list(energy_snapshot.shed_groups_active or []) if energy_snapshot else []
    budget = {
        "load_profile": (
            {
                "name": profile.name,
                "base_tier": profile.base_tier,
                "effective_tier": profile.effective_tier
                if profile.effective_tier is not None
                else profile.base_tier,
                "tier_override_reason": profile.tier_override_reason,
                "tier_override_expires_at": _iso(profile.tier_override_expires_at),
                "criticality": profile.criticality,
                "rated_power_kw": profile.rated_power_kw,
                "estimated_power_kw": profile.estimated_power_kw,
                "measured_power_point": profile.measured_power_point,
                "control_method": profile.control_method,
                "shed_group": profile.shed_group,
                "shed_order": profile.shed_order,
                "restoration_group": profile.restoration_group,
                "restoration_order": profile.restoration_order,
                "minimum_on_time_s": profile.minimum_on_time_s,
                "minimum_off_time_s": profile.minimum_off_time_s,
                "data_status": profile.data_status,
                "open_fields": list(profile.open_fields or []),
            }
            if profile
            else None
        ),
        "load_profile_status": STATUS_OK if profile else STATUS_NO_DATA,
        "load_profile_note": (
            None
            if profile
            else "No load profile recorded; the energy impact of operating this asset is unknown."
        ),
        "measured_power_kw": measured_power,
        "active_leases": [
            {
                "lease_id": lease.lease_id,
                "granted_kw": lease.granted_kw,
                "starts_at": _iso(lease.starts_at),
                "expires_at": _iso(lease.expires_at),
                "priority": lease.priority,
                "reason": lease.reason,
                "revocable": bool(lease.revocable),
                "requested_by": lease.requested_by,
                "state": lease.state,
            }
            for lease in leases
        ],
        "energy_state": energy_snapshot.state if energy_snapshot else None,
        "shed_groups_active": shed_groups_active,
        "currently_shed": bool(profile and profile.shed_group and profile.shed_group in shed_groups_active),
    }

    control_points = [
        {
            "point_id": p.point_id,
            "point_name": p.point_name,
            "point_class": p.point_class,
            "data_type": p.data_type,
            "unit": p.unit,
            "enum_values": p.enum_values,
            "automatic_control_allowed": bool(p.automatic_control_allowed),
            "binding_status": (
                bindings.get(p.point_id).binding_status if bindings.get(p.point_id) else "unbound"
            ),
            "current_value": (_current_value(states[p.point_id]) if p.point_id in states else None),
            "requested_value": (states[p.point_id].requested_value if p.point_id in states else None),
        }
        for p in points
        if p.control_capable
    ]

    commissioned = any(b.binding_status in ("commissioned", "verified", "active") for b in bindings.values())
    commandable = {
        "asset_status": asset.status,
        "control_authority": asset.control_authority,
        "control_point_count": len(control_points),
        "physical_control_enabled": settings.allow_physical_control,
        "any_binding_commissioned": commissioned,
        "blocking_interlocks": len(blocking),
        "manual_override_active": override_active,
        "allowed": bool(
            control_points
            and settings.allow_physical_control
            and not blocking
            and asset.control_authority not in ("none",)
        ),
        "reasons": [
            reason
            for reason in (
                None if control_points else "No control-capable points are registered for this asset.",
                None
                if settings.allow_physical_control
                else "Physical control is globally disabled until commissioning (SDD 19).",
                None if not blocking else f"{len(blocking)} interlock(s) are blocking operation.",
                None
                if asset.control_authority != "none"
                else "This asset has no control authority; it is monitored only.",
                None if override_active is not True else "Manual override is active at the equipment.",
            )
            if reason
        ],
    }

    return {
        "generated_at": _iso(now),
        "data_sources": reader.report(),
        "asset": {
            "asset_id": asset.asset_id,
            "name": asset.name,
            "domain": asset.domain,
            "asset_class": asset.asset_class,
            "status": asset.status,
            "criticality": asset.criticality,
            "control_authority": asset.control_authority,
            "parent_id": asset.parent_id,
            "open_fields": list(asset.open_fields or []),
            "dependencies": list(asset.dependencies or []),
        },
        "actual_state": actual_state,
        "requested_state": requested_state,
        "pending_requests": pending_requests,
        "authority": {
            "declared": asset.control_authority,
            "local_or_remote": local_or_remote,
            "reported_owner": (control_owner or {}).get("value"),
            "reported_owner_point": (control_owner or {}).get("point_id"),
            "status": STATUS_OK if control_owner and control_owner.get("value") else STATUS_NO_DATA,
            "note": (
                None
                if control_owner and control_owner.get("value")
                else "No control_owner point is reporting; authority shown is the registry declaration."
            ),
        },
        "operating_mode": effective_mode,
        "interlocks": {
            "status": STATUS_OK if interlocks else STATUS_NO_DATA,
            "evaluated": interlocks,
            "blocking": blocking,
            "blocking_count": len(blocking),
            "dispatch_blockers": dispatch_blockers,
            "dispatch_blocker_count": len(dispatch_blockers),
            "note": (None if interlocks else "No interlock evaluation has been recorded for this asset yet."),
        },
        "last_command": _command_view(last_command) if last_command else None,
        "recent_commands": [_command_view(c) for c in commands],
        "command_history_status": STATUS_OK if commands else STATUS_NO_DATA,
        "manual_override": {
            "active": override_active,
            "status": STATUS_OK if override_active is not None else STATUS_NO_DATA,
            "record": manual_override_record,
            "live_point": override_point,
            "note": (
                None
                if override_active is not None
                else "No manual-override record or point exists; override state is unknown."
            ),
        },
        "budget_impact": budget,
        "control_points": control_points,
        "commandable": commandable,
    }
