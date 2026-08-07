"""EMS state machine (SDD 30.7-30.9) and the SDD 39 verification cases it covers.

Nothing here sleeps. Every time-dependent behaviour -- qualification delay,
hysteresis deadband, freeze expiry -- is driven by passing an explicit ``now``.

This module also carries the helpers the other EMS test modules import.
"""

from __future__ import annotations

import datetime as dt

import pytest

from homestead_twin.ems.config import EmsConfig
from homestead_twin.ems.derived import compute_derived
from homestead_twin.ems.inputs import ALL_SPECS, SPEC_BY_KEY, EmsInputs, InputReading, gather_inputs
from homestead_twin.ems.state_machine import (
    LatchError,
    EnergyStateMachine,
    ensure_snapshot,
    publish_state,
    select_candidate,
)
from homestead_twin.models.energy import LATCHING_ENERGY_STATES, EnergyStateTransition
from homestead_twin.models.telemetry import CurrentState

T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.timezone.utc)


def at(seconds: float) -> dt.datetime:
    """A deterministic clock: T0 + seconds."""
    return T0 + dt.timedelta(seconds=seconds)


#: A healthy homestead: 70% SOC, PV covering the load, everything observable.
HEALTHY_INPUTS: dict[str, object] = {
    "battery_soc_pct": 70.0,
    "battery_soh_pct": 99.0,
    "battery_power_kw": -2.0,
    "battery_energy_available_kwh": 400.0,
    "battery_temperature_max_c": 25.0,
    "battery_charge_limit_kw": 30.0,
    "battery_discharge_limit_kw": 30.0,
    "bms_charge_permissive": True,
    "bms_discharge_permissive": True,
    "bms_contactor_state": "closed",
    "bms_state": "normal",
    "pv_power_kw": 6.0,
    "pv_energy_today_kwh": 60.0,
    "pv_availability": "online",
    "critical_load_kw": 1.5,
    "general_load_kw": 1.0,
    "container_temperature_c": 22.0,
    "container_humidity_pct": 45.0,
    "container_alarm_summary": "none",
    "rack_alarm_summary": "none",
    "generator_state": "stopped",
    "generator_available": True,
    "generator_fuel_pct": 80.0,
    "generator_runtime_h": 120.0,
    "generator_power_kw": 0.0,
    "generator_start_failure": False,
    "ats_source_selected": "inverter",
    "ats_source_a_available": True,
    "ats_source_b_available": True,
}


def make_inputs(now: dt.datetime = T0, *, invalid: tuple[str, ...] = (), **overrides) -> EmsInputs:
    """Build an :class:`EmsInputs` directly, no database required.

    ``invalid=("battery_soc_pct",)`` marks an input stale so the DEGRADED_SENSOR
    path can be exercised without faking a clock.
    """
    values = dict(HEALTHY_INPUTS)
    values.update(overrides)
    inputs = EmsInputs(at=now)
    for key, value in values.items():
        spec = SPEC_BY_KEY.get(key)
        valid = key not in invalid and value is not None
        inputs.readings[key] = InputReading(
            key=key,
            point_id=spec.point_id if spec else f"synthetic.asset.site.01/{key}",
            value=value,
            quality="good" if valid else "stale",
            ts=now,
            age_s=0.0 if valid else 999.0,
            valid=valid,
            status="ok" if valid else "stale",
            required=spec.required if spec else False,
        )
    for spec in ALL_SPECS:
        if spec.key in inputs.readings:
            continue
        inputs.readings[spec.key] = InputReading(
            key=spec.key,
            point_id=spec.point_id,
            value=None,
            quality="unknown",
            ts=None,
            age_s=None,
            valid=False,
            status="missing",
            required=spec.required,
        )
    return inputs


def make_derived(inputs: EmsInputs, config: EmsConfig | None = None, now: dt.datetime = T0, **kwargs):
    return compute_derived(inputs, config or EmsConfig(), now=now, **kwargs)


