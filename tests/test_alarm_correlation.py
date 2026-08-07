"""Root-cause correlation, incidents and notification routing (SDD 14.2, FR-007).

The load-bearing test here is
:func:`test_power_container_outage_produces_one_incident`. The SDD is explicit:

    "A power-container outage should not create hundreds of separate
    notifications without a parent incident."

That test drives the *real* relationship graph out of
``data/homestead_asset_register.yaml`` -- the same 90 assets and 99 typed
relationships the platform ships with -- knocks the main AC bus out, and asserts
that the whole cascade collapses into a single incident with a single
notification.
"""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from homestead_twin.alarms.correlation import CorrelationEngine, DependencyGraph
from homestead_twin.alarms.definitions import sync_definitions
from homestead_twin.alarms.evaluator import AlarmEvaluator, SuppressionReason
from homestead_twin.alarms.notify import Notifier, build_channels
from homestead_twin.alarms.service import AlarmEngineService
from homestead_twin.config import DATA_DIR
from homestead_twin.models.alarms import Alarm, AlarmDefinition, Incident, NotificationLog
from homestead_twin.models.registry import (
    Asset,
    AssetClass,
    AssetRelationship,
    Point,
    PointDefinition,
)
from homestead_twin.models.telemetry import CurrentState

T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.timezone.utc)


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# The real design package as the test registry
# ---------------------------------------------------------------------------


def load_real_registry(session) -> dict:
    """Load the shipped asset register and point dictionary into the database.

    Correlation is only meaningful against the real topology, so this uses the
    actual v0.3 package rather than a hand-built toy graph.
    """
    register = yaml.safe_load((DATA_DIR / "homestead_asset_register.yaml").read_text())
    points = yaml.safe_load((DATA_DIR / "point_dictionary.yaml").read_text())["points"]

    classes = {a["asset_class"] for a in register["assets"]}
    for name in sorted(classes):
        session.add(AssetClass(name=name, allowed_domains=[]))
    for name, spec in points.items():
        session.add(
            PointDefinition(
                name=name,
                default_class=spec["default_class"],
                data_type=spec["data_type"],
                unit=spec.get("unit"),
                enum_values=spec.get("enum_values"),
            )
        )
    session.flush()

    for entry in register["assets"]:
        session.add(
            Asset(
                asset_id=entry["asset_id"],
                domain=entry["domain"],
                asset_class=entry["asset_class"],
                name=entry["name"],
                status=entry["status"],
                criticality=entry["criticality"],
                control_authority=entry["control_authority"],
                parent_id=entry.get("parent_id"),
                properties=entry.get("properties") or {},
                manual_override=entry.get("manual_override") or {},
                dependencies=entry.get("dependencies") or [],
                open_fields=entry.get("open_fields") or [],
            )
        )
    session.flush()
    for relation in register["relationships"]:
        session.add(
            AssetRelationship(
                relationship_id=relation["relationship_id"],
                from_asset_id=relation["from_asset_id"],
                relationship_type=relation["relationship_type"],
                to_asset_id=relation["to_asset_id"],
                status=relation.get("status", "planned"),
            )
        )
    session.flush()
    return register


def set_state(session, asset_id, point_name, value, *, quality="good", ts=T0, unit=None):
    point_id = f"{asset_id}/{point_name}"
    if session.get(Point, point_id) is None:
        session.add(
            Point(
                point_id=point_id,
                asset_id=asset_id,
                point_name=point_name,
                point_class="AI",
                data_type="boolean" if isinstance(value, bool) else "float",
                unit=unit,
            )
        )
    row = session.get(CurrentState, point_id)
    if row is None:
        row = CurrentState(point_id=point_id, asset_id=asset_id, point_name=point_name)
        session.add(row)
    row.value_numeric = row.value_bool = row.value_text = None
    if isinstance(value, bool):
        row.value_bool = value
    elif isinstance(value, (int, float)):
        row.value_numeric = float(value)
    else:
        row.value_text = str(value)
    row.quality = quality
    row.unit = unit
    row.ts = ts
    session.flush()
    return row


AC_MAIN = "energy.disconnect.power_container.ac_main_01"
CRITICAL_PANEL = "energy.panel.power_container.critical_01"
GENERAL_PANEL = "energy.panel.power_container.general_01"
UPS = "energy.ups.rack_01.01"
RACK_ATS = "energy.ats.rack_01.ap5442_01"
SWITCH = "it.switch.rack_01.catalyst_2960x_01"
SERVER = "it.server.rack_01.r740xd_01"
SECONDARY_NODE = "it.server.secondary_control_node.01"
BEACON = "safety.alarm_output.rack_01.beacon_01"
CAMERA = "security.camera.rack_01.netbotz_01"


