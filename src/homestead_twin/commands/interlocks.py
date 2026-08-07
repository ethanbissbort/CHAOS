"""Pre-dispatch interlocks: the gate every command must pass.

SDD section 6 places this platform at Level 3. Level 3 may *request*; Levels 0
and 1 retain authority to reject. Interlocks are therefore not a safety system
-- the hardwired protection at Level 0 is. They are the platform's own refusal
layer: the set of conditions under which the twin declines to even ask.

Two rules shape the whole module:

1. **Refuse by default.** A command that cannot be *proven* safe is denied. A
   missing point, a missing binding, an absent measurement and a raising
   interlock all deny. Silence is never consent (SDD section 26.6: "safety
   critical permissives must not default to permissive on missing data").
2. **Explain every decision.** Each interlock returns a machine-readable
   ``code`` and a human ``reason``. Both allow and deny results are recorded on
   ``Command.interlocks_evaluated`` and returned to the operator, because
   SDD section 17.4 requires every control to show the interlocks preventing
   operation -- not merely that it is blocked.

The registry is ordered and extensible so that other subsystems (the EMS
load-shedding path in SDD section 32, for example) can register additional
interlocks without editing this module::

    from homestead_twin.commands.interlocks import global_registry

    @global_registry().register(name="power_budget", order=600)
    def power_budget(session, request, context):
        ...
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin.config import Settings
from homestead_twin.models.commands import TERMINAL_COMMAND_STATES, Command
from homestead_twin.models.registry import Asset, Point, PointBinding
from homestead_twin.models.telemetry import CurrentState

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from homestead_twin.commands.manager import CommandRequest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stable interlock codes. Other subsystems reference these strings; treat them
# as part of the platform's public contract.
# ---------------------------------------------------------------------------

PHYSICAL_CONTROL_DISABLED = "physical_control_disabled"
BINDING_NOT_COMMISSIONED = "binding_not_commissioned"
POINT_NOT_CONTROL_CAPABLE = "point_not_control_capable"
ASSET_IN_MAINTENANCE = "asset_in_maintenance"
EMERGENCY_MODE_LOCKOUT = "emergency_mode_lockout"
ASSET_NOT_OPERATIONAL = "asset_not_operational"
STALE_INPUT = "stale_input"
DUPLICATE_IN_FLIGHT = "duplicate_in_flight"

#: Raised-in-an-interlock sentinel. A broken interlock denies; it never opens.
INTERLOCK_ERROR = "interlock_error"

BUILTIN_INTERLOCK_CODES = (
    PHYSICAL_CONTROL_DISABLED,
    BINDING_NOT_COMMISSIONED,
    POINT_NOT_CONTROL_CAPABLE,
    ASSET_IN_MAINTENANCE,
    EMERGENCY_MODE_LOCKOUT,
    ASSET_NOT_OPERATIONAL,
    STALE_INPUT,
    DUPLICATE_IN_FLIGHT,
)

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

#: Asset lifecycle states in which the platform will talk to real equipment.
#: ``concept|planned|procured`` do not physically exist; ``failed|retired`` must
#: not be commanded; ``reserve`` is a deliberate spare; ``maintenance`` is
#: handled by :func:`asset_in_maintenance` so the operator sees that reason.
OPERATIONAL_ASSET_STATUSES = frozenset({"installed", "commissioned", "active", "degraded"})

#: Binding lifecycle value that means "commissioning has explicitly permitted
#: this binding" (SDD section 47).
COMMISSIONED_BINDING_STATUS = "commissioned"

#: Qualities that make a measurement unusable as a control input (SDD 26.6).
#: ``uncertain`` and ``substituted`` are deliberately *not* here -- the SDD
#: requires each decision to state its own tolerance, and a caller that needs a
#: stricter rule passes ``StaleInputInterlock(unusable_qualities=...)``.
UNUSABLE_QUALITIES = frozenset({"bad", "stale"})

#: Commands that remain permitted while a scope is latched into ``emergency``.
#: Every name here unambiguously moves equipment *towards* its safe state;
#: nothing here starts load. Ambiguous verbs (``open``, ``set_mode``) are
#: deliberately excluded -- a subsystem that needs one passes its own allow-list
#: to :class:`EmergencyLockout` and owns that decision explicitly.
DEFAULT_EMERGENCY_ALLOWED_COMMANDS = frozenset(
    {
        "stop",
        "emergency_stop",
        "shutdown",
        "disable",
        "trip",
        "open_disconnect",
        "close_isolation_valve",
        "shed_load",
        "set_safe_state",
        "vent",
        "silence_alarm",
        "acknowledge_alarm",
    }
)

#: SDD section 15.2 role model, least privilege first. Duplicated from
#: ``api.deps`` on purpose: the control path must not import the web layer.
ROLE_ORDER = ("viewer", "operator", "maintainer", "administrator")
_ROLE_RANK = {role: index for index, role in enumerate(ROLE_ORDER)}

#: Minimum role that may override a maintenance lockout.
MAINTENANCE_OVERRIDE_ROLE = "maintainer"


def role_at_least(role: str | None, minimum: str) -> bool:
    """True when ``role`` is at least as privileged as ``minimum``."""
    return _ROLE_RANK.get((role or "").lower(), -1) >= _ROLE_RANK[minimum]


def utc(value: dt.datetime | None) -> dt.datetime | None:
    """Normalise a database timestamp to timezone-aware UTC.

    SQLite discards ``tzinfo`` on write and hands back naive datetimes, while
    PostgreSQL returns aware ones. Every stored timestamp is UTC by platform
    convention (SDD section 16.3), so attaching UTC to a naive value is safe and
    keeps arithmetic from raising on the SQLite deployment.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterlockResult:
    """One interlock's verdict on one command."""

    code: str
    allowed: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)
    #: Allowed, but the command must not reach equipment (``dry_run``).
    blocks_dispatch: bool = False
    #: Set when a human explicitly overrode a condition that would have denied.
    override_by: str | None = None

    @classmethod
    def allow(
        cls,
        code: str,
        reason: str = "",
        *,
        detail: dict[str, Any] | None = None,
        blocks_dispatch: bool = False,
        override_by: str | None = None,
    ) -> InterlockResult:
        return cls(
            code=code,
            allowed=True,
            reason=reason or "ok",
            detail=detail or {},
            blocks_dispatch=blocks_dispatch,
            override_by=override_by,
        )

    @classmethod
    def deny(cls, code: str, reason: str, *, detail: dict[str, Any] | None = None) -> InterlockResult:
        return cls(code=code, allowed=False, reason=reason, detail=detail or {})

    def as_record(self) -> dict[str, Any]:
        """JSON form stored on ``Command.interlocks_evaluated``."""
        return {
            "code": self.code,
            "allowed": self.allowed,
            "reason": self.reason,
            "detail": self.detail,
            "blocks_dispatch": self.blocks_dispatch,
            "override_by": self.override_by,
        }


