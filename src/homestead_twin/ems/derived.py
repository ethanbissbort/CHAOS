"""Derived EMS values (SDD section 30.6, FR-101).

Every derived value is a :class:`DerivedValue` carrying its own validity flag,
the input keys it depended on and a plain-language ``basis``. SDD 30.6 requires
the algorithm to expose its calculation components so an operator can
understand why a state was selected, and SDD 38 requires the dashboard to show
measured values separately from calculated and forecast ones.

An assumption is never hidden. When usable energy has to be inferred from SOC
because the BMS does not publish ``energy_available_kwh``, the resulting value
carries ``assumptions=("usable capacity from planning basis ...",)`` and the
dashboard and the state-transition record both show it.

The PV forecast is pluggable. The default is an explicitly naive persistence
estimator: it is a placeholder for a real forecast service, and it says so in
every value it produces.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from homestead_twin.ems.config import EmsConfig
from homestead_twin.ems.inputs import INVERTERS, EmsInputs, load_input_key


@dataclass(frozen=True)
class DerivedValue:
    """One computed quantity with its provenance."""

    key: str
    value: float | None
    unit: str | None
    valid: bool
    basis: str
    inputs: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    kind: str = "calculated"  # calculated | forecast

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 4) if isinstance(self.value, float) else self.value,
            "unit": self.unit,
            "valid": self.valid,
            "kind": self.kind,
            "basis": self.basis,
            "inputs": list(self.inputs),
            "assumptions": list(self.assumptions),
        }

    @classmethod
    def invalid(cls, key: str, unit: str | None, basis: str, inputs: Iterable[str] = ()) -> DerivedValue:
        return cls(key=key, value=None, unit=unit, valid=False, basis=basis, inputs=tuple(inputs))


@dataclass
class DerivedEnergyState:
    """The full SDD 30.6 derived set for one evaluation."""

    at: dt.datetime
    values: dict[str, DerivedValue] = field(default_factory=dict)
    tier_load_kw: dict[int, float] = field(default_factory=dict)
    tier_load_valid: bool = False

    def add(self, value: DerivedValue) -> DerivedValue:
        self.values[value.key] = value
        return value

    def get(self, key: str) -> DerivedValue | None:
        return self.values.get(key)

    def value(self, key: str) -> float | None:
        derived = self.values.get(key)
        if derived is None or not derived.valid:
            return None
        return derived.value

    def valid(self, key: str) -> bool:
        derived = self.values.get(key)
        return bool(derived and derived.valid)

    #: Keys the snapshot blob uses for structure; a derived value may not
    #: shadow one of them.
    RESERVED_KEYS = ("at", "values", "tier_load_kw", "tier_load_valid", "generator", "black_start")

    def as_dict(self) -> dict[str, Any]:
        """Snapshot form.

        Carries the full provenance under ``values`` *and* a flat
        ``name -> scalar`` projection alongside it. The flat projection is what
        other subsystems (the overview/home-screen roll-up) read, so they do not
        have to know this module's internal shape; an invalid value is ``None``
        there rather than a number without its validity flag.
        """
        payload: dict[str, Any] = {
            "at": self.at.isoformat(),
            "values": {key: value.as_dict() for key, value in sorted(self.values.items())},
            "tier_load_kw": {str(tier): round(kw, 4) for tier, kw in sorted(self.tier_load_kw.items())},
            "tier_load_valid": self.tier_load_valid,
        }
        for key, value in sorted(self.values.items()):
            if key in self.RESERVED_KEYS:
                continue
            payload[key] = value.value if value.valid else None
        return payload

    def summary(self) -> dict[str, Any]:
        """Compact form for a state-transition record."""
        keys = (
            "site_load_kw",
            "critical_load_rolling_kw",
            "site_load_rolling_kw",
            "usable_energy_kwh",
            "energy_above_emergency_reserve_kwh",
            "reserve_pct",
            "autonomy_critical_h",
            "autonomy_current_h",
            "forecast_energy_margin_kwh",
            "surplus_power_kw",
            "available_discharge_kw",
            "thermal_derate_pct",
        )
        return {key: self.value(key) for key in keys if key in self.values}


# ---------------------------------------------------------------------------
# Rolling averages (SDD 30.6 ``*_rolling_kw``)
# ---------------------------------------------------------------------------


@dataclass
class RollingWindow:
    """Time-bounded rolling mean.

    Owned by the service and passed in, so tests drive it with explicit
    timestamps instead of sleeping.
    """

    window_s: int = 300
    samples: dict[str, deque] = field(default_factory=dict)

    def update(self, key: str, value: float | None, now: dt.datetime) -> None:
        if value is None:
            return
        series = self.samples.setdefault(key, deque())
        series.append((now, float(value)))
        self._trim(series, now)

    def _trim(self, series: deque, now: dt.datetime) -> None:
        cutoff = now - dt.timedelta(seconds=self.window_s)
        while series and series[0][0] < cutoff:
            series.popleft()

    def mean(self, key: str, now: dt.datetime) -> float | None:
        series = self.samples.get(key)
        if not series:
            return None
        self._trim(series, now)
        if not series:
            return None
        return sum(value for _, value in series) / len(series)

    def count(self, key: str) -> int:
        return len(self.samples.get(key, ()))


# ---------------------------------------------------------------------------
# PV forecast (SDD 30.5 "forecast and planning state")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForecastResult:
    kwh: float | None
    valid: bool
    method: str
    assumptions: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()


class PvForecast(Protocol):
    """Pluggable PV forecast.

    Replace with a real irradiance/weather-driven service when one exists. The
    interface stays this small on purpose: the EMS only needs expected energy
    over a horizon, plus an honest validity flag.
    """

    def forecast(
        self, horizon_h: float, *, now: dt.datetime, inputs: EmsInputs, config: EmsConfig
    ) -> ForecastResult: ...


class NaivePersistenceForecast:
    """The documented default: persistence, not a forecast.

    * Short horizon: assume the currently measured PV power holds for
      ``forecast_persistence_window_h`` and then stops, derated by
      ``forecast_persistence_derate``.
    * Day horizon: assume tomorrow resembles today, using the array's
      ``energy_today_kwh`` counter when it is valid; otherwise fall back to
      present power times ``forecast_daily_equivalent_full_load_h``.

    This is deliberately crude and every result says so. It exists so the state
    machine has a forecast-shaped input to reason about, not so the platform can
    pretend to predict weather. SDD 30.2 keeps such numbers as commissioning
    parameters.
    """

    method = "naive_persistence"

    def forecast(
        self, horizon_h: float, *, now: dt.datetime, inputs: EmsInputs, config: EmsConfig
    ) -> ForecastResult:
        pv_kw = inputs.numeric("pv_power_kw")
        if pv_kw is None:
            return ForecastResult(
                kwh=None,
                valid=False,
                method=self.method,
                assumptions=("PV power invalid; no forecast produced",),
                inputs=("pv_power_kw",),
            )

        if horizon_h <= config.forecast_short_horizon_h:
            hours = min(horizon_h, config.forecast_persistence_window_h)
            kwh = pv_kw * hours * config.forecast_persistence_derate
            return ForecastResult(
                kwh=kwh,
                valid=True,
                method=self.method,
                assumptions=(
                    f"present PV power held for {hours:g} h then zero",
                    f"derate {config.forecast_persistence_derate:g} applied",
                    "placeholder: no irradiance or weather model is used",
                ),
                inputs=("pv_power_kw",),
            )

        today_kwh = inputs.numeric("pv_energy_today_kwh")
        days = horizon_h / 24.0
        if today_kwh is not None:
            return ForecastResult(
                kwh=today_kwh * days,
                valid=True,
                method=self.method,
                assumptions=(
                    "next 24 h assumed to resemble today's measured PV energy",
                    "placeholder: no irradiance or weather model is used",
                ),
                inputs=("pv_energy_today_kwh",),
            )
        return ForecastResult(
            kwh=pv_kw * config.forecast_daily_equivalent_full_load_h * days,
            valid=True,
            method=self.method,
            assumptions=(
                f"equivalent full-load hours {config.forecast_daily_equivalent_full_load_h:g} "
                "applied to present PV power",
                "today's PV energy counter unavailable",
                "placeholder: no irradiance or weather model is used",
            ),
            inputs=("pv_power_kw",),
        )


@dataclass(frozen=True)
class LoadSnapshot:
    """The bit of a load record the derived layer needs."""

    asset_id: str
    tier: int
    estimated_power_kw: float | None = None
    shed: bool = False


# ---------------------------------------------------------------------------
# The computation
# ---------------------------------------------------------------------------


def compute_derived(
    inputs: EmsInputs,
    config: EmsConfig,
    *,
    now: dt.datetime,
    rolling: RollingWindow | None = None,
    forecast: PvForecast | None = None,
    loads: Iterable[LoadSnapshot] = (),
    scheduled_load_kwh: float = 0.0,
    deferrable_backlog_kwh: float | None = None,
) -> DerivedEnergyState:
    """Compute the SDD 30.6 derived value set."""
    derived = DerivedEnergyState(at=now)
    forecast = forecast or NaivePersistenceForecast()

    # -- Load -----------------------------------------------------------
    critical_kw = inputs.numeric("critical_load_kw")
    general_kw = inputs.numeric("general_load_kw")
    site_kw = None if critical_kw is None or general_kw is None else critical_kw + general_kw

    derived.add(
        DerivedValue(
            key="critical_load_kw",
            value=critical_kw,
            unit="kW",
            valid=critical_kw is not None,
            basis="critical distribution panel total",
            inputs=("critical_load_kw",),
        )
    )
    derived.add(
        DerivedValue(
            key="site_load_kw",
            value=site_kw,
            unit="kW",
            valid=site_kw is not None,
            basis="critical panel + general panel",
            inputs=("critical_load_kw", "general_load_kw"),
        )
    )

    if rolling is not None:
        rolling.update("critical_load_kw", critical_kw, now)
        rolling.update("site_load_kw", site_kw, now)
    critical_rolling = rolling.mean("critical_load_kw", now) if rolling else critical_kw
    site_rolling = rolling.mean("site_load_kw", now) if rolling else site_kw
    # With no history the rolling value is the instantaneous one.
    critical_rolling = critical_rolling if critical_rolling is not None else critical_kw
    site_rolling = site_rolling if site_rolling is not None else site_kw

    derived.add(
        DerivedValue(
            key="critical_load_rolling_kw",
            value=critical_rolling,
            unit="kW",
            valid=critical_rolling is not None,
            basis=f"rolling mean over {config.rolling_window_s} s of the critical panel total",
            inputs=("critical_load_kw",),
        )
    )
    derived.add(
        DerivedValue(
            key="site_load_rolling_kw",
            value=site_rolling,
            unit="kW",
            valid=site_rolling is not None,
            basis=f"rolling mean over {config.rolling_window_s} s of site load",
            inputs=("critical_load_kw", "general_load_kw"),
        )
    )

    # Tiered load from the load schedule and the per-load power points.
    tier_load: dict[int, float] = {}
    tier_valid = True
    load_list = list(loads)
    for load in load_list:
        measured = inputs.numeric(load_input_key(load.asset_id, "power_kw"))
        if measured is None:
            measured = load.estimated_power_kw
            if measured is None:
                tier_valid = False
                continue
            tier_valid = False  # an estimate contributed, so the total is not measured
        tier_load[load.tier] = tier_load.get(load.tier, 0.0) + measured
    derived.tier_load_kw = tier_load
    derived.tier_load_valid = tier_valid and bool(load_list)

    # -- Generation -----------------------------------------------------
    pv_kw = inputs.numeric("pv_power_kw")
    derived.add(
        DerivedValue(
            key="pv_power_kw",
            value=pv_kw,
            unit="kW",
            valid=pv_kw is not None,
            basis="PV array DC power",
            inputs=("pv_power_kw",),
        )
    )
    generator_kw = inputs.numeric("generator_power_kw")
    inverter_ac_total, inverter_used, _ = inputs.sum_valid(
        [f"inverter_{index:02d}_ac_kw" for index in range(1, len(INVERTERS) + 1)]
    )
    derived.add(
        DerivedValue(
            key="inverter_ac_output_kw",
            value=inverter_ac_total if inverter_used else None,
            unit="kW",
            valid=bool(inverter_used),
            basis=f"sum of {len(inverter_used)} reporting inverter positions",
            inputs=tuple(inverter_used),
        )
    )

    # Energy balance: generation minus demand (SDD FR-101).
    if pv_kw is not None and site_kw is not None:
        balance = pv_kw + (generator_kw or 0.0) - site_kw
        derived.add(
            DerivedValue(
                key="energy_balance_kw",
                value=balance,
                unit="kW",
                valid=True,
                basis="PV + generator - site load",
                inputs=("pv_power_kw", "generator_power_kw", "critical_load_kw", "general_load_kw"),
                assumptions=()
                if generator_kw is not None
                else ("generator output not observed; treated as 0 kW",),
            )
        )
    else:
        derived.add(
            DerivedValue.invalid(
                "energy_balance_kw",
                "kW",
                "requires PV power and site load",
                ("pv_power_kw", "critical_load_kw"),
            )
        )

    # -- Stored energy --------------------------------------------------
    soc_pct = inputs.numeric("battery_soc_pct")
    energy_available = inputs.numeric("battery_energy_available_kwh")
    usable_assumptions: tuple[str, ...] = ()
    usable_inputs: tuple[str, ...] = ()
    if energy_available is not None:
        usable_kwh: float | None = energy_available
        usable_basis = "BMS reported usable energy"
        usable_inputs = ("battery_energy_available_kwh",)
    elif soc_pct is not None:
        usable_kwh = config.planning_usable_capacity_kwh * soc_pct / 100.0
        usable_basis = "SOC applied to the planning usable capacity"
        usable_inputs = ("battery_soc_pct",)
        usable_assumptions = (
            f"usable capacity assumed {config.planning_usable_capacity_kwh:g} kWh from the SDD 30.2 "
            "planning basis; the 12 kW/40 kWh vs 45 kW/640 kWh conflict is unresolved",
            "BMS energy_available_kwh not observable",
        )
    else:
        usable_kwh = None
        usable_basis = "requires BMS usable energy or SOC"

    derived.add(
        DerivedValue(
            key="usable_energy_kwh",
            value=usable_kwh,
            unit="kWh",
            valid=usable_kwh is not None,
            basis=usable_basis,
            inputs=usable_inputs,
            assumptions=usable_assumptions,
        )
    )

    reserve_floor = config.emergency_reserve_kwh(config.planning_usable_capacity_kwh)
    derived.add(
        DerivedValue(
            key="emergency_reserve_kwh",
            value=reserve_floor,
            unit="kWh",
            valid=True,
            basis=f"{config.emergency_reserve_pct:g}% of the planning usable capacity",
            assumptions=("commissioning parameter, not a measurement",),
        )
    )

    above_reserve = None if usable_kwh is None else usable_kwh - reserve_floor
    derived.add(
        DerivedValue(
            key="energy_above_emergency_reserve_kwh",
            value=above_reserve,
            unit="kWh",
            valid=above_reserve is not None,
            basis="usable energy minus the emergency reserve floor",
            inputs=usable_inputs,
            assumptions=usable_assumptions,
        )
    )
    derived.add(
        DerivedValue(
            key="reserve_pct",
            value=soc_pct,
            unit="%",
            valid=soc_pct is not None,
            basis="battery state of charge",
            inputs=("battery_soc_pct",),
        )
    )

    # -- Autonomy -------------------------------------------------------
    critical_for_autonomy = critical_rolling
    autonomy_assumptions: tuple[str, ...] = ()
    if critical_for_autonomy is None:
        critical_for_autonomy = config.planning_critical_load_kw
        autonomy_assumptions = (
            f"critical panel not observable; planning critical baseload "
            f"{config.planning_critical_load_kw:g} kW assumed",
        )
    autonomy_critical = None
    if above_reserve is not None and critical_for_autonomy and critical_for_autonomy > 0:
        autonomy_critical = max(above_reserve, 0.0) / critical_for_autonomy
    derived.add(
        DerivedValue(
            key="autonomy_critical_h",
            value=autonomy_critical,
            unit="h",
            valid=autonomy_critical is not None and not autonomy_assumptions,
            basis="energy above the emergency reserve divided by the rolling critical load",
            inputs=usable_inputs + ("critical_load_kw",),
            assumptions=usable_assumptions + autonomy_assumptions,
        )
    )

    autonomy_current = None
    if above_reserve is not None and site_rolling and site_rolling > 0:
        autonomy_current = max(above_reserve, 0.0) / site_rolling
    derived.add(
        DerivedValue(
            key="autonomy_current_h",
            value=autonomy_current,
            unit="h",
            valid=autonomy_current is not None,
            basis="energy above the emergency reserve divided by the rolling site load",
            inputs=usable_inputs + ("critical_load_kw", "general_load_kw"),
            assumptions=usable_assumptions,
        )
    )

    # -- Forecast -------------------------------------------------------
    short = forecast.forecast(config.forecast_short_horizon_h, now=now, inputs=inputs, config=config)
    day = forecast.forecast(config.forecast_day_horizon_h, now=now, inputs=inputs, config=config)
    derived.add(
        DerivedValue(
            key="forecast_pv_next_6h_kwh",
            value=short.kwh,
            unit="kWh",
            valid=short.valid,
            basis=f"PV forecast ({short.method}) over {config.forecast_short_horizon_h:g} h",
            inputs=short.inputs,
            assumptions=short.assumptions,
            kind="forecast",
        )
    )
    derived.add(
        DerivedValue(
            key="forecast_pv_next_24h_kwh",
            value=day.kwh,
            unit="kWh",
            valid=day.valid,
            basis=f"PV forecast ({day.method}) over {config.forecast_day_horizon_h:g} h",
            inputs=day.inputs,
            assumptions=day.assumptions,
            kind="forecast",
        )
    )

    forecast_load = None
    if site_rolling is not None:
        forecast_load = site_rolling * config.forecast_day_horizon_h + scheduled_load_kwh
    derived.add(
        DerivedValue(
            key="forecast_load_next_24h_kwh",
            value=forecast_load,
            unit="kWh",
            valid=forecast_load is not None,
            basis="rolling site load projected over 24 h plus scheduled task energy",
            inputs=("critical_load_kw", "general_load_kw"),
            assumptions=("persistence projection; no occupancy or seasonal model",),
            kind="forecast",
        )
    )

    margin = None
    if above_reserve is not None and day.valid and day.kwh is not None and forecast_load is not None:
        margin = above_reserve + day.kwh - forecast_load
    derived.add(
        DerivedValue(
            key="forecast_energy_margin_kwh",
            value=margin,
            unit="kWh",
            valid=margin is not None,
            basis="energy above reserve + forecast PV - forecast load",
            inputs=usable_inputs + ("pv_power_kw", "critical_load_kw", "general_load_kw"),
            assumptions=usable_assumptions + day.assumptions,
            kind="forecast",
        )
    )

    # -- Power limits ---------------------------------------------------
    charge_limit = inputs.numeric("battery_charge_limit_kw")
    discharge_limit = inputs.numeric("battery_discharge_limit_kw")
    charge_permissive = inputs.flag("bms_charge_permissive")
    discharge_permissive = inputs.flag("bms_discharge_permissive")

    available_charge = None
    if charge_limit is not None and charge_permissive is not None:
        available_charge = charge_limit if charge_permissive else 0.0
    derived.add(
        DerivedValue(
            key="available_charge_kw",
            value=available_charge,
            unit="kW",
            valid=available_charge is not None,
            basis="BMS charge limit gated by the charge permissive",
            inputs=("battery_charge_limit_kw", "bms_charge_permissive"),
        )
    )

    available_discharge = None
    if discharge_limit is not None and discharge_permissive is not None:
        available_discharge = discharge_limit if discharge_permissive else 0.0
    derived.add(
        DerivedValue(
            key="available_discharge_kw",
            value=available_discharge,
            unit="kW",
            valid=available_discharge is not None,
            basis="BMS discharge limit gated by the discharge permissive",
            inputs=("battery_discharge_limit_kw", "bms_discharge_permissive"),
        )
    )

    # -- Surplus and curtailment ----------------------------------------
    surplus = None
    if pv_kw is not None and site_kw is not None and available_charge is not None:
        surplus = max(pv_kw - site_kw - available_charge, 0.0)
    derived.add(
        DerivedValue(
            key="surplus_power_kw",
            value=surplus,
            unit="kW",
            valid=surplus is not None,
            basis="PV power above site demand and safe battery charge acceptance",
            inputs=("pv_power_kw", "critical_load_kw", "general_load_kw", "battery_charge_limit_kw"),
        )
    )
    derived.add(
        DerivedValue(
            key="curtailment_risk_kw",
            value=surplus,
            unit="kW",
            valid=surplus is not None,
            basis="PV energy that would be unused without activating deferred loads",
            inputs=("pv_power_kw", "critical_load_kw", "battery_charge_limit_kw"),
        )
    )

    # -- Thermal derate --------------------------------------------------
    battery_temp = inputs.numeric("battery_temperature_max_c")
    container_temp = inputs.numeric("container_temperature_c")
    derate_pct = None
    derate_inputs: tuple[str, ...] = ()
    if battery_temp is not None or container_temp is not None:
        derate_pct = 0.0
        spans = []
        if battery_temp is not None:
            derate_inputs += ("battery_temperature_max_c",)
            spans.append(
                _linear_derate(battery_temp, config.battery_temp_warning_c, config.battery_temp_emergency_c)
            )
        if container_temp is not None:
            derate_inputs += ("container_temperature_c",)
            spans.append(
                _linear_derate(
                    container_temp, config.container_temp_warning_c, config.container_temp_emergency_c
                )
            )
        derate_pct = max(spans) if spans else 0.0
    derived.add(
        DerivedValue(
            key="thermal_derate_pct",
            value=derate_pct,
            unit="%",
            valid=derate_pct is not None,
            basis="linear derate between the warning and emergency temperature thresholds",
            inputs=derate_inputs,
            assumptions=("derate curve is a commissioning placeholder, not a manufacturer curve",),
        )
    )

    # -- Restoration headroom (SDD 30.6, 33.2) ---------------------------
    headroom = None
    if available_discharge is not None and site_kw is not None:
        supply = available_discharge + (pv_kw or 0.0) + (generator_kw or 0.0)
        headroom = max(supply - site_kw - config.restore_headroom_reserve_kw, 0.0)
    derived.add(
        DerivedValue(
            key="restoration_headroom_kw",
            value=headroom,
            unit="kW",
            valid=headroom is not None,
            basis="available discharge + PV + generator - site load - reserved headroom",
            inputs=("battery_discharge_limit_kw", "pv_power_kw", "critical_load_kw", "general_load_kw"),
        )
    )

    # -- Deferrable backlog ---------------------------------------------
    if deferrable_backlog_kwh is None:
        shed_estimate = sum(
            load.estimated_power_kw or 0.0 for load in load_list if load.shed and load.estimated_power_kw
        )
        derived.add(
            DerivedValue(
                key="deferrable_backlog_kwh",
                value=shed_estimate if shed_estimate else None,
                unit="kWh",
                valid=False,
                basis="no deferred-work queue is integrated yet",
                assumptions=("open item: deferrable work backlog requires the task scheduler from SDD 30.5",),
            )
        )
    else:
        derived.add(
            DerivedValue(
                key="deferrable_backlog_kwh",
                value=deferrable_backlog_kwh,
                unit="kWh",
                valid=True,
                basis="supplied by the deferred-work queue",
            )
        )

    return derived


def _linear_derate(temperature_c: float, warning_c: float, emergency_c: float) -> float:
    """0% below the warning threshold, 100% at the emergency threshold."""
    if emergency_c <= warning_c:
        return 0.0
    if temperature_c <= warning_c:
        return 0.0
    if temperature_c >= emergency_c:
        return 100.0
    return (temperature_c - warning_c) / (emergency_c - warning_c) * 100.0
