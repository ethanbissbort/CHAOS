"""Tests for the registry package loader, point materialisation and queries.

The v0.3 package is the contract the rest of the platform is built on, so these
tests assert the real numbers from ``data/`` rather than a fixture double: 90
assets, 99 relationships, 245 bindings, 701 materialised points.

Structural-failure behaviour is exercised against small synthetic packages
written to ``tmp_path`` so that the shipped package stays untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy.orm import sessionmaker

from chaos import topics
from chaos.config import Settings
from chaos.db import build_engine
from chaos.models import (
    Asset,
    AssetClass,
    AssetRelationship,
    ConfigurationRevision,
    ExternalIdentifier,
    Location,
    Point,
    PointBinding,
    PointDefinition,
    PointProfile,
    PointSampleIndex,
)
from chaos.models.base import Base
from chaos.registry import points as points_module
from chaos.registry import service
from chaos.registry.loader import (
    LoadResult,
    RegistryLoadError,
    load_package,
    read_package,
    topological_order,
)

INVERTER = "energy.inverter.power_container.01"
BATTERY = "energy.battery_bank.power_container.01"
GENERATOR = "energy.generator.site.01"
SITE = "site.site.primary.01"

EXPECTED = {
    "asset_classes": 72,
    "point_definitions": 214,
    "point_profiles": 5,
    "assets": 90,
    "relationships": 99,
    "points": 701,
    "bindings": 245,
}


# ---------------------------------------------------------------------------
# Synthetic package helpers
# ---------------------------------------------------------------------------

_CLASSES: dict[str, Any] = {
    "site": {
        "purpose": "Test site.",
        "allowed_domains": ["site"],
        "required_properties": [],
        "default_points": ["availability_state"],
        "source_section": "test",
        "dictionary_status": "test",
    },
    "room": {
        "purpose": "Test room.",
        "allowed_domains": ["structure"],
        "required_properties": [],
        "default_points": [],
        "source_section": "test",
        "dictionary_status": "test",
    },
    "inverter": {
        "purpose": "Test inverter.",
        "allowed_domains": ["energy"],
        "required_properties": [],
        "default_points": ["power_ac_output_kw", "state_operating"],
        "source_section": "test",
        "dictionary_status": "test",
    },
}

_POINTS: dict[str, Any] = {
    "availability_state": {
        "default_class": "DI",
        "allowed_classes": ["DI"],
        "data_type": "enum",
        "unit": None,
        "description": "Availability.",
        "control_capable": False,
        "automatic_control_default": False,
        "applicable_asset_classes": [],
        "source_section": "test",
        "dictionary_status": "test",
        "enum_values": ["online", "offline"],
    },
    "state_operating": {
        "default_class": "DI",
        "allowed_classes": ["DI"],
        "data_type": "enum",
        "unit": None,
        "description": "Operating state.",
        "control_capable": False,
        "automatic_control_default": False,
        "applicable_asset_classes": [],
        "source_section": "test",
        "dictionary_status": "test",
    },
    "power_ac_output_kw": {
        "default_class": "AI",
        "allowed_classes": ["AI"],
        "data_type": "float",
        "unit": "kW",
        "description": "AC output power.",
        "control_capable": False,
        "automatic_control_default": False,
        "applicable_asset_classes": [],
        "source_section": "test",
        "dictionary_status": "test",
    },
    "mode_requested": {
        "default_class": "DO",
        "allowed_classes": ["DO"],
        "data_type": "enum",
        "unit": None,
        "description": "Requested mode.",
        "control_capable": True,
        "automatic_control_default": False,
        "applicable_asset_classes": [],
        "source_section": "test",
        "dictionary_status": "test",
        "enum_values": ["off", "automatic"],
    },
}

_PROFILES = {"common_monitored": ["availability_state"]}


def make_asset(asset_id: str, asset_class: str, domain: str, parent_id: str | None = None, **extra):
    record = {
        "asset_id": asset_id,
        "domain": domain,
        "asset_class": asset_class,
        "name": asset_id,
        "status": "planned",
        "criticality": "important",
        "control_authority": "supervisory",
        "functional_position": True,
        "parent_id": parent_id,
        "location": {},
        "properties": {},
        "external_identifiers": [],
        "network": {},
        "power": {},
        "dependencies": [],
        "manual_override": {},
        "documentation": [],
        "maintenance_plan": {},
        "point_profile_refs": ["common_monitored"],
        "source_refs": [],
        "notes": [],
        "open_fields": [],
        "tags": [],
    }
    record.update(extra)
    return record


def write_package(
    tmp_path: Path,
    *,
    assets,
    relationships=(),
    bindings=(),
    asset_classes=None,
    point_definitions=None,
    profiles=None,
    site_id: str = "site.site.test.01",
) -> Path:
    """Write a minimal four-document package into ``tmp_path``."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)

    (data_dir / "asset_class_dictionary.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.3.0",
                "domains": {"site": "s", "structure": "s", "energy": "e"},
                "lifecycle_statuses": ["concept", "planned", "active"],
                "criticality_levels": ["critical", "important", "discretionary"],
                "control_authorities": ["none", "supervisory"],
                "relationship_types": {
                    "contains": "c",
                    "feeds": "f",
                    "depends_on": "d",
                    "part_of": "p",
                },
                "asset_classes": asset_classes or _CLASSES,
                "point_profiles": profiles or _PROFILES,
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "point_dictionary.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.3.0",
                "point_classes": ["AI", "DI", "DO", "TEXT"],
                "quality_codes": ["good", "bad"],
                "canonical_units": {},
                "point_name_pattern": "^[a-z][a-z0-9_]*$",
                "points": point_definitions or _POINTS,
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "homestead_asset_register.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.3.0",
                "site_id": site_id,
                "design_basis": {
                    "design_stage": "test",
                    "open_design_conflicts": [{"conflict_id": "test.conflict"}],
                },
                "assets": list(assets),
                "relationships": list(relationships),
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "point_bindings.yaml").write_text(
        yaml.safe_dump({"schema_version": "0.3.0", "bindings": list(bindings)}),
        encoding="utf-8",
    )
    return data_dir