@dataclass(frozen=True)
class InterlockEvaluation:
    """The full ordered verdict set for one command."""

    results: tuple[InterlockResult, ...] = ()

    def __iter__(self) -> Iterator[InterlockResult]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    @property
    def allowed(self) -> bool:
        """True when no interlock denied. Dispatch may still be blocked."""
        return all(result.allowed for result in self.results)

    @property
    def dispatch_allowed(self) -> bool:
        """True only when the command may actually reach equipment."""
        return self.allowed and not any(r.blocks_dispatch for r in self.results)

    @property
    def denials(self) -> tuple[InterlockResult, ...]:
        return tuple(r for r in self.results if not r.allowed)

    @property
    def first_denial(self) -> InterlockResult | None:
        return self.denials[0] if self.denials else None

    @property
    def denial_codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.denials)

    @property
    def overrides(self) -> tuple[InterlockResult, ...]:
        return tuple(r for r in self.results if r.override_by)

    @property
    def blocking_dispatch(self) -> tuple[InterlockResult, ...]:
        return tuple(r for r in self.results if r.blocks_dispatch)

    @property
    def records(self) -> list[dict[str, Any]]:
        return [result.as_record() for result in self.results]

    def by_code(self, code: str) -> InterlockResult | None:
        for result in self.results:
            if result.code == code:
                return result
        return None

    def detail_for(self, code: str) -> dict[str, Any]:
        result = self.by_code(code)
        return dict(result.detail) if result else {}


# ---------------------------------------------------------------------------
# Evaluation context
# ---------------------------------------------------------------------------


