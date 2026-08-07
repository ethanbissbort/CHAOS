"""Black start and recovery (SDD section 35).

Black start is the controlled recovery from a fully de-energised AC
distribution state, or from an inverter shutdown where the normal supervisory
services may themselves be unavailable. SDD 35.2 therefore requires the
prerequisites to be *independent* of this software: protected DC control power,
battery limits, clear fire/emergency-stop conditions, the ability to isolate the
critical distribution from noncritical branches, and at least one local
controller that can execute the sequence without the primary server rack.

This module owns three things:

1. The independent prerequisite checks, evaluated from whatever telemetry is
   reachable and reported honestly as ``unknown`` when they are not.
2. The eleven-step sequence of SDD 35.3 as an explicit, auditable list. Steps
   that are physical, or that belong to a native controller, are marked
   ``manual`` -- the platform tracks them, it does not perform them.
3. The SDD 35.4 reconciliation: after services recover, the platform must not
   assume that retained desired state equals physical state.

No unattended restart of workshop machinery, spa equipment or other attended
loads is permitted after a black start (SDD 35.3).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin.ems.config import EmsConfig
from homestead_twin.ems.inputs import EmsInputs
from homestead_twin.models.commands import TERMINAL_COMMAND_STATES, Command
from homestead_twin.models.energy import LoadShedAction, PowerBudgetLease
from homestead_twin.models.telemetry import CurrentState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Prerequisite:
    name: str
    satisfied: bool | None
    detail: str
    independent_of_platform: bool = True

    @property
    def blocking(self) -> bool:
        """Unknown blocks: a black start is not begun on an unverified check."""
        return self.satisfied is not True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "satisfied": self.satisfied,
            "detail": self.detail,
            "independent_of_platform": self.independent_of_platform,
        }


@dataclass(frozen=True)
class BlackStartStep:
    key: str
    order: int
    description: str
    #: ``manual`` steps are physical or belong to a native controller. The
    #: platform records them; it never claims to have performed them.
    manual: bool
    owner: str


#: SDD 35.3 preliminary sequence, verbatim in intent.
BLACK_START_STEPS: tuple[BlackStartStep, ...] = (
    BlackStartStep("verify_safety", 1, "Verify emergency isolation and physical safety conditions", True, "operator"),
    BlackStartStep("energize_controls", 2, "Energize BMS, inverter controls and critical control power", True, "operator"),
    BlackStartStep("close_contactor", 3, "Close the battery contactor through the native precharge sequence", True, "bms"),
    BlackStartStep("start_master_inverter", 4, "Start one inverter or the manufacturer-defined master group", True, "inverter"),
    BlackStartStep("energize_critical_bus", 5, "Energize the critical control/communications bus only", True, "operator"),
    BlackStartStep("start_core_services", 6, "Start the secondary control node, core switch/router and minimum MQTT/time services", False, "platform"),
    BlackStartStep("validate_measurements", 7, "Validate battery, inverter, frequency and critical-bus measurements", False, "platform"),
    BlackStartStep("energize_survival_loads", 8, "Energize minimum refrigeration, water protection, greenhouse survival and security loads in staggered order", False, "platform"),
    BlackStartStep("generator_if_required", 9, "Start the generator if reserve or battery limits require it", False, "platform"),
    BlackStartStep("start_rack_services", 10, "Start primary rack services once the critical bus and container environment are stable", False, "platform"),
    BlackStartStep("reconcile", 11, "Reconcile actual asset states with the digital twin before normal restoration", False, "platform"),
)

STEP_BY_KEY = {step.key: step for step in BLACK_START_STEPS}


@dataclass
class BlackStartState:
    """Serialisable progress record, stored on the snapshot's derived blob."""

    active: bool = False
    started_at: str | None = None
    started_by: str | None = None
    reason: str | None = None
    current_step: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    step_started_at: str | None = None
    aborted_reason: str | None = None
    completed_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "started_at": self.started_at,
            "started_by": self.started_by,
            "reason": self.reason,
            "current_step": self.current_step,
            "completed_steps": list(self.completed_steps),
            "step_started_at": self.step_started_at,
            "aborted_reason": self.aborted_reason,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> BlackStartState:
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in data.items() if k in known}
        payload.setdefault("completed_steps", [])
        return cls(**payload)


