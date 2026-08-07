"""Alarm engine background service.

One cycle is: evaluate definitions, correlate the open alarms into incidents,
close incidents whose members have all cleared, then notify. The order matters --
correlation runs *before* notification so that a container outage is already one
incident by the time anything is sent.

``evaluate_once`` is the whole cycle and takes an explicit ``now``, so tests
drive delays, hysteresis and escalation timers deterministically without
sleeping. The daemon loop is a thin wrapper around it.

The engine runs on the secondary control node too. SDD 16.1 requires essential
alerts to survive the loss of the power container, so this service is not gated
on ``node_role``.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from homestead_twin.alarms.correlation import CorrelationEngine, CorrelationResult
from homestead_twin.alarms.definitions import DefinitionError, ensure_definitions
from homestead_twin.alarms.evaluator import AlarmEvaluator, EvaluationResult
from homestead_twin.alarms.notify import NotificationResult, Notifier
from homestead_twin.config import Settings, get_settings
from homestead_twin.models.base import utcnow
from homestead_twin.mqtt import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 10.0


@dataclass
class AlarmCycleResult:
    """What one full alarm-engine cycle did."""

    ran_at: dt.datetime
    evaluation: EvaluationResult = field(default_factory=EvaluationResult)
    correlation: CorrelationResult = field(default_factory=CorrelationResult)
    notification: NotificationResult = field(default_factory=NotificationResult)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at.isoformat(),
            "evaluation": self.evaluation.as_dict(),
            "correlation": self.correlation.as_dict(),
            "notification": self.notification.as_dict(),
        }


class AlarmEngineService:
    """:class:`~homestead_twin.runtime.BackgroundService` for alarms and notification."""

    name = "alarms"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        bus: MessageBus | None = None,
        settings: Settings | None = None,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        definitions_path: str | Path | None = None,
        clock: Callable[[], dt.datetime] = utcnow,
        load_definitions_on_start: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self.bus = bus
        self.settings = settings or get_settings()
        self.interval_s = interval_s
        self.definitions_path = definitions_path
        self.clock = clock
        self.load_definitions_on_start = load_definitions_on_start

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_result: AlarmCycleResult | None = None
        self.last_error: str | None = None
        self.cycles = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        if self.load_definitions_on_start:
            self.load_definitions()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="alarm-engine", daemon=True)
        self._thread.start()
        logger.info("Alarm engine started (interval %.1fs)", self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=max(2.0, self.interval_s))
        logger.info("Alarm engine stopped after %d cycles", self.cycles)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.evaluate_once()
            except Exception:  # pragma: no cover - a bad cycle must not kill the engine
                logger.exception("Alarm engine cycle failed")
            self._stop.wait(self.interval_s)

    # -- work --------------------------------------------------------------

    def load_definitions(self, session: Session | None = None) -> None:
        """Load the definition set if the table is empty."""
        owned = session is None
        session = session or self.session_factory()
        try:
            ensure_definitions(session, path=self.definitions_path, settings=self.settings, strict=False)
            if owned:
                session.commit()
        except DefinitionError:
            # A broken definition file must not take the platform down; the rest
            # of the subsystem still reports whatever definitions are loaded.
            logger.exception("Alarm definitions could not be loaded")
            if owned:
                session.rollback()
        finally:
            if owned:
                session.close()

    def evaluate_once(
        self, now: dt.datetime | None = None, *, session: Session | None = None
    ) -> AlarmCycleResult:
        """Run one full cycle: evaluate, correlate, close, notify."""
        now = now or self.clock()
        owned = session is None
        session = session or self.session_factory()
        try:
            result = self._cycle(session, now)
            if owned:
                session.commit()
            self.last_result = result
            self.last_error = None
            self.cycles += 1
            return result
        except Exception as exc:
            if owned:
                session.rollback()
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if owned:
                session.close()

    def _cycle(self, session: Session, now: dt.datetime) -> AlarmCycleResult:
        evaluator = AlarmEvaluator(session, self.bus, self.settings)
        correlator = CorrelationEngine(session, self.settings)
        notifier = Notifier(session, self.settings)

        evaluation = evaluator.evaluate(now)
        session.flush()
        correlation = correlator.correlate(now)
        session.flush()
        notification = notifier.dispatch_pending(now)

        return AlarmCycleResult(
            ran_at=now,
            evaluation=evaluation,
            correlation=correlation,
            notification=notification,
        )

    # -- introspection -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": self.running,
            "enabled": self.settings.alarm_engine_enabled,
            "interval_s": self.interval_s,
            "cycles": self.cycles,
            "notification_backends": self.settings.notification_backend_list,
            "last_error": self.last_error,
            "last_result": self.last_result.as_dict() if self.last_result else None,
        }