@pytest.fixture()
def registry(db_session):
    return load_real_registry(db_session)


@pytest.fixture()
def definitions(db_session, registry):
    return sync_definitions(db_session, strict=True)


@pytest.fixture()
def engine_parts(db_session, bus, settings, definitions):
    return (
        AlarmEvaluator(db_session, bus, settings),
        CorrelationEngine(db_session, settings),
        Notifier(db_session, settings),
    )


def open_alarms(session) -> list[Alarm]:
    return [a for a in session.query(Alarm).all() if a.state not in ("cleared", "reviewed")]


def incidents(session, state: str | None = None) -> list[Incident]:
    rows = session.query(Incident).all()
    return [i for i in rows if state is None or i.state == state]


# ---------------------------------------------------------------------------
# The dependency graph
# ---------------------------------------------------------------------------


def test_graph_walks_the_real_registry_topology(db_session, registry):
    graph = DependencyGraph.from_session(db_session)

    downstream = graph.descendants(AC_MAIN)
    # feeds: ac_main -> both panels -> UPS and load groups -> rack PDUs
    for expected in (
        CRITICAL_PANEL,
        GENERAL_PANEL,
        UPS,
        "energy.pdu.rack_01.switched_01",
        "energy.pdu.rack_01.basic_01",
        "energy.load.site.control_core_01",
        "energy.load.site.spa_01",
    ):
        assert expected in downstream, expected
    assert AC_MAIN not in downstream

    # located_in is a reverse edge: the rack's failure reaches what is in it.
    assert SERVER in graph.descendants("it.rack.power_container.01")
    # hosts is a forward edge: the host's failure reaches the services.
    assert "it.application_service.rack_01.mqtt_01" in graph.descendants(SERVER)

    assert AC_MAIN in graph.ancestors(UPS)
    # The independent node is deliberately not downstream of the power container.
    assert SECONDARY_NODE not in downstream


def test_graph_is_cycle_safe(db_session, registry):
    db_session.add(
        AssetRelationship(
            relationship_id="rel.cycle.test",
            from_asset_id=CRITICAL_PANEL,
            relationship_type="feeds",
            to_asset_id=AC_MAIN,
        )
    )
    db_session.flush()
    graph = DependencyGraph.from_session(db_session)
    assert UPS in graph.descendants(AC_MAIN)      # terminates rather than looping


# ---------------------------------------------------------------------------
# THE test: one container outage, one incident
# ---------------------------------------------------------------------------


def test_power_container_outage_produces_one_incident(db_session, engine_parts, bus):
    """SDD 14.2: a power-container outage is one incident, not a notification storm."""
    evaluator, correlator, notifier = engine_parts

    # The main AC bus drops. Everything it feeds follows.
    set_state(db_session, AC_MAIN, "energized_state", False)
    set_state(db_session, UPS, "on_battery", True)
    set_state(db_session, UPS, "runtime_remaining_min", 4.0, unit="min")
    set_state(db_session, RACK_ATS, "source_a_available", False)
    set_state(db_session, SWITCH, "availability_state", "offline")
    set_state(db_session, SERVER, "availability_state", "offline")
    set_state(db_session, CAMERA, "availability_state", "offline")
    for service in (
        "mqtt_01",
        "postgres_01",
        "digital_twin_api_01",
        "node_red_01",
        "home_assistant_01",
        "historian_01",
        "grafana_01",
        "prometheus_01",
        "opnsense_01",
        "pihole_01",
        "cucm_01",
    ):
        set_state(
            db_session, f"it.application_service.rack_01.{service}", "availability_state", "offline"
        )
    for load in ("control_core_01", "server_rack_01", "rack_cooling_01", "battery_hvac_01"):
        set_state(db_session, f"energy.load.site.{load}", "shed_state", "shed_pending")

    # Let every on-delay expire.
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(600))

    active = [a for a in open_alarms(db_session) if a.state == "active"]
    assert len(active) >= 15, f"expected a cascade, got {len(active)} alarms"

    correlator.correlate(at(600))
    notifier.dispatch_pending(at(600))

    # --- one incident -----------------------------------------------------
    open_incidents = incidents(db_session, "open")
    assert len(open_incidents) == 1, [i.title for i in open_incidents]
    incident = open_incidents[0]

    root = db_session.get(Alarm, incident.root_cause_alarm_id)
    assert root.alarm_key == "power_container_ac_bus_lost"
    assert root.asset_id == AC_MAIN
    assert incident.severity == "critical"

    members = correlator.incident_members(incident.id)
    assert len(members) == len(active)          # every cascade alarm is a member
    assert all(m.incident_id == incident.id for m in members)

    # --- every symptom is recorded, and marked as a symptom ---------------
    symptoms = [m for m in members if m.id != root.id]
    assert len(symptoms) >= 14
    for symptom in symptoms:
        assert symptom.suppressed is True
        assert SuppressionReason.kind(symptom.suppression_reason) in (
            SuppressionReason.SYMPTOM,
            SuppressionReason.FLOOD,
        )
        assert symptom.state == "active"        # suppressed means "not notified", not "dropped"

    # --- one notification, not fifteen ------------------------------------
    logs = db_session.query(NotificationLog).all()
    assert {log.incident_id for log in logs} == {incident.id}
    per_alarm = [log for log in logs if log.incident_id is None]
    assert per_alarm == []
    channels = {log.channel for log in logs}
    assert channels == {"log"}                   # only the log backend is enabled
    assert len(logs) == 1

    body = logs[0].body
    assert "power_container_ac_bus_lost" in body
    assert "OP-ENERGY-CONTAINER-OUTAGE" in body   # FR-008 procedure travels with the alert


