"""Registry entities: identity, topology, points and vendor bindings.

These tables implement the SDD's central separation (section 43):

1. asset identity   -> ``assets``
2. point identity   -> ``point_definitions`` + ``points``
3. vendor binding   -> ``point_bindings``

The registry stores identity, configuration, topology and lifecycle records.
High-volume samples belong to the historian (SDD section 40.1).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from homestead_twin.models.base import (
    ID_LEN,
    POINT_ID_LEN,
    Base,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    utcnow,
)

# ---------------------------------------------------------------------------
# Dictionary layer (loaded from data/asset_class_dictionary.yaml + point_dictionary.yaml)
# ---------------------------------------------------------------------------


class AssetClass(Base, TimestampMixin):
    """An approved asset class from the asset-class dictionary."""

    __tablename__ = "asset_classes"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    purpose: Mapped[str | None] = mapped_column(Text)
    allowed_domains: Mapped[list] = mapped_column(JSONType, default=list)
    required_properties: Mapped[list] = mapped_column(JSONType, default=list)
    default_points: Mapped[list] = mapped_column(JSONType, default=list)
    source_section: Mapped[str | None] = mapped_column(String(40))
    dictionary_status: Mapped[str | None] = mapped_column(String(40))


class PointProfile(Base, TimestampMixin):
    """A reusable named bundle of point names."""

    __tablename__ = "point_profiles"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    point_names: Mapped[list] = mapped_column(JSONType, default=list)


class PointDefinition(Base, TimestampMixin):
    """Canonical point definition -- the meaning of a measured or commanded value."""

    __tablename__ = "point_definitions"

    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    default_class: Mapped[str] = mapped_column(String(12), nullable=False)
    allowed_classes: Mapped[list] = mapped_column(JSONType, default=list)
    data_type: Mapped[str] = mapped_column(String(30), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(30))
    description: Mapped[str | None] = mapped_column(Text)
    control_capable: Mapped[bool] = mapped_column(Boolean, default=False)
    automatic_control_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enum_values: Mapped[list | None] = mapped_column(JSONType)
    applicable_asset_classes: Mapped[list] = mapped_column(JSONType, default=list)
    source_section: Mapped[str | None] = mapped_column(String(40))
    dictionary_status: Mapped[str | None] = mapped_column(String(40))


# ---------------------------------------------------------------------------
# Asset layer
# ---------------------------------------------------------------------------


class Asset(Base, TimestampMixin):
    """A functional position or physical object on the homestead.

    ``asset_id`` is the canonical, never-reused identity (SDD section 25).
    """

    __tablename__ = "assets"

    asset_id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    domain: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    asset_class: Mapped[str] = mapped_column(
        String(80), ForeignKey("asset_classes.name"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="concept", index=True)
    criticality: Mapped[str] = mapped_column(String(30), nullable=False, default="discretionary")
    control_authority: Mapped[str] = mapped_column(String(60), nullable=False, default="none")
    functional_position: Mapped[bool] = mapped_column(Boolean, default=True)

    parent_id: Mapped[str | None] = mapped_column(String(ID_LEN), ForeignKey("assets.asset_id"), index=True)

    location: Mapped[dict] = mapped_column(JSONType, default=dict)
    properties: Mapped[dict] = mapped_column(JSONType, default=dict)
    network: Mapped[dict] = mapped_column(JSONType, default=dict)
    power: Mapped[dict] = mapped_column(JSONType, default=dict)
    dependencies: Mapped[list] = mapped_column(JSONType, default=list)
    manual_override: Mapped[dict] = mapped_column(JSONType, default=dict)
    documentation: Mapped[list] = mapped_column(JSONType, default=list)
    maintenance_plan: Mapped[dict] = mapped_column(JSONType, default=dict)
    point_profile_refs: Mapped[list] = mapped_column(JSONType, default=list)
    source_refs: Mapped[list] = mapped_column(JSONType, default=list)
    open_fields: Mapped[list] = mapped_column(JSONType, default=list)
    tags: Mapped[list] = mapped_column(JSONType, default=list)
    notes: Mapped[list] = mapped_column(JSONType, default=list)

    children: Mapped[list[Asset]] = relationship(back_populates="parent", cascade="all", passive_deletes=True)
    parent: Mapped[Asset | None] = relationship(back_populates="children", remote_side=[asset_id])
    points: Mapped[list[Point]] = relationship(back_populates="asset", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Asset {self.asset_id}>"


class AssetRelationship(Base, TimestampMixin):
    """A typed edge between two assets (SDD section 25.5)."""

    __tablename__ = "asset_relationships"
    __table_args__ = (
        UniqueConstraint("from_asset_id", "relationship_type", "to_asset_id", name="uq_relationship_triple"),
        Index("ix_relationship_from_type", "from_asset_id", "relationship_type"),
        Index("ix_relationship_to_type", "to_asset_id", "relationship_type"),
    )

    relationship_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    from_asset_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("assets.asset_id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(40), nullable=False)
    to_asset_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("assets.asset_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), default="planned")
    notes: Mapped[list] = mapped_column(JSONType, default=list)


class Location(Base, TimestampMixin):
    """Geographic / physical placement for the property map (SDD section 17.2).

    Geometry is stored as GeoJSON so the platform runs without PostGIS. The
    PostgreSQL deployment adds a generated PostGIS column over ``geometry``.
    """

    __tablename__ = "locations"

    asset_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True
    )
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    elevation_m: Mapped[float | None] = mapped_column(Float)
    geometry: Mapped[dict | None] = mapped_column(JSONType)  # GeoJSON geometry
    structure_id: Mapped[str | None] = mapped_column(String(ID_LEN))
    room_id: Mapped[str | None] = mapped_column(String(ID_LEN))
    rack_id: Mapped[str | None] = mapped_column(String(ID_LEN))
    rack_unit_start: Mapped[int | None] = mapped_column(Integer)
    rack_unit_height: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)


class ExternalIdentifier(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Vendor serial, MAC, IP, Modbus unit ID, HA entity ID, LoRa DevEUI...

    Explicitly *not* the canonical identity (SDD section 25.3 rule 7).
    """

    __tablename__ = "external_identifiers"
    __table_args__ = (UniqueConstraint("asset_id", "id_type", "value", name="uq_external_identifier"),)

    asset_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("assets.asset_id", ondelete="CASCADE"), nullable=False, index=True
    )
    id_type: Mapped[str] = mapped_column(String(40), nullable=False)
    value: Mapped[str] = mapped_column(String(240), nullable=False)
    valid_from: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Point layer
