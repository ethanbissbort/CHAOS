"""Battery bank and BMS.

Models ``energy.battery_bank.power_container.01`` (800 kWh nominal / 640 kWh
usable planning target) together with ``energy.bms.power_container.01``.

Two properties matter more than realism here:

**Energy must be conserved.** SOC is integrated from terminal power with
explicit charge and discharge efficiencies, and every kWh that crosses the
terminals is counted, so a test can assert

    stored_end - stored_start == charged * eff_c - discharged / eff_d

**The BMS owns the limits.** Charge and discharge power limits taper at high and
low SOC and at temperature extremes, and ``charge_permissive`` /
``discharge_permissive`` can go false. SDD 30.8 requires the EMS to enter
``EMERGENCY`` when "BMS discharge is not permitted while AC loads remain", and
SDD 39 case ``EMS-T011`` requires a thermal discharge derate to be reproducible.
Supervisory control can never widen these limits (SDD 5.3).

Sign convention, used everywhere in the simulator: **positive power is charging**
(energy into the battery), negative is discharging.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from chaos.envelope import CommandEnvelope
from simulator.components.base import (
    CommandOutcome,
    Component,
    PointCatalog,
    SiteContext,
    approach,
    clamp,
)

BATTERY_ASSET = "energy.battery_bank.power_container.01"
BMS_ASSET = "energy.bms.power_container.01"


@dataclass
class BatteryConfig:
    """Battery and BMS parameters.

    Chemistry is ``TBD`` in the register; the thresholds below are LiFePO4-like
    and are commissioning parameters, not design commitments.
    """

    asset_id: str = BATTERY_ASSET
    bms_asset_id: str = BMS_ASSET

    nominal_capacity_kwh: float = 800.0
    usable_capacity_kwh: float = 640.0
    initial_soc_pct: float = 65.0
    soh_pct: float = 100.0

    charge_efficiency: float = 0.97
    discharge_efficiency: float = 0.97

    #: Nameplate power limits. Sized to the 40 kW inverter block, not to the
    #: cells: the conversion stage is the binding constraint on this site.
    max_charge_kw: float = 40.0
    max_discharge_kw: float = 40.0

    #: SOC tapers. Charge falls to zero at 100 %, discharge falls to zero at 0 %.
    charge_taper_start_pct: float = 90.0
    discharge_taper_start_pct: float = 12.0

    #: Temperature limits. Charging is locked out below freezing (plating risk).
    charge_lockout_below_c: float = 0.0
    charge_full_rate_above_c: float = 8.0
    discharge_lockout_below_c: float = -15.0
    discharge_full_rate_above_c: float = -5.0
    derate_start_above_c: float = 40.0
    lockout_above_c: float = 55.0

    #: Thermal model: kWh of heat per degree C of cell temperature rise, the
    #: conductance to container air, and the fraction of throughput lost as heat.
    thermal_capacity_kwh_per_c: float = 3.0
    thermal_conductance_kw_per_c: float = 0.45
    heat_fraction_of_throughput: float = 0.03
    initial_cell_temperature_c: float = 20.0

    #: Battery-zone HVAC (``energy.load.site.battery_hvac_01``) acts as a second
    #: thermal path, pulling cells toward ``conditioning_setpoint_c``. Shed it
    #: and the cells drift with container air -- which is exactly why the load
    #: bank refuses to shed it while cells are outside their safe band.
    conditioning_setpoint_c: float = 20.0
    conditioning_cop: float = 2.5
    #: Reference temperature error over which the HVAC's rated power acts.
    conditioning_reference_delta_c: float = 10.0

    #: Emergency floor as a fraction of usable capacity (SDD 30.6).
    emergency_reserve_fraction: float = 0.15


class BatteryBank(Component):
    """SOC integrator plus the BMS limit and permissive logic."""

    name = "battery"

    def __init__(self, catalog: PointCatalog, config: BatteryConfig | None = None) -> None:
        super().__init__(catalog)
        self.config = config or BatteryConfig()
        cfg = self.config
        self.stored_kwh = cfg.usable_capacity_kwh * cfg.initial_soc_pct / 100.0
        self.cell_temperature_c = cfg.initial_cell_temperature_c
        self.power_kw = 0.0
        self.fault_active = False
        self.fault_code = ""
        self.contactor_state = "closed"
        self.mode = "automatic"
        self.maintenance_lockout = False

        # Terminal energy counters -- the basis of the conservation assertion.
        self.energy_charged_kwh = 0.0
        self.energy_discharged_kwh = 0.0
        self.initial_stored_kwh = self.stored_kwh

        self.declare(
            cfg.asset_id,
            [
                "soc_pct",
                "soh_pct",
                "power_kw",
                "energy_available_kwh",
                "temperature_cell_max_c",
                "charge_limit_kw",
                "discharge_limit_kw",
                "charge_permissive",
                "discharge_permissive",
                "contactor_state",
                "fault_active",
                "fault_code",
                "availability_state",
                "state_operating",
                "alarm_summary",
            ],
        )
        self.declare(
            cfg.bms_asset_id,
            [
                "state_operating",
                "charge_permissive",
                "discharge_permissive",
                "contactor_state",
                "mode_actual",
                "control_owner",
                "availability_state",
                "fault_active",
                "interlock_permissive",
                "interlock_block_reason",
                "alarm_summary",
            ],
        )

    # -- derived state ------------------------------------------------------
    @property
    def soc_pct(self) -> float:
        return 100.0 * self.stored_kwh / self.config.usable_capacity_kwh

    @property
    def energy_above_emergency_reserve_kwh(self) -> float:
        floor = self.config.usable_capacity_kwh * self.config.emergency_reserve_fraction
        return self.stored_kwh - floor

    def conservation_error_kwh(self) -> float:
        """Residual of the energy balance. Tests assert this stays ~0."""
        expected = (
            self.initial_stored_kwh
            + self.energy_charged_kwh * self.config.charge_efficiency
            - self.energy_discharged_kwh / self.config.discharge_efficiency
        )
        return self.stored_kwh - expected

    # -- BMS limits ---------------------------------------------------------
    def _temperature_charge_factor(self) -> float:
        cfg = self.config
        temp = self.cell_temperature_c
        if temp <= cfg.charge_lockout_below_c:
            return 0.0
        if temp < cfg.charge_full_rate_above_c:
            span = cfg.charge_full_rate_above_c - cfg.charge_lockout_below_c
            return (temp - cfg.charge_lockout_below_c) / span
        return self._high_temperature_factor()

    def _temperature_discharge_factor(self) -> float:
        cfg = self.config
        temp = self.cell_temperature_c
        if temp <= cfg.discharge_lockout_below_c:
            return 0.0
        if temp < cfg.discharge_full_rate_above_c:
            span = cfg.discharge_full_rate_above_c - cfg.discharge_lockout_below_c
            return (temp - cfg.discharge_lockout_below_c) / span
        return self._high_temperature_factor()

    def _high_temperature_factor(self) -> float:
        cfg = self.config
        temp = self.cell_temperature_c
        if temp <= cfg.derate_start_above_c:
            return 1.0
        if temp >= cfg.lockout_above_c:
            return 0.0
        span = cfg.lockout_above_c - cfg.derate_start_above_c
        return clamp(1.0 - (temp - cfg.derate_start_above_c) / span, 0.0, 1.0)

    def limits(self) -> tuple[float, float]:
        """Return ``(charge_limit_kw, discharge_limit_kw)`` for this instant."""
        cfg = self.config
        soc = self.soc_pct

        # SOC taper: constant-current up to the taper point, then linear to zero.
        if soc >= 100.0:
            soc_charge = 0.0
        elif soc > cfg.charge_taper_start_pct:
            soc_charge = (100.0 - soc) / (100.0 - cfg.charge_taper_start_pct)
        else:
            soc_charge = 1.0
        if soc <= 0.0:
            soc_discharge = 0.0
        elif soc < cfg.discharge_taper_start_pct:
            soc_discharge = soc / cfg.discharge_taper_start_pct
        else:
            soc_discharge = 1.0

        charge = cfg.max_charge_kw * soc_charge * self._temperature_charge_factor()
        discharge = cfg.max_discharge_kw * soc_discharge * self._temperature_discharge_factor()
        if self.fault_active or self.contactor_state != "closed":
            charge = discharge = 0.0
        return max(0.0, charge), max(0.0, discharge)

    @property
    def charge_permissive(self) -> bool:
        return self.limits()[0] > 0.0

    @property
    def discharge_permissive(self) -> bool:
        return self.limits()[1] > 0.0

    def block_reason(self) -> str:
        """Primary reason the BMS is inhibiting operation, for the EMS to log."""
        if self.fault_active:
            return self.fault_code or "bms_fault"
        if self.contactor_state != "closed":
            return f"contactor_{self.contactor_state}"
        if self.cell_temperature_c <= self.config.charge_lockout_below_c:
            return "cell_temperature_low_charge_inhibited"
        if self.cell_temperature_c >= self.config.lockout_above_c:
            return "cell_temperature_high"
        if self.soc_pct <= 0.5:
            return "state_of_charge_exhausted"
        if self.maintenance_lockout:
            return "maintenance_lockout"
        return "none"

    # -- integration ----------------------------------------------------------
    def integrate(self, requested_kw: float, dt_s: float) -> float:
        """Apply terminal power for ``dt_s`` and return the power actually accepted.

        The limits are applied first, then the SOC end-stops. The accepted value
        is what the site uses for the rest of the balance, so the AC side and the
        DC side can never disagree.
        """
        cfg = self.config
        charge_limit, discharge_limit = self.limits()
        dt_h = dt_s / 3600.0

        if requested_kw >= 0:
            power = min(requested_kw, charge_limit)
            headroom_kwh = max(0.0, cfg.usable_capacity_kwh - self.stored_kwh)
            if dt_h > 0 and power * cfg.charge_efficiency * dt_h > headroom_kwh:
                power = headroom_kwh / (cfg.charge_efficiency * dt_h)
            self.stored_kwh += power * cfg.charge_efficiency * dt_h
            self.energy_charged_kwh += power * dt_h
        else:
            power = -min(-requested_kw, discharge_limit)
            drawable_kwh = max(0.0, self.stored_kwh)
            if dt_h > 0 and (-power) / cfg.discharge_efficiency * dt_h > drawable_kwh:
                power = -drawable_kwh * cfg.discharge_efficiency / dt_h
            self.stored_kwh += power / cfg.discharge_efficiency * dt_h
            self.energy_discharged_kwh += (-power) * dt_h

        # Guard against float drift at the end-stops.
        self.stored_kwh = clamp(self.stored_kwh, 0.0, cfg.usable_capacity_kwh)
        self.power_kw = power
        return power

    def update_thermal(self, dt_s: float, ambient_c: float, conditioning_kw: float = 0.0) -> None:
        """Advance cell temperature.

        Two thermal paths and one heat source:

        * conduction to container air (``thermal_conductance_kw_per_c``),
        * the battery-zone HVAC pulling toward its setpoint, sized by how much
          electrical power that load is actually drawing, and
        * self-heating proportional to throughput.

        Solving the two-source steady state keeps the model unconditionally
        stable at any step size, which matters when a day is simulated in
        60-second jumps.
        """
        cfg = self.config
        loss_kw = abs(self.power_kw) * cfg.heat_fraction_of_throughput
        g_envelope = cfg.thermal_conductance_kw_per_c
        g_hvac = (
            max(0.0, conditioning_kw) * cfg.conditioning_cop / max(cfg.conditioning_reference_delta_c, 1e-6)
        )
        g_total = g_envelope + g_hvac
        equilibrium = (g_envelope * ambient_c + g_hvac * cfg.conditioning_setpoint_c + loss_kw) / g_total
        tau_s = 3600.0 * cfg.thermal_capacity_kwh_per_c / g_total
        self.cell_temperature_c = approach(self.cell_temperature_c, equilibrium, dt_s, tau_s)

    # -- scenario / command control --------------------------------------------
    def inject_fault(self, code: str = "bms_internal_fault") -> None:
        self.fault_active = True
        self.fault_code = code

    def clear_fault(self) -> None:
        self.fault_active = False
        self.fault_code = ""

    def set_cell_temperature(self, temperature_c: float) -> None:
        """Force cell temperature (thermal derate scenarios, EMS-T011)."""
        self.cell_temperature_c = temperature_c

    def set_soc(self, soc_pct: float) -> None:
        """Jump SOC. Resets the conservation baseline so the invariant still holds."""
        self.stored_kwh = clamp(soc_pct, 0.0, 100.0) / 100.0 * self.config.usable_capacity_kwh
        self.initial_stored_kwh = self.stored_kwh
        self.energy_charged_kwh = 0.0
        self.energy_discharged_kwh = 0.0

    def open_contactor(self, reason: str = "commanded") -> None:
        self.contactor_state = "open"
        self.power_kw = 0.0

    def close_contactor(self) -> None:
        """Close through a precharge step, as the native BMS sequence would."""
        if self.fault_active:
            self.contactor_state = "fault"
            return
        self.contactor_state = "closed"

    def handle_command(self, command: CommandEnvelope) -> CommandOutcome | None:
        if command.asset_id not in (self.config.asset_id, self.config.bms_asset_id):
            return None
        name = command.command
        if name == "close_contactor":
            if self.fault_active:
                return CommandOutcome(False, f"bms_fault_active:{self.fault_code}")
            if self.cell_temperature_c >= self.config.lockout_above_c:
                return CommandOutcome(False, "cell_temperature_above_black_start_limit")
            self.contactor_state = "precharge"
            self.close_contactor()
            return CommandOutcome(True, "contactor_closed", event="bms_contactor_closed")
        if name == "open_contactor":
            self.open_contactor("commanded")
            return CommandOutcome(True, "contactor_open", event="bms_contactor_opened")
        if name in ("mode_requested", "set_mode"):
            mode = str(command.value)
            if mode not in ("automatic", "maintenance", "off", "manual"):
                return CommandOutcome(False, f"unsupported_mode:{mode}")
            self.mode = mode
            self.maintenance_lockout = mode == "maintenance"
            return CommandOutcome(True, f"mode={mode}")
        if name == "clear_fault":
            self.clear_fault()
            return CommandOutcome(True, "fault_cleared")
        return None

    # -- emission ---------------------------------------------------------------
    def step(self, now: dt.datetime, dt_s: float, context: SiteContext) -> dict[str, Any]:
        """Emit battery and BMS telemetry.

        Integration happens in :meth:`integrate` during the site energy balance;
        this method only reports state, so the DC and AC sides cannot diverge.
        """
        cfg = self.config
        charge_limit, discharge_limit = self.limits()
        soc = self.soc_pct
        context.battery_soc_pct = soc
        context.battery_power_kw = self.power_kw
        context.battery_energy_kwh = self.stored_kwh
        context.charge_limit_kw = charge_limit
        context.discharge_limit_kw = discharge_limit
        context.charge_permitted = charge_limit > 0.0
        context.discharge_permitted = discharge_limit > 0.0
        context.cell_temperature_c = self.cell_temperature_c
        context.contactor_state = self.contactor_state

        if self.fault_active:
            state = "fault"
        elif self.contactor_state != "closed":
            state = "standby"
        elif self.power_kw > 0.05:
            state = "charging"
        elif self.power_kw < -0.05:
            state = "discharging"
        else:
            state = "idle"

        if self.fault_active:
            alarm = "critical"
        elif not context.discharge_permitted or self.cell_temperature_c >= cfg.derate_start_above_c:
            alarm = "warning"
        else:
            alarm = "none"

        out: dict[str, Any] = {}
        asset = cfg.asset_id
        self.emit(out, asset, "soc_pct", round(soc, 2))
        self.emit(out, asset, "soh_pct", round(cfg.soh_pct, 2))
        self.emit(out, asset, "power_kw", round(self.power_kw, 3))
        self.emit(out, asset, "energy_available_kwh", round(self.stored_kwh, 3))
        self.emit(out, asset, "temperature_cell_max_c", round(self.cell_temperature_c, 2))
        self.emit(out, asset, "charge_limit_kw", round(charge_limit, 3))
        self.emit(out, asset, "discharge_limit_kw", round(discharge_limit, 3))
        self.emit(out, asset, "charge_permissive", charge_limit > 0.0)
        self.emit(out, asset, "discharge_permissive", discharge_limit > 0.0)
        self.emit(out, asset, "contactor_state", self.contactor_state)
        self.emit(out, asset, "fault_active", self.fault_active)
        self.emit(out, asset, "fault_code", self.fault_code or "none")
        self.emit(out, asset, "availability_state", "degraded" if self.fault_active else "online")
        self.emit(out, asset, "state_operating", state)
        self.emit(out, asset, "alarm_summary", alarm)

        bms = cfg.bms_asset_id
        self.emit(out, bms, "state_operating", "fault" if self.fault_active else "monitoring")
        self.emit(out, bms, "charge_permissive", charge_limit > 0.0)
        self.emit(out, bms, "discharge_permissive", discharge_limit > 0.0)
        self.emit(out, bms, "contactor_state", self.contactor_state)
        self.emit(out, bms, "mode_actual", self.mode)
        self.emit(out, bms, "control_owner", "vendor")
        self.emit(out, bms, "availability_state", "online")
        self.emit(out, bms, "fault_active", self.fault_active)
        self.emit(
            out,
            bms,
            "interlock_permissive",
            not self.fault_active and self.contactor_state == "closed",
        )
        self.emit(out, bms, "interlock_block_reason", self.block_reason())
        self.emit(out, bms, "alarm_summary", alarm)
        return out
