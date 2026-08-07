"""Four parallel hybrid inverters.

Models ``energy.inverter.power_container.01..04``: four ~10 kW hybrid units,
~40 kW continuous block capacity, 120/240 V split phase.

The block is the site's binding power constraint, so this component owns three
things the EMS depends on:

* **Capacity.** Faulted or stopped units remove their share immediately, which
  is what makes SDD 39 case ``EMS-T011`` ("EMS reduces load before inverter
  overload") and the ``inverter_fault`` scenario meaningful.
* **Thermal derate.** Capacity falls as the power container heats up, coupling
  the rack-cooling loss scenario to the electrical system.
* **Operating state.** ``state_operating`` per unit drives the black-start
  sequence, which starts one unit before the rest (SDD 35.3 step 4).

AC sign convention: ``power_ac_output_kw`` is positive when the unit delivers to
the AC bus and negative when it absorbs from it (charging from the generator).
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

INVERTER_ASSETS = tuple(f"energy.inverter.power_container.{i:02d}" for i in range(1, 5))

#: Operating states used for ``state_operating`` (SDD 26.5 leaves the enum
#: equipment-specific; these mirror a typical hybrid inverter).
STATE_OFF = "off"
STATE_STANDBY = "standby"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_FAULT = "fault"


@dataclass
class InverterConfig:
    asset_ids: tuple[str, ...] = INVERTER_ASSETS
    rated_power_kw: float = 10.0
    #: DC to AC conversion efficiency at nominal load.
    efficiency: float = 0.96
    nominal_voltage_v: float = 240.0
    nominal_frequency_hz: float = 60.0
    #: Thermal derate: full output up to ``derate_start_c``, falling linearly to
    #: ``derate_floor`` of nameplate at ``derate_end_c``.
    derate_start_c: float = 40.0
    derate_end_c: float = 60.0
    derate_floor: float = 0.55
    #: Seconds from a start request to ``running`` (soft-start / grid-form ramp).
    start_time_s: float = 20.0
    #: Units that begin the run stopped (black start begins with all stopped).
    initial_running: bool = True
    seed: int = 0


@dataclass
class InverterUnit:
    asset_id: str
    state: str = STATE_RUNNING
    fault_active: bool = False
    fault_code: str = ""
    ac_output_kw: float = 0.0
    dc_input_kw: float = 0.0
    runtime_h: float = 0.0
    starts: int = 0
    mode: str = "automatic"
    _start_timer_s: float = 0.0

    @property
    def online(self) -> bool:
        return self.state == STATE_RUNNING and not self.fault_active


class InverterFarm(Component):
    """The four-unit inverter block, treated as one dispatchable conversion stage."""

    name = "inverter"

    def __init__(self, catalog: PointCatalog, config: InverterConfig | None = None) -> None:
        super().__init__(catalog)
        self.config = config or InverterConfig()
        initial = STATE_RUNNING if self.config.initial_running else STATE_OFF
        self.units = [InverterUnit(asset_id=a, state=initial) for a in self.config.asset_ids]
        self.derate_factor = 1.0
        self.voltage_v = self.config.nominal_voltage_v
        self.frequency_hz = self.config.nominal_frequency_hz
        for unit in self.units:
            self.declare(
                unit.asset_id,
                [
                    "power_ac_output_kw",
                    "power_dc_input_kw",
                    "voltage_ac_v",
                    "frequency_hz",
                    "state_operating",
                    "fault_active",
                    "fault_code",
                    "availability_state",
                    "mode_actual",
                    "control_owner",
                    "runtime_total_h",
                    "starts_total",
                    "alarm_summary",
                ],
            )

    # -- capability ---------------------------------------------------------
    def thermal_derate(self, container_temperature_c: float) -> float:
        cfg = self.config
        if container_temperature_c <= cfg.derate_start_c:
            return 1.0
        if container_temperature_c >= cfg.derate_end_c:
            return cfg.derate_floor
        span = cfg.derate_end_c - cfg.derate_start_c
        fraction = (container_temperature_c - cfg.derate_start_c) / span
        return clamp(1.0 - fraction * (1.0 - cfg.derate_floor), cfg.derate_floor, 1.0)

    def online_units(self) -> list[InverterUnit]:
        return [unit for unit in self.units if unit.online]

    def available_capacity_kw(self, container_temperature_c: float) -> float:
        """Total AC capacity of the healthy, running units after thermal derate."""
        self.derate_factor = self.thermal_derate(container_temperature_c)
        return len(self.online_units()) * self.config.rated_power_kw * self.derate_factor

    # -- dispatch -----------------------------------------------------------
    def apply_dispatch(self, ac_net_kw: float, dc_net_kw: float) -> None:
        """Distribute the block's net AC and DC power evenly across online units.

        Parallel hybrid inverters share load; a real installation would show a
        few percent of imbalance, which is not modelled because nothing in the
        EMS depends on it.
        """
        online = self.online_units()
        count = len(online)
        for unit in self.units:
            if unit not in online:
                unit.ac_output_kw = 0.0
                unit.dc_input_kw = 0.0
        if count == 0:
            return
        for unit in online:
            unit.ac_output_kw = ac_net_kw / count
            unit.dc_input_kw = dc_net_kw / count

    # -- lifecycle / scenario control -----------------------------------------
    def inject_fault(self, index: int, code: str = "inverter_shutdown_fault") -> None:
        """Fault one unit. Its capacity disappears from the block immediately."""
        unit = self.units[index]
        unit.fault_active = True
        unit.fault_code = code
        unit.state = STATE_FAULT
        unit.ac_output_kw = 0.0
        unit.dc_input_kw = 0.0

    def clear_fault(self, index: int) -> None:
        unit = self.units[index]
        unit.fault_active = False
        unit.fault_code = ""
        unit.state = STATE_STANDBY

    def stop_all(self, reason: str = "blackout") -> None:
        """De-energize the block (blackout / emergency isolation)."""
        for unit in self.units:
            if not unit.fault_active:
                unit.state = STATE_OFF
            unit.ac_output_kw = 0.0
            unit.dc_input_kw = 0.0

    def start_unit(self, index: int) -> bool:
        """Begin the start ramp for one unit (SDD 35.3 step 4)."""
        unit = self.units[index]
        if unit.fault_active:
            return False
        if unit.state in (STATE_RUNNING, STATE_STARTING):
            return True
        unit.state = STATE_STARTING
        unit._start_timer_s = 0.0
        unit.starts += 1
        return True

    def running_count(self) -> int:
        return len(self.online_units())

    # -- commands ---------------------------------------------------------------
    def handle_command(self, command: CommandEnvelope) -> CommandOutcome | None:
        index = next(
            (i for i, unit in enumerate(self.units) if unit.asset_id == command.asset_id), None
        )
        if index is None:
            return None
        unit = self.units[index]
        name = command.command
        if name in ("start", "enabled_requested") and (
            name == "start" or bool(command.value) is True
        ):
            if unit.fault_active:
                return CommandOutcome(False, f"fault_active:{unit.fault_code}")
            self.start_unit(index)
            return CommandOutcome(True, "starting", completed=False, event="inverter_mode_change")
        if name == "stop" or (name == "enabled_requested" and bool(command.value) is False):
            unit.state = STATE_OFF
            unit.ac_output_kw = 0.0
            unit.dc_input_kw = 0.0
            return CommandOutcome(True, "stopped", event="inverter_mode_change")
        if name in ("mode_requested", "set_mode"):
            mode = str(command.value)
            if mode not in ("automatic", "manual", "maintenance", "off"):
                return CommandOutcome(False, f"unsupported_mode:{mode}")
            unit.mode = mode
            if mode == "off":
                unit.state = STATE_OFF
            return CommandOutcome(True, f"mode={mode}", event="inverter_mode_change")
        if name == "clear_fault":
            if unit.fault_code == "hardware_lockout":
                return CommandOutcome(False, "hardware_lockout_requires_local_reset")
            self.clear_fault(index)
            return CommandOutcome(True, "fault_cleared")
        return None

    # -- step -------------------------------------------------------------------
    def step(self, now: dt.datetime, dt_s: float, context: SiteContext) -> dict[str, Any]:
        cfg = self.config
        for unit in self.units:
            if unit.state == STATE_STARTING:
                unit._start_timer_s += dt_s
                if unit._start_timer_s >= cfg.start_time_s:
                    unit.state = STATE_RUNNING
            if unit.state == STATE_RUNNING:
                unit.runtime_h += dt_s / 3600.0

        online = self.online_units()
        energized = bool(online) and context.ac_bus_energized
        # Frequency and voltage sag slightly when the block is heavily loaded --
        # enough for a dashboard to show it, not a dynamic stability model.
        capacity = max(len(online) * cfg.rated_power_kw * self.derate_factor, 1e-6)
        loading = clamp(sum(u.ac_output_kw for u in online) / capacity, -1.0, 1.5)
        if energized:
            self.frequency_hz = cfg.nominal_frequency_hz - 0.25 * max(0.0, loading)
            self.voltage_v = cfg.nominal_voltage_v - 6.0 * max(0.0, loading)
        else:
            self.frequency_hz = 0.0
            self.voltage_v = 0.0

        context.inverter_online_count = len(online)
        context.ac_frequency_hz = self.frequency_hz
        context.ac_voltage_v = self.voltage_v

        out: dict[str, Any] = {}
        for unit in self.units:
            unit_energized = unit.online and energized
            if unit.fault_active:
                availability = "degraded"
                alarm = "critical" if unit.fault_code == "hardware_lockout" else "alarm"
            elif unit.state == STATE_OFF:
                availability = "online"
                alarm = "none"
            else:
                availability = "online"
                alarm = "none"
            self.emit(
                out,
                unit.asset_id,
                "power_ac_output_kw",
                round(unit.ac_output_kw if unit_energized else 0.0, 3),
            )
            self.emit(
                out,
                unit.asset_id,
                "power_dc_input_kw",
                round(unit.dc_input_kw if unit_energized else 0.0, 3),
            )
            self.emit(
                out,
                unit.asset_id,
                "voltage_ac_v",
                round(self.voltage_v if unit_energized else 0.0, 1),
            )
            self.emit(
                out,
                unit.asset_id,
                "frequency_hz",
                round(self.frequency_hz if unit_energized else 0.0, 2),
            )
            state = unit.state
            if state == STATE_RUNNING and not energized:
                state = STATE_STANDBY
            self.emit(out, unit.asset_id, "state_operating", state)
            self.emit(out, unit.asset_id, "fault_active", unit.fault_active)
            self.emit(out, unit.asset_id, "fault_code", unit.fault_code or "none")
            self.emit(out, unit.asset_id, "availability_state", availability)
            self.emit(out, unit.asset_id, "mode_actual", unit.mode)
            self.emit(out, unit.asset_id, "control_owner", "vendor")
            self.emit(out, unit.asset_id, "runtime_total_h", round(unit.runtime_h, 3))
            self.emit(out, unit.asset_id, "starts_total", unit.starts)
            self.emit(out, unit.asset_id, "alarm_summary", alarm)
        return out
