"""Load shedding (SDD 32) and restoration (SDD 33) against the real load schedule.

The load schedule under test is ``data/load_schedule.yaml``, not a fixture, so
these tests also verify that the shipped tier assignments and group ordering
behave the way the narrative says they should.
"""

from __future__ import annotations

import json

import pytest
from test_ems_state_machine import T0, at, make_derived, make_inputs

from homestead_twin.ems import CommandOutcome, RecordingCommandPort
from homestead_twin.ems.config import EmsConfig
from homestead_twin.ems.inputs import load_input_key
from homestead_twin.ems.loader import load_schedule
from homestead_twin.ems.shedding import (
    ShedController,
    active_shed_groups,
    current_load_states,
    validate_shed_preconditions,
)
from homestead_twin.models.energy import LoadShedAction, PowerLoadProfile
from homestead_twin.models.registry import Asset

CONTROL_CORE = "energy.load.site.control_core_01"
RACK_COOLING = "energy.load.site.rack_cooling_01"
BATTERY_HVAC = "energy.load.site.battery_hvac_01"
SERVER_RACK = "energy.load.site.server_rack_01"
COMPUTE = "energy.load.site.opportunistic_compute_01"
TOOL_CHARGING = "energy.load.site.tool_charging_01"
SPA = "energy.load.site.spa_01"
WORKSHOP = "energy.load.site.workshop_heavy_01"
IRRIGATION = "energy.load.site.irrigation_01"
GH_LIGHTING = "energy.load.site.greenhouse_lighting_01"
GH_CLIMATE = "energy.load.site.greenhouse_climate_01"
WATER = "energy.load.site.water_pumping_01"

ALL_LOADS = (
    CONTROL_CORE,
    SERVER_RACK,
    RACK_COOLING,
    BATTERY_HVAC,
    WATER,
    IRRIGATION,
    GH_CLIMATE,
    GH_LIGHTING,
    SPA,
    WORKSHOP,
    TOOL_CHARGING,
    COMPUTE,
)


@pytest.fixture()
def config() -> EmsConfig:
    return EmsConfig()


@pytest.fixture()
def loads(db_session):
    """Register the twelve load assets and load the shipped schedule."""
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
    result = load_schedule(db_session)
    db_session.flush()
    assert result.created == 12
    assert not result.missing_assets
    return result


@pytest.fixture()
def port() -> RecordingCommandPort:
    return RecordingCommandPort()


@pytest.fixture()
def controller(config, port, settings, bus) -> ShedController:
    return ShedController(config, port, settings=settings, bus=bus)


def low_reserve_inputs(now=T0, **overrides):
    values = {"battery_soc_pct": 22.0, "battery_energy_available_kwh": 140.0}
    values.update(overrides)
    return make_inputs(now, **values)


def shed(controller, session, inputs, config, now, *, state="CRITICAL_RESERVE", **kwargs):
    derived = make_derived(inputs, config, now)
    return controller.shed_step(
        session, energy_state=state, inputs=inputs, derived=derived, now=now, **kwargs
    )


# ---------------------------------------------------------------------------
# The schedule itself
# ---------------------------------------------------------------------------


def test_schedule_loads_idempotently(db_session, loads):
    assert db_session.query(PowerLoadProfile).count() == 12
    again = load_schedule(db_session)
    assert again.created == 0
    assert again.updated == 0
    assert again.unchanged == 12


def test_schedule_never_invents_power_or_circuits(db_session, loads):
    for profile in db_session.query(PowerLoadProfile).all():
        assert profile.rated_power_kw is None
        assert profile.branch_circuit is None
        assert "rated_power_kw" in profile.open_fields
        assert "branch_circuit" in profile.open_fields
        assert profile.data_status in {"estimated", "unknown"}
        if profile.estimated_power_kw is not None:
            # Only the two loads that already carried a register planning
            # baseline have a number at all.
            assert profile.asset_id in {CONTROL_CORE, SERVER_RACK}


def test_yaml_and_json_mirrors_match():
    import yaml

    with open("data/load_schedule.yaml", encoding="utf-8") as handle:
        from_yaml = yaml.safe_load(handle)
    with open("data/load_schedule.json", encoding="utf-8") as handle:
        from_json = json.load(handle)
    assert from_yaml == from_json


def test_tier_zero_loads_have_no_shed_group(db_session, loads):
    for asset_id in (CONTROL_CORE, RACK_COOLING, BATTERY_HVAC):
        profile = db_session.get(PowerLoadProfile, asset_id)
        assert profile.base_tier == 0
        assert profile.shed_group is None
        assert profile.shed["permitted"] is False


# ---------------------------------------------------------------------------
# SDD 32.1 rule 1: verify the reserve is real before shedding
# ---------------------------------------------------------------------------


def test_shedding_refuses_when_the_reserve_cannot_be_verified(
    controller, db_session, loads, config, port, bus
):
    inputs = low_reserve_inputs(invalid=("battery_soc_pct",))
    result = shed(controller, db_session, inputs, config, T0)
    assert not result.performed
    assert not result.preconditions.ok
    assert not port.requests
    assert "could not be verified" in result.reason
    assert bus.last("homestead/site/#") is not None


def test_precondition_check_lists_each_component(config):
    inputs = low_reserve_inputs()
    result = validate_shed_preconditions(inputs, make_derived(inputs, config), config)
    assert result.ok
    assert set(result.checks) == {
        "soc_valid",
        "discharge_limit_valid",
        "discharge_permissive_valid",
        "site_load_valid",
        "reserve_valid",
    }


def test_shedding_is_not_attempted_in_normal(controller, db_session, loads, config, port):
    result = shed(controller, db_session, make_inputs(), config, T0, state="NORMAL")
    assert not result.performed
    assert not port.requests


# ---------------------------------------------------------------------------
# EMS-T004: ordered shedding, Tier 0 protected
# ---------------------------------------------------------------------------


def test_shedding_follows_group_order_and_never_touches_tier_zero(
    controller, db_session, loads, config, port
):
    inputs = low_reserve_inputs()
    clock = 0.0
    seen_groups = []

    for _ in range(6):
        result = shed(controller, db_session, inputs, config, at(clock))
        if result.performed:
            seen_groups.append(result.group)
            for action in result.actions:
                assert action.outcome in {"requested", "applied"}
        clock += config.shed_group_interval_s
        # Resolve the pending confirmation so the next group may proceed.
        states = current_load_states(db_session, inputs, now=at(clock), config=config)
        controller.confirm_pending(db_session, states, energy_state="CRITICAL_RESERVE", now=at(clock))

    assert seen_groups == ["S1", "S2", "S3", "S4", "S6"]

    shed_assets = [
        row.asset_id
        for row in db_session.query(LoadShedAction).order_by(LoadShedAction.occurred_at).all()
        if row.action in {"shed", "reduce"}
    ]
    # Opportunistic surplus first, survival profiles last.
    assert shed_assets.index(COMPUTE) < shed_assets.index(IRRIGATION)
    assert shed_assets.index(IRRIGATION) < shed_assets.index(GH_CLIMATE)
    # Tier 0 and the beyond-limit IT shutdown group are never commanded.
    for protected in (CONTROL_CORE, RACK_COOLING, BATTERY_HVAC, SERVER_RACK):
        assert protected not in shed_assets
        assert not port.commands_for(protected)


def test_tier_zero_is_reported_as_protected(controller, db_session, loads, config):
    inputs = low_reserve_inputs()
    result = shed(controller, db_session, inputs, config, T0)
    for protected in (CONTROL_CORE, RACK_COOLING, BATTERY_HVAC):
        assert "protected" in result.skipped[protected]


def test_attended_workshop_load_is_never_commanded(controller, db_session, loads, config, port):
    inputs = low_reserve_inputs()
    clock = 0.0
    for _ in range(6):
        shed(controller, db_session, inputs, config, at(clock))
        clock += config.shed_group_interval_s
        states = current_load_states(db_session, inputs, now=at(clock), config=config)
        controller.confirm_pending(db_session, states, energy_state="CRITICAL_RESERVE", now=at(clock))
    assert not port.commands_for(WORKSHOP)


def test_spa_lease_is_withdrawn_without_an_equipment_command(controller, db_session, loads, config, port):
    inputs = low_reserve_inputs()
    shed(controller, db_session, inputs, config, T0)  # S1
    result = shed(controller, db_session, inputs, config, at(config.shed_group_interval_s))  # S2
    assert result.group == "S2"
    spa_action = next(a for a in result.actions if a.asset_id == SPA)
    assert spa_action.outcome == "applied"
    assert not port.commands_for(SPA)


def test_group_gate_waits_for_confirmation(controller, db_session, loads, config):
    inputs = low_reserve_inputs()
    assert shed(controller, db_session, inputs, config, T0).performed
    blocked = shed(controller, db_session, inputs, config, at(5))
    assert not blocked.performed
    assert "awaiting measured confirmation" in blocked.reason


def test_minimum_on_time_blocks_an_immediate_reshed(controller, db_session, loads, config):
    inputs = low_reserve_inputs()
    # Pretend the compute load was restored a moment ago.
    db_session.add(
        LoadShedAction(
            asset_id=COMPUTE,
            action="restore",
            group="R5",
            energy_state="NORMAL",
            reason="test",
            outcome="requested",
            occurred_at=T0,
        )
    )
    db_session.flush()
    result = shed(controller, db_session, inputs, config, at(30))
    assert "minimum on time" in result.skipped[COMPUTE]


# ---------------------------------------------------------------------------
# EMS-T005: a rejected shed escalates
# ---------------------------------------------------------------------------


def test_rejected_shed_escalates_and_is_not_assumed_off(config, db_session, loads, settings, bus):
    port = RecordingCommandPort(
        responses={COMPUTE: CommandOutcome.refused("rejected", "local controller in manual")}
    )
    controller = ShedController(config, port, settings=settings, bus=bus)
    inputs = low_reserve_inputs()

    result = shed(controller, db_session, inputs, config, T0)
    assert result.performed
    assert result.escalations
    failure = next(a for a in result.escalations if a.asset_id == COMPUTE)
    assert failure.outcome == "rejected"
    assert "recalculated with those loads still connected" in result.reason

    states = current_load_states(db_session, inputs, now=at(1), config=config)
    assert not states[COMPUTE].is_shed
    assert states[COMPUTE].shed_failed
    # The alarm reached the bus.
    message = bus.last("homestead/energy/site/load_opportunistic_compute_01/alarm/load_shed_failed")
    assert message is not None
    assert json.loads(message.text)["detail"]["severity"] == "major"


def test_blocked_command_is_a_failure_not_a_success(config, db_session, loads, settings, bus):
    from homestead_twin.ems import NullCommandPort

    port = NullCommandPort()
    controller = ShedController(config, port, settings=settings, bus=bus)
    inputs = low_reserve_inputs()
    result = shed(controller, db_session, inputs, config, T0)
    assert all(a.outcome in {"blocked", "applied"} for a in result.actions)
    states = current_load_states(db_session, inputs, now=at(1), config=config)
    assert not states[COMPUTE].is_shed


def test_repeated_failure_locks_the_load_out(config, db_session, loads, settings, bus):
    port = RecordingCommandPort(
        responses={
            COMPUTE: CommandOutcome.refused("rejected", "manual override"),
            TOOL_CHARGING: CommandOutcome.refused("rejected", "manual override"),
        }
    )
    controller = ShedController(config, port, settings=settings, bus=bus)
    inputs = low_reserve_inputs()

    clock = 0.0
    for _ in range(config.shed_attempt_limit + 1):
        shed(controller, db_session, inputs, config, at(clock))
        clock += max(config.shed_reissue_interval_s, config.shed_group_interval_s)

    states = current_load_states(db_session, inputs, now=at(clock), config=config)
    assert states[COMPUTE].locked_out
    # And a locked-out load is not commanded again.
    before = len(port.commands_for(COMPUTE))
    shed(controller, db_session, inputs, config, at(clock + 10_000))
    assert len(port.commands_for(COMPUTE)) == before


def test_reissue_interval_prevents_rapid_on_off(config, db_session, loads, settings, bus):
    port = RecordingCommandPort(responses={COMPUTE: CommandOutcome.refused("rejected", "manual override")})
    controller = ShedController(config, port, settings=settings, bus=bus)
    inputs = low_reserve_inputs()
    shed(controller, db_session, inputs, config, T0)
    result = shed(controller, db_session, inputs, config, at(config.shed_group_interval_s + 1))
    assert "reissue interval" in result.skipped[COMPUTE]


# ---------------------------------------------------------------------------
# Confirmation by measurement (SDD 32.2)
# ---------------------------------------------------------------------------


def test_measured_reduction_confirms_the_shed(controller, db_session, loads, config):
    inputs = low_reserve_inputs(**{load_input_key(COMPUTE, "power_kw"): 1.2})
    shed(controller, db_session, inputs, config, T0)

    quiet = low_reserve_inputs(at(120), **{load_input_key(COMPUTE, "power_kw"): 0.0})
    states = current_load_states(db_session, inputs=quiet, now=at(120), config=config)
    outcomes = controller.confirm_pending(db_session, states, energy_state="CRITICAL_RESERVE", now=at(120))
    compute = next(o for o in outcomes if o.asset_id == COMPUTE)
    assert compute.outcome == "confirmed"


def test_load_still_drawing_power_raises_load_shed_failed(controller, db_session, loads, config, bus):
    inputs = low_reserve_inputs(**{load_input_key(COMPUTE, "power_kw"): 1.2})
    shed(controller, db_session, inputs, config, T0)

    still_on = low_reserve_inputs(at(120), **{load_input_key(COMPUTE, "power_kw"): 1.2})
    states = current_load_states(db_session, inputs=still_on, now=at(120), config=config)
    outcomes = controller.confirm_pending(db_session, states, energy_state="CRITICAL_RESERVE", now=at(120))
    compute = next(o for o in outcomes if o.asset_id == COMPUTE)
    assert compute.outcome == "no_reduction"

    fresh = current_load_states(db_session, inputs=still_on, now=at(121), config=config)
    assert not fresh[COMPUTE].is_shed  # the reduction is unavailable, not assumed


def test_unmeasurable_load_is_marked_unconfirmed(controller, db_session, loads, config):
    """Every measured_power_point binding is still 'tbd', so this is today's path."""
    inputs = low_reserve_inputs()
    shed(controller, db_session, inputs, config, T0)
    states = current_load_states(db_session, inputs, now=at(120), config=config)
    outcomes = controller.confirm_pending(db_session, states, energy_state="CRITICAL_RESERVE", now=at(120))
    assert [o.outcome for o in outcomes if o.asset_id == COMPUTE] == ["unconfirmed"]


# ---------------------------------------------------------------------------
# Restoration (SDD 33, FR-103)
# ---------------------------------------------------------------------------


def restore(controller, session, inputs, config, now, *, state="NORMAL", entered_at=T0):
    derived = make_derived(inputs, config, now)
    return controller.restore_step(
        session,
        energy_state=state,
        state_entered_at=entered_at,
        inputs=inputs,
        derived=derived,
        now=now,
    )


def shed_everything(controller, db_session, config, groups=6):
    inputs = low_reserve_inputs()
    clock = 0.0
    for _ in range(groups):
        shed(controller, db_session, inputs, config, at(clock))
        clock += config.shed_group_interval_s
        states = current_load_states(db_session, inputs, now=at(clock), config=config)
        controller.confirm_pending(db_session, states, energy_state="CRITICAL_RESERVE", now=at(clock))
    return clock


def test_restoration_requires_sustained_qualification(controller, db_session, loads, config):
    clock = shed_everything(controller, db_session, config)
    healthy = make_inputs(at(clock))

    early = restore(controller, db_session, healthy, config, at(clock + 60), entered_at=at(clock))
    assert not early.qualified
    assert "of the required" in early.reason

    later = restore(
        controller,
        db_session,
        healthy,
        config,
        at(clock + config.restore_qualification_s + 1),
        entered_at=at(clock),
    )
    assert later.qualified


def test_restoration_is_refused_while_the_margin_is_poor(controller, db_session, loads, config):
    clock = shed_everything(controller, db_session, config)
    poor = make_inputs(at(clock), battery_soc_pct=40.0, battery_energy_available_kwh=256.0)
    result = restore(
        controller,
        db_session,
        poor,
        config,
        at(clock + config.restore_qualification_s + 1),
        entered_at=at(clock),
    )
    assert not result.qualified


def test_restoration_is_staggered_and_ordered(controller, db_session, loads, config, port):
    clock = shed_everything(controller, db_session, config)
    healthy = make_inputs()
    entered = at(clock)
    start = clock + config.restore_qualification_s + 1

    restored: list[str] = []
    now = start
    for _ in range(12):
        result = restore(controller, db_session, healthy, config, at(now), entered_at=entered)
        for action in result.actions:
            restored.append(action.asset_id)
        # Only one start per step: FR-103 anti-simultaneous restart.
        assert len(result.actions) <= config.restore_max_per_step
        now += 200

    # R1 (greenhouse climate, freeze protection) precedes R2 (water), which
    # precedes R4 (irrigation, lighting) and R5 (batch compute).
    assert restored.index(GH_CLIMATE) < restored.index(WATER)
    assert restored.index(WATER) < restored.index(IRRIGATION)
    assert restored.index(IRRIGATION) < restored.index(COMPUTE)
    # Attended equipment is never restarted automatically.
    assert SPA not in restored
    assert WORKSHOP not in restored


def test_high_inrush_load_waits_out_its_stagger(controller, db_session, loads, config):
    clock = shed_everything(controller, db_session, config)
    healthy = make_inputs()
    entered = at(clock)
    start = clock + config.restore_qualification_s + 1

    first = restore(controller, db_session, healthy, config, at(start), entered_at=entered)
    assert first.performed
    # A second start immediately afterwards is deferred by the inrush stagger.
    second = restore(controller, db_session, healthy, config, at(start + 5), entered_at=entered)
    assert not second.performed
    assert any("stagger" in why for why in second.deferred.values())


def test_minimum_off_time_defers_restoration(controller, db_session, loads, config):
    inputs = low_reserve_inputs()
    shed(controller, db_session, inputs, config, T0)  # sheds S1 (compute, tool charging)
    healthy = make_inputs()
    result = restore(
        controller,
        db_session,
        healthy,
        config,
        at(config.restore_qualification_s + 1),
        entered_at=T0,
    )
    # Compute has a 60 s minimum off time, long satisfied; the point is that the
    # deferral messages name the constraint when it is not.
    early = restore(controller, db_session, healthy, config, at(30), entered_at=T0)
    assert not early.qualified
    assert result.qualified


def test_attended_loads_require_operator_review(controller, db_session, loads, config):
    clock = shed_everything(controller, db_session, config)
    healthy = make_inputs()
    result = restore(
        controller,
        db_session,
        healthy,
        config,
        at(clock + config.restore_qualification_s + 1),
        entered_at=at(clock),
    )
    assert SPA in result.operator_review_required
    assert "operator action required" in result.deferred[SPA]


# ---------------------------------------------------------------------------
# Minimum service (SDD 31.1)
# ---------------------------------------------------------------------------


def test_greenhouse_freeze_protection_is_restored_at_its_maximum_off_time(
    controller, db_session, loads, config, port
):
    clock = shed_everything(controller, db_session, config)
    inputs = low_reserve_inputs()

    # Twenty minutes is the greenhouse climate load's maximum_off_time_min.
    states = current_load_states(db_session, inputs, now=at(clock + 60), config=config)
    assert not controller.forced_restores(
        db_session, states, energy_state="CRITICAL_RESERVE", now=at(clock + 60)
    )

    later = at(clock + 21 * 60)
    states = current_load_states(db_session, inputs, now=later, config=config)
    forced = controller.forced_restores(db_session, states, energy_state="CRITICAL_RESERVE", now=later)
    assert [f.asset_id for f in forced] == [GH_CLIMATE]
    assert "minimum_service_maximum_off_time" in forced[0].reason


def test_minimum_service_does_not_override_emergency(controller, db_session, loads, config):
    clock = shed_everything(controller, db_session, config)
    inputs = low_reserve_inputs()
    later = at(clock + 3600)
    states = current_load_states(db_session, inputs, now=later, config=config)
    assert not controller.forced_restores(db_session, states, energy_state="EMERGENCY", now=later)


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


def test_active_shed_groups_are_reported(controller, db_session, loads, config):
    inputs = low_reserve_inputs()
    shed(controller, db_session, inputs, config, T0)
    states = current_load_states(db_session, inputs, now=at(1), config=config)
    assert active_shed_groups(states) == ["S1"]


def test_every_action_is_recorded(controller, db_session, loads, config):
    inputs = low_reserve_inputs()
    shed(controller, db_session, inputs, config, T0)
    rows = db_session.query(LoadShedAction).all()
    assert rows
    for row in rows:
        assert row.energy_state == "CRITICAL_RESERVE"
        assert row.reason
        assert row.occurred_at is not None
