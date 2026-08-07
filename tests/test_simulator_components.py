"""Component-level tests for the simulated homestead.

These are physics and state-machine tests: no bus, no envelopes, no broker.
Every one of them is deterministic -- seeded generators, virtual time, no
``time.sleep``.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pytest

from homestead_twin.envelope import CommandEnvelope
from simulator.clock import (
    DEFAULT_START,
    RealTimePacer,
    SimClock,
    SteppedPacer,
    build_pacer,
    format_duration,
    hour_of_day,
    parse_duration,
)
from simulator.components.base import SiteContext, UnknownPointError, load_catalog
from simulator.components.battery import BatteryBank, BatteryConfig
from simulator.components.generator import (
    SOURCE_A,
    SOURCE_B,
    STATE_COOLDOWN,
    STATE_LOCKOUT,
    STATE_OFF,
    STATE_RUNNING,
    Generator,
    GeneratorConfig,
)
from simulator.components.inverter import InverterConfig, InverterFarm
from simulator.components.loads import CONNECTED, LOCKED_OUT, SHED, LoadBank
from simulator.components.rack import ServerRack
from simulator.components.solar import (
    SolarArray,
    clear_sky_poa_w_m2,
    cloud_attenuation,
)
from simulator.components.weather import Weather, WeatherConfig

UTC = dt.UTC


@pytest.fixture()
def catalog(settings):
    return load_catalog(settings.data_dir)


@pytest.fixture()
def context():
    return SiteContext()


def make_command(asset_id: str, command: str, value=None, **kwargs) -> CommandEnvelope:
    return CommandEnvelope(
        command_id=f"test-{asset_id}-{command}",
        issued_by="tests",
        asset_id=asset_id,
        command=command,
        value=value,
        reason=kwargs.pop("reason", "unit_test"),
        **kwargs,
    )


# ---------------------------------------------------------------- clock


class TestClock:
    def test_advance_is_pure_arithmetic(self):
        clock = SimClock(start=DEFAULT_START)
        assert clock.now() == DEFAULT_START
        clock.advance(3600)
        assert clock.now() == DEFAULT_START + dt.timedelta(hours=1)
        assert clock.elapsed_s == 3600
        assert clock.steps == 1

    def test_negative_step_rejected(self):
        with pytest.raises(ValueError):
            SimClock().advance(-1)

    def test_local_time_uses_the_configured_offset(self):
        clock = SimClock(start=dt.datetime(2026, 6, 21, 17, 0, tzinfo=UTC), utc_offset_h=-5.0)
        assert clock.hour_of_day() == pytest.approx(12.0)
        assert hour_of_day(clock.now(), -5.0) == pytest.approx(12.0)

    def test_a_whole_day_costs_no_wall_clock_time(self):
        """A 24 h day in one loop: the point of a virtual clock."""
        clock = SimClock()
        for _ in range(288):
            clock.advance(300)
        assert clock.elapsed_s == 86400

    @pytest.mark.parametrize(
        ("text", "seconds"),
        [("24h", 86400), ("90m", 5400), ("30s", 30), ("1d", 86400), ("1h30m", 5400), ("600", 600)],
    )
    def test_parse_duration(self, text, seconds):
        assert parse_duration(text) == seconds

    def test_parse_duration_rejects_nonsense(self):
        with pytest.raises(ValueError):
            parse_duration("soon")

    def test_format_duration(self):
        assert format_duration(86400) == "1d"
        assert format_duration(5400) == "1h30m"

    def test_stepped_pacer_never_sleeps(self):
        assert isinstance(build_pacer(None), SteppedPacer)
        assert isinstance(build_pacer(0), SteppedPacer)
        assert isinstance(build_pacer(math.inf), SteppedPacer)

    def test_real_time_pacer_sleeps_the_remaining_budget(self):
        slept: list[float] = []
        ticks = iter([0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0])
        pacer = RealTimePacer(speed=10.0, sleeper=slept.append, monotonic=lambda: next(ticks))
        pacer.pace(100.0)  # budget 10 s wall, nothing spent yet
        assert slept and slept[0] == pytest.approx(10.0)

    def test_real_time_pacer_does_not_accumulate_debt(self):
        slept: list[float] = []
        clock = iter([0.0, 100.0, 200.0])
        pacer = RealTimePacer(speed=1.0, sleeper=slept.append, monotonic=lambda: next(clock))
        pacer.pace(1.0)
        pacer.pace(1.0)  # 100 s already spent against a 1 s budget
        assert slept == [1.0]


# ---------------------------------------------------------------- catalog


class TestPointCatalog:
    def test_allowed_points_are_the_documented_union(self, catalog):
        allowed = catalog.allowed_points("energy.battery_bank.power_container.01")
        # class defaults + common_commandable profile + explicit bindings
        assert {"soc_pct", "charge_limit_kw"} <= allowed  # class defaults
        assert {"mode_actual", "runtime_total_h"} <= allowed  # profile
        assert {"charge_permissive", "contactor_state"} <= allowed  # bindings
        assert "flow_total_m3" not in allowed

    def test_spec_carries_the_binding_interval(self, catalog):
        spec = catalog.spec("energy.battery_bank.power_container.01", "soc_pct")
        assert spec.publish_interval_s == 5.0  # point_bindings.yaml
        assert spec.unit == "%"
        assert spec.point_id == "energy.battery_bank.power_container.01/soc_pct"

    def test_unbound_points_fall_back_to_a_class_cadence(self, catalog):
        spec = catalog.spec("energy.generator.site.01", "power_output_kw")
        assert spec.publish_interval_s == 5.0

    def test_config_and_text_points_are_published_slowly(self, catalog):
        spec = catalog.spec("energy.load.site.spa_01", "minimum_off_time_s")
        assert spec.point_class == "CFG"
        assert spec.publish_interval_s >= 60.0

    def test_undeclared_point_is_refused(self, catalog):
        with pytest.raises(UnknownPointError):
            catalog.spec("energy.pv_row.agrivoltaic_field.01", "soc_pct")

    def test_components_only_declare_registry_legitimate_points(self, catalog):
        """Every point every component publishes must be in the asset's union."""
        components = [
            SolarArray(catalog),
            BatteryBank(catalog),
            InverterFarm(catalog),
            Generator(catalog),
            LoadBank(catalog),
            ServerRack(catalog),
            Weather(catalog),
        ]
        for component in components:
            for spec in component.points():
                assert catalog.has_asset(spec.asset_id)
                assert spec.name in catalog.allowed_points(spec.asset_id)

    def test_no_component_reads_the_wall_clock(self):
        """Determinism guard: components take ``now``; they never fetch it."""
        package = Path(__file__).resolve().parents[1] / "src" / "simulator" / "components"
        for path in package.glob("*.py"):
            source = path.read_text()
            for forbidden in ("datetime.now(", "dt.datetime.now(", "time.time(", "time.sleep("):
                assert forbidden not in source, f"{path.name} uses {forbidden}"


