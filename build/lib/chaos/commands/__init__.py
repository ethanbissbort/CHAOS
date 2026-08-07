"""Command and operating-mode subsystem: the platform's audited control path.

This package is where a request becomes an instruction aimed at real equipment,
and where the platform decides not to send one. Its shape follows SDD section 6:
the twin sits at Level 3 and may only *request*; Levels 0 and 1 keep the
authority to reject. Everything here is therefore built to refuse by default and
to explain itself afterwards.

Layout::

    interlocks.py  pre-dispatch refusal registry (extensible)
    modes.py       site / domain / asset operating modes, emergency latching
    manager.py     command lifecycle: issue, dispatch, ack, expire, cancel
    service.py     background ack intake and TTL sweep

Typical use from another subsystem::

    from chaos.commands import CommandManager, CommandRequest, ServiceActor

    manager = CommandManager(session, bus, settings)
    command = manager.issue(
        CommandRequest(
            asset_id="energy.load.workshop.01",
            command="shed_load",
            value=True,
            reason="battery_reserve_protection",
            depends_on=["energy.battery.power_container.01/soc_pct"],
        ),
        ServiceActor("rules.energy_manager"),
    )
    if command.state == "rejected":
        ...  # command.interlocks_evaluated says exactly why
"""

from chaos.commands.interlocks import (
    ASSET_IN_MAINTENANCE,
    ASSET_NOT_OPERATIONAL,
    BINDING_NOT_COMMISSIONED,
    BUILTIN_INTERLOCK_CODES,
    DUPLICATE_IN_FLIGHT,
    EMERGENCY_MODE_LOCKOUT,
    INTERLOCK_ERROR,
    PHYSICAL_CONTROL_DISABLED,
    POINT_NOT_CONTROL_CAPABLE,
    STALE_INPUT,
    EmergencyLockout,
    InterlockContext,
    InterlockEvaluation,
    InterlockRegistry,
    InterlockResult,
    StaleInputInterlock,
    default_registry,
    global_registry,
    reset_global_registry,
)
from chaos.commands.manager import (
    CommandError,
    CommandManager,
    CommandRequest,
    CommandStateError,
    CommandValidationError,
    ServiceActor,
    UnknownCommandError,
    UnknownTargetError,
    new_command_id,
)
from chaos.commands.modes import (
    DEFAULT_MODE,
    LATCHING_MODES,
    MODES,
    RESTRICTIVENESS,
    SCOPE_TYPES,
    EmergencyLatchedError,
    ModeAuthorizationError,
    ModeError,
    ModeManager,
    UnknownModeError,
    more_restrictive,
)

__all__ = [
    "ASSET_IN_MAINTENANCE",
    "ASSET_NOT_OPERATIONAL",
    "BINDING_NOT_COMMISSIONED",
    "BUILTIN_INTERLOCK_CODES",
    "DEFAULT_MODE",
    "DUPLICATE_IN_FLIGHT",
    "EMERGENCY_MODE_LOCKOUT",
    "INTERLOCK_ERROR",
    "LATCHING_MODES",
    "MODES",
    "PHYSICAL_CONTROL_DISABLED",
    "POINT_NOT_CONTROL_CAPABLE",
    "RESTRICTIVENESS",
    "SCOPE_TYPES",
    "STALE_INPUT",
    "CommandError",
    "CommandManager",
    "CommandRequest",
    "CommandStateError",
    "CommandValidationError",
    "EmergencyLatchedError",
    "EmergencyLockout",
    "InterlockContext",
    "InterlockEvaluation",
    "InterlockRegistry",
    "InterlockResult",
    "ModeAuthorizationError",
    "ModeError",
    "ModeManager",
    "ServiceActor",
    "StaleInputInterlock",
    "UnknownCommandError",
    "UnknownModeError",
    "UnknownTargetError",
    "default_registry",
    "global_registry",
    "more_restrictive",
    "new_command_id",
    "reset_global_registry",
]
