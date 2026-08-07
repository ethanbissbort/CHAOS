"""Generator coordination (SDD 34, FR-104).

The EMS *requests* a start through the equipment-native interface. These tests
check that it asks only when a start criterion is met and every permissive
holds, that it handles rejection, that it honours the minimum run and cooldown
timers, and that repeated failure stops it cranking (SDD 34.7).

Time is passed in; nothing sleeps.
"""

from __future__ import annotations

import json

import pytest
from test_ems_state_machine import T0, at, make_derived, make_inputs

from homestead_twin.ems import CommandOutcome, RecordingCommandPort
from homestead_twin.ems.config import EmsConfig
from homestead_twin.ems.generator import GeneratorCoordinator, GeneratorRuntime

GENERATOR = "energy.generator.site.01"


@pytest.fixture()
def config() -> EmsConfig:
    return EmsConfig()


@pytest.fixture()
def port() -> RecordingCommandPort:
    return RecordingCommandPort()


@pytest.fixture()
def coordinator(config, port, settings, bus) -> GeneratorCoordinator:
    return GeneratorCoordinator(config, port, settings=settings, bus=bus)


def low_reserve(now=T0, **overrides):
    """Reserve low enough to satisfy an SDD 34.1 start criterion."""
    values = {"battery_soc_pct": 22.0, "battery_energy_available_kwh": 140.0}
    values.update(overrides)
    return make_inputs(now, **values)


def run(coordinator, inputs, config, now, *, state="CRITICAL_RESERVE", **kwargs):
    return coordinator.evaluate(
        inputs, make_derived(inputs, config, now), energy_state=state, now=now, **kwargs
    )


# ---------------------------------------------------------------------------
# Start criteria and permissives
# ---------------------------------------------------------------------------


def test_no_start_when_the_site_is_healthy(coordinator, config, port):
    decision = run(coordinator, make_inputs(), config, T0, state="NORMAL")
    assert decision.action == "none"
    assert decision.sequence == "idle"
    assert not port.requests


def test_start_criteria_are_listed(coordinator, config):
    decision = run(coordinator, low_reserve(), config, T0)
    assert decision.action == "start_requested"
    assert any("emergency reserve" in reason for reason in decision.start_reasons)


def test_low_autonomy_alone_requests_a_start(coordinator, config):
    inputs = low_reserve(battery_energy_available_kwh=170.0, critical_load_kw=6.0)
    decision = run(coordinator, inputs, config, T0)
    assert decision.action == "start_requested"
    assert any("autonomy" in reason for reason in decision.start_reasons)


def test_start_request_uses_the_equipment_native_point(coordinator, config, port):
    run(coordinator, low_reserve(), config, T0)
    request = port.commands_for(GENERATOR)[0]
    assert request.command == "generator_start_request"
    assert request.value is True
    assert "start request" in request.reason


def test_start_records_cause_fuel_runtime_and_expected_stop(coordinator, config):
    run(coordinator, low_reserve(), config, T0)
    runtime = coordinator.runtime
    assert runtime.start_cause
    assert runtime.fuel_at_start_pct == 80.0
    assert runtime.runtime_at_start_h == 120.0
    assert "minimum run" in runtime.expected_stop_condition


# ---------------------------------------------------------------------------
# EMS-T008: generator unavailable during critical reserve
# ---------------------------------------------------------------------------


def test_unavailable_generator_blocks_the_start_and_alarms(coordinator, config, port, bus):
    decision = run(coordinator, low_reserve(generator_available=False), config, T0)
    assert decision.action == "none"
    assert "automatic_mode" in decision.blocked_by
    assert not port.requests

    message = bus.last("homestead/energy/site/generator_01/alarm/generator_start_blocked")
    assert message is not None
    assert json.loads(message.text)["detail"]["severity"] == "major"


def test_low_fuel_blocks_the_start(coordinator, config, port):
    decision = run(coordinator, low_reserve(generator_fuel_pct=5.0), config, T0)
    assert "fuel_above_minimum" in decision.blocked_by
    assert not port.requests


def test_maintenance_lockout_blocks_the_start(coordinator, config, port):
    decision = run(coordinator, low_reserve(generator_state="maintenance"), config, T0)
    assert "no_maintenance_lockout" in decision.blocked_by
    assert not port.requests


