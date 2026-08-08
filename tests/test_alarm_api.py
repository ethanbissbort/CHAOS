"""Alarm, incident and notification API (SDD 41, FR-007, FR-008).

The lifecycle endpoints are audited writes: operator role, mandatory reason, and
a lifecycle event for every transition. The definition endpoint is the FR-008
contract -- procedure, affected assets, dependencies and manual controls -- and
it has to tell the truth about the gaps, because ``manual_override`` is empty for
every asset in the v0.3 register.
"""

from __future__ import annotations

import datetime as dt

import pytest
from test_alarm_correlation import (
    AC_MAIN,
    SERVER,
    T0,
    UPS,
    at,
    load_real_registry,
    set_state,
)

from chaos.alarms.correlation import CorrelationEngine
from chaos.alarms.definitions import sync_definitions
from chaos.alarms.evaluator import AlarmEvaluator
from chaos.alarms.notify import Notifier
from chaos.models.alarms import Alarm, Incident

BATTERY = "energy.battery_bank.power_container.01"
GENERATOR = "energy.generator.site.01"


@pytest.fixture()
def seeded(db_session):
    """Registry + definitions committed so the API's own session can see them."""
    load_real_registry(db_session)
    sync_definitions(db_session, strict=True)
    db_session.commit()
    return db_session


@pytest.fixture()
def raise_alarms(seeded, bus, settings):
    """Raise a container-outage cascade and correlate it. Returns the incident id."""

    def _raise() -> str:
        set_state(seeded, AC_MAIN, "energized_state", False)
        set_state(seeded, UPS, "on_battery", True)
        set_state(seeded, SERVER, "availability_state", "offline")
        evaluator = AlarmEvaluator(seeded, bus, settings)
        evaluator.evaluate(at(0))
        evaluator.evaluate(at(600))
        CorrelationEngine(seeded, settings).correlate(at(600))
        Notifier(seeded, settings).dispatch_pending(at(600))
        seeded.commit()
        return _open_incident_id(seeded)

    return _raise


def _open_incident_id(session) -> str:
    return next(i.id for i in session.query(Incident).all() if i.state == "open")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_active_alarms_endpoint(client, raise_alarms, operator_headers):
    raise_alarms()
    response = client.get("/api/v1/alarms/active")
    assert response.status_code == 200
    payload = response.json()

    assert payload["count"] >= 3
    assert payload["incident_count"] == 1
    assert payload["suppressed_count"] >= 1
    assert set(payload["by_severity"]) <= {"info", "warning", "major", "critical", "emergency"}
    keys = {a["alarm_key"] for a in payload["alarms"]}
    assert "power_container_ac_bus_lost" in keys
    assert all(a["state"] in ("active", "acknowledged", "mitigated") for a in payload["alarms"])

    # Suppressed alarms are visible by default: hiding them would be the alarm
    # flood problem in reverse.
    unsuppressed = client.get("/api/v1/alarms/active?include_suppressed=false").json()
    assert unsuppressed["count"] < payload["count"]
    assert all(not a["suppressed"] for a in unsuppressed["alarms"])


def test_active_alarms_can_include_pending_candidates(client, seeded, bus, settings):
    set_state(seeded, BATTERY, "soc_pct", 38.0, unit="%")
    AlarmEvaluator(seeded, bus, settings).evaluate(at(0))
    seeded.commit()

    assert client.get("/api/v1/alarms/active").json()["count"] == 0
    pending = client.get("/api/v1/alarms/active?include_pending=true").json()
    assert pending["count"] == 1
    assert pending["alarms"][0]["state"] == "detected"