@pytest.fixture()
def fk_session_factory(tmp_path):
    """A SQLite session factory with ``PRAGMA foreign_keys=ON`` actually set.

    The shared in-memory test engine does not enforce foreign keys, so the
    parent-before-child insert order has to be proven somewhere that does.
    """
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'fk.db'}")
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False, future=True)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Clean load of the shipped package
# ---------------------------------------------------------------------------


def test_clean_load_reports_the_package_counts(loaded_registry: LoadResult):
    counts = loaded_registry.counts()
    for key, expected in EXPECTED.items():
        assert counts[key] == expected, key
    assert loaded_registry.package_version == "0.3.0"


def test_clean_load_produces_no_warnings(loaded_registry: LoadResult):
    assert loaded_registry.warnings == []
    assert loaded_registry.skipped_assets == []
    assert loaded_registry.ok


def test_clean_load_writes_every_row(db_session, loaded_registry):
    def count(model):
        return db_session.scalar(sa.select(sa.func.count()).select_from(model))

    assert count(AssetClass) == EXPECTED["asset_classes"]
    assert count(PointDefinition) == EXPECTED["point_definitions"]
    assert count(PointProfile) == EXPECTED["point_profiles"]
    assert count(Asset) == EXPECTED["assets"]
    assert count(AssetRelationship) == EXPECTED["relationships"]
    assert count(Point) == EXPECTED["points"]
    assert count(PointBinding) == EXPECTED["bindings"]
    # Every point gets a historian pointer, bound or not.
    assert count(PointSampleIndex) == EXPECTED["points"]


def test_load_is_idempotent(db_session, loaded_registry):
    def snapshot():
        return {
            model.__name__: db_session.scalar(sa.select(sa.func.count()).select_from(model))
            for model in (
                AssetClass,
                PointDefinition,
                PointProfile,
                Asset,
                AssetRelationship,
                Location,
                Point,
                PointBinding,
                PointSampleIndex,
                ExternalIdentifier,
            )
        }

    before = snapshot()
    second = load_package(db_session)
    db_session.commit()

    assert snapshot() == before
    assert second.counts() == loaded_registry.counts()
    assert second.warnings == []


def test_load_records_a_configuration_revision(db_session, loaded_registry):
    revisions = db_session.scalars(
        sa.select(ConfigurationRevision).order_by(ConfigurationRevision.revision)
    ).all()
    assert len(revisions) == 1
    revision = revisions[0]
    assert revision.target_type == "registry"
    assert revision.target_id == SITE
    assert revision.changed_by == "registry.loader"
    assert revision.source_package_version == "0.3.0"
    assert revision.diff["counts"]["assets"] == EXPECTED["assets"]
    assert revision.diff["warnings"] == []

    # The registry converges; the audit trail accumulates.
    load_package(db_session)
    db_session.commit()
    assert db_session.scalar(sa.select(sa.func.count()).select_from(ConfigurationRevision)) == 2
    assert db_session.scalars(
        sa.select(ConfigurationRevision.revision).order_by(ConfigurationRevision.revision)
    ).all() == [1, 2]


# ---------------------------------------------------------------------------
# Point materialisation
# ---------------------------------------------------------------------------


def test_inverter_points_are_the_union_of_class_profile_and_bindings(db_session, loaded_registry):
    materialised = {p.point_name: p for p in service.get_asset_points(db_session, INVERTER)}

    package = read_package()
    class_points = set(package.asset_classes["inverter"]["default_points"])
    profile_points = set(package.point_profiles["common_commandable"])
    binding_points = set(package.bindings_by_asset()[INVERTER])

    assert set(materialised) == class_points | profile_points | binding_points
    assert len(materialised) == 19


def test_point_copies_its_dictionary_definition(db_session, loaded_registry):
    point = service.get_point(db_session, f"{INVERTER}/power_ac_output_kw")
    definition = db_session.get(PointDefinition, "power_ac_output_kw")

    assert point is not None
    assert point.point_id == f"{INVERTER}/power_ac_output_kw"
    assert point.asset_id == INVERTER
    assert point.point_class == definition.default_class == "AI"
    assert point.data_type == definition.data_type == "float"
    assert point.unit == definition.unit == "kW"
    assert point.control_capable is definition.control_capable

    enum_point = service.get_point(db_session, f"{INVERTER}/mode_actual")
    assert enum_point.enum_values == db_session.get(PointDefinition, "mode_actual").enum_values
    assert "automatic" in enum_point.enum_values


def test_point_source_records_where_the_point_came_from(db_session, loaded_registry):
    # Bound points win over the class/profile that also name them.
    assert service.get_point(db_session, f"{INVERTER}/power_ac_output_kw").source == "binding"
    # Profile-only point on a commandable asset.
    assert service.get_point(db_session, f"{INVERTER}/runtime_total_h").source == "profile"
    # A class default point that no binding covers stays "class".
    class_sourced = [
        p.point_name for p in service.get_asset_points(db_session, GENERATOR) if p.source == "class"
    ]
    assert class_sourced, "expected at least one class-sourced point on the generator"
    generator = db_session.get(Asset, GENERATOR)
    class_def = db_session.get(AssetClass, generator.asset_class)
    assert set(class_sourced) <= set(class_def.default_points)


def test_collect_point_names_precedence_is_binding_over_class_over_profile():
    resolved = points_module.collect_point_names(
        default_points=["state_operating", "power_ac_output_kw"],
        profiles={"p": ["state_operating", "availability_state", "power_ac_output_kw"]},
        profile_refs=["p"],
        binding_names=["power_ac_output_kw"],
    )
    assert resolved == {
        "power_ac_output_kw": "binding",
        "state_operating": "class",
        "availability_state": "profile",
    }


def test_unknown_profile_is_reported_not_silently_dropped():
    seen: list[str] = []
    resolved = points_module.collect_point_names(
        profiles={"known": ["availability_state"]},
        profile_refs=["known", "missing"],
        warn=seen.append,
    )
    assert resolved == {"availability_state": "profile"}
    assert any("missing" in message for message in seen)


# --- SDD section 47: automatic control is opt-in, per binding ---------------


