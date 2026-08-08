"""Background-service lifecycle.

Every long-running subsystem (MQTT ingest, EMS, alarm engine, maintenance
scheduler) implements :class:`BackgroundService`. The API lifespan starts them
on the primary node; the secondary control node starts a reduced set so it can
keep alerting and bridging when the power container is lost (SDD section 16.1).
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session, sessionmaker

from chaos.config import Settings
from chaos.mqtt import MessageBus

logger = logging.getLogger(__name__)


@runtime_checkable
class BackgroundService(Protocol):
    """Uniform lifecycle contract for every long-running subsystem."""

    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...


class ServiceManager:
    """Starts and stops background services, isolating failures.

    One subsystem failing to start must never prevent the rest of the platform
    from coming up -- a frozen greenhouse is worse than a missing dashboard.
    """

    def __init__(self) -> None:
        self._services: list[BackgroundService] = []
        self._started: list[BackgroundService] = []

    def register(self, service: BackgroundService) -> BackgroundService:
        self._services.append(service)
        return service

    @property
    def services(self) -> list[BackgroundService]:
        return list(self._services)

    def start_all(self) -> None:
        for service in self._services:
            try:
                service.start()
                self._started.append(service)
                logger.info("Started service: %s", service.name)
            except Exception:
                logger.exception("Failed to start service: %s", service.name)

    def stop_all(self) -> None:
        for service in reversed(self._started):
            try:
                service.stop()
                logger.info("Stopped service: %s", service.name)
            except Exception:
                logger.exception("Failed to stop service: %s", service.name)
        self._started.clear()


def build_services(
    settings: Settings,
    session_factory: sessionmaker[Session],
    bus: MessageBus,
) -> ServiceManager:
    """Construct the service set appropriate for this node role.

    Imports are local so a subsystem import error degrades that subsystem only.
    """
    manager = ServiceManager()

    if settings.mqtt_enabled:
        try:
            from chaos.ingest.service import IngestService

            manager.register(IngestService(session_factory, bus, settings))
        except Exception:
            logger.exception("Ingest service unavailable")

        # Command dispatch is a primary-node duty. Two nodes both dispatching
        # would give the site two supervisory control sources, which is exactly
        # the split-brain the secondary node exists to avoid: it observes,
        # alerts and bridges, but never commands.
        if not settings.is_secondary:
            try:
                from chaos.commands.service import CommandDispatchService

                manager.register(CommandDispatchService(session_factory, bus, settings))
            except Exception:
                logger.exception("Command dispatch service unavailable")
        else:
            logger.info("Secondary node: command dispatch not started (single control source)")

    if settings.alarm_engine_enabled:
        try:
            from chaos.alarms.service import AlarmEngineService

            manager.register(AlarmEngineService(session_factory, bus, settings))
        except Exception:
            logger.exception("Alarm engine unavailable")

    # The EMS is a primary-node responsibility. The secondary node observes and
    # alerts but must not attempt supervisory dispatch (avoids split control).
    if settings.ems_enabled and not settings.is_secondary:
        try:
            from chaos.ems.service import EnergyManagerService

            manager.register(EnergyManagerService(session_factory, bus, settings))
        except Exception:
            logger.exception("Energy manager unavailable")

    # Work generation is likewise a primary-node duty; duplicating it on the
    # secondary would raise the same work order twice.
    if not settings.is_secondary:
        try:
            from chaos.maintenance.service import MaintenanceSchedulerService

            manager.register(MaintenanceSchedulerService(session_factory, bus, settings))
        except Exception:
            logger.exception("Maintenance scheduler unavailable")

    return manager