def test_unknown_permissive_blocks_the_start(coordinator, config, port):
    """An unverified permissive is not a permissive."""
    decision = run(coordinator, low_reserve(invalid=("generator_fuel_pct",)), config, T0)
    assert "fuel_above_minimum" in decision.blocked_by
    assert not port.requests


def test_blocked_start_is_not_retried_in_a_loop(coordinator, config, port):
    inputs = low_reserve(generator_available=False)
    for offset in range(0, 600, 60):
        run(coordinator, inputs, config, at(offset))
    assert not port.requests
    assert coordinator.runtime.attempts == 0


# ---------------------------------------------------------------------------
# EMS-T009: a successful start / run / stop cycle
# ---------------------------------------------------------------------------


def test_full_start_run_stop_cycle(coordinator, config, port, bus):
    inputs = low_reserve()

    decision = run(coordinator, inputs, config, at(0))
    assert decision.sequence == "requested"
    assert coordinator.supporting

    # The native controller has not reported running yet.
    decision = run(coordinator, inputs, config, at(30))
    assert decision.sequence == "starting"

    running_inputs = low_reserve(at(60), generator_state="running", generator_power_kw=8.0)
    decision = run(coordinator, running_inputs, config, at(60))
    assert decision.sequence == "running"
    assert coordinator.runtime.running_since is not None

    # The minimum run time is enforced even though PV has already recovered.
    recovered = make_inputs(generator_state="running", generator_power_kw=8.0)
    early = run(coordinator, recovered, config, at(60 + config.generator_min_run_s - 1))
    assert early.sequence == "running"
    assert "minimum run" in early.reason

    stop = run(coordinator, recovered, config, at(60 + config.generator_min_run_s))
    assert stop.action == "stop_requested"
    assert stop.sequence == "stopping"

    cooldown = run(coordinator, recovered, config, at(60 + config.generator_min_run_s + 1))
    assert cooldown.sequence == "cooldown"
    assert coordinator.supporting

    # The start request is only removed once the cooldown has run.
    mid = run(coordinator, recovered, config, at(60 + config.generator_min_run_s + 10))
    assert mid.sequence == "cooldown"
    assert [r.value for r in port.commands_for(GENERATOR)] == [True]

    done = run(
        coordinator,
        recovered,
        config,
        at(60 + config.generator_min_run_s + config.generator_cooldown_s + 2),
    )
    assert done.action == "stopped"
    assert done.sequence == "idle"
    assert not coordinator.supporting
    assert [r.value for r in port.commands_for(GENERATOR)] == [True, False]

    stopped = bus.last("homestead/energy/site/generator_01/alarm/generator_stopped")
    assert stopped is not None


def test_restart_inhibit_after_a_stop(coordinator, config, port):
    coordinator.runtime = GeneratorRuntime(sequence="idle", stopped_at=T0.isoformat())
    decision = run(coordinator, low_reserve(), config, at(60))
    assert "restart_inhibit_elapsed" in decision.blocked_by
    assert not port.requests

    later = run(coordinator, low_reserve(), config, at(config.generator_restart_inhibit_s + 1))
    assert later.action == "start_requested"


def test_maximum_continuous_run_forces_a_stop(coordinator, config):
    coordinator.runtime = GeneratorRuntime(sequence="running", running_since=T0.isoformat())
    # Reserve still poor, PV still dark: only the inspection interval stops it.
    inputs = low_reserve(generator_state="running", pv_power_kw=0.0)
    decision = run(coordinator, inputs, config, at(config.generator_max_continuous_run_s))
    assert decision.action == "stop_requested"
    assert "maximum continuous run" in decision.reason


def test_stop_target_soc_ends_the_run(coordinator, config):
    coordinator.runtime = GeneratorRuntime(sequence="running", running_since=T0.isoformat())
    charged = make_inputs(
        battery_soc_pct=85.0,
        battery_energy_available_kwh=544.0,
        generator_state="running",
        pv_power_kw=0.0,
    )
    decision = run(coordinator, charged, config, at(config.generator_min_run_s + 1))
    assert decision.action == "stop_requested"
    assert "stop target" in decision.reason