@dataclass
class InterlockContext:
    """Everything an interlock may consult besides the session and request.

    The manager resolves these once so that eight interlocks do not issue eight
    copies of the same query, and so a custom interlock sees exactly the same
    view of the world as the built-ins.
    """

    settings: Settings
    now: dt.datetime
    principal_name: str = "unknown"
    principal_role: str = "viewer"
    #: ``human`` | ``service`` | ``rule`` -- who is asking (SDD section 5.7).
    issuer_kind: str = "service"
    asset: Asset | None = None
    point: Point | None = None
    binding: PointBinding | None = None
    point_id: str | None = None
    #: Most restrictive mode across site / domain / asset.
    effective_mode: str = "automatic"
    #: ``{"site": ..., "domain": ..., "asset": ...}``.
    mode_chain: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_human(self) -> bool:
        return self.issuer_kind == "human"

    def scopes_in_mode(self, mode: str) -> list[str]:
        """Scope names currently sitting in ``mode``, outermost first."""
        order = ("site", "domain", "asset")
        found = [scope for scope in order if self.mode_chain.get(scope) == mode]
        if not found and self.effective_mode == mode:
            found = ["effective"]
        return found


Interlock = Callable[[Session, "CommandRequest", InterlockContext], "InterlockResult | None"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    order: int
    seq: int
    name: str
    func: Interlock

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.order, self.seq)


class InterlockRegistry:
    """Ordered, extensible collection of interlocks.

    Ordering matters only for readability of the evaluation record -- *every*
    interlock runs on every command, even after one has denied, because an
    operator staring at a refused pump start needs the whole picture, not the
    first objection.
    """

    def __init__(self, entries: Sequence[_Entry] | None = None) -> None:
        self._entries: list[_Entry] = list(entries or ())
        self._seq = len(self._entries)

    # -- construction ----------------------------------------------------
    def register(
        self,
        func: Interlock | None = None,
        *,
        name: str | None = None,
        order: int = 500,
        replace: bool = False,
    ):
        """Register an interlock. Usable directly or as a decorator."""

        def _register(target: Interlock) -> Interlock:
            key = name or getattr(target, "__name__", None)
            if not key:  # pragma: no cover - defensive
                raise ValueError("Interlock needs a name")
            if any(entry.name == key for entry in self._entries):
                if not replace:
                    raise ValueError(f"Interlock {key!r} is already registered")
                self.unregister(key)
            self._entries.append(_Entry(order=order, seq=self._seq, name=key, func=target))
            self._seq += 1
            return target

        if func is not None:
            return _register(func)
        return _register

    def unregister(self, name: str) -> bool:
        before = len(self._entries)
        self._entries = [entry for entry in self._entries if entry.name != name]
        return len(self._entries) != before

    def clear(self) -> None:
        self._entries.clear()

    def copy(self) -> InterlockRegistry:
        return InterlockRegistry(list(self._entries))

    # -- inspection ------------------------------------------------------
    def names(self) -> list[str]:
        return [entry.name for entry in sorted(self._entries, key=lambda e: e.sort_key)]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        return any(entry.name == name for entry in self._entries)

    # -- evaluation ------------------------------------------------------
    def evaluate(
        self, session: Session, request: CommandRequest, context: InterlockContext
    ) -> InterlockEvaluation:
        results: list[InterlockResult] = []
        for entry in sorted(self._entries, key=lambda e: e.sort_key):
            try:
                result = entry.func(session, request, context)
            except Exception as exc:
                logger.exception("Interlock %s raised; denying command", entry.name)
                result = InterlockResult.deny(
                    INTERLOCK_ERROR,
                    f"Interlock {entry.name!r} failed to evaluate; refusing by default",
                    detail={"interlock": entry.name, "error": repr(exc)},
                )
            if result is not None:
                results.append(result)
        return InterlockEvaluation(tuple(results))


# ---------------------------------------------------------------------------
# Built-in interlocks
# ---------------------------------------------------------------------------


def physical_control_disabled(
    session: Session, request: CommandRequest, context: InterlockContext
) -> InterlockResult:
    """Master safety gate: the platform ships refusing to actuate.

    ``Settings.allow_physical_control`` defaults to False. Until a subsystem has
    passed the SDD section 19 commissioning sequence the twin observes and
    advises but does not move equipment.

    A ``dry_run`` request passes this gate so an operator can see the full
    interlock picture, but is marked non-dispatching -- and stays
    non-dispatching even when physical control is enabled.
    """
    enabled = bool(context.settings.allow_physical_control)
    if not enabled and not request.dry_run:
        return InterlockResult.deny(
            PHYSICAL_CONTROL_DISABLED,
            "Physical control is disabled platform-wide (HOMESTEAD_ALLOW_PHYSICAL_CONTROL is false)",
            detail={"allow_physical_control": False, "dry_run": False},
        )
    if request.dry_run:
        return InterlockResult.allow(
            PHYSICAL_CONTROL_DISABLED,
            "Dry run: interlocks evaluated, nothing will be published",
            detail={"allow_physical_control": enabled, "dry_run": True},
            blocks_dispatch=True,
        )
    return InterlockResult.allow(
        PHYSICAL_CONTROL_DISABLED,
        "Physical control is enabled",
        detail={"allow_physical_control": True, "dry_run": False},
    )


