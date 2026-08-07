"""Backup generator and the site transfer assembly.

Models ``energy.generator.site.01`` and ``energy.ats.power_container.site_01``
following SDD section 34.

The important design point is that the EMS **requests** a start; it never
commands an engine. The native controller owns cranking, and it may refuse:

* start permissives (SDD 34.2) -- automatic mode, no maintenance lockout, fuel
  above the minimum, no shutdown-class fault, transfer interface available
* a bounded crank-attempt policy, after which the unit locks out and requires an
  explicit reset (SDD 34.7)
* minimum run time and cooldown, so the EMS cannot short-cycle it (SDD 34.4)

This is what makes both SDD 39 case ``EMS-T009`` (successful start, transfer,
charge, stop) and ``EMS-T008`` / ``EMS-T010`` (unavailable or failing generator)
reproducible without hardware.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from homestead_twin.envelope import CommandEnvelope
from simulator.components.base import (
    CommandOutcome,
    Component,
    PointCatalog,
    SiteContext,
    clamp,
)

GENERATOR_ASSET = "energy.generator.site.01"
ATS_ASSET = "energy.ats.power_container.site_01"

#: The register defines the ATS as source_a = inverter AC combiner,
#: source_b = generator. ``source_selected`` uses those names so the value can
#: be resolved against the register rather than guessed.
SOURCE_A = "source_a"
SOURCE_B = "source_b"
SOURCE_NONE = "none"

# Engine states.
STATE_OFF = "off"
STATE_CRANKING = "cranking"
STATE_WARMUP = "warmup"
STATE_RUNNING = "running"
STATE_COOLDOWN = "cooldown"
STATE_LOCKOUT = "lockout"


@dataclass
class GeneratorConfig:
    """Generator parameters.

    The register records ``fuel_type`` and ``rated_power_kw`` as TBD; the values
    below are simulator planning assumptions, not design commitments.
    """

    asset_id: str = GENERATOR_ASSET
    ats_asset_id: str = ATS_ASSET
    rated_power_kw: float = 20.0
    minimum_load_kw: float = 2.0

    #: Sequence timing (SDD 34.3).
    crank_time_s: float = 8.0
    warmup_time_s: float = 60.0
    transfer_time_s: float = 5.0
    minimum_run_time_s: float = 900.0
    cooldown_time_s: float = 180.0
    retry_delay_s: float = 30.0
    start_attempt_limit: int = 3

    #: Fuel model.
    fuel_capacity_l: float = 200.0
    initial_fuel_pct: float = 80.0
    fuel_burn_lph_at_rated: float = 7.5
    fuel_burn_lph_idle: float = 1.2
    minimum_start_fuel_pct: float = 10.0
    low_fuel_warning_pct: float = 25.0

    #: Scenario knobs.
    #: Number of consecutive start attempts that will fail before one succeeds.
    #: ``>= start_attempt_limit`` reproduces SDD 34.7 lockout behaviour.
    fail_start_attempts: int = 0
    maintenance_lockout: bool = False
    mode: str = "automatic"
    seed: int = 0


class Generator(Component):
    """Engine-generator with a native start controller, plus the site ATS."""

    name = "generator"

    def __init__(self, catalog: PointCatalog, config: GeneratorConfig | None = None) -> None:
        super().__init__(catalog)
        self.config = config or GeneratorConfig()
        cfg = self.config
        self.state = STATE_OFF
        self.mode = cfg.mode
        self.start_requested = False
        self.start_reason = ""
        self.output_kw = 0.0
        self.fuel_pct = cfg.initial_fuel_pct
        self.runtime_h = 0.0
        self.starts = 0
        self.failed_attempts = 0
        self.remaining_scripted_failures = cfg.fail_start_attempts
        self.start_failure_active = False
        self.fault_active = False
        self.fault_code = ""
        self.maintenance_lockout = cfg.maintenance_lockout
        self.last_command_id = ""
        self.last_command_result = ""
        self.pending_events: list[tuple[str, dict[str, Any]]] = []

        self._state_timer_s = 0.0
        self._run_timer_s = 0.0
        self._retry_timer_s = 0.0

        # ATS state.
        self.source_selected = SOURCE_A
        self.transfer_total = 0
        self._transfer_timer_s = 0.0
        self._transfer_target: str | None = None
        self.transfer_failed = False

        self.declare(
            cfg.asset_id,
            [
                "state_operating",
                "power_output_kw",
                "fuel_level_pct",
                "runtime_total_h",
                "starts_total",
                "start_failure_active",
                "availability_state",
                "mode_actual",
                "control_owner",
                "interlock_permissive",
                "interlock_block_reason",
                "fault_active",
                "fault_code",
                "alarm_summary",
                "command_last_id",
                "command_last_result",
            ],
        )
        self.declare(
            cfg.ats_asset_id,
            [
                "source_selected",
                "source_a_available",
                "source_b_available",
                "transfer_total",
                "state_operating",
                "availability_state",
                "fault_active",
                "alarm_summary",
            ],
        )

    # -- permissives ---------------------------------------------------------
    def start_permissive(self) -> tuple[bool, str]:
        """SDD 34.2. Returns ``(permitted, blocking_reason)``."""
        if self.state == STATE_LOCKOUT:
            return False, "start_lockout_requires_reset"
        if self.mode != "automatic":
            return False, f"mode_not_automatic:{self.mode}"
        if self.maintenance_lockout:
            return False, "maintenance_lockout"
        if self.fuel_pct < self.config.minimum_start_fuel_pct:
            return False, "fuel_below_minimum_start_threshold"
        if self.fault_active:
            return False, f"shutdown_fault:{self.fault_code}"
        if self.transfer_failed:
            return False, "transfer_interface_unavailable"
        return True, "none"

    @property
    def available(self) -> bool:
        return self.start_permissive()[0]

    @property
    def running(self) -> bool:
        return self.state in (STATE_WARMUP, STATE_RUNNING)

    @property
    def loaded(self) -> bool:
        """True only once the ATS has actually transferred to the generator."""
        return self.state == STATE_RUNNING and self.source_selected == SOURCE_B

    def minimum_run_satisfied(self) -> bool:
        return self._run_timer_s >= self.config.minimum_run_time_s

    # -- requests -------------------------------------------------------------
    def request_start(self, reason: str) -> CommandOutcome:
        """Assert ``generator_start_request`` (SDD 34.1). May be refused locally."""
        permitted, blocked = self.start_permissive()
        if not permitted:
            return CommandOutcome(False, blocked)
        if not self.start_requested:
            self.start_requested = True
            self.start_reason = reason
            self.pending_events.append(("generator_start_requested", {"reason": reason}))
        return CommandOutcome(True, "start_request_asserted", completed=False)

    def request_stop(self, reason: str = "ems_stop_criteria") -> CommandOutcome:
        """Remove the start request. Minimum run time still applies (SDD 34.4)."""
        self.start_requested = False
        self.start_reason = reason
        if self.running and not self.minimum_run_satisfied():
            return CommandOutcome(
                True,
                f"stop_deferred_minimum_run_time:{self.config.minimum_run_time_s:.0f}s",
                completed=False,
            )
        return CommandOutcome(True, "stop_request_accepted", completed=False)

    def reset(self) -> CommandOutcome:
        """Explicit operator reset after a start lockout (SDD 34.7)."""
        if self.state != STATE_LOCKOUT and not self.start_failure_active:
            return CommandOutcome(True, "nothing_to_reset")
        self.state = STATE_OFF
        self.failed_attempts = 0
        self.start_failure_active = False
        self._retry_timer_s = 0.0
        return CommandOutcome(True, "start_lockout_reset")

    # -- scenario control ------------------------------------------------------
    def set_fuel_pct(self, value: float) -> None:
        self.fuel_pct = clamp(value, 0.0, 100.0)

    def inject_fault(self, code: str = "generator_shutdown_fault") -> None:
        """Shutdown-class fault: trips the engine and blocks restart."""
        self.fault_active = True
        self.fault_code = code
        if self.running:
            self.pending_events.append(("generator_fault_shutdown", {"fault_code": code}))
            self._begin_cooldown(loaded_trip=True)

    def clear_fault(self) -> None:
        self.fault_active = False
        self.fault_code = ""

    # -- load ------------------------------------------------------------------
    def set_load(self, kw: float) -> None:
        """Set the electrical load the site is putting on the machine."""
        if not self.loaded:
            self.output_kw = 0.0
            return
        self.output_kw = clamp(kw, 0.0, self.config.rated_power_kw)

    # -- transfer --------------------------------------------------------------
    def _begin_transfer(self, target: str) -> None:
        if self.source_selected == target or self._transfer_target == target:
            return
        self._transfer_target = target
        self._transfer_timer_s = 0.0

    def _step_transfer(self, dt_s: float) -> None:
        if self._transfer_target is None:
            return
        self._transfer_timer_s += dt_s
        if self._transfer_timer_s >= self.config.transfer_time_s:
            self.source_selected = self._transfer_target
            self._transfer_target = None
            self.transfer_total += 1
            self.pending_events.append(
                ("source_transfer_completed", {"source_selected": self.source_selected})
            )

    def _begin_cooldown(self, loaded_trip: bool = False) -> None:
        self.output_kw = 0.0
        self._begin_transfer(SOURCE_A)
        self.state = STATE_COOLDOWN
        self._state_timer_s = 0.0
        self.pending_events.append(("generator_cooldown_started", {"tripped": loaded_trip}))

    # -- commands ---------------------------------------------------------------
    def handle_command(self, command: CommandEnvelope) -> CommandOutcome | None:
        if command.asset_id not in (self.config.asset_id, self.config.ats_asset_id):
            return None
        name = command.command
        outcome: CommandOutcome | None = None
        if name in ("generator_start_request", "start_request", "start"):
            requested = True if command.value is None else bool(command.value)
            outcome = (
                self.request_start(command.reason or "ems_request")
                if requested
                else self.request_stop(command.reason or "ems_release")
            )
        elif name in ("stop", "stop_request"):
            outcome = self.request_stop(command.reason or "ems_release")
        elif name == "reset":
            outcome = self.reset()
        elif name in ("mode_requested", "set_mode"):
            mode = str(command.value)
            if mode not in ("automatic", "manual", "maintenance", "off"):
                outcome = CommandOutcome(False, f"unsupported_mode:{mode}")
            else:
                self.mode = mode
                self.maintenance_lockout = mode == "maintenance"
                outcome = CommandOutcome(True, f"mode={mode}")
        elif name == "clear_fault":
            self.clear_fault()
            outcome = CommandOutcome(True, "fault_cleared")
        elif name == "transfer" and command.asset_id == self.config.ats_asset_id:
            target = str(command.value)
            if target not in (SOURCE_A, SOURCE_B):
                outcome = CommandOutcome(False, f"unknown_source:{target}")
            elif target == SOURCE_B and not self.running:
                outcome = CommandOutcome(False, "source_b_not_available")
            else:
                self._begin_transfer(target)
                outcome = CommandOutcome(True, f"transferring_to:{target}", completed=False)
        if outcome is not None:
            self.last_command_id = command.command_id
            self.last_command_result = "accepted" if outcome.accepted else "rejected"
        return outcome

    # -- engine state machine ------------------------------------------------------
    def _step_engine(self, dt_s: float) -> None:
        cfg = self.config
        self._state_timer_s += dt_s

        if self.state == STATE_OFF:
            if self.start_failure_active:
                self._retry_timer_s += dt_s
                if self._retry_timer_s < cfg.retry_delay_s:
                    return
            if not self.start_requested:
                return
            permitted, _reason = self.start_permissive()
            if not permitted:
                return
            self.state = STATE_CRANKING
            self._state_timer_s = 0.0
            self.starts += 1
            self.pending_events.append(("generator_crank_started", {"attempt": self.starts}))
            return

        if self.state == STATE_CRANKING:
            if self._state_timer_s < cfg.crank_time_s:
                return
            if self.remaining_scripted_failures > 0:
                # Native controller reports a failed start attempt.
                self.remaining_scripted_failures -= 1
                self.failed_attempts += 1
                self.start_failure_active = True
                self._retry_timer_s = 0.0
                self.state = STATE_OFF
                self._state_timer_s = 0.0
                self.pending_events.append(("generator_start_failed", {"attempt": self.failed_attempts}))
                if self.failed_attempts >= cfg.start_attempt_limit:
                    # Attempt policy exhausted: lock out and demand a reset.
                    self.state = STATE_LOCKOUT
                    self.start_requested = False
                    self.pending_events.append(
                        ("generator_start_lockout", {"attempts": self.failed_attempts})
                    )
                return
            self.start_failure_active = False
            self.failed_attempts = 0
            self.state = STATE_WARMUP
            self._state_timer_s = 0.0
            self._run_timer_s = 0.0
            self.pending_events.append(("generator_started", {"reason": self.start_reason}))
            return

        if self.state == STATE_WARMUP:
            self._run_timer_s += dt_s
            if self._state_timer_s >= cfg.warmup_time_s:
                self.state = STATE_RUNNING
                self._state_timer_s = 0.0
                self._begin_transfer(SOURCE_B)
            return

        if self.state == STATE_RUNNING:
            self._run_timer_s += dt_s
            if self.fuel_pct <= 0.0:
                self.pending_events.append(("generator_fuel_exhausted", {}))
                self.start_requested = False
                self._begin_cooldown(loaded_trip=True)
                return
            if not self.start_requested and self.minimum_run_satisfied():
                self._begin_cooldown()
            return

        if self.state == STATE_COOLDOWN:
            if self._state_timer_s >= cfg.cooldown_time_s:
                self.state = STATE_OFF
                self._state_timer_s = 0.0
                self._run_timer_s = 0.0
                self.pending_events.append(("generator_stopped", {}))
            return

        if self.state == STATE_LOCKOUT:
            self.start_requested = False
            return

    def _burn_fuel(self, dt_s: float) -> None:
        cfg = self.config
        if self.state not in (STATE_WARMUP, STATE_RUNNING, STATE_COOLDOWN):
            return
        loading = self.output_kw / cfg.rated_power_kw if cfg.rated_power_kw else 0.0
        lph = cfg.fuel_burn_lph_idle + loading * (cfg.fuel_burn_lph_at_rated - cfg.fuel_burn_lph_idle)
        litres = lph * dt_s / 3600.0
        self.fuel_pct = clamp(self.fuel_pct - 100.0 * litres / cfg.fuel_capacity_l, 0.0, 100.0)
        if self.state in (STATE_WARMUP, STATE_RUNNING):
            self.runtime_h += dt_s / 3600.0

    # -- step ---------------------------------------------------------------------
    def step(self, now: dt.datetime, dt_s: float, context: SiteContext) -> dict[str, Any]:
        self._step_engine(dt_s)
        self._step_transfer(dt_s)
        self._burn_fuel(dt_s)
        if not self.loaded:
            self.output_kw = 0.0

        permitted, block_reason = self.start_permissive()
        context.generator_running = self.running
        context.generator_output_kw = self.output_kw
        context.generator_available = permitted
        context.generator_start_request = self.start_requested
        context.transfer_source = self.source_selected

        if self.state == STATE_LOCKOUT or self.fault_active:
            alarm = "critical"
        elif self.start_failure_active or self.fuel_pct < self.config.low_fuel_warning_pct:
            alarm = "warning"
        else:
            alarm = "none"
        if self.state == STATE_LOCKOUT:
            availability = "offline"
        elif not permitted:
            availability = "degraded"
        else:
            availability = "online"

        out: dict[str, Any] = {}
        asset = self.config.asset_id
        self.emit(out, asset, "state_operating", self.state)
        self.emit(out, asset, "power_output_kw", round(self.output_kw, 3))
        self.emit(out, asset, "fuel_level_pct", round(self.fuel_pct, 2))
        self.emit(out, asset, "runtime_total_h", round(self.runtime_h, 4))
        self.emit(out, asset, "starts_total", self.starts)
        self.emit(out, asset, "start_failure_active", self.start_failure_active)
        self.emit(out, asset, "availability_state", availability)
        self.emit(out, asset, "mode_actual", self.mode)
        self.emit(out, asset, "control_owner", "vendor")
        self.emit(out, asset, "interlock_permissive", permitted)
        self.emit(out, asset, "interlock_block_reason", block_reason)
        self.emit(out, asset, "fault_active", self.fault_active)
        self.emit(out, asset, "fault_code", self.fault_code or "none")
        self.emit(out, asset, "alarm_summary", alarm)
        self.emit(out, asset, "command_last_id", self.last_command_id or "none")
        self.emit(out, asset, "command_last_result", self.last_command_result or "none")

        ats = self.config.ats_asset_id
        source_a_available = context.inverter_online_count > 0
        source_b_available = self.state in (STATE_RUNNING, STATE_WARMUP)
        if self._transfer_target is not None:
            ats_state = "transferring"
        elif self.source_selected == SOURCE_NONE:
            ats_state = "open"
        else:
            ats_state = "closed"
        self.emit(out, ats, "source_selected", self.source_selected)
        self.emit(out, ats, "source_a_available", source_a_available)
        self.emit(out, ats, "source_b_available", source_b_available)
        self.emit(out, ats, "transfer_total", self.transfer_total)
        self.emit(out, ats, "state_operating", ats_state)
        self.emit(out, ats, "availability_state", "degraded" if self.transfer_failed else "online")
        self.emit(out, ats, "fault_active", self.transfer_failed)
        self.emit(out, ats, "alarm_summary", "critical" if self.transfer_failed else "none")
        return out

    def drain_events(self) -> list[tuple[str, dict[str, Any]]]:
        """Hand queued sequence events to the site for publication."""
        events, self.pending_events = self.pending_events, []
        return events
