"""Energy Management System (SDD sections 13, 30-39).

The EMS is a **supervisory allocator, not a universal relay board** (SDD 5.3,
13, 43). It observes the electrical system, publishes one site energy state and
a per-load power budget, and requests bounded actions from the systems that own
immediate equipment authority: the BMS, the inverters, the generator
controller, local PLCs and hardwired protection.

Module map
----------

===================  =====================================================
``config``           every commissioning threshold, dwell and timer
``inputs``           SDD 30.5 input gathering with quality and validity
``derived``          SDD 30.6 derived values, each with a validity flag
``state_machine``    SDD 30.7-30.9 ten-state machine with dwell/hysteresis
``shedding``         SDD 32 shed sequence and SDD 33 restoration sequence
``generator``        SDD 34 start/permissive/run/stop/failure handling
``blackstart``       SDD 35 black start and post-recovery reconciliation
``leases``           SDD 31.3 dynamic priority and 31.4 power-budget leases
``loader``           loads ``data/load_schedule.yaml`` into the registry
``service``          the ``BackgroundService`` that ticks the whole cycle
===================  =====================================================

This module deliberately imports **no** EMS submodule, so the ports below can
be imported from anywhere in the package without an import cycle.

Ports
-----

The EMS never talks to hardware directly. It goes through :class:`CommandPort`,
which is injected. The default adapter lazily imports the platform command
manager inside the call so that neither module's import order constrains the
other, and so the EMS stays testable with :class:`RecordingCommandPort`.

A command that is not *accepted* is never treated as done. SDD 32.3 requires a
rejected or unconfirmed shed to escalate, not to be silently assumed off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "CommandOutcome",
    "CommandPort",
    "CommandRequest",
    "ManagerCommandPort",
    "NullCommandPort",
    "RecordingCommandPort",
]


@dataclass(frozen=True)
class CommandRequest:
    """One supervisory request the EMS wants issued against an asset."""

    asset_id: str
    command: str
    value: Any = None
    reason: str = ""
    issued_by: str = "ems"
    priority: int = 100
    expires_in_s: int | None = 300
    correlation_id: str | None = None
    idempotency_key: str | None = None
    operating_mode: str | None = None

    def as_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "command": self.command,
            "value": self.value,
            "reason": self.reason,
            "issued_by": self.issued_by,
            "priority": self.priority,
            "expires_in_s": self.expires_in_s,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "operating_mode": self.operating_mode,
        }


#: Outcome states. ``blocked`` means an interlock refused dispatch (for example
#: ``allow_physical_control`` is False); it is *not* success and never implies
#: the load changed state.
COMMAND_OUTCOMES = ("issued", "rejected", "blocked", "failed")


@dataclass(frozen=True)
class CommandOutcome:
    """Result of asking the platform to issue a command.

    ``accepted`` only means the platform took ownership of the request. Whether
    the load actually stopped is a separate, measured question (SDD 32.2: "a
    load is not considered shed until measured current/power confirms it").
    """

    accepted: bool
    outcome: str = "issued"
    command_id: str | None = None
    detail: str | None = None

    @classmethod
    def issued(cls, command_id: str | None = None, detail: str | None = None) -> CommandOutcome:
        return cls(accepted=True, outcome="issued", command_id=command_id, detail=detail)

    @classmethod
    def refused(cls, outcome: str, detail: str) -> CommandOutcome:
        return cls(accepted=False, outcome=outcome, detail=detail)


@runtime_checkable
class CommandPort(Protocol):
    """How the EMS asks for a physical action to be requested."""

    def issue(self, request: CommandRequest) -> CommandOutcome: ...


class ManagerCommandPort:
    """Default adapter over ``chaos.commands.manager.CommandManager``.

    The import happens inside :meth:`issue`, never at module import time, so the
    EMS does not couple its import order to the command subsystem and remains
    importable when that subsystem is unavailable.

    The command manager owns the interlock that refuses dispatch while
    ``settings.allow_physical_control`` is False. This adapter does not
    second-guess it; it simply reports the refusal so the shed sequence can
    escalate instead of assuming success.
    """

    def __init__(self, session_factory=None, bus=None, settings=None) -> None:
        self._session_factory = session_factory
        self._bus = bus
        self._settings = settings
        self._manager: Any = None
        self._unavailable_reason: str | None = None

    def _resolve_manager(self) -> Any:
        if self._manager is not None:
            return self._manager
        from chaos.commands.manager import CommandManager  # local import on purpose

        for kwargs in (
            {"session_factory": self._session_factory, "bus": self._bus, "settings": self._settings},
            {"bus": self._bus, "settings": self._settings},
            {"settings": self._settings},
            {},
        ):
            try:
                self._manager = CommandManager(**{k: v for k, v in kwargs.items() if v is not None})
                return self._manager
            except TypeError:
                continue
        raise TypeError("CommandManager constructor signature not recognised")

    def issue(self, request: CommandRequest) -> CommandOutcome:
        try:
            manager = self._resolve_manager()
        except Exception as exc:  # ImportError, TypeError, anything the manager raises
            self._unavailable_reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Command manager unavailable for %s: %s", request.asset_id, self._unavailable_reason
            )
            return CommandOutcome.refused(
                "blocked", f"command manager unavailable ({self._unavailable_reason})"
            )

        payload = request.as_dict()
        for method_name in ("issue", "issue_command", "request", "submit"):
            method = getattr(manager, method_name, None)
            if method is None:
                continue
            try:
                result = method(**payload)
            except TypeError:
                try:
                    result = method(payload)
                except Exception as exc:
                    return CommandOutcome.refused("failed", f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                return CommandOutcome.refused("rejected", f"{type(exc).__name__}: {exc}")
            return _coerce_outcome(result)
        return CommandOutcome.refused("blocked", "command manager exposes no issue method")


def _coerce_outcome(result: Any) -> CommandOutcome:
    """Interpret whatever the command manager returned, conservatively."""
    if isinstance(result, CommandOutcome):
        return result
    command_id = getattr(result, "command_id", None)
    state = getattr(result, "state", None)
    if command_id is None and isinstance(result, str):
        command_id = result
        state = "pending"
    if state in {"rejected", "failed", "expired", "cancelled", "superseded"}:
        detail = getattr(result, "state_reason", None) or f"command state {state}"
        return CommandOutcome(accepted=False, outcome="rejected", command_id=command_id, detail=detail)
    if command_id is None and state is None:
        # Unknown shape: refuse to claim success.
        return CommandOutcome.refused("failed", f"unrecognised command result {type(result).__name__}")
    return CommandOutcome.issued(command_id=command_id, detail=state)


@dataclass
class RecordingCommandPort:
    """Test double: records requests and returns a scripted outcome.

    ``responses`` maps ``"<asset_id>:<command>"`` (or just ``asset_id``) to the
    outcome to return, so a test can make one specific load fail to shed.
    """

    requests: list[CommandRequest] = field(default_factory=list)
    default: CommandOutcome = field(default_factory=lambda: CommandOutcome.issued("cmd-test"))
    responses: dict[str, CommandOutcome] = field(default_factory=dict)

    def issue(self, request: CommandRequest) -> CommandOutcome:
        self.requests.append(request)
        key = f"{request.asset_id}:{request.command}"
        return self.responses.get(key, self.responses.get(request.asset_id, self.default))

    # -- helpers ---------------------------------------------------------
    def commands_for(self, asset_id: str) -> list[CommandRequest]:
        return [r for r in self.requests if r.asset_id == asset_id]

    def assets(self) -> list[str]:
        return [r.asset_id for r in self.requests]

    def clear(self) -> None:
        self.requests.clear()


@dataclass
class NullCommandPort:
    """Records requests and refuses every one of them.

    Used where the platform must plan and explain but must not act -- for
    example on the secondary control node, or before commissioning has enabled
    physical control.
    """

    requests: list[CommandRequest] = field(default_factory=list)
    detail: str = "physical control not enabled"

    def issue(self, request: CommandRequest) -> CommandOutcome:
        self.requests.append(request)
        return CommandOutcome.refused("blocked", self.detail)
