"""Talking to a NetBotz appliance -- and being honest that this release does not.

The transport is a separate, injectable object for two reasons. The obvious one
is that it makes the mapping testable without an appliance. The other is that it
lets this release ship the integration *without* shipping a claim it cannot
back: :class:`UnconfiguredTransport` is a working object that reports exactly
what is missing and returns no readings, which is the same shape the
notification channels in :mod:`chaos.alarms.notify` take for email, push and
voice.

Nothing in this module ever reports a sensor value that did not come from a
device. An integration you believe in but that does not exist is worse than no
integration, because the temperature it is not reporting looks like a
temperature that is fine.

**Implementing the real transport.** Write a class satisfying
:class:`NetBotzTransport` and hand it to
:class:`~chaos.plugins.netbotz.NetBotzPlugin`. Two protocols are worth having:

``snmp``
    NetBotz appliances expose sensors through the PowerNet MIB. This needs an
    SNMP library (the platform has no SNMP dependency today), SNMPv3
    credentials, and a walk of the sensor tables to discover pods rather than a
    hard-coded OID list -- pod IDs move when hardware is added.
``http``
    Post-v3 NetBotz appliances serve a JSON sensor API over HTTPS. This needs
    the appliance's certificate to be trusted on the primary node, because
    SDD 15.3 puts management traffic on VLAN 10 and this platform does not turn
    verification off.

Both are reachable only from VLAN 10 (SDD 5.4, section 15.3), which is a
deployment fact the plugin cannot verify for you and should not pretend to.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: Transport kinds this plugin recognises in its ``transport`` option.
TRANSPORT_NONE = "none"
TRANSPORT_SNMP = "snmp"
TRANSPORT_HTTP = "http"
TRANSPORT_KINDS = (TRANSPORT_NONE, TRANSPORT_SNMP, TRANSPORT_HTTP)

#: What each unimplemented transport would need. Surfaced verbatim through the
#: plugin's health so the answer to "why is the NetBotz not mirroring" is on the
#: screen rather than in this file.
REQUIREMENTS: dict[str, str] = {
    TRANSPORT_SNMP: (
        "an SNMP client library (the platform declares no SNMP dependency), SNMPv3 "
        "credentials for the appliance, and a walk of the PowerNet sensor tables so pods "
        "are discovered rather than hard-coded"
    ),
    TRANSPORT_HTTP: (
        "the appliance's HTTPS sensor API enabled, its certificate trusted on this node, "
        "and an account with read access -- certificate verification is not negotiable"
    ),
    TRANSPORT_NONE: (
        "a transport: set CHAOS_PLUGIN_NETBOTZ_TRANSPORT to 'snmp' or 'http', with the "
        "host and credentials for the appliance"
    ),
}


@dataclass(frozen=True)
class NetBotzEnclosure:
    """One appliance, as it identifies itself."""

    enclosure_id: str
    label: str | None = None
    model: str | None = None
    firmware: str | None = None
    reachable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "enclosure_id": self.enclosure_id,
            "label": self.label,
            "model": self.model,
            "firmware": self.firmware,
            "reachable": self.reachable,
        }


@dataclass(frozen=True)
class NetBotzSensorReading:
    """One sensor value as the appliance reports it, before any mapping.

    ``sensor_id`` is the appliance's own address for the sensor and is what a
    ``point_bindings.source_address`` row is expected to hold.
    """

    sensor_id: str
    enclosure_id: str
    sensor_type: str
    value: Any
    unit: str | None = None
    label: str | None = None
    ts: dt.datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "enclosure_id": self.enclosure_id,
            "sensor_type": self.sensor_type,
            "value": self.value,
            "unit": self.unit,
            "label": self.label,
            "ts": self.ts.isoformat() if self.ts else None,
        }


@runtime_checkable
class NetBotzTransport(Protocol):
    """How the plugin reaches an appliance."""

    kind: str
    #: ``False`` means every :meth:`read` will return nothing, and the plugin
    #: reports ``not_configured`` rather than ``degraded``: nothing has broken,
    #: nothing has been set up.
    configured: bool

    def describe(self) -> dict[str, Any]:
        """Non-secret facts for the plugin's health and its API. Never credentials."""
        ...

    def enclosures(self) -> Sequence[NetBotzEnclosure]: ...

    def read(self) -> Sequence[NetBotzSensorReading]: ...


class UnconfiguredTransport:
    """A transport that has nothing to talk to, and says so.

    It is a real object with real behaviour -- returning nothing and explaining
    why -- rather than ``None`` guarded by ``if`` statements at each call site.
    """

    configured = False

    def __init__(self, kind: str = TRANSPORT_NONE, *, reason: str | None = None, host: str | None = None):
        self.kind = kind if kind in TRANSPORT_KINDS else TRANSPORT_NONE
        self.host = host
        self.requirement = REQUIREMENTS.get(self.kind, REQUIREMENTS[TRANSPORT_NONE])
        self.reason = reason or (
            f"The '{self.kind}' transport has no implementation in this release."
            if self.kind != TRANSPORT_NONE
            else "No NetBotz transport is selected."
        )

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "configured": False,
            "host": self.host,
            "reason": self.reason,
            "requirement": self.requirement,
        }

    def enclosures(self) -> Sequence[NetBotzEnclosure]:
        return ()

    def read(self) -> Sequence[NetBotzSensorReading]:
        return ()

    @property
    def detail(self) -> str:
        """One sentence for the plugin's health line."""
        return f"{self.reason} Required before it can deliver: {self.requirement}."


def build_transport(kind: str | None, host: str | None) -> NetBotzTransport:
    """Choose a transport from configuration.

    Every branch currently produces :class:`UnconfiguredTransport`. That is the
    honest state of this release, and it is written as a dispatch rather than a
    constant so that adding a real transport is one line here and nothing
    anywhere else.
    """
    selected = (kind or TRANSPORT_NONE).strip().lower()
    if selected not in TRANSPORT_KINDS:
        return UnconfiguredTransport(
            TRANSPORT_NONE,
            host=host,
            reason=f"Unknown NetBotz transport {selected!r}; expected one of {', '.join(TRANSPORT_KINDS)}.",
        )
    if selected != TRANSPORT_NONE and not host:
        return UnconfiguredTransport(
            selected,
            reason=f"The '{selected}' transport was selected but no host was given.",
        )
    return UnconfiguredTransport(selected, host=host)
