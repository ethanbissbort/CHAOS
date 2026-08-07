"""Alarm evaluation, delays, hysteresis, lifecycle, maintenance and data quality.

Every test drives ``now`` explicitly. Nothing sleeps: an alarm engine whose test
suite depends on wall-clock time cannot be trusted about a 300 s on-delay.

The tests run against the *shipped* definitions in ``data/alarm_definitions.yaml``
rather than synthetic ones, so a bad threshold or a missing reset condition in the
data fails here.
"""

from __future__ import annotations

import datetime as dt

import pytest

from homestead_twin.alarms.definitions import (
    DEFAULT_DEFINITIONS_PATH,
    definition_meta,
    load_document,
    sync_definitions,
    validate_document,
    validate_schema,
)
from homestead_twin.alarms.evaluator import (
    AlarmEvaluator,
    AlarmTransitionError,
    SuppressionReason,
    as_utc,
    derive_reset,
)
from homestead_twin.models.alarms import SEVERITIES, Alarm, AlarmDefinition
from homestead_twin.models.commands import OperatingMode
from homestead_twin.models.registry import Asset, AssetClass, Point, PointDefinition
from homestead_twin.models.telemetry import CurrentState

T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Minimal registry seeding
# ---------------------------------------------------------------------------


def seed_asset(session, asset_id: str, **kwargs) -> Asset:
    domain, asset_class, *_ = asset_id.split(".")
    asset_class = kwargs.pop("asset_class", asset_class)
    if session.get(AssetClass, asset_class) is None:
        session.add(AssetClass(name=asset_class, allowed_domains=[domain]))
    asset = Asset(
        asset_id=asset_id,
        domain=kwargs.pop("domain", domain),
        asset_class=asset_class,
        name=kwargs.pop("name", asset_id),
        **kwargs,
    )
    session.add(asset)
    session.flush()
    return asset


def seed_point(session, asset_id: str, point_name: str, *, data_type: str = "float", unit=None):
    if session.get(PointDefinition, point_name) is None:
        session.add(PointDefinition(name=point_name, default_class="AI", data_type=data_type, unit=unit))
    point_id = f"{asset_id}/{point_name}"
    if session.get(Point, point_id) is None:
        session.add(
            Point(
                point_id=point_id,
                asset_id=asset_id,
                point_name=point_name,
                point_class="AI",
                data_type=data_type,
                unit=unit,
            )
        )
    session.flush()
    return point_id