@dataclass
class ReconciliationReport:
    """SDD 35.4. Reports work rather than silently rewriting other subsystems.

    The EMS clears *its own* stale beliefs (shed state, leases) and reports the
    points and commands that the owning subsystems must resolve. It does not
    rewrite the current-state cache or another subsystem's command records; a
    supervisory service inventing "actual" values is exactly the failure mode
    SDD 35.4 is warning about.
    """

    stale_points: list[str] = field(default_factory=list)
    expired_commands: list[str] = field(default_factory=list)
    unknown_loads: list[str] = field(default_factory=list)
    cleared_leases: list[str] = field(default_factory=list)
    at: dt.datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat() if self.at else None,
            "stale_points": list(self.stale_points),
            "expired_commands": list(self.expired_commands),
            "unknown_loads": list(self.unknown_loads),
            "cleared_leases": list(self.cleared_leases),
        }


class BlackStartCoordinator:
    """Prerequisites, sequence progression and post-recovery reconciliation."""

    def __init__(self, config: EmsConfig, *, state: BlackStartState | None = None) -> None:
        self.config = config
        self.state = state or BlackStartState()

    # -- prerequisites (SDD 35.2) ----------------------------------------
    def prerequisites(self, inputs: EmsInputs) -> list[Prerequisite]:
        soc = inputs.numeric("battery_soc_pct")
        temp = inputs.numeric("battery_temperature_max_c")
        contactor = inputs.text("bms_contactor_state")
        container_alarm = inputs.text("container_alarm_summary")
        rack_alarm = inputs.text("rack_alarm_summary")

        temp_ok: bool | None
        if temp is None:
            temp_ok = None
        else:
            temp_ok = self.config.blackstart_battery_temp_min_c <= temp <= self.config.blackstart_battery_temp_max_c

        alarms_clear: bool | None
        if container_alarm is None and rack_alarm is None:
            alarms_clear = None
        else:
            alarms_clear = all(
                value in {None, "none", "advisory"} for value in (container_alarm, rack_alarm)
            )

        return [
            Prerequisite(
                "control_power_protected",
                None,
                "BMS and inverter controls need protected DC power or an approved manual "
                "startup source; this cannot be verified from the platform and is an "
                "operator check",
            ),
            Prerequisite(
                "battery_within_limits",
                None if soc is None or temp_ok is None else (soc >= self.config.blackstart_min_soc_pct and temp_ok),
                f"SOC {soc if soc is not None else 'unknown'}% (minimum "
                f"{self.config.blackstart_min_soc_pct:g}%), cell temperature "
                f"{temp if temp is not None else 'unknown'} C",
            ),
            Prerequisite(
                "emergency_conditions_clear",
                alarms_clear,
                f"container alarm {container_alarm or 'unknown'}, rack alarm {rack_alarm or 'unknown'}; "
                "fire, smoke and emergency-stop status must be confirmed physically",
            ),
            Prerequisite(
                "critical_distribution_isolatable",
                None,
                "critical distribution must be isolable from noncritical branches; the "
                "branch/contactor schedule is an open field in the load schedule",
            ),
            Prerequisite(
                "local_controller_available",
                None,
                "at least one local controller must execute the sequence without the primary "
                "server rack; the secondary control node is a planned asset",
            ),
            Prerequisite(
                "battery_contactor_state_known",
                contactor is not None,
                f"BMS contactor state {contactor or 'unknown'}",
                independent_of_platform=False,
            ),
        ]

    def can_begin(self, inputs: EmsInputs) -> tuple[bool, list[Prerequisite]]:
        checks = self.prerequisites(inputs)
        # Every prerequisite that the platform *can* evaluate must pass. The
        # ones it cannot evaluate stay operator-attested, which is why begin()
        # requires an explicit operator attestation argument.
        machine_checkable = [c for c in checks if c.satisfied is not None]
        return all(not c.blocking for c in machine_checkable), checks

    # -- sequence --------------------------------------------------------
    def begin(
        self,
        inputs: EmsInputs,
        *,
        actor: str,
        reason: str,
        now: dt.datetime,
        prerequisites_attested: bool,
    ) -> BlackStartState:
        """Start the sequence. The operator attests the physical prerequisites."""
        if not reason:
            raise ValueError("A black start requires a reason")
        if not prerequisites_attested:
            raise PermissionError(
                "The independent prerequisites of SDD 35.2 must be attested by an operator; "
                "the platform cannot verify protected control power or physical isolation"
            )
        ok, checks = self.can_begin(inputs)
        if not ok:
            blocking = [c.name for c in checks if c.satisfied is False]
            raise PermissionError(f"Black-start prerequisites not satisfied: {', '.join(blocking)}")

        self.state = BlackStartState(
            active=True,
            started_at=now.isoformat(),
            started_by=actor,
            reason=reason,
            current_step=BLACK_START_STEPS[0].key,
            step_started_at=now.isoformat(),
            completed_steps=[],
        )
        logger.warning("Black start initiated by %s: %s", actor, reason)
        return self.state

    def advance(self, *, step: str | None = None, actor: str, now: dt.datetime, detail: str = "") -> BlackStartState:
        """Mark the current (or a named) step complete and move to the next."""
        if not self.state.active:
            raise RuntimeError("No black start is in progress")
        key = step or self.state.current_step
        if key not in STEP_BY_KEY:
            raise ValueError(f"Unknown black-start step: {key}")
        if key not in self.state.completed_steps:
            self.state.completed_steps.append(key)
        logger.info("Black-start step %s completed by %s %s", key, actor, detail)

        remaining = [s for s in BLACK_START_STEPS if s.key not in self.state.completed_steps]
        if remaining:
            self.state.current_step = remaining[0].key
            self.state.step_started_at = now.isoformat()
        else:
            self.state.current_step = None
            self.state.active = False
            self.state.completed_at = now.isoformat()
            logger.warning("Black start complete at %s", now.isoformat())
        return self.state

    def abort(self, *, actor: str, reason: str, now: dt.datetime) -> BlackStartState:
        self.state.active = False
        self.state.aborted_reason = f"{reason} (aborted by {actor} at {now.isoformat()})"
        self.state.current_step = None
        logger.warning("Black start aborted by %s: %s", actor, reason)
        return self.state

    def step_timed_out(self, *, now: dt.datetime) -> bool:
        started = self.state.step_started_at
        if not self.state.active or not started:
            return False
        elapsed = (now - _parse(started)).total_seconds()
        return elapsed >= self.config.blackstart_step_timeout_s

    def progress(self) -> dict[str, Any]:
        return {
            **self.state.as_dict(),
            "steps": [
                {
                    "key": step.key,
                    "order": step.order,
                    "description": step.description,
                    "manual": step.manual,
                    "owner": step.owner,
                    "complete": step.key in self.state.completed_steps,
                }
                for step in BLACK_START_STEPS
            ],
            "attended_load_restart": "prohibited after black start (SDD 35.3)",
        }

    # -- reconciliation (SDD 35.4) ----------------------------------------
    def reconcile(self, session: Session, *, now: dt.datetime, config: EmsConfig | None = None) -> ReconciliationReport:
        """Report what must be re-established, and drop the EMS's own stale beliefs."""
        config = config or self.config
        report = ReconciliationReport(at=now)
        cutoff = now - dt.timedelta(seconds=config.blackstart_reconcile_stale_s)

        for row in session.scalars(select(CurrentState)):
            ts = row.ts
            if ts is None:
                report.stale_points.append(row.point_id)
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            if ts < cutoff:
                report.stale_points.append(row.point_id)

        for command in session.scalars(
            select(Command).where(Command.state.not_in(tuple(TERMINAL_COMMAND_STATES)))
        ):
            expires_at = command.expires_at
            if expires_at is None:
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
            if expires_at <= now:
                report.expired_commands.append(command.command_id)

        # The EMS's own beliefs: after a collapse it does not know whether a
        # load it shed is off, so it stops claiming to.
        latest: dict[str, LoadShedAction] = {}
        for action in session.scalars(select(LoadShedAction).order_by(LoadShedAction.occurred_at)):
            latest[action.asset_id] = action
        for asset_id, action in latest.items():
            if action.action in {"shed", "reduce"} and action.outcome in {"requested", "confirmed", "applied"}:
                action.outcome = "unconfirmed"
                action.reason = f"{action.reason} | black-start reconciliation: physical state unknown"
                report.unknown_loads.append(asset_id)

        # A power budget granted before the collapse means nothing now.
        for lease in session.scalars(select(PowerBudgetLease).where(PowerBudgetLease.state == "active")):
            lease.state = "revoked"
            lease.revoked_at = now
            lease.revoked_reason = "black-start reconciliation: allocations do not survive a collapse"
            report.cleared_leases.append(lease.lease_id)

        session.flush()
        logger.warning(
            "Black-start reconciliation: %d stale points, %d expired commands, %d loads of unknown state",
            len(report.stale_points),
            len(report.expired_commands),
            len(report.unknown_loads),
        )
        return report


def _parse(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed
