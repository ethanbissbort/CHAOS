"""Site-level tests: wiring, energy balance, publishing and command handling.

These exercise the contract the rest of the platform depends on -- that every
topic is a ``homestead_twin.topics`` topic for a real register asset, that every
payload is a documented envelope, and that a command always gets an answer.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml

from homestead_twin import topics
from homestead_twin.envelope import (
    CommandEnvelope,
    parse_availability,
    parse_command_ack,
    parse_telemetry,
)
from homestead_twin.mqtt import InMemoryBus
from simulator.clock import RealTimePacer, SteppedPacer
from simulator.components.base import load_catalog
from simulator.components.battery import BatteryConfig
from simulator.components.generator import GeneratorConfig
from simulator.components.weather import WeatherConfig
from simulator.site import SimulatedSite, SiteConfig

UTC = dt.UTC
NOON = dt.datetime(2026, 6, 21, 17, 0, tzinfo=UTC)  # 12:00 local
MIDNIGHT = dt.datetime(2026, 6, 21, 5, 0, tzinfo=UTC)  # 00:00 local
SUNRISE = dt.datetime(2026, 6, 21, 13, 0, tzinfo=UTC)  # 08:00 local


@pytest.fixture()
def catalog(settings):
    return load_catalog(settings.data_dir)


def build_site(bus, settings, **overrides) -> SimulatedSite:
    config = SiteConfig(seed=42, **overrides)
    site = SimulatedSite(bus=bus, settings=settings, config=config)
    site.start()
    return site


def command(asset_id: str, name: str, value=None, **kwargs) -> CommandEnvelope:
    return CommandEnvelope(
        command_id=kwargs.pop("command_id", f"cmd-{asset_id}-{name}"),
        issued_by="tests.ems",
        asset_id=asset_id,
        command=name,
        value=value,
        reason=kwargs.pop("reason", "unit_test"),
        **kwargs,
    )


def publish_command(site: SimulatedSite, envelope: CommandEnvelope) -> None:
    """Send a command the way the EMS will: over the bus."""
    site.bus.publish(
        topics.command_topic(envelope.asset_id, envelope.command, site.base_topic),
        envelope.to_payload(),
        qos=1,
    )


def acks_for(bus: InMemoryBus, asset_id: str, name: str) -> list:
    topic = topics.command_ack_topic(asset_id, name)
    return [parse_command_ack(m.payload) for m in bus.published if m.topic == topic]


# ------------------------------------------------------------------ wiring


class TestConstruction:
    def test_site_covers_the_expected_register_assets(self, bus, settings):
        site = build_site(bus, settings)
        assets = set(site.asset_ids())
        expected = {
            "energy.pv_array.agrivoltaic_field.01",
            "energy.pv_row.agrivoltaic_field.01",
            "energy.pv_row.agrivoltaic_field.04",
            "energy.inverter.power_container.01",
            "energy.inverter.power_container.04",
            "energy.battery_bank.power_container.01",
            "energy.bms.power_container.01",
            "energy.generator.site.01",
            "energy.ats.power_container.site_01",
            "energy.panel.power_container.critical_01",
            "energy.panel.power_container.general_01",
            "energy.ups.rack_01.01",
            "energy.pdu.rack_01.switched_01",
            "it.environmental_monitor.power_container.netbotz_500_01",
            "it.server.rack_01.r740xd_01",
            "safety.safety_sensor.rack_01.nbes0307_01",
        }
        assert expected <= assets
        # All twelve load groups.
        assert len([a for a in assets if a.startswith("energy.load.site.")]) == 12

    def test_every_asset_is_in_the_register(self, bus, settings, catalog):
        site = build_site(bus, settings)
        for asset_id in site.asset_ids():
            assert catalog.has_asset(asset_id)

    def test_every_point_is_registry_legitimate(self, bus, settings, catalog):
        site = build_site(bus, settings)
        for point_id, spec in site.specs.items():
            assert spec.name in catalog.allowed_points(spec.asset_id), point_id

    def test_point_count_is_substantial(self, bus, settings):
        """The design package binds 245 points; the rig should cover well past that."""
        site = build_site(bus, settings)
        assert len(site.point_ids()) > 245

    def test_every_bound_point_is_simulated(self, bus, settings):
        """``point_bindings.yaml`` is the agreed target set: cover all of it."""
        bindings = yaml.safe_load((settings.data_dir / "point_bindings.yaml").read_text())["bindings"]
        bound = {binding["point_id"] for binding in bindings}
        site = build_site(bus, settings)
        assert bound <= set(site.point_ids())


# ------------------------------------------------------------------ publishing


class TestPublishing:
    def test_every_topic_is_a_topics_module_topic(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        for _ in range(20):
            site.step(5.0)
        assert bus.published
        for message in bus.published:
            if message.topic.endswith("/availability"):
                envelope = parse_availability(message.payload)
                assert message.topic == topics.availability_topic(envelope.asset_id)
                continue
            envelope = parse_telemetry(message.payload)
            assert message.topic == topics.telemetry_topic(envelope.asset_id, envelope.point)

    def test_payloads_are_valid_telemetry_envelopes(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        site.step(5.0)
        telemetry = [m for m in bus.published if not m.topic.endswith("/availability")]
        assert telemetry
        for message in telemetry:
            payload = json.loads(message.text)
            assert payload["schema_version"] == 1
            assert payload["source"] == "simulator"
            envelope = parse_telemetry(message.payload)
            assert envelope.quality in ("good", "uncertain", "bad", "stale")
            assert envelope.sequence is not None

    def test_units_come_from_the_point_dictionary(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        site.step(5.0)
        soc = bus.last(topics.telemetry_topic("energy.battery_bank.power_container.01", "soc_pct"))
        assert parse_telemetry(soc.payload).unit == "%"
        power = bus.last(topics.telemetry_topic("energy.battery_bank.power_container.01", "power_kw"))
        assert parse_telemetry(power.payload).unit == "kW"

    def test_availability_is_retained_online_then_offline(self, bus, settings):
        site = build_site(bus, settings)
        topic = topics.availability_topic("energy.battery_bank.power_container.01")
        assert topic in bus.retained
        assert parse_availability(bus.retained[topic].payload).state == "online"
        assert bus.retained[topic].retain is True
        site.step(5.0)
        site.stop()
        assert parse_availability(bus.retained[topic].payload).state == "offline"

    def test_publish_intervals_are_honoured(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        soc_topic = topics.telemetry_topic("energy.battery_bank.power_container.01", "soc_pct")
        pdu_topic = topics.telemetry_topic("energy.pdu.rack_01.switched_01", "power_total_kw")
        for _ in range(60):  # 60 s at 1 s steps
            site.step(1.0)
        soc_count = sum(1 for m in bus.published if m.topic == soc_topic)
        pdu_count = sum(1 for m in bus.published if m.topic == pdu_topic)
        assert 11 <= soc_count <= 14  # 5 s binding interval
        assert 2 <= pdu_count <= 4  # 30 s binding interval

    def test_discrete_points_publish_on_change(self, bus, settings):
        """A state change must not wait for the next interval."""
        site = build_site(bus, settings, start=NOON)
        site.step(1.0)
        bus.clear()
        site.inverters.inject_fault(0, "dc_overvoltage")
        site.step(1.0)
        topic = topics.telemetry_topic("energy.inverter.power_container.01", "state_operating")
        message = bus.last(topic)
        assert message is not None
        assert parse_telemetry(message.payload).value == "fault"


# ------------------------------------------------------------------ energy balance


class TestEnergyBalance:
    def test_pv_is_zero_at_night_and_peaks_near_solar_noon(self, bus, settings):
        site = build_site(bus, settings, start=MIDNIGHT, weather=WeatherConfig(cloud_mode="clear"))
        peak_kw, peak_hour = 0.0, None
        readings = []
        for _ in range(288):  # 24 h at 5 min
            site.step(300.0)
            bus.clear()
            hour = site.clock.hour_of_day()
            readings.append((hour, site.context.pv_available_dc_kw))
            if site.context.pv_available_dc_kw > peak_kw:
                peak_kw = site.context.pv_available_dc_kw
                peak_hour = hour
        night = [kw for hour, kw in readings if hour < 4.0 or hour > 22.0]
        assert night and max(night) == 0.0
        assert 11.5 <= peak_hour <= 13.0
        assert 30.0 < peak_kw < 46.0  # 45 kWdc nameplate

    def test_energy_balances_every_step(self, bus, settings):
        site = build_site(bus, settings, start=MIDNIGHT)
        for _ in range(200):
            balance = site.step(60.0)
            bus.clear()
            supply = balance.pv_ac_kw + balance.generator_kw - balance.battery_kw
            demand = balance.load_kw - balance.unserved_kw
            assert supply == pytest.approx(demand, abs=1e-6)

    def test_battery_energy_is_conserved_over_a_simulated_day(self, bus, settings):
        site = build_site(bus, settings, start=MIDNIGHT)
        for _ in range(288):
            site.step(300.0)
            bus.clear()
            assert 0.0 <= site.battery.soc_pct <= 100.0
        assert site.battery.conservation_error_kwh() == pytest.approx(0.0, abs=1e-6)

    def test_battery_never_exceeds_the_bms_limits(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        for _ in range(200):
            balance = site.step(30.0)
            bus.clear()
            charge_limit, discharge_limit = site.battery.limits()
            if balance.battery_kw >= 0:
                assert balance.battery_kw <= charge_limit + 1e-6
            else:
                assert -balance.battery_kw <= discharge_limit + 1e-6

    def test_full_battery_forces_curtailment(self, bus, settings):
        site = build_site(
            bus,
            settings,
            start=NOON,
            battery=BatteryConfig(initial_soc_pct=99.9),
            weather=WeatherConfig(cloud_mode="clear"),
        )
        curtailed = 0.0
        for _ in range(60):
            balance = site.step(30.0)
            bus.clear()
            curtailed += balance.curtailed_kw
        assert curtailed > 0.0
        assert site.context.pv_delivered_dc_kw < site.context.pv_available_dc_kw

    def test_inverter_capacity_clips_a_big_array(self, bus, settings):
        site = build_site(
            bus,
            settings,
            start=NOON,
            battery=BatteryConfig(initial_soc_pct=50.0),
            weather=WeatherConfig(cloud_mode="clear"),
        )
        for _ in range(20):
            balance = site.step(30.0)
            bus.clear()
            assert balance.pv_ac_kw <= balance.inverter_capacity_kw + 1e-6

    def test_losing_every_inverter_de_energizes_the_bus(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        site.step(5.0)
        for index in range(4):
            site.inverters.inject_fault(index, "dc_overvoltage")
        site.step(5.0)
        assert site.context.ac_bus_energized is False
        assert site.balance.unserved_kw > 0  # the collapsing step reports the shortfall
        site.step(5.0)  # by the next step every group has locked out
        assert site.context.load_actual_kw == 0.0
        assert all(g.shed_state == "locked_out" for g in site.loads.groups.values())

    def test_conversion_losses_are_real(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        for _ in range(10):
            balance = site.step(30.0)
            bus.clear()
        assert balance.pv_delivered_dc_kw >= balance.pv_ac_kw


# ------------------------------------------------------------------ commands


class TestCommands:
    def test_accepted_command_gets_accepted_then_succeeded(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        site.step(5.0)
        bus.clear()
        publish_command(site, command("energy.load.site.opportunistic_compute_01", "shed", reason="surplus"))
        results = [ack.result for ack in acks_for(bus, "energy.load.site.opportunistic_compute_01", "shed")]
        assert results == ["accepted", "succeeded"]
        assert site.stats.commands_received == 1
        assert site.stats.commands_accepted == 1

    def test_tier0_shed_is_rejected_with_a_reason(self, bus, settings):
        """SDD 31.2 Tier 0 / SDD 39 EMS-T005."""
        site = build_site(bus, settings, start=NOON)
        site.step(5.0)
        bus.clear()
        publish_command(
            site,
            command("energy.load.site.control_core_01", "shed", reason="critical_reserve"),
        )
        acks = acks_for(bus, "energy.load.site.control_core_01", "shed")
        assert [ack.result for ack in acks] == ["rejected"]
        assert acks[0].detail == "tier0_control_survival_never_shed"
        assert site.stats.commands_rejected == 1
        site.step(30.0)
        assert site.loads.group("energy.load.site.control_core_01").power_kw > 0

    def test_generator_start_rejected_when_a_permissive_fails(self, bus, settings):
        site = build_site(bus, settings, start=MIDNIGHT, generator=GeneratorConfig(mode="manual"))
        site.step(5.0)
        bus.clear()
        publish_command(
            site,
            command(
                "energy.generator.site.01",
                "generator_start_request",
                True,
                reason="critical_reserve",
            ),
        )
        acks = acks_for(bus, "energy.generator.site.01", "generator_start_request")
        assert [ack.result for ack in acks] == ["rejected"]
        assert acks[0].detail == "mode_not_automatic:manual"
        assert site.generator.state == "off"

    def test_generator_start_request_runs_the_native_sequence(self, bus, settings):
        site = build_site(
            bus,
            settings,
            start=MIDNIGHT,
            generator=GeneratorConfig(
                crank_time_s=8, warmup_time_s=30, transfer_time_s=5, minimum_run_time_s=120
            ),
            battery=BatteryConfig(initial_soc_pct=15.0),
        )
        site.step(5.0)
        publish_command(
            site,
            command("energy.generator.site.01", "generator_start_request", True, reason="reserve"),
        )
        for _ in range(30):
            site.step(5.0)
        assert site.generator.loaded
        assert site.generator.output_kw > 0
        assert site.balance.generator_kw > 0

    def test_unknown_asset_and_unknown_command_are_rejected(self, bus, settings):
        site = build_site(bus, settings)
        ack = site.apply_command(command("energy.load.site.control_core_01", "levitate"))
        assert ack.result == "rejected"
        assert "unsupported_command" in ack.detail
        ack = site.apply_command(command("water.pump.well.01", "start"))
        assert ack.result == "rejected"
        assert ack.detail == "unknown_asset"

    def test_expired_command_is_not_applied(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        site.step(5.0)
        expired = command(
            "energy.load.site.opportunistic_compute_01",
            "shed",
            expires_at=site.clock.now() - dt.timedelta(seconds=1),
        )
        ack = site.apply_command(expired)
        assert ack.result == "expired"
        assert site.loads.group("energy.load.site.opportunistic_compute_01").shed_state == ("connected")

    def test_malformed_command_payload_is_ignored(self, bus, settings):
        site = build_site(bus, settings)
        bus.publish(topics.command_topic("energy.load.site.spa_01", "shed"), b"{not json", qos=1)
        assert site.stats.commands_received == 0  # never counted, never crashed

    def test_power_budget_command_throttles_a_load(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        site.step(5.0)
        publish_command(
            site,
            command("energy.load.site.opportunistic_compute_01", "power_budget_kw", 0.4),
        )
        for _ in range(4):
            site.step(5.0)
        assert site.loads.group("energy.load.site.opportunistic_compute_01").power_kw <= 0.4

    def test_command_ack_topic_matches_the_contract(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        site.step(5.0)
        bus.clear()
        publish_command(site, command("energy.load.site.spa_01", "shed"))
        ack_topics = {m.topic for m in bus.published if m.topic.endswith("/ack")}
        assert ack_topics == {topics.command_ack_topic("energy.load.site.spa_01", "shed")}


# ------------------------------------------------------------------ fault injection


class TestFaultInjection:
    def test_frozen_sensor_keeps_publishing_a_stale_value(self, bus, settings):
        site = build_site(bus, settings, start=MIDNIGHT)
        point = "energy.battery_bank.power_container.01/soc_pct"
        topic = topics.telemetry_topic("energy.battery_bank.power_container.01", "soc_pct")
        for _ in range(10):
            site.step(30.0)
        before = parse_telemetry(bus.last(topic).payload).value
        site.inject_sensor_fault(point, mode="frozen", quality="uncertain")
        for _ in range(40):
            site.step(30.0)
        message = parse_telemetry(bus.last(topic).payload)
        assert message.value == before
        assert message.quality == "uncertain"
        # The real SOC has moved on underneath the frozen reading.
        assert site.battery.soc_pct != before

    def test_bad_sensor_publishes_out_of_range_with_bad_quality(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        point = "energy.battery_bank.power_container.01/temperature_cell_max_c"
        site.inject_sensor_fault(point, mode="bad", quality="bad")
        site.step(5.0)
        topic = topics.telemetry_topic("energy.battery_bank.power_container.01", "temperature_cell_max_c")
        envelope = parse_telemetry(bus.last(topic).payload)
        assert envelope.quality == "bad"
        assert envelope.value == -999.0

    def test_stale_sensor_stops_publishing_one_point_only(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        site.step(5.0)
        site.inject_sensor_fault("energy.pv_array.agrivoltaic_field.01/solar_irradiance_w_m2", mode="stale")
        bus.clear()
        for _ in range(20):
            site.step(30.0)
        irradiance = topics.telemetry_topic("energy.pv_array.agrivoltaic_field.01", "solar_irradiance_w_m2")
        power = topics.telemetry_topic("energy.pv_array.agrivoltaic_field.01", "power_dc_kw")
        assert bus.last(irradiance) is None
        assert bus.last(power) is not None

    def test_unknown_point_cannot_be_faulted(self, bus, settings):
        site = build_site(bus, settings)
        with pytest.raises(KeyError):
            site.inject_sensor_fault("energy.battery_bank.power_container.01/not_a_point")

    def test_comms_loss_silences_a_gateway_then_publishes_its_will(self, bus, settings):
        site = build_site(bus, settings, start=NOON, will_delay_s=30.0)
        site.step(5.0)
        site.lose_comms("power")
        bus.clear()
        for _ in range(3):
            site.step(5.0)
        assert not [m for m in bus.published if "battery_bank" in m.topic and "availability" not in m.topic]
        # The will fires once the keepalive window elapses.
        for _ in range(6):
            site.step(5.0)
        will_topic = topics.availability_topic("energy.battery_bank.power_container.01")
        assert parse_availability(bus.retained[will_topic].payload).state == "offline"
        # Other gateways are unaffected.
        assert [m for m in bus.published if "netbotz" in m.topic]

    def test_comms_restore_republishes_online(self, bus, settings):
        site = build_site(bus, settings, start=NOON, will_delay_s=30.0)
        site.step(5.0)
        site.lose_comms("rack")
        for _ in range(10):
            site.step(5.0)
        site.restore_comms("rack")
        bus.clear()
        site.step(5.0)
        topic = topics.availability_topic("it.server.rack_01.r740xd_01")
        assert parse_availability(bus.retained[topic].payload).state == "online"
        assert [m for m in bus.published if "netbotz" in m.topic]

    def test_unknown_gateway_rejected(self, bus, settings):
        site = build_site(bus, settings)
        with pytest.raises(KeyError):
            site.lose_comms("greenhouse")


# ------------------------------------------------------------------ black start


class TestBlackStart:
    def test_site_can_start_de_energized(self, bus, settings):
        site = build_site(bus, settings, start=NOON, start_de_energized=True)
        site.step(5.0)
        assert site.context.ac_bus_energized is False
        assert site.inverters.running_count() == 0
        assert site.battery.contactor_state == "open"
        assert site.context.load_actual_kw == 0.0

    def test_black_start_restores_the_critical_bus_before_general_loads(self, bus, settings):
        site = build_site(bus, settings, start=NOON, start_de_energized=True)
        site.step(2.0)
        assert site.request_black_start() is True
        control_core = "energy.load.site.control_core_01"
        greenhouse = "energy.load.site.greenhouse_lighting_01"
        control_core_restored_at = None
        greenhouse_restored_at = None
        for _ in range(400):
            site.step(2.0)
            bus.clear()
            if control_core_restored_at is None and site.loads.group(control_core).shed_state == "connected":
                control_core_restored_at = site.clock.elapsed_s
            if greenhouse_restored_at is None and site.loads.group(greenhouse).shed_state == "connected":
                greenhouse_restored_at = site.clock.elapsed_s
        assert control_core_restored_at is not None
        assert greenhouse_restored_at is not None
        assert control_core_restored_at < greenhouse_restored_at
        assert site.context.black_start_stage == "complete"
        assert site.context.ac_bus_energized is True

    def test_attended_loads_do_not_restart_unattended(self, bus, settings):
        """SDD 35.3: no unattended restart of workshop or spa equipment."""
        site = build_site(bus, settings, start=NOON, start_de_energized=True)
        site.step(2.0)
        site.request_black_start()
        for _ in range(400):
            site.step(2.0)
            bus.clear()
        assert site.loads.group("energy.load.site.workshop_heavy_01").shed_state == "locked_out"
        assert site.loads.group("energy.load.site.spa_01").shed_state == "locked_out"

    def test_black_start_refused_while_the_bms_is_faulted(self, bus, settings):
        site = build_site(bus, settings, start=NOON, start_de_energized=True)
        site.battery.inject_fault("cell_overvoltage")
        assert site.request_black_start() is False

    def test_black_start_publishes_stage_events(self, bus, settings):
        site = build_site(bus, settings, start=NOON, start_de_energized=True)
        site.step(2.0)
        site.request_black_start()
        for _ in range(200):
            site.step(2.0)
        events = [e for e in site.stats.events if "black_start" in e]
        assert any("black_start_initiated" in e for e in events)
        assert any("black_start_stage" in e for e in events)
        assert any("black_start_completed" in e for e in events)


# ------------------------------------------------------------------ determinism


class TestDeterminism:
    def test_same_seed_produces_identical_traffic(self, settings):
        def run():
            bus = InMemoryBus()
            site = SimulatedSite(bus=bus, settings=settings, config=SiteConfig(seed=99, start=NOON))
            site.start()
            for _ in range(40):
                site.step(15.0)
            return [(m.topic, m.text) for m in bus.published]

        assert run() == run()

    def test_different_seeds_diverge(self, settings):
        def run(seed):
            bus = InMemoryBus()
            site = SimulatedSite(
                bus=bus,
                settings=settings,
                config=SiteConfig(seed=seed, start=NOON).seeded(seed),
            )
            site.start()
            for _ in range(40):
                site.step(15.0)
            return [(m.topic, m.text) for m in bus.published]

        assert run(1) != run(2)

    def test_step_size_does_not_change_the_energy_outcome(self, settings):
        """Coarse and fine steps must agree; a rig that drifts is useless.

        The per-step process noise is not identical between step sizes, so the
        agreement is close rather than exact -- but the integrated energy and
        the resulting SOC must not depend on how the day was chopped up.
        """

        def energy(step_s):
            bus = InMemoryBus()
            site = SimulatedSite(
                bus=bus,
                settings=settings,
                config=SiteConfig(seed=7, start=SUNRISE, weather=WeatherConfig(cloud_mode="clear")),
            )
            site.start()
            elapsed = 0.0
            while elapsed < 28800:  # 08:00 -> 16:00 local
                site.step(step_s)
                bus.clear()
                elapsed += step_s
            return site.stats.pv_energy_kwh, site.battery.soc_pct

        coarse = energy(300.0)
        fine = energy(60.0)
        assert coarse[0] > 50.0  # a real amount of energy, not a rounding artefact
        assert coarse[0] == pytest.approx(fine[0], rel=0.05)
        assert coarse[1] == pytest.approx(fine[1], abs=1.0)


# ------------------------------------------------------------------ run loop


class TestRunLoop:
    def test_run_advances_the_clock_without_sleeping(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        pacer = SteppedPacer()
        site.run(duration_s=600, dt_s=30.0, pacer=pacer)
        assert site.clock.elapsed_s == pytest.approx(600.0)
        assert site.stats.steps == 20

    def test_run_paces_when_asked(self, bus, settings):
        slept: list[float] = []
        ticks = iter([float(i) * 0.0 for i in range(200)])
        pacer = RealTimePacer(speed=100.0, sleeper=slept.append, monotonic=lambda: next(ticks, 0.0))
        site = build_site(bus, settings, start=NOON)
        site.run(duration_s=300, dt_s=30.0, pacer=pacer)
        assert len(slept) == 10
        assert slept[0] == pytest.approx(0.3)

    def test_run_handles_a_partial_final_step(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        site.run(duration_s=70, dt_s=30.0, pacer=SteppedPacer())
        assert site.clock.elapsed_s == pytest.approx(70.0)
        assert site.stats.steps == 3

    def test_on_step_callback_sees_every_step(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        seen: list[float] = []
        site.run(
            duration_s=120,
            dt_s=30.0,
            pacer=SteppedPacer(),
            on_step=lambda s, b: seen.append(s.clock.elapsed_s),
        )
        assert seen == [30.0, 60.0, 90.0, 120.0]

    def test_snapshot_reports_the_interesting_state(self, bus, settings):
        site = build_site(bus, settings, start=NOON)
        site.step(30.0)
        snapshot = site.snapshot()
        assert set(snapshot) >= {
            "soc_pct",
            "pv_ac_kw",
            "load_kw",
            "critical_load_kw",
            "curtailed_kw",
            "generator_state",
            "ac_bus_energized",
            "container_temperature_c",
        }
