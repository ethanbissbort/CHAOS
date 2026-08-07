"""Maintenance and work-management entities (SDD section 18)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from chaos.models.base import (
    ID_LEN,
    POINT_ID_LEN,
    Base,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class MaintenancePlan(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Rule that generates work for an asset."""

    __tablename__ = "maintenance_plans"
    __table_args__ = (Index("ix_maintenance_plan_asset", "asset_id"),)

    asset_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("assets.asset_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    # calendar | runtime_hours | cycle_count | condition | alarm | seasonal | inspection
    trigger_type: Mapped[str] = mapped_column(String(30), nullable=False)
    interval_days: Mapped[int | None] = mapped_column(Integer)
    interval_runtime_h: Mapped[float | None] = mapped_column(Float)
    interval_cycles: Mapped[int | None] = mapped_column(Integer)
    condition_point: Mapped[str | None] = mapped_column(String(POINT_ID_LEN))
    condition_operator: Mapped[str | None] = mapped_column(String(16))
    condition_value: Mapped[float | None] = mapped_column(Float)
    #: Counter reading captured at the last completion, so runtime/cycle plans
    #: measure wear since the last service rather than since installation.
    counter_baseline: Mapped[float | None] = mapped_column(Float)
    season: Mapped[str | None] = mapped_column(String(30))
    procedure: Mapped[str | None] = mapped_column(Text)
    required_parts: Mapped[list] = mapped_column(JSONType, default=list)
    estimated_duration_min: Mapped[int | None] = mapped_column(Integer)
    last_completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class WorkOrder(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "work_orders"
    __table_args__ = (Index("ix_work_order_state", "state"),)

    asset_id: Mapped[str | None] = mapped_column(String(ID_LEN), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(36))
    alarm_id: Mapped[str | None] = mapped_column(String(36))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    state: Mapped[str] = mapped_column(String(24), default="open")
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_to: Mapped[str | None] = mapped_column(String(160))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completion_note: Mapped[str | None] = mapped_column(Text)
    parts_used: Mapped[list] = mapped_column(JSONType, default=list)


class Inspection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "inspections"

    asset_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False, index=True)
    work_order_id: Mapped[str | None] = mapped_column(String(36))
    inspected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    inspector: Mapped[str | None] = mapped_column(String(160))
    result: Mapped[str] = mapped_column(String(24), nullable=False)  # pass|fail|advisory
    findings: Mapped[str | None] = mapped_column(Text)
    measurements: Mapped[dict] = mapped_column(JSONType, default=dict)


class Calibration(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Sensor calibration record (SDD FR-009, FR-304)."""

    __tablename__ = "calibrations"

    point_id: Mapped[str] = mapped_column(String(POINT_ID_LEN), nullable=False, index=True)
    calibrated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    calibrated_by: Mapped[str | None] = mapped_column(String(160))
    method: Mapped[str | None] = mapped_column(String(200))
    reference_standard: Mapped[str | None] = mapped_column(String(200))
    curve: Mapped[dict] = mapped_column(JSONType, default=dict)
    pre_error: Mapped[float | None] = mapped_column(Float)
    post_error: Mapped[float | None] = mapped_column(Float)
    next_due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    valid: Mapped[bool] = mapped_column(Boolean, default=True)


class SparePart(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Consumable or spare linked to assets, with a minimum stock level."""

    __tablename__ = "spare_parts"

    part_number: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    quantity_on_hand: Mapped[int] = mapped_column(Integer, default=0)
    minimum_quantity: Mapped[int] = mapped_column(Integer, default=0)
    location: Mapped[str | None] = mapped_column(String(200))
    linked_assets: Mapped[list] = mapped_column(JSONType, default=list)
    supplier: Mapped[str | None] = mapped_column(String(200))
    unit_cost: Mapped[float | None] = mapped_column(Float)


class CommissioningRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One step of the SDD section 19 commissioning sequence."""

    __tablename__ = "commissioning_records"
    __table_args__ = (Index("ix_commissioning_asset_step", "asset_id", "step"),)

    asset_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(120), nullable=False)
    preconditions: Mapped[str | None] = mapped_column(Text)
    injected_condition: Mapped[str | None] = mapped_column(Text)
    expected_sequence: Mapped[str | None] = mapped_column(Text)
    observed: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(String(24))  # pass|fail|blocked|not_run
    performed_by: Mapped[str | None] = mapped_column(String(160))
    performed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[list] = mapped_column(JSONType, default=list)