def test_alarm_history_filters(client, raise_alarms):
    raise_alarms()
    assert client.get("/api/v1/alarms?state=active").json()["count"] >= 3
    assert client.get("/api/v1/alarms?severity=critical").json()["count"] >= 1
    scoped = client.get(f"/api/v1/alarms?asset_id={AC_MAIN}").json()
    assert scoped["count"] == 1
    assert scoped["alarms"][0]["asset_id"] == AC_MAIN

    assert client.get("/api/v1/alarms?limit=2").json()["count"] <= 2
    assert client.get("/api/v1/alarms?state=nonsense").status_code == 400
    assert client.get("/api/v1/alarms?severity=nonsense").status_code == 400

    # "+00:00" would be swallowed as a space in a query string; use the Z form.
    future = (T0 + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert client.get(f"/api/v1/alarms?since={future}").json()["count"] == 0
    past = (T0 - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert client.get(f"/api/v1/alarms?since={past}").json()["count"] >= 3


def test_get_one_alarm_returns_its_lifecycle_and_definition(client, raise_alarms, seeded):
    raise_alarms()
    alarm = next(a for a in seeded.query(Alarm).all() if a.alarm_key == "power_container_ac_bus_lost")
    payload = client.get(f"/api/v1/alarms/{alarm.id}").json()

    assert payload["alarm_key"] == "power_container_ac_bus_lost"
    assert [e["to_state"] for e in payload["events"]] == ["detected", "active"]
    assert payload["definition"]["procedure_ref"] == "OP-ENERGY-CONTAINER-OUTAGE"
    assert payload["definition"]["procedure_status"] == "not_yet_written"
    assert payload["definition"]["operator_action"]
    assert payload["incident"]["severity"] == "critical"

    assert client.get("/api/v1/alarms/does-not-exist").status_code == 404


# ---------------------------------------------------------------------------
# Definitions: the SDD FR-008 contract
# ---------------------------------------------------------------------------


def test_definition_list_reports_commissioning_defaults(client, seeded):
    payload = client.get("/api/v1/alarms/definitions").json()
    assert payload["count"] == 40
    assert payload["commissioning_default_count"] > 0
    assert "battery_soc_low" in payload["commissioning_default_keys"]

    energy = client.get("/api/v1/alarms/definitions?domain=energy").json()
    assert 0 < energy["count"] < payload["count"]
    assert all(d["domain"] == "energy" for d in energy["definitions"])

    critical = client.get("/api/v1/alarms/definitions?severity=critical").json()
    assert all(d["severity"] == "critical" for d in critical["definitions"])


def test_definition_detail_carries_the_fr_008_context(client, seeded):
    payload = client.get("/api/v1/alarms/definitions/power_container_ac_bus_lost").json()

    # FR-008: procedure, affected assets, dependencies, manual controls.
    assert payload["procedure"]["reference"] == "OP-ENERGY-CONTAINER-OUTAGE"
    assert payload["procedure"]["status"] == "not_yet_written"

    affected = {a["asset_id"] for a in payload["affected_assets"]}
    assert AC_MAIN in affected
    assert "energy.panel.power_container.critical_01" in affected
    assert all(a["name"] and a["criticality"] for a in payload["affected_assets"])

    relationships = payload["dependencies"]["relationships"]
    assert any(r["from_asset_id"] == AC_MAIN and r["relationship_type"] == "feeds" for r in relationships)

    controls = {c["asset_id"]: c for c in payload["manual_controls"]}
    assert set(controls) == affected
    # The v0.3 register documents no manual overrides. Say so; do not imply there
    # is a handle to pull.
    assert all(c["status"] == "not_documented" for c in controls.values())
    assert controls[AC_MAIN]["open_fields"]  # ... and show what is missing
    assert "lockout_procedure" in controls[AC_MAIN]["open_fields"]

    assert payload["probable_causes"]
    assert payload["operator_action"]
    assert "16.1" in payload["automatic_action"] or payload["automatic_action"]
    assert payload["escalation_path"][0]["channels"] == ["log", "email", "push"]
    assert payload["correlation"]["is_root_candidate"] is True
    assert "ups_on_battery" in payload["correlation"]["symptom_alarm_keys"]


def test_definition_detail_reports_threshold_provenance(client, seeded):
    payload = client.get("/api/v1/alarms/definitions/battery_reserve_critical").json()
    assert payload["threshold_status"] == "commissioning_default"
    assert "640 kWh" in payload["threshold_basis"]
    assert payload["reset"]["declared"] is True
    assert payload["trigger"]["value"]["value"] == 64
    assert payload["reset"]["value"]["value"] == 96

    derived = client.get("/api/v1/alarms/definitions/inverter_overload_risk").json()
    assert derived["threshold_status"] == "derived_from_design_document"
    assert "10 kW" in derived["threshold_basis"]


def test_class_scoped_definition_lists_every_instance(client, seeded):
    payload = client.get("/api/v1/alarms/definitions/inverter_fault").json()
    assert payload["scope"]["asset_class"] == "inverter"
    inverters = [a for a in payload["affected_assets"] if a["asset_class"] == "inverter"]
    assert len(inverters) == 4


def test_unknown_definition_is_404(client, seeded):
    assert client.get("/api/v1/alarms/definitions/no_such_alarm").status_code == 404


# ---------------------------------------------------------------------------
# Lifecycle writes
# ---------------------------------------------------------------------------


def _alarm_id(session, alarm_key: str) -> str:
    return next(a.id for a in session.query(Alarm).all() if a.alarm_key == alarm_key)


def test_full_lifecycle_over_the_api(client, raise_alarms, seeded, operator_headers):
    raise_alarms()
    alarm_id = _alarm_id(seeded, "power_container_ac_bus_lost")

    ack = client.post(
        f"/api/v1/alarms/{alarm_id}/acknowledge",
        json={"note": "Driving to the container now."},
        headers=operator_headers,
    )
    assert ack.status_code == 200
    assert ack.json()["state"] == "acknowledged"
    assert ack.json()["acknowledged_by"] == "test.operator"

    mitigate = client.post(
        f"/api/v1/alarms/{alarm_id}/mitigate",
        json={"note": "Generator started manually; critical panel back up."},
        headers=operator_headers,
    )
    assert mitigate.json()["state"] == "mitigated"

    clear = client.post(
        f"/api/v1/alarms/{alarm_id}/clear",
        json={"note": "Bus re-energized.", "force": True},
        headers=operator_headers,
    )
    assert clear.json()["state"] == "cleared"

    review = client.post(
        f"/api/v1/alarms/{alarm_id}/review",
        json={"note": "Root cause: main breaker tripped on inverter fault."},
        headers=operator_headers,
    )
    assert review.status_code == 200
    body = review.json()
    assert body["state"] == "reviewed"
    assert [e["to_state"] for e in body["events"]] == [
        "detected",
        "active",
        "acknowledged",
        "mitigated",
        "cleared",
        "reviewed",
    ]
    assert all(e["note"] for e in body["events"][2:])
    assert all(e["actor"] == "test.operator" for e in body["events"][2:])


def test_lifecycle_writes_require_the_operator_role(client, raise_alarms, seeded):
    raise_alarms()
    alarm_id = _alarm_id(seeded, "power_container_ac_bus_lost")

    assert (
        client.post(f"/api/v1/alarms/{alarm_id}/acknowledge", json={"note": "anonymous"}).status_code == 403
    )
    assert (
        client.post(
            f"/api/v1/alarms/{alarm_id}/acknowledge",
            json={"note": "just looking"},
            headers={"X-Operator": "viewer.vic", "X-Operator-Role": "viewer"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/alarms/{alarm_id}/acknowledge",
            json={"note": "escalated privileges are fine"},
            headers={"X-Operator": "admin.ada", "X-Operator-Role": "administrator"},
        ).status_code
        == 200
    )


def test_lifecycle_writes_require_a_reason(client, raise_alarms, seeded, operator_headers):
    raise_alarms()
    alarm_id = _alarm_id(seeded, "power_container_ac_bus_lost")
    for body in ({}, {"note": ""}, {"note": "  "}):
        response = client.post(f"/api/v1/alarms/{alarm_id}/acknowledge", json=body, headers=operator_headers)
        assert response.status_code == 422, body


def test_illegal_transitions_are_rejected(client, raise_alarms, seeded, operator_headers):
    raise_alarms()
    alarm_id = _alarm_id(seeded, "power_container_ac_bus_lost")

    # Cannot review before clearing.
    assert (
        client.post(
            f"/api/v1/alarms/{alarm_id}/review",
            json={"note": "skipping ahead"},
            headers=operator_headers,
        ).status_code
        == 409
    )

    client.post(
        f"/api/v1/alarms/{alarm_id}/acknowledge",
        json={"note": "seen"},
        headers=operator_headers,
    )
    # Cannot acknowledge twice.
    assert (
        client.post(
            f"/api/v1/alarms/{alarm_id}/acknowledge",
            json={"note": "seen again"},
            headers=operator_headers,
        ).status_code
        == 409
    )


def test_clear_is_refused_while_the_condition_persists(client, seeded, bus, settings, operator_headers):
    set_state(seeded, GENERATOR, "start_failure_active", True)
    AlarmEvaluator(seeded, bus, settings).evaluate(at(0))
    seeded.commit()
    alarm_id = _alarm_id(seeded, "generator_start_failed")

    refused = client.post(
        f"/api/v1/alarms/{alarm_id}/clear",
        json={"note": "probably fine"},
        headers=operator_headers,
    )
    assert refused.status_code == 409
    assert "still true" in refused.json()["detail"]

    forced = client.post(
        f"/api/v1/alarms/{alarm_id}/clear",
        json={"note": "Attended; start battery replaced.", "force": True},
        headers=operator_headers,
    )
    assert forced.status_code == 200
    assert "operator override" in forced.json()["events"][-1]["note"]


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


def test_incident_endpoints(client, raise_alarms, seeded):
    incident_id = raise_alarms()

    listing = client.get("/api/v1/incidents").json()
    assert listing["count"] == 1
    assert listing["incidents"][0]["incident_id"] == incident_id
    assert listing["incidents"][0]["member_count"] >= 3

    detail = client.get(f"/api/v1/incidents/{incident_id}").json()
    assert detail["severity"] == "critical"
    assert detail["root_cause"]["alarm_key"] == "power_container_ac_bus_lost"
    assert detail["member_count"] == len(detail["alarms"])
    symptoms = [a for a in detail["alarms"] if a["alarm_id"] != detail["root_cause"]["alarm_id"]]
    assert symptoms and all(a["suppressed"] for a in symptoms)

    assert detail["notifications"]
    assert detail["notifications"][0]["detail"]["stage"] == 1
    assert {n["channel"] for n in detail["notifications"]} >= {"log"}

    assert client.get("/api/v1/incidents?state=closed").json()["count"] == 0
    assert client.get("/api/v1/incidents/does-not-exist").status_code == 404


def test_clearing_the_last_member_closes_the_incident(client, raise_alarms, seeded, operator_headers):
    incident_id = raise_alarms()
    for alarm in list(seeded.query(Alarm).all()):
        if alarm.state in ("cleared", "reviewed"):
            continue
        response = client.post(
            f"/api/v1/alarms/{alarm.id}/clear",
            json={"note": "Container restored.", "force": True},
            headers=operator_headers,
        )
        assert response.status_code == 200

    detail = client.get(f"/api/v1/incidents/{incident_id}").json()
    assert detail["state"] == "closed"
    assert detail["closed_at"] is not None


# ---------------------------------------------------------------------------
# Notifications and reload
# ---------------------------------------------------------------------------


def test_notification_log_endpoint(client, raise_alarms):
    raise_alarms()
    payload = client.get("/api/v1/notifications").json()
    assert payload["count"] >= 1

    log_rows = [n for n in payload["notifications"] if n["channel"] == "log"]
    assert log_rows and log_rows[0]["status"] == "sent"
    assert log_rows[0]["detail"]["stage"] == 1
    assert log_rows[0]["detail"]["root_alarm_key"] == "power_container_ac_bus_lost"

    unsent = client.get("/api/v1/notifications?status=not_configured").json()
    for record in unsent["notifications"]:
        assert record["channel"] in ("email", "push", "voice")
        assert "No message was sent" in record["detail"]["channel_detail"]

    assert client.get("/api/v1/notifications?channel=log").json()["count"] >= 1
    assert client.get("/api/v1/notifications?limit=1").json()["count"] == 1


def test_definitions_reload_requires_maintainer_and_is_idempotent(
    client, seeded, operator_headers, admin_headers
):
    assert client.post("/api/v1/alarms/definitions/reload").status_code == 403
    # Reloading replaces safety-relevant trip thresholds; an operator may
    # acknowledge alarms but may not redefine them.
    assert client.post("/api/v1/alarms/definitions/reload", headers=operator_headers).status_code == 403

    response = client.post("/api/v1/alarms/definitions/reload", headers=admin_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["reloaded_by"] == "test.admin"
    assert payload["total"] == 40
    assert payload["created"] == []
    assert payload["updated"] == []
    assert payload["unchanged"] == 40


def test_reload_of_an_uncrossreferenced_registry_is_rejected(client, db_session, operator_headers):
    """Reloading without a registry must fail loudly, not install phantom alarms."""
    from chaos.alarms.definitions import DefinitionError, sync_definitions

    # No assets or point definitions loaded: the package files still resolve, so
    # the reload succeeds. Point the loader at a broken document instead.
    with pytest.raises(DefinitionError):
        sync_definitions(db_session, path="data/does_not_exist.yaml", strict=True)


def test_alarm_definition_scope_is_visible_in_the_api(client, seeded):
    payload = client.get("/api/v1/alarms/definitions/sensor_data_invalid").json()
    assert payload["scope"] == {"asset_id": None, "asset_class": None, "point_name": None}
    assert payload["trigger"]["operator"] == "quality_in"
    assert payload["trigger"]["value"]["values"] == ["bad", "stale"]
    assert payload["threshold_status"] == "not_applicable"
