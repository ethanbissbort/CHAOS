"""Load shedding (SDD section 32) and load restoration (SDD section 33).

The EMS requests; the local controller enforces equipment interlocks (SDD
30.4). Nothing in this module opens a breaker. It asks a load's controller to
stop, to reduce, to enter its survival profile or to shut down gracefully, and
then it looks at the measured power to find out what actually happened.

Invariants
----------

* **Tier 0 is never shed.** The check lives here as well as in the load
  schedule, so a data error in the schedule cannot shed physical protection
  (SDD 31.2).
* **Low reserve is verified before anything is shed** (SDD 32.1 rule 1). If SOC,
  the BMS limits, the site load or the source state cannot be trusted, the EMS
  does not shed on the strength of a possibly bad number.
* **Groups, not everything at once** (rule 4), one group per step with a
  confirmation window between groups (rule 5).
* **A failed shed escalates, it is never assumed done** (SDD 32.3). A rejected
  command, a blocked command, or an accepted command with no measured
  reduction all mark the expected reduction unavailable, raise
  ``load_shed_failed`` and let the sequence advance to the next permitted
  group. The load stays in the reserve calculation.
* **No rapid on/off.** A load is not re-commanded inside
  ``shed_reissue_interval_s`` and is locked out for operator inspection after
  ``shed_attempt_limit`` attempts.
* **Restoration is staged and staggered** (SDD 33, FR-103): sustained
  qualification, group order R1..R5, each asset's minimum off time, each
  asset's restart delay, and an inrush-class stagger so discretionary loads
  cannot restart simultaneously after an outage.
* **Attended equipment is never restarted automatically** (SDD 33.2 rules 6, 7).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin.config import Settings
from homestead_twin.ems import CommandOutcome, CommandPort, CommandRequest
from homestead_twin.ems.config import EmsConfig, load_budget_topic
from homestead_twin.ems.derived import DerivedEnergyState
from homestead_twin.ems.inputs import EmsInputs, load_input_key
from homestead_twin.ems.loader import effective_tier, load_profiles
from homestead_twin.envelope import EventEnvelope, TelemetryEnvelope
from homestead_twin.models.energy import LoadShedAction, PowerLoadProfile
from homestead_twin.mqtt import MessageBus
from homestead_twin.topics import DEFAULT_BASE, alarm_topic

logger = logging.getLogger(__name__)

#: Outcomes of a shed or restore action.
#:
#: ``requested``   command issued, measured confirmation still pending
#: ``confirmed``   measured power confirms the load actually changed
#: ``unconfirmed`` no measurement available, so the reduction is unavailable
#: ``no_reduction`` command accepted but the load is still drawing power
#: ``rejected``    the command port refused or the controller rejected it
#: ``blocked``     an interlock refused dispatch (for example physical control off)
#: ``locked_out``  attempts exhausted; operator inspection required
#: ``applied``     supervisory-only action (a lease denial), no command needed
SHED_OUTCOMES = (
    "requested",
    "confirmed",
    "unconfirmed",
    "no_reduction",
    "rejected",
    "blocked",
    "locked_out",
    "applied",
)

#: Outcomes that mean the load is believed to be off or reduced.
_HOLDING_OUTCOMES = frozenset({"requested", "confirmed", "unconfirmed", "applied"})
#: Outcomes that mean the reduction did not happen and must be escalated.
_FAILED_OUTCOMES = frozenset({"no_reduction", "rejected", "blocked", "locked_out"})

#: Supervisory intent -> (command point, value) for shedding.
SHED_COMMANDS: dict[str, tuple[str, Any] | None] = {
    "stop": ("enabled_requested", False),
    "reduce": ("power_budget_kw", 0.0),
    "local_survival_profile": ("mode_requested", "survival"),
    "graceful_shutdown": ("mode_requested", "graceful_shutdown"),
    # Supervisory-only: the EMS withdraws the allocation, it does not switch
    # attended equipment (SDD 33.2 rules 6 and 7).
    "deny_new_grants": None,
    "revoke_lease": None,
    "never_shed": None,
}

#: Supervisory intent -> (command point, value) for restoration.
RESTORE_COMMANDS: dict[str, tuple[str, Any] | None] = {
    "stop": ("enabled_requested", True),
    "reduce": ("power_budget_kw", None),  # None -> the load's estimated demand
    "local_survival_profile": ("mode_requested", "normal"),
    "graceful_shutdown": ("mode_requested", "normal"),
    "deny_new_grants": None,
    "revoke_lease": None,
    "never_shed": None,
}


# ---------------------------------------------------------------------------
# Current load state
# ---------------------------------------------------------------------------


@dataclass
class LoadState:
    """What the EMS currently believes about one load, and how sure it is."""

    profile: PowerLoadProfile
    tier: int
    measured_kw: float | None
    last_action: LoadShedAction | None = None
    attempts: int = 0

    @property
    def asset_id(self) -> str:
        return self.profile.asset_id

    @property
    def shed_action(self) -> str:
        return (self.profile.shed or {}).get("action", "stop")

    @property
    def shed_permitted(self) -> bool:
        return bool((self.profile.shed or {}).get("permitted", False))

    @property
    def notification_required(self) -> bool:
        return bool((self.profile.shed or {}).get("notification_required", False))

    @property
    def restart(self) -> dict:
        return self.profile.restart or {}

    @property
    def automatic_restart_permitted(self) -> bool:
        return bool(self.restart.get("automatic_restart_permitted", True))

    @property
    def maximum_off_time_min(self) -> int | None:
        return (self.profile.minimum_service or {}).get("maximum_off_time_min")

    @property
    def is_shed(self) -> bool:
        """True when the EMS is holding this load off or reduced."""
        action = self.last_action
        return bool(action and action.action in {"shed", "reduce"} and action.outcome in _HOLDING_OUTCOMES)

    @property
    def shed_failed(self) -> bool:
        action = self.last_action
        return bool(action and action.action in {"shed", "reduce"} and action.outcome in _FAILED_OUTCOMES)

    @property
    def locked_out(self) -> bool:
        action = self.last_action
        return bool(action and action.outcome == "locked_out")

    @property
    def confirmed_shed(self) -> bool:
        action = self.last_action
        return bool(action and action.action in {"shed", "reduce"} and action.outcome == "confirmed")

    def since(self, now: dt.datetime) -> float | None:
        if self.last_action is None:
            return None
        return (now - _aware(self.last_action.occurred_at)).total_seconds()

    def expected_reduction_kw(self) -> float | None:
        return self.profile.estimated_power_kw or self.measured_kw


def current_load_states(
    session: Session, inputs: EmsInputs, *, now: dt.datetime, config: EmsConfig
) -> dict[str, LoadState]:
    """Latest believed state of every load in the schedule."""
    profiles = load_profiles(session)
    states: dict[str, LoadState] = {}
    for profile in profiles:
        states[profile.asset_id] = LoadState(
            profile=profile,
            tier=effective_tier(profile, now),
            measured_kw=inputs.numeric(load_input_key(profile.asset_id, "power_kw")),
        )

    if not states:
        return states

    actions = session.scalars(
        select(LoadShedAction)
        .where(LoadShedAction.asset_id.in_(list(states)))
        .order_by(LoadShedAction.occurred_at.asc())
    )
    attempts: dict[str, int] = {}
    for action in actions:
        state = states.get(action.asset_id)
        if state is None:
            continue
        if action.action == "restore" and action.outcome in _HOLDING_OUTCOMES:
            attempts[action.asset_id] = 0
        elif action.action in {"shed", "reduce"}:
            attempts[action.asset_id] = attempts.get(action.asset_id, 0) + 1
        state.last_action = action
    for asset_id, count in attempts.items():
        states[asset_id].attempts = count
    return states


# ---------------------------------------------------------------------------
# Preconditions (SDD 32.1 rule 1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreconditionResult:
    ok: bool
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": dict(self.checks), "reasons": list(self.reasons)}


def validate_shed_preconditions(
    inputs: EmsInputs, derived: DerivedEnergyState, config: EmsConfig
) -> PreconditionResult:
    """SDD 32.1 rule 1: verify that low reserve is real before shedding.

    Shedding on the strength of one bad meter is exactly the failure SDD 39
    test ``EMS-T003`` guards against, so every one of SOC, the BMS limits, the
    measured load and the source state has to be observable first.
    """
    checks = {
        "soc_valid": inputs.is_valid("battery_soc_pct"),
        "discharge_limit_valid": inputs.is_valid("battery_discharge_limit_kw"),
        "discharge_permissive_valid": inputs.is_valid("bms_discharge_permissive"),
        "site_load_valid": derived.valid("site_load_kw"),
        "reserve_valid": derived.valid("energy_above_emergency_reserve_kwh"),
    }
    reasons = tuple(f"{name} is not observable" for name, ok in checks.items() if not ok)
    return PreconditionResult(ok=not reasons, checks=checks, reasons=reasons)


# ---------------------------------------------------------------------------
# Step results
# ---------------------------------------------------------------------------


@dataclass
class LoadActionOutcome:
    asset_id: str
    action: str
    outcome: str
    group: str | None
    reason: str
    command_id: str | None = None
    expected_reduction_kw: float | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "action": self.action,
            "outcome": self.outcome,
            "group": self.group,
            "reason": self.reason,
            "command_id": self.command_id,
            "expected_reduction_kw": self.expected_reduction_kw,
            "detail": self.detail,
        }


@dataclass
class ShedStepResult:
    performed: bool = False
    group: str | None = None
    actions: list[LoadActionOutcome] = field(default_factory=list)
    escalations: list[LoadActionOutcome] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    preconditions: PreconditionResult | None = None
    reason: str = ""
    active_groups: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "performed": self.performed,
            "group": self.group,
            "actions": [a.as_dict() for a in self.actions],
            "escalations": [a.as_dict() for a in self.escalations],
            "skipped": dict(self.skipped),
            "preconditions": self.preconditions.as_dict() if self.preconditions else None,
            "reason": self.reason,
            "active_groups": list(self.active_groups),
        }


@dataclass
class RestoreStepResult:
    performed: bool = False
    group: str | None = None
    actions: list[LoadActionOutcome] = field(default_factory=list)
    deferred: dict[str, str] = field(default_factory=dict)
    operator_review_required: list[str] = field(default_factory=list)
    qualified: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "performed": self.performed,
            "group": self.group,
            "actions": [a.as_dict() for a in self.actions],
            "deferred": dict(self.deferred),
            "operator_review_required": list(self.operator_review_required),
            "qualified": self.qualified,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------


class ShedController:
    """Executes the SDD 32 shed sequence and the SDD 33 restore sequence."""

    def __init__(
        self,
        config: EmsConfig,
        command_port: CommandPort,
        *,
        settings: Settings | None = None,
        bus: MessageBus | None = None,
    ) -> None:
        self.config = config
        self.command_port = command_port
        self.settings = settings
        self.bus = bus

    # -- helpers ---------------------------------------------------------
    def _record(
        self,
        session: Session,
        *,
        asset_id: str,
        action: str,
        outcome: str,
        group: str | None,
        energy_state: str,
        reason: str,
        now: dt.datetime,
        command_id: str | None = None,
    ) -> LoadShedAction:
        record = LoadShedAction(
            asset_id=asset_id,
            action=action,
            group=group,
            energy_state=energy_state,
            reason=reason,
            command_id=command_id,
            outcome=outcome,
            occurred_at=now,
        )
        session.add(record)
        session.flush()
        return record

    def _alarm(self, asset_id: str, alarm_key: str, severity: str, detail: dict[str, Any]) -> None:
        """Publish an energy alarm event (SDD 37.1).

        The EMS does not own the alarm store; it publishes on the alarm topic so
        the alarm engine, the notifier and any local controller can react. That
        keeps the EMS independent of the alarm subsystem's availability.
        """
        logger.warning("EMS alarm %s for %s: %s", alarm_key, asset_id, detail)
        if self.bus is None or self.settings is None:
            return
        base = self.settings.mqtt_base_topic or DEFAULT_BASE
        envelope = EventEnvelope(
            asset_id=asset_id,
            event=alarm_key,
            detail={"severity": severity, **detail},
            source="ems",
        )
        try:
            self.bus.publish(alarm_topic(asset_id, alarm_key, base=base), envelope.to_payload(), qos=1)
        except Exception:  # pragma: no cover - a broker outage must not stop shedding
            logger.exception("Failed to publish %s for %s", alarm_key, asset_id)

    def publish_budget(self, asset_id: str, budget_kw: float, *, now: dt.datetime) -> None:
        """Publish the load's power budget (SDD 13).

        A budget is an allocation. It is not a safety permissive, and a budget of
        zero never substitutes for the local controller's own protection.
        """
        if self.bus is None or self.settings is None:
            return
        envelope = TelemetryEnvelope(
            ts=now,
            asset_id=asset_id,
            point="power_budget_kw",
            value=budget_kw,
            unit="kW",
            quality="calculated",
            source="ems",
        )
        try:
            self.bus.publish(
                load_budget_topic(self.settings, asset_id),
                envelope.to_payload(),
                qos=1,
                retain=self.config.publish_retained,
            )
        except Exception:  # pragma: no cover
            logger.exception("Failed to publish power budget for %s", asset_id)

    # -- confirmation (SDD 32.1 rules 5 and 6) ---------------------------
    def confirm_pending(
        self,
        session: Session,
        states: dict[str, LoadState],
        *,
        energy_state: str,
        now: dt.datetime,
    ) -> list[LoadActionOutcome]:
        """Check measured power for every shed still awaiting confirmation.

        SDD 32.2: "a load is not considered shed until measured current/power
        confirms the result". Where no measurement exists the reduction is
        marked unavailable rather than assumed.
        """
        results: list[LoadActionOutcome] = []
        for state in states.values():
            action = state.last_action
            if action is None or action.outcome != "requested":
                continue
            if action.action not in {"shed", "reduce"}:
                continue
            elapsed = state.since(now) or 0.0
            if elapsed < self.config.shed_confirm_delay_s:
                continue

            measured = state.measured_kw
            if measured is None:
                outcome = "unconfirmed"
                reason = (
                    f"no measured power available on {state.profile.measured_power_point}; "
                    "expected reduction marked unavailable"
                )
                self._alarm(
                    state.asset_id,
                    "load_shed_failed",
                    "warning",
                    {
                        "stage": "confirmation",
                        "detail": "measurement unavailable",
                        "point": state.profile.measured_power_point,
                        "binding_status": "not commissioned",
                    },
                )
            elif measured > self.config.shed_confirm_power_kw:
                outcome = "no_reduction"
                reason = (
                    f"load still drawing {measured:.3f} kW after "
                    f"{self.config.shed_confirm_delay_s} s; expected reduction unavailable"
                )
                self._alarm(
                    state.asset_id,
                    "load_shed_failed",
                    "major",
                    {"stage": "confirmation", "measured_kw": measured, "group": action.group},
                )
            else:
                outcome = "confirmed"
                reason = f"measured power {measured:.3f} kW confirms the reduction"

            action.outcome = outcome
            action.reason = f"{action.reason} | {reason}"
            session.flush()
            results.append(
                LoadActionOutcome(
                    asset_id=state.asset_id,
                    action=action.action,
                    outcome=outcome,
                    group=action.group,
                    reason=reason,
                )
            )

            if outcome in _FAILED_OUTCOMES and state.attempts >= self.config.shed_attempt_limit:
                self._lock_out(session, state, energy_state=energy_state, now=now)
        return results

    def _lock_out(
        self, session: Session, state: LoadState, *, energy_state: str, now: dt.datetime
    ) -> LoadActionOutcome:
        reason = (
            f"{state.attempts} shed attempts without a confirmed reduction; "
            "operator inspection required before further automatic action"
        )
        self._record(
            session,
            asset_id=state.asset_id,
            action="shed",
            outcome="locked_out",
            group=state.profile.shed_group,
            energy_state=energy_state,
            reason=reason,
            now=now,
        )
        self._alarm(
            state.asset_id,
            "load_shed_failed",
            "major",
            {"stage": "lockout", "attempts": state.attempts},
        )
        return LoadActionOutcome(
            asset_id=state.asset_id,
            action="shed",
            outcome="locked_out",
            group=state.profile.shed_group,
            reason=reason,
        )

    # -- shedding --------------------------------------------------------
    def shed_step(
        self,
        session: Session,
        *,
        energy_state: str,
        inputs: EmsInputs,
        derived: DerivedEnergyState,
        now: dt.datetime,
        states: dict[str, LoadState] | None = None,
        reason: str = "",
        force: bool = False,
    ) -> ShedStepResult:
        """Shed the next eligible group, or explain why nothing was shed."""
        result = ShedStepResult()
        states = (
            states
            if states is not None
            else current_load_states(session, inputs, now=now, config=self.config)
        )
        result.active_groups = active_shed_groups(states)

        if not force and energy_state not in self.config.shed_states:
            result.reason = f"{energy_state} does not permit automatic shedding"
            return result

        preconditions = validate_shed_preconditions(inputs, derived, self.config)
        result.preconditions = preconditions
        if not preconditions.ok:
            result.reason = "low reserve could not be verified: " + "; ".join(preconditions.reasons)
            self._alarm(
                self.settings.site_id if self.settings else "site",
                "energy_meter_data_invalid",
                "major",
                {"stage": "shed_precondition", "checks": preconditions.checks},
            )
            return result

        if not force:
            gate = self._group_gate(states, now=now)
            if gate is not None:
                result.reason = gate
                return result

        group, candidates, skipped = self._next_shed_group(states, now=now)
        result.skipped = skipped
        if group is None:
            result.reason = "no eligible load group remains"
            return result
        result.group = group

        for state in candidates:
            outcome = self._shed_one(
                session,
                state,
                group=group,
                energy_state=energy_state,
                reason=reason or f"{energy_state}: shedding group {group}",
                now=now,
            )
            result.actions.append(outcome)
            if outcome.outcome in _FAILED_OUTCOMES:
                result.escalations.append(outcome)

        result.performed = bool(result.actions)
        result.active_groups = active_shed_groups(states)
        if result.escalations:
            # SDD 32.3: recalculate with the load still present and advance.
            result.reason = (
                f"group {group} shed with {len(result.escalations)} failure(s); "
                "expected reduction recalculated with those loads still connected"
            )
        else:
            result.reason = f"group {group} shed"
        return result

    def _group_gate(self, states: dict[str, LoadState], *, now: dt.datetime) -> str | None:
        """SDD 32.1 rule 5: one group at a time, confirmed before the next.

        Returns the reason to wait, or ``None`` when the next group may go.
        """
        last_shed_at: dt.datetime | None = None
        for state in states.values():
            action = state.last_action
            if action is None or action.action not in {"shed", "reduce"}:
                continue
            occurred = _aware(action.occurred_at)
            elapsed = (now - occurred).total_seconds()
            if action.outcome == "requested" and elapsed < self.config.shed_confirm_delay_s:
                return (
                    f"awaiting measured confirmation of {state.asset_id} in group {action.group} "
                    f"({elapsed:.0f} s of {self.config.shed_confirm_delay_s} s)"
                )
            if last_shed_at is None or occurred > last_shed_at:
                last_shed_at = occurred
        if last_shed_at is not None:
            elapsed = (now - last_shed_at).total_seconds()
            if elapsed < self.config.shed_group_interval_s:
                return (
                    f"inter-group interval: {elapsed:.0f} s of "
                    f"{self.config.shed_group_interval_s} s since the last group"
                )
        return None

    def _next_shed_group(
        self, states: dict[str, LoadState], *, now: dt.datetime
    ) -> tuple[str | None, list[LoadState], dict[str, str]]:
        skipped: dict[str, str] = {}
        by_group: dict[str, list[LoadState]] = {}
        limit = _group_index(self.config.max_automatic_shed_group)

        for state in states.values():
            group = state.profile.shed_group
            if state.tier <= self.config.protected_tier:
                skipped[state.asset_id] = f"tier {state.tier} is protected and is never shed"
                continue
            if not state.shed_permitted:
                skipped[state.asset_id] = "shed not permitted by the load schedule"
                continue
            if group is None:
                skipped[state.asset_id] = "no shed group assigned"
                continue
            if _group_index(group) > limit:
                skipped[state.asset_id] = (
                    f"group {group} is beyond the automatic limit {self.config.max_automatic_shed_group}"
                )
                continue
            if state.locked_out:
                skipped[state.asset_id] = "locked out pending operator inspection"
                continue
            if state.is_shed:
                continue
            since = state.since(now)
            if state.shed_failed and since is not None and since < self.config.shed_reissue_interval_s:
                skipped[state.asset_id] = (
                    f"last attempt {since:.0f} s ago; waiting out the "
                    f"{self.config.shed_reissue_interval_s} s reissue interval"
                )
                continue
            minimum_on = state.profile.minimum_on_time_s
            if (
                minimum_on
                and state.last_action is not None
                and state.last_action.action == "restore"
                and since is not None
                and since < minimum_on
            ):
                skipped[state.asset_id] = (
                    f"minimum on time {minimum_on} s not met ({since:.0f} s since restore)"
                )
                continue
            by_group.setdefault(group, []).append(state)

        for group in sorted(by_group, key=_group_index):
            candidates = sorted(by_group[group], key=lambda s: (s.profile.shed_order or 0, s.asset_id))
            return group, candidates, skipped
        return None, [], skipped

    def _shed_one(
        self,
        session: Session,
        state: LoadState,
        *,
        group: str,
        energy_state: str,
        reason: str,
        now: dt.datetime,
    ) -> LoadActionOutcome:
        if state.tier <= self.config.protected_tier:  # belt and braces
            raise ValueError(f"Refusing to shed protected tier {state.tier} load {state.asset_id}")

        intent = state.shed_action
        command_spec = SHED_COMMANDS.get(intent)
        action = "reduce" if intent in {"reduce", "local_survival_profile"} else "shed"
        expected = state.expected_reduction_kw()
        full_reason = f"{reason} (intent={intent})"

        if command_spec is None:
            # Supervisory-only: withdraw the allocation and notify. No command
            # is sent to attended equipment (SDD 33.2 rules 6 and 7).
            self.publish_budget(state.asset_id, 0.0, now=now)
            record = self._record(
                session,
                asset_id=state.asset_id,
                action=action,
                outcome="applied",
                group=group,
                energy_state=energy_state,
                reason=f"{full_reason}; power budget withdrawn, no equipment command issued",
                now=now,
            )
            state.last_action = record
            state.attempts += 1
            if state.notification_required:
                self._alarm(
                    state.asset_id,
                    "load_budget_withdrawn",
                    "info",
                    {"group": group, "energy_state": energy_state, "intent": intent},
                )
            return LoadActionOutcome(
                asset_id=state.asset_id,
                action=action,
                outcome="applied",
                group=group,
                reason=full_reason,
                expected_reduction_kw=expected,
            )

        command_name, value = command_spec
        outcome: CommandOutcome = self.command_port.issue(
            CommandRequest(
                asset_id=state.asset_id,
                command=command_name,
                value=value,
                reason=full_reason,
                issued_by=self.config.actor,
                priority=10,
                correlation_id=f"ems-shed-{group}",
            )
        )
        state.attempts += 1

        if outcome.accepted:
            self.publish_budget(state.asset_id, 0.0, now=now)
            record = self._record(
                session,
                asset_id=state.asset_id,
                action=action,
                outcome="requested",
                group=group,
                energy_state=energy_state,
                reason=full_reason,
                now=now,
                command_id=outcome.command_id,
            )
            state.last_action = record
            if state.notification_required:
                self._alarm(
                    state.asset_id,
                    "load_shed_requested",
                    "info",
                    {"group": group, "energy_state": energy_state, "expected_reduction_kw": expected},
                )
            return LoadActionOutcome(
                asset_id=state.asset_id,
                action=action,
                outcome="requested",
                group=group,
                reason=full_reason,
                command_id=outcome.command_id,
                expected_reduction_kw=expected,
            )

        # SDD 32.3: rejected or blocked -> escalate, never assume off.
        failure_outcome = "blocked" if outcome.outcome == "blocked" else "rejected"
        detail = outcome.detail or "command not accepted"
        record = self._record(
            session,
            asset_id=state.asset_id,
            action=action,
            outcome=failure_outcome,
            group=group,
            energy_state=energy_state,
            reason=f"{full_reason}; command not accepted: {detail}",
            now=now,
        )
        state.last_action = record
        self._alarm(
            state.asset_id,
            "load_shed_failed",
            "major",
            {
                "stage": "dispatch",
                "group": group,
                "outcome": failure_outcome,
                "detail": detail,
                "expected_reduction_kw": expected,
            },
        )
        if state.attempts >= self.config.shed_attempt_limit:
            self._lock_out(session, state, energy_state=energy_state, now=now)
        return LoadActionOutcome(
            asset_id=state.asset_id,
            action=action,
            outcome=failure_outcome,
            group=group,
            reason=full_reason,
            expected_reduction_kw=expected,
            detail=detail,
        )

    # -- restoration -----------------------------------------------------
    def restoration_qualified(
        self,
        *,
        energy_state: str,
        state_entered_at: dt.datetime | None,
        inputs: EmsInputs,
        derived: DerivedEnergyState,
        now: dt.datetime,
    ) -> tuple[bool, str]:
        """SDD 33.1 restoration qualification."""
        if energy_state not in ("SURPLUS", "NORMAL"):
            return False, f"{energy_state} does not qualify for restoration"
        if state_entered_at is None:
            return False, "state entry time unknown"
        held_s = (now - _aware(state_entered_at)).total_seconds()
        if held_s < self.config.restore_qualification_s:
            return False, (
                f"{energy_state} has been held for {held_s:.0f} s of the required "
                f"{self.config.restore_qualification_s} s"
            )
        if not inputs.observable:
            return False, "required inputs are not observable"

        discharge = derived.value("available_discharge_kw")
        if discharge is None or discharge <= 0:
            return False, "battery discharge limit is not healthy"
        soc = derived.value("reserve_pct")
        if soc is None or soc < self.config.restore_soc_pct:
            return False, f"SOC {soc if soc is not None else 'unknown'} below the restoration threshold"
        margin = derived.value("forecast_energy_margin_kwh")
        if margin is None or margin < self.config.restore_margin_kwh:
            return False, (
                f"forecast margin {margin if margin is not None else 'unknown'} kWh below the "
                f"recovery threshold {self.config.restore_margin_kwh:.1f} kWh"
            )
        derate = derived.value("thermal_derate_pct")
        if derate is not None and derate > 0:
            return False, f"thermal derate {derate:.0f}% still requires conservation"
        headroom = derived.value("restoration_headroom_kw")
        if headroom is None or headroom <= 0:
            return False, "no restoration headroom"
        return True, (
            f"{energy_state} held {held_s:.0f} s with margin {margin:.1f} kWh and "
            f"{headroom:.1f} kW of headroom"
        )

    def restore_step(
        self,
        session: Session,
        *,
        energy_state: str,
        state_entered_at: dt.datetime | None,
        inputs: EmsInputs,
        derived: DerivedEnergyState,
        now: dt.datetime,
        states: dict[str, LoadState] | None = None,
    ) -> RestoreStepResult:
        """Restore at most ``restore_max_per_step`` loads, staggered."""
        result = RestoreStepResult()
        states = (
            states
            if states is not None
            else current_load_states(session, inputs, now=now, config=self.config)
        )

        qualified, why = self.restoration_qualified(
            energy_state=energy_state,
            state_entered_at=state_entered_at,
            inputs=inputs,
            derived=derived,
            now=now,
        )
        result.qualified = qualified
        result.reason = why
        if not qualified:
            return result

        headroom = derived.value("restoration_headroom_kw") or 0.0
        last_restore_at = _last_restore_at(session)
        candidates, deferred, review = self._restore_candidates(states, now=now, headroom=headroom)
        result.deferred = deferred
        result.operator_review_required = review
        if not candidates:
            result.reason = "no load is eligible for restoration yet"
            return result

        # Anti-simultaneous-restart (FR-103): one group, and a stagger between
        # individual starts sized by inrush class.
        group = candidates[0].profile.restoration_group
        result.group = group
        started = 0
        for state in candidates:
            if state.profile.restoration_group != group:
                break
            if started >= self.config.restore_max_per_step:
                result.deferred[state.asset_id] = (
                    f"only {self.config.restore_max_per_step} start(s) per step; staggered to the next step"
                )
                continue
            stagger = self.config.stagger_s(state.profile.inrush_class)
            if last_restore_at is not None:
                gap = (now - last_restore_at).total_seconds()
                if gap < stagger:
                    result.deferred[state.asset_id] = (
                        f"inrush class {state.profile.inrush_class}: {gap:.0f} s of the required "
                        f"{stagger} s stagger elapsed since the last restart"
                    )
                    continue
            outcome = self._restore_one(
                session, state, group=group, energy_state=energy_state, now=now, reason=why
            )
            result.actions.append(outcome)
            if outcome.outcome in _HOLDING_OUTCOMES:
                started += 1
                last_restore_at = now

        result.performed = bool(result.actions)
        if result.performed:
            result.reason = f"restored {len(result.actions)} load(s) in group {group}"
        return result

    def _restore_candidates(
        self, states: dict[str, LoadState], *, now: dt.datetime, headroom: float
    ) -> tuple[list[LoadState], dict[str, str], list[str]]:
        candidates: list[LoadState] = []
        deferred: dict[str, str] = {}
        review: list[str] = []

        for state in states.values():
            if not state.is_shed:
                continue
            if state.locked_out:
                deferred[state.asset_id] = "locked out pending operator inspection"
                review.append(state.asset_id)
                continue
            if not state.automatic_restart_permitted:
                # SDD 33.2 rules 6, 7, 9: attended equipment enters
                # operator_review_required rather than blindly resuming.
                deferred[state.asset_id] = "automatic restart not permitted; operator action required"
                review.append(state.asset_id)
                continue
            since = state.since(now) or 0.0
            minimum_off = max(
                state.profile.minimum_off_time_s or 0,
                int(state.restart.get("minimum_off_time_s") or 0),
            )
            if since < minimum_off:
                deferred[state.asset_id] = f"minimum off time {minimum_off} s not met ({since:.0f} s elapsed)"
                continue
            delay = int(state.restart.get("delay_s") or 0)
            if since < delay:
                deferred[state.asset_id] = f"restart delay {delay} s not met ({since:.0f} s elapsed)"
                continue
            expected = state.profile.estimated_power_kw
            if expected is not None and expected > headroom:
                deferred[state.asset_id] = (
                    f"estimated {expected:.2f} kW exceeds the {headroom:.2f} kW restoration headroom"
                )
                continue
            candidates.append(state)

        candidates.sort(
            key=lambda s: (
                _group_index(s.profile.restoration_group or "R99"),
                s.profile.restoration_order or 0,
                s.asset_id,
            )
        )
        return candidates, deferred, review

    def _restore_one(
        self,
        session: Session,
        state: LoadState,
        *,
        group: str | None,
        energy_state: str,
        now: dt.datetime,
        reason: str,
    ) -> LoadActionOutcome:
        intent = state.shed_action
        command_spec = RESTORE_COMMANDS.get(intent)
        full_reason = f"restoration in {energy_state}: {reason} (intent={intent})"
        budget = state.profile.estimated_power_kw or state.profile.rated_power_kw or 0.0

        if command_spec is None:
            self.publish_budget(state.asset_id, budget, now=now)
            record = self._record(
                session,
                asset_id=state.asset_id,
                action="restore",
                outcome="applied",
                group=group,
                energy_state=energy_state,
                reason=f"{full_reason}; allocation restored, a new lease is still required",
                now=now,
            )
            state.last_action = record
            return LoadActionOutcome(
                asset_id=state.asset_id,
                action="restore",
                outcome="applied",
                group=group,
                reason=full_reason,
            )

        command_name, value = command_spec
        if command_name == "power_budget_kw" and value is None:
            value = budget
        outcome = self.command_port.issue(
            CommandRequest(
                asset_id=state.asset_id,
                command=command_name,
                value=value,
                reason=full_reason,
                issued_by=self.config.actor,
                priority=50,
                correlation_id=f"ems-restore-{group}",
            )
        )
        if outcome.accepted:
            self.publish_budget(state.asset_id, budget, now=now)
            record = self._record(
                session,
                asset_id=state.asset_id,
                action="restore",
                outcome="requested",
                group=group,
                energy_state=energy_state,
                reason=full_reason,
                now=now,
                command_id=outcome.command_id,
            )
            state.last_action = record
            return LoadActionOutcome(
                asset_id=state.asset_id,
                action="restore",
                outcome="requested",
                group=group,
                reason=full_reason,
                command_id=outcome.command_id,
            )

        detail = outcome.detail or "command not accepted"
        record = self._record(
            session,
            asset_id=state.asset_id,
            action="restore",
            outcome="rejected",
            group=group,
            energy_state=energy_state,
            reason=f"{full_reason}; command not accepted: {detail}",
            now=now,
        )
        state.last_action = record
        self._alarm(state.asset_id, "load_restore_failed", "warning", {"group": group, "detail": detail})
        return LoadActionOutcome(
            asset_id=state.asset_id,
            action="restore",
            outcome="rejected",
            group=group,
            reason=full_reason,
            detail=detail,
        )

    # -- minimum service (SDD 31.1) --------------------------------------
    def forced_restores(
        self,
        session: Session,
        states: dict[str, LoadState],
        *,
        energy_state: str,
        now: dt.datetime,
    ) -> list[LoadActionOutcome]:
        """Restore loads whose ``maximum_off_time_min`` contract has expired.

        A minimum-service contract outranks the energy state: greenhouse freeze
        protection comes back even while the site is conserving. Only EMERGENCY
        and BLACK_START override it (SDD 32.1 rule 7).
        """
        results: list[LoadActionOutcome] = []
        if energy_state in {"EMERGENCY", "BLACK_START"}:
            return results
        for state in states.values():
            maximum_off = state.maximum_off_time_min
            if not maximum_off or not state.is_shed:
                continue
            since = state.since(now) or 0.0
            if since < maximum_off * 60:
                continue
            outcome = self._restore_one(
                session,
                state,
                group=state.profile.restoration_group,
                energy_state=energy_state,
                now=now,
                reason=(
                    f"minimum service {(state.profile.minimum_service or {}).get('mode')} reached its "
                    f"{maximum_off} min maximum off time"
                ),
            )
            outcome.reason = f"minimum_service_maximum_off_time: {outcome.reason}"
            results.append(outcome)
        return results


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def active_shed_groups(states: dict[str, LoadState]) -> list[str]:
    groups = {
        state.profile.shed_group for state in states.values() if state.is_shed and state.profile.shed_group
    }
    return sorted(groups, key=_group_index)


def recent_actions(
    session: Session, *, limit: int = 100, asset_id: str | None = None
) -> list[LoadShedAction]:
    statement = select(LoadShedAction).order_by(LoadShedAction.occurred_at.desc())
    if asset_id:
        statement = statement.where(LoadShedAction.asset_id == asset_id)
    return list(session.scalars(statement.limit(limit)))


def _last_restore_at(session: Session) -> dt.datetime | None:
    row = session.scalars(
        select(LoadShedAction)
        .where(LoadShedAction.action == "restore")
        .order_by(LoadShedAction.occurred_at.desc())
        .limit(1)
    ).first()
    return _aware(row.occurred_at) if row else None


def _group_index(group: str | None) -> int:
    if not group:
        return 99
    digits = "".join(ch for ch in group if ch.isdigit())
    return int(digits) if digits else 99


def _aware(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        raise ValueError("timestamp required")
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def summarise_loads(states: Iterable[LoadState]) -> list[dict[str, Any]]:
    """Dashboard view of every load (SDD 38)."""
    rows = []
    for state in states:
        profile = state.profile
        rows.append(
            {
                "asset_id": profile.asset_id,
                "name": profile.name,
                "base_tier": profile.base_tier,
                "effective_tier": state.tier,
                "tier_override_reason": profile.tier_override_reason,
                "tier_override_expires_at": (
                    profile.tier_override_expires_at.isoformat() if profile.tier_override_expires_at else None
                ),
                "criticality": profile.criticality,
                "control_method": profile.control_method,
                "rated_power_kw": profile.rated_power_kw,
                "estimated_power_kw": profile.estimated_power_kw,
                "measured_power_kw": state.measured_kw,
                "measured_power_point": profile.measured_power_point,
                "data_status": profile.data_status,
                "shed_group": profile.shed_group,
                "shed_order": profile.shed_order,
                "restoration_group": profile.restoration_group,
                "restoration_order": profile.restoration_order,
                "shed": profile.shed,
                "restart": profile.restart,
                "minimum_service": profile.minimum_service,
                "minimum_on_time_s": profile.minimum_on_time_s,
                "minimum_off_time_s": profile.minimum_off_time_s,
                "is_shed": state.is_shed,
                "shed_confirmed": state.confirmed_shed,
                "shed_failed": state.shed_failed,
                "locked_out": state.locked_out,
                "attempts": state.attempts,
                "last_action": (
                    {
                        "action": state.last_action.action,
                        "outcome": state.last_action.outcome,
                        "group": state.last_action.group,
                        "reason": state.last_action.reason,
                        "occurred_at": state.last_action.occurred_at.isoformat()
                        if state.last_action.occurred_at
                        else None,
                    }
                    if state.last_action
                    else None
                ),
                "open_fields": profile.open_fields,
            }
        )
    return rows
