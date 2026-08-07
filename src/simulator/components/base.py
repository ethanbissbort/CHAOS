"""Component contract and the point catalogue that keeps the simulator honest.

Every component is a small, explicit, deterministic physical model. It exposes

* :meth:`Component.points` -- the point metadata it will publish, and
* :meth:`Component.step` -- ``(now, dt_s, context) -> {point_id: value}``.

The catalogue exists because the simulator must not invent telemetry. A point
is publishable for an asset only if the machine-readable design package says so.
Following ``homestead_twin.registry.points``, the point set of an asset is the
union of three sources (SDD section 43):

1. the asset class's ``default_points`` in ``asset_class_dictionary.yaml``
2. every point named by a referenced ``point_profile``
3. every ``point_name`` bound to the asset in ``point_bindings.yaml``

:class:`PointCatalog` enforces that union. Declaring a point outside it raises
:class:`UnknownPointError` at construction time, which is the whole reason the
simulator can be trusted as an ingest test source.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from homestead_twin.config import DATA_DIR
from homestead_twin.envelope import CommandEnvelope

# --- publishing cadence ---------------------------------------------------

#: Fallback publish interval by asset class, used when a point has no binding
#: in ``point_bindings.yaml``. Chosen to match the cadence of the bound points
#: on the same class so a partly-bound asset does not publish erratically.
DEFAULT_PUBLISH_INTERVAL_S: Mapping[str, float] = {
    "battery_bank": 5.0,
    "bms": 5.0,
    "inverter": 5.0,
    "generator": 5.0,
    "ats": 5.0,
    "panel": 5.0,
    "load": 5.0,
    "pv_array": 10.0,
    "pv_row": 10.0,
    "ups": 10.0,
    "pdu": 30.0,
    "server": 30.0,
    "rack": 30.0,
    "environmental_monitor": 30.0,
    "safety_sensor": 30.0,
    "alarm_output": 30.0,
}
FALLBACK_PUBLISH_INTERVAL_S = 30.0

#: Configuration, text and counter points describe slow-moving facts. Even when
#: the binding asks for 5 s they are published no faster than this, matching how
#: a real gateway treats CFG/TEXT registers.
SLOW_POINT_CLASSES = frozenset({"CFG", "TEXT", "COUNTER"})
SLOW_POINT_INTERVAL_S = 60.0

#: Point classes whose value is discrete: publish immediately on change as well
#: as on the interval, exactly as a change-of-state gateway would.
DISCRETE_POINT_CLASSES = frozenset({"DI", "DO", "TEXT", "CFG", "EVENT", "ALARM"})


class UnknownPointError(KeyError):
    """Raised when a component declares a point the design package does not allow."""


@dataclass(frozen=True)
class PointSpec:
    """Everything the publisher needs to know about one simulated point."""

    asset_id: str
    name: str
    unit: str | None
    point_class: str
    data_type: str
    publish_interval_s: float
    stale_after_s: float
    enum_values: tuple[str, ...] | None = None

    @property
    def point_id(self) -> str:
        return f"{self.asset_id}/{self.name}"

    @property
    def is_discrete(self) -> bool:
        return self.point_class in DISCRETE_POINT_CLASSES


@dataclass(frozen=True)
class Reading:
    """A value plus a deliberate quality code.

    Components return bare values almost always; they return a ``Reading`` when
    the model itself knows the measurement is degraded (a derived value while an
    input is missing, a sensor under maintenance, ...). Injected sensor faults
    are applied later, by the site publisher.
    """

    value: Any
    quality: str = "good"


class PointCatalog:
    """Read-only view of the machine-readable design package."""

    def __init__(
        self,
        assets: Mapping[str, Mapping[str, Any]],
        asset_classes: Mapping[str, Mapping[str, Any]],
        point_profiles: Mapping[str, Sequence[str]],
        points: Mapping[str, Mapping[str, Any]],
        bindings: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> None:
        self._assets = assets
        self._asset_classes = asset_classes
        self._point_profiles = point_profiles
        self._points = points
        self._bindings = bindings
        self._allowed_cache: dict[str, frozenset[str]] = {}

    # -- assets ----------------------------------------------------------
    def has_asset(self, asset_id: str) -> bool:
        return asset_id in self._assets

    def asset(self, asset_id: str) -> Mapping[str, Any]:
        try:
            return self._assets[asset_id]
        except KeyError as exc:  # pragma: no cover - defensive
            raise UnknownPointError(f"asset {asset_id!r} is not in the register") from exc

    def asset_class(self, asset_id: str) -> str:
        return str(self.asset(asset_id)["asset_class"])

    def asset_ids(self) -> list[str]:
        return list(self._assets)

    def properties(self, asset_id: str) -> Mapping[str, Any]:
        return self.asset(asset_id).get("properties") or {}

    # -- points ----------------------------------------------------------
    def allowed_points(self, asset_id: str) -> frozenset[str]:
        """The union of class defaults, profile points and explicit bindings."""
        cached = self._allowed_cache.get(asset_id)
        if cached is not None:
            return cached
        asset = self.asset(asset_id)
        class_def = self._asset_classes.get(asset["asset_class"], {})
        names: set[str] = set(class_def.get("default_points") or ())
        for profile in asset.get("point_profile_refs") or ():
            names.update(self._point_profiles.get(profile) or ())
        names.update(self._bindings.get(asset_id, {}))
        # A point that is not in the dictionary has no unit, class or type and
        # therefore cannot be published under a documented envelope.
        allowed = frozenset(name for name in names if name in self._points)
        self._allowed_cache[asset_id] = allowed
        return allowed

    def binding(self, asset_id: str, point_name: str) -> Mapping[str, Any] | None:
        return self._bindings.get(asset_id, {}).get(point_name)

    def spec(self, asset_id: str, point_name: str) -> PointSpec:
        """Build a :class:`PointSpec`, refusing anything outside the allowed union."""
        allowed = self.allowed_points(asset_id)
        if point_name not in allowed:
            raise UnknownPointError(
                f"point {point_name!r} is not defined for {asset_id!r} (allowed: {sorted(allowed)})"
            )
        definition = self._points[point_name]
        binding = self.binding(asset_id, point_name)
        point_class = str(definition.get("default_class") or "AI")
        asset_class = self.asset_class(asset_id)
        if binding and binding.get("publish_interval_s"):
            interval = float(binding["publish_interval_s"])
            stale = float(binding.get("stale_after_s") or interval * 4)
        else:
            interval = DEFAULT_PUBLISH_INTERVAL_S.get(asset_class, FALLBACK_PUBLISH_INTERVAL_S)
            stale = interval * 4
        if point_class in SLOW_POINT_CLASSES:
            interval = max(interval, SLOW_POINT_INTERVAL_S)
            stale = max(stale, interval * 4)
        enum_values = definition.get("enum_values")
        return PointSpec(
            asset_id=asset_id,
            name=point_name,
            unit=definition.get("unit"),
            point_class=point_class,
            data_type=str(definition.get("data_type") or "float"),
            publish_interval_s=interval,
            stale_after_s=stale,
            enum_values=tuple(enum_values) if enum_values else None,
        )

    def binding_count(self) -> int:
        return sum(len(v) for v in self._bindings.values())


@lru_cache(maxsize=4)
def load_catalog(data_dir: Path | str = DATA_DIR) -> PointCatalog:
    """Load the design package once per data directory.

    The simulator only ever reads these files; ``data/`` belongs to the design
    package, not to the simulator.
    """
    root = Path(data_dir)
    register = yaml.safe_load((root / "homestead_asset_register.yaml").read_text())
    class_dict = yaml.safe_load((root / "asset_class_dictionary.yaml").read_text())
    point_dict = yaml.safe_load((root / "point_dictionary.yaml").read_text())
    bindings_doc = yaml.safe_load((root / "point_bindings.yaml").read_text())

    assets = {asset["asset_id"]: asset for asset in register["assets"]}
    bindings: dict[str, dict[str, Mapping[str, Any]]] = {}
    for binding in bindings_doc["bindings"]:
        bindings.setdefault(binding["asset_id"], {})[binding["point_name"]] = binding
    return PointCatalog(
        assets=assets,
        asset_classes=class_dict["asset_classes"],
        point_profiles=class_dict["point_profiles"],
        points=point_dict["points"],
        bindings=bindings,
    )


# --- shared simulation state ---------------------------------------------


@dataclass
class LoadSnapshot:
    """What one load group looks like to the rest of the site this step."""

    asset_id: str
    tier: int
    power_kw: float = 0.0
    requested_kw: float = 0.0
    enabled: bool = True
    shed_state: str = "connected"
    critical: bool = True


@dataclass
class SiteContext:
    """Mutable per-step blackboard shared by the components.

    Explicit fields rather than a dict: the coupling between subsystems *is*
    the interesting part of an energy model and should be readable.
    """

    seed: int = 0
    step_index: int = 0

    # -- weather --------------------------------------------------------
    ambient_temperature_c: float = 18.0
    humidity_pct: float = 55.0
    wind_speed_m_s: float = 2.0
    cloud_cover: float = 0.0
    poa_irradiance_w_m2: float = 0.0
    solar_elevation_deg: float = 0.0

    # -- PV -------------------------------------------------------------
    pv_available_dc_kw: float = 0.0
    pv_delivered_dc_kw: float = 0.0
    pv_curtailed_kw: float = 0.0

    # -- battery / BMS ---------------------------------------------------
    battery_soc_pct: float = 50.0
    battery_power_kw: float = 0.0  # + charging, - discharging (terminal power)
    battery_energy_kwh: float = 0.0
    charge_limit_kw: float = 0.0
    discharge_limit_kw: float = 0.0
    charge_permitted: bool = True
    discharge_permitted: bool = True
    cell_temperature_c: float = 20.0
    contactor_state: str = "closed"

    # -- conversion / AC bus ---------------------------------------------
    inverter_capacity_kw: float = 0.0
    inverter_ac_output_kw: float = 0.0
    inverter_online_count: int = 0
    ac_bus_energized: bool = True
    critical_bus_energized: bool = True
    ac_frequency_hz: float = 60.0
    ac_voltage_v: float = 240.0
    unserved_load_kw: float = 0.0

    # -- generator / transfer --------------------------------------------
    generator_start_request: bool = False
    generator_running: bool = False
    generator_output_kw: float = 0.0
    generator_available: bool = True
    transfer_source: str = "inverter"

    # -- loads ------------------------------------------------------------
    load_demand_kw: float = 0.0
    load_actual_kw: float = 0.0
    critical_load_kw: float = 0.0
    loads: dict[str, LoadSnapshot] = field(default_factory=dict)

    # -- rack / container -------------------------------------------------
    rack_it_kw: float = 1.1
    rack_cooling_kw: float = 0.0
    rack_cooling_available: bool = True
    container_temperature_c: float = 22.0
    rack_inlet_temperature_c: float = 22.0

    # -- site mode ---------------------------------------------------------
    black_start_stage: str = "not_required"
    site_mode: str = "automatic"

    def load(self, asset_id: str) -> LoadSnapshot | None:
        return self.loads.get(asset_id)


@dataclass
class CommandOutcome:
    """Result of applying a command to a component.

    ``accepted=False`` models a *local* refusal -- a Tier 0 load whose local
    controller retains authority, or a generator whose start permissives are not
    satisfied. SDD section 5.3 requires that supervisory control cannot override
    equipment-native safety, so these refusals are first-class.
    """

    accepted: bool
    detail: str
    payload: dict[str, Any] | None = None
    completed: bool = True
    event: str | None = None


class Component:
    """Base class for every simulated subsystem."""

    #: Short stable name used for seeding and for CLI summaries.
    name: str = "component"

    def __init__(self, catalog: PointCatalog, name: str | None = None) -> None:
        self.catalog = catalog
        if name is not None:
            self.name = name
        self._specs: dict[str, PointSpec] = {}

    # -- point declaration -----------------------------------------------
    def declare(self, asset_id: str, point_names: Iterable[str]) -> None:
        """Register the points this component publishes for one asset.

        Raises immediately if the design package does not allow the point, so a
        typo can never reach the broker.
        """
        for point_name in point_names:
            spec = self.catalog.spec(asset_id, point_name)
            self._specs[spec.point_id] = spec

    def points(self) -> list[PointSpec]:
        """Metadata for every point this component publishes."""
        return list(self._specs.values())

    def point_ids(self) -> list[str]:
        return list(self._specs)

    def asset_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for spec in self._specs.values():
            seen.setdefault(spec.asset_id, None)
        return list(seen)

    # -- simulation --------------------------------------------------------
    def step(self, now: dt.datetime, dt_s: float, context: SiteContext) -> dict[str, Any]:
        """Advance the model and return ``{point_id: value | Reading}``."""
        raise NotImplementedError

    def handle_command(self, command: CommandEnvelope) -> CommandOutcome | None:
        """Apply a command. ``None`` means "not mine / unknown command"."""
        return None

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def emit(
        out: dict[str, Any],
        asset_id: str,
        point_name: str,
        value: Any,
        quality: str | None = None,
    ) -> None:
        out[f"{asset_id}/{point_name}"] = Reading(value, quality) if quality else value

    def validate(self, values: Mapping[str, Any]) -> None:
        """Assert a step only produced declared points (used by tests)."""
        unknown = set(values) - set(self._specs)
        if unknown:
            raise UnknownPointError(f"{self.name} emitted undeclared points: {sorted(unknown)}")


def clamp(value: float, low: float, high: float) -> float:
    """Clamp helper used throughout the physical models."""
    return low if value < low else min(value, high)


def approach(current: float, target: float, dt_s: float, tau_s: float) -> float:
    """First-order lag toward ``target`` with time constant ``tau_s``.

    Used for every thermal mass in the simulator. Exact exponential form rather
    than an Euler step so the result does not depend on the step size.
    """
    if tau_s <= 0 or dt_s <= 0:
        return target
    import math

    alpha = 1.0 - math.exp(-dt_s / tau_s)
    return current + (target - current) * alpha
