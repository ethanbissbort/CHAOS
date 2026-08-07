"""EMS input gathering (SDD section 30.5).

Every value the state machine uses is read from the current-state cache by
canonical point ID and arrives wrapped in an :class:`InputReading` that records
its quality, its age and whether the EMS may act on it.

Two rules are enforced here and nowhere else:

1. **A missing or stale required input is visible, never filled in.** SDD 30.7
   requires ``DEGRADED_SENSOR`` when observability is impaired, and SDD 39
   ``EMS-T003`` requires that a stale SOC does not let the EMS assume a healthy
   reserve. :meth:`EmsInputs.numeric` returns ``None`` for an invalid reading;
   there is no default-substitution path.
2. **Quality is part of the value.** Only the qualities listed in
   ``EmsConfig.valid_qualities`` are acted on. ``substituted``, ``uncertain``,
   ``bad``, ``stale`` and ``maintenance`` readings are kept for the snapshot and
   the dashboard but are not valid inputs.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin.ems.config import EmsConfig
from homestead_twin.models.telemetry import CurrentState

# --- Canonical asset IDs used by the EMS ----------------------------------
# These come from data/homestead_asset_register.yaml. They are functional
# positions, so equipment can be replaced without touching this module.

BATTERY_BANK = "energy.battery_bank.power_container.01"
BMS = "energy.bms.power_container.01"
PV_ARRAY = "energy.pv_array.agrivoltaic_field.01"
INVERTERS = (
    "energy.inverter.power_container.01",
    "energy.inverter.power_container.02",
    "energy.inverter.power_container.03",
    "energy.inverter.power_container.04",
)
CRITICAL_PANEL = "energy.panel.power_container.critical_01"
GENERAL_PANEL = "energy.panel.power_container.general_01"
GENERATOR = "energy.generator.site.01"
SITE_ATS = "energy.ats.power_container.site_01"
RACK_UPS = "energy.ups.rack_01.01"
CONTAINER_MONITOR = "it.environmental_monitor.power_container.netbotz_500_01"
RACK_MONITOR = "it.environmental_monitor.rack_01.nbrk0550_01"


@dataclass(frozen=True)
class InputSpec:
    """One EMS input: where it comes from and how much the EMS depends on it."""

    key: str
    asset_id: str
    point_name: str
    #: A required input that is invalid drives the EMS to DEGRADED_SENSOR.
    required: bool = False
    #: Override for the staleness limit; otherwise the point's own
    #: ``stale_after_s`` and then ``EmsConfig.input_max_age_s`` apply.
    max_age_s: int | None = None
    category: str = "electrical"
    description: str = ""

    @property
    def point_id(self) -> str:
        return f"{self.asset_id}/{self.point_name}"


def _spec(key, asset, point, required=False, max_age_s=None, category="electrical", description=""):
    return InputSpec(key, asset, point, required, max_age_s, category, description)


#: SDD 30.5 "Electrical state". The ``required`` flags mark the inputs without
#: which the EMS cannot honestly claim to know the site's energy position.
ELECTRICAL_INPUTS: tuple[InputSpec, ...] = (
    _spec("battery_soc_pct", BATTERY_BANK, "soc_pct", required=True, description="Battery state of charge"),
    _spec("battery_soh_pct", BATTERY_BANK, "soh_pct", description="Battery state of health"),
    _spec("battery_power_kw", BATTERY_BANK, "power_kw", description="Battery power, discharge positive"),
    _spec("battery_energy_available_kwh", BATTERY_BANK, "energy_available_kwh",
          description="Usable energy reported by the BMS"),
    _spec("battery_temperature_max_c", BATTERY_BANK, "temperature_cell_max_c", required=True,
          description="Hottest reported cell"),
    _spec("battery_charge_limit_kw", BATTERY_BANK, "charge_limit_kw", required=True,
          description="BMS charge power limit"),
    _spec("battery_discharge_limit_kw", BATTERY_BANK, "discharge_limit_kw", required=True,
          description="BMS discharge power limit"),
    _spec("bms_state", BMS, "state_operating", description="BMS operating state"),
    _spec("bms_charge_permissive", BMS, "charge_permissive", required=True),
    _spec("bms_discharge_permissive", BMS, "discharge_permissive", required=True),
    _spec("bms_contactor_state", BMS, "contactor_state"),
    _spec("pv_power_kw", PV_ARRAY, "power_dc_kw", required=True, description="PV DC power"),
    _spec("pv_energy_today_kwh", PV_ARRAY, "energy_today_kwh"),
    _spec("pv_irradiance_w_m2", PV_ARRAY, "solar_irradiance_w_m2", category="forecast"),
    _spec("pv_availability", PV_ARRAY, "availability_state"),
    _spec("critical_load_kw", CRITICAL_PANEL, "power_total_kw", required=True,
          description="Critical distribution panel load"),
    _spec("general_load_kw", GENERAL_PANEL, "power_total_kw", required=True,
          description="General and deferrable panel load"),
    _spec("critical_panel_breaker_trip", CRITICAL_PANEL, "breaker_trip_active"),
    _spec("general_panel_breaker_trip", GENERAL_PANEL, "breaker_trip_active"),
    _spec("generator_state", GENERATOR, "state_operating", category="generator"),
    _spec("generator_power_kw", GENERATOR, "power_output_kw", category="generator"),
    _spec("generator_fuel_pct", GENERATOR, "fuel_level_pct", category="generator"),
    _spec("generator_runtime_h", GENERATOR, "runtime_total_h", category="generator"),
    _spec("generator_start_failure", GENERATOR, "start_failure_active", category="generator"),
    _spec("generator_available", GENERATOR, "generator_available", category="generator"),
    _spec("ats_source_selected", SITE_ATS, "source_selected"),
    _spec("ats_source_a_available", SITE_ATS, "source_a_available"),
    _spec("ats_source_b_available", SITE_ATS, "source_b_available"),
    _spec("ups_runtime_remaining_min", RACK_UPS, "runtime_remaining_min", category="it"),
    _spec("ups_on_battery", RACK_UPS, "on_battery", category="it"),
    _spec("ups_input_available", RACK_UPS, "input_available", category="it"),
    _spec("ups_load_pct", RACK_UPS, "load_pct", category="it"),
)

#: Per-inverter state, expanded over the four functional inverter positions.
INVERTER_INPUTS: tuple[InputSpec, ...] = tuple(
    spec
    for index, asset in enumerate(INVERTERS, start=1)
    for spec in (
        _spec(f"inverter_{index:02d}_ac_kw", asset, "power_ac_output_kw"),
        _spec(f"inverter_{index:02d}_dc_kw", asset, "power_dc_input_kw"),
        _spec(f"inverter_{index:02d}_state", asset, "state_operating"),
        _spec(f"inverter_{index:02d}_fault_code", asset, "fault_code"),
        _spec(f"inverter_{index:02d}_frequency_hz", asset, "frequency_hz"),
        _spec(f"inverter_{index:02d}_voltage_ac_v", asset, "voltage_ac_v"),
    )
)

#: SDD 30.5 "Context and constraints".
CONTEXT_INPUTS: tuple[InputSpec, ...] = (
    _spec("container_temperature_c", CONTAINER_MONITOR, "temperature_air_c", required=True,
          category="thermal", description="Power/battery container air temperature"),
    _spec("container_humidity_pct", CONTAINER_MONITOR, "humidity_relative_pct", category="thermal"),
    _spec("container_alarm_summary", CONTAINER_MONITOR, "alarm_summary", category="thermal"),
    _spec("rack_temperature_c", RACK_MONITOR, "temperature_air_c", category="thermal"),
    _spec("rack_alarm_summary", RACK_MONITOR, "alarm_summary", category="thermal"),
)

ALL_SPECS: tuple[InputSpec, ...] = ELECTRICAL_INPUTS + INVERTER_INPUTS + CONTEXT_INPUTS

SPEC_BY_KEY: dict[str, InputSpec] = {spec.key: spec for spec in ALL_SPECS}

#: Per-load points. Expanded lazily against the load schedule so a load added to
#: the schedule is observed without editing this module.
LOAD_POINTS = ("power_kw", "enabled_actual", "shed_state")


def load_input_key(asset_id: str, point_name: str) -> str:
    return f"load:{asset_id}:{point_name}"


@dataclass(frozen=True)
class InputReading:
    """One gathered input with everything needed to judge it."""

    key: str
    point_id: str
    value: Any
    quality: str
    ts: dt.datetime | None
    age_s: float | None
    valid: bool
    status: str  # ok | missing | stale | bad_quality | no_value
    required: bool
    max_age_s: int | None = None

    @property
    def numeric(self) -> float | None:
        if not self.valid or self.value is None:
            return None
        if isinstance(self.value, bool):
            return 1.0 if self.value else 0.0
        if isinstance(self.value, (int, float)):
            return float(self.value)
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "point_id": self.point_id,
            "value": self.value,
            "quality": self.quality,
            "ts": self.ts.isoformat() if self.ts else None,
            "age_s": round(self.age_s, 3) if self.age_s is not None else None,
            "valid": self.valid,
            "status": self.status,
            "required": self.required,
        }


@dataclass
class EmsInputs:
    """The gathered SDD 30.5 input set for one evaluation."""

    at: dt.datetime
    readings: dict[str, InputReading] = field(default_factory=dict)
    #: Points that were expected but had no current-state row at all.
    missing_points: list[str] = field(default_factory=list)

    # -- access ----------------------------------------------------------
    def get(self, key: str) -> InputReading | None:
        return self.readings.get(key)

    def numeric(self, key: str) -> float | None:
        """Numeric value, or ``None`` when the input is missing/stale/bad.

        There is deliberately no ``default`` argument. Substituting a default
        for a missing safety-relevant input is what SDD 30.5 forbids.
        """
        reading = self.readings.get(key)
        return reading.numeric if reading else None

    def text(self, key: str) -> str | None:
        reading = self.readings.get(key)
        if reading is None or not reading.valid or reading.value is None:
            return None
        return str(reading.value).lower()

    def flag(self, key: str) -> bool | None:
        """Tri-state boolean: True, False, or None when not observable."""
        reading = self.readings.get(key)
        if reading is None or not reading.valid or reading.value is None:
            return None
        if isinstance(reading.value, bool):
            return reading.value
        if isinstance(reading.value, (int, float)):
            return bool(reading.value)
        return str(reading.value).strip().lower() in {"true", "yes", "on", "1", "active"}

    def is_valid(self, key: str) -> bool:
        reading = self.readings.get(key)
        return bool(reading and reading.valid)

    # -- quality ---------------------------------------------------------
    def invalid_required(self) -> list[InputReading]:
        return [r for r in self.readings.values() if r.required and not r.valid]

    @property
    def observable(self) -> bool:
        """True when every required input is valid (SDD 30.7)."""
        return not self.invalid_required()

    @property
    def data_quality(self) -> str:
        """``good`` | ``degraded`` | ``bad`` summary for the snapshot."""
        invalid_required = self.invalid_required()
        if invalid_required:
            return "bad" if len(invalid_required) > 2 else "degraded"
        if any(not r.valid for r in self.readings.values()):
            return "degraded"
        return "good"

    def sum_valid(self, keys: Iterable[str]) -> tuple[float, list[str], list[str]]:
        """Sum the valid numeric readings, reporting which contributed."""
        total = 0.0
        used: list[str] = []
        skipped: list[str] = []
        for key in keys:
            value = self.numeric(key)
            if value is None:
                skipped.append(key)
            else:
                total += value
                used.append(key)
        return total, used, skipped

    # -- serialisation ---------------------------------------------------
    def as_dict(self, *, include_invalid: bool = True) -> dict[str, Any]:
        """Snapshot form stored on transitions and published to the dashboard."""
        return {
            "at": self.at.isoformat(),
            "data_quality": self.data_quality,
            "observable": self.observable,
            "invalid_required": [r.key for r in self.invalid_required()],
            "readings": {
                key: reading.as_dict()
                for key, reading in sorted(self.readings.items())
                if include_invalid or reading.valid
            },
        }

    def summary(self) -> dict[str, Any]:
        """Compact form for a transition record."""
        return {
            "at": self.at.isoformat(),
            "data_quality": self.data_quality,
            "observable": self.observable,
            "invalid_required": [r.key for r in self.invalid_required()],
            "battery_soc_pct": self.numeric("battery_soc_pct"),
            "battery_temperature_max_c": self.numeric("battery_temperature_max_c"),
            "pv_power_kw": self.numeric("pv_power_kw"),
            "critical_load_kw": self.numeric("critical_load_kw"),
            "general_load_kw": self.numeric("general_load_kw"),
            "generator_state": self.text("generator_state"),
        }


def _age_seconds(ts: dt.datetime | None, now: dt.datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (now - ts).total_seconds()


def evaluate_reading(
    spec: InputSpec,
    row: CurrentState | None,
    now: dt.datetime,
    config: EmsConfig,
) -> InputReading:
    """Turn one current-state row into a judged :class:`InputReading`."""
    if row is None:
        return InputReading(
            key=spec.key,
            point_id=spec.point_id,
            value=None,
            quality="unknown",
            ts=None,
            age_s=None,
            valid=False,
            status="missing",
            required=spec.required,
            max_age_s=spec.max_age_s,
        )

    max_age = spec.max_age_s or row.stale_after_s or config.input_max_age_s
    age = _age_seconds(row.ts, now)
    value = row.value
    quality = row.quality or "unknown"

    if value is None:
        status = "no_value"
    elif quality not in config.valid_qualities:
        status = "bad_quality"
    elif age is None:
        status = "no_timestamp"
    elif age > max_age:
        status = "stale"
    elif age < 0:
        # A timestamp from the future is not trustworthy either.
        status = "future_timestamp"
    else:
        status = "ok"

    return InputReading(
        key=spec.key,
        point_id=spec.point_id,
        value=value,
        quality=quality,
        ts=row.ts,
        age_s=age,
        valid=status == "ok",
        status=status,
        required=spec.required,
        max_age_s=max_age,
    )


def gather_inputs(
    session: Session,
    now: dt.datetime,
    config: EmsConfig,
    *,
    specs: Iterable[InputSpec] = ALL_SPECS,
    load_asset_ids: Iterable[str] = (),
) -> EmsInputs:
    """Read every EMS input from the current-state cache in one query."""
    spec_list = list(specs)
    for asset_id in load_asset_ids:
        for point_name in LOAD_POINTS:
            spec_list.append(
                InputSpec(
                    key=load_input_key(asset_id, point_name),
                    asset_id=asset_id,
                    point_name=point_name,
                    required=False,
                    category="load",
                )
            )

    point_ids = [spec.point_id for spec in spec_list]
    rows: dict[str, CurrentState] = {}
    if point_ids:
        # Chunked so a large schedule does not blow past the SQLite variable limit.
        chunk = 400
        for start in range(0, len(point_ids), chunk):
            window = point_ids[start : start + chunk]
            for row in session.scalars(select(CurrentState).where(CurrentState.point_id.in_(window))):
                rows[row.point_id] = row

    inputs = EmsInputs(at=now)
    for spec in spec_list:
        reading = evaluate_reading(spec, rows.get(spec.point_id), now, config)
        inputs.readings[spec.key] = reading
        if reading.status == "missing":
            inputs.missing_points.append(spec.point_id)
    return inputs
