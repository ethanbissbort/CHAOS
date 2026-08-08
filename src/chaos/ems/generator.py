"""Generator coordination (SDD section 34, FR-104).

The platform **requests** a generator start through the equipment-native
interface and handles rejection. It does not crank an engine with generic
relays: preheat, crank, oil pressure, overspeed and shutdown all belong to the
generator controller (SDD 30.4, 34.3). This module decides *whether* to ask,
checks the permissives before asking, watches the result, keeps the machine
running long enough to be worth starting, and stops it in an orderly sequence.

Sequence states
---------------

``idle`` -> ``requested`` -> ``starting`` -> ``running`` -> ``stopping``
-> ``cooldown`` -> ``idle``, with ``failed`` and ``lockout`` for the SDD 34.7
failure paths.

Timers, all in :class:`~chaos.ems.config.EmsConfig`:

* ``generator_start_timeout_s`` -- how long the native controller gets to
  report a running state before the attempt counts as failed.
* ``generator_min_run_s`` -- minimum run time, so the EMS cannot short-cycle
  the machine (SDD 34.4).
* ``generator_max_continuous_run_s`` -- inspection interval (SDD 34.4).
* ``generator_cooldown_s`` -- cooldown before the request is removed (34.6).
* ``generator_restart_inhibit_s`` -- quiet period after a stop.
* ``generator_start_attempt_limit`` -- after this many failed sequences the EMS
  stops asking and requires an explicit reset (SDD 34.7).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from chaos.config import Settings
from chaos.ems import CommandPort, CommandRequest
from chaos.ems.config import GENERATOR_START_REQUEST_POINT, EmsConfig
from chaos.ems.derived import DerivedEnergyState
from chaos.ems.inputs import EmsInputs
from chaos.envelope import EventEnvelope
from chaos.mqtt import MessageBus
from chaos.topics import DEFAULT_BASE, alarm_topic

logger = logging.getLogger(__name__)

GENERATOR_SEQUENCE_STATES = (
    "idle",
    "requested",
    "starting",
    "running",
    "stopping",
    "cooldown",
    "failed",
    "lockout",
)


@dataclass
class GeneratorRuntime:
    """Everything the coordinator has to remember between ticks.

    Serialised onto ``EnergyStateSnapshot.derived['generator']`` so a service
    restart does not forget that the machine is running, and so the SDD 38
    dashboard can show status, fuel, runtime and the latest start cause.
    """

    sequence: str = "idle"
    requested_at: str | None = None
    running_since: str | None = None
    stop_requested_at: str | None = None
    stopped_at: str | None = None
    cooldown_until: str | None = None
    start_cause: str | None = None
    stop_cause: str | None = None
    expected_stop_condition: str | None = None
    attempts: int = 0
    fuel_at_start_pct: float | None = None
    runtime_at_start_h: float | None = None
    last_failure: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> GeneratorRuntime:
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class Permissive:
    name: str
    satisfied: bool | None
    detail: str

    @property
    def blocking(self) -> bool:
        """Unknown blocks too: an unverified permissive is not a permissive."""
        return self.satisfied is not True

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "satisfied": self.satisfied, "detail": self.detail}


@dataclass
class GeneratorDecision:
    sequence: str
    action: str = "none"  # none|start_requested|start_failed|stop_requested|stopped|lockout
    reason: str = ""
    start_reasons: list[str] = field(default_factory=list)
    permissives: list[Permissive] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    command_id: str | None = None
    supporting: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "action": self.action,
            "reason": self.reason,
            "start_reasons": list(self.start_reasons),
            "permissives": [p.as_dict() for p in self.permissives],
            "blocked_by": list(self.blocked_by),
            "command_id": self.command_id,
            "supporting": self.supporting,
        }


class GeneratorCoordinator:
    """Start requests, permissives, run control, stop and failure handling."""

    def __init__(
        self,
        config: EmsConfig,
        command_port: CommandPort,
        *,
        settings: Settings | None = None,
        bus: MessageBus | None = None,
        runtime: GeneratorRuntime | None = None,
    ) -> None:
        self.config = config
        self.command_port = command_port
        self.settings = settings
        self.bus = bus
        self.runtime = runtime or GeneratorRuntime()

    # -- observation -----------------------------------------------------
    def observed_running(self, inputs: EmsInputs) -> bool | None:
        state = inputs.text("generator_state")
        if state is None:
            return None
        if state in self.config.generator_running_states:
            return True
        if state in self.config.generator_stopped_states:
            return False
        return None

    @property
    def supporting(self) -> bool:
        """True when the site is on generator support (drives the state machine).

        Cooldown counts: the machine is still turning and the start request is
        still asserted until SDD 34.6 step 6 removes it.
        """
        return self.runtime.sequence in {"requested", "starting", "running", "stopping", "cooldown"}

    # -- start criteria (SDD 34.1) ---------------------------------------
    def start_reasons(self, inputs: EmsInputs, derived: DerivedEnergyState) -> list[str]:
        reasons: list[str] = []

        autonomy = derived.value("autonomy_critical_h")
        if autonomy is not None and autonomy < self.config.generator_start_autonomy_h:
            reasons.append(
                f"critical-load autonomy {autonomy:.1f} h is below the generator-start horizon "
                f"{self.config.generator_start_autonomy_h:.1f} h"
            )

        above = derived.value("energy_above_emergency_reserve_kwh")
        if above is not None and above < self.config.generator_start_margin_kwh:
            reasons.append(
                f"energy above the emergency reserve {above:.1f} kWh is below the generator-start "
                f"margin {self.config.generator_start_margin_kwh:.1f} kWh"
            )

        margin = derived.value("forecast_energy_margin_kwh")
        if margin is not None and margin < self.config.generator_start_forecast_margin_kwh:
            reasons.append(f"forecast energy margin {margin:.1f} kWh is materially negative")

        discharge = derived.value("available_discharge_kw")
        site_load = derived.value("site_load_kw")
        if (
            discharge is not None
            and site_load is not None
            and discharge < site_load * self.config.discharge_limit_margin
        ):
            reasons.append(
                f"battery discharge limit {discharge:.1f} kW cannot support the expected load "
                f"{site_load:.1f} kW"
            )

        pv_availability = inputs.text("pv_availability")
        balance = derived.value("energy_balance_kw")
        if pv_availability == "offline" and balance is not None and balance < 0:
            reasons.append("major PV outage with the site in deficit")

        return reasons

    # -- permissives (SDD 34.2) ------------------------------------------
    def permissives(self, inputs: EmsInputs, *, now: dt.datetime) -> list[Permissive]:
        available = inputs.flag("generator_available")
        fuel = inputs.numeric("generator_fuel_pct")
        start_failure = inputs.flag("generator_start_failure")
        state = inputs.text("generator_state")
        source_b = inputs.flag("ats_source_b_available")

        checks = [
            Permissive(
                "automatic_mode",
                available,
                "generator reports available / remote-enabled"
                if available
                else "generator_available is not true",
            ),
            Permissive(
                "no_maintenance_lockout",
                None if state is None else state not in {"maintenance", "lockout", "locked_out"},
                f"generator state {state or 'unknown'}",
            ),
            Permissive(
                "fuel_above_minimum",
                None if fuel is None else fuel >= self.config.generator_min_fuel_pct,
                f"fuel {fuel if fuel is not None else 'unknown'}% vs minimum "
                f"{self.config.generator_min_fuel_pct:g}%",
            ),
            Permissive(
                "no_shutdown_fault",
                None if start_failure is None else not start_failure,
                "start_failure_active" if start_failure else "no active start failure",
            ),
            Permissive(
                "transfer_interface_available",
                source_b,
                "transfer assembly reports the generator source available",
            ),
            Permissive(
                "attempts_remaining",
                self.runtime.attempts < self.config.generator_start_attempt_limit,
                f"{self.runtime.attempts} of {self.config.generator_start_attempt_limit} attempts used",
            ),
            Permissive(
                "restart_inhibit_elapsed",
                self._restart_inhibit_elapsed(now),
                f"quiet period after the last stop is {self.config.generator_restart_inhibit_s} s",
            ),
        ]
        return checks

    def _restart_inhibit_elapsed(self, now: dt.datetime) -> bool:
        stopped_at = _parse(self.runtime.stopped_at)
        if stopped_at is None:
            return True
        return (now - stopped_at).total_seconds() >= self.config.generator_restart_inhibit_s

    # -- stop criteria (SDD 34.5) ----------------------------------------
    def stop_reasons(self, inputs: EmsInputs, derived: DerivedEnergyState, *, now: dt.datetime) -> list[str]:
        running_since = _parse(self.runtime.running_since)
        if running_since is None:
            return []
        run_s = (now - running_since).total_seconds()
        if run_s < self.config.generator_min_run_s:
            return []

        reasons: list[str] = []
        soc = derived.value("reserve_pct")
        if soc is not None and soc >= self.config.generator_stop_soc_pct:
            reasons.append(
                f"battery reached the stop target {self.config.generator_stop_soc_pct:.0f}% (SOC {soc:.1f}%)"
            )
        margin = derived.value("forecast_energy_margin_kwh")
        if margin is not None and margin >= self.config.generator_stop_margin_kwh:
            reasons.append(f"forecast margin recovered to {margin:.1f} kWh")

        pv_kw = derived.value("pv_power_kw")
        site_load = derived.value("site_load_kw")
        if pv_kw is not None and site_load is not None and pv_kw > site_load:
            reasons.append(f"PV {pv_kw:.1f} kW now exceeds site load {site_load:.1f} kW")

        if run_s >= self.config.generator_max_continuous_run_s:
            reasons.append(
                f"maximum continuous run {self.config.generator_max_continuous_run_s} s reached; "
                "inspection interval"
            )
        return reasons

    # -- the tick --------------------------------------------------------
    def evaluate(
        self,
        inputs: EmsInputs,
        derived: DerivedEnergyState,
        *,
        energy_state: str,
        now: dt.datetime,
        operator_request: bool = False,
    ) -> GeneratorDecision:
        """One pass of the SDD 34 narrative."""
        sequence = self.runtime.sequence
        running = self.observed_running(inputs)

        if sequence == "lockout":
            return GeneratorDecision(
                sequence=sequence,
                reason=(
                    f"generator locked out after {self.runtime.attempts} failed start sequences; "
                    "explicit reset required (SDD 34.7)"
                ),
                supporting=False,
            )

        if sequence == "cooldown":
            return self._cooldown(inputs, now=now)

        if sequence in {"requested", "starting"}:
            return self._await_start(inputs, now=now, running=running)

        if sequence in {"running", "stopping"}:
            return self._run_control(inputs, derived, energy_state=energy_state, now=now, running=running)

        # idle ------------------------------------------------------------
        if running is True:
            # The generator is running without an EMS request: adopt it rather
            # than fight it, and record why.
            self.runtime.sequence = "running"
            self.runtime.running_since = _iso(now)
            self.runtime.start_cause = "observed running without an EMS request"
            return GeneratorDecision(
                sequence="running",
                action="none",
                reason="generator observed running; EMS adopted the run without issuing a request",
                supporting=True,
            )

        reasons = self.start_reasons(inputs, derived)
        if operator_request:
            reasons.append("operator requested generator support for a planned load")
        if not reasons:
            return GeneratorDecision(sequence="idle", reason="no start criterion satisfied")

        permissives = self.permissives(inputs, now=now)
        blocked = [p.name for p in permissives if p.blocking]
        if blocked:
            # SDD 39 EMS-T008: no uncontrolled repeated start request.
            self._alarm(
                "generator_start_blocked",
                "major" if energy_state == "CRITICAL_RESERVE" else "warning",
                {
                    "blocked_by": blocked,
                    "start_reasons": reasons,
                    "energy_state": energy_state,
                    "permissives": [p.as_dict() for p in permissives],
                },
            )
            return GeneratorDecision(
                sequence="idle",
                action="none",
                reason="start criteria met but permissives are not satisfied",
                start_reasons=reasons,
                permissives=permissives,
                blocked_by=blocked,
            )

        return self._request_start(inputs, reasons, permissives, now=now)

    # -- sequence steps ---------------------------------------------------
    def _request_start(
        self,
        inputs: EmsInputs,
        reasons: list[str],
        permissives: list[Permissive],
        *,
        now: dt.datetime,
    ) -> GeneratorDecision:
        """SDD 34.3 step 1: assert generator_start_request and let the native
        controller do preheat, crank and start."""
        cause = "; ".join(reasons)
        outcome = self.command_port.issue(
            CommandRequest(
                asset_id=self.config.generator_asset_id,
                command=GENERATOR_START_REQUEST_POINT,
                value=True,
                reason=f"EMS generator start request: {cause}",
                issued_by=self.config.actor,
                priority=5,
                correlation_id="ems-generator-start",
            )
        )
        self.runtime.attempts += 1
        if not outcome.accepted:
            detail = outcome.detail or "start request not accepted"
            self.runtime.last_failure = detail
            self.runtime.sequence = (
                "failed" if self.runtime.attempts < self.config.generator_start_attempt_limit else "lockout"
            )
            self._alarm(
                "generator_start_failed",
                "critical",
                {"stage": "request", "detail": detail, "attempts": self.runtime.attempts},
            )
            return GeneratorDecision(
                sequence=self.runtime.sequence,
                action="start_failed",
                reason=f"generator start request rejected: {detail}",
                start_reasons=reasons,
                permissives=permissives,
            )

        self.runtime.sequence = "requested"
        self.runtime.requested_at = _iso(now)
        self.runtime.start_cause = cause
        self.runtime.fuel_at_start_pct = inputs.numeric("generator_fuel_pct")
        self.runtime.runtime_at_start_h = inputs.numeric("generator_runtime_h")
        self.runtime.expected_stop_condition = (
            f"SOC >= {self.config.generator_stop_soc_pct:.0f}% or forecast margin >= "
            f"{self.config.generator_stop_margin_kwh:.0f} kWh, after the "
            f"{self.config.generator_min_run_s} s minimum run"
        )
        self._event("generator_start_requested", {"cause": cause, "attempt": self.runtime.attempts})
        return GeneratorDecision(
            sequence="requested",
            action="start_requested",
            reason=cause,
            start_reasons=reasons,
            permissives=permissives,
            command_id=outcome.command_id,
            supporting=True,
        )

    def _await_start(self, inputs: EmsInputs, *, now: dt.datetime, running: bool | None) -> GeneratorDecision:
        """SDD 34.3 steps 3-5: wait for a valid running state."""
        if running is True:
            self.runtime.sequence = "running"
            self.runtime.running_since = _iso(now)
            self._event(
                "generator_running",
                {"cause": self.runtime.start_cause, "expected_stop": self.runtime.expected_stop_condition},
            )
            return GeneratorDecision(
                sequence="running",
                action="none",
                reason="generator reported running; native transfer sequence owns synchronisation",
                supporting=True,
            )

        if inputs.flag("generator_start_failure") is True:
            return self._start_failed("generator controller reported a start failure", now=now)

        requested_at = _parse(self.runtime.requested_at)
        if requested_at is not None:
            waited = (now - requested_at).total_seconds()
            if waited >= self.config.generator_start_timeout_s:
                return self._start_failed(
                    f"no running state within {self.config.generator_start_timeout_s} s", now=now
                )
            self.runtime.sequence = "starting"
            return GeneratorDecision(
                sequence="starting",
                reason=f"waiting for the native start sequence ({waited:.0f} s elapsed)",
                supporting=True,
            )
        self.runtime.sequence = "starting"
        return GeneratorDecision(
            sequence="starting", reason="waiting for the native start sequence", supporting=True
        )

    def _start_failed(self, detail: str, *, now: dt.datetime) -> GeneratorDecision:
        """SDD 34.7: stay in deep conservation, alarm, and do not re-crank."""
        self.runtime.last_failure = detail
        self.runtime.stopped_at = _iso(now)
        if self.runtime.attempts >= self.config.generator_start_attempt_limit:
            self.runtime.sequence = "lockout"
            reason = (
                f"{detail}; attempt limit {self.config.generator_start_attempt_limit} reached, "
                "explicit operator reset required"
            )
        else:
            self.runtime.sequence = "failed"
            reason = detail
        # Withdraw the request so the native controller is not left asserted.
        self.command_port.issue(
            CommandRequest(
                asset_id=self.config.generator_asset_id,
                command=GENERATOR_START_REQUEST_POINT,
                value=False,
                reason=f"withdrawing start request after failure: {detail}",
                issued_by=self.config.actor,
                priority=5,
                correlation_id="ems-generator-start",
            )
        )
        self._alarm(
            "generator_start_failed",
            "critical",
            {"stage": "start_sequence", "detail": detail, "attempts": self.runtime.attempts},
        )
        return GeneratorDecision(
            sequence=self.runtime.sequence,
            action="lockout" if self.runtime.sequence == "lockout" else "start_failed",
            reason=reason,
        )

    def _run_control(
        self,
        inputs: EmsInputs,
        derived: DerivedEnergyState,
        *,
        energy_state: str,
        now: dt.datetime,
        running: bool | None,
    ) -> GeneratorDecision:
        """SDD 34.4 run control and 34.5/34.6 stop."""
        if running is False:
            # Lost the machine mid-run (SDD 34.7): transfer back if possible,
            # shed load, critical alarm with sequence context.
            detail = f"generator stopped unexpectedly during {self.runtime.sequence}"
            self.runtime.sequence = "failed"
            self.runtime.stopped_at = _iso(now)
            self.runtime.running_since = None
            self._alarm(
                "generator_start_failed",
                "critical",
                {"stage": "run", "detail": detail, "start_cause": self.runtime.start_cause},
            )
            return GeneratorDecision(sequence="failed", action="start_failed", reason=detail)

        if self.runtime.sequence == "stopping":
            return self._begin_cooldown(now=now, reason=self.runtime.stop_cause or "stop sequence complete")

        run_s = 0.0
        running_since = _parse(self.runtime.running_since)
        if running_since is not None:
            run_s = (now - running_since).total_seconds()

        reasons = self.stop_reasons(inputs, derived, now=now)
        if not reasons:
            remaining = max(self.config.generator_min_run_s - run_s, 0.0)
            return GeneratorDecision(
                sequence="running",
                reason=(
                    f"running {run_s:.0f} s; minimum run has {remaining:.0f} s to go"
                    if remaining > 0
                    else "running; no stop criterion satisfied yet"
                ),
                supporting=True,
            )

        # SDD 34.6 steps 1-3 are executed by removing discretionary budgets and
        # letting the inverter/ATS perform the native transfer; the EMS only
        # removes its request after cooldown.
        self.runtime.sequence = "stopping"
        self.runtime.stop_requested_at = _iso(now)
        self.runtime.stop_cause = "; ".join(reasons)
        self._event("generator_stop_requested", {"cause": self.runtime.stop_cause, "run_s": run_s})
        return GeneratorDecision(
            sequence="stopping",
            action="stop_requested",
            reason=self.runtime.stop_cause,
            supporting=True,
        )

    def _begin_cooldown(self, *, now: dt.datetime, reason: str) -> GeneratorDecision:
        self.runtime.sequence = "cooldown"
        self.runtime.cooldown_until = _iso(now + dt.timedelta(seconds=self.config.generator_cooldown_s))
        return GeneratorDecision(
            sequence="cooldown",
            action="none",
            reason=f"{reason}; running {self.config.generator_cooldown_s} s cooldown",
            supporting=True,
        )

    def _cooldown(self, inputs: EmsInputs, *, now: dt.datetime) -> GeneratorDecision:
        until = _parse(self.runtime.cooldown_until)
        if until is not None and now < until:
            remaining = (until - now).total_seconds()
            return GeneratorDecision(
                sequence="cooldown",
                reason=f"cooldown has {remaining:.0f} s remaining",
                supporting=True,
            )
        # SDD 34.6 step 6: remove the start request, then confirm stopped.
        outcome = self.command_port.issue(
            CommandRequest(
                asset_id=self.config.generator_asset_id,
                command=GENERATOR_START_REQUEST_POINT,
                value=False,
                reason=f"cooldown complete: {self.runtime.stop_cause or 'stop criteria satisfied'}",
                issued_by=self.config.actor,
                priority=5,
                correlation_id="ems-generator-stop",
            )
        )
        self.runtime.sequence = "idle"
        self.runtime.stopped_at = _iso(now)
        self.runtime.running_since = None
        self.runtime.cooldown_until = None
        self.runtime.attempts = 0
        self._event(
            "generator_stopped",
            {
                "cause": self.runtime.stop_cause,
                "runtime_total_h": inputs.numeric("generator_runtime_h"),
                "fuel_level_pct": inputs.numeric("generator_fuel_pct"),
            },
        )
        return GeneratorDecision(
            sequence="idle",
            action="stopped",
            reason="cooldown complete; start request removed",
            command_id=outcome.command_id,
        )

    # -- operator actions -------------------------------------------------
    def reset(self, *, actor: str, reason: str, now: dt.datetime) -> GeneratorRuntime:
        """Clear a failed/locked-out generator sequence (SDD 34.7)."""
        if not reason:
            raise ValueError("A generator reset requires a reason")
        logger.warning("Generator sequence reset by %s: %s", actor, reason)
        self.runtime.sequence = "idle"
        self.runtime.attempts = 0
        self.runtime.last_failure = None
        self.runtime.stopped_at = _iso(now)
        self._event("generator_reset", {"actor": actor, "reason": reason})
        return self.runtime

    # -- publication -------------------------------------------------------
    def _event(self, name: str, detail: dict[str, Any]) -> None:
        logger.info("Generator event %s: %s", name, detail)
        if self.bus is None or self.settings is None:
            return
        base = self.settings.mqtt_base_topic or DEFAULT_BASE
        envelope = EventEnvelope(
            asset_id=self.config.generator_asset_id, event=name, detail=detail, source="ems"
        )
        try:
            self.bus.publish(
                alarm_topic(self.config.generator_asset_id, name, base=base), envelope.to_payload(), qos=1
            )
        except Exception:  # pragma: no cover
            logger.exception("Failed to publish generator event %s", name)

    def _alarm(self, name: str, severity: str, detail: dict[str, Any]) -> None:
        self._event(name, {"severity": severity, **detail})

    # -- dashboard ---------------------------------------------------------
    def status(self, inputs: EmsInputs | None = None) -> dict[str, Any]:
        status = self.runtime.as_dict()
        status["supporting"] = self.supporting
        if inputs is not None:
            status["observed_state"] = inputs.text("generator_state")
            status["fuel_level_pct"] = inputs.numeric("generator_fuel_pct")
            status["runtime_total_h"] = inputs.numeric("generator_runtime_h")
            status["available"] = inputs.flag("generator_available")
            status["power_output_kw"] = inputs.numeric("generator_power_kw")
        return status


def _iso(value: dt.datetime) -> str:
    return value.isoformat()


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed
