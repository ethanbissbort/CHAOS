"""Telemetry API tests (SDD 41).

Point IDs contain a slash, so every assertion here also proves the ``:path``
routing works against real canonical identifiers.

Point resources answer at two addresses (see the router docstring): the canonical
``/points/{point_id:path}/...`` and the ``/telemetry/points/...`` alias that no
other subsystem's routes can shadow. Both are exercised through the assembled
application, so a future routing collision under ``/points/`` fails a test here
instead of silently removing an endpoint.
"""

from __future__ import annotations

import datetime as dt

import pytest

from chaos import topics
from chaos.envelope import TelemetryEnvelope
from chaos.ingest.service import IngestService
from chaos.ingest.writer import TelemetryWriter
from chaos.models.registry import Asset, AssetClass, Point, PointBinding, PointDefinition
from chaos.models.telemetry import CurrentState, IngestDeadLetter

T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)

BATTERY = "energy.battery_bank.power_container.01"
CISTERN = "water.cistern.orchard.01"

SOC = f"{BATTERY}/soc_pct"
LEVEL = f"{CISTERN}/level_pct"
MODE = f"{BATTERY}/mode_actual"


def seconds(n: float) -> dt.timedelta:
    return dt.timedelta(seconds=n)


def _point(session, asset_id, point_name, data_type, unit=None, enum_values=None, stale_after_s=None):
    parts = topics.split_asset_id(asset_id)
    if session.get(AssetClass, parts.asset_class) is None:
        session.add(AssetClass(name=parts.asset_class, allowed_domains=[parts.domain]))
    if session.get(Asset, asset_id) is None:
        session.add(
            Asset(
                asset_id=asset_id,
                domain=parts.domain,
                asset_class=parts.asset_class,
                name=asset_id,
                status="commissioned",
            )
        )
    if session.get(PointDefinition, point_name) is None:
        session.add(
            PointDefinition(
                name=point_name,
                default_class="AI",
                allowed_classes=["AI"],
                data_type=data_type,
                unit=unit,
                enum_values=enum_values,
            )
        )
    point_id = topics.point_id(asset_id, point_name)
    session.add(
        Point(
            point_id=point_id,
            asset_id=asset_id,
            point_name=point_name,
            point_class="AI",
            data_type=data_type,
            unit=unit,
            enum_values=enum_values,
            stale_after_s=stale_after_s,
        )
    )
    session.add(
        PointBinding(
            point_id=point_id,
            asset_id=asset_id,
            point_name=point_name,
            binding_status="commissioned",
            quality_policy="reject_invalid",
        )
    )
    return point_id


@pytest.fixture()
def seeded(db_session, settings):
    """A small registry with history, one stale point and one dead letter."""
    _point(db_session, BATTERY, "soc_pct", "float", unit="%", stale_after_s=20)
    _point(db_session, CISTERN, "level_pct", "float", unit="%", stale_after_s=20)
    _point(
        db_session,
        BATTERY,
        "mode_actual",
        "enum",
        enum_values=["off", "manual", "automatic"],
        stale_after_s=86400,  # a mode is not expected to republish every minute
    )
    _point(db_session, BATTERY, "power_kw", "float", unit="kW")  # registered, never reports
    db_session.commit()

    writer = TelemetryWriter(settings)
    for index, value in enumerate([70.0, 71.5, 73.4]):
        writer.apply(
            db_session,
            TelemetryEnvelope(
                ts=T0 + seconds(index * 60),
                asset_id=BATTERY,
                point="soc_pct",
                value=value,
                unit="%",
                source="bms.master",
                sequence=index + 1,
            ),
            now=T0 + seconds(index * 60),
        )
    writer.apply(
        db_session,
        TelemetryEnvelope(ts=T0, asset_id=CISTERN, point="level_pct", value=41.0, source="water.gateway"),
        now=T0,
    )
    writer.apply(
        db_session,
        TelemetryEnvelope(ts=T0, asset_id=BATTERY, point="mode_actual", value="automatic"),
        now=T0,
    )
    # The cistern stopped reporting an hour ago.
    writer.mark_stale(db_session, T0 + seconds(3600))
    db_session.add(
        IngestDeadLetter(
            topic="chaos/energy/power_container/battery_bank_99/soc_pct",
            payload='{"value": 1}',
            reason="unresolved_topic: not bound to a point",
        )
    )
    db_session.commit()
    return writer


# ---------------------------------------------------------------------------
# Current state
# ---------------------------------------------------------------------------


def test_point_resources_answer_on_both_addresses(client, seeded):
    """The canonical path and the unshadowable alias must stay interchangeable."""
    for suffix in ("current", "history"):
        canonical = client.get(f"/api/v1/points/{SOC}/{suffix}")
        alias = client.get(f"/api/v1/telemetry/points/{SOC}/{suffix}")

        assert canonical.status_code == 200, f"/points/.../{suffix} is shadowed by another router"
        assert alias.status_code == 200
        # ``age_s`` is computed per request, so it differs by microseconds.
        assert _without_age(canonical.json()) == _without_age(alias.json())


def _without_age(body: dict) -> dict:
    return {key: value for key, value in body.items() if key != "age_s"}


def test_get_current_state_for_a_point_id_containing_slashes(client, seeded):
    response = client.get(f"/api/v1/points/{SOC}/current")

    assert response.status_code == 200
    body = response.json()
    assert body["point_id"] == SOC
    assert body["value"] == pytest.approx(73.4)
    assert body["unit"] == "%"
    assert body["quality"] == "stale"  # swept forward by the fixture
    assert body["source"] == "bms.master"
    assert body["sequence"] == 3
    assert body["stale_after_s"] == 20
    assert body["age_s"] > 0


def test_get_current_state_distinguishes_unknown_from_never_reported(client, seeded):
    unknown = client.get("/api/v1/points/energy.inverter.power_container.09/power_kw/current")
    assert unknown.status_code == 404
    assert "Unknown point" in unknown.json()["detail"]

    silent = client.get(f"/api/v1/points/{BATTERY}/power_kw/current")
    assert silent.status_code == 404
    assert "never reported" in silent.json()["detail"]


def test_bulk_current_state_and_filters(client, seeded):
    everything = client.get("/api/v1/telemetry/current").json()
    assert {row["point_id"] for row in everything} == {SOC, LEVEL, MODE}

    by_asset = client.get("/api/v1/telemetry/current", params={"asset_id": CISTERN}).json()
    assert [row["point_id"] for row in by_asset] == [LEVEL]

    by_domain = client.get("/api/v1/telemetry/current", params={"domain": "water"}).json()
    assert [row["point_id"] for row in by_domain] == [LEVEL]

    by_name = client.get("/api/v1/telemetry/current", params={"point_name": "soc_pct"}).json()
    assert [row["point_id"] for row in by_name] == [SOC]

    by_quality = client.get("/api/v1/telemetry/current", params={"quality": "good"}).json()
    assert {row["point_id"] for row in by_quality} == {MODE}


def test_bulk_current_state_reports_enum_values_as_text(client, seeded):
    rows = client.get("/api/v1/telemetry/current", params={"point_name": "mode_actual"}).json()
    assert rows[0]["value"] == "automatic"
    assert rows[0]["quality"] == "good"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_history_returns_samples_oldest_first(client, seeded):
    body = client.get(f"/api/v1/points/{SOC}/history").json()

    assert body["point_id"] == SOC
    assert body["count"] == 3
    assert body["truncated"] is False
    values = [sample["value"] for sample in body["samples"]]
    assert values == pytest.approx([70.0, 71.5, 73.4])
    assert body["samples"][0]["unit"] == "%"


def test_history_window_and_limit(client, seeded):
    windowed = client.get(
        f"/api/v1/points/{SOC}/history",
        params={"start": T0.isoformat(), "end": (T0 + seconds(90)).isoformat()},
    ).json()
    assert [s["value"] for s in windowed["samples"]] == pytest.approx([70.0, 71.5])

    limited = client.get(f"/api/v1/points/{SOC}/history", params={"limit": 1}).json()
    assert limited["count"] == 1
    assert limited["truncated"] is True
    assert limited["samples"][0]["value"] == pytest.approx(73.4)  # newest kept


def test_history_of_unknown_point_is_404(client, seeded):
    response = client.get("/api/v1/points/energy.inverter.power_container.09/power_kw/history")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Operator aids
# ---------------------------------------------------------------------------


def test_stale_endpoint_lists_untrusted_points(client, seeded):
    rows = client.get("/api/v1/telemetry/stale").json()

    assert {row["point_id"] for row in rows} == {SOC, LEVEL}
    assert all(row["quality"] == "stale" for row in rows)

    filtered = client.get("/api/v1/telemetry/stale", params={"asset_id": CISTERN}).json()
    assert [row["point_id"] for row in filtered] == [LEVEL]


def test_dead_letters_endpoint(client, seeded):
    rows = client.get("/api/v1/telemetry/dead-letters").json()
    assert len(rows) == 1
    assert rows[0]["reason"].startswith("unresolved_topic")
    assert rows[0]["topic"].endswith("soc_pct")

    assert client.get("/api/v1/telemetry/dead-letters", params={"reason": "malformed"}).json() == []
    assert len(client.get("/api/v1/telemetry/dead-letters", params={"topic": "battery"}).json()) == 1


def test_stats_reports_database_volume_without_a_running_service(client, seeded):
    body = client.get("/api/v1/telemetry/stats").json()

    assert body["ingest"] is None  # MQTT disabled in tests
    assert body["ingest_running"] is False
    assert body["database"]["points"] == 4
    assert body["database"]["current_state_rows"] == 3
    assert body["database"]["samples"] == 5
    assert body["database"]["indexed_series"] == 3
    assert body["database"]["dead_letters"] == 1
    assert body["database"]["quality"]["stale"] == 2