def test_automatic_control_allowed_defaults_to_false(db_session, loaded_registry):
    """A point is never automatically controllable without an explicit binding."""
    bound = {b.point_id for b in db_session.scalars(sa.select(PointBinding)).all()}
    granted = {
        b.point_id
        for b in db_session.scalars(
            sa.select(PointBinding).where(PointBinding.automatic_control_allowed.is_(True))
        ).all()
    }

    allowed = {
        p.point_id
        for p in db_session.scalars(sa.select(Point).where(Point.automatic_control_allowed.is_(True))).all()
    }
    assert allowed <= bound, "unbound points must never allow automatic control"
    assert allowed == granted, "the binding is the only thing that can grant automatic control"

    # Every point the package does not bind is denied.
    unbound_allowed = db_session.scalars(
        sa.select(Point)
        .outerjoin(PointBinding, Point.point_id == PointBinding.point_id)
        .where(PointBinding.point_id.is_(None), Point.automatic_control_allowed.is_(True))
    ).all()
    assert unbound_allowed == []


def test_control_capability_alone_does_not_grant_automatic_control(db_session, loaded_registry):
    """SDD 47: 'control-capable' is a dictionary fact, not a commissioning decision."""
    control_capable = db_session.scalars(sa.select(Point).where(Point.control_capable.is_(True))).all()
    assert control_capable, "the package defines control-capable points"

    denied = [p for p in control_capable if not p.automatic_control_allowed]
    assert denied, "expected control-capable points that commissioning has not permitted"
    # e.g. every inverter/generator mode_requested is capable but not permitted.
    inverter_mode = service.get_point(db_session, f"{INVERTER}/mode_requested")
    assert inverter_mode.control_capable is True
    assert inverter_mode.automatic_control_allowed is False
    assert service.get_binding(db_session, inverter_mode.point_id) is None


# --- bindings ---------------------------------------------------------------


def test_binding_keeps_tbd_addresses_and_adds_the_topic_projection(db_session, loaded_registry):
    binding = service.get_binding(db_session, f"{INVERTER}/power_ac_output_kw")
    assert binding is not None
    # Never invent an address (SDD 47).
    assert binding.source_address == "TBD"
    assert binding.source_protocol == "modbus_tcp_or_vendor_api_TBD"
    assert binding.binding_status == "tbd"
    assert binding.sample_interval_s == 2
    assert binding.publish_interval_s == 5
    assert binding.stale_after_s == 20
    assert binding.historian_policy == "high_resolution"
    assert binding.quality_policy == "reject_invalid"
    # The MQTT projection is derived, because it follows from identity.
    assert binding.mqtt_topic == topics.telemetry_topic(INVERTER, "power_ac_output_kw")
    assert binding.mqtt_topic == "chaos/energy/power_container/inverter_01/power_ac_output_kw"
    # Not control capable -> no command topic.
    assert binding.command_topic is None


def test_control_capable_bindings_get_a_command_topic(db_session, loaded_registry):
    binding = service.get_binding(db_session, "energy.load.site.server_rack_01/power_budget_kw")
    assert binding is not None
    point = service.get_point(db_session, binding.point_id)
    assert point.control_capable is True
    assert binding.command_topic == topics.command_topic("energy.load.site.server_rack_01", "power_budget_kw")
    assert "/cmd/" in binding.command_topic


def test_every_binding_matches_a_materialised_point(db_session, loaded_registry):
    bindings = db_session.scalars(sa.select(PointBinding)).all()
    assert len(bindings) == EXPECTED["bindings"]
    for binding in bindings:
        point = service.get_point(db_session, binding.point_id)
        assert point is not None
        assert binding.point_id == f"{binding.asset_id}/{binding.point_name}"
        assert point.source == "binding"


def test_sample_index_points_at_the_historian(db_session, settings, loaded_registry):
    bound = db_session.get(PointSampleIndex, f"{INVERTER}/power_ac_output_kw")
    assert bound.series_key == f"{INVERTER}/power_ac_output_kw"
    assert bound.historian_backend == settings.historian_backend
    assert bound.retention_policy == "high_resolution"  # from the binding

    unbound = db_session.get(PointSampleIndex, f"{INVERTER}/runtime_total_h")
    assert unbound.retention_policy == points_module.DEFAULT_RETENTION_POLICY == "standard"


# ---------------------------------------------------------------------------
# Locations and external identifiers
# ---------------------------------------------------------------------------


