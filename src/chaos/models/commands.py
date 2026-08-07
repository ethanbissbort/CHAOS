"""Command and operating-mode entities.

SDD section 5.7: every command records who or what issued it, why, when, under
which operating mode, and whether it was accepted, rejected, timed out or
overridden. SDD section 10.3 defines the wire envelope.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from chaos.models.base import (
    ID_LEN,
    POINT_ID_LEN,
    Base,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

# Command lifecycle states.
COMMAND_STATES = (
    "pending",  # accepted by the API, not yet dispatched
    "dispatched",  # published to the device / controller
    "acknowledged",  # device acknowledged receipt
    "succeeded",  # device reported the final result
    "rejected",  # refused by interlock, permission or local controller
    "failed",  # dispatch or execution error
    "expired",  # TTL elapsed without a final result
    "superseded",  # replaced by a newer command for the same target
    "cancelled",
)

TERMINAL_COMMAND_STATES = frozenset({"succeeded", "rejected", "failed", "expired", "superseded", "cancelled"})


class Command(Base, TimestampMixin):
    """A supervisory command request and its audited lifecycle."""

    __tablename__ = "commands"
    __table_args__ = (
        Index("ix_command_asset_issued", "asset_id", "issued_at"),
        Index("ix_command_state", "state"),
        Index("ix_command_idempotency", "idempotency_key", unique=True),
    )

    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("assets.asset_id", ondelete="CASCADE"), nullable=False
    )
    point_id: Mapped[str | None] = mapped_column(String(POINT_ID_LEN))
    command: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[dict | None] = mapped_column(JSONType)

    issued_by: Mapped[str] = mapped_column(String(160), nullable=False)
    issued_by_kind: Mapped[str] = mapped_column(String(30), default="service")  # human|service|rule
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operating_mode: Mapped[str | None] = mapped_column(String(30))
    priority: Mapped[int] = mapped_column(Integer, default=100)

    issued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    requires_ack: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    state_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(120))
    dispatch_topic: Mapped[str | None] = mapped_column(String(300))
    envelope: Mapped[dict | None] = mapped_column(JSONType)
    interlocks_evaluated: Mapped[list] = mapped_column(JSONType, default=list)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)

    results: Mapped[list[CommandResult]] = relationship(
        back_populates="command", cascade="all, delete-orphan", order_by="CommandResult.reported_at"
    )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_COMMAND_STATES


class CommandResult(Base, UUIDPrimaryKeyMixin):
    """An acknowledgement or final result reported for a command."""

    __tablename__ = "command_results"

    command_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("commands.command_id", ondelete="CASCADE"), nullable=False, index=True
    )
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    reported_by: Mapped[str | None] = mapped_column(String(160))
    reported_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONType)

    command: Mapped[Command] = relationship(back_populates="results")


class OperatingMode(Base, TimestampMixin):
    """Current operating mode of a domain or an individual asset (SDD section 11)."""

    __tablename__ = "operating_modes"
    __table_args__ = (Index("ix_operating_mode_scope", "scope_type", "scope_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)  # domain|asset|site
    scope_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)
    mode: Mapped[str] = mapped_column(String(30), nullable=False, default="automatic")
    previous_mode: Mapped[str | None] = mapped_column(String(30))
    changed_by: Mapped[str | None] = mapped_column(String(160))
    reason: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Emergency mode must not clear itself unless policy allows (SDD section 11).
    auto_clear_allowed: Mapped[bool] = mapped_column(Boolean, default=True)


class ModeTransition(Base, UUIDPrimaryKeyMixin):
    """Append-only log of operating-mode transitions."""

    __tablename__ = "mode_transitions"
    __table_args__ = (Index("ix_mode_transition_scope_ts", "scope_id", "occurred_at"),)

    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)
    from_mode: Mapped[str | None] = mapped_column(String(30))
    to_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    changed_by: Mapped[str | None] = mapped_column(String(160))
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLogEntry(Base, UUIDPrimaryKeyMixin):
    """Security-relevant audit trail (SDD section 15.2: all control actions audited)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts", "occurred_at"),)

    actor: Mapped[str] = mapped_column(String(160), nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(POINT_ID_LEN))
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONType)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