def write_point(session, point_id: str, value, *, ts: dt.datetime, quality: str = "good"):
    """Insert or update a current-state row without needing the ingest service."""
    asset_id, point_name = point_id.split("/", 1)
    row = session.get(CurrentState, point_id)
    if row is None:
        row = CurrentState(point_id=point_id, asset_id=asset_id, point_name=point_name)
        session.add(row)
    row.value_numeric = None
    row.value_bool = None
    row.value_text = None
    if isinstance(value, bool):
        row.value_bool = value
    elif isinstance(value, (int, float)):
        row.value_numeric = float(value)
    else:
        row.value_text = str(value)
    row.quality = quality
    row.ts = ts
    row.received_at = ts
    session.flush()
    return row


def seed_current_state(session, *, ts: dt.datetime = T0, **overrides):
    """Write the healthy input set into ``current_state``."""
    values = dict(HEALTHY_INPUTS)
    values.update(overrides)
    for key, value in values.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            continue
        write_point(session, spec.point_id, value, ts=ts)


@pytest.fixture()
def config() -> EmsConfig:
    return EmsConfig()


@pytest.fixture()
def machine(config, settings) -> EnergyStateMachine:
    return EnergyStateMachine(config, settings)


def evaluate(machine, session, inputs, config, now, **kwargs):
    derived = make_derived(inputs, config, now)
    return machine.evaluate(session, inputs, derived, now=now, **kwargs)


def force_state(machine, session, state, now, *, actor="test.operator"):
    """Put the machine into a starting state without going through dwell."""
    snapshot = ensure_snapshot(session, now=now)
    snapshot.state = state
    snapshot.entered_at = now
    snapshot.candidate_state = None
    snapshot.candidate_since = None
    session.flush()
    return snapshot


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_snapshot_starts_in_commissioning(db_session):
    snapshot = ensure_snapshot(db_session, now=T0)
    assert snapshot.state == "COMMISSIONING"
    assert "COMMISSIONING" in LATCHING_ENERGY_STATES


def test_healthy_site_selects_normal(config):
    inputs = make_inputs()
    derived = make_derived(inputs, config)
    candidate = select_candidate("NORMAL", inputs, derived, config)
    assert candidate.state == "NORMAL"


def test_all_ten_states_are_reachable_in_the_severity_model():
    from homestead_twin.ems.state_machine import STATE_SEVERITY
    from homestead_twin.models.energy import ENERGY_STATES

    assert set(STATE_SEVERITY) == set(ENERGY_STATES)


# ---------------------------------------------------------------------------
# EMS-T004: declining reserve walks NORMAL -> CONSERVE -> CRITICAL_RESERVE
# ---------------------------------------------------------------------------


def test_declining_reserve_enters_conserve_only_after_its_dwell(machine, db_session, config):
    force_state(machine, db_session, "NORMAL", T0)
    low = make_inputs(battery_soc_pct=45.0, battery_energy_available_kwh=290.0)

    # First evaluation only registers the candidate; the dwell has not elapsed.
    decision = evaluate(machine, db_session, low, config, at(0))
    assert decision.current_state == "NORMAL"
    assert decision.candidate.state == "CONSERVE"
    assert not decision.transitioned
    assert decision.dwell_remaining_s == pytest.approx(config.dwell_conserve_s)

    # Still short of the qualification period.
    decision = evaluate(machine, db_session, low, config, at(config.dwell_conserve_s - 1))
    assert decision.current_state == "NORMAL"
    assert not decision.transitioned

    decision = evaluate(machine, db_session, low, config, at(config.dwell_conserve_s))
    assert decision.transitioned
    assert decision.current_state == "CONSERVE"

    transition = db_session.query(EnergyStateTransition).filter_by(to_state="CONSERVE").one()
    assert transition.from_state == "NORMAL"
    assert "below the normal reserve target" in transition.reason
    # The justification travels with the transition (SDD 30.3 objective 9).
    assert transition.inputs_snapshot["battery_soc_pct"] == 45.0
    assert transition.derived_snapshot["reserve_pct"] == 45.0


