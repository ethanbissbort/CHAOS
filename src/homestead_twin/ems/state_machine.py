"""The energy operating-state machine (SDD sections 30.7, 30.8, 30.9).

The EMS publishes exactly one site energy state. Subsystems translate that
state into their own bounded operating profiles (SDD 13); the EMS does not
switch every load itself.

Design rules implemented here
-----------------------------

* **Ten states**, exactly the set in ``models.energy.ENERGY_STATES``.
* **Qualification delays.** A candidate state must persist for its dwell time
  before it is adopted. The pending candidate is visible on the snapshot
  (``candidate_state`` / ``candidate_since``) so an operator can see what the
  EMS is about to do and why (SDD 30.9).
* **Separate entry and recovery thresholds.** Leaving ``CONSERVE`` or
  ``CRITICAL_RESERVE`` needs a *better* condition than entering it did, so a
  passing cloud cannot walk the site back and forth (SDD 30.9, test EMS-T006).
* **Latching states.** ``EMERGENCY``, ``MAINTENANCE`` and ``COMMISSIONING`` are
  never left automatically. Clearing one requires an operator, a reason and an
  explicit statement that the condition is clear.
* **Safety outranks everything.** ``EMERGENCY`` is evaluated before the latch
  hold and before an operator freeze, and it has no qualification delay.
* **Conservative under degraded observability.** ``DEGRADED_SENSOR`` ranks
  above ``CONSERVE`` on the conservatism scale, and if the still-valid inputs
  indicate something worse, the worse state wins (SDD 39 test EMS-T003).
* **Every transition is explained.** An ``EnergyStateTransition`` row records
  the trigger, the reason, and the input and derived snapshots that justified
  it (SDD 30.3 objective 9).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin.config import Settings
from homestead_twin.ems.config import EmsConfig, energy_state_topic
from homestead_twin.ems.derived import DerivedEnergyState
from homestead_twin.ems.inputs import INVERTERS, EmsInputs
from homestead_twin.envelope import TelemetryEnvelope
from homestead_twin.models.energy import (
    ENERGY_STATES,
    LATCHING_ENERGY_STATES,
    EnergyStateSnapshot,
    EnergyStateTransition,
)
from homestead_twin.mqtt import MessageBus

logger = logging.getLogger(__name__)

SNAPSHOT_ID = 1

#: How conservative each state is. Used when impaired observability and a
#: partial reading disagree: the more conservative state wins.
STATE_SEVERITY: dict[str, int] = {
    "SURPLUS": 0,
    "NORMAL": 1,
    "COMMISSIONING": 1,
    "MAINTENANCE": 2,
    "CONSERVE": 2,
    "GENERATOR_SUPPORT": 3,
    "DEGRADED_SENSOR": 3,
    "CRITICAL_RESERVE": 4,
    "BLACK_START": 5,
    "EMERGENCY": 6,
}

#: States in which new discretionary power may be granted (SDD 31.4, 32.1 rule 2).
GRANTING_STATES = frozenset({"SURPLUS", "NORMAL"})

#: States in which automatic load restoration may be considered (SDD 33.1).
RESTORING_STATES = frozenset({"SURPLUS", "NORMAL"})

#: States that override economic dispatch (SDD 30.7).
OVERRIDE_ECONOMIC_STATES = frozenset({"EMERGENCY", "BLACK_START", "MAINTENANCE", "COMMISSIONING"})


class LatchError(RuntimeError):
    """Raised when a latching state is cleared without the required conditions."""


@dataclass(frozen=True)
class Candidate:
    """The state the present conditions argue for, and why."""

    state: str
    trigger: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StateDecision:
    """Outcome of one evaluation."""

    current_state: str
    candidate: Candidate
    transitioned: bool
    dwell_remaining_s: float | None
    frozen: bool = False
    frozen_until: dt.datetime | None = None
    transition_id: str | None = None
    note: str | None = None

    @property
    def state(self) -> str:
        return self.current_state

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.current_state,
            "candidate_state": self.candidate.state,
            "trigger": self.candidate.trigger,
            "reason": self.candidate.reason,
            "transitioned": self.transitioned,
            "dwell_remaining_s": self.dwell_remaining_s,
            "frozen": self.frozen,
            "frozen_until": self.frozen_until.isoformat() if self.frozen_until else None,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def get_snapshot(session: Session) -> EnergyStateSnapshot | None:
    return session.get(EnergyStateSnapshot, SNAPSHOT_ID)


def ensure_snapshot(session: Session, *, now: dt.datetime | None = None) -> EnergyStateSnapshot:
    """Return the singleton snapshot, creating it in ``COMMISSIONING``.

    SDD 19: the platform starts in a commissioning state and is walked out of
    it deliberately. ``COMMISSIONING`` is latching, so this default cannot be
    left by an automatic transition.
    """
    snapshot = get_snapshot(session)
    if snapshot is None:
        snapshot = EnergyStateSnapshot(
            id=SNAPSHOT_ID,
            state="COMMISSIONING",
            entered_at=now,
            inputs={},
            derived={},
            data_quality="unknown",
            shed_groups_active=[],
        )
        session.add(snapshot)
        session.flush()
    return snapshot


# ---------------------------------------------------------------------------
# Condition evaluation (SDD 30.8)
# ---------------------------------------------------------------------------


def _inverter_fault(inputs: EmsInputs, config: EmsConfig) -> tuple[bool, list[str]]:
    """Shutdown-class inverter fault on any functional inverter position."""
    faulted: list[str] = []
    for index in range(1, len(INVERTERS) + 1):
        state = inputs.text(f"inverter_{index:02d}_state")
        code = inputs.text(f"inverter_{index:02d}_fault_code")
        if state and state in config.inverter_fault_states:
            faulted.append(f"inverter_{index:02d}_state={state}")
        elif code and any(token in code for token in config.inverter_shutdown_fault_codes):
            faulted.append(f"inverter_{index:02d}_fault_code={code}")
    return bool(faulted), faulted


def check_emergency(inputs: EmsInputs, derived: DerivedEnergyState, config: EmsConfig) -> Candidate | None:
    """SDD 30.8 ``Enter EMERGENCY``. Protection-adjacent, no dwell."""
    reasons: list[str] = []
    detail: dict[str, Any] = {}

    # 1. BMS discharge is not permitted while AC loads remain.
    discharge_permissive = inputs.flag("bms_discharge_permissive")
    site_load = derived.value("site_load_kw")
    if discharge_permissive is False and site_load is not None and site_load > config.residual_load_kw:
        reasons.append(f"BMS discharge inhibited with {site_load:.2f} kW of AC load still connected")
        detail["site_load_kw"] = site_load

    # 2. Inverter reports a shutdown-class fault.
    faulted, fault_detail = _inverter_fault(inputs, config)
    if faulted:
        reasons.append("shutdown-class inverter fault")
        detail["inverter_faults"] = fault_detail

    # 3. Battery or container temperature above the emergency threshold.
    battery_temp = inputs.numeric("battery_temperature_max_c")
    if battery_temp is not None and battery_temp >= config.battery_temp_emergency_c:
        reasons.append(
            f"battery cell temperature {battery_temp:.1f} C at or above the emergency limit "
            f"{config.battery_temp_emergency_c:.1f} C"
        )
        detail["battery_temperature_max_c"] = battery_temp
    container_temp = inputs.numeric("container_temperature_c")
    if container_temp is not None and container_temp >= config.container_temp_emergency_c:
        reasons.append(
            f"power-container temperature {container_temp:.1f} C at or above the emergency limit "
            f"{config.container_temp_emergency_c:.1f} C"
        )
        detail["container_temperature_c"] = container_temp

    # 4. Fire / smoke / emergency-stop logic requests power isolation.
    for key in ("container_alarm_summary", "rack_alarm_summary"):
        summary = inputs.text(key)
        if summary and summary in config.emergency_alarm_summaries:
            reasons.append(f"{key} = {summary}")
            detail[key] = summary

    # 5. Control authority and source state contradict each other.
    source_selected = inputs.text("ats_source_selected")
    source_a = inputs.flag("ats_source_a_available")
    source_b = inputs.flag("ats_source_b_available")
    if (
        source_selected in {"none", "open", "off"}
        and source_a is False
        and source_b is False
        and site_load is not None
        and site_load > config.residual_load_kw
    ):
        reasons.append("no source available at the transfer assembly while load is still connected")
        detail["ats_source_selected"] = source_selected

    if not reasons:
        return None
    return Candidate(
        state="EMERGENCY",
        trigger="emergency_condition",
        reason="; ".join(reasons),
        detail=detail,
    )


def _critical_reserve_reasons(inputs: EmsInputs, derived: DerivedEnergyState, config: EmsConfig) -> list[str]:
    """SDD 30.8 ``Enter CRITICAL_RESERVE``."""
    reasons: list[str] = []

    above = derived.value("energy_above_emergency_reserve_kwh")
    if above is not None and above < config.critical_margin_kwh:
        reasons.append(
            f"energy above the emergency reserve {above:.1f} kWh is below the critical margin "
            f"{config.critical_margin_kwh:.1f} kWh"
        )

    autonomy = derived.value("autonomy_critical_h")
    if autonomy is not None and autonomy < config.response_horizon_h:
        reasons.append(
            f"critical-load autonomy {autonomy:.1f} h is below the response horizon "
            f"{config.response_horizon_h:.1f} h"
        )

    faulted, fault_detail = _inverter_fault(inputs, config)
    pv_kw = derived.value("pv_power_kw")
    balance = derived.value("energy_balance_kw")
    if faulted and balance is not None and balance < 0:
        reasons.append(f"inverter fault with a {abs(balance):.1f} kW deficit ({', '.join(fault_detail)})")
    availability = inputs.text("pv_availability")
    if availability == "offline" and pv_kw is not None and balance is not None and balance < 0:
        reasons.append("PV array offline while the site is in deficit")

    discharge = derived.value("available_discharge_kw")
    site_load = derived.value("site_load_kw")
    if (
        discharge is not None
        and site_load is not None
        and discharge < site_load * config.discharge_limit_margin
    ):
        reasons.append(
            f"battery discharge limit {discharge:.1f} kW cannot safely support the present load "
            f"{site_load:.1f} kW"
        )
    return reasons


def _critical_recovered(derived: DerivedEnergyState, config: EmsConfig) -> tuple[bool, str]:
    """Recovery from ``CRITICAL_RESERVE`` uses its own, higher thresholds."""
    above = derived.value("energy_above_emergency_reserve_kwh")
    autonomy = derived.value("autonomy_critical_h")
    if above is None:
        return False, "energy above the emergency reserve is not observable"
    if above < config.critical_recovery_margin_kwh:
        return False, (
            f"energy above the emergency reserve {above:.1f} kWh has not reached the recovery margin "
            f"{config.critical_recovery_margin_kwh:.1f} kWh"
        )
    if autonomy is not None and autonomy < config.critical_autonomy_recovery_h:
        return False, (
            f"critical autonomy {autonomy:.1f} h has not reached the recovery horizon "
            f"{config.critical_autonomy_recovery_h:.1f} h"
        )
    return True, (
        f"energy above the emergency reserve {above:.1f} kWh met the recovery margin "
        f"{config.critical_recovery_margin_kwh:.1f} kWh"
    )


def _conserve_reasons(inputs: EmsInputs, derived: DerivedEnergyState, config: EmsConfig) -> list[str]:
    """SDD 30.8 ``Enter CONSERVE``. Any configured combination may qualify."""
    reasons: list[str] = []

    soc = derived.value("reserve_pct")
    if soc is not None and soc < config.normal_reserve_soc_pct:
        reasons.append(
            f"SOC {soc:.1f}% is below the normal reserve target {config.normal_reserve_soc_pct:.1f}%"
        )

    margin = derived.value("forecast_energy_margin_kwh")
    if margin is not None and margin < 0:
        reasons.append(f"forecast energy margin {margin:.1f} kWh is negative")

    discharge = derived.value("available_discharge_kw")
    site_load = derived.value("site_load_kw")
    if discharge is not None and site_load is not None and 0 < discharge < site_load * 1.5:
        reasons.append(
            f"available discharge {discharge:.1f} kW is close to the present load {site_load:.1f} kW"
        )

    derate = derived.value("thermal_derate_pct")
    if derate is not None and derate > 0:
        reasons.append(f"thermal derate {derate:.0f}% active on the battery or container")

    generator_available = inputs.flag("generator_available")
    balance = derived.value("energy_balance_kw")
    if generator_available is False and balance is not None and balance < 0:
        reasons.append("generator unavailable while the site reserve is trending down")

    return reasons


def _conserve_recovered(derived: DerivedEnergyState, config: EmsConfig) -> tuple[bool, str]:
    """SDD 30.9: sustained positive margin is required to leave ``CONSERVE``."""
    soc = derived.value("reserve_pct")
    margin = derived.value("forecast_energy_margin_kwh")
    if soc is None:
        return False, "SOC is not observable"
    if soc < config.conserve_recovery_soc_pct:
        return False, (
            f"SOC {soc:.1f}% has not reached the conserve recovery threshold "
            f"{config.conserve_recovery_soc_pct:.1f}%"
        )
    if margin is not None and margin < config.conserve_recovery_margin_kwh:
        return False, (
            f"forecast margin {margin:.1f} kWh has not reached the recovery margin "
            f"{config.conserve_recovery_margin_kwh:.1f} kWh"
        )
    return True, f"SOC {soc:.1f}% and forecast margin met the conserve recovery thresholds"


def _surplus_reasons(inputs: EmsInputs, derived: DerivedEnergyState, config: EmsConfig) -> list[str] | None:
    """SDD 30.8 ``Enter SURPLUS``: all conditions should normally be true."""
    soc = derived.value("reserve_pct")
    if soc is None or soc < config.surplus_soc_pct:
        return None

    charge_permissive = inputs.flag("bms_charge_permissive")
    surplus_kw = derived.value("surplus_power_kw")
    accepts_charge = charge_permissive is True
    curtailing = surplus_kw is not None and surplus_kw > 0
    if not (accepts_charge or curtailing):
        return None

    pv_kw = derived.value("pv_power_kw")
    site_load = derived.value("site_load_kw")
    if pv_kw is None or site_load is None or pv_kw < site_load + config.surplus_pv_headroom_kw:
        return None

    margin = derived.value("forecast_energy_margin_kwh")
    if margin is None or margin <= 0:
        return None

    derate = derived.value("thermal_derate_pct")
    if derate is not None and derate > 0:
        return None

    return [
        f"SOC {soc:.1f}% is above the surplus threshold {config.surplus_soc_pct:.1f}%",
        f"PV {pv_kw:.1f} kW exceeds site load {site_load:.1f} kW",
        f"forecast energy margin {margin:.1f} kWh is positive",
    ]


def _surplus_hold(derived: DerivedEnergyState, config: EmsConfig) -> bool:
    """Stay in ``SURPLUS`` until SOC drops through the separate exit threshold."""
    soc = derived.value("reserve_pct")
    margin = derived.value("forecast_energy_margin_kwh")
    if soc is None or soc < config.surplus_exit_soc_pct:
        return False
    return margin is None or margin > 0


def economic_candidate(
    current_state: str, inputs: EmsInputs, derived: DerivedEnergyState, config: EmsConfig
) -> Candidate:
    """Pick between SURPLUS / NORMAL / CONSERVE / CRITICAL_RESERVE.

    Hysteresis is expressed by making the answer depend on the state the site is
    already in: entry thresholds apply on the way down, recovery thresholds on
    the way up, and the gap between them is a deadband in which the current
    state is held.
    """
    critical_reasons = _critical_reserve_reasons(inputs, derived, config)

    if current_state == "CRITICAL_RESERVE":
        if critical_reasons:
            return Candidate("CRITICAL_RESERVE", "critical_reserve_hold", "; ".join(critical_reasons))
        recovered, why = _critical_recovered(derived, config)
        if not recovered:
            return Candidate("CRITICAL_RESERVE", "critical_reserve_deadband", why)
        # Climb back one rung at a time; NORMAL has to be earned from CONSERVE.
        return Candidate("CONSERVE", "critical_reserve_recovery", why)

    if critical_reasons:
        return Candidate("CRITICAL_RESERVE", "critical_reserve_entry", "; ".join(critical_reasons))

    conserve_reasons = _conserve_reasons(inputs, derived, config)

    if current_state == "CONSERVE":
        if conserve_reasons:
            return Candidate("CONSERVE", "conserve_hold", "; ".join(conserve_reasons))
        recovered, why = _conserve_recovered(derived, config)
        if not recovered:
            return Candidate("CONSERVE", "conserve_deadband", why)
        surplus = _surplus_reasons(inputs, derived, config)
        if surplus:
            return Candidate("SURPLUS", "surplus_entry", "; ".join(surplus))
        return Candidate("NORMAL", "conserve_recovery", why)

    if conserve_reasons:
        return Candidate("CONSERVE", "conserve_entry", "; ".join(conserve_reasons))

    surplus = _surplus_reasons(inputs, derived, config)
    if surplus:
        return Candidate("SURPLUS", "surplus_entry", "; ".join(surplus))
    if current_state == "SURPLUS" and _surplus_hold(derived, config):
        return Candidate("SURPLUS", "surplus_hold", "SOC still above the surplus exit threshold")

    return Candidate("NORMAL", "normal", "reserve and equipment healthy")


def select_candidate(
    current_state: str,
    inputs: EmsInputs,
    derived: DerivedEnergyState,
    config: EmsConfig,
    *,
    black_start_active: bool = False,
    generator_supporting: bool = False,
) -> Candidate:
    """Choose the state the present conditions argue for (SDD 30.7, 30.8).

    Precedence: EMERGENCY, then latch hold, then BLACK_START, then impaired
    observability, then generator support, then economic dispatch.
    """
    emergency = check_emergency(inputs, derived, config)
    if emergency is not None:
        return emergency

    if current_state in LATCHING_ENERGY_STATES:
        return Candidate(
            current_state,
            "latched",
            f"{current_state} is latching and is only left by an authorised operator action",
        )

    if black_start_active:
        return Candidate(
            "BLACK_START",
            "black_start_active",
            "black-start sequence in progress; economic dispatch is overridden",
        )

    if not inputs.observable:
        invalid = inputs.invalid_required()
        detail = {reading.key: reading.status for reading in invalid}
        degraded = Candidate(
            "DEGRADED_SENSOR",
            "required_input_invalid",
            "required inputs invalid: "
            + ", ".join(f"{reading.key} ({reading.status})" for reading in invalid),
            detail,
        )
        # Conservative, never optimistic: if what is still readable argues for a
        # more severe state, take the more severe state.
        fallback = economic_candidate(current_state, inputs, derived, config)
        if STATE_SEVERITY.get(fallback.state, 0) > STATE_SEVERITY["DEGRADED_SENSOR"]:
            return Candidate(
                fallback.state,
                fallback.trigger,
                f"{fallback.reason}; observability impaired ({degraded.reason})",
                detail,
            )
        return degraded

    if generator_supporting:
        return Candidate(
            "GENERATOR_SUPPORT",
            "generator_supporting",
            "generator is running or its start request has been accepted",
        )

    return economic_candidate(current_state, inputs, derived, config)


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------


class EnergyStateMachine:
    """Applies dwell, freeze and latch rules and records every transition."""

    def __init__(self, config: EmsConfig, settings: Settings | None = None) -> None:
        self.config = config
        self.settings = settings

    # -- evaluation ------------------------------------------------------
    def evaluate(
        self,
        session: Session,
        inputs: EmsInputs,
        derived: DerivedEnergyState,
        *,
        now: dt.datetime,
        black_start_active: bool = False,
        generator_supporting: bool = False,
        shed_groups_active: list[str] | None = None,
        generator_request: str | None = None,
    ) -> StateDecision:
        snapshot = ensure_snapshot(session, now=now)
        current = snapshot.state
        candidate = select_candidate(
            current,
            inputs,
            derived,
            self.config,
            black_start_active=black_start_active,
            generator_supporting=generator_supporting,
        )

        frozen_until = _aware(snapshot.frozen_until)
        frozen = frozen_until is not None and frozen_until > now
        if frozen_until is not None and frozen_until <= now:
            # The freeze lapsed; clear it so the snapshot reflects reality.
            snapshot.frozen_until = None
            snapshot.frozen_by = None

        transitioned = False
        transition_id: str | None = None
        note: str | None = None
        dwell_remaining: float | None = None

        if candidate.state == current:
            snapshot.candidate_state = None
            snapshot.candidate_since = None
        else:
            if snapshot.candidate_state != candidate.state:
                snapshot.candidate_state = candidate.state
                snapshot.candidate_since = now
            dwell = self.config.dwell_s(candidate.state)
            since = _aware(snapshot.candidate_since) or now
            elapsed = (now - since).total_seconds()
            dwell_remaining = max(dwell - elapsed, 0.0)

            if frozen and candidate.state != "EMERGENCY":
                # A freeze holds the published state but never hides what the
                # EMS wants to do, and never suppresses EMERGENCY (SDD 30.9).
                note = (
                    f"state frozen by {snapshot.frozen_by or 'operator'} until "
                    f"{frozen_until.isoformat() if frozen_until else 'unknown'}; "
                    f"candidate {candidate.state} withheld"
                )
            elif elapsed >= dwell:
                transition_id = self._transition(
                    session,
                    snapshot,
                    candidate,
                    inputs,
                    derived,
                    now=now,
                    actor=self.config.actor,
                )
                transitioned = True
                dwell_remaining = 0.0
                if frozen:
                    note = "freeze overridden: EMERGENCY is never suppressed by an operator freeze"
                    snapshot.frozen_until = None
                    snapshot.frozen_by = None

        snapshot.inputs = inputs.as_dict()
        snapshot.derived = _merge_derived(snapshot.derived, derived.as_dict())
        snapshot.data_quality = inputs.data_quality
        snapshot.last_evaluated_at = now
        if shed_groups_active is not None:
            snapshot.shed_groups_active = list(shed_groups_active)
        if generator_request is not None:
            snapshot.generator_request = generator_request
        session.flush()

        return StateDecision(
            current_state=snapshot.state,
            candidate=candidate,
            transitioned=transitioned,
            dwell_remaining_s=dwell_remaining,
            frozen=frozen and not transitioned,
            frozen_until=_aware(snapshot.frozen_until),
            transition_id=transition_id,
            note=note,
        )

    def _transition(
        self,
        session: Session,
        snapshot: EnergyStateSnapshot,
        candidate: Candidate,
        inputs: EmsInputs,
        derived: DerivedEnergyState,
        *,
        now: dt.datetime,
        actor: str,
    ) -> str:
        record = EnergyStateTransition(
            from_state=snapshot.state,
            to_state=candidate.state,
            trigger=candidate.trigger,
            reason=candidate.reason,
            inputs_snapshot=inputs.summary() | {"detail": candidate.detail},
            derived_snapshot=derived.summary(),
            actor=actor,
            occurred_at=now,
        )
        session.add(record)
        session.flush()
        logger.info(
            "EMS state %s -> %s (%s): %s",
            snapshot.state,
            candidate.state,
            candidate.trigger,
            candidate.reason,
        )
        snapshot.state = candidate.state
        snapshot.entered_at = now
        snapshot.candidate_state = None
        snapshot.candidate_since = None
        return record.id

    # -- operator actions -------------------------------------------------
    def freeze(
        self,
        session: Session,
        *,
        actor: str,
        reason: str,
        now: dt.datetime,
        duration_s: int | None = None,
    ) -> EnergyStateSnapshot:
        """Hold the published state for a bounded period (SDD 30.9).

        A freeze is always bounded by ``EmsConfig.freeze_max_s`` and never
        suppresses ``EMERGENCY``.
        """
        if not reason:
            raise ValueError("A freeze requires a reason")
        requested = duration_s if duration_s is not None else self.config.freeze_default_s
        if requested <= 0:
            raise ValueError("Freeze duration must be positive")
        duration = min(requested, self.config.freeze_max_s)
        snapshot = ensure_snapshot(session, now=now)
        snapshot.frozen_until = now + dt.timedelta(seconds=duration)
        snapshot.frozen_by = actor
        session.add(
            EnergyStateTransition(
                from_state=snapshot.state,
                to_state=snapshot.state,
                trigger="operator_freeze",
                reason=f"{reason} (frozen for {duration} s; EMERGENCY is never suppressed)",
                inputs_snapshot={},
                derived_snapshot={},
                actor=actor,
                occurred_at=now,
            )
        )
        session.flush()
        return snapshot

    def unfreeze(self, session: Session, *, actor: str, reason: str, now: dt.datetime) -> EnergyStateSnapshot:
        snapshot = ensure_snapshot(session, now=now)
        snapshot.frozen_until = None
        snapshot.frozen_by = None
        session.add(
            EnergyStateTransition(
                from_state=snapshot.state,
                to_state=snapshot.state,
                trigger="operator_unfreeze",
                reason=reason,
                inputs_snapshot={},
                derived_snapshot={},
                actor=actor,
                occurred_at=now,
            )
        )
        session.flush()
        return snapshot

    def set_state(
        self,
        session: Session,
        state: str,
        *,
        actor: str,
        reason: str,
        now: dt.datetime,
        trigger: str = "operator_action",
    ) -> EnergyStateSnapshot:
        """Operator-driven state selection, used to enter MAINTENANCE etc."""
        if state not in ENERGY_STATES:
            raise ValueError(f"Unknown energy state: {state}")
        if not reason:
            raise ValueError("A state change requires a reason")
        snapshot = ensure_snapshot(session, now=now)
        if snapshot.state in LATCHING_ENERGY_STATES and state != snapshot.state:
            raise LatchError(
                f"{snapshot.state} is latching; clear the latch with a reason and a "
                "condition-clear confirmation before selecting another state"
            )
        session.add(
            EnergyStateTransition(
                from_state=snapshot.state,
                to_state=state,
                trigger=trigger,
                reason=reason,
                inputs_snapshot={},
                derived_snapshot={},
                actor=actor,
                occurred_at=now,
            )
        )
        snapshot.state = state
        snapshot.entered_at = now
        snapshot.candidate_state = None
        snapshot.candidate_since = None
        session.flush()
        return snapshot

    def clear_latch(
        self,
        session: Session,
        *,
        actor: str,
        reason: str,
        condition_clear: bool,
        now: dt.datetime,
        to_state: str = "CONSERVE",
    ) -> EnergyStateSnapshot:
        """Leave a latching state (SDD 30.7, 11).

        Requires a named operator, a reason and an explicit statement that the
        originating condition has been removed. The EMS re-enters at a
        conservative state and has to earn its way back to NORMAL through the
        usual dwell and hysteresis rules; it never returns straight to SURPLUS.
        """
        snapshot = ensure_snapshot(session, now=now)
        if snapshot.state not in LATCHING_ENERGY_STATES:
            raise LatchError(f"{snapshot.state} is not a latching state")
        if not condition_clear:
            raise LatchError("The originating condition must be confirmed clear before a latch is released")
        if not reason:
            raise LatchError("Clearing a latch requires a reason")
        if to_state not in ENERGY_STATES:
            raise ValueError(f"Unknown energy state: {to_state}")
        if to_state in LATCHING_ENERGY_STATES and to_state != "COMMISSIONING":
            raise LatchError(f"Cannot clear a latch into the latching state {to_state}")
        if STATE_SEVERITY.get(to_state, 0) < STATE_SEVERITY["CONSERVE"]:
            raise LatchError(f"A latch may only be cleared into a conservative state; {to_state} is not one")

        session.add(
            EnergyStateTransition(
                from_state=snapshot.state,
                to_state=to_state,
                trigger="operator_clear_latch",
                reason=reason,
                inputs_snapshot={"condition_clear": condition_clear},
                derived_snapshot={},
                actor=actor,
                occurred_at=now,
            )
        )
        logger.warning("EMS latch %s cleared by %s: %s", snapshot.state, actor, reason)
        snapshot.state = to_state
        snapshot.entered_at = now
        snapshot.candidate_state = None
        snapshot.candidate_since = None
        session.flush()
        return snapshot


# ---------------------------------------------------------------------------
# Publication (SDD 13)
# ---------------------------------------------------------------------------


def state_envelope(
    snapshot: EnergyStateSnapshot,
    settings: Settings,
    *,
    now: dt.datetime,
    reason: str | None = None,
) -> TelemetryEnvelope:
    """The retained site energy state message."""
    quality = "calculated"
    if snapshot.state == "MAINTENANCE":
        quality = "maintenance"
    elif snapshot.data_quality in {"degraded", "bad", "unknown"}:
        quality = "uncertain"
    return TelemetryEnvelope(
        ts=now,
        asset_id=settings.site_id,
        point="energy_state",
        value={
            "state": snapshot.state,
            "entered_at": snapshot.entered_at.isoformat() if snapshot.entered_at else None,
            "candidate_state": snapshot.candidate_state,
            "candidate_since": (snapshot.candidate_since.isoformat() if snapshot.candidate_since else None),
            "frozen_until": snapshot.frozen_until.isoformat() if snapshot.frozen_until else None,
            "data_quality": snapshot.data_quality,
            "shed_groups_active": list(snapshot.shed_groups_active or []),
            "generator_request": snapshot.generator_request,
            "latching": snapshot.state in LATCHING_ENERGY_STATES,
            "reason": reason,
        },
        quality=quality,
        source="ems",
    )


def publish_state(
    bus: MessageBus,
    settings: Settings,
    snapshot: EnergyStateSnapshot,
    *,
    now: dt.datetime,
    reason: str | None = None,
    retain: bool = True,
) -> str:
    """Publish the site energy state and return the topic used."""
    topic = energy_state_topic(settings)
    envelope = state_envelope(snapshot, settings, now=now, reason=reason)
    bus.publish(topic, envelope.to_payload(), qos=1, retain=retain)
    return topic


def recent_transitions(
    session: Session, *, limit: int = 50, since: dt.datetime | None = None
) -> list[EnergyStateTransition]:
    statement = select(EnergyStateTransition).order_by(EnergyStateTransition.occurred_at.desc())
    if since is not None:
        statement = statement.where(EnergyStateTransition.occurred_at >= since)
    return list(session.scalars(statement.limit(limit)))


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite hands back naive datetimes; the platform is UTC throughout."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def _merge_derived(existing: dict | None, incoming: dict) -> dict:
    """Keep subsystem scratch state (generator, black start) across updates."""
    merged = dict(incoming)
    for key in ("generator", "black_start"):
        if existing and key in existing:
            merged[key] = existing[key]
    return merged
