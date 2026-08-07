"""Tests for the FR-001 overview aggregate.

The platform is built before the homestead exists, so the two states that matter
most are "empty database" and "registry loaded but nothing installed". Both must
produce a complete, honest payload -- never an error, never a fabricated zero.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from homestead_twin.models import (
    Alarm,
    AlarmDefinition,
    Asset,
    AssetClass,
    Command,
    CurrentState,
    EnergyStateSnapshot,
    EnergyStateTransition,
    Incident,
    Location,
    OperatingMode,
    Point,
    PointBinding,
    PointDefinition,
    PowerBudgetLease,
    PowerLoadProfile,
    utcnow,
)

OVERVIEW = "/api/v1/overview"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _asset(session: Session, asset_id: str, **kwargs) -> Asset:
    defaults = {
        "domain": "energy",
        "asset_class": "load",
        "name": asset_id,
        "status": "planned",
        "criticality": "discretionary",
        "control_authority": "none",
        "open_fields": [],
        "manual_override": {},
        "dependencies": [],
    }
    defaults.update(kwargs)
    klass = session.get(AssetClass, defaults["asset_class"])
    if klass is None:
        session.add(AssetClass(name=defaults["asset_class"], allowed_domains=[], required_properties=[], default_points=[]))
        session.flush()
    asset = Asset(asset_id=asset_id, **defaults)
    session.add(asset)
    return asset


def _point(
    session: Session,
    asset_id: str,
    point_name: str,
    *,
    data_type: str = "float",
    unit: str | None = "kW",
    point_class: str = "AI",
    control_capable: bool = False,
) -> Point:
    if session.get(PointDefinition, point_name) is None:
        session.add(
            PointDefinition(
                name=point_name,
                default_class=point_class,
                allowed_classes=[point_class],
                data_type=data_type,
                unit=unit,
                control_capable=control_capable,
                applicable_asset_classes=[],
            )
        )
        session.flush()
    point = Point(
        point_id=Point.make_id(asset_id, point_name),
        asset_id=asset_id,
        point_name=point_name,
        point_class=point_class,
        data_type=data_type,
        unit=unit,
        control_capable=control_capable,
        limits={},
    )
    session.add(point)
    return point


def _state(
    session: Session,
    point: Point,
    *,
    numeric: float | None = None,
    text: str | None = None,
    boolean: bool | None = None,
    quality: str = "good",
    age_s: int = 1,
    requested=None,
) -> CurrentState:
    now = utcnow()
    state = CurrentState(
        point_id=point.point_id,
        asset_id=point.asset_id,
        point_name=point.point_name,
        value_numeric=numeric,
        value_text=text,
        value_bool=boolean,
        unit=point.unit,
        quality=quality,
        source="test",
        ts=now - dt.timedelta(seconds=age_s),
        received_at=now - dt.timedelta(seconds=age_s),
        stale_after_s=60,
        requested_value=requested,
        requested_at=now if requested is not None else None,
    )
    session.add(state)
    return state


@pytest.fixture()
def seeded(db_session: Session) -> Session:
    """A small but realistic slice of the homestead: energy live, water absent."""
    session = db_session

    _asset(session, "site.site.primary.01", domain="site", asset_class="site", name="Primary Site",
           criticality="critical", control_authority="supervisory",
           open_fields=["property_coordinates", "survey_boundary"])
    _asset(session, "energy.pv_array.field.01", asset_class="pv_array", name="PV Array 01",
           status="commissioned", criticality="important")
    _asset(session, "energy.battery_bank.container.01", asset_class="battery_bank", name="Battery Bank",
           status="commissioned", criticality="critical")
    _asset(session, "energy.panel.container.critical", asset_class="panel", name="Critical Panel",
           status="commissioned", criticality="critical")
    _asset(
        session,
        "energy.load.workshop.dust_collector",
        asset_class="load",
        name="Workshop Dust Collector",
        status="installed",
        criticality="discretionary",
        control_authority="supervisory",
        manual_override={"method": "local disconnect", "active": False, "documented": True},
        open_fields=["branch_circuit"],
    )
    _asset(session, "it.router.core.01", domain="it", asset_class="router", name="Core Router",
           status="active", criticality="critical")
    _asset(session, "security.camera.gate.01", domain="security", asset_class="camera", name="Gate Camera",
           status="installed")
    session.flush()

    pv = _point(session, "energy.pv_array.field.01", "power_dc_kw")
    soc = _point(session, "energy.battery_bank.container.01", "soc_pct", unit="%")
    panel = _point(session, "energy.panel.container.critical", "power_ac_kw")
    load_kw = _point(session, "energy.load.workshop.dust_collector", "power_kw")
    enabled_actual = _point(session, "energy.load.workshop.dust_collector", "enabled_actual",
                            data_type="bool", unit=None, point_class="DI")
    enabled_req = _point(session, "energy.load.workshop.dust_collector", "enabled_requested",
                         data_type="bool", unit=None, point_class="DO", control_capable=True)
    override = _point(session, "energy.load.workshop.dust_collector", "manual_override_active",
                      data_type="bool", unit=None, point_class="DI")
    router_avail = _point(session, "it.router.core.01", "availability_state", data_type="enum",
                          unit=None, point_class="DI")
    # A registered but never-reporting point: this is "no_data", not zero.
    _point(session, "security.camera.gate.01", "availability_state", data_type="enum", unit=None,
           point_class="DI")
    session.flush()

    _state(session, pv, numeric=7.4)
    _state(session, soc, numeric=61.5)
    _state(session, panel, numeric=3.2)
    _state(session, load_kw, numeric=1.1)
    _state(session, enabled_actual, boolean=False)
    _state(session, enabled_req, boolean=True, requested={"value": True})
    _state(session, override, boolean=False)
    _state(session, router_avail, text="online")

    session.add(
        PointBinding(
            point_id=enabled_req.point_id,
            asset_id=enabled_req.asset_id,
            point_name=enabled_req.point_name,
            binding_status="commissioned",
            source_protocol="modbus_tcp",
            automatic_control_allowed=True,
        )
    )

    session.add(
        EnergyStateSnapshot(
            id=1,
            state="CONSERVE",
            entered_at=utcnow() - dt.timedelta(minutes=30),
            data_quality="good",
            inputs={"soc_pct": 61.5},
            derived={"autonomy_current_h": 9.5, "autonomy_critical_h": 26.0,
                     "energy_above_emergency_reserve_kwh": 180.0},
            shed_groups_active=["SG-3"],
            generator_request="not_requested",
            last_evaluated_at=utcnow(),
        )
    )
    session.add(
        EnergyStateTransition(
            from_state="NORMAL",
            to_state="CONSERVE",
            trigger="reserve_declining",
            reason="Forecast margin negative",
            occurred_at=utcnow() - dt.timedelta(minutes=30),
        )
    )
    session.add(
        PowerLoadProfile(
            asset_id="energy.load.workshop.dust_collector",
            name="Workshop Dust Collector",
            base_tier=3,
            effective_tier=3,
            criticality="discretionary",
            rated_power_kw=2.2,
            estimated_power_kw=1.8,
            control_method="contactor",
            shed_group="SG-3",
            shed_order=2,
            restoration_group="RG-3",
            data_status="estimated",
            open_fields=["measured_power"],
        )
    )
    session.add(
        PowerBudgetLease(
            lease_id="lease-0001",
            asset_id="energy.load.workshop.dust_collector",
            granted_kw=2.0,
            starts_at=utcnow() - dt.timedelta(minutes=5),
            expires_at=utcnow() + dt.timedelta(minutes=55),
            priority=3,
            reason="Workshop session during high-solar window",
            requested_by="ethan",
            state="active",
        )
    )
    session.add(
        OperatingMode(
            scope_type="site",
            scope_id="site.site.primary.01",
            mode="automatic",
            changed_by="ethan",
            reason="Commissioning complete for energy",
            changed_at=utcnow() - dt.timedelta(days=1),
        )
    )
    session.add(
        Command(
            command_id="cmd-0001",
            asset_id="energy.load.workshop.dust_collector",
            point_id=enabled_req.point_id,
            command="set_enabled",
            value={"value": True},
            issued_by="ethan",
            issued_by_kind="human",
            reason="Workshop run",
            operating_mode="automatic",
            issued_at=utcnow() - dt.timedelta(minutes=4),
            state="rejected",
            state_reason="Interlock blocked",
            interlocks_evaluated=[
                {"name": "energy_state_permits_tier3", "passed": False,
                 "detail": "Energy state CONSERVE does not permit tier 3 loads"},
                {"name": "minimum_off_time", "passed": True, "detail": "Elapsed 900 s"},
            ],
        )
    )

    session.add(
        AlarmDefinition(alarm_key="ems.reserve.low", name="Battery reserve low", severity="major",
                        domain="energy")
    )
    session.add(
        AlarmDefinition(alarm_key="it.wan.down", name="WAN down", severity="critical", domain="it")
    )
    session.flush()
    incident = Incident(
        id="inc-0001",
        title="Power container under-generation",
        severity="major",
        opened_at=utcnow() - dt.timedelta(hours=2),
        state="open",
    )
    session.add(incident)
    session.flush()
    session.add(
        Alarm(
            id="alm-0001",
            alarm_key="ems.reserve.low",
            asset_id="energy.battery_bank.container.01",
            severity="major",
            state="active",
            detected_at=utcnow() - dt.timedelta(hours=2),
            message="Reserve below conserve threshold",
            incident_id="inc-0001",
        )
    )
    session.add(
        Alarm(
            id="alm-0002",
            alarm_key="it.wan.down",
            asset_id="it.router.core.01",
            severity="critical",
            state="acknowledged",
            detected_at=utcnow() - dt.timedelta(minutes=20),
            acknowledged_at=utcnow() - dt.timedelta(minutes=10),
            acknowledged_by="ethan",
            message="WAN offline",
        )
    )
    session.add(
        Alarm(
            id="alm-0003",
            alarm_key="it.wan.down",
            asset_id="it.router.core.01",
            severity="critical",
            state="cleared",
            detected_at=utcnow() - dt.timedelta(days=1),
            cleared_at=utcnow() - dt.timedelta(hours=20),
            message="Older, cleared",
        )
    )
    session.commit()
    return session


# ---------------------------------------------------------------------------
# Empty database
# ---------------------------------------------------------------------------


def test_overview_on_empty_database_is_200(client):
    response = client.get(OVERVIEW)
    assert response.status_code == 200


def test_overview_empty_reports_unknown_not_zero(client):
    body = client.get(OVERVIEW).json()

    assert body["site"]["operating_state"]["state"] == "unknown"
    assert body["site"]["operating_state"]["assets_total"] == 0
    assert body["site"]["operating_mode"]["available"] is False

    for role in ("pv_production_kw", "site_load_kw", "battery_soc_pct"):
        metric = body["energy"][role]
        assert metric["available"] is False
        assert metric["value"] is None, f"{role} must not fabricate a value"
        assert metric["status"] in ("not_deployed", "no_points", "no_data", "design_only")

    assert body["energy"]["state"] is None
    assert body["energy"]["status"] == "no_data"
    assert body["alarms"]["active_total"] == 0
    assert body["alarms"]["critical_active"] == 0


def test_overview_empty_water_is_unavailable_marker(client):
    water = client.get(OVERVIEW).json()["water"]
    assert water["status"] == "not_deployed"
    assert water["available"] is False
    assert water["reserve_pct"]["value"] is None
    assert "not zero" in water["note"]


def test_overview_empty_lists_every_sdd_domain(client):
    subsystems = client.get(OVERVIEW).json()["subsystems"]
    domains = {entry["domain"] for entry in subsystems}
    for expected in ("site", "energy", "water", "agriculture", "it", "security", "safety", "spa"):
        assert expected in domains
    assert all(entry["health"] == "not_deployed" for entry in subsystems)


def test_subsystems_endpoint_on_empty_database(client):
    response = client.get("/api/v1/overview/subsystems")
    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["assets"] == 0
    assert body["totals"]["unresolved_open_fields"] == 0
    assert body["totals"]["domains_not_deployed"] == len(body["domains"])


def test_map_endpoint_on_empty_database(client):
    response = client.get("/api/v1/overview/map")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert body["features"] == []
    assert body["pending_placement"] == []
    assert body["metadata"]["survey_status"] == "not_surveyed"
    assert "bbox" not in body


def test_control_endpoint_unknown_asset_is_404(client):
    response = client.get("/api/v1/overview/control/energy.load.nonexistent.01")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Seeded registry
# ---------------------------------------------------------------------------


def test_overview_seeded_energy_metrics_carry_provenance(client, seeded):
    body = client.get(OVERVIEW).json()
    pv = body["energy"]["pv_production_kw"]
    assert pv["available"] is True
    assert pv["value"] == pytest.approx(7.4)
    assert pv["source"] == "PV array DC power"
    assert pv["contributors"][0]["point_id"] == "energy.pv_array.field.01/power_dc_kw"
    assert pv["contributors"][0]["quality"] == "good"

    load = body["energy"]["site_load_kw"]
    assert load["available"] is True
    # Falls back from the (absent) site meter to panel AC power.
    assert load["source"] == "Distribution panel AC power (summed)"
    assert load["value"] == pytest.approx(3.2)

    soc = body["energy"]["battery_soc_pct"]
    assert soc["value"] == pytest.approx(61.5)
    assert soc["unit"] == "%"

    net = body["energy"]["net_power_kw"]
    assert net["available"] is True
    assert net["value"] == pytest.approx(4.2)
    assert "Calculated" in net["note"]


def test_overview_seeded_reports_ems_state_and_autonomy(client, seeded):
    energy = client.get(OVERVIEW).json()["energy"]
    assert energy["state"] == "CONSERVE"
    assert energy["shed_groups_active"] == ["SG-3"]
    assert energy["autonomy_current_h"]["value"] == pytest.approx(9.5)
    assert energy["autonomy_critical_h"]["available"] is True
    assert energy["recent_transitions"][0]["to_state"] == "CONSERVE"


def test_overview_seeded_alarm_counts_exclude_cleared(client, seeded):
    alarms = client.get(OVERVIEW).json()["alarms"]
    assert alarms["active_total"] == 2
    assert alarms["critical_active"] == 1
    assert alarms["major_active"] == 1
    assert alarms["open_incidents"] == 1
    # Critical sorts above major.
    assert alarms["top"][0]["severity"] == "critical"
    assert alarms["top"][0]["asset_name"] == "Core Router"
    incident_titles = [a["incident_title"] for a in alarms["top"]]
    assert "Power container under-generation" in incident_titles


def test_overview_seeded_site_state_reflects_critical_alarm(client, seeded):
    site = client.get(OVERVIEW).json()["site"]
    assert site["operating_state"]["state"] == "alarm"
    assert site["operating_mode"]["mode"] == "automatic"
    assert site["operating_mode"]["changed_by"] == "ethan"


def test_overview_seeded_water_still_not_deployed(client, seeded):
    water = client.get(OVERVIEW).json()["water"]
    assert water["status"] == "not_deployed"
    assert water["reserve_pct"]["value"] is None


def test_overview_seeded_distinguishes_no_data_from_healthy(client, seeded):
    body = client.get(OVERVIEW).json()
    comms = body["communications"]
    assert comms["by_value"]["online"] == 1
    security = body["security"]
    # The camera has a registered point that has never reported.
    assert security["status"] == "no_data"
    assert security["available"] is False
    assert security["items"][0]["status"] == "no_data"


def test_overview_alarm_limit_query(client, seeded):
    body = client.get(OVERVIEW, params={"alarm_limit": 1}).json()
    assert len(body["alarms"]["top"]) == 1


def test_subsystems_seeded_exposes_open_fields(client, seeded):
    body = client.get("/api/v1/overview/subsystems").json()
    domains = {d["domain"]: d for d in body["domains"]}
    assert domains["water"]["health"] == "not_deployed"
    assert domains["energy"]["assets"]["total"] == 4
    assert domains["energy"]["alarms"]["by_severity"]["major"] == 1
    assert domains["it"]["alarms"]["by_severity"]["critical"] == 1
    assert body["totals"]["unresolved_open_fields"] == 3
    site_fields = domains["site"]["open_fields"]["items"]
    assert site_fields[0]["fields"] == ["property_coordinates", "survey_boundary"]


def test_map_pending_placement_lists_unsurveyed_assets(client, seeded):
    body = client.get("/api/v1/overview/map").json()
    assert body["features"] == []
    assert body["metadata"]["pending_count"] == 7
    ids = {item["asset_id"] for item in body["pending_placement"]}
    assert "energy.pv_array.field.01" in ids
    assert all(item["reason"] == "no_location_record" for item in body["pending_placement"])
    assert body["metadata"]["survey_status"] == "not_surveyed"


def test_map_emits_only_real_coordinates(client, seeded, db_session):
    db_session.add(
        Location(
            asset_id="energy.pv_array.field.01",
            latitude=44.5,
            longitude=-79.2,
            description="Surveyed 2026-08",
        )
    )
    # A location record that exists but has no coordinates must stay pending.
    db_session.add(Location(asset_id="it.router.core.01", description="Rack 01, awaiting survey"))
    db_session.commit()

    body = client.get("/api/v1/overview/map").json()
    assert len(body["features"]) == 1
    feature = body["features"][0]
    assert feature["geometry"] == {"type": "Point", "coordinates": [-79.2, 44.5]}
    assert feature["properties"]["asset_id"] == "energy.pv_array.field.01"
    assert body["bbox"] == [-79.2, 44.5, -79.2, 44.5]
    assert body["metadata"]["survey_status"] == "partial"

    pending = {item["asset_id"]: item for item in body["pending_placement"]}
    assert pending["it.router.core.01"]["reason"] == "location_record_without_coordinates"


def test_map_supports_non_point_geometry(client, seeded, db_session):
    db_session.add(
        Location(
            asset_id="site.site.primary.01",
            geometry={"type": "Polygon", "coordinates": [[[-79.3, 44.4], [-79.1, 44.4],
                                                          [-79.1, 44.6], [-79.3, 44.6],
                                                          [-79.3, 44.4]]]},
        )
    )
    db_session.commit()
    body = client.get("/api/v1/overview/map").json()
    geometries = [f["geometry"]["type"] for f in body["features"]]
    assert "Polygon" in geometries


# ---------------------------------------------------------------------------
# SDD 17.4 control presentation
# ---------------------------------------------------------------------------


CONTROL_URL = "/api/v1/overview/control/energy.load.workshop.dust_collector"


def test_control_payload_has_every_sdd_17_4_element(client, seeded):
    body = client.get(CONTROL_URL).json()
    for key in (
        "actual_state",
        "requested_state",
        "authority",
        "interlocks",
        "last_command",
        "manual_override",
        "budget_impact",
    ):
        assert key in body, f"SDD 17.4 requires {key}"


def test_control_actual_and_requested_differ(client, seeded):
    body = client.get(CONTROL_URL).json()
    assert body["actual_state"]["enabled_actual"]["value"] is False
    assert body["requested_state"]["enabled_requested"]["value"] is True
    pending = body["pending_requests"]
    assert pending and pending[0]["requested_value"] == {"value": True}
    assert pending[0]["matches_actual"] is False


def test_control_reports_blocking_interlocks(client, seeded):
    interlocks = client.get(CONTROL_URL).json()["interlocks"]
    assert interlocks["blocking_count"] == 1
    blocking = interlocks["blocking"][0]
    assert blocking["name"] == "energy_state_permits_tier3"
    assert "CONSERVE" in blocking["detail"]
    names = {i["name"] for i in interlocks["evaluated"]}
    assert "minimum_off_time" in names


def test_control_reports_last_command_and_issuer(client, seeded):
    last = client.get(CONTROL_URL).json()["last_command"]
    assert last["command_id"] == "cmd-0001"
    assert last["issued_by"] == "ethan"
    assert last["issued_by_kind"] == "human"
    assert last["reason"] == "Workshop run"
    assert last["state"] == "rejected"


def test_control_reports_authority_and_override(client, seeded):
    body = client.get(CONTROL_URL).json()
    assert body["authority"]["declared"] == "supervisory"
    assert body["authority"]["local_or_remote"] == "remote_supervisory"
    assert body["manual_override"]["active"] is False
    assert body["manual_override"]["record"]["method"] == "local disconnect"
    assert body["operating_mode"]["mode"] == "automatic"
    assert body["operating_mode"]["scope"] == "site"


def test_control_reports_budget_impact(client, seeded):
    budget = client.get(CONTROL_URL).json()["budget_impact"]
    assert budget["load_profile"]["base_tier"] == 3
    assert budget["load_profile"]["shed_group"] == "SG-3"
    assert budget["load_profile"]["rated_power_kw"] == pytest.approx(2.2)
    assert budget["measured_power_kw"]["value"] == pytest.approx(1.1)
    assert budget["energy_state"] == "CONSERVE"
    assert budget["currently_shed"] is True
    assert budget["active_leases"][0]["granted_kw"] == pytest.approx(2.0)


def test_control_blocks_commands_when_physical_control_disabled(client, seeded):
    commandable = client.get(CONTROL_URL).json()["commandable"]
    assert commandable["allowed"] is False
    assert commandable["physical_control_enabled"] is False
    assert any("Physical control is globally disabled" in r for r in commandable["reasons"])
    assert commandable["blocking_interlocks"] == 1


def test_control_on_asset_with_no_control_data_is_honest(client, seeded):
    body = client.get("/api/v1/overview/control/security.camera.gate.01").json()
    assert body["last_command"] is None
    assert body["command_history_status"] == "no_data"
    assert body["interlocks"]["status"] == "no_data"
    assert body["manual_override"]["active"] is None
    assert body["manual_override"]["status"] == "no_data"
    assert body["budget_impact"]["load_profile"] is None
    assert body["budget_impact"]["load_profile_status"] == "no_data"
    assert body["commandable"]["allowed"] is False


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_stale_telemetry_is_not_treated_as_live(client, seeded, db_session):
    state = db_session.get(CurrentState, "energy.pv_array.field.01/power_dc_kw")
    state.ts = utcnow() - dt.timedelta(hours=6)
    state.received_at = state.ts
    db_session.commit()

    pv = client.get(OVERVIEW).json()["energy"]["pv_production_kw"]
    # A point that reported and went quiet is not the same as one that never
    # reported, and neither is a reading of zero.
    assert pv["value"] is None
    assert pv["available"] is False
    assert pv["status"] == "stale"
    assert "not reading zero" in pv["note"]


def test_interlocks_accept_the_command_services_record_shape(client, seeded, db_session):
    """The command router records {code, allowed, blocks_dispatch}; older
    producers record {name, passed}. Both must survive the round trip."""
    command = db_session.get(Command, "cmd-0001")
    command.interlocks_evaluated = [
        {
            "code": "physical_control_disabled",
            "allowed": True,
            "reason": "Dry run: interlocks evaluated, nothing will be published",
            "detail": {"allow_physical_control": False, "dry_run": True},
            "blocks_dispatch": True,
            "override_by": None,
        },
        {
            "code": "asset_not_operational",
            "allowed": False,
            "reason": "Asset status is 'planned'",
            "blocks_dispatch": True,
        },
    ]
    db_session.commit()

    interlocks = client.get(CONTROL_URL).json()["interlocks"]
    by_name = {entry["name"]: entry for entry in interlocks["evaluated"]}
    assert by_name["asset_not_operational"]["passed"] is False
    assert by_name["asset_not_operational"]["blocking"] is True
    # Passing but still dispatch-blocking must not read as "all clear".
    assert by_name["physical_control_disabled"]["passed"] is True
    assert by_name["physical_control_disabled"]["blocking"] is False
    assert by_name["physical_control_disabled"]["blocks_dispatch"] is True
    assert by_name["physical_control_disabled"]["context"] == {
        "allow_physical_control": False,
        "dry_run": True,
    }
    assert interlocks["blocking_count"] == 1
    assert interlocks["dispatch_blocker_count"] == 2


# ---------------------------------------------------------------------------
# Real design package (skipped until the registry loader lands)
# ---------------------------------------------------------------------------


def test_overview_against_real_v03_registry(client, db_session):
    """The shipped v0.3 package: 90 planned assets, no coordinates, no telemetry."""
    pytest.importorskip(
        "homestead_twin.registry.loader",
        reason="registry loader is built by another agent; skipped until present",
    )
    from homestead_twin.registry.loader import load_package

    load_package(db_session)
    db_session.commit()

    body = client.get(OVERVIEW).json()
    assert body["site"]["operating_state"]["state"] == "pre_deployment"
    assert body["site"]["operating_state"]["assets_deployed"] == 0
    assert body["water"]["status"] == "not_deployed"
    assert body["energy"]["pv_production_kw"]["value"] is None

    map_body = client.get("/api/v1/overview/map").json()
    assert map_body["features"] == []
    assert map_body["metadata"]["pending_count"] > 0


# ---------------------------------------------------------------------------
# Uninitialised schema
# ---------------------------------------------------------------------------


def test_overview_survives_an_uninitialised_schema(tmp_path, settings):
    """A node that comes up before its schema exists must still paint a screen.

    The overview is what an operator opens when something is wrong, and
    "the database is not initialised" is exactly that. It must be reported,
    not raised.
    """
    from fastapi.testclient import TestClient

    from homestead_twin.api.app import create_app
    from homestead_twin.config import Settings

    empty = Settings(
        database_url=f"sqlite:///{tmp_path / 'no-schema.db'}",
        mqtt_enabled=False,
        ems_enabled=False,
        alarm_engine_enabled=False,
    )
    app = create_app(empty, start_services=False, init_db=False)
    with TestClient(app) as client:
        response = client.get(OVERVIEW)
        assert response.status_code == 200
        body = response.json()
        assert body["data_sources"]["degraded"] is True
        assert "assets" in {p["source"] for p in body["data_sources"]["unavailable"]}
        assert body["site"]["operating_state"]["state"] == "unknown"
        # Still no fabricated numbers.
        assert body["energy"]["pv_production_kw"]["value"] is None

        assert client.get("/api/v1/overview/subsystems").status_code == 200
        assert client.get("/api/v1/overview/map").status_code == 200
        assert client.get("/api/v1/overview/control/anything").status_code == 404


# ---------------------------------------------------------------------------
# UI static assets
# ---------------------------------------------------------------------------


def test_operator_ui_is_served(client):
    root = client.get("/")
    assert root.status_code == 200
    assert "<title>" in root.text
    for asset in ("/ui/app.js", "/ui/api.js", "/ui/styles.css", "/ui/views/home.js"):
        response = client.get(asset)
        assert response.status_code == 200, asset


#: The SVG namespace is an identifier, not a URL: nothing ever fetches it, but
#: it is required by ``createElementNS`` and by SVG data URIs.
_SAFE_NAMESPACE_STRINGS = (
    "http://www.w3.org/2000/svg",
    "http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg",
)


def test_ui_has_no_external_network_references():
    """SDD 5.1: the operator UI must work with no internet at all."""
    import pathlib

    web_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "homestead_twin" / "web"
    files = [p for p in web_dir.rglob("*") if p.suffix in (".html", ".js", ".css")]
    assert files, "no web assets found"
    for path in files:
        text = path.read_text(encoding="utf-8")
        for safe in _SAFE_NAMESPACE_STRINGS:
            text = text.replace(safe, "")
        for needle in ("http://", "https://", "//cdn.", "fonts.googleapis", "integrity="):
            assert needle not in text, f"{path.name} references an external resource: {needle}"