# ---------------------------------------------------------------------------


class Point(Base, TimestampMixin):
    """A point instance: one canonical point definition applied to one asset.

    ``point_id`` is always ``<asset_id>/<point_name>`` (SDD section 26.2).
    """

    __tablename__ = "points"
    __table_args__ = (
        UniqueConstraint("asset_id", "point_name", name="uq_point_asset_name"),
        Index("ix_point_asset", "asset_id"),
    )

    point_id: Mapped[str] = mapped_column(String(POINT_ID_LEN), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("assets.asset_id", ondelete="CASCADE"), nullable=False
    )
    point_name: Mapped[str] = mapped_column(String(120), ForeignKey("point_definitions.name"), nullable=False)
    point_class: Mapped[str] = mapped_column(String(12), nullable=False)
    data_type: Mapped[str] = mapped_column(String(30), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(30))
    enum_values: Mapped[list | None] = mapped_column(JSONType)
    control_capable: Mapped[bool] = mapped_column(Boolean, default=False)
    # A control-capable point is still not automatically controllable until
    # commissioning enables its binding (SDD section 47).
    automatic_control_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(40), default="profile")  # profile|class|binding|manual
    limits: Mapped[dict] = mapped_column(JSONType, default=dict)
    historian_policy: Mapped[str | None] = mapped_column(String(40))
    stale_after_s: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)

    asset: Mapped[Asset] = relationship(back_populates="points")

    @staticmethod
    def make_id(asset_id: str, point_name: str) -> str:
        return f"{asset_id}/{point_name}"


class PointBinding(Base, TimestampMixin):
    """How a device currently exposes a canonical point.

    Bindings change over the life of the property; the point identity does not.
    """

    __tablename__ = "point_bindings"

    point_id: Mapped[str] = mapped_column(
        String(POINT_ID_LEN), ForeignKey("points.point_id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False, index=True)
    point_name: Mapped[str] = mapped_column(String(120), nullable=False)
    binding_status: Mapped[str] = mapped_column(String(30), default="tbd", index=True)
    source_protocol: Mapped[str | None] = mapped_column(String(80))
    source_address: Mapped[str | None] = mapped_column(String(240))
    mqtt_topic: Mapped[str | None] = mapped_column(String(300), index=True)
    command_topic: Mapped[str | None] = mapped_column(String(300))
    scale: Mapped[float | None] = mapped_column(Float)
    offset: Mapped[float | None] = mapped_column(Float)
    sample_interval_s: Mapped[int | None] = mapped_column(Integer)
    publish_interval_s: Mapped[int | None] = mapped_column(Integer)
    stale_after_s: Mapped[int | None] = mapped_column(Integer)
    historian_policy: Mapped[str | None] = mapped_column(String(40))
    quality_policy: Mapped[str | None] = mapped_column(String(40))
    automatic_control_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    commissioned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    commissioned_by: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[list] = mapped_column(JSONType, default=list)


class PointSampleIndex(Base, TimestampMixin):
    """Pointer from registry identity to the historian series (SDD section 40.1)."""

    __tablename__ = "point_samples_index"

    point_id: Mapped[str] = mapped_column(
        String(POINT_ID_LEN), ForeignKey("points.point_id", ondelete="CASCADE"), primary_key=True
    )
    historian_backend: Mapped[str] = mapped_column(String(40), default="sql")
    series_key: Mapped[str] = mapped_column(String(300), nullable=False)
    measurement: Mapped[str | None] = mapped_column(String(120))
    retention_policy: Mapped[str | None] = mapped_column(String(60))
    downsample_policy: Mapped[dict] = mapped_column(JSONType, default=dict)
    first_sample_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_sample_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


class Document(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Manual, drawing, photo, warranty or commissioning record."""

    __tablename__ = "documents"

    asset_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("assets.asset_id", ondelete="CASCADE"), index=True
    )
    doc_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    uri: Mapped[str | None] = mapped_column(String(600))
    revision: Mapped[str | None] = mapped_column(String(60))
    issued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class ConfigurationRevision(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Versioned record of any registry or control-configuration change (SDD 5.6)."""

    __tablename__ = "configuration_revisions"
    __table_args__ = (Index("ix_config_revision_target", "target_type", "target_id"),)

    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(POINT_ID_LEN), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    changed_by: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    diff: Mapped[dict] = mapped_column(JSONType, default=dict)
    applied_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    source_package_version: Mapped[str | None] = mapped_column(String(40))