def binding_not_commissioned(
    session: Session, request: CommandRequest, context: InterlockContext
) -> InterlockResult:
    """SDD section 47: control capability in the dictionary is not permission.

    A binding must be commissioned *and* explicitly flagged for automatic
    control. An absent binding is a denial, not a shrug: without one there is no
    verified address to command.
    """
    binding = context.binding
    if binding is None:
        return InterlockResult.deny(
            BINDING_NOT_COMMISSIONED,
            f"No point binding exists for {context.point_id!r}; "
            "commissioning has not mapped this point to a device",
            detail={"point_id": context.point_id, "binding": None},
        )
    detail = {
        "point_id": binding.point_id,
        "binding_status": binding.binding_status,
        "automatic_control_allowed": bool(binding.automatic_control_allowed),
        "source_protocol": binding.source_protocol,
    }
    if binding.binding_status != COMMISSIONED_BINDING_STATUS:
        return InterlockResult.deny(
            BINDING_NOT_COMMISSIONED,
            f"Binding status is {binding.binding_status!r}, not {COMMISSIONED_BINDING_STATUS!r}",
            detail=detail,
        )
    if not binding.automatic_control_allowed:
        return InterlockResult.deny(
            BINDING_NOT_COMMISSIONED,
            "Binding is commissioned but automatic control has not been permitted",
            detail=detail,
        )
    return InterlockResult.allow(
        BINDING_NOT_COMMISSIONED, "Binding is commissioned for control", detail=detail
    )


def point_not_control_capable(
    session: Session, request: CommandRequest, context: InterlockContext
) -> InterlockResult:
    """The target point must be a command/setpoint point, not a measurement."""
    point = context.point
    if point is None:
        return InterlockResult.deny(
            POINT_NOT_CONTROL_CAPABLE,
            f"Point {context.point_id!r} is not in the registry",
            detail={"point_id": context.point_id},
        )
    detail = {
        "point_id": point.point_id,
        "point_class": point.point_class,
        "control_capable": bool(point.control_capable),
    }
    if not point.control_capable:
        return InterlockResult.deny(
            POINT_NOT_CONTROL_CAPABLE,
            f"Point {point.point_id!r} is not control capable",
            detail=detail,
        )
    return InterlockResult.allow(POINT_NOT_CONTROL_CAPABLE, "Point is control capable", detail=detail)


def asset_in_maintenance(
    session: Session, request: CommandRequest, context: InterlockContext
) -> InterlockResult:
    """SDD section 11: maintenance inhibits automatic starts and shows a lockout.

    Automatic and service-issued commands are refused outright. A human
    maintainer may proceed with an explicit override flag; the override is
    recorded on the result and the manager writes a dedicated audit entry so the
    bypass is never invisible.
    """
    scopes = [scope for scope, mode in context.mode_chain.items() if mode == "maintenance"]
    if not scopes and context.effective_mode != "maintenance":
        return InterlockResult.allow(
            ASSET_IN_MAINTENANCE,
            "No maintenance lockout in the mode chain",
            detail={"mode_chain": dict(context.mode_chain)},
        )
    detail = {
        "mode_chain": dict(context.mode_chain),
        "scopes_in_maintenance": scopes,
        "issuer_kind": context.issuer_kind,
        "maintenance_override": bool(request.maintenance_override),
    }
    if not context.is_human:
        return InterlockResult.deny(
            ASSET_IN_MAINTENANCE,
            f"{context.issuer_kind} commands are inhibited while "
            f"{', '.join(scopes) or 'the asset'} is in maintenance",
            detail=detail,
        )
    if not request.maintenance_override:
        return InterlockResult.deny(
            ASSET_IN_MAINTENANCE,
            "Maintenance lockout is active; a maintainer must set maintenance_override to proceed",
            detail=detail,
        )
    if not role_at_least(context.principal_role, MAINTENANCE_OVERRIDE_ROLE):
        return InterlockResult.deny(
            ASSET_IN_MAINTENANCE,
            f"Overriding a maintenance lockout requires the "
            f"{MAINTENANCE_OVERRIDE_ROLE!r} role "
            f"(caller has {context.principal_role!r})",
            detail=detail,
        )
    return InterlockResult.allow(
        ASSET_IN_MAINTENANCE,
        f"Maintenance lockout overridden by {context.principal_name}",
        detail=detail,
        override_by=context.principal_name,
    )


