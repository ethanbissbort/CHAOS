"""Alarm entities (SDD section 14).

Lifecycle: Detected -> Active -> Acknowledged -> Mitigated -> Cleared -> Reviewed.
Alarms are correlated into incidents so that one power-container outage does not
produce hundreds of independent notifications.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from homestead_twin.models.base import (
    ID_LEN,
    POINT_ID_LEN,
    Base,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

SEVERITIES = ("info", "warning", "major", "critical", "emergency")
ALARM_STATES = ("detected", "active", "acknowledged", "mitigated", "cleared", "reviewed")


class AlarmDefinition(Base, TimestampMixin):
    """Everything SDD section 14.3 requires of an alarm definition."""

    __tablename__ = "alarm_definitions"

    alarm_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(30), index=True)

    # Trigger / reset
    point_name: Mapped[str | None] = mapped_column(String(120))
    asset_id: Mapped[str | None] = mapped_column(String(ID_LEN))
    asset_class: Mapped[str | None] = mapped_column(String(80))
    trigger_operator: Mapped[str | None] = mapped_column(String(16))  # lt|le|gt|ge|eq|ne|in|not_in
    trigger_value: Mapped[dict | None] = mapped_column(JSONType)
    reset_operator: Mapped[str | None] = mapped_column(String(16))
    reset_value: Mapped[dict | None] = mapped_column(JSONType)
    trigger_expression: Mapped[str | None] = mapped_column(Text)

    on_delay_s: Mapped[int] = mapped_column(Integer, default=0)
    off_delay_s: Mapped[int] = mapped_column(Integer, default=0)
    hysteresis: Mapped[float | None] = mapped_column(Float)

    affected_assets: Mapped[list] = mapped_column(JSONType, default=list)
    probable_causes: Mapped[list] = mapped_column(JSONType, default=list)
    automatic_action: Mapped[str | None] = mapped_column(Text)
    operator_action: Mapped[str | None] = mapped_column(Text)
    procedure_ref: Mapped[str | None] = mapped_column(String(300))
    escalation_path: Mapped[list] = mapped_column(JSONType, default=list)
    suppression_conditions: Mapped[list] = mapped_column(JSONType, default=list)
    maintenance_mode_behaviour: Mapped[str] = mapped_column(String(40), default="suppress")
    # Root-cause correlation: if the parent alarm is active, this one is a symptom.
    parent_alarm_key: Mapped[str | None] = mapped_column(String(120))
    requires_manual_reset: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[list] = mapped_column(JSONType, default=list)


class Incident(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Parent record grouping correlated alarms (SDD section 14.2)."""

    __tablename__ = "incidents"

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    root_cause_alarm_id: Mapped[str | None] = mapped_column(String(36))
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), default="open", index=True)
    summary: Mapped[str | None] = mapped_column(Text)

    alarms: Mapped[list[Alarm]] = relationship(back_populates="incident")


class Alarm(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An alarm occurrence and its lifecycle state."""

    __tablename__ = "alarms"
    __table_args__ = (
        Index("ix_alarm_state_severity", "state", "severity"),
        Index("ix_alarm_asset", "asset_id"),
        Index("ix_alarm_key_active", "alarm_key", "state"),
    )

    alarm_key: Mapped[str] = mapped_column(
        String(120), ForeignKey("alarm_definitions.alarm_key"), nullable=False
    )
    asset_id: Mapped[str | None] = mapped_column(String(ID_LEN))
    point_id: Mapped[str | None] = mapped_column(String(POINT_ID_LEN))
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="detected", nullable=False)

    detected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(160))
    mitigated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(160))

    trigger_value: Mapped[dict | None] = mapped_column(JSONType)
    message: Mapped[str | None] = mapped_column(Text)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    suppression_reason: Mapped[str | None] = mapped_column(String(200))
    notified: Mapped[bool] = mapped_column(Boolean, default=False)

    incident_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("incidents.id", ondelete="SET NULL"), index=True
    )
    incident: Mapped[Incident | None] = relationship(back_populates="alarms")

    events: Mapped[list[AlarmEvent]] = relationship(
        back_populates="alarm", cascade="all, delete-orphan", order_by="AlarmEvent.occurred_at"
    )


class AlarmEvent(Base, UUIDPrimaryKeyMixin):
    """Append-only alarm lifecycle transition log."""

    __tablename__ = "alarm_events"

    alarm_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("alarms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(160))
    note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Reciprocal of ``Alarm.events``; without it mapper configuration fails for
    # the whole registry, which breaks every model in the platform.
    alarm: Mapped[Alarm] = relationship(back_populates="events")


class NotificationLog(Base, UUIDPrimaryKeyMixin):
    """Delivery record for every notification attempt (SDD FR-007)."""

    __tablename__ = "notification_log"

    alarm_id: Mapped[str | None] = mapped_column(String(36), index=True)
    incident_id: Mapped[str | None] = mapped_column(String(36))
    channel: Mapped[str] = mapped_column(String(24), nullable=False)  # log|email|push|voice
    recipient: Mapped[str | None] = mapped_column(String(240))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="sent")
    detail: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