def set_state(session, asset_id, point_name, value, *, quality="good", ts=None, unit=None):
    point_id = seed_point(
        session,
        asset_id,
        point_name,
        data_type="boolean" if isinstance(value, bool) else "float",
        unit=unit,
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
    row.ts = ts or T0
    session.flush()
    return row


def set_mode(session, scope_type, scope_id, mode, *, now=T0):
    row = OperatingMode(
        scope_type=scope_type, scope_id=scope_id, mode=mode, changed_at=now, changed_by="test"
    )
    session.add(row)
    session.flush()
    return row


BATTERY = "energy.battery_bank.power_container.01"
GENERATOR = "energy.generator.site.01"
RACK_ACCESS = "security.access_controller.rack_01.ap9361_01"
LEAK_SENSOR = "safety.safety_sensor.power_container.nbes0308_01"


@pytest.fixture()
def definitions(db_session):
    """The shipped definition set, loaded and cross-referenced."""
    return sync_definitions(db_session, strict=True)


@pytest.fixture()
def evaluator(db_session, bus, settings, definitions):
    return AlarmEvaluator(db_session, bus, settings)


def open_alarms(session, alarm_key: str) -> list[Alarm]:
    return [
        a
        for a in session.query(Alarm).filter(Alarm.alarm_key == alarm_key).all()
        if a.state not in ("cleared", "reviewed")
    ]


def all_alarms(session, alarm_key: str) -> list[Alarm]:
    return session.query(Alarm).filter(Alarm.alarm_key == alarm_key).all()


# ---------------------------------------------------------------------------
# The definition set itself
# ---------------------------------------------------------------------------


def test_shipped_definitions_validate_against_schema_and_registry():
    document = load_document(DEFAULT_DEFINITIONS_PATH)
    assert validate_schema(document) == []
    assert validate_document(document) == []
    assert len(document["alarms"]) >= 30


def test_yaml_and_json_mirrors_are_equal():
    yaml_doc = load_document(DEFAULT_DEFINITIONS_PATH)
    json_doc = load_document(DEFAULT_DEFINITIONS_PATH.with_suffix(".json"))
    assert yaml_doc == json_doc


def test_every_numeric_threshold_declares_its_provenance(db_session, definitions):
    """No trip point may read as a decided setpoint (README: deliberately unresolved)."""
    numeric = [
        d for d in db_session.query(AlarmDefinition).all() if d.trigger_operator in ("lt", "le", "gt", "ge")
    ]
    assert numeric
    for definition in numeric:
        meta = definition_meta(definition)
        assert meta["threshold_status"] in (
            "commissioning_default",
            "derived_from_design_document",
        ), definition.alarm_key
        assert meta["threshold_basis"], definition.alarm_key


def test_every_definition_carries_the_sdd_14_3_fields(db_session, definitions):
    for definition in db_session.query(AlarmDefinition).all():
        key = definition.alarm_key
        assert definition.trigger_operator, key
        assert derive_reset(definition)[0], key  # reset logic
        assert definition.on_delay_s is not None, key  # delay
        assert definition.severity in SEVERITIES, key  # severity
        assert definition.probable_causes, key  # probable causes
        assert definition.automatic_action, key  # automatic protective action
        assert definition.operator_action, key  # operator action
        assert definition.procedure_ref, key  # FR-008 procedure link
        assert definition.escalation_path, key  # escalation path
        assert definition.suppression_conditions is not None, key
        assert definition.maintenance_mode_behaviour, key  # maintenance behaviour
        # Affected assets: dynamic data-quality alarms bind to a point at raise time.
        if definition.point_name:
            assert definition.affected_assets or definition.asset_id or definition.asset_class, key


def test_emergency_alarms_are_never_silenced_or_self_clearing(db_session, definitions):
    """SDD 14.1: the platform reports; it does not become the safety system."""
    emergency = db_session.query(AlarmDefinition).filter(AlarmDefinition.severity == "emergency").all()
    assert emergency
    for definition in emergency:
        assert definition.maintenance_mode_behaviour in ("notify_only", "normal")
        assert definition.requires_manual_reset is True


def test_sync_is_idempotent(db_session):
    first = sync_definitions(db_session, strict=True)
    db_session.commit()
    second = sync_definitions(db_session, strict=True)
    assert first.created and not first.updated
    assert not second.created and not second.updated
    assert len(second.unchanged) == first.total


def test_sync_rejects_a_definition_naming_an_unknown_point(db_session, tmp_path):
    import yaml

    document = load_document(DEFAULT_DEFINITIONS_PATH)
    document["alarms"][0]["point_name"] = "a_point_nobody_defined"
    broken = tmp_path / "alarm_definitions.yaml"
    broken.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(Exception) as excinfo:
        sync_definitions(db_session, path=broken, strict=True)
    assert "point dictionary" in str(excinfo.value)


# ---------------------------------------------------------------------------
# On-delay
# ---------------------------------------------------------------------------


def test_on_delay_suppresses_a_transient(db_session, evaluator):
    """A 20 s dip below the SOC threshold must not become an alarm (SDD 14.3)."""
    seed_asset(db_session, BATTERY)
    definition = db_session.get(AlarmDefinition, "battery_soc_low")
    assert definition.on_delay_s == 300

    set_state(db_session, BATTERY, "soc_pct", 38.0, unit="%")
    evaluator.evaluate(at(0))
    candidate = open_alarms(db_session, "battery_soc_low")
    assert len(candidate) == 1
    assert candidate[0].state == "detected"  # recorded, not active
    assert candidate[0].activated_at is None
    assert candidate[0].notified is False

    # Still inside the on-delay window.
    evaluator.evaluate(at(120))
    assert open_alarms(db_session, "battery_soc_low")[0].state == "detected"

    # SOC recovers before the on-delay expires.
    set_state(db_session, BATTERY, "soc_pct", 55.0, unit="%")
    result = evaluator.evaluate(at(200))

    assert not open_alarms(db_session, "battery_soc_low")
    assert len(result.transient) == 1
    history = all_alarms(db_session, "battery_soc_low")
    assert len(history) == 1  # never silently dropped
    assert history[0].state == "cleared"
    assert history[0].activated_at is None  # never reached active
    states = [e.to_state for e in history[0].events]
    assert states == ["detected", "cleared"]
    assert "transient" in history[0].events[-1].note


def test_alarm_activates_once_the_on_delay_expires(db_session, evaluator):
    seed_asset(db_session, BATTERY)
    set_state(db_session, BATTERY, "soc_pct", 38.0, unit="%")

    evaluator.evaluate(at(0))
    evaluator.evaluate(at(299))
    assert open_alarms(db_session, "battery_soc_low")[0].state == "detected"

    evaluator.evaluate(at(300))
    alarm = open_alarms(db_session, "battery_soc_low")[0]
    assert alarm.state == "active"
    assert as_utc(alarm.activated_at) == at(300)
    assert [e.to_state for e in alarm.events] == ["detected", "active"]


# ---------------------------------------------------------------------------
# Hysteresis
# ---------------------------------------------------------------------------


def test_hysteresis_prevents_chatter(db_session, evaluator):
    """Trigger at <40 %, reset at >=45 %: values inside the deadband hold the alarm."""
    seed_asset(db_session, BATTERY)
    definition = db_session.get(AlarmDefinition, "battery_soc_low")
    assert definition.trigger_value["value"] == 40
    assert definition.reset_value["value"] == 45

    set_state(db_session, BATTERY, "soc_pct", 38.0, unit="%")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(300))
    assert open_alarms(db_session, "battery_soc_low")[0].state == "active"

    # Oscillate across the trip point but stay inside the deadband.
    for index, (value, moment) in enumerate(
        [(42.0, 400), (38.5, 500), (43.0, 600), (39.0, 700), (44.9, 800)]
    ):
        set_state(db_session, BATTERY, "soc_pct", value, unit="%")
        evaluator.evaluate(at(moment))
        alarms = open_alarms(db_session, "battery_soc_low")
        assert len(alarms) == 1, f"step {index} produced {len(alarms)} alarms"
        assert alarms[0].state == "active"

    assert len(all_alarms(db_session, "battery_soc_low")) == 1  # exactly one alarm, no chatter

    # Cross the reset threshold: still needs the off-delay.
    set_state(db_session, BATTERY, "soc_pct", 46.0, unit="%")
    evaluator.evaluate(at(900))
    assert open_alarms(db_session, "battery_soc_low")[0].state == "active"

    evaluator.evaluate(at(900 + definition.off_delay_s))
    assert not open_alarms(db_session, "battery_soc_low")
    assert all_alarms(db_session, "battery_soc_low")[0].state == "cleared"