# ---------------------------------------------------------------- weather


class TestWeather:
    def test_same_seed_gives_the_same_weather(self, catalog):
        def sample():
            weather = Weather(catalog, WeatherConfig(seed=7, cloud_mode="variable"))
            context = SiteContext()
            clock = SimClock()
            values = []
            for _ in range(60):
                weather.step(clock.advance(60), 60, context)
                values.append((context.ambient_temperature_c, context.cloud_cover))
            return values

        assert sample() == sample()

    def test_different_seeds_diverge(self, catalog):
        def sample(seed):
            weather = Weather(catalog, WeatherConfig(seed=seed, cloud_mode="variable"))
            context = SiteContext()
            clock = SimClock()
            for _ in range(60):
                weather.step(clock.advance(60), 60, context)
            return context.cloud_cover

        assert sample(1) != sample(2)

    def test_summer_is_warmer_than_winter(self, catalog):
        def mean_temperature(month):
            weather = Weather(catalog, WeatherConfig(seed=3))
            context = SiteContext()
            start = dt.datetime(2026, month, 15, 17, 0, tzinfo=UTC)
            clock = SimClock(start=start)
            weather.step(clock.advance(0.1), 0.1, context)
            return context.ambient_temperature_c

        assert mean_temperature(7) > mean_temperature(1) + 15.0

    def test_passing_clouds_are_periodic(self, catalog):
        config = WeatherConfig(seed=1, cloud_mode="passing_clouds", cloud_period_s=600, cloud_duration_s=240)
        weather = Weather(catalog, config)
        context = SiteContext()
        clock = SimClock()
        series = []
        for _ in range(240):  # 20 minutes at 5 s = two full cloud cycles
            weather.step(clock.advance(5), 5, context)
            series.append(context.cloud_cover)
        assert max(series) > 0.8
        assert min(series) < 0.2
        # The pattern repeats with the configured period (600 s = 120 samples).
        assert series[10] == pytest.approx(series[130], abs=0.05)

    def test_mode_switch_changes_the_regime(self, catalog):
        weather = Weather(catalog, WeatherConfig(seed=1, cloud_mode="clear"))
        context = SiteContext()
        clock = SimClock()
        for _ in range(20):
            weather.step(clock.advance(60), 60, context)
        clear = context.cloud_cover
        weather.set_mode("overcast")
        for _ in range(40):
            weather.step(clock.advance(60), 60, context)
        assert context.cloud_cover > clear + 0.5

    def test_unknown_mode_rejected(self, catalog):
        with pytest.raises(ValueError):
            Weather(catalog).set_mode("hurricane")


# ---------------------------------------------------------------- solar