def test_locations_are_projected_without_inventing_coordinates(db_session, loaded_registry):
    locations = db_session.scalars(sa.select(Location)).all()
    assert locations, "the register places some assets"
    # The register records coordinates_status: TBD everywhere (SDD 46).
    assert all(loc.latitude is None and loc.longitude is None for loc in locations)
    # rack_units is TBD throughout, so no rack positions are guessed.
    assert all(loc.rack_unit_start is None for loc in locations)

    inverter = db_session.get(Location, INVERTER)
    assert inverter.structure_id == "structure.structure.power_container.01"
    assert inverter.room_id == "structure.room.power_container.power_zone"
    assert inverter.rack_id is None

    ups = db_session.get(Location, "energy.ups.rack_01.01")
    assert ups.rack_id == "it.rack.power_container.01"

    # A location whose only concrete value is TBD does not fabricate an ID.
    secondary = db_session.get(Location, "it.server.secondary_control_node.01")
    assert secondary.structure_id == "TBD_physically_separate_from_power_container"

    # Assets with no location dict get no row rather than an empty one.
    assert db_session.get(Location, SITE) is None


def test_external_identifiers_skip_tbd_placeholders(tmp_path, session_factory):
    data_dir = write_package(
        tmp_path,
        assets=[
            make_asset(
                "energy.inverter.test.01",
                "inverter",
                "energy",
                external_identifiers=[
                    {"serial_number": "TBD", "mac_address": "00:11:22:33:44:55"},
                    {"id_type": "modbus_unit_id", "value": "TBD"},
                    {"id_type": "ha_entity_id", "value": "sensor.inverter_01"},
                ],
            )
        ],
    )
    session = session_factory()
    load_package(session, data_dir)
    session.commit()

    stored = {(row.id_type, row.value) for row in session.scalars(sa.select(ExternalIdentifier)).all()}
    assert stored == {
        ("mac_address", "00:11:22:33:44:55"),
        ("ha_entity_id", "sensor.inverter_01"),
    }
    session.close()


# ---------------------------------------------------------------------------
# Ordering and structural validation
# ---------------------------------------------------------------------------


def test_topological_order_places_parents_before_children():
    records = [
        {"asset_id": "a.b.c.03", "parent_id": "a.b.c.02"},
        {"asset_id": "a.b.c.02", "parent_id": "a.b.c.01"},
        {"asset_id": "a.b.c.01", "parent_id": None},
        {"asset_id": "a.b.c.04", "parent_id": "a.b.c.01"},
    ]
    levels = topological_order(records)
    assert levels == [["a.b.c.01"], ["a.b.c.02", "a.b.c.04"], ["a.b.c.03"]]

    position = {aid: index for index, level in enumerate(levels) for aid in level}
    for record in records:
        if record["parent_id"]:
            assert position[record["parent_id"]] < position[record["asset_id"]]


def test_topological_order_of_the_real_register_is_consistent():
    package = read_package()
    levels = topological_order(package.assets)
    position = {aid: index for index, level in enumerate(levels) for aid in level}
    assert sum(len(level) for level in levels) == EXPECTED["assets"]
    for record in package.assets:
        if record["parent_id"] is not None:
            assert position[record["parent_id"]] < position[record["asset_id"]]


def test_topological_order_detects_cycles():
    records = [
        {"asset_id": "a.b.c.01", "parent_id": "a.b.c.02"},
        {"asset_id": "a.b.c.02", "parent_id": "a.b.c.01"},
    ]
    with pytest.raises(RegistryLoadError) as excinfo:
        topological_order(records)
    assert "cycle" in str(excinfo.value).lower()
    assert excinfo.value.warnings


def test_children_are_inserted_after_parents_under_real_fk_enforcement(tmp_path, fk_session_factory):
    """Deliberately list the deepest asset first; the loader must reorder."""
    data_dir = write_package(
        tmp_path,
        assets=[
            make_asset("energy.inverter.test.01", "inverter", "energy", "structure.room.test.01"),
            make_asset("structure.room.test.01", "room", "structure", "site.site.test.01"),
            make_asset("site.site.test.01", "site", "site", None),
        ],
    )
    session = fk_session_factory()
    result = load_package(session, data_dir)
    session.commit()

    assert result.assets == 3
    assert result.warnings == []
    inverter = session.get(Asset, "energy.inverter.test.01")
    assert inverter.parent_id == "structure.room.test.01"
    session.close()


