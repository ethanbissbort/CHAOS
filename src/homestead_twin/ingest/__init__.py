"""Telemetry ingest subsystem.

The ingest path turns MQTT traffic into authoritative platform state:

``bus message`` -> :class:`~homestead_twin.ingest.resolver.TopicResolver`
-> :class:`~homestead_twin.ingest.writer.TelemetryWriter`
-> ``current_state`` (SDD 40.2) + ``telemetry_samples`` (SDD 40.1).

Two rules shape every module here:

1. **The registry owns identity.** A topic is resolved to a ``point_id`` through
   the registry (``point_bindings.mqtt_topic``, or the deterministic projection
   of a registered point). ``topics.parse_topic`` is used only to classify a
   topic and to describe failures (SDD 26.2).
2. **Nothing is dropped silently.** Anything that cannot be resolved, parsed or
   trusted becomes an :class:`~homestead_twin.models.telemetry.IngestDeadLetter`
   with a specific reason, because a silently discarded message during
   commissioning looks exactly like a dead sensor (SDD 19).
"""

from homestead_twin.ingest.resolver import ResolverStats, TopicResolver
from homestead_twin.ingest.retention import apply_retention, downsample
from homestead_twin.ingest.service import IngestService
from homestead_twin.ingest.writer import (
    AvailabilityResult,
    PointMeta,
    Problem,
    TelemetryWriter,
    WriteResult,
    as_utc,
)

__all__ = [
    "AvailabilityResult",
    "IngestService",
    "PointMeta",
    "Problem",
    "ResolverStats",
    "TelemetryWriter",
    "TopicResolver",
    "WriteResult",
    "apply_retention",
    "as_utc",
    "downsample",
]
