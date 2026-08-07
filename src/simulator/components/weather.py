"""Ambient conditions and the cloud process that drives PV.

Weather is a *driver*: the v0.3 asset register contains no ``weather_station``
instance, so this component publishes no points of its own. Its outputs surface
through assets that do exist -- plane-of-array irradiance on the PV array
(``solar_irradiance_w_m2``) and container air temperature on the NetBotz
appliance (``temperature_air_c``).

The cloud process matters more than the temperature model: SDD section 30.9
requires that "temporary irradiance changes caused by passing clouds must not
trigger repeated load switching", and SDD section 39 case ``EMS-T006`` tests
exactly that. :class:`CloudMode` therefore includes a deterministic
``passing_clouds`` mode with a controllable period and depth.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass
from typing import Any

from simulator.clock import day_of_year, hour_of_day
from simulator.components.base import Component, PointCatalog, SiteContext, clamp

#: Cloud regimes. ``variable`` is a bounded random walk; ``passing_clouds`` is a
#: deterministic square-ish pulse train sized to provoke EMS oscillation.
CLOUD_MODES = ("clear", "light", "variable", "overcast", "passing_clouds", "storm")


@dataclass
class WeatherConfig:
    """Site climate parameters (southern Ontario baseline)."""

    latitude_deg: float = 44.3
    longitude_deg: float = -78.3
    utc_offset_h: float = -5.0

    #: Annual mean and swing of daily-mean air temperature.
    annual_mean_temp_c: float = 7.5
    annual_temp_swing_c: float = 15.0
    #: Peak-to-peak diurnal swing, and the hour at which the daily maximum falls.
    diurnal_swing_c: float = 9.0
    peak_temp_hour: float = 15.0
    #: Temperature offset applied on top of the seasonal model (scenario knob).
    temperature_offset_c: float = 0.0

    mean_humidity_pct: float = 62.0
    mean_wind_m_s: float = 3.0

    cloud_mode: str = "clear"
    #: Mean cover for the steady modes and the floor/ceiling for pulses.
    cloud_base: float | None = None
    #: ``passing_clouds`` geometry: one cloud every ``cloud_period_s`` seconds,
    #: obscuring the sun for ``cloud_duration_s``.
    cloud_period_s: float = 900.0
    cloud_duration_s: float = 300.0
    cloud_depth: float = 0.85
    #: Random-walk strength for ``variable``.
    cloud_volatility: float = 0.08

    seed: int = 0


#: Nominal mean cover per steady mode.
_MODE_BASE = {
    "clear": 0.04,
    "light": 0.25,
    "variable": 0.45,
    "overcast": 0.95,
    "passing_clouds": 0.10,
    "storm": 0.97,
}


class Weather(Component):
    """Ambient temperature, humidity, wind and cloud cover.

    All state is advanced from ``(now, dt_s)`` and a seeded generator, so two
    runs with the same seed produce identical weather.
    """

    name = "weather"

    def __init__(self, catalog: PointCatalog, config: WeatherConfig | None = None) -> None:
        super().__init__(catalog)
        self.config = config or WeatherConfig()
        self.random = random.Random(f"{self.config.seed}:weather")
        base = self.config.cloud_base
        if base is None:
            base = _MODE_BASE.get(self.config.cloud_mode, 0.1)
        self.cloud_base = clamp(base, 0.0, 1.0)
        self.cloud_cover = self.cloud_base
        self.temperature_c = self.config.annual_mean_temp_c
        self.humidity_pct = self.config.mean_humidity_pct
        self.wind_speed_m_s = self.config.mean_wind_m_s
        self._noise_c = 0.0
        self._elapsed_s = 0.0
        # Weather publishes no points: no weather_station asset exists in the
        # v0.3 register. See module docstring.

    # -- scenario control -------------------------------------------------
    def set_mode(self, mode: str, base: float | None = None) -> None:
        """Switch cloud regime mid-run (used by scenario events)."""
        if mode not in CLOUD_MODES:
            raise ValueError(f"unknown cloud mode {mode!r}; expected one of {CLOUD_MODES}")
        self.config.cloud_mode = mode
        self.cloud_base = clamp(_MODE_BASE.get(mode, 0.1) if base is None else base, 0.0, 1.0)

    # -- model -------------------------------------------------------------
    def _seasonal_mean_c(self, now: dt.datetime) -> float:
        """Sinusoidal seasonal temperature, minimum near day 20."""
        doy = day_of_year(now, self.config.utc_offset_h)
        phase = 2.0 * math.pi * (doy - 20) / 365.0
        return self.config.annual_mean_temp_c - self.config.annual_temp_swing_c * math.cos(phase)

    def _diurnal_offset_c(self, now: dt.datetime) -> float:
        """Daily temperature swing peaking at ``peak_temp_hour``."""
        hour = hour_of_day(now, self.config.utc_offset_h)
        phase = 2.0 * math.pi * (hour - self.config.peak_temp_hour) / 24.0
        return 0.5 * self.config.diurnal_swing_c * math.cos(phase)

    def _cloud(self, dt_s: float) -> float:
        mode = self.config.cloud_mode
        if mode == "passing_clouds":
            # Deterministic pulse train: fully clear between clouds, deep cover
            # while one passes. Edges are softened over 10 % of the duration so
            # irradiance ramps rather than stepping, which is what makes an
            # under-damped EMS oscillate.
            period = max(self.config.cloud_period_s, 1.0)
            phase = self._elapsed_s % period
            duration = min(self.config.cloud_duration_s, period)
            ramp = max(duration * 0.1, 1.0)
            if phase < duration:
                if phase < ramp:
                    shape = phase / ramp
                elif phase > duration - ramp:
                    shape = (duration - phase) / ramp
                else:
                    shape = 1.0
            else:
                shape = 0.0
            return clamp(self.cloud_base + self.config.cloud_depth * shape, 0.0, 1.0)
        if mode == "variable":
            # Bounded random walk (Ornstein-Uhlenbeck style) around the base.
            pull = (self.cloud_base - self.cloud_cover) * min(dt_s / 1800.0, 1.0)
            kick = self.random.gauss(0.0, self.config.cloud_volatility) * min(dt_s / 300.0, 1.0)
            return clamp(self.cloud_cover + pull + kick, 0.0, 1.0)
        # Steady modes still breathe a little so the EMS never sees a constant.
        pull = (self.cloud_base - self.cloud_cover) * min(dt_s / 600.0, 1.0)
        kick = self.random.gauss(0.0, 0.02) * min(dt_s / 300.0, 1.0)
        return clamp(self.cloud_cover + pull + kick, 0.0, 1.0)

    def step(self, now: dt.datetime, dt_s: float, context: SiteContext) -> dict[str, Any]:
        self._elapsed_s += dt_s

        # Temperature: seasonal mean + diurnal swing + a slow AR(1) weather
        # anomaly, damped by cloud cover (cloudy days are flatter and cooler).
        self._noise_c = self._noise_c * math.exp(-dt_s / 10800.0) + self.random.gauss(0.0, 0.35) * min(
            dt_s / 600.0, 1.0
        )
        self.cloud_cover = self._cloud(dt_s)
        seasonal = self._seasonal_mean_c(now)
        diurnal = self._diurnal_offset_c(now) * (1.0 - 0.55 * self.cloud_cover)
        self.temperature_c = (
            seasonal + diurnal + self._noise_c - 2.0 * self.cloud_cover + self.config.temperature_offset_c
        )

        # Humidity rises with cloud and falls with temperature above the mean.
        target_rh = clamp(
            self.config.mean_humidity_pct + 25.0 * self.cloud_cover - 1.6 * (self.temperature_c - seasonal),
            15.0,
            100.0,
        )
        self.humidity_pct = clamp(
            self.humidity_pct + (target_rh - self.humidity_pct) * min(dt_s / 900.0, 1.0),
            5.0,
            100.0,
        )

        # Wind: mean-reverting, gustier under heavy cloud.
        target_wind = self.config.mean_wind_m_s * (1.0 + 1.2 * self.cloud_cover)
        self.wind_speed_m_s = max(
            0.0,
            self.wind_speed_m_s
            + (target_wind - self.wind_speed_m_s) * min(dt_s / 1200.0, 1.0)
            + self.random.gauss(0.0, 0.4) * min(dt_s / 300.0, 1.0),
        )

        context.ambient_temperature_c = self.temperature_c
        context.humidity_pct = self.humidity_pct
        context.wind_speed_m_s = self.wind_speed_m_s
        context.cloud_cover = self.cloud_cover
        return {}