def test_unknown_asset_class_is_structural(tmp_path, session_factory):
    data_dir = write_package(
        tmp_path,
        assets=[make_asset("energy.turbine.test.01", "turbine", "energy")],
    )
    session = session_factory()
    with pytest.raises(RegistryLoadError) as excinfo:
        load_package(session, data_dir)
    assert any("unknown asset class" in warning for warning in excinfo.value.warnings)
    assert excinfo.value.result.skipped_assets == ["energy.turbine.test.01"]
    session.rollback()
    session.close()


def test_domain_outside_the_classes_allowed_domains_is_structural(tmp_path, session_factory):
    data_dir = write_package(
        tmp_path,
        assets=[make_asset("site.inverter.test.01", "inverter", "site")],
    )
    session = session_factory()
    with pytest.raises(RegistryLoadError) as excinfo:
        load_package(session, data_dir)
    assert any("is not allowed for" in warning for warning in excinfo.value.warnings)
    session.rollback()
    session.close()


def test_unknown_point_profile_is_structural(tmp_path, session_factory):
    data_dir = write_package(
        tmp_path,
        assets=[
            make_asset(
                "energy.inverter.test.01",
                "inverter",
                "energy",
                point_profile_refs=["not_a_profile"],
            )
        ],
    )
    session = session_factory()
    with pytest.raises(RegistryLoadError) as excinfo:
        load_package(session, data_dir)
    assert any("unknown point profile" in warning for warning in excinfo.value.warnings)
    session.rollback()
    session.close()


def test_dangling_parent_is_structural_and_skips_the_subtree(tmp_path, session_factory):
    data_dir = write_package(
        tmp_path,
        assets=[
            make_asset("structure.room.test.01", "room", "structure", "site.site.missing.01"),
            make_asset("energy.inverter.test.01", "inverter", "energy", "structure.room.test.01"),
        ],
    )
    session = session_factory()
    with pytest.raises(RegistryLoadError) as excinfo:
        load_package(session, data_dir)
    warnings = excinfo.value.warnings
    assert any("is not in the register" in warning for warning in warnings)
    # The orphaned child cannot be inserted either.
    assert excinfo.value.result.skipped_assets == [
        "energy.inverter.test.01",
        "structure.room.test.01",
    ]
    session.rollback()
    session.close()


def test_dangling_relationship_endpoint_is_structural(tmp_path, session_factory):
    data_dir = write_package(
        tmp_path,
        assets=[make_asset("energy.inverter.test.01", "inverter", "energy")],
        relationships=[
            {
                "relationship_id": "rel.0001",
                "from_asset_id": "energy.inverter.test.01",
                "relationship_type": "feeds",
                "to_asset_id": "energy.load.missing.01",
                "status": "planned",
                "notes": [],
            }
        ],
    )
    session = session_factory()
    with pytest.raises(RegistryLoadError) as excinfo:
        load_package(session, data_dir)
    assert any("target asset" in warning for warning in excinfo.value.warnings)
    session.rollback()
    session.close()


def test_an_unprojectable_asset_id_is_reported_not_fatal(tmp_path, session_factory):
    """The JSON Schema allows IDs deeper than section 25.2's four components.

    Such an ID has no deterministic MQTT projection. That is worth reporting,
    but it must not abort the load of every other asset.
    """
    deep = "energy.inverter.power_container.rack_01.01"
    data_dir = write_package(
        tmp_path,
        assets=[
            make_asset(deep, "inverter", "energy"),
            make_asset("energy.inverter.test.01", "inverter", "energy"),
        ],
        bindings=[
            {
                "point_id": f"{deep}/power_ac_output_kw",
                "asset_id": deep,
                "point_name": "power_ac_output_kw",
                "binding_status": "tbd",
                "source_protocol": "modbus_tcp_TBD",
                "source_address": "TBD",
                "sample_interval_s": 5,
                "publish_interval_s": 5,
                "stale_after_s": 30,
                "historian_policy": "standard",
                "quality_policy": "reject_invalid",
                "automatic_control_allowed": False,
                "notes": [],
            }
        ],
    )
    session = session_factory()
    with pytest.raises(RegistryLoadError) as excinfo:
        load_package(session, data_dir)
    assert any("cannot derive an MQTT topic" in w for w in excinfo.value.warnings)
    # The other asset still made it through.
    assert excinfo.value.result.assets == 2
    session.rollback()
    session.close()