# ---------------------------------------------------------------------------
# Failure handling (SDD 34.7, EMS-T010)
# ---------------------------------------------------------------------------


def test_rejected_start_request_is_handled(config, settings, bus):
    port = RecordingCommandPort(
        responses={GENERATOR: CommandOutcome.refused("rejected", "generator controller in manual")}
    )
    coordinator = GeneratorCoordinator(config, port, settings=settings, bus=bus)
    decision = run(coordinator, low_reserve(), config, T0)
    assert decision.action == "start_failed"
    assert coordinator.runtime.sequence == "failed"
    assert coordinator.runtime.attempts == 1
    message = bus.last("homestead/energy/site/generator_01/alarm/generator_start_failed")
    assert json.loads(message.text)["detail"]["severity"] == "critical"


def test_start_timeout_fails_the_sequence(coordinator, config, port, bus):
    run(coordinator, low_reserve(), config, at(0))
    decision = run(coordinator, low_reserve(), config, at(config.generator_start_timeout_s + 1))
    assert decision.action == "start_failed"
    # The request is withdrawn so the native controller is not left asserted.
    assert [r.value for r in port.commands_for(GENERATOR)] == [True, False]


def test_controller_reported_start_failure(coordinator, config):
    run(coordinator, low_reserve(), config, at(0))
    decision = run(coordinator, low_reserve(generator_start_failure=True), config, at(30))
    assert decision.action == "start_failed"
    assert "start failure" in coordinator.runtime.last_failure


def test_repeated_failure_locks_out_and_needs_a_reset(coordinator, config, port):
    clock = 0
    for _ in range(config.generator_start_attempt_limit):
        run(coordinator, low_reserve(), config, at(clock))
        clock += config.generator_start_timeout_s + 1
        run(coordinator, low_reserve(), config, at(clock))
        clock += config.generator_restart_inhibit_s + 1

    assert coordinator.runtime.sequence == "lockout"
    attempts_before = len(port.commands_for(GENERATOR))

    decision = run(coordinator, low_reserve(), config, at(clock + 100_000))
    assert decision.sequence == "lockout"
    assert "explicit reset" in decision.reason
    assert len(port.commands_for(GENERATOR)) == attempts_before

    coordinator.reset(actor="op", reason="fuel filter replaced, controller reset", now=at(clock + 100_001))
    assert coordinator.runtime.sequence == "idle"
    assert coordinator.runtime.attempts == 0


def test_reset_requires_a_reason(coordinator):
    with pytest.raises(ValueError):
        coordinator.reset(actor="op", reason="", now=T0)


def test_generator_lost_mid_run_alarms_critically(coordinator, config, bus):
    coordinator.runtime = GeneratorRuntime(sequence="running", running_since=T0.isoformat())
    decision = run(coordinator, low_reserve(generator_state="stopped"), config, at(600))
    assert decision.action == "start_failed"
    assert coordinator.runtime.sequence == "failed"
    message = bus.last("homestead/energy/site/generator_01/alarm/generator_start_failed")
    assert json.loads(message.text)["detail"]["stage"] == "run"


# ---------------------------------------------------------------------------
# Persistence and reporting
# ---------------------------------------------------------------------------


def test_runtime_state_round_trips(coordinator, config):
    run(coordinator, low_reserve(), config, T0)
    blob = coordinator.runtime.as_dict()
    restored = GeneratorRuntime.from_dict(blob)
    assert restored.sequence == "requested"
    assert restored.start_cause == coordinator.runtime.start_cause


def test_status_reports_observed_values(coordinator, config):
    inputs = low_reserve(generator_state="running")
    run(coordinator, inputs, config, T0)
    status = coordinator.status(inputs)
    assert status["observed_state"] == "running"
    assert status["fuel_level_pct"] == 80.0
    assert status["supporting"] is True


def test_generator_running_without_a_request_is_adopted(coordinator, config, port):
    decision = run(coordinator, make_inputs(generator_state="running"), config, T0, state="NORMAL")
    assert decision.sequence == "running"
    assert not port.requests
    assert "adopted" in decision.reason
