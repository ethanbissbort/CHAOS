"""Telemetry entities: current-state cache and the relational historian.

SDD section 40.2 requires a current-state model that is reconstructable from
the registry plus the message stream after a service restart.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from homestead_twin.models.base import (
    ID_LEN,
    POINT_ID_LEN,
    Base,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class CurrentState(Base, TimestampMixin):
    """Last known value of one point, with quality and provenance."""

    __tablename__ = "current_state"
    __table_args__ = (Index("ix_current_state_asset", "asset_id"),)

    point_id: Mapped[str] = mapped_column(
        String(POINT_ID_LEN), ForeignKey("points.point_id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)
    point_name: Mapped[str] = mapped_column(String(120), nullable=False)

    value_numeric: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_bool: Mapped[bool | None] = mapped_column(Boolean)
    value_json: Mapped[dict | None] = mapped_column(JSONType)

    unit: Mapped[str | None] = mapped_column(String(30))
    quality: Mapped[str] = mapped_column(String(20), default="good", index=True)
    source: Mapped[str | None] = mapped_column(String(120))
    sequence: Mapped[int | None] = mapped_column(BigInteger)
    schema_version: Mapped[int | None] = mapped_column(Integer)

    ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    stale_after_s: Mapped[int | None] = mapped_column(Integer)

    # Requested vs accepted vs actual (SDD section 10.3).
    requested_value: Mapped[dict | None] = mapped_column(JSONType)
    requested_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    alarm_state: Mapped[str | None] = mapped_column(String(30))

    @property
    def value(self):
        for candidate in (self.value_numeric, self.value_bool, self.value_text, self.value_json):
            if candidate is not None:
                return candidate
        return None


class TelemetrySample(Base, UUIDPrimaryKeyMixin):
    """Relational historian fallback.

    The deployment target is TimescaleDB/InfluxDB; this table keeps the
    platform whole on SQLite and on the low-power secondary node.
    """

    __tablename__ = "telemetry_samples"
    __table_args__ = (
        Index("ix_sample_point_ts", "point_id", "ts"),
        Index("ix_sample_ts", "ts"),
    )

    point_id: Mapped[str] = mapped_column(String(POINT_ID_LEN), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)
    point_name: Mapped[str] = mapped_column(String(120), nullable=False)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value_numeric: Mapped[float | None] = mapped_column(Float)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_bool: Mapped[bool | None] = mapped_column(Boolean)
    unit: Mapped[str | None] = mapped_column(String(30))
    quality: Mapped[str] = mapped_column(String(20), default="good")
    source: Mapped[str | None] = mapped_column(String(120))


class IngestDeadLetter(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Messages that could not be resolved to a registry point.

    Dropping unknown telemetry silently would hide commissioning errors.
    """

    __tablename__ = "ingest_dead_letters"

    topic: Mapped[str] = mapped_column(String(400), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
