"""Applies validated telemetry to the current-state cache and the historian.

Design position (SDD 26.6, 40.1, 40.2):

* The current-state row is the platform's answer to "what is true now". It
  carries value, quality, provenance and the timeout that makes *absence* of
  data detectable.
* Every accepted message is also offered to the historian. The registry keeps
  the series pointer (``point_samples_index``); the sample rows are the
  relational historian fallback that keeps the platform whole on SQLite.
* **Nothing is silently coerced.** A value whose Python type contradicts the
  registry's ``data_type``, an enum outside its allowed set, or a unit that
  disagrees with the point dictionary is a data-integrity fault: the value is
  marked ``bad`` and the reason is handed back for dead-lettering. Quietly
  casting ``"73.4"`` to a float, or accepting ``kWh`` where the dictionary says
  ``kW``, converts a wiring mistake into a plausible-looking number, and
  plausible-looking wrong numbers are what drive bad control decisions.

Quality precedence: the device's declared quality is honoured, and any fault
detected here escalates it to ``bad``. A fault never silently improves quality.

Historian policy (``point_bindings.historian_policy``, else ``points``):

===================  ========================================================
``none``             current state only, no sample row
``event_on_change``  sample written only when the value differs from current
everything else      every accepted message produces a sample row
===================  ========================================================
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chaos.config import Settings, get_settings
from chaos.envelope import AvailabilityEnvelope, TelemetryEnvelope
from chaos.models.base import utcnow
from chaos.models.registry import Point, PointBinding, PointSampleIndex
from chaos.models.telemetry import CurrentState, TelemetrySample

logger = logging.getLogger(__name__)

#: Point name carrying device availability (SDD 26.5).
AVAILABILITY_POINT = "availability_state"

#: Problem codes. These land in ``ingest_dead_letters.reason``.
UNKNOWN_POINT = "unknown_point"
VALUE_TYPE_MISMATCH = "value_type_mismatch"
ENUM_VIOLATION = "enum_violation"
UNIT_MISMATCH = "unit_mismatch"
OUT_OF_PHYSICAL_RANGE = "out_of_physical_range"
OUT_OF_ORDER = "out_of_order"
SEQUENCE_GAP = "sequence_gap"

#: Faults that make a value untrustworthy.
_BAD_QUALITY_PROBLEMS = frozenset({VALUE_TYPE_MISMATCH, ENUM_VIOLATION, UNIT_MISMATCH, OUT_OF_PHYSICAL_RANGE})

#: Quality policies that refuse to publish a faulted value as current state.
_REJECTING_POLICIES = frozenset({"reject_invalid", "reject_outside_physical_range"})

#: Qualities that a device going offline invalidates. ``bad``/``maintenance``
#: are already more specific than ``stale`` and are left alone.
_STALEABLE_QUALITIES = frozenset({"good", "uncertain"})

# Value-kind taxonomy, derived from ``points.data_type``.
_NUMERIC_FLOAT = "numeric"
_NUMERIC_INT = "integer"
_BOOLEAN = "boolean"
_ENUM = "enum"
_TEXT = "text"
_JSON = "json"

_DATA_TYPE_KINDS = {
    "float": _NUMERIC_FLOAT,
    "double": _NUMERIC_FLOAT,
    "number": _NUMERIC_FLOAT,
    "real": _NUMERIC_FLOAT,
    "decimal": _NUMERIC_FLOAT,
    "integer": _NUMERIC_INT,
    "int": _NUMERIC_INT,
    "counter": _NUMERIC_INT,
    "boolean": _BOOLEAN,
    "bool": _BOOLEAN,
    "enum": _ENUM,
    "string": _TEXT,
    "text": _TEXT,
    "object": _JSON,
    "json": _JSON,
    "dict": _JSON,
}


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Normalise a timestamp to aware UTC.

    SQLite drops the offset from ``DateTime(timezone=True)`` columns, so values
    read back from the historian are naive. Platform time is UTC everywhere
    (SDD 16.3), so a naive value is interpreted as UTC rather than rejected.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


@dataclass(frozen=True)
class Problem:
    """One detected fault, ready to become a dead letter."""

    code: str
    detail: str = ""

    @property
    def reason(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail else self.code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.reason


@dataclass(frozen=True)
class PointMeta:
    """Flattened registry view of one point: definition plus current binding.

    A plain snapshot rather than an ORM instance so it can be cached across
    sessions without detached-instance hazards.
    """

    point_id: str
    asset_id: str
    point_name: str
    point_class: str
    data_type: str
    kind: str
    unit: str | None
    enum_values: tuple[str, ...] | None
    historian_policy: str
    quality_policy: str | None
    stale_after_s: int
    minimum_physical: float | None
    maximum_physical: float | None

    @property
    def rejects_faulted_values(self) -> bool:
        return (self.quality_policy or "").lower() in _REJECTING_POLICIES


@dataclass
class WriteResult:
    """Outcome of applying one telemetry envelope."""

    point_id: str
    known_point: bool = False
    quality: str = "bad"
    current_state_updated: bool = False
    historised: bool = False
    out_of_order: bool = False
    sequence_gap: int | None = None
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the message was accepted without any integrity fault."""
        return self.known_point and not self.problems

    @property
    def dead_letter_reasons(self) -> list[str]:
        """Faults worth recording. Ordering/timing warnings are counters only."""
        return [p.reason for p in self.problems if p.code not in (OUT_OF_ORDER, SEQUENCE_GAP)]

    def has(self, code: str) -> bool:
        return any(p.code == code for p in self.problems)


