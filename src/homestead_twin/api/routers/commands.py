"""Control surface: commands, operating modes and the audit trail.

SDD section 41 fixes the shape of ``POST /commands``, ``GET /commands/{id}`` and
``POST /operating-modes/{domain}``; the rest of this router exists so an
operator can answer "what did the platform do, and why did it refuse?" without
opening a database client.

Two design decisions worth stating plainly:

* **A refusal is still a record.** An interlock denial returns 403 or 409, but
  the :class:`Command` row is written first, in state ``rejected``, with the
  full interlock evaluation. FR-004 requires every supervisory command to be
  recorded and the refused ones are the half that matter after an incident. The
  error body carries the ``command_id`` so the operator can cite it.
* **Every write names an actor and a reason.** ``reason`` is a required body
  field on every mutating endpoint (SDD section 5.7); the principal comes from
  :mod:`homestead_twin.api.deps`.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from homestead_twin.api.deps import (
    AppSettings,
    Bus,
    CurrentPrincipal,
    DbSession,
    MaintainerPrincipal,
    OperatorPrincipal,
)
from homestead_twin.commands.interlocks import (
    ASSET_IN_MAINTENANCE,
    EMERGENCY_MODE_LOCKOUT,
    INTERLOCK_ERROR,
    PHYSICAL_CONTROL_DISABLED,
    utc,
)
from homestead_twin.commands.manager import (
    CommandManager,
    CommandRequest,
    CommandStateError,
    CommandValidationError,
    UnknownCommandError,
    UnknownTargetError,
)
from homestead_twin.commands.modes import (
    MODES,
    SCOPE_TYPES,
    EmergencyLatchedError,
    ModeAuthorizationError,
    ModeError,
    ModeManager,
    UnknownModeError,
)
from homestead_twin.envelope import CommandAckEnvelope
from homestead_twin.models.commands import AuditLogEntry, Command
from homestead_twin.models.registry import Asset

router = APIRouter(tags=["control"])

#: Interlock code -> HTTP status. A refusal about *authority* is 403; a refusal
#: about the target's *state* is 409. Unknown (subsystem-registered) codes fall
#: back to 409 so a new interlock never accidentally reads as "try again later".
INTERLOCK_HTTP_STATUS: dict[str, int] = {
    PHYSICAL_CONTROL_DISABLED: status.HTTP_403_FORBIDDEN,
    ASSET_IN_MAINTENANCE: status.HTTP_403_FORBIDDEN,
    EMERGENCY_MODE_LOCKOUT: status.HTTP_403_FORBIDDEN,
    INTERLOCK_ERROR: status.HTTP_409_CONFLICT,
}
DEFAULT_INTERLOCK_STATUS = status.HTTP_409_CONFLICT

#: Starlette renamed 422 between releases; pin the number, not the alias.
HTTP_422_UNPROCESSABLE = 422


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CommandCreate(BaseModel):
    """Body of ``POST /commands``."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, description="Canonical asset identity (SDD section 25.2)")
    command: str = Field(min_length=1, description="Command / setpoint name")
    value: Any = Field(default=None, description="Requested value")
    reason: str = Field(
        min_length=1,
        description="Why this command is being issued. Mandatory (SDD section 5.7).",
    )
    point_name: str | None = Field(
        default=None, description="Registry point name when it differs from the command name"
    )
    expires_at: dt.datetime | None = None
    ttl_s: int | None = Field(default=None, ge=1, description="TTL when expires_at is not given")
    idempotency_key: str | None = Field(default=None, max_length=120)
    dry_run: bool = Field(default=False, description="Evaluate every interlock but publish nothing")
    maintenance_override: bool = Field(
        default=False,
        description="Maintainer bypass of a maintenance lockout; recorded in the audit log",
    )
    priority: int = 100
    requires_ack: bool = True
    depends_on: list[str] = Field(
        default_factory=list,
        description="Point IDs whose measurements this decision depends on",
    )
    correlation_id: str | None = None


class CommandResultView(BaseModel):
    result: str
    detail: str | None = None
    reported_by: str | None = None
    reported_at: dt.datetime | None = None
    payload: dict[str, Any] | None = None