def test_outage_incident_closes_when_every_member_clears(db_session, engine_parts):
    evaluator, correlator, notifier = engine_parts
    set_state(db_session, AC_MAIN, "energized_state", False)
    set_state(db_session, UPS, "on_battery", True)
    set_state(db_session, SERVER, "availability_state", "offline")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(600))
    correlator.correlate(at(600))
    incident = incidents(db_session, "open")[0]

    # Power comes back.
    set_state(db_session, AC_MAIN, "energized_state", True)
    set_state(db_session, UPS, "on_battery", False)
    set_state(db_session, SERVER, "availability_state", "online")
    evaluator.evaluate(at(700))
    evaluator.evaluate(at(1400))

    assert not [a for a in open_alarms(db_session)]
    correlator.correlate(at(1400))
    assert incident.state == "closed"
    assert incident.closed_at is not None


def test_independent_alerting_paths_are_never_folded_into_the_outage(db_session, engine_parts):
    """SDD 16.1: the secondary node and the local beacon exist to survive this failure."""
    evaluator, correlator, notifier = engine_parts
    set_state(db_session, AC_MAIN, "energized_state", False)
    set_state(db_session, SERVER, "availability_state", "offline")
    set_state(db_session, SECONDARY_NODE, "availability_state", "offline")
    set_state(db_session, BEACON, "availability_state", "offline")

    evaluator.evaluate(at(0))
    evaluator.evaluate(at(600))
    correlator.correlate(at(600))
    notifier.dispatch_pending(at(600))

    outage = next(
        a for a in open_alarms(db_session) if a.alarm_key == "power_container_ac_bus_lost"
    )
    secondary = next(
        a for a in open_alarms(db_session) if a.alarm_key == "secondary_control_node_unreachable"
    )
    beacon = next(
        a for a in open_alarms(db_session) if a.alarm_key == "alarm_beacon_unavailable"
    )

    assert secondary.incident_id != outage.incident_id
    assert secondary.suppressed is False
    # The beacon sits inside the rack, so it does correlate -- but it keeps its own
    # notification because the definition anchors it, not because it was hidden.
    assert beacon.alarm_key == "alarm_beacon_unavailable"

    notified_keys = {
        db_session.get(Alarm, log.alarm_id).alarm_key
        for log in db_session.query(NotificationLog).all()
        if log.alarm_id
    }
    assert "secondary_control_node_unreachable" in notified_keys


# ---------------------------------------------------------------------------
# Declared parentage
# ---------------------------------------------------------------------------


def test_declared_parent_makes_the_child_a_symptom(db_session, engine_parts):
    evaluator, correlator, _ = engine_parts
    definition = db_session.get(AlarmDefinition, "ups_runtime_low")
    assert definition.parent_alarm_key == "ups_on_battery"

    set_state(db_session, UPS, "on_battery", True)
    set_state(db_session, UPS, "runtime_remaining_min", 4.0, unit="min")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(120))
    correlator.correlate(at(120))

    runtime = next(a for a in open_alarms(db_session) if a.alarm_key == "ups_runtime_low")
    on_battery = next(a for a in open_alarms(db_session) if a.alarm_key == "ups_on_battery")
    assert runtime.incident_id == on_battery.incident_id
    assert runtime.suppressed is True
    assert "ups_on_battery" in runtime.suppression_reason
    assert on_battery.suppressed is False