@dataclass
class AvailabilityResult:
    """Outcome of applying one availability / last-will message."""

    asset_id: str
    state: str
    availability_point_updated: bool = False
    points_marked_stale: int = 0


class TelemetryWriter:
    """Applies envelopes to the database. The caller owns the transaction.

    ``apply`` and ``apply_availability`` never commit, so a subscriber can batch
    a burst of messages into one transaction. ``write`` is the convenience path
    that manages (and commits) its own session.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        cache_metadata: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self._session_factory = session_factory
        self._cache_metadata = cache_metadata
        self._meta_cache: dict[str, PointMeta] = {}
        self.counters: dict[str, int] = {
            "applied": 0,
            "current_state_updated": 0,
            "historised": 0,
            "out_of_order": 0,
            "sequence_gaps": 0,
            "unknown_points": 0,
            "type_mismatches": 0,
            "enum_violations": 0,
            "unit_mismatches": 0,
            "range_violations": 0,
            "stale_marked": 0,
            "availability_applied": 0,
        }

    # -- registry metadata ----------------------------------------------

    def invalidate(self) -> None:
        """Drop cached point metadata (call after a registry reload)."""
        self._meta_cache.clear()

    def point_meta(self, session: Session, point_id: str) -> PointMeta | None:
        """Registry view of ``point_id``, or ``None`` if it is not registered."""
        cached = self._meta_cache.get(point_id)
        if cached is not None:
            return cached

        point = session.get(Point, point_id)
        if point is None:
            return None
        binding = session.get(PointBinding, point_id)
        limits = point.limits or {}
        enum_values = point.enum_values or None

        meta = PointMeta(
            point_id=point.point_id,
            asset_id=point.asset_id,
            point_name=point.point_name,
            point_class=point.point_class,
            data_type=point.data_type,
            kind=_DATA_TYPE_KINDS.get((point.data_type or "").lower(), _TEXT),
            unit=point.unit,
            enum_values=tuple(str(v) for v in enum_values) if enum_values else None,
            historian_policy=(
                (binding.historian_policy if binding else None) or point.historian_policy or "standard"
            ),
            quality_policy=binding.quality_policy if binding else None,
            stale_after_s=(
                (binding.stale_after_s if binding else None)
                or point.stale_after_s
                or self.settings.default_stale_after_s
            ),
            minimum_physical=_as_float(limits.get("minimum_physical")),
            maximum_physical=_as_float(limits.get("maximum_physical")),
        )
        if self._cache_metadata:
            self._meta_cache[point_id] = meta
        return meta

    # -- telemetry -------------------------------------------------------

    def apply(
        self,
        session: Session,
        envelope: TelemetryEnvelope,
        *,
        now: dt.datetime | None = None,
        source: str | None = None,
    ) -> WriteResult:
        """Apply one envelope. Does not commit.

        ``source`` overrides the envelope's declared source, which is how
        simulated data is kept distinguishable from field telemetry.
        """
        now = as_utc(now) or utcnow()
        point_id = envelope.point_id
        result = WriteResult(point_id=point_id, quality=envelope.quality)

        meta = self.point_meta(session, point_id)
        if meta is None:
            self.counters["unknown_points"] += 1
            result.problems.append(Problem(UNKNOWN_POINT, f"{point_id} is not in the registry"))
            return result
        result.known_point = True
        self.counters["applied"] += 1

        columns, problems = self._coerce(meta, envelope.value)
        problems.extend(self._check_unit(meta, envelope))
        problems.extend(self._check_range(meta, columns.get("value_numeric")))
        result.problems.extend(problems)
        self._count_problems(problems)

        quality = envelope.quality
        if any(p.code in _BAD_QUALITY_PROBLEMS for p in problems):
            quality = "bad"
        result.quality = quality

        value_valid = not any(p.code in (VALUE_TYPE_MISMATCH, ENUM_VIOLATION) for p in problems)
        # A rejecting quality policy refuses to publish a faulted value as the
        # asset's current state; the fault is still visible via quality + the
        # historian row (SDD 26.6, 29 ``quality_policy``).
        publish_value = value_valid and not (
            meta.rejects_faulted_values and any(p.code in _BAD_QUALITY_PROBLEMS for p in problems)
        )

        ts = as_utc(envelope.ts) or now
        effective_source = source or envelope.source

        state = session.get(CurrentState, point_id)
        stored_ts = as_utc(state.ts) if state is not None else None
        # Snapshot before mutation so ``event_on_change`` compares against the
        # previous value rather than the one just written.
        previous = _value_snapshot(state) if state is not None else None

        if state is not None and envelope.sequence is not None and state.sequence is not None:
            gap = envelope.sequence - state.sequence - 1
            if gap > 0:
                result.sequence_gap = gap
                self.counters["sequence_gaps"] += 1
                result.problems.append(
                    Problem(SEQUENCE_GAP, f"{point_id} missed {gap} message(s) before {envelope.sequence}")
                )
                logger.warning("Sequence gap of %d on %s", gap, point_id)

        if stored_ts is not None and ts < stored_ts:
            # Out-of-order delivery must never rewind the twin's view of now.
            # The sample is still historised so the timeline stays complete.
            result.out_of_order = True
            self.counters["out_of_order"] += 1
            result.problems.append(
                Problem(OUT_OF_ORDER, f"{point_id} ts {ts.isoformat()} older than {stored_ts.isoformat()}")
            )
            logger.info("Out-of-order sample for %s (%s < %s)", point_id, ts, stored_ts)
        else:
            if state is None:
                state = CurrentState(point_id=point_id, asset_id=meta.asset_id, point_name=meta.point_name)
                session.add(state)
            if publish_value:
                _assign_value(state, columns)
            state.unit = meta.unit
            state.quality = quality
            state.source = effective_source
            state.sequence = envelope.sequence
            state.schema_version = envelope.schema_version
            state.ts = ts
            state.received_at = now
            state.stale_after_s = meta.stale_after_s
            result.current_state_updated = True
            self.counters["current_state_updated"] += 1

        if self._should_historise(meta, columns, previous, publish_value):
            sample_columns = columns if publish_value else {}
            self._write_sample(
                session,
                meta,
                ts=ts,
                columns=sample_columns,
                quality=quality,
                source=effective_source,
                now=now,
            )
            result.historised = True
            self.counters["historised"] += 1

        return result

    def apply_many(
        self,
        session: Session,
        envelopes: Iterable[TelemetryEnvelope],
        *,
        now: dt.datetime | None = None,
        source: str | None = None,
    ) -> list[WriteResult]:
        """Apply a batch inside one transaction. Does not commit."""
        results = [self.apply(session, e, now=now, source=source) for e in envelopes]
        session.flush()
        return results

    def write(
        self,
        envelope: TelemetryEnvelope,
        *,
        now: dt.datetime | None = None,
        source: str | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> WriteResult:
        """Convenience path: open a session, apply, commit."""
        factory = session_factory or self._session_factory
        if factory is None:  # pragma: no cover - programmer error
            raise RuntimeError("TelemetryWriter.write requires a session_factory")
        session = factory()
        try:
            result = self.apply(session, envelope, now=now, source=source)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- availability ----------------------------------------------------

    def apply_availability(
        self,
        session: Session,
        envelope: AvailabilityEnvelope,
        *,
        now: dt.datetime | None = None,
        source: str | None = None,
    ) -> AvailabilityResult:
        """Apply a retained availability / last-will message (SDD 8.2).

        Going ``offline`` invalidates everything that asset was reporting: the
        values are no longer being refreshed, so they become ``stale``
        immediately rather than after each point's individual timeout. This is
        what stops an EMS or alarm rule from acting on a frozen reading.
        """
        now = as_utc(now) or utcnow()
        ts = as_utc(envelope.ts) or now
        result = AvailabilityResult(asset_id=envelope.asset_id, state=envelope.state)
        self.counters["availability_applied"] += 1

        point_id = f"{envelope.asset_id}/{AVAILABILITY_POINT}"
        meta = self.point_meta(session, point_id)
        if meta is not None:
            state_row = session.get(CurrentState, point_id)
            if state_row is None:
                state_row = CurrentState(
                    point_id=point_id, asset_id=meta.asset_id, point_name=meta.point_name
                )
                session.add(state_row)
            state_row.value_text = envelope.state
            state_row.value_numeric = None
            state_row.value_bool = None
            state_row.value_json = None
            state_row.unit = meta.unit
            state_row.quality = "good"
            state_row.source = source or envelope.source or "mqtt.availability"
            state_row.ts = ts
            state_row.received_at = now
            state_row.stale_after_s = meta.stale_after_s
            state_row.schema_version = envelope.schema_version
            result.availability_point_updated = True

        if envelope.state == "offline":
            rows = (
                session.execute(select(CurrentState).where(CurrentState.asset_id == envelope.asset_id))
                .scalars()
                .all()
            )
            for row in rows:
                if row.point_name == AVAILABILITY_POINT:
                    continue
                if row.quality in _STALEABLE_QUALITIES:
                    row.quality = "stale"
                    result.points_marked_stale += 1
            if result.points_marked_stale:
                self.counters["stale_marked"] += result.points_marked_stale
                logger.warning(
                    "Asset %s offline: %d point(s) marked stale",
                    envelope.asset_id,
                    result.points_marked_stale,
                )
        return result

    # -- staleness -------------------------------------------------------

    def mark_stale(self, session: Session, now: dt.datetime) -> int:
        """Demote timed-out ``good`` values to ``stale`` (SDD 5.5, 26.6).

        Absence of data is a reportable condition, not an absence of condition:
        without this sweep a dead sensor keeps presenting its last good reading
        forever. ``now`` is always supplied by the caller so the sweep is
        deterministic and testable.
        """
        now = as_utc(now) or utcnow()
        rows = session.execute(select(CurrentState).where(CurrentState.quality == "good")).scalars().all()
        marked = 0
        for row in rows:
            ts = as_utc(row.ts)
            if ts is None:
                continue
            timeout = row.stale_after_s or self.settings.default_stale_after_s
            if ts + dt.timedelta(seconds=timeout) < now:
                row.quality = "stale"
                marked += 1
        if marked:
            self.counters["stale_marked"] += marked
            logger.info("Staleness sweep marked %d point(s) stale", marked)
        return marked

    # -- internals -------------------------------------------------------

    def _coerce(self, meta: PointMeta, value: Any) -> tuple[dict[str, Any], list[Problem]]:
        """Route ``value`` into the right column, or report a mismatch.

        Booleans are checked before numbers: ``bool`` is a subclass of ``int``
        in Python, and letting ``True`` land in ``value_numeric`` as ``1.0``
        would be exactly the silent coercion this module refuses to do.
        """
        problems: list[Problem] = []
        if value is None:
            return {}, [Problem(VALUE_TYPE_MISMATCH, f"{meta.point_id} received a null value")]

        kind = meta.kind
        is_bool = isinstance(value, bool)

        if kind in (_NUMERIC_FLOAT, _NUMERIC_INT):
            if is_bool or not isinstance(value, (int, float)):
                return {}, [
                    Problem(
                        VALUE_TYPE_MISMATCH,
                        f"{meta.point_id} expects {meta.data_type}, got {type(value).__name__} {value!r}",
                    )
                ]
            if kind == _NUMERIC_INT and isinstance(value, float) and not value.is_integer():
                return {}, [
                    Problem(
                        VALUE_TYPE_MISMATCH,
                        f"{meta.point_id} expects an integer, got {value!r}",
                    )
                ]
            return {"value_numeric": float(value)}, problems

        if kind == _BOOLEAN:
            if not is_bool:
                return {}, [
                    Problem(
                        VALUE_TYPE_MISMATCH,
                        f"{meta.point_id} expects boolean, got {type(value).__name__} {value!r}",
                    )
                ]
            return {"value_bool": value}, problems

        if kind == _ENUM:
            if not isinstance(value, str):
                return {}, [
                    Problem(
                        VALUE_TYPE_MISMATCH,
                        f"{meta.point_id} expects an enum string, got {type(value).__name__} {value!r}",
                    )
                ]
            if meta.enum_values and value not in meta.enum_values:
                return {}, [
                    Problem(
                        ENUM_VIOLATION,
                        f"{meta.point_id} value {value!r} not in {list(meta.enum_values)}",
                    )
                ]
            return {"value_text": value}, problems

        if kind == _JSON:
            if not isinstance(value, (dict, list)):
                return {}, [
                    Problem(
                        VALUE_TYPE_MISMATCH,
                        f"{meta.point_id} expects an object, got {type(value).__name__} {value!r}",
                    )
                ]
            return {"value_json": value}, problems

        # _TEXT
        if not isinstance(value, str):
            return {}, [
                Problem(
                    VALUE_TYPE_MISMATCH,
                    f"{meta.point_id} expects a string, got {type(value).__name__} {value!r}",
                )
            ]
        return {"value_text": value}, problems

    def _check_unit(self, meta: PointMeta, envelope: TelemetryEnvelope) -> list[Problem]:
        """A unit that contradicts the dictionary is an integrity fault.

        The canonical unit for a point cannot change without a schema revision
        (SDD 26.7), so a device publishing a different one is either miswired or
        misconfigured. The stored unit stays canonical; the message is flagged.
        """
        if envelope.unit is None or meta.unit is None:
            return []
        if envelope.unit.strip() == meta.unit.strip():
            return []
        return [
            Problem(
                UNIT_MISMATCH,
                f"{meta.point_id} is {meta.unit!r} in the dictionary, message declared {envelope.unit!r}",
            )
        ]

    def _check_range(self, meta: PointMeta, numeric: float | None) -> list[Problem]:
        if numeric is None:
            return []
        low, high = meta.minimum_physical, meta.maximum_physical
        if low is not None and numeric < low:
            return [Problem(OUT_OF_PHYSICAL_RANGE, f"{meta.point_id} {numeric} < minimum {low}")]
        if high is not None and numeric > high:
            return [Problem(OUT_OF_PHYSICAL_RANGE, f"{meta.point_id} {numeric} > maximum {high}")]
        return []

    def _count_problems(self, problems: Iterable[Problem]) -> None:
        for problem in problems:
            if problem.code == VALUE_TYPE_MISMATCH:
                self.counters["type_mismatches"] += 1
            elif problem.code == ENUM_VIOLATION:
                self.counters["enum_violations"] += 1
            elif problem.code == UNIT_MISMATCH:
                self.counters["unit_mismatches"] += 1
            elif problem.code == OUT_OF_PHYSICAL_RANGE:
                self.counters["range_violations"] += 1

    def _should_historise(
        self,
        meta: PointMeta,
        columns: dict[str, Any],
        previous: tuple | None,
        publish_value: bool,
    ) -> bool:
        policy = (meta.historian_policy or "standard").lower()
        if policy == "none" or self.settings.historian_backend == "none":
            return False
        if policy == "event_on_change" and previous is not None and publish_value:
            return previous != _column_snapshot(columns)
        return True

    def _write_sample(
        self,
        session: Session,
        meta: PointMeta,
        *,
        ts: dt.datetime,
        columns: dict[str, Any],
        quality: str,
        source: str | None,
        now: dt.datetime,
    ) -> None:
        value_text = columns.get("value_text")
        if "value_json" in columns:
            # The relational historian has no JSON column; keep the document
            # readable rather than dropping it.
            value_text = json.dumps(columns["value_json"], sort_keys=True)
        sample = TelemetrySample(
            point_id=meta.point_id,
            asset_id=meta.asset_id,
            point_name=meta.point_name,
            ts=ts,
            value_numeric=columns.get("value_numeric"),
            value_text=value_text,
            value_bool=columns.get("value_bool"),
            unit=meta.unit,
            quality=quality,
            source=source,
        )
        session.add(sample)
        self._touch_sample_index(session, meta, ts=ts, now=now)

    def _touch_sample_index(
        self, session: Session, meta: PointMeta, *, ts: dt.datetime, now: dt.datetime
    ) -> None:
        index = session.get(PointSampleIndex, meta.point_id)
        if index is None:
            index = PointSampleIndex(
                point_id=meta.point_id,
                historian_backend=self.settings.historian_backend,
                series_key=meta.point_id,
                measurement=meta.point_name,
                retention_policy=f"raw_{self.settings.historian_raw_retention_days}d",
                downsample_policy={"interval": "1min", "keep": "2y"},
                first_sample_at=ts,
                last_sample_at=ts,
                sample_count=0,
            )
            session.add(index)
        first = as_utc(index.first_sample_at)
        last = as_utc(index.last_sample_at)
        if first is None or ts < first:
            index.first_sample_at = ts
        if last is None or ts > last:
            index.last_sample_at = ts
        index.sample_count = (index.sample_count or 0) + 1
        index.updated_at = now


def _assign_value(state: CurrentState, columns: dict[str, Any]) -> None:
    """Write one value column and clear the others."""
    state.value_numeric = columns.get("value_numeric")
    state.value_text = columns.get("value_text")
    state.value_bool = columns.get("value_bool")
    state.value_json = columns.get("value_json")


def _value_snapshot(state: CurrentState) -> tuple:
    return (state.value_numeric, state.value_text, state.value_bool, _hashable(state.value_json))


def _column_snapshot(columns: dict[str, Any]) -> tuple:
    return (
        columns.get("value_numeric"),
        columns.get("value_text"),
        columns.get("value_bool"),
        _hashable(columns.get("value_json")),
    )


def _hashable(value: Any) -> Any:
    """JSON documents compare by canonical text so ``event_on_change`` works."""
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