class CommandView(BaseModel):
    command_id: str
    asset_id: str
    point_id: str | None = None
    command: str
    value: Any = None
    state: str
    state_reason: str | None = None
    issued_by: str
    issued_by_kind: str | None = None
    reason: str
    operating_mode: str | None = None
    priority: int = 100
    requires_ack: bool = True
    issued_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    dispatched_at: dt.datetime | None = None
    acknowledged_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    dispatch_topic: str | None = None
    envelope: dict[str, Any] | None = None
    interlocks_evaluated: list[dict[str, Any]] = Field(default_factory=list)
    idempotency_key: str | None = None
    correlation_id: str | None = None
    is_terminal: bool = False
    dispatched: bool = False
    results: list[CommandResultView] = Field(default_factory=list)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class AckBody(BaseModel):
    """Result reported by an integration that does not speak MQTT."""

    model_config = ConfigDict(extra="forbid")

    result: Literal["accepted", "rejected", "succeeded", "failed", "expired"]
    detail: str | None = None
    reported_by: str | None = None
    reported_at: dt.datetime | None = None
    payload: dict[str, Any] | None = None


class ModeSetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = Field(description=f"One of: {', '.join(MODES)}")
    reason: str = Field(min_length=1)
    expires_at: dt.datetime | None = None
    condition_clear: bool = Field(
        default=False,
        description=(
            "Assert that the condition which triggered an emergency is gone. "
            "Required to leave emergency; defaults to false, so the default is refusal."
        ),
    )
    scope_type: Literal["site", "domain", "asset"] | None = Field(
        default=None, description="Override the inferred scope type"
    )


class ModeView(BaseModel):
    scope_type: str
    scope_id: str
    mode: str
    stored_mode: str | None = None
    previous_mode: str | None = None
    changed_by: str | None = None
    changed_at: dt.datetime | None = None
    reason: str | None = None
    expires_at: dt.datetime | None = None
    auto_clear_allowed: bool = True
    latched: bool = False


class AssetModeView(ModeView):
    effective_mode: str | None = None
    mode_chain: dict[str, str] | None = None


class AuditView(BaseModel):
    actor: str
    actor_role: str | None = None
    action: str
    target_type: str | None = None
    target_id: str | None = None
    outcome: str
    reason: str | None = None
    detail: dict[str, Any] | None = None
    occurred_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _command_view(command: Command) -> CommandView:
    return CommandView(
        command_id=command.command_id,
        asset_id=command.asset_id,
        point_id=command.point_id,
        command=command.command,
        value=command.value,
        state=command.state,
        state_reason=command.state_reason,
        issued_by=command.issued_by,
        issued_by_kind=command.issued_by_kind,
        reason=command.reason,
        operating_mode=command.operating_mode,
        priority=command.priority,
        requires_ack=bool(command.requires_ack),
        issued_at=utc(command.issued_at),
        expires_at=utc(command.expires_at),
        dispatched_at=utc(command.dispatched_at),
        acknowledged_at=utc(command.acknowledged_at),
        completed_at=utc(command.completed_at),
        dispatch_topic=command.dispatch_topic,
        envelope=command.envelope,
        interlocks_evaluated=list(command.interlocks_evaluated or []),
        idempotency_key=command.idempotency_key,
        correlation_id=command.correlation_id,
        is_terminal=command.is_terminal,
        dispatched=command.dispatched_at is not None,
        results=[
            CommandResultView(
                result=r.result,
                detail=r.detail,
                reported_by=r.reported_by,
                reported_at=utc(r.reported_at),
                payload=r.payload,
            )
            for r in command.results
        ],
    )


def _manager(session, bus, settings) -> CommandManager:
    return CommandManager(session, bus, settings)