class TestSolar:
    def test_night_is_dark(self):
        midnight_local = dt.datetime(2026, 6, 21, 5, 0, tzinfo=UTC)
        poa, elevation = clear_sky_poa_w_m2(midnight_local, 44.3, -78.3, 30.0, 0.22)
        assert poa == 0.0
        assert elevation < 0

    def test_peak_is_near_solar_noon(self):
        best_poa, best_time = 0.0, None
        for minute in range(0, 24 * 60, 5):
            when = dt.datetime(2026, 6, 21, tzinfo=UTC) + dt.timedelta(minutes=minute)
            poa, _ = clear_sky_poa_w_m2(when, 44.3, -78.3, 30.0, 0.22)
            if poa > best_poa:
                best_poa, best_time = poa, when
        # Solar noon at 78.3 W is ~12:15 local; allow half an hour either side.
        assert 11.75 <= hour_of_day(best_time, -5.0) <= 12.75
        assert 900 < best_poa < 1150

    def test_winter_peak_is_lower_than_summer(self):
        def peak(month, day):
            return max(
                clear_sky_poa_w_m2(
                    dt.datetime(2026, month, day, tzinfo=UTC) + dt.timedelta(minutes=m),
                    44.3,
                    -78.3,
                    30.0,
                    0.22,
                )[0]
                for m in range(0, 24 * 60, 10)
            )

        assert peak(1, 15) < peak(6, 21)

    def test_cloud_attenuation_is_monotonic_and_non_linear(self):
        assert cloud_attenuation(0.0) == pytest.approx(1.0)
        assert cloud_attenuation(0.3) > 0.9  # thin cover is nearly free
        assert cloud_attenuation(1.0) == pytest.approx(0.25)
        values = [cloud_attenuation(c / 10) for c in range(11)]
        assert values == sorted(values, reverse=True)

    def test_array_power_zero_at_night(self, catalog, context):
        solar = SolarArray(catalog)
        clock = SimClock(start=dt.datetime(2026, 6, 21, 5, 0, tzinfo=UTC))
        available = solar.compute(clock.advance(1), 1, context)
        assert available == 0.0
        solar.apply_delivered(0.0, 1, clock.now())
        values = solar.step(clock.now(), 1, context)
        assert values["energy.pv_array.agrivoltaic_field.01/power_dc_kw"] == 0.0

    def test_array_peaks_near_nameplate_at_noon(self, catalog, context):
        solar = SolarArray(catalog)
        clock = SimClock(start=dt.datetime(2026, 6, 21, 17, 15, tzinfo=UTC))
        context.ambient_temperature_c = 25.0
        available = solar.compute(clock.advance(1), 1, context)
        # 45 kWdc nameplate, ~1020 W/m2 POA, warm cells: high 30s.
        assert 33.0 < available < 46.0

    def test_cloud_reduces_output(self, catalog):
        clock = SimClock(start=dt.datetime(2026, 6, 21, 17, 15, tzinfo=UTC))
        now = clock.advance(1)

        def output(cloud):
            solar = SolarArray(catalog)
            context = SiteContext(cloud_cover=cloud, ambient_temperature_c=25.0)
            return solar.compute(now, 1, context)

        assert output(0.95) < 0.4 * output(0.0)

    def test_hot_cells_derate(self, catalog):
        clock = SimClock(start=dt.datetime(2026, 6, 21, 17, 15, tzinfo=UTC))
        now = clock.advance(1)

        def output(ambient):
            solar = SolarArray(catalog)
            context = SiteContext(ambient_temperature_c=ambient, wind_speed_m_s=1.0)
            return solar.compute(now, 1, context)

        assert output(38.0) < output(10.0)

    def test_offline_row_removes_its_share(self, catalog, context):
        solar = SolarArray(catalog)
        clock = SimClock(start=dt.datetime(2026, 6, 21, 17, 15, tzinfo=UTC))
        now = clock.advance(1)
        context.ambient_temperature_c = 20.0
        full = solar.compute(now, 1, context)
        solar.set_row_available(2, False)
        degraded = solar.compute(now, 1, context)
        assert degraded == pytest.approx(full * 0.75, rel=0.02)
        solar.apply_delivered(degraded, 1, now)
        values = solar.step(now, 1, context)
        assert values["energy.pv_array.agrivoltaic_field.01/availability_state"] == "degraded"
        assert values["energy.pv_row.agrivoltaic_field.03/power_dc_kw"] == 0.0

    def test_delivered_never_exceeds_available(self, catalog, context):
        solar = SolarArray(catalog)
        clock = SimClock(start=dt.datetime(2026, 6, 21, 17, 15, tzinfo=UTC))
        now = clock.advance(1)
        available = solar.compute(now, 1, context)
        solar.apply_delivered(available * 10, 1, now)
        assert solar.delivered_dc_kw == pytest.approx(available)

    def test_daily_energy_resets_at_local_midnight(self, catalog, context):
        solar = SolarArray(catalog)
        clock = SimClock(start=dt.datetime(2026, 6, 21, 16, 0, tzinfo=UTC))
        for _ in range(60):
            now = clock.advance(600)
            solar.compute(now, 600, context)
            solar.apply_delivered(solar.available_dc_kw, 600, now)
        assert solar.energy_today_kwh > 0
        peak_day = solar.energy_today_kwh
        for _ in range(60):  # roll past local midnight
            now = clock.advance(600)
            solar.compute(now, 600, context)
            solar.apply_delivered(solar.available_dc_kw, 600, now)
        assert solar.energy_today_kwh < peak_day


# ---------------------------------------------------------------- battery


