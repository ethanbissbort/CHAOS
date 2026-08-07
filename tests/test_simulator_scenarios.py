"""Scenario tests.

Each scenario exists to make one or more SDD section 39 verification cases
reproducible. These tests assert that the scenario actually produces the
condition it claims to, so that a later EMS test can rely on it.

They are deliberately short: a scenario slice long enough to show the behaviour,
never a full 24-hour sweep, so the suite stays fast.
"""

from __future__ import annotations

import pytest

from homestead_twin import topics
from homestead_twin.envelope import parse_availability, parse_command_ack, parse_telemetry
from homestead_twin.mqtt import InMemoryBus
from simulator.clock import SteppedPacer, parse_duration
from simulator.scenarios import (
    Scenario,
    ScenarioEvent,
    ScenarioRunner,
    all_scenarios,
    get_scenario,
    scenario_names,
)

#: Every scenario the brief requires, plus the extras this rig adds.
REQUIRED = {
    "clear_summer_day",
    "overcast_winter_day",
    "passing_clouds",
    "reserve_decline",
    "generator_support",
    "generator_fails_to_start",
    "inverter_fault",
    "black_start",
    "comms_loss",
    "sensor_failure",
    "rack_cooling_loss",
}


def run_scenario(settings, name, duration_s=None, step_s=None, bus=None):
    """Run a scenario slice and return ``(site, runner, bus)``."""
    scenario = get_scenario(name)
    bus = bus if bus is not None else InMemoryBus()
    site = scenario.build(bus, settings=settings)
    runner = ScenarioRunner(site, scenario)
    site.start()
    runner.pump()
    duration = scenario.duration_s if duration_s is None else parse_duration(duration_s)
    step = scenario.step_s if step_s is None else step_s

    def on_step(current, _balance):
        runner.pump()

    site.run(duration_s=duration, dt_s=step, pacer=SteppedPacer(), on_step=on_step)
    return site, runner, bus


def run_scenario_capped(settings, name, duration_s, step_s=None):
    """Run a scenario slice while discarding published messages, to bound memory."""
    scenario = get_scenario(name)
    bus = InMemoryBus()
    site = scenario.build(bus, settings=settings)
    runner = ScenarioRunner(site, scenario)
    site.start()
    runner.pump()

    def on_step(current, _balance):
        runner.pump()
        bus.clear()

    site.run(
        duration_s=parse_duration(duration_s),
        dt_s=step_s or scenario.step_s,
        pacer=SteppedPacer(),
        on_step=on_step,
    )
    return site, runner, bus


# ------------------------------------------------------------------ catalogue


class TestCatalogue:
    def test_every_required_scenario_exists(self):
        assert REQUIRED <= set(scenario_names())

    def test_scenarios_are_freshly_built_each_time(self):
        """Configs are mutable; two callers must never share one."""
        first = get_scenario("clear_summer_day")
        second = get_scenario("clear_summer_day")
        assert first is not second
        assert first.config is not second.config

    def test_unknown_scenario_names_the_alternatives(self):
        with pytest.raises(KeyError) as excinfo:
            get_scenario("sunny_with_a_chance")
        assert "clear_summer_day" in str(excinfo.value)

    def test_every_scenario_is_well_formed(self):
        for name, scenario in all_scenarios().items():
            assert isinstance(scenario, Scenario)
            assert scenario.name == name
            assert scenario.description.strip()
            assert scenario.duration_s > 0
            assert scenario.step_s > 0
            assert all(isinstance(event, ScenarioEvent) for event in scenario.events)
            assert all(0 <= event.at_s <= scenario.duration_s for event in scenario.events)

    def test_verification_cases_are_covered(self):
        """The SDD 39 cases this rig can drive from the site side."""
        covered = {case for s in all_scenarios().values() for case in s.verifies}
        assert {
            "EMS-T003",
            "EMS-T004",
            "EMS-T005",
            "EMS-T006",
            "EMS-T007",
            "EMS-T008",
            "EMS-T009",
            "EMS-T010",
            "EMS-T011",
            "EMS-T012",
            "EMS-T014",
        } <= covered

    @pytest.mark.parametrize("name", sorted(REQUIRED))
    def test_scenario_runs_and_conserves_energy(self, settings, name):
        site, runner, _bus = run_scenario_capped(settings, name, "20m", step_s=10.0)
        assert site.stats.steps > 0
        assert site.battery.conservation_error_kwh() == pytest.approx(0.0, abs=1e-6)
        assert 0.0 <= site.battery.soc_pct <= 100.0

    def test_scenarios_are_deterministic(self, settings):
        def trace():
            site, _runner, bus = run_scenario_capped(settings, "passing_clouds", "10m", step_s=5.0)
            return site.snapshot()

        assert trace() == trace()

    def test_seed_override_changes_the_trace(self, settings):
        def trace(seed):
            scenario = get_scenario("passing_clouds")
            bus = InMemoryBus()
            site = scenario.build(bus, settings=settings, seed=seed)
            site.start()
            site.run(duration_s=600, dt_s=5.0, pacer=SteppedPacer())
            return site.snapshot()

        assert trace(1) != trace(2)


