"""What a NetBotz sensor means in the canonical model.

An APC NetBotz appliance -- a NetBotz 500 on the container wall, an NBRK0550 in
the rack -- reports its own sensor pods by its own type names and its own
numeric IDs. This module is the single place that says what each of those types
*is* in the point dictionary, and it does that by naming candidate canonical
points rather than by deciding.

Two things it deliberately does not do:

* **It does not choose which point a reading lands on.** A temperature probe is
  ``temperature_air_c`` when the pod hangs off an ``environmental_monitor`` and
  ``value`` when the same probe is registered as its own ``safety_sensor``
  (which is how ``safety.safety_sensor.rack_01.ap9512thblk_01`` is registered in
  the v0.3 package). The candidates are offered in preference order and the
  registry picks the one that exists -- see
  :meth:`chaos.plugins.mirror.IdentityMap.resolve`.
* **It does not invent point names.** Every name below is in
  ``data/point_dictionary.yaml``. A NetBotz sensor type with no canonical
  equivalent is listed with no candidates at all, so it is dropped as
  unresolvable and shows up on the plugin's screen as a gap in the dictionary --
  which is a design decision for the property owner, not something an
  integration should quietly paper over.

Units follow the dictionary, so a mirrored value needs no conversion beyond what
the vendor already reports. NetBotz reports temperature in whichever scale the
appliance is configured for; :func:`normalise_temperature` is the one conversion
this module performs, because ``temperature_air_c`` is degrees Celsius by name
and a mirrored Fahrenheit reading would be a silent 30-degree error in a
freeze-protection alarm.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Enum value used when a vendor state cannot be mapped to a dictionary enum.
#: ``availability_state`` and ``door_state`` both define ``unknown``; publishing
#: it is honest, and ingest accepts it, where an unmapped vendor string would be
#: dead-lettered as an enum violation.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class SensorType:
    """One NetBotz sensor type and the canonical points it may become."""

    key: str
    summary: str
    #: Canonical point names in preference order. Empty means the dictionary has
    #: no equivalent and readings of this type cannot be mirrored.
    point_names: tuple[str, ...] = ()
    unit: str | None = None
    #: ``numeric`` | ``boolean`` | ``enum``. Drives :func:`coerce`.
    shape: str = "numeric"
    #: For ``enum``: the dictionary values this type may publish.
    enum_values: tuple[str, ...] = ()

    @property
    def mirrorable(self) -> bool:
        return bool(self.point_names)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "summary": self.summary,
            "point_names": list(self.point_names),
            "unit": self.unit,
            "shape": self.shape,
            "enum_values": list(self.enum_values),
            "mirrorable": self.mirrorable,
        }


#: The NetBotz sensor types this plugin understands.
#:
#: Ordering inside ``point_names`` is the mapping decision, and each one is
#: deliberate: the most specific dictionary point first, the asset-class-neutral
#: ``value`` last, because ``value`` carries no unit or semantics of its own and
#: is only right when the sensor *is* the asset.
SENSOR_TYPES: tuple[SensorType, ...] = (
    SensorType(
        key="temperature",
        summary="Temperature probe or integrated pod sensor",
        point_names=("temperature_air_c", "temperature_c", "value"),
        unit="°C",
    ),
    SensorType(
        key="humidity",
        summary="Relative humidity",
        point_names=("humidity_relative_pct", "value"),
        unit="%RH",
    ),
    SensorType(
        key="dew_point",
        summary="Dew point, computed by the appliance",
        point_names=("dew_point_c",),
        unit="°C",
    ),
    SensorType(
        key="door",
        summary="Rack or enclosure door contact",
        point_names=("door_state",),
        shape="enum",
        enum_values=("open", "closed", "ajar", "unknown"),
    ),
    SensorType(
        key="fluid",
        summary="Rope or spot fluid-detection sensor (NBES0308)",
        point_names=("leak_active", "alarm_active"),
        shape="boolean",
    ),
    SensorType(
        key="smoke",
        summary="Smoke sensor (NBES0307)",
        point_names=("smoke_active", "alarm_active"),
        shape="boolean",
    ),
    SensorType(
        key="alarm",
        summary="A pod's own alarm contact, where the sensor kind is not reported",
        point_names=("alarm_active", "fault_active"),
        shape="boolean",
    ),
    SensorType(
        key="battery",
        summary="Wireless sensor battery level",
        point_names=("battery_pct",),
        unit="%",
    ),
    SensorType(
        key="availability",
        summary="Whether the appliance is answering",
        point_names=("availability_state",),
        shape="enum",
        enum_values=("online", "offline", "degraded", "unknown"),
    ),
    # Listed and not mapped, on purpose. Each is a real NetBotz capability with
    # no point in data/point_dictionary.yaml; mirroring one would require
    # inventing a point, which is a design decision and not an integration's to
    # make. They surface as unmirrorable rather than disappearing.
    SensorType(key="airflow", summary="Airflow sensor -- no canonical point defined"),
    SensorType(key="audio", summary="Sound-level sensor -- no canonical point defined"),
    SensorType(key="vibration", summary="Vibration sensor -- no canonical point defined"),
    SensorType(key="camera_motion", summary="Camera motion detection -- no canonical point defined"),
)

SENSOR_TYPES_BY_KEY: dict[str, SensorType] = {sensor.key: sensor for sensor in SENSOR_TYPES}

#: Vendor spellings seen on NetBotz appliances and in their SNMP tables, mapped
#: to the keys above. Matching is done on a lowercased, underscore-normalised
#: form of whatever the appliance reports.
TYPE_ALIASES: dict[str, str] = {
    "temp": "temperature",
    "temperature_c": "temperature",
    "temperature_f": "temperature",
    "temperature_sensor": "temperature",
    "humi": "humidity",
    "humidity_sensor": "humidity",
    "relative_humidity": "humidity",
    "dewpoint": "dew_point",
    "dew_point_c": "dew_point",
    "door_switch": "door",
    "door_contact": "door",
    "door_sensor": "door",
    "leak": "fluid",
    "fluid_detector": "fluid",
    "rope_leak": "fluid",
    "spot_fluid": "fluid",
    "water": "fluid",
    "smoke_detector": "smoke",
    "smoke_sensor": "smoke",
    "dry_contact": "alarm",
    "alarm_contact": "alarm",
    "battery_level": "battery",
    "reachability": "availability",
    "comm_status": "availability",
    "air_flow": "airflow",
    "sound": "audio",
    "motion": "camera_motion",
}

#: Vendor door states -> the ``door_state`` enum.
DOOR_STATES: dict[str, str] = {
    "open": "open",
    "opened": "open",
    "closed": "closed",
    "close": "closed",
    "shut": "closed",
    "ajar": "ajar",
    "partially_open": "ajar",
}

#: Vendor availability states -> the ``availability_state`` enum.
AVAILABILITY_STATES: dict[str, str] = {
    "online": "online",
    "up": "online",
    "normal": "online",
    "ok": "online",
    "offline": "offline",
    "down": "offline",
    "unreachable": "offline",
    "degraded": "degraded",
    "warning": "degraded",
    "error": "degraded",
}

#: Vendor truthy/falsey spellings for boolean sensors.
TRUE_VALUES = frozenset({"1", "true", "yes", "on", "active", "alarm", "wet", "detected", "critical"})
FALSE_VALUES = frozenset({"0", "false", "no", "off", "inactive", "normal", "dry", "clear", "none"})


def normalise_key(raw: str | None) -> str:
    """Lowercase, underscore-separate and trim a vendor type string."""
    if not raw:
        return ""
    text = str(raw).strip().lower()
    for character in (" ", "-", ".", "/"):
        text = text.replace(character, "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def sensor_type_for(raw: str | None) -> SensorType | None:
    """Resolve a vendor type string to a :class:`SensorType`, or ``None``."""
    key = normalise_key(raw)
    if not key:
        return None
    if key in SENSOR_TYPES_BY_KEY:
        return SENSOR_TYPES_BY_KEY[key]
    alias = TYPE_ALIASES.get(key)
    return SENSOR_TYPES_BY_KEY.get(alias) if alias else None


def normalise_temperature(value: float, unit: str | None) -> float:
    """Convert a vendor temperature to Celsius.

    NetBotz appliances are configured in either scale and report the one they
    are set to. Every canonical temperature point in the dictionary is named
    ``*_c``, so a Fahrenheit reading mirrored verbatim is a silent error large
    enough to defeat freeze protection. Unknown or absent units are taken as
    Celsius, which is what the appliance ships as.
    """
    scale = normalise_key(unit)
    if scale in ("f", "degf", "deg_f", "fahrenheit", "°f"):
        return (float(value) - 32.0) * 5.0 / 9.0
    return float(value)


class CoercionError(ValueError):
    """A vendor value could not be shaped into what the point dictionary needs."""


def coerce(sensor: SensorType, value: Any, *, unit: str | None = None) -> Any:
    """Shape a vendor value for its canonical point.

    Raises :class:`CoercionError` rather than substituting a default. A leak
    sensor whose value cannot be read is not a leak sensor reading "dry"; the
    reading is dropped, counted and visible.
    """
    if value is None:
        raise CoercionError("the appliance reported no value")

    if sensor.shape == "boolean":
        return _as_bool(value)

    if sensor.shape == "enum":
        table = DOOR_STATES if sensor.key == "door" else AVAILABILITY_STATES
        text = normalise_key(str(value))
        mapped = table.get(text)
        if mapped is not None:
            return mapped
        if text in sensor.enum_values:
            return text
        # The dictionary defines "unknown" for both enums this plugin publishes,
        # so an unrecognised vendor state is recorded as genuinely unknown
        # rather than dead-lettered or guessed into "closed".
        return UNKNOWN

    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise CoercionError(f"{value!r} is not numeric, and {sensor.point_names[0]} is") from exc
    if sensor.key == "temperature":
        return normalise_temperature(numeric, unit)
    return numeric


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = normalise_key(str(value))
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise CoercionError(f"{value!r} is not a state this plugin knows how to read as true or false")