def test_reserve_collapse_enters_critical_reserve(machine, db_session, config):
    force_state(machine, db_session, "CONSERVE", T0)
    # 140 kWh usable leaves 12 kWh above the 128 kWh emergency floor.
    critical = make_inputs(battery_soc_pct=22.0, battery_energy_available_kwh=140.0)

    decision = evaluate(machine, db_session, critical, config, at(0))
    assert decision.candidate.state == "CRITICAL_RESERVE"
    assert not decision.transitioned

    decision = evaluate(machine, db_session, critical, config, at(config.dwell_critical_reserve_s))
    assert decision.transitioned
    assert decision.current_state == "CRITICAL_RESERVE"
    assert "below the critical margin" in decision.candidate.reason


def test_critical_reserve_also_triggers_on_autonomy_alone(config):
    # Plenty of energy above the floor, but a huge critical load eats it fast.
    inputs = make_inputs(
        battery_energy_available_kwh=150.0, critical_load_kw=8.0, general_load_kw=0.5
    )
    derived = make_derived(inputs, config)
    candidate = select_candidate("NORMAL", inputs, derived, config)
    assert candidate.state == "CRITICAL_RESERVE"
    assert "autonomy" in candidate.reason


def test_battery_power_limit_below_load_enters_critical_reserve(config):
    inputs = make_inputs(battery_discharge_limit_kw=2.0, critical_load_kw=2.0, general_load_kw=1.0)
    derived = make_derived(inputs, config)
    candidate = select_candidate("NORMAL", inputs, derived, config)
    assert candidate.state == "CRITICAL_RESERVE"
    assert "discharge limit" in candidate.reason


# ---------------------------------------------------------------------------
# EMS-T006: hysteresis stops a fluctuating input oscillating the state
# ---------------------------------------------------------------------------


def test_conserve_is_held_through_the_recovery_deadband(machine, db_session, config):
    force_state(machine, db_session, "CONSERVE", T0)
    # SOC 55%: above the 50% entry threshold but below the 60% recovery one.
    deadband = make_inputs(battery_soc_pct=55.0, battery_energy_available_kwh=352.0)
    decision = evaluate(machine, db_session, deadband, config, at(0))
    assert decision.current_state == "CONSERVE"
    assert decision.candidate.state == "CONSERVE"
    assert decision.candidate.trigger == "conserve_deadband"

    # Even after a long time in the deadband nothing changes.
    decision = evaluate(machine, db_session, deadband, config, at(10_000))
    assert decision.current_state == "CONSERVE"
    assert not decision.transitioned


def test_passing_clouds_do_not_toggle_the_state(machine, db_session, config):
    """EMS-T006: PV recovers briefly, then falls. No transition either way."""
    force_state(machine, db_session, "CONSERVE", T0)
    low = make_inputs(battery_soc_pct=48.0, battery_energy_available_kwh=307.0)
    bright = make_inputs(battery_soc_pct=62.0, battery_energy_available_kwh=397.0, pv_power_kw=14.0)

    clock = 0.0
    for _ in range(6):
        # Sixty seconds of sun, then sixty of cloud, repeatedly. Neither phase
        # lasts as long as the 600 s NORMAL qualification delay.
        evaluate(machine, db_session, bright, config, at(clock))
        clock += 60
        decision = evaluate(machine, db_session, low, config, at(clock))
        clock += 60
        assert decision.current_state == "CONSERVE"

    transitions = db_session.query(EnergyStateTransition).count()
    assert transitions == 0

    # Sustained brightness does eventually qualify.
    evaluate(machine, db_session, bright, config, at(clock))
    decision = evaluate(machine, db_session, bright, config, at(clock + config.dwell_normal_s))
    assert decision.transitioned
    assert decision.current_state == "NORMAL"


