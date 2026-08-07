"""MQTT topic conventions (SDD sections 10.1 and 26.2).

Canonical asset IDs have four dot-separated parts::

    <domain>.<asset_class>.<location_or_system>.<instance>
    energy.inverter.power_container.01

The MQTT projection of that identity is::

    homestead/<domain>/<location>/<asset_class>_<instance>/<point_name>
    homestead/energy/power_container/inverter_01/ac_output_kw

Topic construction is deterministic, but topic *parsing* is deliberately not the
identity mechanism: ``<asset_class>_<instance>`` cannot be split unambiguously
because both parts may contain underscores. SDD section 26.2 requires the
registry to map topic -> point_id. ``parse_topic`` therefore returns only a
structural hint used for diagnostics and dead-lettering, never for identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_BASE = "homestead"

#: Sub-topic namespaces that separate a point read from a write or an event.
KIND_TELEMETRY = "telemetry"
KIND_COMMAND = "cmd"
KIND_SETPOINT = "setpoint"
KIND_EVENT = "event"
KIND_ALARM = "alarm"
KIND_AVAILABILITY = "availability"
KIND_ACK = "ack"

_RESERVED_SEGMENTS = frozenset(
    {KIND_COMMAND, KIND_SETPOINT, KIND_EVENT, KIND_ALARM, KIND_AVAILABILITY, KIND_ACK}
)

ASSET_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_]*){3}$")
POINT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class TopicError(ValueError):
    """Raised when an asset ID or point name cannot be projected onto a topic."""


@dataclass(frozen=True)
class AssetIdParts:
    domain: str
    asset_class: str
    location: str
    instance: str

    @property
    def asset_segment(self) -> str:
        return f"{self.asset_class}_{self.instance}"


def split_asset_id(asset_id: str) -> AssetIdParts:
    """Split a canonical asset ID into its four parts."""
    if not ASSET_ID_RE.match(asset_id):
        raise TopicError(f"Asset ID {asset_id!r} does not match <domain>.<asset_class>.<location>.<instance>")
    domain, asset_class, location, instance = asset_id.split(".")
    return AssetIdParts(domain, asset_class, location, instance)


def asset_prefix(asset_id: str, base: str = DEFAULT_BASE) -> str:
    """``homestead/<domain>/<location>/<asset_class>_<instance>``."""
    parts = split_asset_id(asset_id)
    return f"{base}/{parts.domain}/{parts.location}/{parts.asset_segment}"


def _validate_point_name(point_name: str) -> str:
    if not POINT_NAME_RE.match(point_name):
        raise TopicError(f"Point name {point_name!r} must match {POINT_NAME_RE.pattern}")
    if point_name in _RESERVED_SEGMENTS:
        raise TopicError(f"Point name {point_name!r} collides with a reserved topic segment")
    return point_name


def telemetry_topic(asset_id: str, point_name: str, base: str = DEFAULT_BASE) -> str:
    return f"{asset_prefix(asset_id, base)}/{_validate_point_name(point_name)}"


def command_topic(asset_id: str, command_name: str, base: str = DEFAULT_BASE) -> str:
    return f"{asset_prefix(asset_id, base)}/{KIND_COMMAND}/{_validate_point_name(command_name)}"


def command_ack_topic(asset_id: str, command_name: str, base: str = DEFAULT_BASE) -> str:
    return f"{asset_prefix(asset_id, base)}/{KIND_COMMAND}/{_validate_point_name(command_name)}/{KIND_ACK}"


def setpoint_topic(asset_id: str, setpoint_name: str, base: str = DEFAULT_BASE) -> str:
    return f"{asset_prefix(asset_id, base)}/{KIND_SETPOINT}/{_validate_point_name(setpoint_name)}"


def event_topic(asset_id: str, event_name: str, base: str = DEFAULT_BASE) -> str:
    return f"{asset_prefix(asset_id, base)}/{KIND_EVENT}/{_validate_point_name(event_name)}"


def alarm_topic(asset_id: str, alarm_name: str, base: str = DEFAULT_BASE) -> str:
    return f"{asset_prefix(asset_id, base)}/{KIND_ALARM}/{_validate_point_name(alarm_name)}"


def availability_topic(asset_id: str, base: str = DEFAULT_BASE) -> str:
    """Last-will / availability topic for a device or gateway (SDD section 8.2)."""
    return f"{asset_prefix(asset_id, base)}/{KIND_AVAILABILITY}"


def point_id(asset_id: str, point_name: str) -> str:
    """Canonical point path ``<asset_id>/<point_name>`` (SDD section 26.2)."""
    return f"{asset_id}/{point_name}"


def split_point_id(value: str) -> tuple[str, str]:
    asset_id, sep, point_name = value.partition("/")
    if not sep:
        raise TopicError(f"Point ID {value!r} must be <asset_id>/<point_name>")
    return asset_id, point_name


# --- Subscription helpers -------------------------------------------------


def telemetry_subscription(base: str = DEFAULT_BASE) -> str:
    """Wildcard covering every asset point under the base topic."""
    return f"{base}/#"


def command_subscription(base: str = DEFAULT_BASE) -> str:
    return f"{base}/+/+/+/{KIND_COMMAND}/+"


@dataclass(frozen=True)
class ParsedTopic:
    """Structural view of a topic. Not an identity -- resolve via the registry."""

    base: str
    domain: str
    location: str
    asset_segment: str
    kind: str
    name: str | None

    @property
    def is_telemetry(self) -> bool:
        return self.kind == KIND_TELEMETRY


def parse_topic(topic: str, base: str = DEFAULT_BASE) -> ParsedTopic | None:
    """Best-effort structural parse. Returns ``None`` if the shape is unknown.

    Used for diagnostics and dead-letter messages only.
    """
    segments = topic.strip("/").split("/")
    if len(segments) < 5 or segments[0] != base:
        return None
    _, domain, location, asset_segment, *tail = segments

    if tail[0] == KIND_AVAILABILITY and len(tail) == 1:
        return ParsedTopic(base, domain, location, asset_segment, KIND_AVAILABILITY, None)
    if tail[0] in _RESERVED_SEGMENTS and len(tail) >= 2:
        # ``cmd/<name>`` and ``cmd/<name>/ack`` share the same kind namespace.
        kind = KIND_ACK if (tail[0] == KIND_COMMAND and tail[-1] == KIND_ACK) else tail[0]
        return ParsedTopic(base, domain, location, asset_segment, kind, tail[1])
    if len(tail) == 1:
        return ParsedTopic(base, domain, location, asset_segment, KIND_TELEMETRY, tail[0])
    return None
