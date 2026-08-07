"""Energy-management entities (SDD sections 30-36).

The EMS is a supervisory allocator, not a universal relay board. It publishes an
energy state and grants time-limited power budgets; the BMS, inverters,
generator controller and local PLCs keep immediate equipment authority.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from homestead_twin.models.base import (
    ID_LEN,
    POINT_ID_LEN,
    Base,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

ENERGY_STATES = (
    "COMMISSIONING",
    "MAINTENANCE",
    "SURPLUS",
    "NORMAL",
    "CONSERVE",
    "CRITICAL_RESERVE",
    "GENERATOR_SUPPORT",
    "EMERGENCY",
    "BLACK_START",
    "DEGRADED_SENSOR",
)

#: States that must not be left automatically (SDD sections 30.7, 11).
LATCHING_ENERGY_STATES = frozenset({"EMERGENCY", "MAINTENANCE", "COMMISSIONING"})


class PowerLoadProfile(Base, TimestampMixin):
    """The load schedule record required by SDD section 31.1."""

    __tablename__ = "power_load_profiles"
    __table_args__ = (Index("ix_load_profile_tier", "base_tier"),)

    asset_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    base_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_tier: Mapped[int | None] = mapped_column(Integer)
    tier_override_reason: Mapped[str | None] = mapped_column(Text)
    tier_override_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    criticality: Mapped[str] = mapped_column(String(40), nullable=False)
    rated_power_kw: Mapped[float | None] = mapped_column(Float)
    measured_power_point: Mapped[str | None] = mapped_column(String(POINT_ID_LEN))
    estimated_power_kw: Mapped[float | None] = mapped_column(Float)
    branch_circuit: Mapped[str | None] = mapped_column(String(120))
    panel_asset_id: Mapped[str | None] = mapped_column(String(ID_LEN))
    control_method: Mapped[str] = mapped_column(String(60), default="not_controllable")

    minimum_service: Mapped[dict] = mapped_column(JSONType, default=dict)
    restart: Mapped[dict] = mapped_column(JSONType, default=dict)
    shed: Mapped[dict] = mapped_column(JSONType, default=dict)
    restoration_group: Mapped[str | None] = mapped_column(String(60))
    restoration_order: Mapped[int | None] = mapped_column(Integer)
    shed_group: Mapped[str | None] = mapped_column(String(60))
    shed_order: Mapped[int | None] = mapped_column(Integer)

    minimum_on_time_s: Mapped[int | None] = mapped_column(Integer)
    minimum_off_time_s: Mapped[int | None] = mapped_column(Integer)
    inrush_class: Mapped[str | None] = mapped_column(String(20))

    data_status: Mapped[str] = mapped_column(String(30), default="estimated")
    open_fields: Mapped[list] = mapped_column(JSONType, default=list)
    notes: Mapped[list] = mapped_column(JSONType, default=list)


class PowerBudgetLease(Base, TimestampMixin):
    """A time-limited power grant (SDD section 31.4).

    A lease is an allocation, never a safety permissive: subsystems must remain
    safe when a lease expires or is revoked.
    """

    __tablename__ = "power_budget_leases"
    __table_args__ = (Index("ix_lease_asset_state", "asset_id", "state"),)

    lease_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)
    granted_kw: Mapped[float] = mapped_column(Float, nullable=False)
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    revocable: Mapped[bool] = mapped_column(Boolean, default=True)
    requested_by: Mapped[str | None] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(24), default="active")  # active|expired|revoked|denied
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)


class EnergyStateTransition(Base, UUIDPrimaryKeyMixin):
    """Append-only EMS state history."""

    __tablename__ = "energy_state_transitions"
    __table_args__ = (Index("ix_energy_transition_ts", "occurred_at"),)

    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30), nullable=False)
    trigger: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    inputs_snapshot: Mapped[dict] = mapped_column(JSONType, default=dict)
    derived_snapshot: Mapped[dict] = mapped_column(JSONType, default=dict)
    actor: Mapped[str | None] = mapped_column(String(160))
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EnergyStateSnapshot(Base, TimestampMixin):
    """Singleton row holding the current published EMS state and derived values."""

    __tablename__ = "energy_state_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    state: Mapped[str] = mapped_column(String(30), default="COMMISSIONING", nullable=False)
    entered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    candidate_state: Mapped[str | None] = mapped_column(String(30))
    candidate_since: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    frozen_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    frozen_by: Mapped[str | None] = mapped_column(String(160))

    inputs: Mapped[dict] = mapped_column(JSONType, default=dict)
    derived: Mapped[dict] = mapped_column(JSONType, default=dict)
    data_quality: Mapped[str] = mapped_column(String(20), default="unknown")
    shed_groups_active: Mapped[list] = mapped_column(JSONType, default=list)
    generator_request: Mapped[str | None] = mapped_column(String(40))
    last_evaluated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class LoadShedAction(Base, UUIDPrimaryKeyMixin):
    """Record of a shed or restore action taken against a load."""

    __tablename__ = "load_shed_actions"
    __table_args__ = (Index("ix_shed_action_ts", "occurred_at"),)

    asset_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # shed|restore|reduce
    group: Mapped[str | None] = mapped_column(String(60))
    energy_state: Mapped[str | None] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(Text)
    command_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24), default="requested")
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