def test_child_stands_alone_when_the_parent_is_not_open(db_session, engine_parts):
    evaluator, correlator, notifier = engine_parts
    set_state(db_session, UPS, "on_battery", False)
    set_state(db_session, UPS, "runtime_remaining_min", 4.0, unit="min")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(120))
    correlator.correlate(at(120))

    runtime = next(a for a in open_alarms(db_session) if a.alarm_key == "ups_runtime_low")
    assert runtime.suppressed is False
    notifier.dispatch_pending(at(120))
    assert db_session.query(NotificationLog).count() >= 1


# ---------------------------------------------------------------------------
# Flood guard
# ---------------------------------------------------------------------------


def test_flood_guard_collapses_many_alarms_of_one_definition(db_session, engine_parts):
    """Eleven services fail at once: one incident, one notification."""
    evaluator, correlator, notifier = engine_parts
    services = [
        "mqtt_01",
        "postgres_01",
        "digital_twin_api_01",
        "node_red_01",
        "home_assistant_01",
        "historian_01",
        "grafana_01",
        "prometheus_01",
        "opnsense_01",
        "pihole_01",
        "cucm_01",
    ]
    for service in services:
        set_state(
            db_session, f"it.application_service.rack_01.{service}", "availability_state", "offline"
        )
    # The host itself is fine, so there is no upstream alarm to hang them on.
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(200))

    raised = [a for a in open_alarms(db_session) if a.alarm_key == "service_unavailable"]
    assert len(raised) == len(services)

    correlator.correlate(at(200))
    notifier.dispatch_pending(at(200))

    open_incidents = incidents(db_session, "open")
    assert len(open_incidents) == 1
    assert len(correlator.incident_members(open_incidents[0].id)) == len(services)
    assert db_session.query(NotificationLog).count() == 1
    assert sum(1 for a in raised if a.suppressed) == len(services)
    assert any(
        SuppressionReason.kind(a.suppression_reason) == SuppressionReason.FLOOD for a in raised
    )


def test_two_unrelated_alarms_stay_two_incidents(db_session, engine_parts):
    """Correlation must not glue together things that are not related."""
    evaluator, correlator, notifier = engine_parts
    set_state(db_session, "safety.safety_sensor.rack_01.nbes0307_01", "smoke_active", True)
    set_state(db_session, "energy.generator.site.01", "start_failure_active", True)
    evaluator.evaluate(at(0))
    correlator.correlate(at(0))

    open_incidents = incidents(db_session, "open")
    assert len(open_incidents) == 2
    roots = {db_session.get(Alarm, i.root_cause_alarm_id).alarm_key for i in open_incidents}
    assert roots == {"rack_smoke_detected", "generator_start_failed"}

    notifier.dispatch_pending(at(0))
    assert {log.incident_id for log in db_session.query(NotificationLog).all()} == {
        i.id for i in open_incidents
    }


def test_alarms_outside_the_correlation_window_are_not_grouped(db_session, engine_parts):
    evaluator, correlator, _ = engine_parts
    set_state(db_session, AC_MAIN, "energized_state", False)
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(60))

    # The UPS drops onto battery two hours later: far outside the 1800 s window.
    late = 7200
    set_state(db_session, UPS, "on_battery", True, ts=at(late))
    evaluator.evaluate(at(late))
    evaluator.evaluate(at(late + 60))
    correlator.correlate(at(late + 60))

    outage = next(
        a for a in open_alarms(db_session) if a.alarm_key == "power_container_ac_bus_lost"
    )
    ups = next(a for a in open_alarms(db_session) if a.alarm_key == "ups_on_battery")
    assert ups.incident_id != outage.incident_id


# ---------------------------------------------------------------------------
# Notification behaviour (SDD FR-007)
# ---------------------------------------------------------------------------