def test_missing_package_file_raises(tmp_path, session_factory):
    session = session_factory()
    with pytest.raises(RegistryLoadError):
        load_package(session, tmp_path / "nowhere")
    session.close()


# ---------------------------------------------------------------------------
# Query service
# ---------------------------------------------------------------------------


def test_list_assets_filters_and_paginates(db_session, loaded_registry):
    everything = service.list_assets(db_session, limit=500)
    assert everything.total == EXPECTED["assets"]
    assert len(everything.items) == EXPECTED["assets"]

    energy = service.list_assets(db_session, domain="energy", limit=500)
    assert energy.total == 36
    assert {asset.domain for asset in energy} == {"energy"}

    inverters = service.list_assets(db_session, asset_class="inverter", limit=500)
    assert inverters.total == 4

    page = service.list_assets(db_session, limit=10, offset=10)
    assert page.total == EXPECTED["assets"]
    assert len(page.items) == 10
    assert page.items[0].asset_id == everything.items[10].asset_id

    search = service.list_assets(db_session, q="inverter", limit=500)
    assert search.total >= 4
    assert all("inverter" in asset.asset_id.lower() or "inverter" in asset.name.lower() for asset in search)

    assert service.list_assets(db_session, criticality="life_safety", limit=500).total >= 0
    assert service.list_assets(db_session, tag="nonexistent", limit=500).total == 0


def test_asset_tree_nests_children(db_session, loaded_registry):
    tree = service.get_asset_tree(db_session)
    assert tree["asset_id"] == SITE
    assert tree["child_count"] > 0
    assert len(tree["children"]) == tree["child_count"]

    def walk(node):
        yield node
        for child in node["children"]:
            yield from walk(child)

    assert len({node["asset_id"] for node in walk(tree)}) == EXPECTED["assets"]

    shallow = service.get_asset_tree(db_session, depth=1)
    assert all(child["children"] == [] for child in shallow["children"])
    assert shallow["children"][0]["child_count"] >= 0

    subtree = service.get_asset_tree(db_session, root_id="it.rack.power_container.01")
    assert subtree["asset_id"] == "it.rack.power_container.01"
    assert subtree["children"]

    with pytest.raises(KeyError):
        service.get_asset_tree(db_session, root_id="nope")


def test_relationships_resolve_the_counterpart(db_session, loaded_registry):
    views = service.get_asset_relationships(db_session, INVERTER)
    assert views
    for view in views:
        assert view.counterpart_id != INVERTER
        assert view.counterpart_name, view.counterpart_id
        assert view.direction in ("incoming", "outgoing")
        assert INVERTER in (view.from_asset_id, view.to_asset_id)

    outgoing = service.get_asset_relationships(db_session, INVERTER, direction="outgoing")
    assert all(view.from_asset_id == INVERTER for view in outgoing)
    incoming = service.get_asset_relationships(db_session, INVERTER, direction="incoming")
    assert all(view.to_asset_id == INVERTER for view in incoming)
    assert len(outgoing) + len(incoming) == len(views)

    with pytest.raises(ValueError):
        service.get_asset_relationships(db_session, INVERTER, direction="sideways")


def test_dependencies_follow_feeds_depends_on_and_part_of(db_session, loaded_registry):
    direct = service.get_dependencies(db_session, INVERTER)
    assert direct
    direct_ids = {asset.asset_id for asset in direct}
    # The battery bank feeds the inverter, so it is upstream of it.
    assert BATTERY in direct_ids

    transitive = service.get_dependencies(db_session, INVERTER, transitive=True)
    transitive_ids = {asset.asset_id for asset in transitive}
    assert direct_ids <= transitive_ids
    assert len(transitive_ids) >= len(direct_ids)
    assert INVERTER not in transitive_ids  # cycle guard keeps the root out

    dependents = service.get_dependents(db_session, BATTERY)
    assert INVERTER in {asset.asset_id for asset in dependents}


