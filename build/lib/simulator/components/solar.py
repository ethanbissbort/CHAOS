"""Agrivoltaic PV array: clear-sky irradiance, cloud, temperature derate.

Models ``energy.pv_array.agrivoltaic_field.01`` and the four
``energy.pv_row.agrivoltaic_field.0X`` rows from the v0.3 register: 100 bifacial
450 W modules, 45 kWdc, true south, 30 degree tilt, four rows of 25.

The chain is deliberately textbook and each step is named:

    declination -> hour angle -> solar elevation -> air mass -> clear-sky DNI
    -> plane-of-array irradiance -> cloud attenuation -> cell temperature
    -> DC power

Two power figures are kept apart, because the EMS needs both (SDD 30.5 "PV power
available and generated") and because SDD 36 dispatches *surplus*:

``available_dc_kw``   what the array could produce right now
``delivered_dc_kw``   what it actually produces after the site curtails it

The site sets ``delivered`` during the energy balance; the array publishes
``power_dc_kw`` as delivered, which is what a real DC meter would read.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass
from typing import Any

from simulator.clock import local_day_index
from simulator.components.base import Component, PointCatalog, SiteContext, clamp

#: Solar constant, W/m2.
SOLAR_CONSTANT_W_M2 = 1361.0

ARRAY_ASSET = "energy.pv_array.agrivoltaic_field.01"
ROW_ASSETS = tuple(f"energy.pv_row.agrivoltaic_field.{i:02d}" for i in range(1, 5))


@dataclass
class SolarConfig:
    """PV plant parameters. Defaults come from the v0.3 asset register."""

    array_asset_id: str = ARRAY_ASSET
    row_asset_ids: tuple[str, ...] = ROW_ASSETS
    #: Per-row DC capacity, kW (register: ``row_dc_capacity_kw`` 11.25 x 4).
    row_capacity_kw: tuple[float, ...] = (11.25, 11.25, 11.25, 11.25)
    tilt_deg: float = 30.0
    latitude_deg: float = 44.3
    longitude_deg: float = -78.3
    utc_offset_h: float = -5.0

    #: Bifacial rear-side gain (register: ``module_type: bifacial``).
    bifacial_gain: float = 1.06
    #: Soiling / mismatch / wiring losses applied to DC.
    soiling_factor: float = 0.97
    #: Ground albedo used for the rear/ground-reflected term.
    albedo: float = 0.22
    #: Module power temperature coefficient, fraction per degree C.
    temperature_coefficient_per_c: float = -0.0035
    #: Nominal operating cell temperature, degrees C.
    noct_c: float = 45.0
    #: Inter-row shading strength at low sun elevation (rows 2..N).
    row_shading_strength: float = 0.45
    #: Elevation below which inter-row shading starts to bite.
    row_shading_elevation_deg: float = 18.0

    #: Per-row availability. A row set False models a blown string/combiner.
    row_available: tuple[bool, ...] = (True, True, True, True)
    seed: int = 0


@dataclass
class RowState:
    asset_id: str
    capacity_kw: float
    available_kw: float = 0.0
    delivered_kw: float = 0.0
    online: bool = True


def solar_declination_deg(doy: int) -> float:
    """Cooper's equation, degrees."""
    return 23.45 * math.sin(math.radians(360.0 * (284 + doy) / 365.0))


def equation_of_time_min(doy: int) -> float:
    """Spencer/Duffie-Beckman equation of time, minutes."""
    b = 2.0 * math.pi * (doy - 1) / 365.0
    return 229.18 * (
        0.000075
        + 0.001868 * math.cos(b)
        - 0.032077 * math.sin(b)
        - 0.014615 * math.cos(2 * b)
        - 0.040890 * math.sin(2 * b)
    )


def solar_hour_angle_deg(now: dt.datetime, longitude_deg: float) -> float:
    """Hour angle: 0 at true solar noon, +15 deg per hour after noon.

    Derived from UTC and longitude, so the simulator never needs a timezone
    database and "solar noon" is a physical result rather than an assumption.
    """
    doy = now.timetuple().tm_yday
    utc_hours = now.hour + now.minute / 60.0 + now.second / 3600.0
    solar_hours = utc_hours + longitude_deg / 15.0 + equation_of_time_min(doy) / 60.0
    return 15.0 * (solar_hours - 12.0)