def test_unconfigured_channels_record_an_honest_failure(db_session, settings, definitions, registry):
    from homestead_twin.config import Settings

    configured = Settings(
        database_url="sqlite://",
        mqtt_enabled=False,
        notification_backends="log,email,push,voice",
    )
    channels = build_channels(configured)
    assert set(channels) == {"log", "email", "push", "voice"}
    assert channels["log"].available is True
    assert all(not channels[name].available for name in ("email", "push", "voice"))

    evaluator = AlarmEvaluator(db_session, None, configured)
    notifier = Notifier(db_session, configured, channels=channels)
    set_state(db_session, "safety.safety_sensor.power_container.nbes0308_01", "leak_active", True)
    evaluator.evaluate(at(0))
    CorrelationEngine(db_session, configured).correlate(at(0))
    notifier.dispatch_pending(at(0))

    logs = db_session.query(NotificationLog).all()
    by_channel = {log.channel: log for log in logs}
    assert by_channel["log"].status == "sent"
    for name in ("email", "push", "voice"):
        record = by_channel[name]
        assert record.status == "not_configured"
        assert "No message was sent" in record.detail
        assert record.recipient is None
    assert "CUCM" in by_channel["voice"].detail       # FR-007 names the escalation path


def test_critical_alarm_escalates_then_stops_on_acknowledgement(db_session, engine_parts):
    evaluator, correlator, notifier = engine_parts
    set_state(db_session, "energy.generator.site.01", "start_failure_active", True)
    evaluator.evaluate(at(0))
    correlator.correlate(at(0))
    notifier.dispatch_pending(at(0))

    stage1 = db_session.query(NotificationLog).count()
    assert stage1 >= 1

    # Stage 2 of generator_start_failed fires at +600 s.
    notifier.dispatch_pending(at(300))
    assert db_session.query(NotificationLog).count() == stage1     # nothing new yet

    notifier.dispatch_pending(at(600))
    escalated = db_session.query(NotificationLog).count()
    assert escalated > stage1

    alarm = next(
        a for a in open_alarms(db_session) if a.alarm_key == "generator_start_failed"
    )
    evaluator.acknowledge(alarm, "ops.alice", "On my way to the generator.", at(700))
    notifier.dispatch_pending(at(3600))
    assert db_session.query(NotificationLog).count() == escalated   # escalation stopped


def test_unacknowledged_critical_alarm_is_re_notified(db_session, engine_parts):
    evaluator, correlator, notifier = engine_parts
    set_state(db_session, "energy.ats.power_container.site_01", "fault_active", True)
    evaluator.evaluate(at(0))
    correlator.correlate(at(0))
    notifier.dispatch_pending(at(0))
    first = db_session.query(NotificationLog).count()

    definition = db_session.get(AlarmDefinition, "transfer_failed")
    from homestead_twin.alarms.definitions import definition_meta

    renotify = definition_meta(definition)["renotify_after_s"]
    assert renotify > 0

    notifier.dispatch_pending(at(renotify - 1))
    assert db_session.query(NotificationLog).count() == first

    notifier.dispatch_pending(at(renotify))
    records = db_session.query(NotificationLog).all()
    assert len(records) > first
    assert any("unacknowledged" in (r.subject or "") for r in records)


def test_maintenance_suppressed_alarms_are_not_notified(db_session, engine_parts):
    from homestead_twin.models.commands import OperatingMode

    evaluator, correlator, notifier = engine_parts
    db_session.add(
        OperatingMode(scope_type="domain", scope_id="security", mode="maintenance", changed_at=T0)
    )
    db_session.flush()

    access = "security.access_controller.rack_01.ap9361_01"
    set_state(db_session, access, "door_state", "open")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(700))
    correlator.correlate(at(700))
    notifier.dispatch_pending(at(700))

    alarm = next(
        a for a in open_alarms(db_session) if a.alarm_key == "rack_door_open_extended"
    )
    assert alarm.suppressed is True
    assert alarm.state == "active"                 # still fully recorded
    assert db_session.query(NotificationLog).count() == 0


# ---------------------------------------------------------------------------
# The service wrapper
# ---------------------------------------------------------------------------


def test_service_runs_a_whole_cycle_without_sleeping(session_factory, bus, settings, db_session):
    load_real_registry(db_session)
    db_session.commit()

    service = AlarmEngineService(session_factory, bus, settings, interval_s=3600)
    assert service.name == "alarms"
    service.load_definitions()

    set_state(db_session, AC_MAIN, "energized_state", False)
    set_state(db_session, UPS, "on_battery", True)
    set_state(db_session, SERVER, "availability_state", "offline")
    db_session.commit()

    service.evaluate_once(at(0))
    result = service.evaluate_once(at(600))

    assert result.evaluation.activated
    assert result.correlation.opened or result.correlation.updated
    assert service.cycles == 2
    assert service.last_error is None

    fresh = session_factory()
    try:
        assert len([i for i in fresh.query(Incident).all() if i.state == "open"]) == 1
        assert fresh.query(NotificationLog).count() == 1
    finally:
        fresh.close()

    assert service.status()["running"] is False