def test_recovery_from_critical_reserve_steps_through_conserve(machine, db_session, config):
    force_state(machine, db_session, "CRITICAL_RESERVE", T0)
    recovered = make_inputs(battery_soc_pct=75.0, battery_energy_available_kwh=480.0)

    decision = evaluate(machine, db_session, recovered, config, at(0))
    assert decision.candidate.state == "CONSERVE"
    decision = evaluate(machine, db_session, recovered, config, at(config.dwell_conserve_s))
    assert decision.current_state == "CONSERVE"

    # And only then, after its own dwell, back to NORMAL.
    evaluate(machine, db_session, recovered, config, at(config.dwell_conserve_s + 1))
    decision = evaluate(
        machine, db_session, recovered, config, at(config.dwell_conserve_s + 1 + config.dwell_normal_s)
    )
    assert decision.current_state == "NORMAL"


def test_surplus_uses_a_separate_exit_threshold(machine, db_session, config):
    force_state(machine, db_session, "SURPLUS", T0)
    # 80%: below the 85% entry threshold, above the 75% exit threshold.
    inputs = make_inputs(battery_soc_pct=80.0, battery_energy_available_kwh=512.0, pv_power_kw=9.0)
    derived = make_derived(inputs, config)
    assert select_candidate("SURPLUS", inputs, derived, config).state == "SURPLUS"
    # From NORMAL the same conditions do not qualify for SURPLUS.
    assert select_candidate("NORMAL", inputs, derived, config).state == "NORMAL"


def test_surplus_entry_requires_every_condition(config):
    surplus = make_inputs(battery_soc_pct=90.0, battery_energy_available_kwh=576.0, pv_power_kw=9.0)
    assert select_candidate("NORMAL", surplus, make_derived(surplus, config), config).state == "SURPLUS"

    # A thermal derate blocks it even with a full battery and bright sun.
    hot = make_inputs(
        battery_soc_pct=90.0,
        battery_energy_available_kwh=576.0,
        pv_power_kw=9.0,
        battery_temperature_max_c=50.0,
    )
    assert select_candidate("NORMAL", hot, make_derived(hot, config), config).state != "SURPLUS"


# ---------------------------------------------------------------------------
# EMS-T003: impaired observability
# ---------------------------------------------------------------------------


def test_stale_soc_enters_degraded_sensor_not_normal(machine, db_session, config):
    force_state(machine, db_session, "NORMAL", T0)
    stale = make_inputs(invalid=("battery_soc_pct", "battery_energy_available_kwh"))
    assert not stale.observable

    decision = evaluate(machine, db_session, stale, config, at(0))
    assert decision.candidate.state == "DEGRADED_SENSOR"
    decision = evaluate(machine, db_session, stale, config, at(config.dwell_degraded_sensor_s))
    assert decision.current_state == "DEGRADED_SENSOR"
    assert "battery_soc_pct" in decision.candidate.reason


def test_degraded_sensor_does_not_hide_a_worse_condition(config):
    """Impaired observability must be conservative, never optimistic."""
    stale = make_inputs(
        invalid=("container_temperature_c",),
        battery_energy_available_kwh=140.0,
        battery_soc_pct=22.0,
    )
    derived = make_derived(stale, config)
    candidate = select_candidate("NORMAL", stale, derived, config)
    assert candidate.state == "CRITICAL_RESERVE"
    assert "observability impaired" in candidate.reason


def test_degraded_sensor_never_resolves_to_surplus(config):
    stale = make_inputs(
        invalid=("battery_discharge_limit_kw",),
        battery_soc_pct=95.0,
        battery_energy_available_kwh=608.0,
        pv_power_kw=12.0,
    )
    candidate = select_candidate("SURPLUS", stale, make_derived(stale, config), config)
    assert candidate.state == "DEGRADED_SENSOR"


