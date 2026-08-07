"""Alarm evaluation and lifecycle (SDD 14.2, 14.3, 26.6).

The evaluator is deliberately boring and deliberately explicit:

* **Delays are states, not timers.** A threshold crossing creates the alarm in
  the ``detected`` state immediately, and the alarm only reaches ``active`` once
  the condition has held for ``on_delay_s``. A crossing that goes away first is
  closed out as a transient and never notified -- but it is still a row, because
  "the sensor twitched fourteen times last night" is operational information.

* **Reset is a separate condition, not the negation of the trigger.** Every
  definition carries ``reset_operator``/``reset_value``; where it does not, the
  reset is derived from the trigger and ``hysteresis``. Either way there is a
  real deadband, so a value hovering on the trip point cannot chatter.

* **Bad data is not good news.** SDD 26.6: a dead sensor must not look like a
  healthy in-range reading. A process alarm whose input quality is ``bad`` or
  ``stale`` is not evaluated at all; the definition's data-quality alarm is
  raised against that specific point instead.

* **Maintenance mode modifies alarms, it does not delete them** (SDD 11). A
  suppressed alarm is stored with ``suppressed=True`` and a machine-readable
  reason so the operator can see what was hidden and why.

* **The platform is not the safety system** (SDD 14.1). Nothing here executes a
  protective action. ``automatic_action`` on a definition is a description of
  what the equipment does on its own.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin import topics
from homestead_twin.alarms.definitions import definition_meta, load_definitions
from homestead_twin.config import Settings, get_settings
from homestead_twin.envelope import EventEnvelope
from homestead_twin.models.alarms import SEVERITIES, Alarm, AlarmDefinition, AlarmEvent
from homestead_twin.models.base import utcnow
from homestead_twin.models.commands import OperatingMode
from homestead_twin.models.registry import Asset
from homestead_twin.models.telemetry import CurrentState
from homestead_twin.mqtt import MessageBus

logger = logging.getLogger(__name__)

#: Lifecycle states in which an alarm is still an open item of work.
OPEN_STATES = ("detected", "active", "acknowledged", "mitigated")
#: States shown on the "active alarms" view (SDD section 41).
ACTIVE_STATES = ("active", "acknowledged", "mitigated")
#: States from which an alarm no longer participates in correlation.
CLOSED_STATES = ("cleared", "reviewed")

#: SDD 26.6 quality codes that make a measurement unusable for a decision.
QUALITY_BLOCKING = frozenset({"bad", "stale"})

_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
_DOWNGRADE = {"emergency": "critical", "critical": "major", "major": "warning", "warning": "info"}

#: State ordering used to reject illegal lifecycle transitions.
_LIFECYCLE_ORDER = {
    "detected": 0,
    "active": 1,
    "acknowledged": 2,
    "mitigated": 3,
    "cleared": 4,
    "reviewed": 5,
}


class SuppressionReason:
    """Machine-readable prefixes for ``Alarm.suppression_reason``.

    ``suppressed`` means "do not notify this alarm on its own". It never means
    "this alarm did not happen".
    """

    MAINTENANCE = "maintenance"
    CONDITION = "condition"
    SYMPTOM = "symptom"
    FLOOD = "flood"

    @staticmethod
    def format(kind: str, detail: str) -> str:
        return f"{kind}: {detail}"

    @staticmethod
    def kind(reason: str | None) -> str | None:
        if not reason:
            return None
        return reason.split(":", 1)[0].strip() or None


class AlarmTransitionError(ValueError):
    """Raised when a lifecycle transition is not legal (SDD 14.2)."""


@dataclass
class EvaluationResult:
    """What one evaluation pass did."""

    evaluated: int = 0
    skipped_no_data: int = 0
    detected: list[Alarm] = field(default_factory=list)
    activated: list[Alarm] = field(default_factory=list)
    cleared: list[Alarm] = field(default_factory=list)
    transient: list[Alarm] = field(default_factory=list)
    suppressed: list[Alarm] = field(default_factory=list)
    quality_blocked: list[str] = field(default_factory=list)
    held_manual_reset: list[Alarm] = field(default_factory=list)

    @property
    def changed(self) -> list[Alarm]:
        seen: dict[str, Alarm] = {}
        for alarm in (*self.detected, *self.activated, *self.cleared, *self.transient):
            seen[id(alarm)] = alarm
        return list(seen.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "skipped_no_data": self.skipped_no_data,
            "detected": len(self.detected),
            "activated": len(self.activated),
            "cleared": len(self.cleared),
            "transient": len(self.transient),
            "suppressed": len(self.suppressed),
            "quality_blocked": len(self.quality_blocked),
            "held_manual_reset": len(self.held_manual_reset),
        }


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_pair(left: Any, right: Any) -> tuple[Any, Any]:
    """Make an enum/boolean/number comparison work regardless of transport typing."""
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left), bool(right)
    left_num, right_num = _as_number(left), _as_number(right)
    if left_num is not None and right_num is not None:
        return left_num, right_num
    return str(left), str(right)


def compare(operator: str, value: Any, payload: dict[str, Any] | None) -> bool:
    """Evaluate one comparison. Unknown operators never fire."""
    payload = payload or {}
    if operator in ("in", "not_in", "quality_in"):
        candidates = payload.get("values") or []
        members = {str(c).lower() for c in candidates}
        present = str(value).lower() in members
        return present if operator in ("in", "quality_in") else not present

    if "value" not in payload:
        return False
    left, right = _coerce_pair(value, payload["value"])

    if operator == "eq":
        return left == right
    if operator == "ne":
        return left != right
    if not isinstance(left, float) or not isinstance(right, float):
        return False
    if operator == "lt":
        return left < right
    if operator == "le":
        return left <= right
    if operator == "gt":
        return left > right
    if operator == "ge":
        return left >= right
    return False


_INVERSE_OPERATOR = {
    "lt": "ge",
    "le": "gt",
    "gt": "le",
    "ge": "lt",
    "eq": "ne",
    "ne": "eq",
    "in": "not_in",
    "not_in": "in",
    "quality_in": "not_in",
}


def derive_reset(definition: AlarmDefinition) -> tuple[str | None, dict[str, Any] | None]:
    """Return the reset condition, deriving it from hysteresis when not declared.

    An explicit ``reset_operator``/``reset_value`` always wins. Otherwise the
    reset is the inverse of the trigger, offset by ``hysteresis`` for the
    numeric operators, so a deadband exists whichever way the definition was
    written.
    """
    if definition.reset_operator:
        return definition.reset_operator, definition.reset_value

    operator = definition.trigger_operator
    if not operator:
        return None, None
    inverse = _INVERSE_OPERATOR.get(operator)
    if inverse is None:
        return None, None

    payload = dict(definition.trigger_value or {})
    payload.pop("guards", None)
    hysteresis = definition.hysteresis
    if hysteresis and operator in ("lt", "le", "gt", "ge") and "value" in payload:
        base = _as_number(payload["value"])
        if base is not None:
            # Trip low -> reset higher; trip high -> reset lower.
            payload["value"] = base + hysteresis if operator in ("lt", "le") else base - hysteresis
    return inverse, payload


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


@dataclass
class _Reading:
    point_id: str
    asset_id: str
    value: Any
    quality: str
    unit: str | None
    ts: dt.datetime | None


class AlarmEvaluator:
    """Evaluates alarm definitions against the current-state cache."""

    def __init__(
        self,
        session: Session,
        bus: MessageBus | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.bus = bus
        self.settings = settings or get_settings()
        self._state_cache: dict[str, CurrentState] | None = None
        self._asset_cache: dict[str, Asset] | None = None
        self._mode_cache: dict[tuple[str, str], str] | None = None
        self._definitions: dict[str, AlarmDefinition] | None = None
        self._unknown_suppression_types: set[str] = set()

    # -- caches ----------------------------------------------------------

    def _invalidate(self) -> None:
        self._state_cache = None
        self._asset_cache = None
        self._mode_cache = None
        self._definitions = None

    @property
    def states(self) -> dict[str, CurrentState]:
        if self._state_cache is None:
            self._state_cache = {
                row.point_id: row for row in self.session.scalars(select(CurrentState)).all()
            }
        return self._state_cache

    @property
    def assets(self) -> dict[str, Asset]:
        if self._asset_cache is None:
            self._asset_cache = {a.asset_id: a for a in self.session.scalars(select(Asset)).all()}
        return self._asset_cache

    @property
    def modes(self) -> dict[tuple[str, str], str]:
        if self._mode_cache is None:
            self._mode_cache = {
                (m.scope_type, m.scope_id): m.mode for m in self.session.scalars(select(OperatingMode)).all()
            }
        return self._mode_cache

    @property
    def definitions(self) -> dict[str, AlarmDefinition]:
        if self._definitions is None:
            self._definitions = {d.alarm_key: d for d in load_definitions(self.session, enabled_only=False)}
        return self._definitions

    # -- scope resolution ------------------------------------------------

    def resolve_targets(self, definition: AlarmDefinition) -> list[str]:
        """Assets this definition is evaluated against.

        ``asset_id`` scopes to one asset. ``asset_class`` scopes to every asset
        of that class in the definition's domain, so one ``inverter_fault``
        definition covers all four inverter functional positions. A definition
        with neither is instantiated dynamically (the data-quality alarms).
        """
        if definition.asset_id:
            return [definition.asset_id]
        if definition.asset_class:
            return sorted(
                asset_id
                for asset_id, asset in self.assets.items()
                if asset.asset_class == definition.asset_class
                and (definition.domain is None or asset.domain == definition.domain)
            )
        return []

    def _reading(self, asset_id: str, point_name: str) -> _Reading | None:
        state = self.states.get(topics.point_id(asset_id, point_name))
        if state is None:
            return None
        return _Reading(
            point_id=state.point_id,
            asset_id=state.asset_id,
            value=state.value,
            quality=(state.quality or "good").lower(),
            unit=state.unit,
            ts=state.ts,
        )

    # -- operating mode --------------------------------------------------

    def operating_mode(self, asset_id: str | None, domain: str | None) -> tuple[str, str] | None:
        """Return (mode, scope description) for the most specific scope that has one."""
        if asset_id:
            mode = self.modes.get(("asset", asset_id))
            if mode:
                return mode, f"asset {asset_id}"
            asset = self.assets.get(asset_id)
            if asset is not None:
                domain = domain or asset.domain
        if domain:
            mode = self.modes.get(("domain", domain))
            if mode:
                return mode, f"domain {domain}"
        mode = self.modes.get(("site", self.settings.site_id))
        if mode:
            return mode, f"site {self.settings.site_id}"
        return None

    # -- suppression -----------------------------------------------------

    def _evaluate_suppression_conditions(
        self, definition: AlarmDefinition, asset_id: str | None
    ) -> str | None:
        """Return a suppression reason from the definition's declared conditions."""
        for condition in definition.suppression_conditions or []:
            kind = condition.get("type")
            if kind == "operating_mode":
                scope = condition.get("scope", "asset")
                modes = {str(m).lower() for m in condition.get("modes") or []}
                current = None
                if scope == "asset" and asset_id:
                    current = self.modes.get(("asset", asset_id))
                elif scope == "domain" and definition.domain:
                    current = self.modes.get(("domain", definition.domain))
                elif scope == "site":
                    current = self.modes.get(("site", self.settings.site_id))
                if current and current.lower() in modes:
                    return SuppressionReason.format(
                        SuppressionReason.CONDITION,
                        f"{scope} operating mode '{current}' is a declared suppression condition",
                    )
            elif kind == "point_state":
                reading = self._reading(condition["asset_id"], condition["point_name"])
                if reading is None or reading.quality in QUALITY_BLOCKING:
                    continue
                payload = {k: condition[k] for k in ("value", "values") if k in condition}
                if compare(condition.get("operator", "eq"), reading.value, payload):
                    return SuppressionReason.format(
                        SuppressionReason.CONDITION,
                        f"{reading.point_id} satisfies a declared suppression condition",
                    )
            elif kind in ("manual", "parent_alarm_active"):
                # 'manual' is documentation for the operator; 'parent_alarm_active'
                # is handled by the correlation engine, which owns incident membership.
                continue
            elif kind and kind not in self._unknown_suppression_types:
                self._unknown_suppression_types.add(kind)
                logger.warning(
                    "Alarm %s declares unknown suppression condition type %r; ignoring it",
                    definition.alarm_key,
                    kind,
                )
        return None

    def maintenance_decision(
        self, definition: AlarmDefinition, asset_id: str | None
    ) -> tuple[str, str | None, str | None]:
        """Return (severity, suppression_reason, note) after maintenance handling.

        SDD 11: maintenance modifies alarms and inhibits automatic starts; it
        does not make the platform blind. Behaviour comes from the definition's
        ``maintenance_mode_behaviour``.
        """
        severity = definition.severity
        current = self.operating_mode(asset_id, definition.domain)
        if current is None or current[0].lower() != "maintenance":
            return severity, None, None

        mode, scope = current
        behaviour = (definition.maintenance_mode_behaviour or "suppress").lower()
        if behaviour == "suppress":
            return (
                severity,
                SuppressionReason.format(SuppressionReason.MAINTENANCE, f"{scope} is in {mode} mode"),
                f"Notification withheld: {scope} is in {mode} mode (behaviour=suppress).",
            )
        if behaviour == "downgrade":
            downgraded = _DOWNGRADE.get(severity, severity)
            return (
                downgraded,
                None,
                f"Severity downgraded {severity} -> {downgraded}: {scope} is in {mode} mode.",
            )
        if behaviour == "notify_only":
            return severity, None, f"{scope} is in {mode} mode; alarm notifies normally."
        return severity, None, None

    # -- alarm lookup ----------------------------------------------------

    def _open_alarm(self, alarm_key: str, asset_id: str | None, point_id: str | None) -> Alarm | None:
        statement = (
            select(Alarm)
            .where(Alarm.alarm_key == alarm_key, Alarm.state.in_(OPEN_STATES))
            .order_by(Alarm.detected_at.desc())
        )
        for alarm in self.session.scalars(statement).all():
            if alarm.asset_id == asset_id and (point_id is None or alarm.point_id == point_id):
                return alarm
        return None

    # -- lifecycle -------------------------------------------------------

    def _append_event(
        self,
        alarm: Alarm,
        from_state: str | None,
        to_state: str,
        occurred_at: dt.datetime,
        actor: str | None = None,
        note: str | None = None,
    ) -> AlarmEvent:
        # Assigning ``alarm`` back-populates ``Alarm.events`` exactly once.
        # Appending to the collection *and* adding the row separately would leave
        # a duplicate in the in-memory list, which makes the audit trail lie.
        event = AlarmEvent(
            alarm=alarm,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            note=note,
            occurred_at=occurred_at,
        )
        self.session.add(event)
        return event

    def _publish(self, alarm: Alarm, definition: AlarmDefinition | None = None) -> None:
        """Publish the alarm's current state (SDD 10.1 alarm topic namespace)."""
        if self.bus is None or not alarm.asset_id:
            return
        try:
            topic = topics.alarm_topic(alarm.asset_id, alarm.alarm_key, self.settings.mqtt_base_topic)
            envelope = EventEnvelope(
                ts=alarm.updated_at or utcnow(),
                asset_id=alarm.asset_id,
                event=alarm.alarm_key,
                source="alarm-engine",
                detail={
                    "alarm_id": alarm.id,
                    "state": alarm.state,
                    "severity": alarm.severity,
                    "point_id": alarm.point_id,
                    "message": alarm.message,
                    "suppressed": alarm.suppressed,
                    "suppression_reason": alarm.suppression_reason,
                    "incident_id": alarm.incident_id,
                    "requires_manual_reset": bool(definition.requires_manual_reset if definition else False),
                },
            )
            self.bus.publish(topic, envelope.to_payload(), qos=1, retain=False)
        except Exception:  # pragma: no cover - a dead bus must not stop evaluation
            logger.exception("Failed to publish alarm state for %s", alarm.alarm_key)

    def transition(
        self,
        alarm: Alarm,
        to_state: str,
        now: dt.datetime,
        *,
        actor: str | None = None,
        note: str | None = None,
        definition: AlarmDefinition | None = None,
    ) -> Alarm:
        """Move an alarm to a new lifecycle state and append the audit event."""
        from_state = alarm.state
        if to_state == from_state:
            return alarm
        alarm.state = to_state
        alarm.updated_at = now
        if to_state == "active":
            alarm.activated_at = alarm.activated_at or now
        elif to_state == "acknowledged":
            alarm.acknowledged_at = now
            alarm.acknowledged_by = actor
        elif to_state == "mitigated":
            alarm.mitigated_at = now
        elif to_state == "cleared":
            alarm.cleared_at = now
        elif to_state == "reviewed":
            alarm.reviewed_at = now
            alarm.reviewed_by = actor
        self._append_event(alarm, from_state, to_state, now, actor=actor, note=note)
        self._publish(alarm, definition or self.definitions.get(alarm.alarm_key))
        return alarm

    # -- operator actions -------------------------------------------------

    def _require_alarm(self, alarm: Alarm | str) -> Alarm:
        if isinstance(alarm, Alarm):
            return alarm
        found = self.session.get(Alarm, alarm)
        if found is None:
            raise AlarmTransitionError(f"Unknown alarm: {alarm}")
        return found

    def _guard_transition(self, alarm: Alarm, to_state: str) -> None:
        if alarm.state == to_state:
            raise AlarmTransitionError(f"Alarm is already {to_state}")
        if _LIFECYCLE_ORDER[to_state] < _LIFECYCLE_ORDER[alarm.state]:
            raise AlarmTransitionError(
                f"Illegal lifecycle transition {alarm.state} -> {to_state} (SDD 14.2 is one-way)"
            )

    def acknowledge(self, alarm: Alarm | str, actor: str, note: str, now: dt.datetime | None = None) -> Alarm:
        """Operator has seen the alarm. Stops escalation; changes nothing physical."""
        alarm = self._require_alarm(alarm)
        self._guard_transition(alarm, "acknowledged")
        if not (note or "").strip():
            raise AlarmTransitionError("Acknowledgement requires a note (SDD 5.7)")
        return self.transition(alarm, "acknowledged", now or utcnow(), actor=actor, note=note)

    def mitigate(self, alarm: Alarm | str, actor: str, note: str, now: dt.datetime | None = None) -> Alarm:
        """Operator has taken action. The condition may still be present."""
        alarm = self._require_alarm(alarm)
        self._guard_transition(alarm, "mitigated")
        if not (note or "").strip():
            raise AlarmTransitionError("Mitigation requires a note describing the action taken")
        return self.transition(alarm, "mitigated", now or utcnow(), actor=actor, note=note)

    def clear(
        self,
        alarm: Alarm | str,
        actor: str,
        note: str,
        now: dt.datetime | None = None,
        *,
        force: bool = False,
    ) -> Alarm:
        """Manual clear. Required for ``requires_manual_reset`` definitions.

        A manual clear while the trigger condition is still true is refused
        unless ``force`` is set, and the override is recorded in the event note.
        """
        alarm = self._require_alarm(alarm)
        self._guard_transition(alarm, "cleared")
        if not (note or "").strip():
            raise AlarmTransitionError("Clearing requires a reason")
        definition = self.definitions.get(alarm.alarm_key)
        still_true = definition is not None and self._trigger_still_true(definition, alarm)
        if still_true and not force:
            raise AlarmTransitionError(
                "Trigger condition is still true; clear with force=true and a reason to override"
            )
        if still_true:
            note = f"{note} [operator override: trigger condition still true]"
        return self.transition(
            alarm, "cleared", now or utcnow(), actor=actor, note=note, definition=definition
        )

    def review(self, alarm: Alarm | str, actor: str, note: str, now: dt.datetime | None = None) -> Alarm:
        """Post-event review closes the SDD 14.2 lifecycle."""
        alarm = self._require_alarm(alarm)
        if alarm.state != "cleared":
            raise AlarmTransitionError("Only a cleared alarm can be reviewed")
        if not (note or "").strip():
            raise AlarmTransitionError("Review requires a note")
        return self.transition(alarm, "reviewed", now or utcnow(), actor=actor, note=note)

    def _trigger_still_true(self, definition: AlarmDefinition, alarm: Alarm) -> bool:
        if not definition.point_name or not alarm.asset_id:
            return False
        reading = self._reading(alarm.asset_id, definition.point_name)
        if reading is None or reading.quality in QUALITY_BLOCKING:
            return False
        return self._trigger_holds(definition, reading, alarm.asset_id)

    # -- trigger evaluation ------------------------------------------------

    def _guards_pass(self, definition: AlarmDefinition, asset_id: str) -> bool:
        """Guards let a definition require live load or usable irradiance.

        A guard whose own measurement is missing or untrustworthy fails closed:
        SDD 26.6 forbids defaulting a permissive on missing data.
        """
        for guard in (definition.trigger_value or {}).get("guards") or []:
            reading = self._reading(guard.get("asset_id", asset_id), guard["point_name"])
            if reading is None or reading.quality in QUALITY_BLOCKING:
                return False
            payload = {k: guard[k] for k in ("value", "values") if k in guard}
            if not compare(guard.get("operator", "eq"), reading.value, payload):
                return False
        return True

    def _trigger_holds(self, definition: AlarmDefinition, reading: _Reading, asset_id: str) -> bool:
        if not definition.trigger_operator:
            return False
        if not self._guards_pass(definition, asset_id):
            return False
        return compare(definition.trigger_operator, reading.value, definition.trigger_value)

    def _reset_holds(self, definition: AlarmDefinition, reading: _Reading) -> bool:
        operator, payload = derive_reset(definition)
        if not operator:
            return False
        return compare(operator, reading.value, payload)

    # -- main pass ---------------------------------------------------------

    def evaluate(self, now: dt.datetime | None = None) -> EvaluationResult:
        """Evaluate every enabled definition once."""
        now = now or utcnow()
        self._invalidate()
        result = EvaluationResult()

        definitions = [d for d in self.definitions.values() if d.enabled]
        quality_driven = {d.alarm_key for d in definitions if d.trigger_operator == "quality_in"}

        for definition in definitions:
            if definition.alarm_key in quality_driven:
                # Raised by the process alarms below, then reconciled at the end.
                continue
            if not definition.point_name:
                continue
            for asset_id in self.resolve_targets(definition):
                result.evaluated += 1
                self._evaluate_one(definition, asset_id, now, result)

        self._reconcile_quality_alarms(quality_driven, now, result)
        return result

    def _evaluate_one(
        self,
        definition: AlarmDefinition,
        asset_id: str,
        now: dt.datetime,
        result: EvaluationResult,
    ) -> None:
        point_name = definition.point_name
        assert point_name is not None
        reading = self._reading(asset_id, point_name)
        existing = self._open_alarm(definition.alarm_key, asset_id, topics.point_id(asset_id, point_name))

        if reading is None:
            # No telemetry for this point yet -- an uncommissioned binding, not a
            # healthy reading. Never invent a value.
            result.skipped_no_data += 1
            return

        if reading.quality in QUALITY_BLOCKING:
            # SDD 26.6: do not treat a dead sensor as an in-range reading.
            result.quality_blocked.append(reading.point_id)
            self._raise_quality_alarm(definition, reading, now, result)
            if existing is not None and existing.state == "detected":
                # A pending candidate cannot be confirmed on untrustworthy data.
                self._close_transient(
                    existing,
                    now,
                    result,
                    note=(
                        f"Closed without activating: input quality became '{reading.quality}'; "
                        f"the data-quality alarm carries this instead."
                    ),
                )
            return

        self._clear_quality_alarm(definition, reading, now, result)

        triggered = self._trigger_holds(definition, reading, asset_id)
        if triggered:
            self._handle_trigger(definition, asset_id, reading, existing, now, result)
        elif existing is None:
            return
        elif existing.state == "detected":
            # The candidate never activated, so the on-delay timer simply drops.
            # The off-delay and the deadband exist to stop an *active* alarm
            # clearing too eagerly; they must not keep a transient pending.
            self._close_transient(
                existing,
                now,
                result,
                note=(
                    f"Condition cleared after "
                    f"{elapsed_s(now, existing.detected_at):.0f} s, before the "
                    f"{definition.on_delay_s} s on-delay expired. Recorded as a transient: "
                    "never activated, never notified."
                ),
            )
        else:
            self._handle_reset(definition, reading, existing, now, result)

    # -- trigger side ------------------------------------------------------

    def _handle_trigger(
        self,
        definition: AlarmDefinition,
        asset_id: str,
        reading: _Reading,
        existing: Alarm | None,
        now: dt.datetime,
        result: EvaluationResult,
    ) -> None:
        if existing is None:
            alarm = self._create_alarm(definition, asset_id, reading, now)
            result.detected.append(alarm)
            if alarm.suppressed:
                result.suppressed.append(alarm)
            existing = alarm
        else:
            self._record_reading(existing, reading, now, reset_pending=False)

        if existing.state != "detected":
            return

        held_for = elapsed_s(now, existing.detected_at)
        if held_for + 1e-9 >= (definition.on_delay_s or 0):
            note = (
                f"Trigger held {held_for:.0f} s (on_delay {definition.on_delay_s} s): "
                f"{definition.trigger_expression or definition.alarm_key}"
            )
            self.transition(existing, "active", now, note=note, definition=definition)
            result.activated.append(existing)

    def _create_alarm(
        self,
        definition: AlarmDefinition,
        asset_id: str,
        reading: _Reading,
        now: dt.datetime,
    ) -> Alarm:
        severity, suppression, maintenance_note = self.maintenance_decision(definition, asset_id)
        condition_suppression = self._evaluate_suppression_conditions(definition, asset_id)
        suppression = suppression or condition_suppression

        alarm = Alarm(
            alarm_key=definition.alarm_key,
            asset_id=asset_id,
            point_id=reading.point_id,
            severity=severity,
            state="detected",
            detected_at=now,
            trigger_value={
                "value": reading.value,
                "unit": reading.unit,
                "quality": reading.quality,
                "observed_at": reading.ts.isoformat() if reading.ts else None,
                "first_value": reading.value,
            },
            message=self._message(definition, asset_id, reading),
            suppressed=bool(suppression),
            suppression_reason=suppression,
            notified=False,
        )
        alarm.created_at = now
        alarm.updated_at = now
        self.session.add(alarm)
        self.session.flush()

        note = f"Condition detected on {reading.point_id} (value={reading.value!r})."
        if maintenance_note:
            note = f"{note} {maintenance_note}"
        if condition_suppression:
            note = f"{note} {condition_suppression}."
        self._append_event(alarm, None, "detected", now, actor="alarm-engine", note=note)
        self._publish(alarm, definition)
        return alarm

    def _message(self, definition: AlarmDefinition, asset_id: str, reading: _Reading) -> str:
        unit = f" {reading.unit}" if reading.unit else ""
        return (
            f"{definition.name} on {asset_id}: "
            f"{definition.point_name}={reading.value}{unit} "
            f"({definition.trigger_expression or definition.alarm_key})"
        )

    # -- reset side --------------------------------------------------------

    def _handle_reset(
        self,
        definition: AlarmDefinition,
        reading: _Reading,
        alarm: Alarm,
        now: dt.datetime,
        result: EvaluationResult,
    ) -> None:
        if not self._reset_holds(definition, reading):
            # Between the trigger and the reset condition: the deadband. Hold.
            self._record_reading(alarm, reading, now, reset_pending=False)
            return

        pending_since = self._record_reading(alarm, reading, now, reset_pending=True)
        held_for = elapsed_s(now, pending_since)
        if held_for + 1e-9 < (definition.off_delay_s or 0):
            return

        if definition.requires_manual_reset:
            result.held_manual_reset.append(alarm)
            return

        note = (
            f"Reset condition held {held_for:.0f} s (off_delay {definition.off_delay_s} s); "
            f"{definition.point_name}={reading.value!r}."
        )
        self.transition(alarm, "cleared", now, actor="alarm-engine", note=note, definition=definition)
        result.cleared.append(alarm)

    def _close_transient(
        self, alarm: Alarm, now: dt.datetime, result: EvaluationResult, *, note: str
    ) -> None:
        self.transition(alarm, "cleared", now, actor="alarm-engine", note=note)
        result.transient.append(alarm)

    def _record_reading(
        self, alarm: Alarm, reading: _Reading, now: dt.datetime, *, reset_pending: bool
    ) -> dt.datetime:
        """Store the latest observation and the off-delay timer on the alarm row.

        The alarm's own ``trigger_value`` JSON carries the evaluation scratch, so
        delays and hysteresis survive a service restart without a side table.
        """
        payload = dict(alarm.trigger_value or {})
        payload.update(
            {
                "value": reading.value,
                "unit": reading.unit,
                "quality": reading.quality,
                "observed_at": reading.ts.isoformat() if reading.ts else None,
            }
        )
        if reset_pending:
            raw = payload.get("reset_pending_since")
            pending_since = _parse_ts(raw) or now
            payload["reset_pending_since"] = pending_since.isoformat()
        else:
            payload.pop("reset_pending_since", None)
            pending_since = now
        alarm.trigger_value = payload
        alarm.updated_at = now
        return pending_since

    # -- data-quality alarms -----------------------------------------------

    def _quality_definition(self, definition: AlarmDefinition) -> AlarmDefinition | None:
        key = definition_meta(definition).get("data_quality_alarm_key")
        if not key:
            return None
        quality = self.definitions.get(key)
        if quality is None or not quality.enabled:
            return None
        return quality

    def _raise_quality_alarm(
        self,
        definition: AlarmDefinition,
        reading: _Reading,
        now: dt.datetime,
        result: EvaluationResult,
    ) -> None:
        quality = self._quality_definition(definition)
        if quality is None:
            return
        existing = self._open_alarm(quality.alarm_key, reading.asset_id, reading.point_id)
        if existing is None:
            severity, suppression, maintenance_note = self.maintenance_decision(quality, reading.asset_id)
            alarm = Alarm(
                alarm_key=quality.alarm_key,
                asset_id=reading.asset_id,
                point_id=reading.point_id,
                severity=severity,
                state="detected",
                detected_at=now,
                trigger_value={"quality": reading.quality, "value": reading.value},
                message=(
                    f"{quality.name}: {reading.point_id} quality is '{reading.quality}'. "
                    f"'{definition.alarm_key}' is held rather than evaluated on it (SDD 26.6)."
                ),
                suppressed=bool(suppression),
                suppression_reason=suppression,
            )
            alarm.created_at = now
            alarm.updated_at = now
            self.session.add(alarm)
            self.session.flush()
            note = (
                f"Raised because {definition.alarm_key} would otherwise have used "
                f"{reading.point_id} at quality '{reading.quality}'."
            )
            if maintenance_note:
                note = f"{note} {maintenance_note}"
            self._append_event(alarm, None, "detected", now, actor="alarm-engine", note=note)
            self._publish(alarm, quality)
            result.detected.append(alarm)
            if alarm.suppressed:
                result.suppressed.append(alarm)
            existing = alarm
        else:
            payload = dict(existing.trigger_value or {})
            payload["quality"] = reading.quality
            payload.pop("reset_pending_since", None)
            existing.trigger_value = payload
            existing.updated_at = now

        if existing.state == "detected":
            held_for = elapsed_s(now, existing.detected_at)
            if held_for + 1e-9 >= (quality.on_delay_s or 0):
                self.transition(
                    existing,
                    "active",
                    now,
                    note=f"Quality '{reading.quality}' held {held_for:.0f} s.",
                    definition=quality,
                )
                result.activated.append(existing)

    def _clear_quality_alarm(
        self,
        definition: AlarmDefinition,
        reading: _Reading,
        now: dt.datetime,
        result: EvaluationResult,
    ) -> None:
        quality = self._quality_definition(definition)
        if quality is None:
            return
        alarm = self._open_alarm(quality.alarm_key, reading.asset_id, reading.point_id)
        if alarm is None:
            return
        note = f"{reading.point_id} quality returned to '{reading.quality}'."
        if alarm.state == "detected":
            self._close_transient(alarm, now, result, note=note)
            return
        payload = dict(alarm.trigger_value or {})
        pending_since = _parse_ts(payload.get("reset_pending_since")) or now
        payload["reset_pending_since"] = pending_since.isoformat()
        payload["quality"] = reading.quality
        alarm.trigger_value = payload
        alarm.updated_at = now
        if elapsed_s(now, pending_since) + 1e-9 < (quality.off_delay_s or 0):
            return
        if not quality.requires_manual_reset:
            self.transition(alarm, "cleared", now, actor="alarm-engine", note=note, definition=quality)
            result.cleared.append(alarm)

    def _reconcile_quality_alarms(
        self, quality_keys: set[str], now: dt.datetime, result: EvaluationResult
    ) -> None:
        """Close data-quality alarms whose point has disappeared from current state."""
        if not quality_keys:
            return
        statement = select(Alarm).where(Alarm.alarm_key.in_(quality_keys), Alarm.state.in_(OPEN_STATES))
        for alarm in self.session.scalars(statement).all():
            if alarm.point_id and alarm.point_id in self.states:
                continue
            self.transition(
                alarm,
                "cleared",
                now,
                actor="alarm-engine",
                note="Point no longer present in the current-state cache.",
            )
            result.cleared.append(alarm)


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Return an aware UTC datetime.

    SQLite has no timezone type, so a row round-tripped through the database --
    or simply reloaded after the ORM's weak identity map dropped it -- comes back
    naive. Every timestamp the platform writes is UTC (SDD 16.3), so a naive
    value read back is UTC. Comparing without this is how "on-delay" turns into
    a ``TypeError`` at three in the morning.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def elapsed_s(now: dt.datetime, since: dt.datetime | None) -> float:
    """Seconds between two timestamps, tolerant of naive values from the database."""
    since = as_utc(since)
    if since is None:
        return 0.0
    return (as_utc(now) - since).total_seconds()


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return as_utc(value)
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return as_utc(parsed)


def severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(severity, 0)
