"""The audited command path: issue -> interlocks -> dispatch -> ack -> result.

This is the only place in the platform that turns an intention into a message
aimed at real equipment, so it is deliberately conservative:

* Nothing is dispatched that has not passed every registered interlock.
* Every outcome -- accepted, rejected, timed out, overridden, superseded,
  cancelled -- is written to :class:`~homestead_twin.models.commands.Command`
  and to the audit log, with who, what, why, when and under which operating
  mode (SDD section 5.7, FR-004).
* The twin tracks **requested** state separately from **actual measured** state
  (SDD section 10.3). ``CurrentState.requested_value`` carries what we asked
  for; only the ingest path writes what actually happened. **Accepted** state is
  the acknowledgement recorded against the command itself, because Levels 0-1
  retain the authority to reject anything Level 3 requests (SDD section 6).
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin import topics
from homestead_twin.commands.interlocks import (
    DUPLICATE_IN_FLIGHT,
    InterlockContext,
    InterlockEvaluation,
    InterlockRegistry,
    global_registry,
    utc,
)
from homestead_twin.commands.modes import ModeManager
from homestead_twin.config import Settings
from homestead_twin.envelope import CommandAckEnvelope, CommandEnvelope
from homestead_twin.models.base import utcnow
from homestead_twin.models.commands import (
    TERMINAL_COMMAND_STATES,
    AuditLogEntry,
    Command,
    CommandResult,
)
from homestead_twin.models.registry import Asset, Point, PointBinding
from homestead_twin.models.telemetry import CurrentState
from homestead_twin.mqtt import MessageBus

logger = logging.getLogger(__name__)

#: Result codes an acknowledgement may carry -> the lifecycle state it produces.
ACK_STATE_MAP = {
    "accepted": "acknowledged",
    "succeeded": "succeeded",
    "failed": "failed",
    "rejected": "rejected",
    "expired": "expired",
}

#: Who the platform names as the reporter when it closes a command itself.
SYSTEM_ACTOR = "twin.command_manager"
INTERLOCK_ACTOR = "twin.interlocks"


# ---------------------------------------------------------------------------
# Errors -- the API layer maps these onto status codes.
# ---------------------------------------------------------------------------


class CommandError(Exception):
    """Base class for command-path refusals."""


class CommandValidationError(CommandError):
    """The request is malformed (missing reason, empty command name...)."""


class UnknownTargetError(CommandError):
    """The asset the command targets is not in the registry."""


class UnknownCommandError(CommandError):
    """No command exists with the given id."""


class CommandStateError(CommandError):
    """The command is not in a state that permits the requested operation."""


# ---------------------------------------------------------------------------
# ULID-like identifiers
# ---------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_command_id(now: dt.datetime | None = None) -> str:
    """A 26-character ULID-like identifier: sortable by issue time.

    48 bits of millisecond timestamp in Crockford base32 followed by 80 bits of
    randomness, exactly like a ULID. Implemented here rather than pulled in as a
    dependency -- the platform must build and run offline (SDD section 5.1).
    Lexicographic order equals chronological order, which is what makes command
    logs readable without a sort key.
    """
    now = now or utcnow()
    milliseconds = int(now.timestamp() * 1000)
    return _encode(milliseconds, 10) + _encode(secrets.randbits(80), 16)


# ---------------------------------------------------------------------------
# Request / actor
# ---------------------------------------------------------------------------


class _PrincipalLike(Protocol):  # pragma: no cover - structural typing only
    name: str
    role: str
    kind: str


@dataclass(frozen=True)
class ServiceActor:
    """Non-human issuer (a rule, the EMS, a schedule).

    Kept distinct from an API principal so ``issued_by_kind`` is honest: a
    service can never satisfy the human-only maintenance override.
    """

    name: str
    role: str = "operator"
    kind: str = "service"

    def has_role(self, minimum: str) -> bool:  # pragma: no cover - parity with Principal
        from homestead_twin.commands.interlocks import role_at_least

        return role_at_least(self.role, minimum)


@dataclass
class CommandRequest:
    """What a caller asks for, before the platform decides anything."""

    asset_id: str
    command: str
    reason: str
    value: Any = None
    #: Defaults to ``command``; set when the command name and the registry point
    #: name differ.
    point_name: str | None = None
    expires_at: dt.datetime | None = None
    ttl_s: int | None = None
    idempotency_key: str | None = None
    #: Evaluate everything, publish nothing.
    dry_run: bool = False
    #: Human maintainer bypassing a maintenance lockout (audited).
    maintenance_override: bool = False
    priority: int = 100
    requires_ack: bool = True
    #: Point IDs whose measurements this decision depends on. A bad, stale or
    #: missing value on any of them refuses the command.
    depends_on: Sequence[str] = ()
    correlation_id: str | None = None
    #: Overrides the principal's kind when a human issues on behalf of a rule.
    issuer_kind: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def target_point_name(self) -> str:
        return self.point_name or self.command


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class CommandManager:
    """Issues, dispatches, acknowledges, expires and cancels commands."""

    def __init__(
        self,
        session: Session,
        bus: MessageBus,
        settings: Settings,
        *,
        interlocks: InterlockRegistry | None = None,
        mode_manager: ModeManager | None = None,
        clock=None,
    ) -> None:
        self.session = session
        self.bus = bus
        self.settings = settings
        self.interlocks = interlocks if interlocks is not None else global_registry()
        self.modes = mode_manager or ModeManager(session, site_id=settings.site_id, clock=clock)
        self._clock = clock or utcnow

    # -- small helpers ---------------------------------------------------
    def _now(self, now: dt.datetime | None = None) -> dt.datetime:
        return now or self._clock()

    @staticmethod
    def _actor(principal: _PrincipalLike | None) -> tuple[str, str, str]:
        if principal is None:
            return (SYSTEM_ACTOR, "operator", "service")
        return (
            getattr(principal, "name", "unknown"),
            getattr(principal, "role", "viewer"),
            getattr(principal, "kind", "human"),
        )

    def _audit(
        self,
        *,
        actor: str,
        actor_role: str | None,
        action: str,
        target_id: str | None,
        outcome: str,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
        occurred_at: dt.datetime,
        target_type: str = "command",
    ) -> AuditLogEntry:
        entry = AuditLogEntry(
            actor=actor,
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            reason=reason,
            detail=detail or {},
            occurred_at=occurred_at,
        )
        self.session.add(entry)
        return entry

    @staticmethod
    def _add_result(
        command: Command,
        result: str,
        *,
        reported_at: dt.datetime,
        detail: str | None = None,
        reported_by: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> CommandResult:
        row = CommandResult(
            command_id=command.command_id,
            result=result,
            detail=detail,
            reported_by=reported_by,
            reported_at=reported_at,
            payload=payload,
        )
        command.results.append(row)
        return row

    # -- reads -----------------------------------------------------------
    def get(self, command_id: str) -> Command | None:
        return self.session.get(Command, command_id)

    def require(self, command_id: str) -> Command:
        command = self.get(command_id)
        if command is None:
            raise UnknownCommandError(f"No command with id {command_id!r}")
        return command

    def list_commands(
        self,
        *,
        asset_id: str | None = None,
        state: str | None = None,
        since: dt.datetime | None = None,
        limit: int = 50,
    ) -> list[Command]:
        stmt = select(Command)
        if asset_id:
            stmt = stmt.where(Command.asset_id == asset_id)
        if state:
            stmt = stmt.where(Command.state == state)
        if since:
            stmt = stmt.where(Command.issued_at >= since)
        stmt = stmt.order_by(Command.issued_at.desc(), Command.command_id.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars())

    # -- issue -----------------------------------------------------------
    def issue(
        self,
        request: CommandRequest,
        principal: _PrincipalLike | None = None,
        *,
        now: dt.datetime | None = None,
        commit: bool = True,
    ) -> Command:
        """Validate, evaluate interlocks, record and (when permitted) dispatch.

        Always returns a persisted :class:`Command`. A refusal is a *recorded*
        command in state ``rejected`` carrying the full interlock evaluation --
        FR-004 requires every supervisory command to be recorded, and the ones
        the platform refused are the interesting half of that record.
        """
        now = self._now(now)
        actor, actor_role, actor_kind = self._actor(principal)
        issuer_kind = request.issuer_kind or actor_kind

        asset_id = (request.asset_id or "").strip()
        command_name = (request.command or "").strip()
        reason = (request.reason or "").strip()
        if not asset_id:
            raise CommandValidationError("asset_id is required")
        if not command_name:
            raise CommandValidationError("command is required")
        if not reason:
            raise CommandValidationError("A command requires a reason (SDD section 5.7)")

        # Idempotency: replaying a key returns the original decision unchanged.
        if request.idempotency_key:
            existing = self.session.execute(
                select(Command).where(Command.idempotency_key == request.idempotency_key)
            ).scalars().first()
            if existing is not None:
                self._audit(
                    actor=actor,
                    actor_role=actor_role,
                    action="command.issue",
                    target_id=existing.command_id,
                    outcome="idempotent_replay",
                    reason=reason,
                    detail={
                        "idempotency_key": request.idempotency_key,
                        "original_state": existing.state,
                    },
                    occurred_at=now,
                )
                if commit:
                    self.session.commit()
                logger.info(
                    "Idempotent replay of %s (key=%s)", existing.command_id, request.idempotency_key
                )
                return existing

        asset = self.session.get(Asset, asset_id)
        if asset is None:
            raise UnknownTargetError(f"Asset {asset_id!r} is not in the registry")

        point_name = request.point_name or command_name
        point_id = topics.point_id(asset_id, point_name)
        point = self.session.get(Point, point_id)
        binding = self.session.get(PointBinding, point_id)

        chain = self.modes.mode_chain(asset_id, now)
        context = InterlockContext(
            settings=self.settings,
            now=now,
            principal_name=actor,
            principal_role=actor_role,
            issuer_kind=issuer_kind,
            asset=asset,
            point=point,
            binding=binding,
            point_id=point_id,
            effective_mode=chain.effective,
            mode_chain=chain.as_dict(),
            extra=dict(request.metadata),
        )
        evaluation = self.interlocks.evaluate(self.session, request, context)

        expires_at = request.expires_at
        if expires_at is None:
            ttl = request.ttl_s if request.ttl_s is not None else self.settings.command_default_ttl_s
            expires_at = now + dt.timedelta(seconds=int(ttl)) if ttl else None

        command = Command(
            command_id=new_command_id(now),
            asset_id=asset_id,
            point_id=point_id,
            command=command_name,
            value=request.value,
            issued_by=actor,
            issued_by_kind=issuer_kind,
            reason=reason,
            operating_mode=chain.effective,
            priority=request.priority,
            issued_at=now,
            expires_at=expires_at,
            requires_ack=request.requires_ack,
            state="pending",
            idempotency_key=request.idempotency_key or None,
            interlocks_evaluated=evaluation.records,
            correlation_id=request.correlation_id,
        )
        self.session.add(command)

        audit_detail = {
            "command": command_name,
            "point_id": point_id,
            "value": request.value,
            "operating_mode": chain.effective,
            "mode_chain": chain.as_dict(),
            "dry_run": bool(request.dry_run),
            "interlocks": evaluation.records,
        }

        if not evaluation.allowed:
            denial = evaluation.first_denial
            command.state = "rejected"
            command.state_reason = f"{denial.code}: {denial.reason}"
            command.completed_at = now
            self._add_result(
                command,
                "rejected",
                reported_at=now,
                detail=command.state_reason,
                reported_by=INTERLOCK_ACTOR,
                payload={"interlocks": [r.as_record() for r in evaluation.denials]},
            )
            self._audit(
                actor=actor,
                actor_role=actor_role,
                action="command.issue",
                target_id=command.command_id,
                outcome="rejected",
                reason=reason,
                detail={**audit_detail, "denied_by": list(evaluation.denial_codes)},
                occurred_at=now,
            )
            if commit:
                self.session.commit()
            logger.warning(
                "Command %s refused for %s: %s", command.command_id, asset_id, command.state_reason
            )
            return command

        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="command.issue",
            target_id=command.command_id,
            outcome="accepted",
            reason=reason,
            detail=audit_detail,
            occurred_at=now,
        )
        # A bypassed interlock gets its own audit row so the override is never
        # buried inside a command record.
        for override in evaluation.overrides:
            self._audit(
                actor=actor,
                actor_role=actor_role,
                action="command.interlock_override",
                target_id=command.command_id,
                outcome="overridden",
                reason=override.reason,
                detail={"code": override.code, "detail": override.detail},
                occurred_at=now,
            )

        self._supersede(
            command,
            evaluation.detail_for(DUPLICATE_IN_FLIGHT).get("supersedes", []),
            now=now,
            actor=actor,
            actor_role=actor_role,
        )

        if evaluation.dispatch_allowed:
            self.dispatch(command, now=now, commit=False)
        else:
            blocked = ", ".join(r.code for r in evaluation.blocking_dispatch)
            command.state = "cancelled"
            command.state_reason = f"Not dispatched ({blocked}): evaluation only"
            command.completed_at = now
            self._add_result(
                command,
                "not_dispatched",
                reported_at=now,
                detail=command.state_reason,
                reported_by=SYSTEM_ACTOR,
            )
            self._audit(
                actor=actor,
                actor_role=actor_role,
                action="command.dry_run",
                target_id=command.command_id,
                outcome="not_dispatched",
                reason=reason,
                detail={"blocked_by": [r.code for r in evaluation.blocking_dispatch]},
                occurred_at=now,
            )

        if commit:
            self.session.commit()
        return command

    # -- dispatch --------------------------------------------------------
    def dispatch(
        self, command: Command, *, now: dt.datetime | None = None, commit: bool = True
    ) -> Command:
        """Publish the SDD section 10.3 envelope and record the request.

        The dispatch topic comes from the commissioned binding when it declares
        one; otherwise it is derived from the canonical asset identity. If
        neither yields a topic the command fails rather than guessing -- an
        unaddressed command is an unsafe command.
        """
        now = self._now(now)
        if command.state != "pending":
            raise CommandStateError(
                f"Command {command.command_id} is {command.state!r}; only 'pending' may dispatch"
            )

        topic = self._resolve_topic(command)
        if topic is None:
            command.state = "failed"
            command.state_reason = (
                "No dispatch topic: the binding declares none and the asset identity "
                "cannot be projected onto a topic"
            )
            command.completed_at = now
            self._add_result(
                command,
                "failed",
                reported_at=now,
                detail=command.state_reason,
                reported_by=SYSTEM_ACTOR,
            )
            self._audit(
                actor=command.issued_by,
                actor_role=None,
                action="command.dispatch",
                target_id=command.command_id,
                outcome="failed",
                reason=command.state_reason,
                detail={"asset_id": command.asset_id, "point_id": command.point_id},
                occurred_at=now,
            )
            if commit:
                self.session.commit()
            logger.error("Command %s has no dispatch topic", command.command_id)
            return command

        envelope = CommandEnvelope(
            command_id=command.command_id,
            issued_at=utc(command.issued_at) or now,
            issued_by=command.issued_by,
            asset_id=command.asset_id,
            command=command.command,
            value=command.value,
            reason=command.reason,
            expires_at=utc(command.expires_at),
            requires_ack=bool(command.requires_ack),
            operating_mode=command.operating_mode,
            priority=command.priority,
        )

        try:
            self.bus.publish(topic, envelope.to_payload(), qos=1)
        except Exception as exc:  # noqa: BLE001 - a broken bus must not crash the API
            logger.exception("Failed to publish command %s to %s", command.command_id, topic)
            command.state = "failed"
            command.state_reason = f"Publish failed: {exc!r}"
            command.completed_at = now
            command.dispatch_topic = topic
            self._add_result(
                command,
                "failed",
                reported_at=now,
                detail=command.state_reason,
                reported_by=SYSTEM_ACTOR,
            )
            self._audit(
                actor=command.issued_by,
                actor_role=None,
                action="command.dispatch",
                target_id=command.command_id,
                outcome="failed",
                reason=command.state_reason,
                detail={"topic": topic},
                occurred_at=now,
            )
            if commit:
                self.session.commit()
            return command

        command.dispatch_topic = topic
        command.envelope = envelope.model_dump(mode="json", exclude_none=True)
        command.dispatched_at = now
        command.state = "dispatched"
        command.state_reason = None
        self._record_requested_state(command, now)
        self._audit(
            actor=command.issued_by,
            actor_role=None,
            action="command.dispatch",
            target_id=command.command_id,
            outcome="dispatched",
            reason=command.reason,
            detail={"topic": topic, "requires_ack": bool(command.requires_ack)},
            occurred_at=now,
        )
        logger.info("Dispatched command %s to %s", command.command_id, topic)

        if not command.requires_ack:
            # Fire-and-forget: close the record now rather than leave it hanging
            # until the TTL sweep invents an expiry that never applied.
            command.state = "succeeded"
            command.completed_at = now
            command.state_reason = "Dispatched; no acknowledgement required"
            self._add_result(
                command,
                "succeeded",
                reported_at=now,
                detail=command.state_reason,
                reported_by=SYSTEM_ACTOR,
            )

        if commit:
            self.session.commit()
        return command

    def _resolve_topic(self, command: Command) -> str | None:
        if command.point_id:
            binding = self.session.get(PointBinding, command.point_id)
            if binding is not None and binding.command_topic:
                return binding.command_topic
        try:
            return topics.command_topic(
                command.asset_id, command.command, base=self.settings.mqtt_base_topic
            )
        except topics.TopicError as exc:
            logger.warning("Cannot derive a command topic for %s: %s", command.asset_id, exc)
            return None

    def _record_requested_state(self, command: Command, now: dt.datetime) -> None:
        """Store requested-vs-actual for the UI (SDD sections 10.3 and 17.4)."""
        point_id = command.point_id
        if not point_id:
            return
        state = self.session.get(CurrentState, point_id)
        if state is None:
            if self.session.get(Point, point_id) is None:
                return
            asset_id, _, point_name = point_id.partition("/")
            state = CurrentState(
                point_id=point_id,
                asset_id=asset_id,
                point_name=point_name,
                # Nothing has been measured yet; claiming "good" would be a lie
                # the interlocks would later believe.
                quality="uncertain",
                source=SYSTEM_ACTOR,
            )
            self.session.add(state)
        state.requested_value = {
            "value": command.value,
            "command": command.command,
            "command_id": command.command_id,
            "issued_by": command.issued_by,
            "reason": command.reason,
        }
        state.requested_at = now

    def _supersede(
        self,
        command: Command,
        command_ids: Sequence[str],
        *,
        now: dt.datetime,
        actor: str,
        actor_role: str | None,
    ) -> list[Command]:
        superseded: list[Command] = []
        for command_id in command_ids:
            if command_id == command.command_id:
                continue
            older = self.session.get(Command, command_id)
            if older is None or older.is_terminal:
                continue
            older.state = "superseded"
            older.state_reason = f"Superseded by {command.command_id}"
            older.completed_at = now
            self._add_result(
                older,
                "superseded",
                reported_at=now,
                detail=older.state_reason,
                reported_by=SYSTEM_ACTOR,
            )
            self._audit(
                actor=actor,
                actor_role=actor_role,
                action="command.supersede",
                target_id=older.command_id,
                outcome="superseded",
                reason=older.state_reason,
                detail={"superseded_by": command.command_id, "asset_id": older.asset_id},
                occurred_at=now,
            )
            superseded.append(older)
            logger.info("Command %s superseded by %s", older.command_id, command.command_id)
        return superseded

    # -- acknowledgements ------------------------------------------------
    def record_ack(
        self,
        ack: CommandAckEnvelope,
        *,
        now: dt.datetime | None = None,
        commit: bool = True,
    ) -> Command | None:
        """Apply a device acknowledgement or final result.

        Idempotent by design: acks for unknown, mismatched or already-terminal
        commands are logged and dropped rather than raising, because an MQTT
        redelivery must never take the dispatch service down.
        """
        now = self._now(now)
        reported_at = utc(ack.reported_at) or now
        command = self.session.get(Command, ack.command_id)

        if command is None:
            logger.warning("Ack for unknown command %s (asset %s)", ack.command_id, ack.asset_id)
            self._audit(
                actor=ack.reported_by or "unknown.device",
                actor_role="device",
                action="command.ack",
                target_id=ack.command_id,
                outcome="unknown_command",
                reason=ack.detail,
                detail={"asset_id": ack.asset_id, "result": ack.result},
                occurred_at=now,
            )
            if commit:
                self.session.commit()
            return None

        if ack.asset_id and ack.asset_id != command.asset_id:
            logger.warning(
                "Ack for %s claims asset %s but the command targets %s",
                ack.command_id,
                ack.asset_id,
                command.asset_id,
            )
            self._audit(
                actor=ack.reported_by or "unknown.device",
                actor_role="device",
                action="command.ack",
                target_id=command.command_id,
                outcome="asset_mismatch",
                reason=ack.detail,
                detail={"claimed_asset_id": ack.asset_id, "asset_id": command.asset_id},
                occurred_at=now,
            )
            if commit:
                self.session.commit()
            return command

        if command.is_terminal:
            logger.info(
                "Ignoring %s ack for already-terminal command %s (%s)",
                ack.result,
                command.command_id,
                command.state,
            )
            self._audit(
                actor=ack.reported_by or command.asset_id,
                actor_role="device",
                action="command.ack",
                target_id=command.command_id,
                outcome="ignored_terminal",
                reason=ack.detail,
                detail={"result": ack.result, "state": command.state},
                occurred_at=now,
            )
            if commit:
                self.session.commit()
            return command

        self._add_result(
            command,
            ack.result,
            reported_at=reported_at,
            detail=ack.detail,
            reported_by=ack.reported_by,
            payload=ack.payload,
        )

        new_state = ACK_STATE_MAP.get(ack.result)
        if new_state is None:  # pragma: no cover - envelope validation prevents this
            logger.warning("Unknown ack result %r for %s", ack.result, command.command_id)
            if commit:
                self.session.commit()
            return command

        command.state = new_state
        command.state_reason = ack.detail
        if ack.result == "accepted":
            command.acknowledged_at = reported_at
        else:
            if command.acknowledged_at is None:
                command.acknowledged_at = reported_at
            command.completed_at = reported_at

        self._audit(
            actor=ack.reported_by or command.asset_id,
            actor_role="device",
            action="command.ack",
            target_id=command.command_id,
            outcome=ack.result,
            reason=ack.detail,
            detail={"state": command.state, "asset_id": command.asset_id},
            occurred_at=now,
        )
        if commit:
            self.session.commit()
        logger.info("Command %s -> %s (%s)", command.command_id, command.state, ack.result)
        return command

    # -- expiry ----------------------------------------------------------
    def expire_due(
        self, now: dt.datetime | None = None, *, commit: bool = True
    ) -> list[Command]:
        """Expire every non-terminal command whose TTL has elapsed.

        SDD section 5.7 counts "timed out" as an outcome that must be recorded,
        and a command with no final result is worse than a rejected one: it
        leaves the twin believing a request may still be in flight.
        """
        now = self._now(now)
        stmt = select(Command).where(
            Command.state.not_in(sorted(TERMINAL_COMMAND_STATES)),
            Command.expires_at.is_not(None),
            Command.expires_at <= now,
        )
        expired: list[Command] = []
        for command in self.session.execute(stmt).scalars():
            command.state = "expired"
            command.state_reason = "Time-to-live elapsed without a final result"
            command.completed_at = now
            self._add_result(
                command,
                "expired",
                reported_at=now,
                detail=command.state_reason,
                reported_by=SYSTEM_ACTOR,
            )
            self._audit(
                actor=SYSTEM_ACTOR,
                actor_role="service",
                action="command.expire",
                target_id=command.command_id,
                outcome="expired",
                reason=command.state_reason,
                detail={
                    "asset_id": command.asset_id,
                    "expires_at": (utc(command.expires_at) or now).isoformat(),
                },
                occurred_at=now,
            )
            expired.append(command)
            logger.warning("Command %s expired without a result", command.command_id)
        if expired and commit:
            self.session.commit()
        return expired

    # -- cancel ----------------------------------------------------------
    def cancel(
        self,
        command_id: str,
        principal: _PrincipalLike | None,
        reason: str,
        *,
        now: dt.datetime | None = None,
        commit: bool = True,
    ) -> Command:
        """Cancel a command that has not reached a terminal state.

        Cancellation stops the platform waiting on the request. It does not
        recall a message already delivered to a controller -- Level 1 owns what
        it has accepted (SDD section 6), so the operator still needs the local
        stop for equipment that is already moving.
        """
        now = self._now(now)
        actor, actor_role, _kind = self._actor(principal)
        reason = (reason or "").strip()
        if not reason:
            raise CommandValidationError("Cancelling a command requires a reason")

        command = self.get(command_id)
        if command is None:
            self._audit(
                actor=actor,
                actor_role=actor_role,
                action="command.cancel",
                target_id=command_id,
                outcome="unknown_command",
                reason=reason,
                detail={},
                occurred_at=now,
            )
            if commit:
                self.session.commit()
            raise UnknownCommandError(f"No command with id {command_id!r}")

        if command.is_terminal:
            self._audit(
                actor=actor,
                actor_role=actor_role,
                action="command.cancel",
                target_id=command_id,
                outcome="refused",
                reason=reason,
                detail={"state": command.state},
                occurred_at=now,
            )
            if commit:
                self.session.commit()
            raise CommandStateError(
                f"Command {command_id} is already {command.state!r} and cannot be cancelled"
            )

        command.state = "cancelled"
        command.state_reason = f"Cancelled by {actor}: {reason}"
        command.completed_at = now
        self._add_result(
            command,
            "cancelled",
            reported_at=now,
            detail=command.state_reason,
            reported_by=actor,
        )
        self._audit(
            actor=actor,
            actor_role=actor_role,
            action="command.cancel",
            target_id=command.command_id,
            outcome="cancelled",
            reason=reason,
            detail={"asset_id": command.asset_id, "dispatched": command.dispatched_at is not None},
            occurred_at=now,
        )
        if commit:
            self.session.commit()
        logger.info("Command %s cancelled by %s", command.command_id, actor)
        return command


__all__ = [
    "ACK_STATE_MAP",
    "CommandError",
    "CommandManager",
    "CommandRequest",
    "CommandStateError",
    "CommandValidationError",
    "InterlockEvaluation",
    "ServiceActor",
    "UnknownCommandError",
    "UnknownTargetError",
    "new_command_id",
]