def test_resolve_topic_maps_mqtt_back_to_the_binding(db_session, loaded_registry):
    topic = topics.telemetry_topic(INVERTER, "power_ac_output_kw")
    binding = service.resolve_topic(db_session, topic)
    assert binding is not None
    assert binding.point_id == f"{INVERTER}/power_ac_output_kw"
    assert binding.asset_id == INVERTER

    assert service.resolve_topic(db_session, "chaos/energy/nowhere/thing_01/x") is None

    command = service.resolve_command_topic(
        db_session, topics.command_topic("energy.load.site.server_rack_01", "power_budget_kw")
    )
    assert command is not None
    assert command.point_name == "power_budget_kw"


def test_every_binding_topic_is_unique_and_resolvable(db_session, loaded_registry):
    bindings = db_session.scalars(sa.select(PointBinding)).all()
    seen = {binding.mqtt_topic for binding in bindings}
    assert len(seen) == len(bindings), "topics must round-trip to exactly one point"
    for binding in bindings[:20]:
        assert service.resolve_topic(db_session, binding.mqtt_topic).point_id == binding.point_id


def test_registry_summary_counts(db_session, loaded_registry):
    summary = service.registry_summary(db_session)
    assert summary["assets"] == EXPECTED["assets"]
    assert summary["points"] == EXPECTED["points"]
    assert summary["relationships"] == EXPECTED["relationships"]
    assert summary["bindings"] == EXPECTED["bindings"]

    assert sum(summary["assets_by_domain"].values()) == EXPECTED["assets"]
    assert sum(summary["assets_by_status"].values()) == EXPECTED["assets"]
    assert sum(summary["assets_by_criticality"].values()) == EXPECTED["assets"]
    assert sum(summary["assets_by_class"].values()) == EXPECTED["assets"]
    assert sum(summary["bindings_by_status"].values()) == EXPECTED["bindings"]
    assert summary["assets_by_domain"]["energy"] == 36
    assert summary["bindings_by_status"]["tbd"] == 240

    # Every asset in the v0.3 register still has unresolved fields.
    assert summary["assets_with_open_fields"] == EXPECTED["assets"]
    assert summary["open_field_count"] > EXPECTED["assets"]

    assert summary["points_control_capable"] > 0
    assert summary["points_automatic_control_allowed"] < summary["points"]


def test_get_asset_and_points_helpers(db_session, loaded_registry):
    asset = service.get_asset(db_session, INVERTER)
    assert asset.name == "Hybrid Inverter 1"
    assert asset.criticality == "critical"
    assert asset.open_fields
    assert service.get_asset(db_session, "no.such.asset.01") is None

    assert service.count_asset_points(db_session, INVERTER) == 19
    assert service.count_children(db_session, SITE) > 0

    items, total = service.list_points(db_session, asset_id=INVERTER)
    assert total == 19 and len(items) == 19

    capable, total_capable = service.list_points(db_session, control_capable=True, limit=1000)
    assert total_capable == len(capable) > 0
    assert all(point.control_capable for point in capable)

    named, total_named = service.list_points(db_session, point_name="availability_state", limit=1000)
    assert total_named == len(named) > 1
    assert {point.point_name for point in named} == {"availability_state"}


def test_materialize_points_standalone_reuses_stored_bindings(db_session, loaded_registry):
    """Re-running materialisation for one asset changes nothing."""
    asset = db_session.get(Asset, INVERTER)
    before = {
        p.point_id: (p.source, p.automatic_control_allowed)
        for p in service.get_asset_points(db_session, INVERTER)
    }

    again = points_module.materialize_points(db_session, asset)
    db_session.commit()

    assert len(again) == len(before)
    after = {
        p.point_id: (p.source, p.automatic_control_allowed)
        for p in service.get_asset_points(db_session, INVERTER)
    }
    assert after == before
    assert db_session.scalar(sa.select(sa.func.count()).select_from(Point)) == EXPECTED["points"]