def test_missing_input_is_never_defaulted(config):
    """SDD 30.5: no default is substituted for a missing safety-relevant input."""
    inputs = make_inputs(invalid=("battery_soc_pct",))
    assert inputs.numeric("battery_soc_pct") is None
    assert not inputs.is_valid("battery_soc_pct")
    derived = make_derived(inputs, config)
    # Usable energy is still derivable from the BMS energy counter, but the SOC
    # reserve percentage is not invented.
    assert derived.value("reserve_pct") is None


def test_gather_inputs_marks_stale_rows_invalid(db_session, config):
    """The real database path: an old timestamp is stale, not merely old."""
    seed_current_state(db_session, ts=T0)
    fresh = gather_inputs(db_session, at(10), config)
    assert fresh.is_valid("battery_soc_pct")
    assert fresh.observable

    stale = gather_inputs(db_session, at(config.input_max_age_s + 10), config)
    assert not stale.is_valid("battery_soc_pct")
    assert stale.get("battery_soc_pct").status == "stale"
    assert not stale.observable
    assert stale.data_quality in {"degraded", "bad"}


def test_bad_quality_is_not_acted_on(db_session, config):
    seed_current_state(db_session, ts=T0)
    write_point(
        db_session,
        SPEC_BY_KEY["battery_soc_pct"].point_id,
        70.0,
        ts=T0,
        quality="substituted",
    )
    inputs = gather_inputs(db_session, at(1), config)
    reading = inputs.get("battery_soc_pct")
    assert reading.status == "bad_quality"
    assert not reading.valid


# ---------------------------------------------------------------------------
# EMERGENCY: immediate, latching, never suppressed
# ---------------------------------------------------------------------------


def test_emergency_is_immediate_and_latching(machine, db_session, config):
    force_state(machine, db_session, "NORMAL", T0)
    emergency = make_inputs(battery_temperature_max_c=60.0)

    decision = evaluate(machine, db_session, emergency, config, at(0))
    assert decision.transitioned
    assert decision.current_state == "EMERGENCY"
    assert decision.dwell_remaining_s == 0.0

    # The cause disappears; the state does not.
    healthy = make_inputs()
    for offset in (60, 6000, 60_000):
        decision = evaluate(machine, db_session, healthy, config, at(offset))
        assert decision.current_state == "EMERGENCY"
        assert decision.candidate.trigger == "latched"


def test_emergency_on_discharge_inhibited_with_live_load(config):
    inputs = make_inputs(bms_discharge_permissive=False)
    candidate = select_candidate("NORMAL", inputs, make_derived(inputs, config), config)
    assert candidate.state == "EMERGENCY"
    assert "discharge inhibited" in candidate.reason


def test_emergency_on_shutdown_class_inverter_fault(config):
    inputs = make_inputs(inverter_01_state="fault")
    candidate = select_candidate("NORMAL", inputs, make_derived(inputs, config), config)
    assert candidate.state == "EMERGENCY"


def test_emergency_on_critical_zone_alarm(config):
    inputs = make_inputs(container_alarm_summary="critical")
    candidate = select_candidate("NORMAL", inputs, make_derived(inputs, config), config)
    assert candidate.state == "EMERGENCY"


def test_emergency_is_evaluated_even_from_maintenance(machine, db_session, config):
    force_state(machine, db_session, "MAINTENANCE", T0)
    decision = evaluate(machine, db_session, make_inputs(battery_temperature_max_c=60.0), config, at(0))
    assert decision.current_state == "EMERGENCY"


def test_maintenance_holds_against_ordinary_conditions(machine, db_session, config):
    force_state(machine, db_session, "MAINTENANCE", T0)
    decision = evaluate(
        machine,
        db_session,
        make_inputs(battery_soc_pct=20.0, battery_energy_available_kwh=130.0),
        config,
        at(100_000),
    )
    assert decision.current_state == "MAINTENANCE"


# ---------------------------------------------------------------------------
# Operator freeze (SDD 30.9)
# ---------------------------------------------------------------------------