# ------------------------------------------------------------------ weather days


class TestWeatherDays:
    def test_clear_summer_day_delivers_a_real_harvest(self, settings):
        site, _runner, _bus = run_scenario_capped(
            settings, "clear_summer_day", "24h", step_s=120.0
        )
        assert site.stats.pv_energy_kwh > 150.0  # 45 kWdc on the solstice
        assert site.stats.pv_peak_kw > 25.0
        assert site.stats.soc_max_pct > site.config.battery.initial_soc_pct
        assert site.stats.unserved_energy_kwh == pytest.approx(0.0, abs=1e-6)

    def test_overcast_winter_day_barely_generates(self, settings):
        winter, _runner, _bus = run_scenario_capped(
            settings, "overcast_winter_day", "24h", step_s=120.0
        )
        # A clear solstice day delivers >150 kWh (see the test above); heavy
        # January overcast must come nowhere near it, and the bank must end the
        # day lower than it started.
        assert winter.stats.pv_energy_kwh < 60.0
        assert winter.stats.pv_peak_kw < 15.0
        assert winter.battery.soc_pct < winter.config.battery.initial_soc_pct

    def test_passing_clouds_swing_pv_without_swinging_the_bus(self, settings):
        """SDD 30.9 / EMS-T006: irradiance oscillates, the site must not."""
        site, _runner, bus = run_scenario_capped(settings, "passing_clouds", "2h", step_s=10.0)
        # The resource really does swing.
        assert site.stats.pv_peak_kw > 10.0
        # ... but nothing shed itself, and the bus never dropped.
        assert site.stats.blackout_s == 0.0
        assert site.stats.unserved_energy_kwh == pytest.approx(0.0, abs=1e-6)
        shed_events = [e for e in site.stats.events if "load_shed" in e]
        assert shed_events == []

    def test_passing_clouds_irradiance_actually_oscillates(self, settings):
        scenario = get_scenario("passing_clouds")
        bus = InMemoryBus()
        site = scenario.build(bus, settings=settings)
        site.start()
        series = []
        for _ in range(360):  # 30 minutes at 5 s: three cloud cycles
            site.step(5.0)
            bus.clear()
            series.append(site.context.poa_irradiance_w_m2)
        assert max(series) > 3 * (min(series) + 1)


# ------------------------------------------------------------------ reserve / generator


