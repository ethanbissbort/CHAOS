"""Mirroring a third-party system into the canonical model.

A vendor appliance -- a NetBotz, a UPS card, a PDU, a weather station -- knows
its own sensors by its own names. Mirroring is the act of making those readings
appear in CHAOS as ordinary telemetry on canonical points, so that every
downstream feature the platform already has (current state, historian, alarm
evaluation, staleness, the annunciator, blast-radius analysis) works on vendor
data without a single one of them learning the vendor's name.

**Identity is resolved through the registry, never inferred.** SDD 26.2 is
explicit that parsing a vendor string is not an identity mechanism, so a reading
becomes a point in exactly two registry-backed ways:

1. **By binding address.** ``point_bindings.source_protocol`` names the
   integration and ``point_bindings.source_address`` holds the vendor address.
   This is per-point, exact, and already part of the v0.3 schema. It wins.
2. **By external identifier plus canonical point name.** An
   ``external_identifiers`` row maps a vendor device (``id_type`` =
   ``netbotz.enclosure``, value = the appliance's own ID) to an ``asset_id``;
   the reading then names a canonical point on that asset. This is how a whole
   appliance is mirrored without hand-writing a binding row per sensor.

A reading that resolves to neither is **dropped and counted**. It is never
written under a guessed identity, and it never creates a point. Commissioning
resolves it; the mirror does not.

**The mirror publishes; it does not write.** Each resolved reading is published
to the bus as a standard :class:`~chaos.envelope.TelemetryEnvelope`, on the same
topic ingest would resolve that point on. Ingest then applies its own rules --
enum and range validation, quality, out-of-order detection, historian policy,
dead-lettering. That keeps exactly one write path into the historian, means a
vendor integration can be no more trusted than a field device, and lets the
mirror run on a node that is not the one holding the database.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from chaos import topics as topic_utils
from chaos.config import Settings
from chaos.envelope import TelemetryEnvelope
from chaos.ingest.resolver import TopicResolver
from chaos.models.base import utcnow
from chaos.models.registry import ExternalIdentifier, Point, PointBinding
from chaos.mqtt import MessageBus

logger = logging.getLogger(__name__)

#: Bounds on a source's declared poll interval. A plugin asking for 0.05s would
#: turn a vendor appliance into a denial-of-service target; one asking for a day
#: is almost certainly a units mistake.
MIN_POLL_INTERVAL_S = 1.0
MAX_POLL_INTERVAL_S = 3600.0

#: Why a reading did not become telemetry. Every drop lands in exactly one of
#: these, so "the NetBotz is mirroring nothing" has an answer on a screen.
DROP_UNRESOLVED = "unresolved_identity"
DROP_NO_TOPIC = "no_mqtt_projection"
DROP_INVALID = "invalid_reading"
DROP_REASONS = (DROP_UNRESOLVED, DROP_NO_TOPIC, DROP_INVALID)

#: Addresses the design package uses to mean "not commissioned yet". The v0.3
#: register carries ``source_address: TBD`` on every SNMP binding, and treating
#: that as a real vendor address would make six UPS points collide on one key
#: and would claim a binding that field work has not done. They are skipped, so
#: an uncommissioned point resolves to nothing and says so.
PLACEHOLDER_ADDRESSES = frozenset({"tbd", "t.b.d.", "n/a", "na", "none", "null", "unknown", "-", "?"})


@dataclass(frozen=True)
class MirrorReading:
    """One value as the vendor system reports it.

    A source fills in whichever identity route it can support. ``external_id``
    is the vendor address matched against a binding; ``device_id`` +
    ``point_name`` is the external-identifier route. Supplying both is fine and
    is the normal case for an appliance whose sensors are also individually
    bound -- the binding wins.
    """

    value: Any
    external_id: str | None = None
    device_id: str | None = None
    point_name: str | None = None
    #: Canonical point names to try after :attr:`point_name`, in order. One
    #: vendor sensor means different canonical points depending on what it is
    #: attached to -- a NetBotz temperature probe is ``temperature_air_c`` on an
    #: ``environmental_monitor`` and ``value`` on a ``safety_sensor`` -- and a
    #: source cannot know which without reading the registry, which is not its
    #: job. It offers the candidates; the registry picks.
    alternate_point_names: tuple[str, ...] = ()
    ts: dt.datetime | None = None
    unit: str | None = None
    quality: str = "good"
    #: Free-form vendor context kept for diagnostics. It never reaches the
    #: envelope: the historian records what a point read, not what a vendor
    #: called it.
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """How this reading is named in a drop report."""
        if self.external_id:
            return self.external_id
        if self.device_id and self.point_name:
            return f"{self.device_id}/{self.point_name}"
        return repr(self.value)[:60]


@runtime_checkable
class MirrorSource(Protocol):
    """One vendor system, or one device within it, offered for mirroring."""

    #: Unique within its plugin; ``<plugin>.<name>`` identifies it platform-wide.
    name: str
    #: The plugin that contributed this source.
    plugin: str
    #: Matched against ``point_bindings.source_protocol``.
    protocol: str
    #: Matched against ``external_identifiers.id_type``.
    id_type: str
    #: Seconds between polls, clamped to [MIN_POLL_INTERVAL_S, MAX_POLL_INTERVAL_S].
    poll_interval_s: float

    def available(self) -> bool:
        """Whether a poll would do anything. A source with no transport says no."""
        ...

    def read(self) -> Iterable[MirrorReading]:
        """Return the current readings. May raise; the engine isolates it."""
        ...


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """What the registry made of one reading."""

    point_id: str | None
    route: str | None = None  # "binding" | "external_identifier"
    reason: str | None = None


class IdentityMap:
    """Vendor address -> canonical ``point_id``, built from the registry.

    Rebuilt wholesale by :meth:`refresh`, for the same reason
    :class:`~chaos.ingest.resolver.TopicResolver` is: the registry loader is a
    batch operation and incremental invalidation would add risk for no gain.
    """

    def __init__(self) -> None:
        self._by_address: dict[tuple[str, str], str] = {}
        self._device_to_asset: dict[tuple[str, str], str] = {}
        self._known_points: set[str] = set()
        self.counts: dict[str, int] = {"bindings": 0, "external_identifiers": 0, "points": 0}

    def refresh(self, session: Session) -> dict[str, int]:
        by_address: dict[tuple[str, str], str] = {}
        device_to_asset: dict[tuple[str, str], str] = {}

        known_points = set(session.execute(select(Point.point_id)).scalars().all())

        binding_rows = session.execute(
            select(PointBinding.point_id, PointBinding.source_protocol, PointBinding.source_address).where(
                PointBinding.source_protocol.is_not(None), PointBinding.source_address.is_not(None)
            )
        ).all()
        skipped_placeholders = 0
        for point_id, protocol, address in binding_rows:
            key = _key(protocol, address)
            if key is None:
                continue
            if key[1].lower() in PLACEHOLDER_ADDRESSES:
                skipped_placeholders += 1
                continue
            if point_id not in known_points:
                logger.warning("Mirror binding for unknown point %s ignored", point_id)
                continue
            # First row wins, so a duplicated vendor address cannot silently
            # steal another point's readings; the duplicate is a commissioning
            # fault and is logged as one.
            if key in by_address and by_address[key] != point_id:
                logger.warning(
                    "Vendor address %s/%s is bound to both %s and %s; keeping %s",
                    key[0],
                    key[1],
                    by_address[key],
                    point_id,
                    by_address[key],
                )
                continue
            by_address[key] = point_id

        identifier_rows = session.execute(
            select(ExternalIdentifier.id_type, ExternalIdentifier.value, ExternalIdentifier.asset_id)
        ).all()
        for id_type, value, asset_id in identifier_rows:
            key = _key(id_type, value)
            if key is None:
                continue
            device_to_asset.setdefault(key, asset_id)

        self._by_address = by_address
        self._device_to_asset = device_to_asset
        self._known_points = known_points
        self.counts = {
            "bindings": len(by_address),
            "external_identifiers": len(device_to_asset),
            "points": len(known_points),
            "placeholder_addresses": skipped_placeholders,
        }
        logger.info(
            "Mirror identity map refreshed: %d vendor address(es), %d external identifier(s)",
            len(by_address),
            len(device_to_asset),
        )
        return dict(self.counts)

    def resolve(self, source: MirrorSource, reading: MirrorReading) -> Resolution:
        """Resolve one reading, binding route first."""
        key = _key(source.protocol, reading.external_id)
        if key is not None:
            point_id = self._by_address.get(key)
            if point_id is not None:
                return Resolution(point_id, route="binding")

        device_key = _key(source.id_type, reading.device_id)
        candidates = tuple(
            name.strip()
            for name in (reading.point_name, *reading.alternate_point_names)
            if name and name.strip()
        )
        if device_key is not None and candidates:
            asset_id = self._device_to_asset.get(device_key)
            if asset_id is not None:
                for candidate in candidates:
                    point_id = topic_utils.point_id(asset_id, candidate)
                    if point_id in self._known_points:
                        return Resolution(point_id, route="external_identifier")
                return Resolution(
                    None,
                    reason=(
                        f"{device_key[1]} resolves to asset {asset_id}, which has none of the "
                        f"candidate points {list(candidates)} in the registry"
                    ),
                )

        return Resolution(
            None,
            reason=(
                f"no point_bindings row with source_protocol='{source.protocol}' and "
                f"source_address='{reading.external_id}', and no external_identifiers row of "
                f"type '{source.id_type}' for '{reading.device_id}'"
            ),
        )

    def __len__(self) -> int:
        return len(self._by_address) + len(self._device_to_asset)


def _key(namespace: str | None, value: str | None) -> tuple[str, str] | None:
    if not namespace or not value:
        return None
    namespace = namespace.strip().lower()
    value = value.strip()
    if not namespace or not value:
        return None
    return (namespace, value)


def clamp_interval(value: float) -> float:
    return max(MIN_POLL_INTERVAL_S, min(MAX_POLL_INTERVAL_S, float(value)))


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


@dataclass
class SourceState:
    """Per-source bookkeeping, and the material for its row on the screen."""

    source: MirrorSource
    interval_s: float
    next_due_at: float = 0.0
    last_polled_at: dt.datetime | None = None
    last_error: str | None = None
    polls: int = 0
    readings: int = 0
    published: int = 0
    drops: dict[str, int] = field(default_factory=lambda: dict.fromkeys(DROP_REASONS, 0))
    #: Vendor addresses seen that the registry could not place. Bounded, because
    #: a misconfigured appliance would otherwise grow this without limit.
    unresolved: dict[str, str] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return f"{self.source.plugin}.{self.source.name}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.qualified_name,
            "plugin": self.source.plugin,
            "source": self.source.name,
            "protocol": self.source.protocol,
            "id_type": self.source.id_type,
            "poll_interval_s": self.interval_s,
            "available": _safe_available(self.source),
            "last_polled_at": self.last_polled_at.isoformat() if self.last_polled_at else None,
            "last_error": self.last_error,
            "polls": self.polls,
            "readings": self.readings,
            "published": self.published,
            "drops": dict(self.drops),
            "unresolved_examples": dict(self.unresolved),
        }


#: How many distinct unresolved vendor addresses to remember per source. Enough
#: to commission from; small enough that a broken appliance cannot fill memory.
MAX_UNRESOLVED_TRACKED = 25


class MirrorService:
    """Background service: poll every mirror source, publish canonical telemetry.

    One thread serves every source. Sources are polled on their own intervals;
    a slow or failing source delays only itself in the same pass and never
    prevents the others from being polled again.
    """

    name = "plugin-mirror"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        bus: MessageBus,
        settings: Settings,
        sources: Sequence[MirrorSource] = (),
        *,
        clock: Callable[[], dt.datetime] = utcnow,
        monotonic: Callable[[], float] | None = None,
        auto_poll: bool = True,
        resolver: TopicResolver | None = None,
        identity: IdentityMap | None = None,
    ) -> None:
        import time as _time

        self.session_factory = session_factory
        self.bus = bus
        self.settings = settings
        self.clock = clock
        self.monotonic = monotonic or _time.monotonic
        self.resolver = resolver or TopicResolver(settings.mqtt_base_topic)
        self.identity = identity or IdentityMap()
        self._auto_poll = auto_poll
        self._running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.counters: dict[str, int] = {
            "passes": 0,
            "polls": 0,
            "readings": 0,
            "published": 0,
            "source_errors": 0,
            "refreshes": 0,
            **{f"dropped_{reason}": 0 for reason in DROP_REASONS},
        }
        self.states: dict[str, SourceState] = {}
        for source in sources:
            self.add_source(source)

    # -- composition -----------------------------------------------------

    def add_source(self, source: MirrorSource) -> SourceState:
        state = SourceState(
            source=source, interval_s=clamp_interval(getattr(source, "poll_interval_s", 30.0))
        )
        if state.qualified_name in self.states:
            raise ValueError(f"Duplicate mirror source: {state.qualified_name}")
        self.states[state.qualified_name] = state
        return state

    @property
    def sources(self) -> list[MirrorSource]:
        return [state.source for state in self.states.values()]

    #: The shortest interval any source asked for, which is how often the loop
    #: needs to wake to honour every source's cadence.
    @property
    def tick_interval_s(self) -> float:
        if not self.states:
            return MIN_POLL_INTERVAL_S
        return min(state.interval_s for state in self.states.values())

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self.refresh_identity()
        self._running = True
        self._stop_event.clear()
        if self._auto_poll:
            self._thread = threading.Thread(target=self._loop, name="plugin-mirror", daemon=True)
            self._thread.start()
        logger.info(
            "Mirror engine running with %d source(s) over %d resolvable vendor address(es)",
            len(self.states),
            len(self.identity),
        )

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(2.0, self.tick_interval_s))
        logger.info("Mirror engine stopped after %d published reading(s)", self.counters["published"])

    @property
    def running(self) -> bool:
        return self._running

    def refresh_identity(self) -> None:
        """Rebuild vendor-address resolution and topic projection from the registry."""
        with self.session_factory() as session:
            self.identity.refresh(session)
            self.resolver.refresh(session)
        self.counters["refreshes"] += 1

    # -- polling ---------------------------------------------------------

    def poll_once(self, *, force: bool = False) -> int:
        """Poll every source that is due. Returns the number published.

        ``force`` ignores the per-source schedule, which is what tests and the
        manual "poll now" path want.
        """
        now = self.monotonic()
        published = 0
        for state in self.states.values():
            if not force and now < state.next_due_at:
                continue
            state.next_due_at = now + state.interval_s
            published += self._poll_source(state)
        self.counters["passes"] += 1
        return published

    def _poll_source(self, state: SourceState) -> int:
        source = state.source
        state.polls += 1
        self.counters["polls"] += 1
        state.last_polled_at = self.clock()

        if not _safe_available(source):
            # Not an error. A source with no configured transport says so on
            # every pass, and the plugin's health explains why.
            state.last_error = None
            return 0

        try:
            readings = list(source.read())
        except Exception as exc:
            logger.exception("Mirror source %s failed to read", state.qualified_name)
            state.last_error = f"{type(exc).__name__}: {_short(exc)}"
            self.counters["source_errors"] += 1
            return 0

        state.last_error = None
        state.readings += len(readings)
        self.counters["readings"] += len(readings)

        published = 0
        for reading in readings:
            if self._publish(state, reading):
                published += 1
        state.published += published
        return published

    def _publish(self, state: SourceState, reading: MirrorReading) -> bool:
        resolution = self.identity.resolve(state.source, reading)
        if resolution.point_id is None:
            self._drop(state, DROP_UNRESOLVED, reading.label, resolution.reason or "unresolved")
            return False

        topic = self.resolver.topic_for_point(resolution.point_id)
        if topic is None:
            self._drop(
                state,
                DROP_NO_TOPIC,
                reading.label,
                f"{resolution.point_id} has no MQTT projection, so mirrored telemetry has nowhere to go",
            )
            return False

        asset_id, point_name = topic_utils.split_point_id(resolution.point_id)
        try:
            envelope = TelemetryEnvelope(
                ts=reading.ts or self.clock(),
                asset_id=asset_id,
                point=point_name,
                value=reading.value,
                unit=reading.unit,
                quality=reading.quality,
                source=f"mirror.{state.qualified_name}",
            )
        except ValueError as exc:
            # A bad quality code or an unserialisable value. The vendor system
            # is wrong, not the platform; say which reading and carry on.
            self._drop(state, DROP_INVALID, reading.label, _short(exc))
            return False

        self.bus.publish(topic, envelope.to_payload())
        self.counters["published"] += 1
        return True

    def _drop(self, state: SourceState, reason: str, label: str, detail: str) -> None:
        state.drops[reason] = state.drops.get(reason, 0) + 1
        self.counters[f"dropped_{reason}"] = self.counters.get(f"dropped_{reason}", 0) + 1
        if reason == DROP_UNRESOLVED and len(state.unresolved) < MAX_UNRESOLVED_TRACKED:
            state.unresolved.setdefault(label, detail)
        logger.debug("Mirror %s dropped %s (%s): %s", state.qualified_name, label, reason, detail)

    def _loop(self) -> None:  # pragma: no cover - exercised via start()/stop()
        while not self._stop_event.wait(self.tick_interval_s):
            try:
                self.poll_once()
            except Exception:
                logger.exception("Mirror pass failed")

    # -- introspection ---------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": self._running,
            "tick_interval_s": self.tick_interval_s,
            "counters": dict(self.counters),
            "identity": dict(self.identity.counts),
            "sources": [state.as_dict() for state in self.states.values()],
        }


def _safe_available(source: MirrorSource) -> bool:
    try:
        return bool(source.available())
    except Exception:
        logger.exception("Mirror source %s.%s failed its availability check", source.plugin, source.name)
        return False


def _short(exc: Exception, limit: int = 160) -> str:
    return " ".join(str(exc).split())[:limit]