def test_off_delay_restarts_when_the_condition_returns(db_session, evaluator):
    seed_asset(db_session, BATTERY)
    set_state(db_session, BATTERY, "soc_pct", 38.0, unit="%")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(300))

    set_state(db_session, BATTERY, "soc_pct", 46.0, unit="%")  # reset condition starts
    evaluator.evaluate(at(400))
    set_state(db_session, BATTERY, "soc_pct", 38.0, unit="%")  # ... and is interrupted
    evaluator.evaluate(at(500))
    set_state(db_session, BATTERY, "soc_pct", 46.0, unit="%")
    evaluator.evaluate(at(600))

    # 600 s of reset has not accumulated since the interruption.
    evaluator.evaluate(at(1100))
    assert open_alarms(db_session, "battery_soc_low")[0].state == "active"
    evaluator.evaluate(at(1201))
    assert not open_alarms(db_session, "battery_soc_low")


def test_derived_reset_uses_hysteresis_when_no_reset_is_declared(db_session, definitions):
    definition = db_session.get(AlarmDefinition, "battery_soc_low")
    definition.reset_operator = None
    definition.reset_value = None
    operator, payload = derive_reset(definition)
    assert operator == "ge"
    assert payload["value"] == 45.0  # 40 trigger + 5 hysteresis


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_full_lifecycle_transitions_are_recorded(db_session, evaluator):
    seed_asset(db_session, BATTERY)
    set_state(db_session, BATTERY, "soc_pct", 38.0, unit="%")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(300))
    alarm = open_alarms(db_session, "battery_soc_low")[0]

    evaluator.acknowledge(alarm, "ops.alice", "Seen; checking the forecast.", at(400))
    assert alarm.state == "acknowledged" and alarm.acknowledged_by == "ops.alice"

    evaluator.mitigate(alarm, "ops.alice", "Shed the workshop circuit by hand.", at(500))
    assert alarm.state == "mitigated"

    set_state(db_session, BATTERY, "soc_pct", 60.0, unit="%")
    evaluator.evaluate(at(600))
    evaluator.evaluate(at(1300))
    assert alarm.state == "cleared"

    evaluator.review(alarm, "ops.bob", "Root cause: three cloudy days, no generator.", at(2000))
    assert alarm.state == "reviewed" and alarm.reviewed_by == "ops.bob"

    assert [e.to_state for e in alarm.events] == [
        "detected",
        "active",
        "acknowledged",
        "mitigated",
        "cleared",
        "reviewed",
    ]
    assert all(e.occurred_at is not None for e in alarm.events)
    assert alarm.events[2].actor == "ops.alice" and alarm.events[2].note