class TestReserveAndGenerator:
    def test_reserve_decline_drives_soc_down(self, settings):
        site, runner, _bus = run_scenario_capped(
            settings, "reserve_decline", "9h", step_s=30.0
        )
        assert site.battery.soc_pct < site.config.battery.initial_soc_pct - 5.0
        # The generator is in manual, so it is unavailable to rescue the reserve.
        assert site.generator.available is False
        assert site.generator.state == "off"

    def test_reserve_decline_shed_sequence_is_answered(self, settings):
        site, runner, _bus = run_scenario_capped(
            settings, "reserve_decline", "9h", step_s=30.0
        )
        labels = [label for _, label in runner.fired]
        assert any("opportunistic" in label for label in labels)
        assert site.stats.commands_accepted >= 3
        assert site.stats.commands_rejected >= 1  # the Tier 0 attempt
        assert any(
            "tier0_control_survival_never_shed" in reason for reason in site.stats.rejections
        )

    def test_generator_support_completes_the_full_sequence(self, settings):
        """EMS-T009: start, warm up, transfer, charge, stop through cooldown."""
        site, _runner, _bus = run_scenario_capped(
            settings, "generator_support", "4h", step_s=5.0
        )
        assert site.generator.starts == 1
        assert site.generator.runtime_h > 1.0
        assert site.generator.state == "off"  # stopped through cooldown
        assert site.generator.source_selected == "source_a"  # transferred back
        assert site.stats.generator_energy_kwh > 20.0
        assert site.battery.energy_charged_kwh > 0.0  # it charged the bank
        assert site.generator.fuel_pct < site.config.generator.initial_fuel_pct

    def test_generator_failure_locks_out_after_the_attempt_policy(self, settings):
        """EMS-T008 / SDD 34.7."""
        site, _runner, _bus = run_scenario_capped(
            settings, "generator_fails_to_start", "2h", step_s=5.0
        )
        assert site.generator.state == "lockout"
        assert site.generator.failed_attempts >= 3
        assert site.generator.starts == site.config.generator.start_attempt_limit
        assert site.generator.runtime_h == 0.0
        assert site.generator.available is False
        events = [e for e in site.stats.events if "generator" in e]
        assert any("generator_start_failed" in e for e in events)
        assert any("generator_start_lockout" in e for e in events)

    def test_generator_trip_during_run_transfers_back(self, settings):
        """EMS-T010."""
        site, _runner, _bus = run_scenario_capped(
            settings, "generator_fails_during_run", "2h", step_s=5.0
        )
        assert site.generator.starts == 1
        assert site.generator.fault_active is True
        assert site.generator.output_kw == 0.0
        assert site.generator.source_selected == "source_a"
        assert site.generator.available is False
        assert any("generator_fault_shutdown" in e for e in site.stats.events)


# ------------------------------------------------------------------ equipment faults


class TestEquipmentFaults:
    def test_inverter_fault_halves_the_block(self, settings):
        site, _runner, _bus = run_scenario_capped(settings, "inverter_fault", "4h", step_s=10.0)
        # Unit 2 was reset by the operator; unit 4 is a hardware lockout.
        assert site.inverters.units[3].fault_active is True
        assert site.inverters.units[3].fault_code == "hardware_lockout"
        assert site.inverters.running_count() < 4
        assert site.balance.inverter_capacity_kw < 40.0

    def test_inverter_fault_forces_curtailment(self, settings):
        site, _runner, _bus = run_scenario_capped(settings, "inverter_fault", "3h", step_s=10.0)
        assert site.stats.curtailed_energy_kwh > 0.0

    def test_thermal_derate_reduces_the_battery_envelope(self, settings):
        """EMS-T011."""
        site, _runner, _bus = run_scenario_capped(settings, "thermal_derate", "3h", step_s=10.0)
        charge_limit, discharge_limit = site.battery.limits()
        assert site.battery.cell_temperature_c > 40.0
        assert discharge_limit < site.config.battery.max_discharge_kw
        assert charge_limit < site.config.battery.max_charge_kw

    def test_pv_underperformance_loses_rows(self, settings):
        site, _runner, _bus = run_scenario_capped(
            settings, "pv_underperformance", "3h", step_s=10.0
        )
        assert sum(1 for row in site.solar.rows if not row.online) == 2
        assert site.solar.fault_active is True

    def test_rack_cooling_loss_heats_the_container(self, settings):
        """EMS-T014 / SDD 37.1 power_container_cooling_failed."""
        scenario = get_scenario("rack_cooling_loss")
        bus = InMemoryBus()
        site = scenario.build(bus, settings=settings)
        runner = ScenarioRunner(site, scenario)
        site.start()
        runner.pump()
        temperatures = []

        def on_step(current, _balance):
            runner.pump()
            bus.clear()
            temperatures.append(current.rack.container_temperature_c)

        site.run(duration_s=parse_duration("2h"), dt_s=10.0, pacer=SteppedPacer(), on_step=on_step)
        assert site.rack.cooling_failed is True  # repair does not land until 3 h
        assert temperatures[-1] > temperatures[0] + 10.0
        assert site.rack.container_temperature_c > 38.0
        # The thermal event reaches the electrical system.
        assert site.balance.inverter_capacity_kw < 40.0
        assert site.context.rack_inlet_temperature_c > 35.0

    def test_rack_cooling_loss_raises_the_rack_alarm(self, settings):
        site, _runner, bus = run_scenario(
            settings, "rack_cooling_loss", duration_s="2h", step_s=60.0
        )
        topic = topics.telemetry_topic("it.rack.power_container.01", "alarm_summary")
        assert parse_telemetry(bus.last(topic).payload).value in ("warning", "alarm", "critical")