def _modes(session, settings) -> ModeManager:
    return ModeManager(session, site_id=settings.site_id)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@router.post(
    "/commands",
    response_model=CommandView,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a supervisory command",
)
def create_command(
    body: CommandCreate,
    principal: OperatorPrincipal,
    session: DbSession,
    bus: Bus,
    settings: AppSettings,
) -> CommandView:
    """Request an action of a Level 1/2 controller.

    The platform may refuse. When it does the response is 403 (authority) or 409
    (target state) and carries the whole interlock evaluation, not just the first
    objection, because SDD section 17.4 requires the operator to be shown the
    interlocks preventing operation.
    """
    manager = _manager(session, bus, settings)
    request = CommandRequest(
        asset_id=body.asset_id,
        command=body.command,
        reason=body.reason,
        value=body.value,
        point_name=body.point_name,
        expires_at=body.expires_at,
        ttl_s=body.ttl_s,
        idempotency_key=body.idempotency_key,
        dry_run=body.dry_run,
        maintenance_override=body.maintenance_override,
        priority=body.priority,
        requires_ack=body.requires_ack,
        depends_on=tuple(body.depends_on),
        correlation_id=body.correlation_id,
    )
    try:
        command = manager.issue(request, principal)
    except UnknownTargetError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except CommandValidationError as exc:
        raise HTTPException(HTTP_422_UNPROCESSABLE, str(exc)) from exc

    view = _command_view(command)
    if command.state == "rejected":
        records = view.interlocks_evaluated
        denied = [record for record in records if not record.get("allowed")]
        first = denied[0] if denied else {}
        raise HTTPException(
            INTERLOCK_HTTP_STATUS.get(first.get("code", ""), DEFAULT_INTERLOCK_STATUS),
            detail={
                "message": command.state_reason,
                "command_id": command.command_id,
                "state": command.state,
                "refused_by": first.get("code"),
                "denied_by": [record.get("code") for record in denied],
                "interlocks_evaluated": records,
                "command": view.model_dump(mode="json"),
            },
        )
    return view


