"""Energy API (SDD 41) and the section 38 dashboard aggregate.

Writes require a named operator and an audit reason. The two endpoints that
change control behaviour carry their safety constraints into the HTTP layer:
a freeze is bounded and never suppresses EMERGENCY, and a latch is only cleared
with a reason plus an explicit condition-clear confirmation.
"""

from __future__ import annotations

import datetime as dt

import pytest
from test_ems_shedding import ALL_LOADS, COMPUTE, CONTROL_CORE, SPA, WORKSHOP
from test_ems_state_machine import HEALTHY_INPUTS, seed_current_state

from homestead_twin.ems.loader import load_schedule
from homestead_twin.ems.state_machine import ensure_snapshot
from homestead_twin.models.base import utcnow
from homestead_twin.models.energy import PowerBudgetLease
from homestead_twin.models.registry import Asset


@pytest.fixture()
def energy_db(db_session):
    """Registry rows, the load schedule and a healthy, fresh input set."""
    for asset_id in ALL_LOADS:
        db_session.add(
            Asset(
                asset_id=asset_id,
                domain="energy",
                asset_class="load",
                name=asset_id,
                status="planned",
                criticality="critical",
                control_authority="supervisory",
            )
        )
    db_session.flush()
    load_schedule(db_session)
    seed_current_state(db_session, ts=utcnow())
    db_session.commit()
    return db_session


def set_state(session, state: str, *, entered_at: dt.datetime | None = None):
    snapshot = ensure_snapshot(session, now=utcnow())
    snapshot.state = state
    snapshot.entered_at = entered_at or utcnow()
    session.commit()
    return snapshot


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def test_get_state_creates_and_returns_the_commissioning_default(client):
    response = client.get("/api/v1/energy/state")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "COMMISSIONING"
    assert body["latching"] is True
    assert body["published_topic"] == "homestead/site/primary/site_01/energy_state"


def test_state_history_is_empty_then_records_operator_actions(client, operator_headers):
    assert client.get("/api/v1/energy/state/history").json()["count"] == 0
    client.post(
        "/api/v1/energy/state/freeze",
        json={"reason": "commissioning walkdown", "duration_s": 600},
        headers=operator_headers,
    )
    history = client.get("/api/v1/energy/state/history").json()
    assert history["count"] == 1
    assert history["transitions"][0]["trigger"] == "operator_freeze"
    assert history["transitions"][0]["actor"] == "test.operator"


def test_freeze_requires_an_operator(client):
    response = client.post("/api/v1/energy/state/freeze", json={"reason": "because"})
    assert response.status_code == 403


def test_freeze_requires_a_reason(client, operator_headers):
    response = client.post("/api/v1/energy/state/freeze", json={"reason": ""}, headers=operator_headers)
    assert response.status_code == 422


def test_freeze_is_bounded_and_published(client, operator_headers, bus, settings):
    response = client.post(
        "/api/v1/energy/state/freeze",
        json={"reason": "inverter firmware update window", "duration_s": 100_000},
        headers=operator_headers,
    )
    assert response.status_code == 200
    body = response.json()
    frozen_until = dt.datetime.fromisoformat(body["frozen_until"])
    assert (frozen_until - utcnow()).total_seconds() <= 3600 + 5
    assert "never suppresses EMERGENCY" in body["note"]
    assert bus.last("homestead/site/primary/site_01/energy_state") is not None


def test_freeze_can_be_released(client, operator_headers):
    client.post(
        "/api/v1/energy/state/freeze",
        json={"reason": "walkdown"},
        headers=operator_headers,
    )
    response = client.post(
        "/api/v1/energy/state/freeze",
        json={"reason": "walkdown complete", "release": True},
        headers=operator_headers,
    )
    assert response.json()["frozen_until"] is None


def test_clear_latch_needs_condition_clear(client, operator_headers, db_session):
    set_state(db_session, "EMERGENCY")
    response = client.post(
        "/api/v1/energy/state/clear-latch",
        json={"reason": "battery cooled", "condition_clear": False},
        headers=operator_headers,
    )
    assert response.status_code == 409
    assert "condition" in response.json()["detail"]


def test_clear_latch_re_enters_conservatively(client, operator_headers, db_session):
    set_state(db_session, "EMERGENCY")
    response = client.post(
        "/api/v1/energy/state/clear-latch",
        json={"reason": "BMS reset and inspected", "condition_clear": True},
        headers=operator_headers,
    )
    assert response.status_code == 200
    assert response.json()["state"] == "CONSERVE"


def test_clear_latch_refuses_an_optimistic_target(client, operator_headers, db_session):
    set_state(db_session, "EMERGENCY")
    response = client.post(
        "/api/v1/energy/state/clear-latch",
        json={"reason": "looks fine", "condition_clear": True, "to_state": "SURPLUS"},
        headers=operator_headers,
    )
    assert response.status_code == 409