class EmergencyLockout:
    """SDD section 11: in emergency only predefined protective actions run.

    Implemented as a class so a subsystem can register its own allow-list, for
    example a black-start sequence (SDD section 35) that must be permitted while
    the site is still latched into emergency.
    """

    code = EMERGENCY_MODE_LOCKOUT

    def __init__(self, allowed_commands: frozenset[str] | None = None) -> None:
        self.allowed_commands = frozenset(
            allowed_commands if allowed_commands is not None else DEFAULT_EMERGENCY_ALLOWED_COMMANDS
        )

    def __call__(
        self, session: Session, request: CommandRequest, context: InterlockContext
    ) -> InterlockResult:
        scopes = context.scopes_in_mode("emergency")
        if not scopes:
            return InterlockResult.allow(
                self.code,
                "No emergency lockout in the mode chain",
                detail={"mode_chain": dict(context.mode_chain)},
            )
        allowed = sorted(self.allowed_commands)
        detail = {
            "mode_chain": dict(context.mode_chain),
            "scopes_in_emergency": scopes,
            "command": request.command,
            "allowed_commands": allowed,
        }
        if request.command not in self.allowed_commands:
            return InterlockResult.deny(
                self.code,
                f"{', '.join(scopes)} is in emergency; only protective commands "
                f"are permitted ({', '.join(allowed)})",
                detail=detail,
            )
        return InterlockResult.allow(
            self.code,
            f"{request.command!r} is a permitted protective command in emergency",
            detail=detail,
        )


#: Module-level default instance used by :func:`default_registry`.
emergency_mode_lockout = EmergencyLockout()


def asset_not_operational(
    session: Session, request: CommandRequest, context: InterlockContext
) -> InterlockResult:
    """Only assets that physically exist and are healthy accept commands."""
    asset = context.asset
    if asset is None:
        return InterlockResult.deny(
            ASSET_NOT_OPERATIONAL,
            "Target asset is not in the registry",
            detail={"asset_id": request.asset_id},
        )
    detail = {
        "asset_id": asset.asset_id,
        "status": asset.status,
        "operational_statuses": sorted(OPERATIONAL_ASSET_STATUSES),
    }
    if asset.status not in OPERATIONAL_ASSET_STATUSES:
        return InterlockResult.deny(
            ASSET_NOT_OPERATIONAL,
            f"Asset status is {asset.status!r}; commands are accepted only for "
            f"{', '.join(sorted(OPERATIONAL_ASSET_STATUSES))}",
            detail=detail,
        )
    return InterlockResult.allow(ASSET_NOT_OPERATIONAL, f"Asset status is {asset.status!r}", detail=detail)


class StaleInputInterlock:
    """SDD sections 5.5 and 26.6: refuse to act on an invalid sensor picture.

    Two sources of dependency are checked:

    * ``request.depends_on`` -- point IDs the caller declares the decision rests
      on. A *missing* current value denies: no data is not good data.
    * the target point's own current value, when one exists. A valve whose
      position feedback has failed is not a valve the twin will stroke blind.

    Staleness is judged both by the reported quality code and by the point's own
    timeout, so a device that stopped publishing without anyone marking it stale
    still blocks control.
    """

    code = STALE_INPUT

    def __init__(
        self,
        unusable_qualities: frozenset[str] | None = None,
        *,
        check_target_point: bool = True,
    ) -> None:
        self.unusable_qualities = frozenset(
            unusable_qualities if unusable_qualities is not None else UNUSABLE_QUALITIES
        )
        self.check_target_point = check_target_point

    def _age_limit(self, state: CurrentState, context: InterlockContext) -> int | None:
        if state.stale_after_s:
            return state.stale_after_s
        point = context.point
        if point is not None and point.stale_after_s:
            return point.stale_after_s
        return context.settings.default_stale_after_s or None

    def _problem(
        self, point_id: str, state: CurrentState | None, context: InterlockContext, *, required: bool
    ) -> dict[str, Any] | None:
        if state is None:
            if not required:
                return None
            return {"point_id": point_id, "quality": "missing", "reason": "no current value"}
        if state.quality in self.unusable_qualities:
            return {"point_id": point_id, "quality": state.quality, "reason": "unusable quality"}
        ts = utc(state.ts)
        limit = self._age_limit(state, context)
        if required and ts is None:
            return {"point_id": point_id, "quality": state.quality, "reason": "no timestamp"}
        if ts is not None and limit:
            age = (context.now - ts).total_seconds()
            if age > limit:
                return {
                    "point_id": point_id,
                    "quality": state.quality,
                    "reason": "older than the point timeout",
                    "age_s": round(age, 3),
                    "stale_after_s": limit,
                }
        return None

    def __call__(
        self, session: Session, request: CommandRequest, context: InterlockContext
    ) -> InterlockResult:
        checked: dict[str, bool] = {}
        for point_id in request.depends_on or ():
            checked[point_id] = True
        if self.check_target_point and context.point_id:
            # The target's own feedback is advisory: absence is normal for a
            # command point, so it is checked but not *required*.
            checked.setdefault(context.point_id, False)

        problems: list[dict[str, Any]] = []
        for point_id, required in checked.items():
            state = session.get(CurrentState, point_id)
            problem = self._problem(point_id, state, context, required=required)
            if problem is not None:
                problems.append(problem)

        detail = {"checked": sorted(checked), "problems": problems}
        if problems:
            names = ", ".join(p["point_id"] for p in problems)
            return InterlockResult.deny(
                self.code,
                f"Command depends on measurements that are not usable: {names}",
                detail=detail,
            )
        return InterlockResult.allow(self.code, "All declared input measurements are usable", detail=detail)