# ------------------------------------------------------------------ black start


class TestBlackStartScenarios:
    def test_black_start_scenario_recovers_the_site(self, settings):
        """EMS-T012."""
        site, _runner, _bus = run_scenario_capped(settings, "black_start", "1h", step_s=2.0)
        assert site.context.black_start_stage == "complete"
        assert site.context.ac_bus_energized is True
        assert site.inverters.running_count() == 4
        assert site.battery.contactor_state == "closed"
        assert site.context.load_actual_kw > 0.0
        assert site.loads.group("energy.load.site.control_core_01").shed_state == "connected"

    def test_blackout_recovery_transitions_both_ways(self, settings):
        site, _runner, _bus = run_scenario_capped(settings, "blackout_recovery", "2h", step_s=2.0)
        assert site.stats.blackout_s > 0.0
        assert site.context.ac_bus_energized is True
        assert site.context.black_start_stage == "complete"
        assert any("ac_blackout" in e for e in site.stats.events)
        assert any("black_start_completed" in e for e in site.stats.events)


# ------------------------------------------------------------------ observability


class TestObservabilityScenarios:
    def test_comms_loss_creates_a_gap_then_recovers(self, settings):
        scenario = get_scenario("comms_loss")
        bus = InMemoryBus()
        site = scenario.build(bus, settings=settings)
        runner = ScenarioRunner(site, scenario)
        site.start()
        runner.pump()
        battery_topic = topics.telemetry_topic(
            "energy.battery_bank.power_container.01", "soc_pct"
        )
        seen: list[tuple[float, bool]] = []

        def on_step(current, _balance):
            runner.pump()
            seen.append((current.clock.elapsed_s, bus.last(battery_topic) is not None))
            bus.clear()

        site.run(duration_s=parse_duration("70m"), dt_s=10.0, pacer=SteppedPacer(), on_step=on_step)

        def fraction(low, high):
            window = [ok for at, ok in seen if low <= at < high]
            return sum(window) / max(len(window), 1)

        assert fraction(0, 1700) > 0.9  # publishing normally
        assert fraction(1900, 2900) == 0.0  # gateway silent
        assert fraction(3200, 4000) > 0.9  # publishing again

    def test_internet_loss_does_not_touch_local_control(self, settings):
        """EMS-T001: lose internet while normal -- no control loss."""
        scenario = get_scenario("comms_loss")
        bus = InMemoryBus()
        site = scenario.build(bus, settings=settings)
        runner = ScenarioRunner(site, scenario)
        site.start()
        runner.pump()

        def on_step(current, _balance):
            runner.pump()

        # Stop between the WAN drop (10 min) and its return (25 min).
        site.run(duration_s=parse_duration("20m"), dt_s=10.0, pacer=SteppedPacer(),
                 on_step=on_step)
        wan = topics.telemetry_topic("it.router.rack_01.isr4321_01", "wan_state")
        assert parse_telemetry(bus.last(wan).payload).value == "down"
        # Local control is untouched: the bus is up, loads are served, telemetry
        # keeps flowing and nothing shed itself.
        assert site.context.ac_bus_energized is True
        assert site.context.load_actual_kw > 0
        assert site.stats.unserved_energy_kwh == pytest.approx(0.0, abs=1e-6)
        assert [e for e in site.stats.events if "load_shed" in e] == []
        soc = topics.telemetry_topic("energy.battery_bank.power_container.01", "soc_pct")
        assert parse_telemetry(bus.last(soc).payload).quality == "good"

    def test_comms_loss_publishes_the_will_then_comes_back_online(self, settings):
        site, _runner, bus = run_scenario(settings, "comms_loss", duration_s="2h", step_s=60.0)
        topic = topics.availability_topic("energy.battery_bank.power_container.01")
        assert parse_availability(bus.retained[topic].payload).state == "online"
        offline = [
            m
            for m in bus.published
            if m.topic == topic and parse_availability(m.payload).state == "offline"
        ]
        assert offline  # the will fired while the gateway was silent

    def test_sensor_failure_degrades_quality(self, settings):
        """EMS-T003."""
        site, _runner, bus = run_scenario(settings, "sensor_failure", duration_s="2h", step_s=60.0)
        soc_topic = topics.telemetry_topic("energy.battery_bank.power_container.01", "soc_pct")
        soc = parse_telemetry(bus.last(soc_topic).payload)
        assert soc.quality == "uncertain"
        assert soc.value != pytest.approx(site.battery.soc_pct)

        temp_topic = topics.telemetry_topic(
            "energy.battery_bank.power_container.01", "temperature_cell_max_c"
        )
        temperature = parse_telemetry(bus.last(temp_topic).payload)
        assert temperature.quality == "bad"
        assert temperature.value == -999.0

    def test_sensor_failure_stops_one_point_updating(self, settings):
        scenario = get_scenario("sensor_failure")
        bus = InMemoryBus()
        site = scenario.build(bus, settings=settings)
        runner = ScenarioRunner(site, scenario)
        site.start()
        runner.pump()

        def on_step(current, _balance):
            runner.pump()

        site.run(duration_s=parse_duration("100m"), dt_s=30.0, pacer=SteppedPacer(), on_step=on_step)
        irradiance = topics.telemetry_topic(
            "energy.pv_array.agrivoltaic_field.01", "solar_irradiance_w_m2"
        )
        published = [m for m in bus.published if m.topic == irradiance]
        assert published
        last = parse_telemetry(published[-1].payload)
        # The last irradiance sample is from before the fault at 90 minutes.
        assert (site.clock.now() - last.ts).total_seconds() > 500


# ------------------------------------------------------------------ interlocks


class TestInterlockScenario:
    def test_tier0_shed_refused_produces_rejected_acks(self, settings):
        """EMS-T005: every refusal is a rejected ack carrying a reason."""
        site, runner, bus = run_scenario(settings, "tier0_shed_refused", step_s=10.0)
        assert site.stats.commands_received == 5
        assert site.stats.commands_rejected == 3
        assert site.stats.commands_accepted == 2

        reasons = {reason.split(":", 1)[-1] for reason in site.stats.rejections}
        assert "tier0_control_survival_never_shed" in reasons
        assert "battery_temperature_outside_safe_band" in reasons
        assert "rack_energized_temperature_governed" in reasons

        ack_topic = topics.command_ack_topic("energy.load.site.control_core_01", "shed")
        acks = [parse_command_ack(m.payload) for m in bus.published if m.topic == ack_topic]
        assert [ack.result for ack in acks] == ["rejected"]
        assert acks[0].reported_by == "simulator"

        # The refused load is still carrying power.
        assert site.loads.group("energy.load.site.control_core_01").power_kw > 0.0
        # The emergency-mode shed of rack cooling was permitted.
        assert site.loads.group("energy.load.site.rack_cooling_01").shed_state == "shed"
