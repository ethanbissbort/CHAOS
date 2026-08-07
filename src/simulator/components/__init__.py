"""Deterministic physical models for the simulated homestead.

Each module holds one subsystem. The models are intentionally simple and
explicit -- this is an engineering test rig for the EMS, alarm and ingest
subsystems, not a research-grade simulator. Where a coefficient is a guess it is
named and commented so a commissioning engineer can replace it with a measured
value.
"""

from __future__ import annotations

from simulator.components.base import (
    CommandOutcome,
    Component,
    LoadSnapshot,
    PointCatalog,
    PointSpec,
    Reading,
    SiteContext,
    UnknownPointError,
    load_catalog,
)
from simulator.components.battery import BatteryBank, BatteryConfig
from simulator.components.generator import Generator, GeneratorConfig
from simulator.components.inverter import InverterConfig, InverterFarm
from simulator.components.loads import LoadBank, LoadConfig, LoadGroup
from simulator.components.rack import RackConfig, ServerRack
from simulator.components.solar import SolarArray, SolarConfig
from simulator.components.weather import Weather, WeatherConfig

__all__ = [
    "BatteryBank",
    "BatteryConfig",
    "CommandOutcome",
    "Component",
    "Generator",
    "GeneratorConfig",
    "InverterConfig",
    "InverterFarm",
    "LoadBank",
    "LoadConfig",
    "LoadGroup",
    "LoadSnapshot",
    "PointCatalog",
    "PointSpec",
    "RackConfig",
    "Reading",
    "ServerRack",
    "SiteContext",
    "SolarArray",
    "SolarConfig",
    "UnknownPointError",
    "Weather",
    "WeatherConfig",
    "load_catalog",
]