def solar_elevation_deg(now: dt.datetime, latitude_deg: float, longitude_deg: float) -> float:
    """Sun elevation above the horizon, degrees (negative at night)."""
    doy = now.timetuple().tm_yday
    lat = math.radians(latitude_deg)
    dec = math.radians(solar_declination_deg(doy))
    omega = math.radians(solar_hour_angle_deg(now, longitude_deg))
    sin_elev = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(omega)
    return math.degrees(math.asin(clamp(sin_elev, -1.0, 1.0)))


def air_mass(elevation_deg: float) -> float:
    """Kasten-Young relative air mass. Large but finite near the horizon."""
    if elevation_deg <= 0:
        return 40.0
    zenith = 90.0 - elevation_deg
    return min(
        40.0,
        1.0 / (math.cos(math.radians(zenith)) + 0.50572 * (96.07995 - zenith) ** -1.6364),
    )


def clear_sky_poa_w_m2(
    now: dt.datetime,
    latitude_deg: float,
    longitude_deg: float,
    tilt_deg: float,
    albedo: float,
) -> tuple[float, float]:
    """Clear-sky plane-of-array irradiance for a true-south fixed tilt.

    Returns ``(poa_w_m2, elevation_deg)``.

    * Direct beam from the Meinel clear-sky model ``1361 * 0.7 ** AM**0.678``.
    * Diffuse taken as 10 % of the horizontal beam component (adequate for a
      test rig; a real forecast integration replaces this later).
    * Incidence on a south-facing tilted plane collapses to the classic
      ``sin(d)sin(phi-beta) + cos(d)cos(phi-beta)cos(omega)`` form.
    """
    elevation = solar_elevation_deg(now, latitude_deg, longitude_deg)
    if elevation <= 0.0:
        return 0.0, elevation
    dni = SOLAR_CONSTANT_W_M2 * 0.7 ** (air_mass(elevation) ** 0.678)
    sin_elev = math.sin(math.radians(elevation))
    ghi_beam = dni * sin_elev
    dhi = 0.10 * ghi_beam
    ghi = ghi_beam + dhi

    doy = now.timetuple().tm_yday
    dec = math.radians(solar_declination_deg(doy))
    omega = math.radians(solar_hour_angle_deg(now, longitude_deg))
    lat_eff = math.radians(latitude_deg - tilt_deg)
    cos_incidence = math.sin(dec) * math.sin(lat_eff) + math.cos(dec) * math.cos(lat_eff) * math.cos(omega)
    cos_incidence = max(0.0, cos_incidence)

    tilt = math.radians(tilt_deg)
    poa = (
        dni * cos_incidence + dhi * (1.0 + math.cos(tilt)) / 2.0 + ghi * albedo * (1.0 - math.cos(tilt)) / 2.0
    )
    return max(0.0, poa), elevation


def cloud_attenuation(cloud_cover: float) -> float:
    """Kasten-Czeplak style transmittance: ``1 - 0.75 * cc**3.4``.

    Deliberately non-linear -- thin cover costs almost nothing, heavy overcast
    costs three quarters of the resource.
    """
    return clamp(1.0 - 0.75 * clamp(cloud_cover, 0.0, 1.0) ** 3.4, 0.05, 1.0)