class TestBattery:
    def test_soc_stays_within_bounds(self, catalog):
        battery = BatteryBank(catalog, BatteryConfig(initial_soc_pct=50.0))
        for _ in range(1500):  # absurd charge request, ~25 h of it
            battery.integrate(1000.0, 60)
            assert 0.0 <= battery.soc_pct <= 100.0
        assert battery.soc_pct == pytest.approx(100.0, abs=0.5)
        for _ in range(3000):
            battery.integrate(-1000.0, 60)
            assert 0.0 <= battery.soc_pct <= 100.0
        assert battery.soc_pct == pytest.approx(0.0, abs=0.5)

    def test_energy_is_conserved(self, catalog):
        battery = BatteryBank(catalog, BatteryConfig(initial_soc_pct=50.0))
        pattern = [20.0, -12.0, 35.0, -40.0, 5.0, -3.0, 0.0]
        for index in range(700):
            battery.integrate(pattern[index % len(pattern)], 30)
            battery.update_thermal(30, 22.0)
        assert battery.conservation_error_kwh() == pytest.approx(0.0, abs=1e-9)
        expected = (
            battery.initial_stored_kwh
            + battery.energy_charged_kwh * battery.config.charge_efficiency
            - battery.energy_discharged_kwh / battery.config.discharge_efficiency
        )
        assert battery.stored_kwh == pytest.approx(expected, abs=1e-9)

    def test_round_trip_efficiency_costs_energy(self, catalog):
        config = BatteryConfig(initial_soc_pct=50.0)
        battery = BatteryBank(catalog, config)
        start = battery.stored_kwh
        for _ in range(60):
            battery.integrate(10.0, 60)
        for _ in range(60):
            battery.integrate(-10.0, 60)
        assert battery.stored_kwh < start  # losses, never a free lunch
        assert battery.energy_charged_kwh == pytest.approx(10.0)
        assert battery.energy_discharged_kwh == pytest.approx(10.0)
        expected_loss = 10.0 * (1 - config.charge_efficiency) + 10.0 * (1 / config.discharge_efficiency - 1)
        assert start - battery.stored_kwh == pytest.approx(expected_loss, rel=1e-6)

    def test_charge_limit_tapers_at_high_soc(self, catalog):
        battery = BatteryBank(catalog, BatteryConfig(initial_soc_pct=50.0))
        mid = battery.limits()[0]
        battery.set_soc(95.0)
        high = battery.limits()[0]
        battery.set_soc(100.0)
        assert high < mid
        assert battery.limits()[0] == pytest.approx(0.0)
        assert battery.charge_permissive is False

    def test_discharge_limit_tapers_at_low_soc(self, catalog):
        battery = BatteryBank(catalog, BatteryConfig(initial_soc_pct=50.0))
        mid = battery.limits()[1]
        battery.set_soc(6.0)
        assert battery.limits()[1] < mid
        battery.set_soc(0.0)
        assert battery.limits()[1] == pytest.approx(0.0)
        assert battery.discharge_permissive is False

    def test_cold_cells_inhibit_charging(self, catalog):
        battery = BatteryBank(catalog, BatteryConfig(initial_soc_pct=50.0))
        battery.set_cell_temperature(-2.0)
        assert battery.limits()[0] == 0.0
        assert battery.charge_permissive is False
        assert battery.limits()[1] > 0.0  # discharge still allowed at -2 C
        assert "cell_temperature_low" in battery.block_reason()

    def test_hot_cells_derate_both_directions(self, catalog):
        battery = BatteryBank(catalog, BatteryConfig(initial_soc_pct=50.0))
        nominal = battery.limits()
        battery.set_cell_temperature(48.0)
        derated = battery.limits()
        assert derated[0] < nominal[0]
        assert derated[1] < nominal[1]
        battery.set_cell_temperature(56.0)
        assert battery.limits() == (0.0, 0.0)

    def test_integrate_respects_the_bms_limits(self, catalog):
        battery = BatteryBank(catalog, BatteryConfig(initial_soc_pct=50.0, max_charge_kw=12.0))
        accepted = battery.integrate(100.0, 60)
        assert accepted == pytest.approx(12.0)

    def test_fault_opens_the_envelope_entirely(self, catalog):
        battery = BatteryBank(catalog, BatteryConfig(initial_soc_pct=50.0))
        battery.inject_fault("cell_overvoltage")
        assert battery.limits() == (0.0, 0.0)
        assert battery.integrate(20.0, 60) == 0.0
        assert battery.block_reason() == "cell_overvoltage"

    def test_thermal_model_follows_ambient_and_throughput(self, catalog):
        battery = BatteryBank(catalog, BatteryConfig(initial_cell_temperature_c=20.0))
        for _ in range(500):  # ~40 h: several thermal time constants
            battery.integrate(0.0, 300)
            battery.update_thermal(300, 30.0)
        assert battery.cell_temperature_c == pytest.approx(30.0, abs=0.5)
        for _ in range(200):
            battery.integrate(-40.0, 300)
            battery.update_thermal(300, 30.0)
        assert battery.cell_temperature_c > 30.0  # self-heating under load

    def test_zone_hvac_pulls_cells_back_to_setpoint(self, catalog):
        """Shedding the Tier 1 battery HVAC lets the cells drift with the room."""
        hot_room = 42.0
        unconditioned = BatteryBank(catalog, BatteryConfig(initial_cell_temperature_c=25.0))
        conditioned = BatteryBank(catalog, BatteryConfig(initial_cell_temperature_c=25.0))
        for _ in range(400):
            unconditioned.update_thermal(300, hot_room, conditioning_kw=0.0)
            conditioned.update_thermal(300, hot_room, conditioning_kw=1.2)
        assert unconditioned.cell_temperature_c == pytest.approx(hot_room, abs=0.5)
        assert conditioned.cell_temperature_c < unconditioned.cell_temperature_c - 5.0

    def test_emitted_points_track_state(self, catalog, context):
        battery = BatteryBank(catalog, BatteryConfig(initial_soc_pct=42.0))
        battery.integrate(10.0, 60)
        values = battery.step(SimClock().now(), 60, context)
        battery.validate(values)
        assert values["energy.battery_bank.power_container.01/state_operating"] == "charging"
        assert values["energy.bms.power_container.01/charge_permissive"] is True
        assert context.charge_limit_kw > 0

    def test_contactor_command_round_trip(self, catalog):
        battery = BatteryBank(catalog)
        outcome = battery.handle_command(make_command("energy.bms.power_container.01", "open_contactor"))
        assert outcome and outcome.accepted
        assert battery.contactor_state == "open"
        assert battery.limits() == (0.0, 0.0)
        outcome = battery.handle_command(make_command("energy.bms.power_container.01", "close_contactor"))
        assert outcome and outcome.accepted
        assert battery.contactor_state == "closed"

    def test_contactor_close_refused_while_faulted(self, catalog):
        battery = BatteryBank(catalog)
        battery.inject_fault("bms_internal_fault")
        outcome = battery.handle_command(make_command("energy.bms.power_container.01", "close_contactor"))
        assert outcome and not outcome.accepted
        assert "bms_fault_active" in outcome.detail


# ---------------------------------------------------------------- inverter