#: Module-level default instance used by :func:`default_registry`.
stale_input = StaleInputInterlock()


def duplicate_in_flight(
    session: Session, request: CommandRequest, context: InterlockContext
) -> InterlockResult:
    """A newer command for the same target supersedes older in-flight ones.

    This interlock allows -- issuing the newer request is the point -- but
    reports the command IDs the manager must mark ``superseded`` so two
    contradictory requests never sit on the wire at once (SDD section 32.1
    rule 6: no rapid contradictory commands).
    """
    stmt = (
        select(Command)
        .where(
            Command.asset_id == request.asset_id,
            Command.command == request.command,
            Command.state.not_in(sorted(TERMINAL_COMMAND_STATES)),
        )
        .order_by(Command.issued_at)
    )
    existing = list(session.execute(stmt).scalars())
    if not existing:
        return InterlockResult.allow(DUPLICATE_IN_FLIGHT, "No in-flight command for this target")
    ids = [command.command_id for command in existing]
    return InterlockResult.allow(
        DUPLICATE_IN_FLIGHT,
        f"Supersedes {len(ids)} in-flight command(s) for the same target",
        detail={"supersedes": ids, "count": len(ids)},
    )


# ---------------------------------------------------------------------------
# Registry construction
# ---------------------------------------------------------------------------

#: Built-ins in evaluation order. Master switch first so the most fundamental
#: refusal is the first line an operator reads.
BUILTIN_INTERLOCKS: tuple[tuple[str, Interlock, int], ...] = (
    (PHYSICAL_CONTROL_DISABLED, physical_control_disabled, 10),
    (ASSET_NOT_OPERATIONAL, asset_not_operational, 20),
    (POINT_NOT_CONTROL_CAPABLE, point_not_control_capable, 30),
    (BINDING_NOT_COMMISSIONED, binding_not_commissioned, 40),
    (EMERGENCY_MODE_LOCKOUT, emergency_mode_lockout, 50),
    (ASSET_IN_MAINTENANCE, asset_in_maintenance, 60),
    (STALE_INPUT, stale_input, 70),
    (DUPLICATE_IN_FLIGHT, duplicate_in_flight, 80),
)


def default_registry() -> InterlockRegistry:
    """A fresh registry holding the eight built-in interlocks."""
    registry = InterlockRegistry()
    for name, func, order in BUILTIN_INTERLOCKS:
        registry.register(func, name=name, order=order)
    return registry


_GLOBAL_REGISTRY: InterlockRegistry | None = None


def global_registry() -> InterlockRegistry:
    """Process-wide registry used when a caller supplies none.

    Subsystems extend the control path by registering against this instance at
    import time; :class:`~homestead_twin.commands.manager.CommandManager` picks
    the additions up automatically.
    """
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = default_registry()
    return _GLOBAL_REGISTRY


def reset_global_registry() -> InterlockRegistry:
    """Restore the global registry to the built-ins (used by tests)."""
    global _GLOBAL_REGISTRY
    _GLOBAL_REGISTRY = default_registry()
    return _GLOBAL_REGISTRY
