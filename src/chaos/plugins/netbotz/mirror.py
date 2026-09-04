"""The NetBotz mirror source: appliance readings -> canonical readings.

This is the only place the vendor's model and the canonical model meet, and it
is deliberately thin. It reads whatever the transport gives it, asks
:mod:`chaos.plugins.netbotz.sensors` what each reading could be, and hands the
candidates to the mirror engine, which asks the registry. It resolves nothing
itself and writes nothing.

Every reading is offered by **both** identity routes at once:

* ``external_id`` -- the appliance's own sensor address, for a
  ``point_bindings`` row that names it. This is what commissioning fills in, and
  it wins.
* ``device_id`` + candidate point names -- for an ``external_identifiers`` row
  mapping the appliance to an asset, which mirrors a whole appliance without a
  binding row per sensor.

Offering both costs nothing and means the integration works before *and* after
per-point commissioning, which is the whole span during which somebody wants to
see whether the rack is getting warm.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from chaos.plugins.mirror import MirrorReading, clamp_interval
from chaos.plugins.netbotz import sensors as sensor_map
from chaos.plugins.netbotz.client import NetBotzSensorReading, NetBotzTransport

logger = logging.getLogger(__name__)

#: ``point_bindings.source_protocol`` value this source matches. The v0.3
#: register already uses bare ``snmp`` for the UPS; NetBotz gets its own value
#: so that two SNMP integrations on the same property cannot claim each other's
#: addresses.
PROTOCOL = "netbotz"

#: ``external_identifiers.id_type`` that maps an appliance to an asset.
ID_TYPE = "netbotz.enclosure"

#: NetBotz pods report on the order of tens of seconds; polling faster reads the
#: same value repeatedly and gives the appliance work to do for nothing.
DEFAULT_POLL_INTERVAL_S = 30.0


class NetBotzMirrorSource:
    """One appliance (or one transport covering several), offered for mirroring."""

    plugin = "netbotz"
    protocol = PROTOCOL
    id_type = ID_TYPE

    def __init__(
        self,
        transport: NetBotzTransport,
        *,
        name: str = "sensors",
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.transport = transport
        self.name = name
        self.poll_interval_s = clamp_interval(poll_interval_s)
        self.counters: dict[str, int] = {
            "readings_seen": 0,
            "unknown_sensor_type": 0,
            "unmirrorable_sensor_type": 0,
            "uncoercible_value": 0,
        }
        #: Vendor sensor types seen that this plugin has no mapping for. A gap in
        #: the mapping should be visible as a gap, not as silence.
        self.unmapped_types: dict[str, int] = {}

    def available(self) -> bool:
        return bool(getattr(self.transport, "configured", False))

    def read(self) -> Iterable[MirrorReading]:
        readings: Sequence[NetBotzSensorReading] = self.transport.read()
        produced: list[MirrorReading] = []
        for reading in readings:
            self.counters["readings_seen"] += 1
            translated = self.translate(reading)
            if translated is not None:
                produced.append(translated)
        return produced

    def translate(self, reading: NetBotzSensorReading) -> MirrorReading | None:
        """Map one appliance reading, or explain why it cannot be mapped."""
        sensor = sensor_map.sensor_type_for(reading.sensor_type)
        if sensor is None:
            self.counters["unknown_sensor_type"] += 1
            self._note_unmapped(reading.sensor_type)
            logger.debug("NetBotz sensor type %r has no mapping", reading.sensor_type)
            return None

        if not sensor.mirrorable:
            # A real NetBotz capability with no canonical point. Counted rather
            # than mapped onto an approximate point: see sensors.SENSOR_TYPES.
            self.counters["unmirrorable_sensor_type"] += 1
            self._note_unmapped(sensor.key)
            return None

        try:
            value = sensor_map.coerce(sensor, reading.value, unit=reading.unit)
        except sensor_map.CoercionError as exc:
            self.counters["uncoercible_value"] += 1
            logger.warning("NetBotz sensor %s: %s", reading.sensor_id, exc)
            return None

        return MirrorReading(
            value=value,
            external_id=reading.sensor_id,
            device_id=reading.enclosure_id,
            point_name=sensor.point_names[0],
            alternate_point_names=sensor.point_names[1:],
            ts=reading.ts,
            unit=sensor.unit,
            detail={"sensor_type": sensor.key, "label": reading.label},
        )

    def _note_unmapped(self, key: str | None) -> None:
        normalised = sensor_map.normalise_key(key) or "(unnamed)"
        self.unmapped_types[normalised] = self.unmapped_types.get(normalised, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "plugin": self.plugin,
            "protocol": self.protocol,
            "id_type": self.id_type,
            "poll_interval_s": self.poll_interval_s,
            "available": self.available(),
            "transport": self.transport.describe(),
            "counters": dict(self.counters),
            "unmapped_sensor_types": dict(self.unmapped_types),
        }