class SolarArray(Component):
    """PV array plus its four physical canopy rows."""

    name = "solar"

    def __init__(self, catalog: PointCatalog, config: SolarConfig | None = None) -> None:
        super().__init__(catalog)
        self.config = config or SolarConfig()
        self.random = random.Random(f"{self.config.seed}:solar")
        self.rows = [
            RowState(asset_id=asset_id, capacity_kw=capacity, online=online)
            for asset_id, capacity, online in zip(
                self.config.row_asset_ids,
                self.config.row_capacity_kw,
                self.config.row_available,
            )
        ]
        self.available_dc_kw = 0.0
        self.delivered_dc_kw = 0.0
        self.poa_w_m2 = 0.0
        self.elevation_deg = 0.0
        self.cell_temperature_c = 20.0
        self.energy_today_kwh = 0.0
        self.fault_code = ""
        self.fault_active = False
        self._day_index: int | None = None

        self.declare(
            self.config.array_asset_id,
            [
                "power_dc_kw",
                "energy_today_kwh",
                "solar_irradiance_w_m2",
                "availability_state",
                "alarm_summary",
                "fault_active",
                "fault_code",
                "heartbeat_age_s",
                "firmware_version",
            ],
        )
        for row in self.rows:
            self.declare(row.asset_id, ["power_dc_kw", "availability_state"])

    # -- scenario control -------------------------------------------------
    def set_row_available(self, index: int, available: bool, code: str = "string_open") -> None:
        """Take a canopy row out of service (blown string, combiner fault)."""
        self.rows[index].online = available
        self.fault_active = any(not row.online for row in self.rows)
        self.fault_code = code if self.fault_active else ""

    # -- physics ------------------------------------------------------------
    def compute(self, now: dt.datetime, dt_s: float, context: SiteContext) -> float:
        """Compute *available* DC power and publish it to the context.

        Called by the site before the energy balance; the balance then decides
        how much of it is actually taken and sets ``pv_delivered_dc_kw``.
        """
        poa_clear, elevation = clear_sky_poa_w_m2(
            now,
            self.config.latitude_deg,
            self.config.longitude_deg,
            self.config.tilt_deg,
            self.config.albedo,
        )
        self.elevation_deg = elevation
        self.poa_w_m2 = poa_clear * cloud_attenuation(context.cloud_cover)

        # Cell temperature from the NOCT model, with a small wind correction.
        wind_relief = 1.0 + 0.08 * max(0.0, context.wind_speed_m_s - 1.0)
        self.cell_temperature_c = (
            context.ambient_temperature_c
            + ((self.config.noct_c - 20.0) / 800.0) * self.poa_w_m2 / wind_relief
        )
        temp_derate = clamp(
            1.0 + self.config.temperature_coefficient_per_c * (self.cell_temperature_c - 25.0),
            0.5,
            1.15,
        )

        # Inter-row shading: rows behind the first lose output when the sun is
        # low, which is why row-level telemetry exists at all.
        if elevation > 0 and elevation < self.config.row_shading_elevation_deg:
            shade_depth = (
                self.config.row_shading_strength
                * (self.config.row_shading_elevation_deg - elevation)
                / self.config.row_shading_elevation_deg
            )
        else:
            shade_depth = 0.0

        total = 0.0
        for index, row in enumerate(self.rows):
            if not row.online:
                row.available_kw = 0.0
                continue
            shading = 1.0 - (shade_depth if index > 0 else 0.0)
            row.available_kw = max(
                0.0,
                row.capacity_kw
                * (self.poa_w_m2 / 1000.0)
                * temp_derate
                * self.config.soiling_factor
                * self.config.bifacial_gain
                * shading,
            )
            total += row.available_kw

        self.available_dc_kw = total
        context.pv_available_dc_kw = total
        context.poa_irradiance_w_m2 = self.poa_w_m2
        context.solar_elevation_deg = elevation
        return total

    def apply_delivered(self, delivered_dc_kw: float, dt_s: float, now: dt.datetime) -> None:
        """Record what the site actually took, and split it across the rows."""
        self.delivered_dc_kw = clamp(delivered_dc_kw, 0.0, max(self.available_dc_kw, 0.0))
        share = self.delivered_dc_kw / self.available_dc_kw if self.available_dc_kw > 1e-9 else 0.0
        for row in self.rows:
            row.delivered_kw = row.available_kw * share
        day = local_day_index(now, self.config.utc_offset_h)
        if self._day_index is None:
            self._day_index = day
        elif day != self._day_index:
            self.energy_today_kwh = 0.0
            self._day_index = day
        self.energy_today_kwh += self.delivered_dc_kw * dt_s / 3600.0

    # -- emission ------------------------------------------------------------
    def step(self, now: dt.datetime, dt_s: float, context: SiteContext) -> dict[str, Any]:
        """Emit array and row telemetry using the values the balance settled on."""
        out: dict[str, Any] = {}
        array = self.config.array_asset_id
        online_rows = sum(1 for row in self.rows if row.online)
        if online_rows == 0:
            availability = "offline"
        elif online_rows < len(self.rows):
            availability = "degraded"
        else:
            availability = "online"

        self.emit(out, array, "power_dc_kw", round(self.delivered_dc_kw, 3))
        self.emit(out, array, "energy_today_kwh", round(self.energy_today_kwh, 3))
        self.emit(out, array, "solar_irradiance_w_m2", round(self.poa_w_m2, 1))
        self.emit(out, array, "availability_state", availability)
        self.emit(out, array, "alarm_summary", "warning" if self.fault_active else "none")
        self.emit(out, array, "fault_active", self.fault_active)
        self.emit(out, array, "fault_code", self.fault_code or "none")
        self.emit(out, array, "heartbeat_age_s", 0.0)
        self.emit(out, array, "firmware_version", "sim-pv-1.0.0")
        for row in self.rows:
            self.emit(out, row.asset_id, "power_dc_kw", round(row.delivered_kw, 3))
            self.emit(out, row.asset_id, "availability_state", "online" if row.online else "offline")
        return out