def test_lifecycle_is_one_way_and_requires_a_note(db_session, evaluator):
    seed_asset(db_session, BATTERY)
    set_state(db_session, BATTERY, "soc_pct", 38.0, unit="%")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(300))
    alarm = open_alarms(db_session, "battery_soc_low")[0]

    with pytest.raises(AlarmTransitionError):
        evaluator.acknowledge(alarm, "ops.alice", "   ", at(400))

    evaluator.acknowledge(alarm, "ops.alice", "ack", at(400))
    with pytest.raises(AlarmTransitionError):
        evaluator.acknowledge(alarm, "ops.alice", "again", at(500))

    evaluator.mitigate(alarm, "ops.alice", "did a thing", at(500))
    with pytest.raises(AlarmTransitionError):
        evaluator.acknowledge(alarm, "ops.alice", "backwards", at(600))

    with pytest.raises(AlarmTransitionError):
        evaluator.review(alarm, "ops.alice", "not cleared yet", at(700))


def test_manual_clear_is_refused_while_the_condition_persists(db_session, evaluator):
    seed_asset(db_session, BATTERY)
    set_state(db_session, BATTERY, "soc_pct", 30.0, unit="%")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(300))
    alarm = open_alarms(db_session, "battery_soc_low")[0]

    with pytest.raises(AlarmTransitionError, match="still true"):
        evaluator.clear(alarm, "ops.alice", "looks fine to me", at(400))

    evaluator.clear(alarm, "ops.alice", "known test discharge", at(400), force=True)
    assert alarm.state == "cleared"
    assert "operator override" in alarm.events[-1].note


# ---------------------------------------------------------------------------
# Manual reset
# ---------------------------------------------------------------------------


def test_manual_reset_alarm_does_not_self_clear(db_session, evaluator):
    """SDD 34.7: a generator start failure requires an explicit reset."""
    seed_asset(db_session, GENERATOR)
    definition = db_session.get(AlarmDefinition, "generator_start_failed")
    assert definition.requires_manual_reset is True

    set_state(db_session, GENERATOR, "start_failure_active", True)
    evaluator.evaluate(at(0))
    alarm = open_alarms(db_session, "generator_start_failed")[0]
    assert alarm.state == "active"  # on_delay 0: critical, no delay

    # The controller drops the flag on its own. The alarm must stay open.
    set_state(db_session, GENERATOR, "start_failure_active", False)
    for moment in (60, 600, 3600, 86_400):
        result = evaluator.evaluate(at(moment))
        assert alarm.state == "active", f"self-cleared at +{moment}s"
    assert result.held_manual_reset

    evaluator.clear(alarm, "ops.alice", "Attended, flat start battery replaced.", at(90_000))
    assert alarm.state == "cleared"
    assert alarm.events[-1].actor == "ops.alice"


def test_emergency_alarm_holds_until_an_operator_clears_it(db_session, evaluator):
    seed_asset(db_session, LEAK_SENSOR)
    set_state(db_session, LEAK_SENSOR, "leak_active", True)
    evaluator.evaluate(at(0))
    alarm = open_alarms(db_session, "power_container_water_ingress")[0]
    assert alarm.severity == "emergency" and alarm.state == "active"

    set_state(db_session, LEAK_SENSOR, "leak_active", False)
    evaluator.evaluate(at(7200))
    assert alarm.state == "active"


# ---------------------------------------------------------------------------
# Maintenance mode (SDD 11)
# ---------------------------------------------------------------------------


def test_maintenance_suppression_still_records_the_alarm(db_session, evaluator):
    seed_asset(db_session, RACK_ACCESS)
    definition = db_session.get(AlarmDefinition, "rack_door_open_extended")
    assert definition.maintenance_mode_behaviour == "suppress"
    set_mode(db_session, "asset", RACK_ACCESS, "maintenance")

    set_state(db_session, RACK_ACCESS, "door_state", "open")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(definition.on_delay_s))

    alarms = open_alarms(db_session, "rack_door_open_extended")
    assert len(alarms) == 1  # evaluated and stored, not dropped
    alarm = alarms[0]
    assert alarm.state == "active"  # the lifecycle still runs
    assert alarm.suppressed is True
    assert SuppressionReason.kind(alarm.suppression_reason) == SuppressionReason.MAINTENANCE
    assert "maintenance" in alarm.suppression_reason
    assert any("maintenance" in (e.note or "") for e in alarm.events)