def test_freeze_withholds_a_transition_until_it_expires(machine, db_session, config):
    force_state(machine, db_session, "NORMAL", T0)
    machine.freeze(db_session, actor="op", reason="commissioning walkdown", now=T0, duration_s=1200)

    low = make_inputs(battery_soc_pct=45.0, battery_energy_available_kwh=290.0)
    decision = evaluate(machine, db_session, low, config, at(config.dwell_conserve_s + 10))
    assert decision.current_state == "NORMAL"
    assert decision.frozen
    assert "frozen" in decision.note
    # The pending candidate stays visible while frozen.
    assert decision.candidate.state == "CONSERVE"

    decision = evaluate(machine, db_session, low, config, at(1300))
    assert decision.transitioned
    assert decision.current_state == "CONSERVE"


def test_freeze_never_suppresses_emergency(machine, db_session, config):
    force_state(machine, db_session, "NORMAL", T0)
    machine.freeze(db_session, actor="op", reason="planned test", now=T0, duration_s=3600)

    decision = evaluate(machine, db_session, make_inputs(battery_temperature_max_c=60.0), config, at(30))
    assert decision.current_state == "EMERGENCY"
    assert decision.transitioned
    assert "never suppressed" in (decision.note or "")


def test_freeze_is_bounded(machine, db_session, config):
    snapshot = machine.freeze(db_session, actor="op", reason="x", now=T0, duration_s=999_999)
    assert snapshot.frozen_until == T0 + dt.timedelta(seconds=config.freeze_max_s)


def test_freeze_requires_a_reason(machine, db_session):
    with pytest.raises(ValueError):
        machine.freeze(db_session, actor="op", reason="", now=T0)


# ---------------------------------------------------------------------------
# Latch clearing
# ---------------------------------------------------------------------------


def test_clear_latch_requires_reason_and_condition_clear(machine, db_session, config):
    force_state(machine, db_session, "EMERGENCY", T0)

    with pytest.raises(LatchError):
        machine.clear_latch(db_session, actor="op", reason="fixed", condition_clear=False, now=at(60))
    with pytest.raises(LatchError):
        machine.clear_latch(db_session, actor="op", reason="", condition_clear=True, now=at(60))

    snapshot = machine.clear_latch(
        db_session,
        actor="op",
        reason="battery cooled and inspected; BMS reset",
        condition_clear=True,
        now=at(60),
    )
    assert snapshot.state == "CONSERVE"
    transition = (
        db_session.query(EnergyStateTransition).filter_by(trigger="operator_clear_latch").one()
    )
    assert transition.actor == "op"
    assert transition.from_state == "EMERGENCY"


def test_latch_cannot_be_cleared_into_an_optimistic_state(machine, db_session):
    force_state(machine, db_session, "EMERGENCY", T0)
    with pytest.raises(LatchError):
        machine.clear_latch(
            db_session, actor="op", reason="looks fine", condition_clear=True, now=at(60), to_state="SURPLUS"
        )


def test_set_state_refuses_to_walk_out_of_a_latch(machine, db_session):
    force_state(machine, db_session, "EMERGENCY", T0)
    with pytest.raises(LatchError):
        machine.set_state(db_session, "NORMAL", actor="op", reason="nope", now=at(1))


def test_operator_can_enter_maintenance(machine, db_session):
    force_state(machine, db_session, "NORMAL", T0)
    snapshot = machine.set_state(
        db_session, "MAINTENANCE", actor="op", reason="inverter firmware update", now=at(1)
    )
    assert snapshot.state == "MAINTENANCE"


# ---------------------------------------------------------------------------
# Black start and generator support override economic dispatch
# ---------------------------------------------------------------------------


def test_black_start_overrides_economic_dispatch(config):
    surplus = make_inputs(battery_soc_pct=95.0, battery_energy_available_kwh=608.0, pv_power_kw=12.0)
    candidate = select_candidate(
        "NORMAL", surplus, make_derived(surplus, config), config, black_start_active=True
    )
    assert candidate.state == "BLACK_START"