class TestInverter:
    def test_block_capacity_is_the_sum_of_healthy_units(self, catalog):
        farm = InverterFarm(catalog)
        assert farm.available_capacity_kw(22.0) == pytest.approx(40.0)
        farm.inject_fault(0, "dc_overvoltage")
        assert farm.available_capacity_kw(22.0) == pytest.approx(30.0)
        farm.inject_fault(1)
        assert farm.available_capacity_kw(22.0) == pytest.approx(20.0)

    def test_thermal_derate(self, catalog):
        farm = InverterFarm(catalog)
        assert farm.thermal_derate(30.0) == 1.0
        assert 0.55 < farm.thermal_derate(50.0) < 1.0
        assert farm.thermal_derate(70.0) == pytest.approx(0.55)
        assert farm.available_capacity_kw(70.0) == pytest.approx(40.0 * 0.55)

    def test_dispatch_splits_evenly_and_faulted_units_carry_nothing(self, catalog, context):
        farm = InverterFarm(catalog)
        farm.inject_fault(3)
        farm.available_capacity_kw(22.0)
        farm.apply_dispatch(15.0, 16.0)
        assert [round(u.ac_output_kw, 3) for u in farm.units] == [5.0, 5.0, 5.0, 0.0]

    def test_operating_state_and_fault_points(self, catalog, context):
        farm = InverterFarm(catalog)
        farm.inject_fault(1, "hardware_lockout")
        values = farm.step(SimClock().now(), 5, context)
        farm.validate(values)
        assert values["energy.inverter.power_container.02/state_operating"] == "fault"
        assert values["energy.inverter.power_container.02/fault_active"] is True
        assert values["energy.inverter.power_container.02/alarm_summary"] == "critical"
        assert values["energy.inverter.power_container.01/state_operating"] == "running"

    def test_start_ramp_takes_the_configured_time(self, catalog, context):
        farm = InverterFarm(catalog, InverterConfig(initial_running=False, start_time_s=20.0))
        assert farm.running_count() == 0
        farm.start_unit(0)
        clock = SimClock()
        farm.step(clock.advance(10), 10, context)
        assert farm.running_count() == 0  # still ramping
        farm.step(clock.advance(15), 15, context)
        assert farm.running_count() == 1
        assert farm.units[0].starts == 1

    def test_hardware_lockout_cannot_be_cleared_remotely(self, catalog):
        farm = InverterFarm(catalog)
        farm.inject_fault(0, "hardware_lockout")
        outcome = farm.handle_command(make_command("energy.inverter.power_container.01", "clear_fault"))
        assert outcome and not outcome.accepted
        farm.inject_fault(1, "dc_overvoltage")
        outcome = farm.handle_command(make_command("energy.inverter.power_container.02", "clear_fault"))
        assert outcome and outcome.accepted

    def test_stop_all_de_energizes_the_block(self, catalog):
        farm = InverterFarm(catalog)
        farm.stop_all("blackout")
        assert farm.running_count() == 0
        assert farm.available_capacity_kw(22.0) == 0.0


# ---------------------------------------------------------------- generator


class TestGenerator:
    def _run(self, generator, seconds, dt_s=5.0, context=None, clock=None):
        context = context or SiteContext()
        clock = clock or SimClock()
        for _ in range(int(seconds / dt_s)):
            generator.step(clock.advance(dt_s), dt_s, context)
        return context

    def test_start_sequence_takes_crank_plus_warmup_plus_transfer(self, catalog):
        generator = Generator(catalog, GeneratorConfig(crank_time_s=8, warmup_time_s=60, transfer_time_s=5))
        assert generator.request_start("test").accepted
        self._run(generator, 5)
        assert generator.state != STATE_RUNNING
        self._run(generator, 15)
        assert generator.running  # cranked, now warming up
        assert not generator.loaded  # not transferred yet
        self._run(generator, 80)
        assert generator.state == STATE_RUNNING
        assert generator.loaded
        assert generator.source_selected == SOURCE_B

    def test_minimum_run_time_is_enforced(self, catalog):
        generator = Generator(
            catalog,
            GeneratorConfig(crank_time_s=5, warmup_time_s=10, minimum_run_time_s=600),
        )
        generator.request_start("test")
        self._run(generator, 120)
        assert generator.loaded
        outcome = generator.request_stop()
        assert outcome.accepted
        assert "minimum_run_time" in outcome.detail
        self._run(generator, 120)
        assert generator.state == STATE_RUNNING  # still running: min run not met
        self._run(generator, 500)
        assert generator.state in (STATE_COOLDOWN, STATE_OFF)

    def test_cooldown_precedes_stop(self, catalog):
        generator = Generator(
            catalog,
            GeneratorConfig(crank_time_s=5, warmup_time_s=10, minimum_run_time_s=30, cooldown_time_s=120),
        )
        generator.request_start("test")
        self._run(generator, 60)
        generator.request_stop()
        self._run(generator, 20)
        assert generator.state == STATE_COOLDOWN
        assert generator.output_kw == 0.0
        assert generator.source_selected == SOURCE_A  # transferred back first
        self._run(generator, 150)
        assert generator.state == STATE_OFF

    def test_start_can_fail_and_locks_out_after_the_attempt_policy(self, catalog):
        generator = Generator(
            catalog,
            GeneratorConfig(crank_time_s=5, retry_delay_s=10, fail_start_attempts=9, start_attempt_limit=3),
        )
        generator.request_start("critical_reserve")
        self._run(generator, 400)
        assert generator.state == STATE_LOCKOUT
        assert generator.failed_attempts >= 3
        assert generator.start_failure_active is True
        # A locked-out machine refuses further requests until reset.
        assert generator.request_start("again").accepted is False
        assert generator.reset().accepted
        assert generator.state == STATE_OFF
        assert generator.failed_attempts == 0

    def test_permissives_block_the_start(self, catalog):
        generator = Generator(catalog, GeneratorConfig(minimum_start_fuel_pct=10.0))
        generator.set_fuel_pct(4.0)
        outcome = generator.request_start("test")
        assert not outcome.accepted
        assert outcome.detail == "fuel_below_minimum_start_threshold"

        generator = Generator(catalog, GeneratorConfig(mode="manual"))
        assert generator.request_start("test").detail == "mode_not_automatic:manual"

        generator = Generator(catalog, GeneratorConfig(maintenance_lockout=True))
        assert generator.request_start("test").detail == "maintenance_lockout"

    def test_start_request_is_a_request_not_a_command(self, catalog):
        """The EMS asserts a request; the native controller decides."""
        generator = Generator(catalog, GeneratorConfig(maintenance_lockout=True))
        command = make_command("energy.generator.site.01", "generator_start_request", True, reason="reserve")
        outcome = generator.handle_command(command)
        assert outcome and not outcome.accepted
        assert generator.state == STATE_OFF
        assert generator.last_command_result == "rejected"

    def test_fuel_burns_with_load(self, catalog):
        generator = Generator(
            catalog, GeneratorConfig(crank_time_s=5, warmup_time_s=10, initial_fuel_pct=50.0)
        )
        generator.request_start("test")
        context = SiteContext()
        clock = SimClock()
        for _ in range(400):
            generator.set_load(20.0)
            generator.step(clock.advance(30), 30, context)
        assert generator.fuel_pct < 50.0
        assert generator.runtime_h > 3.0

    def test_shutdown_fault_trips_a_running_machine(self, catalog):
        generator = Generator(
            catalog, GeneratorConfig(crank_time_s=5, warmup_time_s=10, minimum_run_time_s=10)
        )
        generator.request_start("test")
        self._run(generator, 60)
        assert generator.loaded
        generator.inject_fault("low_oil_pressure")
        assert generator.state == STATE_COOLDOWN
        assert generator.output_kw == 0.0
        assert generator.request_start("retry").accepted is False

    def test_ats_reports_both_sources(self, catalog, context):
        generator = Generator(catalog, GeneratorConfig(crank_time_s=5, warmup_time_s=10))
        context.inverter_online_count = 4
        values = generator.step(SimClock().now(), 5, context)
        generator.validate(values)
        assert values["energy.ats.power_container.site_01/source_a_available"] is True
        assert values["energy.ats.power_container.site_01/source_b_available"] is False
        assert values["energy.ats.power_container.site_01/source_selected"] == SOURCE_A

    def test_events_are_queued_for_the_site(self, catalog):
        generator = Generator(catalog, GeneratorConfig(crank_time_s=5, warmup_time_s=10))
        generator.request_start("test")
        self._run(generator, 60)
        events = [name for name, _ in generator.drain_events()]
        assert "generator_start_requested" in events
        assert "generator_started" in events
        assert generator.drain_events() == []