def test_maintenance_downgrade_lowers_severity_but_keeps_the_alarm(db_session, evaluator):
    zone = "structure.room.power_container.server_zone"
    seed_asset(db_session, zone, asset_class="room", domain="structure")
    definition = db_session.get(AlarmDefinition, "server_zone_temperature_high")
    assert definition.maintenance_mode_behaviour == "downgrade"
    set_mode(db_session, "asset", zone, "maintenance")

    set_state(db_session, zone, "temperature_air_c", 36.0, unit="degC")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(definition.on_delay_s))
    alarm = open_alarms(db_session, "server_zone_temperature_high")[0]

    assert definition.severity == "major"
    assert alarm.severity == "warning"  # downgraded one step
    assert alarm.suppressed is False  # still notifies
    assert any("downgraded" in (e.note or "").lower() for e in alarm.events)


def test_maintenance_does_not_silence_an_emergency_alarm(db_session, evaluator):
    seed_asset(db_session, LEAK_SENSOR)
    set_mode(db_session, "asset", LEAK_SENSOR, "maintenance")
    set_state(db_session, LEAK_SENSOR, "leak_active", True)
    evaluator.evaluate(at(0))
    alarm = open_alarms(db_session, "power_container_water_ingress")[0]
    assert alarm.suppressed is False
    assert alarm.severity == "emergency"


def test_domain_maintenance_mode_applies_when_the_asset_has_none(db_session, evaluator):
    seed_asset(db_session, RACK_ACCESS)
    set_mode(db_session, "domain", "security", "maintenance")
    set_state(db_session, RACK_ACCESS, "door_state", "open")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(600))
    alarm = open_alarms(db_session, "rack_door_open_extended")[0]
    assert alarm.suppressed is True
    assert "domain security" in alarm.suppression_reason


# ---------------------------------------------------------------------------
# Measurement quality (SDD 26.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("quality", ["bad", "stale"])
def test_bad_quality_input_does_not_trigger_a_process_alarm(db_session, evaluator, quality):
    """A dead SOC sensor reading 0 % is a dead sensor, not an empty battery."""
    seed_asset(db_session, BATTERY)
    set_state(db_session, BATTERY, "soc_pct", 0.0, quality=quality, unit="%")

    result = evaluator.evaluate(at(0))
    evaluator.evaluate(at(600))

    assert not all_alarms(db_session, "battery_soc_low")  # process alarm never raised
    assert f"{BATTERY}/soc_pct" in result.quality_blocked

    quality_alarms = open_alarms(db_session, "energy_meter_data_invalid")
    assert len(quality_alarms) == 1
    raised = quality_alarms[0]
    assert raised.point_id == f"{BATTERY}/soc_pct"
    assert raised.state == "active"
    assert quality in (raised.message or "")


def test_quality_alarm_clears_and_the_process_alarm_then_evaluates(db_session, evaluator):
    seed_asset(db_session, BATTERY)
    set_state(db_session, BATTERY, "soc_pct", 0.0, quality="bad", unit="%")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(300))
    assert open_alarms(db_session, "energy_meter_data_invalid")

    set_state(db_session, BATTERY, "soc_pct", 38.0, quality="good", unit="%")
    evaluator.evaluate(at(600))
    evaluator.evaluate(at(800))
    assert not open_alarms(db_session, "energy_meter_data_invalid")

    evaluator.evaluate(at(1000))
    assert open_alarms(db_session, "battery_soc_low")[0].state == "active"


def test_a_pending_candidate_is_not_confirmed_on_bad_data(db_session, evaluator):
    seed_asset(db_session, BATTERY)
    set_state(db_session, BATTERY, "soc_pct", 38.0, unit="%")
    evaluator.evaluate(at(0))
    assert open_alarms(db_session, "battery_soc_low")[0].state == "detected"

    set_state(db_session, BATTERY, "soc_pct", 38.0, quality="bad", unit="%")
    evaluator.evaluate(at(400))

    assert not open_alarms(db_session, "battery_soc_low")
    closed = all_alarms(db_session, "battery_soc_low")[0]
    assert closed.state == "cleared" and closed.activated_at is None
    assert "quality" in closed.events[-1].note
    assert open_alarms(db_session, "energy_meter_data_invalid")