def test_stats_exposes_ingest_counters_when_the_service_is_registered(
    client, app, session_factory, bus, settings, seeded
):
    service = IngestService(session_factory, bus, settings, auto_sweep=False, clock=lambda: T0)
    app.state.services.register(service)
    service.start()
    try:
        bus.publish(
            topics.telemetry_topic(BATTERY, "soc_pct"),
            TelemetryEnvelope(
                ts=T0 + seconds(600), asset_id=BATTERY, point="soc_pct", value=74.0, unit="%"
            ).to_payload(),
        )
        body = client.get("/api/v1/telemetry/stats").json()
    finally:
        service.stop()

    assert body["ingest_running"] is True
    counters = body["ingest"]["counters"]
    assert counters["messages_received"] == 1
    assert counters["messages_written"] == 1
    for required in ("dead_lettered", "out_of_order", "stale_marked"):
        assert required in counters
    assert body["ingest"]["resolver"]["points"] == 4


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def test_simulate_requires_an_operator(client, seeded):
    payload = TelemetryEnvelope(ts=T0, asset_id=BATTERY, point="soc_pct", value=61.0, unit="%").model_dump(
        mode="json"
    )

    anonymous = client.post("/api/v1/telemetry/simulate", json=payload)
    assert anonymous.status_code == 403

    viewer = client.post(
        "/api/v1/telemetry/simulate",
        json=payload,
        headers={"X-Operator": "someone", "X-Operator-Role": "viewer"},
    )
    assert viewer.status_code == 403


def test_simulate_writes_through_the_writer_and_marks_provenance(
    client, db_session, seeded, operator_headers
):
    response = client.post(
        "/api/v1/telemetry/simulate",
        json=TelemetryEnvelope(
            ts=T0 + seconds(3600),
            asset_id=BATTERY,
            point="soc_pct",
            value=61.0,
            unit="%",
            source="pretend.bms",
        ).model_dump(mode="json"),
        headers=operator_headers,
    )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["quality"] == "good"
    assert body["current_state_updated"] and body["historised"]
    assert body["source"] == "api.simulate"

    db_session.rollback()
    db_session.expire_all()
    state = db_session.get(CurrentState, SOC)
    assert state.value_numeric == pytest.approx(61.0)
    # The caller's claimed source is discarded: simulated data stays labelled.
    assert state.source == "api.simulate"
    assert state.quality == "good"


def test_simulate_rejects_an_unknown_point(client, seeded, operator_headers):
    response = client.post(
        "/api/v1/telemetry/simulate",
        json=TelemetryEnvelope(
            ts=T0, asset_id="energy.inverter.power_container.09", point="power_kw", value=3.0
        ).model_dump(mode="json"),
        headers=operator_headers,
    )
    assert response.status_code == 404
    assert "Unknown point" in response.json()["detail"]


def test_simulate_reports_faults_and_dead_letters_them(client, seeded, operator_headers):
    response = client.post(
        "/api/v1/telemetry/simulate",
        json=TelemetryEnvelope(
            ts=T0 + seconds(7200), asset_id=BATTERY, point="soc_pct", value=61.0, unit="kWh"
        ).model_dump(mode="json"),
        headers=operator_headers,
    )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is False
    assert body["quality"] == "bad"
    assert any("unit_mismatch" in problem for problem in body["problems"])

    letters = client.get("/api/v1/telemetry/dead-letters", params={"reason": "unit_mismatch"}).json()
    assert len(letters) == 1
    assert "test.operator" in letters[0]["reason"]
    assert letters[0]["topic"].startswith("api.simulate/")


def test_simulate_enum_violation_is_visible_to_the_operator(client, seeded, operator_headers):
    response = client.post(
        "/api/v1/telemetry/simulate",
        json=TelemetryEnvelope(
            ts=T0 + seconds(60), asset_id=BATTERY, point="mode_actual", value="turbo"
        ).model_dump(mode="json"),
        headers=operator_headers,
    )

    body = response.json()
    assert body["quality"] == "bad"
    assert any("enum_violation" in problem for problem in body["problems"])

    current = client.get(f"/api/v1/points/{MODE}/current").json()
    assert current["value"] == "automatic"  # last good value retained
    assert current["quality"] == "bad"


def test_simulated_history_is_distinguishable_from_field_data(client, seeded, operator_headers):
    client.post(
        "/api/v1/telemetry/simulate",
        json=TelemetryEnvelope(
            ts=T0 + seconds(300), asset_id=BATTERY, point="soc_pct", value=75.0, unit="%"
        ).model_dump(mode="json"),
        headers=operator_headers,
    )

    body = client.get(f"/api/v1/points/{SOC}/history").json()
    sources = {sample["source"] for sample in body["samples"]}
    assert sources == {"bms.master", "api.simulate"}
