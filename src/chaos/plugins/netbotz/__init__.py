"""APC NetBotz environmental monitoring -- the reference plugin.

The v0.3 design package puts four NetBotz-family appliances and four NetBotz
sensor pods in the rack and the power container: a NetBotz 500 on the container
wall, an NBRK0550 rack monitor, an AP9320 EMU, and smoke, fluid and
temperature/humidity pods. SDD FR-800 asks for their data. This plugin is how
that data becomes canonical telemetry -- and it is also the worked example every
other integration should be read against, because it exercises the whole plugin
surface:

* ``api`` -- ``/api/v1/ext/netbotz``, including the commissioning view that
  distinguishes "the appliance is unreachable" from "nothing is bound to it".
* ``mirror`` -- appliance sensors resolved through the registry and published as
  standard telemetry envelopes.
* health -- one honest sentence about whether any of it is working.

**What this release actually does.** The mapping, the identity routes, the API
and the health reporting are complete and tested. The network transport is not:
:class:`~chaos.plugins.netbotz.client.UnconfiguredTransport` returns no readings
and reports what implementing SNMP or HTTPS would require. The plugin therefore
reports ``not_configured`` on a real deployment and never reports a temperature
that did not come from a device. See
:mod:`chaos.plugins.netbotz.client` for what a real transport needs.

Configuration, all optional, all through the environment::

    CHAOS_PLUGIN_NETBOTZ_TRANSPORT=snmp        # none | snmp | http
    CHAOS_PLUGIN_NETBOTZ_HOST=10.10.10.40      # VLAN 10, per SDD 15.3
    CHAOS_PLUGIN_NETBOTZ_POLL_INTERVAL_S=30
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from chaos.plugins.netbotz.client import (
    TRANSPORT_NONE,
    NetBotzTransport,
    build_transport,
)
from chaos.plugins.netbotz.mirror import DEFAULT_POLL_INTERVAL_S, ID_TYPE, PROTOCOL, NetBotzMirrorSource
from chaos.plugins.spec import PluginBase, PluginContext, PluginHealth, PluginManifest

if TYPE_CHECKING:  # pragma: no cover - imports for typing only
    from fastapi import APIRouter

    from chaos.plugins.mirror import MirrorSource

MANIFEST = PluginManifest(
    name="netbotz",
    version="0.1.0",
    summary="Mirrors APC NetBotz environmental sensors into canonical points",
    vendor="APC by Schneider Electric",
    capabilities=("api", "mirror"),
    # Environmental monitoring is exactly what the secondary control node is for
    # (SDD 16.1): it keeps observing and alerting when the power container --
    # which is where the NetBotz 500 is mounted -- is what has been lost.
    node_roles=("primary", "secondary"),
    required_options=("host",),
    docs="plugins.html",
)


class NetBotzPlugin(PluginBase):
    """The plugin object discovered as ``PLUGIN``."""

    manifest = MANIFEST

    def __init__(
        self,
        transport_factory: Callable[[PluginContext], NetBotzTransport] | None = None,
    ) -> None:
        super().__init__()
        # Injectable so the mapping can be exercised against a scripted
        # appliance without one existing, and so a site can supply its own
        # transport without forking the mapping.
        self._transport_factory = transport_factory or _default_transport
        self.transport: NetBotzTransport = build_transport(TRANSPORT_NONE, None)
        self._sources: tuple[NetBotzMirrorSource, ...] = ()

    def setup(self, context: PluginContext) -> None:
        super().setup(context)
        self.transport = self._transport_factory(context)
        self._sources = (
            NetBotzMirrorSource(
                self.transport,
                poll_interval_s=context.float_option("poll_interval_s", DEFAULT_POLL_INTERVAL_S),
            ),
        )

    def routers(self) -> Sequence[APIRouter]:
        from chaos.plugins.netbotz.api import build_router

        return (build_router(self),)

    def mirror_sources(self) -> Sequence[MirrorSource]:
        return self._sources

    def health(self) -> PluginHealth:
        """What the transport can actually do, not what is configured.

        The base implementation would report ``ok`` as soon as ``host`` is set.
        That would be a lie in this release: a host is not a connection, and the
        distinction is the entire point of the health field.
        """
        if self.context is None:  # pragma: no cover - manager always calls setup()
            return PluginHealth.failed("setup() has not run, so the plugin has no configuration")

        facts = {
            "transport": self.transport.kind,
            "protocol": PROTOCOL,
            "id_type": ID_TYPE,
            "sources": len(self._sources),
        }

        if not self.transport.configured:
            detail = getattr(self.transport, "detail", None) or "The transport is not configured."
            return PluginHealth.not_configured(detail, **facts)

        try:
            enclosures = list(self.transport.enclosures())
        except Exception as exc:
            return PluginHealth.degraded(
                f"The transport failed to enumerate appliances: {type(exc).__name__}: {exc}", **facts
            )

        unreachable = [item.enclosure_id for item in enclosures if not item.reachable]
        if unreachable:
            return PluginHealth.degraded(
                "Configured, but not answering: " + ", ".join(sorted(unreachable)),
                unreachable=sorted(unreachable),
                **facts,
            )
        return PluginHealth.ok(
            f"{len(enclosures)} appliance(s) reachable", enclosures=len(enclosures), **facts
        )


def _default_transport(context: PluginContext) -> NetBotzTransport:
    return build_transport(context.option("transport", TRANSPORT_NONE), context.option("host"))


#: What :mod:`chaos.plugins.manager` discovers.
PLUGIN = NetBotzPlugin()

__all__ = ["MANIFEST", "PLUGIN", "NetBotzPlugin"]
