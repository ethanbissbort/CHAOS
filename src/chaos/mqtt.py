"""Message-bus abstraction over MQTT.

Two implementations share one interface:

* :class:`PahoBus` -- Eclipse Mosquitto over paho-mqtt, the deployment path.
* :class:`InMemoryBus` -- a synchronous in-process broker used by tests, the
  simulator's offline mode and bench testing (SDD section 19 step 1).

Everything that touches the bus depends on :class:`MessageBus`, so no subsystem
needs a live broker to be exercised.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from chaos.config import Settings, get_settings

logger = logging.getLogger(__name__)

Handler = Callable[["Message"], None]


@dataclass(frozen=True)
class Message:
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False

    @property
    def text(self) -> str:
        return self.payload.decode("utf-8", errors="replace")


def topic_matches(filter_: str, topic: str) -> bool:
    """MQTT wildcard matching for ``+`` (one level) and ``#`` (rest)."""
    if filter_ == "#":
        return not topic.startswith("$")
    f_parts = filter_.split("/")
    t_parts = topic.split("/")
    for index, f_part in enumerate(f_parts):
        if f_part == "#":
            return index <= len(t_parts)
        if index >= len(t_parts):
            return False
        if f_part == "+":
            continue
        if f_part != t_parts[index]:
            return False
    return len(f_parts) == len(t_parts)


class MessageBus(Protocol):
    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None: ...

    def subscribe(self, topic_filter: str, handler: Handler) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


@dataclass
class InMemoryBus:
    """Synchronous in-process bus.

    Publishing delivers to matching subscribers immediately on the calling
    thread, which makes tests deterministic. Retained messages are replayed to
    late subscribers exactly like a real broker.
    """

    subscriptions: list[tuple[str, Handler]] = field(default_factory=list)
    retained: dict[str, Message] = field(default_factory=dict)
    published: list[Message] = field(default_factory=list)
    running: bool = False

    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        message = Message(topic=topic, payload=payload, qos=qos, retain=retain)
        self.published.append(message)
        if retain:
            if payload:
                self.retained[topic] = message
            else:
                self.retained.pop(topic, None)
        for topic_filter, handler in list(self.subscriptions):
            if topic_matches(topic_filter, topic):
                handler(message)

    def subscribe(self, topic_filter: str, handler: Handler) -> None:
        self.subscriptions.append((topic_filter, handler))
        for topic, message in list(self.retained.items()):
            if topic_matches(topic_filter, topic):
                handler(message)

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    # -- test helpers ----------------------------------------------------
    def clear(self) -> None:
        self.published.clear()

    def topics(self) -> list[str]:
        return [m.topic for m in self.published]

    def last(self, topic_filter: str) -> Message | None:
        for message in reversed(self.published):
            if topic_matches(topic_filter, message.topic):
                return message
        return None


class PahoBus:
    """MQTT bus backed by paho-mqtt."""

    def __init__(self, settings: Settings | None = None, client_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.client_id = client_id or self.settings.mqtt_client_id
        self._subscriptions: list[tuple[str, Handler]] = []
        self._lock = threading.Lock()
        self._client = None
        self._connected = threading.Event()

    def _build_client(self):
        import paho.mqtt.client as mqtt

        try:  # paho-mqtt 2.x
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id, clean_session=False
            )
        except AttributeError:  # pragma: no cover - paho-mqtt 1.x
            client = mqtt.Client(client_id=self.client_id, clean_session=False)

        if self.settings.mqtt_username:
            client.username_pw_set(self.settings.mqtt_username, self.settings.mqtt_password)
        if self.settings.mqtt_tls:
            client.tls_set()

        def on_connect(_client, _userdata, _flags, reason_code, *_args):
            if reason_code not in (0, "Success"):
                logger.error("MQTT connect failed: %s", reason_code)
                return
            logger.info("MQTT connected to %s:%s", self.settings.mqtt_host, self.settings.mqtt_port)
            self._connected.set()
            with self._lock:
                for topic_filter, _ in self._subscriptions:
                    _client.subscribe(topic_filter, qos=1)

        def on_disconnect(_client, _userdata, *args):  # pragma: no cover - network path
            self._connected.clear()
            logger.warning("MQTT disconnected: %s", args[-1] if args else "unknown")

        def on_message(_client, _userdata, msg):
            message = Message(topic=msg.topic, payload=msg.payload, qos=msg.qos, retain=msg.retain)
            with self._lock:
                handlers = [h for f, h in self._subscriptions if topic_matches(f, msg.topic)]
            for handler in handlers:
                try:
                    handler(message)
                except Exception:  # pragma: no cover - handler isolation
                    logger.exception("MQTT handler failed for topic %s", msg.topic)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        return client

    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        if self._client is None:
            raise RuntimeError("PahoBus.start() must be called before publishing")
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def subscribe(self, topic_filter: str, handler: Handler) -> None:
        with self._lock:
            self._subscriptions.append((topic_filter, handler))
        if self._client is not None and self._connected.is_set():
            self._client.subscribe(topic_filter, qos=1)

    def start(self, timeout: float = 5.0) -> None:
        self._client = self._build_client()
        self._client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=60)
        self._client.loop_start()
        self._connected.wait(timeout=timeout)

    def stop(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        self._connected.clear()


def build_bus(settings: Settings | None = None) -> MessageBus:
    """Return the configured bus, falling back to in-memory when MQTT is off."""
    settings = settings or get_settings()
    if not settings.mqtt_enabled:
        logger.info("MQTT disabled; using in-memory bus")
        return InMemoryBus()
    return PahoBus(settings)
