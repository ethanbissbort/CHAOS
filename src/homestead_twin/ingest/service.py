"""The MQTT ingest subscriber (SDD 8.1, 8.2, 26.2).

One subscription to ``<base>/#`` carries everything the property publishes. The
service classifies each topic, resolves identity through the registry, and hands
telemetry to :class:`~homestead_twin.ingest.writer.TelemetryWriter`:

===========================  ==============================================
``.../cmd/<name>``           ignored -- the command subsystem owns dispatch
``.../cmd/<name>/ack``       ignored -- ditto
``.../setpoint/<name>``      ignored -- ditto
``.../availability``         last-will handling (SDD 8.2)
``.../event/<name>``         event point materialised as current state
``.../alarm/<name>``         alarm point materialised; lifecycle is the alarm
                             engine's job, not ingest's
``.../<point_name>``         telemetry
===========================  ==============================================

The subscriber never raises. A malformed payload, an unregistered topic or a
failing write becomes an ``IngestDeadLetter`` with a specific reason, written in
its own transaction so a rolled-back ingest still leaves the evidence behind. A
single bad publisher must not be able to stop telemetry for the whole property.

A daemon thread runs the staleness sweep (SDD 5.5): a value nobody is refreshing
must stop presenting itself as ``good``. The interval and the clock are both
injectable so tests never sleep.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from homestead_twin import topics as topic_utils
from homestead_twin.config import Settings
from homestead_twin.envelope import (
    EventEnvelope,
    TelemetryEnvelope,
    parse_availability,
    parse_telemetry,
)
from homestead_twin.models.base import utcnow
from homestead_twin.models.telemetry import IngestDeadLetter
from homestead_twin.mqtt import Message, MessageBus, topic_matches
from homestead_twin.ingest.resolver import TopicResolver
from homestead_twin.ingest.writer import TelemetryWriter

logger = logging.getLogger(__name__)

#: Topic kinds owned by the command subsystem.
_CONTROL_KINDS = frozenset(
    {topic_utils.KIND_COMMAND, topic_utils.KIND_SETPOINT, topic_utils.KIND_ACK}
)

#: How much of a rejected payload to keep. Enough to diagnose, bounded so a
#: chatty broken device cannot fill the disk.
MAX_DEAD_LETTER_PAYLOAD = 2000

#: Bounds on the derived sweep interval when settings do not name one.
MIN_SWEEP_INTERVAL_S = 1.0
MAX_SWEEP_INTERVAL_S = 60.0


class IngestService:
    """Background service: MQTT -> registry-resolved identity -> database."""

    name = "ingest"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        bus: MessageBus,
        settings: Settings,
        *,
        sweep_interval_s: float | None = None,
        clock: Callable[[], dt.datetime] = utcnow,
        auto_sweep: bool = True,
        resolver: TopicResolver | None = None,
        writer: TelemetryWriter | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.bus = bus
        self.settings = settings
        self.clock = clock
        self.base = settings.mqtt_base_topic
        self.resolver = resolver or TopicResolver(self.base)
        self.writer = writer or TelemetryWriter(settings, session_factory=session_factory)
        # Half the default point timeout: fast enough that a dead sensor is
        # flagged within roughly one timeout, cheap enough to be irrelevant.
        self.sweep_interval_s = (
            sweep_interval_s
            if sweep_interval_s is not None
            else _clamp(settings.default_stale_after_s / 2)
        )
        self._auto_sweep = auto_sweep
        self._running = False
        self._subscribed: set[str] = set()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.counters: dict[str, int] = {
            "messages_received": 0,
            "messages_written": 0,
            "dead_lettered": 0,
            "out_of_order": 0,
            "stale_marked": 0,
            "sequence_gaps": 0,
            "ignored_control": 0,
            "ignored_empty": 0,
            "availability_received": 0,
            "points_marked_offline": 0,
            "events_received": 0,
            "unresolved_topics": 0,
            "invalid_payloads": 0,
            "registry_refreshes": 0,
            "sweeps": 0,
            "handler_errors": 0,
        }
        self.last_message_at: dt.datetime | None = None
        self.last_error: str | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self.refresh_registry()
        # Set running before subscribing: a broker replays retained messages on
        # subscribe, and those are real current state.
        self._running = True
        self._stop_event.clear()
        self._ensure_subscriptions()
        if self._auto_sweep:
            self._thread = threading.Thread(
                target=self._sweep_loop, name="ingest-staleness-sweep", daemon=True
            )
            self._thread.start()
        logger.info(
            "Ingest listening on %s/# with %d resolvable topic(s)", self.base, len(self.resolver)
        )

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(2.0, self.sweep_interval_s))
        logger.info("Ingest stopped after %d message(s)", self.counters["messages_received"])

    @property
    def running(self) -> bool:
        return self._running

    def refresh_registry(self) -> None:
        """Rebuild topic resolution and point metadata from the database."""
        with self.session_factory() as session:
            self.resolver.refresh(session)
        self.writer.invalidate()
        self.counters["registry_refreshes"] += 1
        if self._running:
            self._ensure_subscriptions()

    def _ensure_subscriptions(self) -> None:
        """Subscribe to ``<base>/#`` plus any binding topic outside that tree.

        A vendor gateway may be bound to a topic that does not follow the
        homestead convention at all (SDD 26.2 allows exactly that); the wildcard
        would never deliver it, so each such topic gets its own subscription.
        """
        wildcard = topic_utils.telemetry_subscription(self.base)
        if wildcard not in self._subscribed:
            self.bus.subscribe(wildcard, self._on_message)
            self._subscribed.add(wildcard)
        for topic in self.resolver.topics():
            if topic in self._subscribed or topic_matches(wildcard, topic):
                continue
            self.bus.subscribe(topic, self._on_message)
            self._subscribed.add(topic)

    # -- staleness sweep -------------------------------------------------

    def sweep_stale(self, now: dt.datetime | None = None) -> int:
        """Run one staleness sweep. Called by the thread and directly by tests."""
        now = now or self.clock()
        with self.session_factory() as session:
            marked = self.writer.mark_stale(session, now)
            if marked:
                session.commit()
        self.counters["sweeps"] += 1
        self.counters["stale_marked"] += marked
        return marked

    def _sweep_loop(self) -> None:  # pragma: no cover - exercised via stop()
        while not self._stop_event.wait(self.sweep_interval_s):
            try:
                self.sweep_stale()
            except Exception:
                logger.exception("Staleness sweep failed")
                self.counters["handler_errors"] += 1

    # -- message handling ------------------------------------------------

    def _on_message(self, message: Message) -> None:
        if not self._running:
            return
        self.counters["messages_received"] += 1
        self.last_message_at = self.clock()
        try:
            self._handle(message)
        except Exception as exc:  # never let one message kill the subscriber
            logger.exception("Ingest handler failed for %s", message.topic)
            self.counters["handler_errors"] += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._dead_letter(message, f"handler_error: {type(exc).__name__}: {exc}")

    def _handle(self, message: Message) -> None:
        parsed = topic_utils.parse_topic(message.topic, self.base)
        if parsed is not None and parsed.kind in _CONTROL_KINDS:
            # Requested state belongs to the command subsystem; ingest only ever
            # records measured state (SDD 10.3).
            self.counters["ignored_control"] += 1
            return

        if not message.payload.strip():
            # An empty retained payload is how a broker clears a topic.
            self.counters["ignored_empty"] += 1
            return

        if parsed is not None and parsed.kind == topic_utils.KIND_AVAILABILITY:
            self._handle_availability(message)
            return
        if parsed is not None and parsed.kind in (
            topic_utils.KIND_EVENT,
            topic_utils.KIND_ALARM,
        ):
            self._handle_event(message, parsed.kind, parsed.name)
            return
        self._handle_telemetry(message)

    def _handle_telemetry(self, message: Message) -> None:
        point_id = self.resolver.resolve(message.topic)
        if point_id is None:
            self.counters["unresolved_topics"] += 1
            self._dead_letter(message, f"unresolved_topic: {message.topic} is not bound to a point")
            return
        envelope = self._parse(message, parse_telemetry, "telemetry")
        if envelope is None:
            return
        if envelope.point_id != point_id:
            # The registry is the identity mechanism (SDD 26.2). The registry
            # wins, but the disagreement is a commissioning fault worth seeing.
            self._dead_letter(
                message,
                f"envelope_topic_mismatch: topic resolves to {point_id}, "
                f"envelope claims {envelope.point_id}",
            )
            asset_id, point_name = topic_utils.split_point_id(point_id)
            envelope = envelope.model_copy(update={"asset_id": asset_id, "point": point_name})
        self._apply(message, envelope)

    def _handle_availability(self, message: Message) -> None:
        self.counters["availability_received"] += 1
        envelope = self._parse(message, parse_availability, "availability")
        if envelope is None:
            return
        asset_id = self.resolver.resolve_availability(message.topic)
        if asset_id is None:
            self.counters["unresolved_topics"] += 1
            self._dead_letter(
                message,
                f"unresolved_availability_topic: {message.topic} has no registered asset",
            )
            return
        if envelope.asset_id != asset_id:
            self._dead_letter(
                message,
                f"envelope_topic_mismatch: topic resolves to {asset_id}, "
                f"envelope claims {envelope.asset_id}",
            )
            envelope = envelope.model_copy(update={"asset_id": asset_id})
        with self.session_factory() as session:
            result = self.writer.apply_availability(session, envelope, now=self.clock())
            session.commit()
        self.counters["points_marked_offline"] += result.points_marked_stale
        self.counters["stale_marked"] += result.points_marked_stale
        if result.availability_point_updated:
            self.counters["messages_written"] += 1
        else:
            self._dead_letter(
                message,
                f"no_availability_point: {asset_id} has no {topic_utils.KIND_AVAILABILITY} point",
            )

    def _handle_event(self, message: Message, kind: str, name: str | None) -> None:
        """Materialise an event/alarm publication onto its registry point.

        Ingest records *that the event happened*; the alarm engine owns alarm
        lifecycle (SDD 14.2) and the maintenance subsystem owns work orders.
        """
        self.counters["events_received"] += 1
        envelope = self._parse(message, _parse_event, "event")
        if envelope is None:
            return
        asset_id = self.resolver.resolve_asset(message.topic)
        if asset_id is None:
            self.counters["unresolved_topics"] += 1
            self._dead_letter(message, f"unresolved_{kind}_topic: {message.topic} has no registered asset")
            return

        with self.session_factory() as session:
            meta = None
            point_id = None
            for candidate in (name, envelope.event):
                if not candidate:
                    continue
                point_id = topic_utils.point_id(asset_id, candidate)
                meta = self.writer.point_meta(session, point_id)
                if meta is not None:
                    break
            if meta is None:
                self.counters["unresolved_topics"] += 1
                self._dead_letter(
                    message,
                    f"unresolved_{kind}_point: no registered point for {asset_id}/{name or envelope.event}",
                )
                return
            telemetry = TelemetryEnvelope(
                ts=envelope.ts,
                asset_id=meta.asset_id,
                point=meta.point_name,
                value=_event_value(meta.kind, envelope),
                quality="good",
                source=envelope.source or f"mqtt.{kind}",
                schema_version=envelope.schema_version,
            )
            self._apply(message, telemetry, session=session)

    # -- shared write path ----------------------------------------------

    def _apply(
        self, message: Message, envelope: TelemetryEnvelope, *, session: Session | None = None
    ) -> None:
        if session is not None:
            result = self.writer.apply(session, envelope, now=self.clock())
            session.commit()
        else:
            with self.session_factory() as owned:
                result = self.writer.apply(owned, envelope, now=self.clock())
                owned.commit()

        if result.out_of_order:
            self.counters["out_of_order"] += 1
        if result.sequence_gap:
            self.counters["sequence_gaps"] += 1
        if result.current_state_updated or result.historised:
            self.counters["messages_written"] += 1
        for reason in result.dead_letter_reasons:
            self._dead_letter(message, reason)

    def _parse(self, message: Message, parser: Callable[[bytes], Any], label: str) -> Any | None:
        try:
            return parser(message.payload)
        except ValueError as exc:
            # json.JSONDecodeError and pydantic's ValidationError are both
            # ValueError subclasses; both mean "this payload is not the
            # documented schema" (SDD MVP criterion 3).
            self.counters["invalid_payloads"] += 1
            reason = "malformed_json" if _is_json_error(exc) else f"invalid_{label}_envelope"
            self._dead_letter(message, f"{reason}: {_short(exc)}")
            return None

    def _dead_letter(self, message: Message, reason: str) -> None:
        """Record a rejected message in its own transaction."""
        self.counters["dead_lettered"] += 1
        logger.warning("Dead-lettering %s: %s", message.topic, reason)
        try:
            with self.session_factory() as session:
                session.add(
                    IngestDeadLetter(
                        topic=message.topic[:400],
                        payload=message.text[:MAX_DEAD_LETTER_PAYLOAD],
                        reason=reason[:200],
                    )
                )
                session.commit()
        except Exception:  # pragma: no cover - storage failure must not cascade
            logger.exception("Failed to record dead letter for %s", message.topic)

    # -- introspection ---------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Counters for the health/stats endpoint."""
        return {
            "name": self.name,
            "running": self._running,
            "base_topic": self.base,
            "sweep_interval_s": self.sweep_interval_s,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "last_error": self.last_error,
            "counters": dict(self.counters),
            "writer": dict(self.writer.counters),
            "resolver": self.resolver.stats.as_dict(),
        }