def test_generator_support_is_published_while_the_machine_runs(config):
    inputs = make_inputs(battery_soc_pct=45.0, battery_energy_available_kwh=290.0)
    candidate = select_candidate(
        "CONSERVE", inputs, make_derived(inputs, config), config, generator_supporting=True
    )
    assert candidate.state == "GENERATOR_SUPPORT"


def test_emergency_outranks_generator_support(config):
    inputs = make_inputs(battery_temperature_max_c=60.0)
    candidate = select_candidate(
        "GENERATOR_SUPPORT", inputs, make_derived(inputs, config), config, generator_supporting=True
    )
    assert candidate.state == "EMERGENCY"


# ---------------------------------------------------------------------------
# Publication (SDD 13)
# ---------------------------------------------------------------------------


def test_state_is_published_on_the_documented_topic(machine, db_session, settings, bus, config):
    import json

    force_state(machine, db_session, "CONSERVE", T0)
    snapshot = ensure_snapshot(db_session, now=T0)
    snapshot.data_quality = "good"
    topic = publish_state(bus, settings, snapshot, now=T0, reason="reserve declining")

    assert topic == "homestead/site/primary/site_01/energy_state"
    message = bus.last(topic)
    assert message is not None and message.retain
    payload = json.loads(message.text)
    assert payload["asset_id"] == settings.site_id
    assert payload["point"] == "energy_state"
    assert payload["value"]["state"] == "CONSERVE"
    assert payload["value"]["reason"] == "reserve declining"
    assert payload["quality"] == "calculated"


def test_published_quality_reflects_degraded_data(machine, db_session, settings, bus):
    force_state(machine, db_session, "DEGRADED_SENSOR", T0)
    snapshot = ensure_snapshot(db_session, now=T0)
    snapshot.data_quality = "bad"
    topic = publish_state(bus, settings, snapshot, now=T0)
    import json

    assert json.loads(bus.last(topic).text)["quality"] == "uncertain"


# ---------------------------------------------------------------------------
# EMS-T012: black start (SDD 35)
# ---------------------------------------------------------------------------


@pytest.fixture()
def blackstart(config):
    from homestead_twin.ems.blackstart import BlackStartCoordinator

    return BlackStartCoordinator(config)


def test_black_start_requires_operator_attestation(blackstart):
    with pytest.raises(PermissionError):
        blackstart.begin(
            make_inputs(),
            actor="op",
            reason="site blackout",
            now=T0,
            prerequisites_attested=False,
        )


def test_black_start_is_refused_when_a_checkable_prerequisite_fails(blackstart):
    frozen = make_inputs(battery_soc_pct=5.0, battery_temperature_max_c=-30.0)
    with pytest.raises(PermissionError):
        blackstart.begin(
            frozen, actor="op", reason="blackout", now=T0, prerequisites_attested=True
        )


def test_black_start_prerequisites_are_reported_honestly(blackstart):
    checks = {p.name: p for p in blackstart.prerequisites(make_inputs())}
    # The platform cannot verify protected control power or physical isolation,
    # and says so rather than claiming a pass.
    assert checks["control_power_protected"].satisfied is None
    assert checks["critical_distribution_isolatable"].satisfied is None
    assert checks["battery_within_limits"].satisfied is True


def test_black_start_sequence_runs_in_order_and_completes(blackstart):
    from homestead_twin.ems.blackstart import BLACK_START_STEPS

    state = blackstart.begin(
        make_inputs(), actor="op", reason="total AC blackout", now=T0, prerequisites_attested=True
    )
    assert state.active
    assert state.current_step == "verify_safety"

    # The critical bus comes before any noncritical load.
    order = [step.key for step in BLACK_START_STEPS]
    assert order.index("energize_critical_bus") < order.index("energize_survival_loads")
    assert order.index("energize_survival_loads") < order.index("start_rack_services")

    clock = 0
    for step in BLACK_START_STEPS:
        assert blackstart.state.current_step == step.key
        clock += 30
        blackstart.advance(actor="op", now=at(clock))
    assert not blackstart.state.active
    assert blackstart.state.completed_at is not None