def test_missing_telemetry_is_not_a_healthy_reading(db_session, evaluator):
    """An uncommissioned binding produces no alarm and no false all-clear."""
    seed_asset(db_session, BATTERY)
    result = evaluator.evaluate(at(0))
    assert result.skipped_no_data > 0
    assert db_session.query(Alarm).count() == 0


def test_a_guard_with_untrustworthy_data_fails_closed(db_session, evaluator):
    """pv_generation_underperformance is gated on irradiance; bad irradiance blocks it."""
    array = "energy.pv_array.agrivoltaic_field.01"
    field_zone = "site.geographic_zone.agrivoltaic_field.01"
    seed_asset(db_session, array)
    seed_asset(db_session, field_zone, asset_class="geographic_zone", domain="site")

    set_state(db_session, array, "power_dc_kw", 1.0, unit="kW")
    set_state(db_session, field_zone, "solar_irradiance_w_m2", 900.0, quality="bad")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(1000))
    assert not open_alarms(db_session, "pv_generation_underperformance")

    set_state(db_session, field_zone, "solar_irradiance_w_m2", 900.0, quality="good")
    evaluator.evaluate(at(2000))
    evaluator.evaluate(at(3000))
    assert open_alarms(db_session, "pv_generation_underperformance")[0].state == "active"


def test_guard_gates_the_trigger_on_live_load(db_session, evaluator):
    """battery_discharge_inhibited only fires when there is still AC load to serve."""
    bms = "energy.bms.power_container.01"
    combiner = "energy.combiner.power_container.ac_01"
    seed_asset(db_session, bms)
    seed_asset(db_session, combiner)

    set_state(db_session, bms, "discharge_permissive", False)
    set_state(db_session, combiner, "power_ac_kw", 0.0, unit="kW")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(100))
    assert not open_alarms(db_session, "battery_discharge_inhibited")

    set_state(db_session, combiner, "power_ac_kw", 4.0, unit="kW")
    evaluator.evaluate(at(200))
    evaluator.evaluate(at(300))
    assert open_alarms(db_session, "battery_discharge_inhibited")[0].state == "active"


# ---------------------------------------------------------------------------
# Scope resolution and publication
# ---------------------------------------------------------------------------


def test_class_scoped_definition_instantiates_per_asset(db_session, evaluator):
    for index in ("01", "02", "03", "04"):
        seed_asset(db_session, f"energy.inverter.power_container.{index}")
    definition = db_session.get(AlarmDefinition, "inverter_fault")
    assert definition.asset_class == "inverter" and definition.asset_id is None
    assert len(evaluator.resolve_targets(definition)) == 4

    set_state(db_session, "energy.inverter.power_container.02", "fault_active", True)
    set_state(db_session, "energy.inverter.power_container.03", "fault_active", False)
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(60))

    alarms = open_alarms(db_session, "inverter_fault")
    assert len(alarms) == 1
    assert alarms[0].asset_id == "energy.inverter.power_container.02"


def test_state_changes_are_published_to_the_alarm_topic(db_session, evaluator, bus):
    seed_asset(db_session, BATTERY)
    set_state(db_session, BATTERY, "soc_pct", 38.0, unit="%")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(300))

    published = [m for m in bus.published if "/alarm/" in m.topic]
    assert published
    topic = published[-1].topic
    assert topic == "homestead/energy/power_container/battery_bank_01/alarm/battery_soc_low"

    import json

    payload = json.loads(published[-1].text)
    assert payload["asset_id"] == BATTERY
    assert payload["event"] == "battery_soc_low"
    assert payload["detail"]["state"] == "active"
    assert payload["detail"]["severity"] == "warning"


def test_a_dead_bus_does_not_stop_evaluation(db_session, settings, definitions):
    class BrokenBus:
        def publish(self, *args, **kwargs):
            raise RuntimeError("broker unreachable")

    evaluator = AlarmEvaluator(db_session, BrokenBus(), settings)
    seed_asset(db_session, BATTERY)
    set_state(db_session, BATTERY, "soc_pct", 38.0, unit="%")
    evaluator.evaluate(at(0))
    evaluator.evaluate(at(300))
    assert open_alarms(db_session, "battery_soc_low")[0].state == "active"