def _parse_event(payload: bytes | str) -> EventEnvelope:
    import json as _json

    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return EventEnvelope.model_validate(_json.loads(payload))


def _event_value(kind: str, envelope: EventEnvelope) -> Any:
    """Shape an event payload to the registry's data type for that point.

    An EVENT point may be an object (camera motion), a number (rainfall
    increment) or a flag. ``detail["value"]`` is honoured when present so a
    publisher can be explicit; otherwise the event name is the value and a
    boolean point simply records that it fired.
    """
    detail = envelope.detail or {}
    if isinstance(detail, dict) and "value" in detail:
        return detail["value"]
    if kind == "json":
        return detail
    if kind == "boolean":
        return True
    if kind in ("numeric", "integer"):
        return detail.get("count", 1) if isinstance(detail, dict) else 1
    return envelope.event


def _is_json_error(exc: Exception) -> bool:
    import json as _json

    return isinstance(exc, _json.JSONDecodeError) or isinstance(exc, UnicodeDecodeError)


def _short(exc: Exception, limit: int = 140) -> str:
    text = " ".join(str(exc).split())
    return text[:limit]


def _clamp(value: float) -> float:
    return max(MIN_SWEEP_INTERVAL_S, min(MAX_SWEEP_INTERVAL_S, float(value)))