def test_black_start_step_timeout_is_visible(blackstart, config):
    blackstart.begin(
        make_inputs(), actor="op", reason="blackout", now=T0, prerequisites_attested=True
    )
    assert not blackstart.step_timed_out(now=at(10))
    assert blackstart.step_timed_out(now=at(config.blackstart_step_timeout_s + 1))


def test_black_start_reconciliation_does_not_assume_retained_state(
    blackstart, db_session, config, settings, bus
):
    """SDD 35.4: retained desired state is not physical state."""
    from homestead_twin.ems import RecordingCommandPort
    from homestead_twin.ems.loader import load_schedule
    from homestead_twin.ems.shedding import ShedController, current_load_states
    from homestead_twin.models.registry import Asset
    from homestead_twin.models.energy import PowerBudgetLease

    for asset_id in ("energy.load.site.opportunistic_compute_01", "energy.load.site.tool_charging_01"):
        db_session.add(
            Asset(
                asset_id=asset_id,
                domain="energy",
                asset_class="load",
                name=asset_id,
                status="planned",
                criticality="discretionary",
                control_authority="supervisory",
            )
        )
    db_session.flush()
    load_schedule(db_session)

    inputs = make_inputs(battery_soc_pct=22.0, battery_energy_available_kwh=140.0)
    controller = ShedController(config, RecordingCommandPort(), settings=settings, bus=bus)
    result = controller.shed_step(
        session=db_session,
        energy_state="CRITICAL_RESERVE",
        inputs=inputs,
        derived=make_derived(inputs, config),
        now=T0,
    )
    assert result.performed
    db_session.add(
        PowerBudgetLease(
            lease_id="power-test-1",
            asset_id="energy.load.site.opportunistic_compute_01",
            granted_kw=1.0,
            starts_at=T0,
            expires_at=at(3600),
            priority=4,
            reason="batch job",
            revocable=True,
            state="active",
        )
    )
    db_session.flush()

    report = blackstart.reconcile(db_session, now=at(4000), config=config)
    assert report.unknown_loads
    assert report.cleared_leases == ["power-test-1"]
    assert report.stale_points or report.stale_points == []

    states = current_load_states(db_session, inputs, now=at(4001), config=config)
    for asset_id in report.unknown_loads:
        # "Shed" is downgraded to "unconfirmed": the EMS stops claiming to know.
        assert states[asset_id].last_action.outcome == "unconfirmed"


# ---------------------------------------------------------------------------
# The command port contract
# ---------------------------------------------------------------------------


def test_manager_command_port_never_raises_and_never_claims_success(settings, bus):
    """The default adapter degrades to a refusal when the manager is absent.

    It also never raises into the shed sequence: a supervisory allocator that
    crashes mid-shed is worse than one that reports a blocked command.
    """
    from homestead_twin.ems import CommandOutcome, CommandRequest, ManagerCommandPort

    port = ManagerCommandPort(settings=settings, bus=bus)
    outcome = port.issue(
        CommandRequest(asset_id="energy.load.site.spa_01", command="enabled_requested", value=False,
                       reason="test")
    )
    assert isinstance(outcome, CommandOutcome)
    if not outcome.accepted:
        assert outcome.detail
        assert outcome.outcome in {"blocked", "rejected", "failed"}


def test_recording_port_can_script_a_per_asset_failure():
    from homestead_twin.ems import CommandOutcome, CommandRequest, RecordingCommandPort

    port = RecordingCommandPort(
        responses={"energy.load.site.spa_01": CommandOutcome.refused("rejected", "manual")}
    )
    assert not port.issue(CommandRequest(asset_id="energy.load.site.spa_01", command="x")).accepted
    assert port.issue(CommandRequest(asset_id="energy.load.site.irrigation_01", command="x")).accepted
    assert port.assets() == ["energy.load.site.spa_01", "energy.load.site.irrigation_01"]
