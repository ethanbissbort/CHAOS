"""Background half of the control path.

Two jobs, both of which exist so a command can never simply go quiet:

* **Acknowledgement intake.** Subscribes to every command-ack topic and feeds
  results into :meth:`CommandManager.record_ack`. A malformed payload is logged
  and dropped; the control plane must survive a device that publishes rubbish.
* **Expiry sweep.** Walks non-terminal commands past their TTL and closes them
  as ``expired`` (SDD section 5.7 counts "timed out" as a recorded outcome), and
  clears operating modes whose temporary expiry has elapsed. Emergency never
  clears here -- see :mod:`homestead_twin.commands.modes`.

The sweep interval is injectable and the sweep body is a public method, so tests
drive it directly and never sleep.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy.orm import Session, sessionmaker

from homestead_twin.commands.manager import CommandManager
from homestead_twin.commands.modes import ModeManager
from homestead_twin.config import Settings
from homestead_twin.envelope import parse_command_ack
from homestead_twin.models.base import utcnow
from homestead_twin.mqtt import Message, MessageBus
from homestead_twin.topics import KIND_ACK, KIND_COMMAND

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_S = 30.0


def ack_subscription(base: str) -> str:
    """``homestead/+/+/+/cmd/+/ack`` -- every command acknowledgement."""
    return f"{base}/+/+/+/{KIND_COMMAND}/+/{KIND_ACK}"


class CommandDispatchService:
    """:class:`~homestead_twin.runtime.BackgroundService` for the command path."""

    name = "commands"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        bus: MessageBus,
        settings: Settings,
        *,
        sweep_interval_s: float | None = None,
        clock=None,
    ) -> None:
        self.session_factory = session_factory
        self.bus = bus
        self.settings = settings
        self.sweep_interval_s = (
            DEFAULT_SWEEP_INTERVAL_S if sweep_interval_s is None else float(sweep_interval_s)
        )
        self._clock = clock or utcnow
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribed = False

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if not self._subscribed:
            topic_filter = ack_subscription(self.settings.mqtt_base_topic)
            self.bus.subscribe(topic_filter, self.on_ack_message)
            self._subscribed = True
            logger.info("Command dispatch service subscribed to %s", topic_filter)

        self._stop.clear()
        if self.sweep_interval_s > 0 and self._thread is None:
            self._thread = threading.Thread(
                target=self._sweep_loop, name="command-expiry-sweep", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            # The loop waits on the event, so this returns as soon as it wakes.
            thread.join(timeout=max(1.0, self.sweep_interval_s))
            if thread.is_alive():  # pragma: no cover - daemon thread, best effort
                logger.warning("Command expiry sweep did not stop within the join timeout")

    # -- ack intake ------------------------------------------------------
    def on_ack_message(self, message: Message) -> None:
        """Bus handler. Never raises: a bad ack must not kill the subscription."""
        try:
            ack = parse_command_ack(message.payload)
        except Exception:  # noqa: BLE001 - untrusted device payload
            logger.warning(
                "Discarding malformed command ack on %s: %s", message.topic, message.text[:400]
            )
            return
        try:
            with self._session() as session:
                CommandManager(session, self.bus, self.settings).record_ack(ack)
        except Exception:  # noqa: BLE001 - one bad ack must not stop the service
            logger.exception("Failed to record ack for command %s", ack.command_id)

    # -- sweep -----------------------------------------------------------
    def sweep(self, now: dt.datetime | None = None) -> dict[str, int]:
        """One expiry pass. Public so tests call it without a timer."""
        now = now or self._clock()
        with self._session() as session:
            manager = CommandManager(session, self.bus, self.settings)
            expired = manager.expire_due(now)
            cleared = ModeManager(session, site_id=self.settings.site_id).expire_modes(now)
        return {"expired_commands": len(expired), "cleared_modes": len(cleared)}

    def _sweep_loop(self) -> None:
        while not self._stop.is_set():
            # Wait first: start() should not fire a sweep before the platform
            # has finished coming up.
            if self._stop.wait(self.sweep_interval_s):
                return
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 - keep sweeping after a failure
                logger.exception("Command expiry sweep failed")

    # -- plumbing --------------------------------------------------------
    class _SessionScope:
        def __init__(self, factory: sessionmaker[Session]) -> None:
            self._factory = factory
            self._session: Session | None = None

        def __enter__(self) -> Session:
            self._session = self._factory()
            return self._session

        def __exit__(self, exc_type, exc, tb) -> None:
            if self._session is not None:
                if exc_type is not None:
                    self._session.rollback()
                self._session.close()
                self._session = None

    def _session(self) -> "CommandDispatchService._SessionScope":
        return self._SessionScope(self.session_factory)