# ---------------------------------------------------------------- loads


class TestLoads:
    def _step(self, loads, context, seconds, dt_s=5.0, clock=None):
        clock = clock or SimClock(start=dt.datetime(2026, 6, 21, 17, 0, tzinfo=UTC))
        values = {}
        for _ in range(max(1, int(seconds / dt_s))):
            values = loads.step(clock.advance(dt_s), dt_s, context)
        return values, clock

    def test_all_twelve_register_groups_are_modelled(self, catalog):
        loads = LoadBank(catalog)
        assert len(loads.groups) == 12
        assert all(a.startswith("energy.load.site.") for a in loads.groups)

    def test_daily_profile_moves_power(self, catalog):
        loads = LoadBank(catalog, seed=5)
        context = SiteContext()
        night = SimClock(start=dt.datetime(2026, 6, 21, 8, 0, tzinfo=UTC))  # 03:00 local
        self._step(loads, context, 60, clock=night)
        night_kw = loads.group("energy.load.site.greenhouse_lighting_01").power_kw
        day = SimClock(start=dt.datetime(2026, 6, 21, 23, 0, tzinfo=UTC))  # 18:00 local
        self._step(loads, context, 60, clock=day)
        assert loads.group("energy.load.site.greenhouse_lighting_01").power_kw > night_kw + 2.0

    def test_shed_then_restore_honours_the_timers(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext()
        self._step(loads, context, 60)
        asset = "energy.load.site.opportunistic_compute_01"
        outcome = loads.handle_command(make_command(asset, "shed"))
        assert outcome and outcome.accepted and not outcome.completed
        assert loads.group(asset).shed_state == "shed_pending"
        self._step(loads, context, 10)
        assert loads.group(asset).shed_state == SHED
        assert loads.group(asset).power_kw == 0.0

        # Too soon: the minimum off time refuses the restore.
        refusal = loads.handle_command(make_command(asset, "restore"))
        assert refusal and not refusal.accepted
        assert "minimum_off_time_not_elapsed" in refusal.detail

        self._step(loads, context, 40)
        accepted = loads.handle_command(make_command(asset, "restore"))
        assert accepted and accepted.accepted
        self._step(loads, context, 30)
        assert loads.group(asset).shed_state == CONNECTED
        assert loads.group(asset).power_kw > 0.0

    def test_tier0_control_core_refuses_every_shed(self, catalog):
        """SDD 31.2 Tier 0: local controllers retain authority."""
        loads = LoadBank(catalog, seed=1)
        context = SiteContext()
        self._step(loads, context, 60)
        asset = "energy.load.site.control_core_01"
        outcome = loads.handle_command(make_command(asset, "shed"))
        assert outcome and not outcome.accepted
        assert outcome.detail == "tier0_control_survival_never_shed"
        # Even an emergency-mode command is refused.
        emergency = loads.handle_command(make_command(asset, "shed", operating_mode="emergency"))
        assert emergency and not emergency.accepted
        assert loads.group(asset).power_kw > 0.0

    def test_enabled_requested_false_is_treated_as_a_shed(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext()
        self._step(loads, context, 60)
        outcome = loads.handle_command(
            make_command("energy.load.site.control_core_01", "enabled_requested", False)
        )
        assert outcome and not outcome.accepted

    def test_temperature_interlocks_refuse_until_emergency(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext(cell_temperature_c=1.0, rack_inlet_temperature_c=30.0)
        self._step(loads, context, 60)
        hvac = "energy.load.site.battery_hvac_01"
        refusal = loads.handle_command(make_command(hvac, "shed"))
        assert refusal and not refusal.accepted
        assert refusal.detail == "battery_temperature_outside_safe_band"
        override = loads.handle_command(make_command(hvac, "shed", operating_mode="emergency"))
        assert override and override.accepted

    def test_rack_cooling_interlock_depends_on_the_rack(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext(rack_inlet_temperature_c=28.0, rack_it_kw=1.4, cell_temperature_c=20.0)
        self._step(loads, context, 60)
        refusal = loads.handle_command(make_command("energy.load.site.rack_cooling_01", "shed"))
        assert refusal and not refusal.accepted
        assert refusal.detail == "rack_energized_temperature_governed"

    def test_minimum_on_time_prevents_rapid_cycling(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext()
        self._step(loads, context, 60)
        asset = "energy.load.site.server_rack_01"  # minimum_on_time_s = 300
        loads.group(asset)._time_since_change_s = 10.0
        refusal = loads.handle_command(make_command(asset, "shed"))
        assert refusal and not refusal.accepted
        assert "minimum_on_time_not_elapsed" in refusal.detail

    def test_power_budget_throttles_only_throttleable_loads(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext()
        self._step(loads, context, 60)
        asset = "energy.load.site.opportunistic_compute_01"
        assert loads.handle_command(make_command(asset, "power_budget_kw", 0.5)).accepted
        self._step(loads, context, 20)
        assert loads.group(asset).power_kw <= 0.5
        assert loads.group(asset).requested_kw > 0.5  # request is still reported
        refusal = loads.handle_command(
            make_command("energy.load.site.water_pumping_01", "power_budget_kw", 0.5)
        )
        assert refusal and not refusal.accepted

    def test_inrush_on_restore(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext()
        clock = SimClock(start=dt.datetime(2026, 6, 21, 17, 0, tzinfo=UTC))
        self._step(loads, context, 60, clock=clock)
        asset = "energy.load.site.water_pumping_01"  # inrush_factor 3.0
        group = loads.group(asset)
        steady = group.power_kw
        loads.force_shed(asset)
        group._time_since_change_s = 10_000
        loads.handle_command(make_command(asset, "restore"))
        self._step(loads, context, 61, dt_s=1.0, clock=clock)  # 60 s restart delay
        assert group.shed_state == CONNECTED
        assert group.power_kw > steady * 1.5  # inrush multiplier is active
        self._step(loads, context, 20, dt_s=1.0, clock=clock)
        assert group._inrush_remaining_s == 0.0
        assert group.power_kw == pytest.approx(steady, rel=0.3)

    def test_de_energized_bus_locks_every_group_out(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext(ac_bus_energized=False)
        self._step(loads, context, 20)
        assert all(g.shed_state == LOCKED_OUT for g in loads.groups.values())
        assert context.load_actual_kw == 0.0

    def test_panels_split_critical_from_general(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext()
        values, _ = self._step(loads, context, 60)
        loads.validate(values)
        critical = values["energy.panel.power_container.critical_01/power_total_kw"]
        general = values["energy.panel.power_container.general_01/power_total_kw"]
        assert critical > 0
        assert critical + general == pytest.approx(context.load_actual_kw, abs=0.01)

    def test_equipment_failure_shows_as_a_request_actual_mismatch(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext()
        self._step(loads, context, 20)
        asset = "energy.load.site.rack_cooling_01"
        loads.fail_equipment(asset)
        values, _ = self._step(loads, context, 20)
        assert values[f"{asset}/enabled_requested"] is True
        assert values[f"{asset}/enabled_actual"] is False
        assert values[f"{asset}/power_kw"] == 0.0

    def test_register_tier_is_published_verbatim(self, catalog):
        loads = LoadBank(catalog, seed=1)
        context = SiteContext()
        values, _ = self._step(loads, context, 20)
        assert values["energy.load.site.control_core_01/load_tier"] == 1
        assert values["energy.load.site.opportunistic_compute_01/load_tier"] == 5


# ---------------------------------------------------------------- rack


class TestRack:
    def _run(self, rack, context, seconds, dt_s=30.0):
        clock = SimClock()
        values = {}
        for _ in range(int(seconds / dt_s)):
            values = rack.step(clock.advance(dt_s), dt_s, context)
        return values

    def test_container_temperature_settles_with_cooling(self, catalog):
        rack = ServerRack(catalog)
        context = SiteContext(ambient_temperature_c=25.0, rack_cooling_available=True)
        self._run(rack, context, 7200)
        assert 20.0 < rack.container_temperature_c < 30.0

    def test_losing_cooling_heats_the_container(self, catalog):
        rack = ServerRack(catalog)
        context = SiteContext(ambient_temperature_c=30.0, rack_cooling_available=True)
        self._run(rack, context, 3600)
        cooled = rack.container_temperature_c
        rack.fail_cooling(True)
        self._run(rack, context, 7200)
        assert rack.container_temperature_c > cooled + 10.0
        self._run(rack, context, 7200)
        # Four hours without cooling on a 30 C day pushes the container past the
        # 40 C threshold where the inverter block starts to derate.
        assert rack.container_temperature_c > 38.0
        assert rack.rack_exhaust_c > rack.rack_inlet_c

    def test_shedding_cooling_has_the_same_thermal_effect(self, catalog):
        rack = ServerRack(catalog)
        context = SiteContext(ambient_temperature_c=28.0, rack_cooling_available=True)
        self._run(rack, context, 3600)
        baseline = rack.container_temperature_c
        context.rack_cooling_available = False
        self._run(rack, context, 5400)
        assert rack.container_temperature_c > baseline + 8.0

    def test_ups_runs_down_on_battery(self, catalog):
        rack = ServerRack(catalog)
        context = SiteContext(critical_bus_energized=True)
        values = self._run(rack, context, 600)
        assert values["energy.ups.rack_01.01/on_battery"] is False
        full_runtime = values["energy.ups.rack_01.01/runtime_remaining_min"]
        context.critical_bus_energized = False
        values = self._run(rack, context, 180, dt_s=30.0)
        assert values["energy.ups.rack_01.01/on_battery"] is True
        assert values["energy.ups.rack_01.01/runtime_remaining_min"] < full_runtime

    def test_pdu_metering_tracks_the_it_load(self, catalog):
        rack = ServerRack(catalog)
        context = SiteContext()
        values = self._run(rack, context, 300)
        switched = values["energy.pdu.rack_01.switched_01/power_total_kw"]
        basic = values["energy.pdu.rack_01.basic_01/power_total_kw"]
        assert switched + basic == pytest.approx(context.rack_it_kw, rel=0.01)
        assert values["energy.pdu.rack_01.switched_01/current_total_a"] > 0

    def test_switched_outlet_command_and_reserved_outlet(self, catalog):
        rack = ServerRack(catalog)
        outcome = rack.handle_command(
            make_command("energy.pdu.rack_01.switched_01", "outlet_state", {"outlet": 4, "state": False})
        )
        assert outcome and outcome.accepted
        reserved = rack.handle_command(
            make_command("energy.pdu.rack_01.switched_01", "outlet_state", {"outlet": 0, "state": False})
        )
        assert reserved and not reserved.accepted

    def test_safety_sensors_and_beacon(self, catalog):
        rack = ServerRack(catalog)
        context = SiteContext()
        rack.set_smoke(True)
        values = self._run(rack, context, 60)
        rack.validate(values)
        assert values["safety.safety_sensor.rack_01.nbes0307_01/alarm_active"] is True
        assert values["it.rack.power_container.01/smoke_active"] is True
        assert values["it.rack.power_container.01/alarm_summary"] == "critical"
        assert values["safety.alarm_output.rack_01.beacon_01/state_operating"] == "active"

    def test_network_devices_report_reachability_and_health(self, catalog):
        rack = ServerRack(catalog)
        context = SiteContext()
        values = self._run(rack, context, 300)
        rack.validate(values)
        assert values["it.switch.rack_01.catalyst_2960x_01/availability_state"] == "online"
        assert values["it.switch.rack_01.catalyst_2960x_01/port_up_count"] > 0
        assert values["it.switch.rack_01.arista_7050qx_01/temperature_c"] > 0
        assert values["it.router.rack_01.isr4321_01/wan_state"] == "up"
        assert values["it.wireless_controller.rack_01.wlc5508_01/ap_online_count"] == 5

    def test_losing_the_wan_leaves_voice_internal_only(self, catalog):
        """SDD 39 EMS-T001: the internet is not part of local control."""
        rack = ServerRack(catalog)
        context = SiteContext()
        rack.set_wan(False)
        values = self._run(rack, context, 120)
        assert values["it.router.rack_01.isr4321_01/wan_state"] == "down"
        assert values["it.router.rack_01.isr4321_01/vpn_state"] == "down"
        assert values["it.router.rack_01.isr4321_01/voice_gateway_state"] == ("degraded_internal_only")
        # Everything else on the rack carries on exactly as before.
        assert values["it.switch.rack_01.catalyst_2960x_01/availability_state"] == "online"
        assert values["energy.ups.rack_01.01/on_battery"] is False
        assert context.rack_it_kw > 0

    def test_hot_rack_shows_up_as_switch_packet_errors(self, catalog):
        rack = ServerRack(catalog)
        context = SiteContext(ambient_temperature_c=35.0, rack_cooling_available=True)
        cool = self._run(rack, context, 600)
        assert cool["it.switch.rack_01.catalyst_2960x_01/packet_error_rate"] == 0.0
        rack.fail_cooling(True)
        hot = self._run(rack, context, 14400)
        assert hot["it.switch.rack_01.catalyst_2960x_01/packet_error_rate"] > 0.0

    def test_reserve_pdu_is_metered_at_208_v(self, catalog):
        rack = ServerRack(catalog)
        context = SiteContext()
        values = self._run(rack, context, 120)
        power = values["energy.pdu.rack_01.ap9570_01/power_total_kw"]
        current = values["energy.pdu.rack_01.ap9570_01/current_total_a"]
        assert power > 0
        assert current == pytest.approx(power * 1000.0 / 208.0, rel=0.01)
        assert values["energy.pdu.rack_01.ap9570_01/overload_active"] is False

    def test_netbotz_reports_container_and_rack_air(self, catalog):
        rack = ServerRack(catalog)
        context = SiteContext(ambient_temperature_c=20.0)
        values = self._run(rack, context, 600)
        container = values["it.environmental_monitor.power_container.netbotz_500_01/temperature_air_c"]
        assert container == pytest.approx(rack.container_temperature_c, abs=0.01)
        assert values["it.environmental_monitor.rack_01.nbrk0550_01/temperature_air_c"] > 0
