"""Background maintenance scheduler.

Polls maintenance plans and raises work orders. Interval-based work does not
need second-level precision, so this runs infrequently and cheaply.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session, sessionmaker

from homestead_twin.config import Settings
from homestead_twin.maintenance.scheduler import generate_work_orders
from homestead_twin.models.base import utcnow
from homestead_twin.mqtt import MessageBus

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 900


class MaintenanceSchedulerService:
    """Generates work orders from due maintenance plans (SDD section 18)."""

    name = "maintenance"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        bus: MessageBus,
        settings: Settings,
        interval_s: int = DEFAULT_INTERVAL_S,
    ) -> None:
        self._session_factory = session_factory
        self._bus = bus
        self._settings = settings
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.work_orders_created = 0

    def tick(self, now=None) -> int:
        """Run one scheduling pass. Returns the number of work orders created."""
        now = now or utcnow()
        session = self._session_factory()
        try:
            result = generate_work_orders(session, now)
            session.commit()
            self.work_orders_created += result.created_count
            if result.created_count:
                logger.info("Maintenance scheduler created %d work order(s)", result.created_count)
            return result.created_count
        except Exception:
            session.rollback()
            logger.exception("Maintenance scheduling pass failed")
            return 0
        finally:
            session.close()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            self.tick()

    def start(self) -> None:
        self.tick()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="maintenance-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