def test_clear_latch_on_a_non_latching_state_is_a_conflict(client, operator_headers, db_session):
    set_state(db_session, "NORMAL")
    response = client.post(
        "/api/v1/energy/state/clear-latch",
        json={"reason": "nothing to clear", "condition_clear": True},
        headers=operator_headers,
    )
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Loads
# ---------------------------------------------------------------------------


def test_get_loads_returns_the_schedule_with_open_fields(client, energy_db):
    body = client.get("/api/v1/energy/loads").json()
    assert body["count"] == 12
    assert body["protected_tier"] == 0
    by_id = {row["asset_id"]: row for row in body["loads"]}
    assert by_id[CONTROL_CORE]["effective_tier"] == 0
    assert by_id[CONTROL_CORE]["shed"]["permitted"] is False
    assert by_id[COMPUTE]["effective_tier"] == 4
    for row in body["loads"]:
        assert row["rated_power_kw"] is None
        assert "rated_power_kw" in row["open_fields"]
    # Ordered by effective tier.
    tiers = [row["effective_tier"] for row in body["loads"]]
    assert tiers == sorted(tiers)


def test_shed_actions_endpoint_is_empty_before_any_shedding(client, energy_db):
    assert client.get("/api/v1/energy/shed-actions").json()["count"] == 0


def test_shed_actions_reflect_a_real_shed(client, energy_db, db_session, settings, bus):
    from homestead_twin.ems import RecordingCommandPort
    from homestead_twin.ems.config import EmsConfig
    from homestead_twin.ems.derived import compute_derived
    from homestead_twin.ems.inputs import gather_inputs
    from homestead_twin.ems.shedding import ShedController

    config = EmsConfig()
    now = utcnow()
    inputs = gather_inputs(db_session, now, config)
    # Force a low reserve so the shed preconditions are satisfiable.
    seed_current_state(db_session, ts=now, battery_soc_pct=22.0, battery_energy_available_kwh=140.0)
    inputs = gather_inputs(db_session, now, config)
    derived = compute_derived(inputs, config, now=now)
    controller = ShedController(config, RecordingCommandPort(), settings=settings, bus=bus)
    controller.shed_step(db_session, energy_state="CRITICAL_RESERVE", inputs=inputs, derived=derived, now=now)
    db_session.commit()

    body = client.get("/api/v1/energy/shed-actions").json()
    assert body["count"] >= 1
    assert {row["asset_id"] for row in body["actions"]} <= set(ALL_LOADS)
    assert all(row["energy_state"] == "CRITICAL_RESERVE" for row in body["actions"])

    filtered = client.get(f"/api/v1/energy/shed-actions?asset_id={COMPUTE}").json()
    assert {row["asset_id"] for row in filtered["actions"]} == {COMPUTE}


# ---------------------------------------------------------------------------
# Load budgets and reservations
# ---------------------------------------------------------------------------


def surplus_state(session):
    """A site with real curtailable surplus, in NORMAL."""
    seed_current_state(
        session,
        ts=utcnow(),
        pv_power_kw=20.0,
        battery_charge_limit_kw=5.0,
        battery_soc_pct=90.0,
        battery_energy_available_kwh=576.0,
    )
    set_state(session, "NORMAL")


def test_load_budgets_reports_the_grantable_surplus(client, energy_db, db_session):
    surplus_state(db_session)
    body = client.get("/api/v1/energy/load-budgets").json()
    assert body["energy_state"] == "NORMAL"
    assert body["grantable_surplus_kw"] == pytest.approx(10.0)
    assert body["granted_kw"] == 0.0
    assert body["grants_permitted"] is True
    assert len(body["budgets"]) == 12
    assert "never a safety permissive" in body["note"]