@router.get("/commands", response_model=list[CommandView], summary="List commands")
def list_commands(
    principal: CurrentPrincipal,
    session: DbSession,
    bus: Bus,
    settings: AppSettings,
    asset_id: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    since: Annotated[dt.datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[CommandView]:
    commands = _manager(session, bus, settings).list_commands(
        asset_id=asset_id, state=state, since=since, limit=limit
    )
    return [_command_view(command) for command in commands]


@router.get("/commands/{command_id}", response_model=CommandView, summary="Fetch one command")
def get_command(
    command_id: str,
    principal: CurrentPrincipal,
    session: DbSession,
    bus: Bus,
    settings: AppSettings,
) -> CommandView:
    command = _manager(session, bus, settings).get(command_id)
    if command is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No command with id {command_id!r}")
    return _command_view(command)


@router.post("/commands/{command_id}/cancel", response_model=CommandView, summary="Cancel a command")
def cancel_command(
    command_id: str,
    body: CancelBody,
    principal: OperatorPrincipal,
    session: DbSession,
    bus: Bus,
    settings: AppSettings,
) -> CommandView:
    """Stop waiting on a command.

    This does not recall an instruction a controller has already accepted --
    Level 1 owns what it holds (SDD section 6).
    """
    try:
        command = _manager(session, bus, settings).cancel(command_id, principal, body.reason)
    except UnknownCommandError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except CommandStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except CommandValidationError as exc:
        raise HTTPException(HTTP_422_UNPROCESSABLE, str(exc)) from exc
    return _command_view(command)


@router.post(
    "/commands/{command_id}/ack",
    response_model=CommandView,
    summary="Report a command result from a non-MQTT integration",
)
def ack_command(
    command_id: str,
    body: AckBody,
    principal: OperatorPrincipal,
    session: DbSession,
    bus: Bus,
    settings: AppSettings,
) -> CommandView:
    manager = _manager(session, bus, settings)
    command = manager.get(command_id)
    if command is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No command with id {command_id!r}")
    ack = CommandAckEnvelope(
        command_id=command_id,
        asset_id=command.asset_id,
        result=body.result,
        detail=body.detail,
        reported_by=body.reported_by or principal.name,
        **({"reported_at": body.reported_at} if body.reported_at else {}),
        payload=body.payload,
    )
    updated = manager.record_ack(ack)
    return _command_view(updated or command)


# ---------------------------------------------------------------------------
# Operating modes
# ---------------------------------------------------------------------------


def _resolve_scope(session, settings, key: str, explicit: str | None) -> tuple[str, str]:
    """Map ``POST /operating-modes/{domain}`` onto a (scope_type, scope_id).

    SDD section 41 names the path segment ``{domain}``, but the mode model
    covers site, domain and asset. The segment is therefore resolved: the
    configured site id (or the literal ``site``) is the site scope, a known asset
    id is the asset scope, anything else is a domain. ``scope_type`` in the body
    settles it explicitly when the caller wants no guessing.
    """
    if explicit:
        return explicit, key
    if key == "site" or key == settings.site_id:
        return "site", settings.site_id
    if session.get(Asset, key) is not None:
        return "asset", key
    return "domain", key


@router.get("/operating-modes", response_model=list[ModeView], summary="List operating modes")
def list_operating_modes(
    principal: CurrentPrincipal, session: DbSession, settings: AppSettings
) -> list[ModeView]:
    return [ModeView(**row) for row in _modes(session, settings).list_modes()]


@router.get(
    "/operating-modes/{scope_type}/{scope_id:path}",
    response_model=AssetModeView,
    summary="Fetch one scope's operating mode",
)
def get_operating_mode(
    scope_type: str,
    scope_id: str,
    principal: CurrentPrincipal,
    session: DbSession,
    settings: AppSettings,
) -> AssetModeView:
    if scope_type not in SCOPE_TYPES:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown scope type {scope_type!r}; expected one of {', '.join(SCOPE_TYPES)}",
        )
    manager = _modes(session, settings)
    if scope_type == "site" and scope_id in {"site", settings.site_id}:
        scope_id = settings.site_id
    row = manager.get_mode(scope_type, scope_id)
    payload: dict[str, Any] = {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "mode": manager.resolve(scope_type, scope_id),
        "stored_mode": row.mode if row else None,
        "previous_mode": row.previous_mode if row else None,
        "changed_by": row.changed_by if row else None,
        "changed_at": utc(row.changed_at) if row else None,
        "reason": row.reason if row else None,
        "expires_at": utc(row.expires_at) if row else None,
        "auto_clear_allowed": bool(row.auto_clear_allowed) if row else True,
        "latched": bool(row and not row.auto_clear_allowed),
    }
    if scope_type == "asset":
        chain = manager.mode_chain(scope_id)
        payload["effective_mode"] = chain.effective
        payload["mode_chain"] = chain.as_dict()
    return AssetModeView(**payload)


@router.post(
    "/operating-modes/{domain}",
    response_model=ModeView,
    summary="Set the operating mode of a site, domain or asset",
)
def set_operating_mode(
    domain: str,
    body: ModeSetBody,
    principal: OperatorPrincipal,
    session: DbSession,
    settings: AppSettings,
) -> ModeView:
    """Change an operating mode (SDD sections 11 and 41).

    Emergency is latching: setting it drops any expiry, and leaving it requires
    ``condition_clear=true`` plus a reason from a named operator. A refusal is
    itself audited.
    """
    manager = _modes(session, settings)
    scope_type, scope_id = _resolve_scope(session, settings, domain, body.scope_type)
    try:
        row = manager.set_mode(
            scope_type,
            scope_id,
            body.mode,
            principal,
            body.reason,
            expires_at=body.expires_at,
            condition_clear=body.condition_clear,
        )
    except UnknownModeError as exc:
        raise HTTPException(HTTP_422_UNPROCESSABLE, str(exc)) from exc
    except ModeAuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except EmergencyLatchedError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "scope_type": scope_type,
                "scope_id": scope_id,
                "mode": "emergency",
                "latched": True,
                "required": ["condition_clear=true", "reason", "operator role"],
            },
        ) from exc
    except ModeError as exc:
        raise HTTPException(HTTP_422_UNPROCESSABLE, str(exc)) from exc

    return ModeView(
        scope_type=row.scope_type,
        scope_id=row.scope_id,
        mode=row.mode,
        stored_mode=row.mode,
        previous_mode=row.previous_mode,
        changed_by=row.changed_by,
        changed_at=utc(row.changed_at),
        reason=row.reason,
        expires_at=utc(row.expires_at),
        auto_clear_allowed=bool(row.auto_clear_allowed),
        latched=not bool(row.auto_clear_allowed),
    )


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


@router.get("/audit", response_model=list[AuditView], summary="Read the control audit trail")
def read_audit(
    principal: MaintainerPrincipal,
    session: DbSession,
    actor: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    since: Annotated[dt.datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[AuditView]:
    """SDD section 15.2: all control actions are audited, and auditable."""
    stmt = select(AuditLogEntry)
    if actor:
        stmt = stmt.where(AuditLogEntry.actor == actor)
    if action:
        stmt = stmt.where(AuditLogEntry.action == action)
    if since:
        stmt = stmt.where(AuditLogEntry.occurred_at >= since)
    stmt = stmt.order_by(AuditLogEntry.occurred_at.desc(), AuditLogEntry.id.desc()).limit(limit)
    return [
        AuditView(
            actor=entry.actor,
            actor_role=entry.actor_role,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            outcome=entry.outcome,
            reason=entry.reason,
            detail=entry.detail,
            occurred_at=utc(entry.occurred_at),
        )
        for entry in session.execute(stmt).scalars()
    ]