def test_reservation_is_granted_and_listed(client, energy_db, db_session, operator_headers):
    surplus_state(db_session)
    response = client.post(
        "/api/v1/energy/load-budgets/reservations",
        json={
            "asset_id": WORKSHOP,
            "requested_kw": 8.0,
            "reason": "planned welding during the high-solar window",
            "duration_s": 5400,
            "priority": 3,
        },
        headers=operator_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["granted"] is True
    lease_id = body["lease_id"]

    budgets = client.get("/api/v1/energy/load-budgets").json()
    assert budgets["granted_kw"] == 8.0
    assert [lease["lease_id"] for lease in budgets["leases"]] == [lease_id]
    assert budgets["leases"][0]["requested_by"] == "test.operator"


def test_reservation_requires_an_operator(client, energy_db, db_session):
    surplus_state(db_session)
    response = client.post(
        "/api/v1/energy/load-budgets/reservations",
        json={"asset_id": WORKSHOP, "requested_kw": 1.0, "reason": "welding"},
    )
    assert response.status_code == 403


def test_reservation_beyond_the_surplus_is_denied_with_a_reason(
    client, energy_db, db_session, operator_headers
):
    surplus_state(db_session)
    response = client.post(
        "/api/v1/energy/load-budgets/reservations",
        json={"asset_id": WORKSHOP, "requested_kw": 40.0, "reason": "big weld"},
        headers=operator_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["granted"] is False
    assert "exceeds" in body["reason"]
    assert body["lease"]["state"] == "denied"


def test_reservation_is_denied_while_conserving(client, energy_db, db_session, operator_headers):
    surplus_state(db_session)
    set_state(db_session, "CONSERVE")
    body = client.post(
        "/api/v1/energy/load-budgets/reservations",
        json={"asset_id": SPA, "requested_kw": 2.0, "reason": "evening session"},
        headers=operator_headers,
    ).json()
    assert body["granted"] is False
    assert "CONSERVE" in body["reason"]


def test_reservation_can_be_revoked(client, energy_db, db_session, operator_headers):
    surplus_state(db_session)
    lease_id = client.post(
        "/api/v1/energy/load-budgets/reservations",
        json={"asset_id": WORKSHOP, "requested_kw": 4.0, "reason": "welding window"},
        headers=operator_headers,
    ).json()["lease_id"]

    response = client.delete(
        f"/api/v1/energy/load-budgets/reservations/{lease_id}?reason=reserve+declining",
        headers=operator_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["lease"]["state"] == "revoked"
    assert "No stop command is issued" in body["note"]
    assert db_session.get(PowerBudgetLease, lease_id) is not None


def test_revoking_an_unknown_lease_is_404(client, operator_headers):
    response = client.delete(
        "/api/v1/energy/load-budgets/reservations/nope?reason=cleanup", headers=operator_headers
    )
    assert response.status_code == 404


def test_revocation_requires_a_reason(client, energy_db, db_session, operator_headers):
    surplus_state(db_session)
    lease_id = client.post(
        "/api/v1/energy/load-budgets/reservations",
        json={"asset_id": WORKSHOP, "requested_kw": 1.0, "reason": "welding"},
        headers=operator_headers,
    ).json()["lease_id"]
    response = client.delete(f"/api/v1/energy/load-budgets/reservations/{lease_id}", headers=operator_headers)
    assert response.status_code == 422


def test_leases_endpoint_includes_denials(client, energy_db, db_session, operator_headers):
    surplus_state(db_session)
    client.post(
        "/api/v1/energy/load-budgets/reservations",
        json={"asset_id": WORKSHOP, "requested_kw": 99.0, "reason": "too big"},
        headers=operator_headers,
    )
    body = client.get("/api/v1/energy/leases?state=denied").json()
    assert body["count"] == 1


# ---------------------------------------------------------------------------
# Dashboard (SDD 38)
# ---------------------------------------------------------------------------


def test_dashboard_separates_measured_calculated_and_forecast(client, energy_db):
    body = client.get("/api/v1/energy/dashboard").json()

    assert set(body["power_flow"]) >= {"pv_kw", "battery_kw", "critical_panel_kw", "site_load_kw"}
    assert body["power_flow"]["pv_kw"] == HEALTHY_INPUTS["pv_power_kw"]
    assert body["reserve"]["soc_pct"] == HEALTHY_INPUTS["battery_soc_pct"]

    # Measured values are the raw readings with quality; calculated and forecast
    # are separate blocks (SDD 38 final rule).
    assert body["measured"]["battery_soc_pct"]["quality"] == "good"
    assert "energy_above_emergency_reserve_kwh" in body["calculated"]
    assert "forecast_pv_next_24h_kwh" in body["forecast"]
    assert body["forecast"]["forecast_pv_next_24h_kwh"]["assumptions"]
    assert "placeholder" in " ".join(body["forecast"]["forecast_pv_next_24h_kwh"]["assumptions"])


def test_dashboard_reports_data_quality_for_every_state_machine_value(client, energy_db):
    body = client.get("/api/v1/energy/dashboard").json()
    quality = body["data_quality"]
    assert quality["observable"] is True
    assert quality["invalid_required"] == []
    assert quality["per_input"]["battery_soc_pct"] == "ok"
    assert quality["per_derived"]["autonomy_critical_h"] is True
    # Points the register has not bound yet are reported missing, not defaulted.
    assert any(status == "missing" for status in quality["per_input"].values())


def test_dashboard_shows_impaired_observability(client, energy_db, db_session):
    from test_ems_state_machine import write_point

    from homestead_twin.ems.inputs import SPEC_BY_KEY

    write_point(
        db_session,
        SPEC_BY_KEY["battery_soc_pct"].point_id,
        70.0,
        ts=utcnow() - dt.timedelta(hours=2),
    )
    db_session.commit()

    body = client.get("/api/v1/energy/dashboard").json()
    assert body["data_quality"]["observable"] is False
    assert "battery_soc_pct" in body["data_quality"]["invalid_required"]
    assert body["reserve"]["soc_pct"] is None


def test_dashboard_lists_generator_leases_and_container(client, energy_db, db_session):
    surplus_state(db_session)
    body = client.get("/api/v1/energy/dashboard").json()
    assert body["generator"]["observed_state"] == "stopped"
    assert body["generator"]["fuel_level_pct"] == 80.0
    assert body["power_container"]["temperature_c"] == HEALTHY_INPUTS["container_temperature_c"]
    assert body["power_container"]["thermal_derate_pct"] == 0.0
    assert body["leases"] == []
    assert body["grantable_surplus_kw"] == pytest.approx(10.0)
    assert body["restoration_headroom_kw"] is not None


def test_dashboard_shows_shed_and_restoration_context(client, energy_db, db_session, settings, bus):
    from homestead_twin.ems import RecordingCommandPort
    from homestead_twin.ems.config import EmsConfig
    from homestead_twin.ems.derived import compute_derived
    from homestead_twin.ems.inputs import gather_inputs
    from homestead_twin.ems.shedding import ShedController

    config = EmsConfig()
    now = utcnow()
    seed_current_state(db_session, ts=now, battery_soc_pct=22.0, battery_energy_available_kwh=140.0)
    inputs = gather_inputs(db_session, now, config)
    derived = compute_derived(inputs, config, now=now)
    ShedController(config, RecordingCommandPort(), settings=settings, bus=bus).shed_step(
        db_session, energy_state="CRITICAL_RESERVE", inputs=inputs, derived=derived, now=now
    )
    db_session.commit()

    body = client.get("/api/v1/energy/dashboard").json()
    assert body["shed_loads"]
    assert {row["asset_id"] for row in body["shed_loads"]} == {COMPUTE, "energy.load.site.tool_charging_01"}
    assert body["pending_restoration"]
    assert all("inrush_class" in row for row in body["pending_restoration"])


def test_dashboard_is_readable_without_an_operator_header(client, energy_db):
    body = client.get("/api/v1/energy/dashboard").json()
    assert body["viewer"] == "anonymous"


# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------


def test_service_tick_publishes_state_and_budgets(session_factory, bus, settings, energy_db, db_session):
    from homestead_twin.ems import RecordingCommandPort
    from homestead_twin.ems.service import EnergyManagerService

    set_state(db_session, "NORMAL")
    service = EnergyManagerService(
        session_factory,
        bus,
        settings.model_copy(update={"ems_enabled": True}),
        command_port=RecordingCommandPort(),
    )
    assert service.name == "ems"
    assert service.state_topic == "homestead/site/primary/site_01/energy_state"

    result = service.tick(utcnow())
    assert result.state == "NORMAL"
    assert result.published_topic == service.state_topic
    assert bus.last(service.state_topic) is not None
    # A load budget is published per load (SDD 13: state plus load budget).
    assert bus.last("homestead/energy/site/load_spa_01/power_budget_kw") is not None


def test_snapshot_derived_blob_is_readable_by_other_subsystems(
    session_factory, bus, settings, energy_db, db_session, client
):
    """The overview roll-up reads flat scalars; the EMS keeps the provenance."""
    from homestead_twin.ems import RecordingCommandPort
    from homestead_twin.ems.service import EnergyManagerService

    set_state(db_session, "NORMAL")
    EnergyManagerService(
        session_factory,
        bus,
        settings.model_copy(update={"ems_enabled": True}),
        command_port=RecordingCommandPort(),
    ).tick(utcnow())

    derived = client.get("/api/v1/energy/state").json()["derived"]
    # Flat projection for consumers that should not know this module's shape.
    assert derived["reserve_pct"] == HEALTHY_INPUTS["battery_soc_pct"]
    assert derived["autonomy_critical_h"] is not None
    # Full provenance is still there for the energy dashboard.
    assert derived["values"]["autonomy_critical_h"]["basis"]
    assert derived["values"]["autonomy_critical_h"]["inputs"]
    assert "generator" in derived


def test_service_tick_is_a_no_op_when_disabled(session_factory, bus, settings, energy_db):
    from homestead_twin.ems.service import EnergyManagerService

    service = EnergyManagerService(session_factory, bus, settings)  # ems_enabled False
    result = service.tick(utcnow())
    assert result.skipped_reason == "ems_enabled is false"
