"""Named, reproducible scenarios.

Each scenario is a :class:`SiteConfig` plus a schedule of injected events. Given
the same ``(scenario, seed)`` the telemetry is identical, so a scenario can be
cited in a commissioning record the way a test case is cited in a test plan.

The set below covers the SDD section 39 verification cases that can be driven
from the site side. ``verifies`` on each scenario names the case IDs, so a
report can be assembled mechanically::

    EMS-T003  sensor_failure          stale/frozen SOC input
    EMS-T004  reserve_decline         declining reserve, ordered shedding
    EMS-T005  tier0_shed_refused      shed command rejected
    EMS-T006  passing_clouds          hysteresis, no rapid re-shedding
    EMS-T007  passing_clouds          sustained recovery, staggered restore
    EMS-T008  generator_fails_to_start generator unavailable in critical reserve
    EMS-T009  generator_support       successful start, transfer, charge, stop
    EMS-T010  generator_fails_during_run generator trips while loaded
    EMS-T011  thermal_derate          BMS discharge limit derates thermally
    EMS-T012  black_start             complete AC blackout and recovery
    EMS-T014  rack_cooling_loss       rack thermal event forcing IT shutdown
    (comms)   comms_loss              gateway silence, staleness detection

Events are plain callables against the live :class:`SimulatedSite`, scheduled by
simulated elapsed time, so they replay exactly regardless of step size.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

from homestead_twin import topics
from homestead_twin.envelope import CommandEnvelope
from simulator.clock import parse_duration
from simulator.components.battery import BatteryConfig
from simulator.components.generator import GENERATOR_ASSET, GeneratorConfig
from simulator.components.rack import RackConfig
from simulator.components.solar import SolarConfig
from simulator.components.weather import WeatherConfig
from simulator.site import SimulatedSite, SiteConfig

#: A scenario event: ``fn(site)`` applied once, at ``at_s`` simulated seconds.
EventFn = Callable[[SimulatedSite], None]

#: Summer solstice and midwinter starts, 04:00 local (09:00 UTC at -5).
SUMMER_START = dt.datetime(2026, 6, 21, 9, 0, tzinfo=dt.timezone.utc)
WINTER_START = dt.datetime(2026, 1, 15, 9, 0, tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class ScenarioEvent:
    """One injected condition, with a human-readable label for the run log."""

    at_s: float
    label: str
    apply: EventFn

    def __call__(self, site: SimulatedSite) -> None:
        self.apply(site)


@dataclass
class Scenario:
    """A configuration plus a schedule of injected events."""

    name: str
    description: str
    config: SiteConfig
    events: tuple[ScenarioEvent, ...] = ()
    duration_s: float = 86400.0
    #: Recommended step size. Sequenced scenarios need finer resolution than a
    #: 24-hour energy sweep.
    step_s: float = 5.0
    verifies: tuple[str, ...] = ()

    def build(self, bus, settings=None, seed: int | None = None) -> SimulatedSite:
        """Instantiate a site for this scenario."""
        config = self.config.seeded(self.config.seed if seed is None else seed)
        return SimulatedSite(bus=bus, settings=settings, config=config)


class ScenarioRunner:
    """Drives a site through a scenario's event schedule."""

    def __init__(self, site: SimulatedSite, scenario: Scenario) -> None:
        self.site = site
        self.scenario = scenario
        self._pending = sorted(scenario.events, key=lambda e: e.at_s)
        self.fired: list[tuple[float, str]] = []

    def pump(self) -> None:
        """Fire every event whose time has arrived."""
        elapsed = self.site.clock.elapsed_s
        while self._pending and self._pending[0].at_s <= elapsed:
            event = self._pending.pop(0)
            event(self.site)
            self.fired.append((event.at_s, event.label))

    def run(
        self,
        duration_s: float | None = None,
        step_s: float | None = None,
        speed: float | None = None,
        pacer=None,
    ):
        """Run the scenario to completion, firing events on schedule."""
        duration = self.scenario.duration_s if duration_s is None else duration_s
        step = self.scenario.step_s if step_s is None else step_s
        self.site.start()
        self.pump()

        def on_step(site: SimulatedSite, _balance) -> None:
            self.pump()

        return self.site.run(
            duration_s=duration, dt_s=step, speed=speed, pacer=pacer, on_step=on_step
        )


# --------------------------------------------------------------------------
# event helpers
# --------------------------------------------------------------------------


def _do_cloud(mode: str, base: float | None = None) -> EventFn:
    def apply(site: SimulatedSite) -> None:
        site.weather.set_mode(mode, base)

    return apply


def _do_inverter_fault(index: int, code: str) -> EventFn:
    def apply(site: SimulatedSite) -> None:
        site.inverters.inject_fault(index, code)

    return apply


def _do_generator_fault(code: str) -> EventFn:
    def apply(site: SimulatedSite) -> None:
        site.generator.inject_fault(code)

    return apply


def _do_comms_loss(gateway: str) -> EventFn:
    def apply(site: SimulatedSite) -> None:
        site.lose_comms(gateway)

    return apply


def _do_comms_restore(gateway: str) -> EventFn:
    def apply(site: SimulatedSite) -> None:
        site.restore_comms(gateway)

    return apply


def _do_sensor_fault(point_id: str, mode: str, quality: str = "bad") -> EventFn:
    def apply(site: SimulatedSite) -> None:
        site.inject_sensor_fault(point_id, mode=mode, quality=quality)

    return apply


def _do_cooling_failure(failed: bool = True) -> EventFn:
    def apply(site: SimulatedSite) -> None:
        site.rack.fail_cooling(failed)
        site.loads.fail_equipment("energy.load.site.rack_cooling_01", failed)

    return apply


def _do_blackout(reason: str) -> EventFn:
    def apply(site: SimulatedSite) -> None:
        site.trigger_blackout(reason)

    return apply


def _do_black_start() -> EventFn:
    def apply(site: SimulatedSite) -> None:
        site.request_black_start()

    return apply


def _do_cell_temperature(value: float) -> EventFn:
    def apply(site: SimulatedSite) -> None:
        site.battery.set_cell_temperature(value)

    return apply


def _do_command(
    asset_id: str,
    command: str,
    value: Any = None,
    reason: str = "scenario",
    operating_mode: str | None = None,
    issued_by: str = "scenario.injector",
) -> EventFn:
    """Publish a real command on the bus, so the full subscribe/ack path runs.

    Scenarios use this rather than poking components directly whenever the point
    of the case is the *command* -- a refused shed, a generator start request --
    because that is what the EMS will actually do.
    """

    def apply(site: SimulatedSite) -> None:
        envelope = CommandEnvelope(
            command_id=f"scn-{site.clock.steps:06d}-{asset_id}-{command}",
            issued_at=site.clock.now(),
            issued_by=issued_by,
            asset_id=asset_id,
            command=command,
            value=value,
            reason=reason,
            operating_mode=operating_mode,
        )
        site.bus.publish(
            topics.command_topic(asset_id, command, site.base_topic),
            envelope.to_payload(),
            qos=1,
        )

    return apply


# --------------------------------------------------------------------------
# scenario definitions
# --------------------------------------------------------------------------


def _base_config(**overrides) -> SiteConfig:
    overrides.setdefault("start", SUMMER_START)
    return SiteConfig(seed=1, **overrides)


def _clear_summer_day() -> Scenario:
    config = _base_config(
        weather=WeatherConfig(cloud_mode="clear"),
        battery=BatteryConfig(initial_soc_pct=55.0),
    )
    return Scenario(
        name="clear_summer_day",
        description=(
            "Solstice, clear sky, healthy reserve. Baseline energy sweep: PV is "
            "zero at night, peaks near solar noon, and curtails when the battery "
            "fills."
        ),
        config=config,
        duration_s=parse_duration("24h"),
        step_s=10.0,
        verifies=("EMS-T007",),
    )


def _overcast_winter_day() -> Scenario:
    config = _base_config(
        start=WINTER_START,
        weather=WeatherConfig(cloud_mode="overcast"),
        battery=BatteryConfig(initial_soc_pct=45.0),
    )
    return Scenario(
        name="overcast_winter_day",
        description=(
            "Mid-January, heavy overcast, short day. Generation barely covers "
            "the critical baseload; reserve declines all day."
        ),
        config=config,
        duration_s=parse_duration("24h"),
        step_s=10.0,
        verifies=("EMS-T004",),
    )


def _passing_clouds() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=2),
        weather=WeatherConfig(
            cloud_mode="passing_clouds",
            cloud_period_s=600.0,
            cloud_duration_s=240.0,
            cloud_depth=0.9,
        ),
        battery=BatteryConfig(initial_soc_pct=35.0),
    )
    return Scenario(
        name="passing_clouds",
        description=(
            "Cumulus field over midday: PV swings between full output and a "
            "quarter of it every ten minutes. The EMS must not oscillate "
            "(SDD 30.9); shed/restore counts should stay near zero."
        ),
        config=config,
        events=(
            ScenarioEvent(parse_duration("3h"), "sustained clearing", _do_cloud("clear")),
        ),
        duration_s=parse_duration("6h"),
        step_s=5.0,
        verifies=("EMS-T006", "EMS-T007"),
    )


def _reserve_decline() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=8),  # early afternoon, then night
        weather=WeatherConfig(cloud_mode="storm"),
        battery=BatteryConfig(initial_soc_pct=28.0),
        generator=GeneratorConfig(mode="manual"),  # generator not available
    )
    return Scenario(
        name="reserve_decline",
        description=(
            "Storm cover with the generator in manual: reserve falls steadily "
            "through the CONSERVE band into CRITICAL_RESERVE. Tier 4 grants must "
            "stop first, then ordered shedding after the configured delays."
        ),
        config=config,
        events=(
            ScenarioEvent(parse_duration("2h"), "deeper cover", _do_cloud("storm", 0.99)),
            ScenarioEvent(
                parse_duration("3h"),
                "S1: stop opportunistic compute",
                _do_command(
                    "energy.load.site.opportunistic_compute_01", "shed",
                    reason="reserve_declining",
                ),
            ),
            ScenarioEvent(
                parse_duration("4h"),
                "S2: defer tool charging and spa",
                _do_command(
                    "energy.load.site.tool_charging_01", "shed", reason="reserve_declining"
                ),
            ),
            ScenarioEvent(
                parse_duration("5h"),
                "S3: shed greenhouse lighting",
                _do_command(
                    "energy.load.site.greenhouse_lighting_01", "shed",
                    reason="critical_reserve_approaching",
                ),
            ),
            ScenarioEvent(
                parse_duration("6h"),
                "S5: attempt to shed the control core (must be refused)",
                _do_command(
                    "energy.load.site.control_core_01", "shed", reason="critical_reserve"
                ),
            ),
            ScenarioEvent(
                parse_duration("8h"),
                "restore lighting too early (must be refused)",
                _do_command(
                    "energy.load.site.greenhouse_lighting_01", "restore",
                    reason="premature_restore",
                ),
            ),
        ),
        duration_s=parse_duration("12h"),
        step_s=10.0,
        verifies=("EMS-T004", "EMS-T008"),
    )


def _generator_support() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=14),  # evening
        weather=WeatherConfig(cloud_mode="overcast"),
        battery=BatteryConfig(initial_soc_pct=18.0),
        generator=GeneratorConfig(minimum_run_time_s=1800.0, cooldown_time_s=300.0),
    )
    return Scenario(
        name="generator_support",
        description=(
            "Low reserve at night. The EMS requests a start; the machine cranks, "
            "warms up, transfers, carries the load and charges the battery, then "
            "stops through cooldown after the minimum run time."
        ),
        config=config,
        events=(
            ScenarioEvent(
                parse_duration("10m"),
                "EMS requests generator start",
                _do_command(
                    GENERATOR_ASSET, "generator_start_request", True,
                    reason="battery_reserve_protection",
                ),
            ),
            ScenarioEvent(
                parse_duration("2h"),
                "EMS releases the start request",
                _do_command(
                    GENERATOR_ASSET, "generator_start_request", False, reason="reserve_recovered"
                ),
            ),
        ),
        duration_s=parse_duration("4h"),
        step_s=5.0,
        verifies=("EMS-T009",),
    )


def _generator_fails_to_start() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=14),
        weather=WeatherConfig(cloud_mode="overcast"),
        battery=BatteryConfig(initial_soc_pct=15.0),
        generator=GeneratorConfig(fail_start_attempts=5, start_attempt_limit=3),
    )
    return Scenario(
        name="generator_fails_to_start",
        description=(
            "Every crank attempt fails. After the attempt policy is exhausted the "
            "controller locks out and requires an explicit reset (SDD 34.7); the "
            "EMS must stay in CRITICAL_RESERVE, shed deeper, and must not "
            "re-request the start indefinitely."
        ),
        config=config,
        events=(
            ScenarioEvent(
                parse_duration("5m"),
                "EMS requests generator start",
                _do_command(
                    GENERATOR_ASSET, "generator_start_request", True, reason="critical_reserve"
                ),
            ),
            ScenarioEvent(
                parse_duration("30m"), "operator finds the fuel valve closed",
                lambda site: site.generator.set_fuel_pct(4.0),
            ),
        ),
        duration_s=parse_duration("2h"),
        step_s=5.0,
        verifies=("EMS-T008",),
    )


def _generator_fails_during_run() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=14),
        weather=WeatherConfig(cloud_mode="overcast"),
        battery=BatteryConfig(initial_soc_pct=20.0),
    )
    return Scenario(
        name="generator_fails_during_run",
        description=(
            "The generator starts and transfers, then trips on a shutdown-class "
            "fault while carrying load. Transfer back to the inverter bus, shed, "
            "and raise a critical alarm with the failed sequence step."
        ),
        config=config,
        events=(
            ScenarioEvent(
                parse_duration("5m"),
                "EMS requests generator start",
                _do_command(
                    GENERATOR_ASSET, "generator_start_request", True,
                    reason="battery_reserve_protection",
                ),
            ),
            ScenarioEvent(
                parse_duration("45m"), "engine trips on low oil pressure",
                _do_generator_fault("low_oil_pressure"),
            ),
        ),
        duration_s=parse_duration("2h"),
        step_s=5.0,
        verifies=("EMS-T010",),
    )


def _scenario_inverter_fault() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=3),
        weather=WeatherConfig(cloud_mode="light"),
        battery=BatteryConfig(initial_soc_pct=50.0),
    )
    return Scenario(
        name="inverter_fault",
        description=(
            "Two of four inverters fault during the PV peak. Block capacity "
            "halves to ~20 kW, forcing curtailment and, if load stays high, an "
            "overload-risk condition."
        ),
        config=config,
        events=(
            ScenarioEvent(
                parse_duration("1h"), "inverter 2 shutdown fault",
                _do_inverter_fault(1, "dc_overvoltage"),
            ),
            ScenarioEvent(
                parse_duration("90m"), "inverter 4 hardware lockout",
                _do_inverter_fault(3, "hardware_lockout"),
            ),
            ScenarioEvent(
                parse_duration("3h"), "inverter 2 reset by operator",
                lambda site: site.inverters.clear_fault(1),
            ),
        ),
        duration_s=parse_duration("6h"),
        step_s=5.0,
        verifies=("EMS-T011",),
    )


def _thermal_derate() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=4),
        weather=WeatherConfig(cloud_mode="clear", temperature_offset_c=12.0),
        battery=BatteryConfig(initial_soc_pct=60.0, initial_cell_temperature_c=32.0),
    )
    return Scenario(
        name="thermal_derate",
        description=(
            "Heat wave with the container running warm: BMS charge and discharge "
            "limits derate and the inverter block loses capacity. The EMS must "
            "reduce load before the inverter overloads or the BMS trips."
        ),
        config=config,
        events=(
            ScenarioEvent(parse_duration("30m"), "cells reach 46 C", _do_cell_temperature(46.0)),
            ScenarioEvent(parse_duration("2h"), "cells reach 52 C", _do_cell_temperature(52.0)),
        ),
        duration_s=parse_duration("4h"),
        step_s=5.0,
        verifies=("EMS-T011",),
    )


def _scenario_black_start() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=1),
        weather=WeatherConfig(cloud_mode="light"),
        battery=BatteryConfig(initial_soc_pct=40.0),
        start_de_energized=True,
    )
    return Scenario(
        name="black_start",
        description=(
            "Site begins fully de-energized. The SDD 35.3 sequence runs: verify "
            "isolation, energize controls, close the contactor, start the master "
            "inverter, energize the critical bus, then stagger critical loads "
            "before general ones. Attended loads stay out."
        ),
        config=config,
        events=(
            ScenarioEvent(parse_duration("2m"), "operator authorises black start", _do_black_start()),
        ),
        duration_s=parse_duration("1h"),
        step_s=2.0,
        verifies=("EMS-T012",),
    )


def _blackout_recovery() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=6),
        weather=WeatherConfig(cloud_mode="light"),
        battery=BatteryConfig(initial_soc_pct=45.0),
    )
    return Scenario(
        name="blackout_recovery",
        description=(
            "A healthy site collapses mid-morning (all inverters trip), then "
            "black-starts. Exercises the transition into and out of a "
            "de-energized state rather than starting there."
        ),
        config=config,
        events=(
            ScenarioEvent(parse_duration("20m"), "AC system collapses", _do_blackout("inverter_trip")),
            ScenarioEvent(parse_duration("30m"), "black start authorised", _do_black_start()),
        ),
        duration_s=parse_duration("2h"),
        step_s=2.0,
        verifies=("EMS-T012",),
    )


def _scenario_comms_loss() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=4),
        weather=WeatherConfig(cloud_mode="light"),
        battery=BatteryConfig(initial_soc_pct=55.0),
    )
    return Scenario(
        name="comms_loss",
        description=(
            "The power-container gateway stops publishing for 20 minutes. Every "
            "battery, BMS, inverter, generator and ATS point goes stale; the "
            "retained availability turns offline once the will delay elapses. "
            "The EMS must enter a conservative data-quality state, not assume a "
            "healthy reserve."
        ),
        config=config,
        events=(
            ScenarioEvent(parse_duration("30m"), "power gateway drops", _do_comms_loss("power")),
            ScenarioEvent(parse_duration("50m"), "power gateway returns", _do_comms_restore("power")),
            ScenarioEvent(parse_duration("70m"), "rack gateway drops", _do_comms_loss("rack")),
            ScenarioEvent(parse_duration("85m"), "rack gateway returns", _do_comms_restore("rack")),
        ),
        duration_s=parse_duration("2h"),
        step_s=5.0,
        verifies=("EMS-T001", "EMS-T003"),
    )


def _sensor_failure() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=4),
        weather=WeatherConfig(cloud_mode="light"),
        battery=BatteryConfig(initial_soc_pct=48.0),
    )
    return Scenario(
        name="sensor_failure",
        description=(
            "SOC freezes at its last value while the battery keeps discharging, "
            "then a cell-temperature sensor starts reporting an out-of-range "
            "value with bad quality. Reproduces SDD 39 case EMS-T003."
        ),
        config=config,
        events=(
            ScenarioEvent(
                parse_duration("20m"), "SOC input freezes",
                _do_sensor_fault(
                    "energy.battery_bank.power_container.01/soc_pct", "frozen", "uncertain"
                ),
            ),
            ScenarioEvent(
                parse_duration("60m"), "cell temperature reads out of range",
                _do_sensor_fault(
                    "energy.battery_bank.power_container.01/temperature_cell_max_c", "bad", "bad"
                ),
            ),
            ScenarioEvent(
                parse_duration("90m"), "PV irradiance stops updating",
                _do_sensor_fault(
                    "energy.pv_array.agrivoltaic_field.01/solar_irradiance_w_m2", "stale", "stale"
                ),
            ),
        ),
        duration_s=parse_duration("2h"),
        step_s=5.0,
        verifies=("EMS-T003",),
    )


def _rack_cooling_loss() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=5),
        weather=WeatherConfig(cloud_mode="clear", temperature_offset_c=8.0),
        battery=BatteryConfig(initial_soc_pct=60.0),
        rack=RackConfig(initial_container_temperature_c=24.0),
    )
    return Scenario(
        name="rack_cooling_loss",
        description=(
            "Rack cooling fails on a hot afternoon. Container temperature climbs, "
            "the NetBotz appliances alarm, the inverter block thermally derates "
            "and battery cell temperature follows. Reproduces the "
            "power_container_cooling_failed alarm basis (SDD 37.1) and the "
            "controlled IT shutdown case (EMS-T014)."
        ),
        config=config,
        events=(
            ScenarioEvent(parse_duration("15m"), "cooling plant fails", _do_cooling_failure(True)),
            ScenarioEvent(
                parse_duration("3h"), "cooling restored after repair", _do_cooling_failure(False)
            ),
        ),
        duration_s=parse_duration("5h"),
        step_s=10.0,
        verifies=("EMS-T014",),
    )


def _tier0_shed_refused() -> Scenario:
    """Direct exercise of the local-authority refusal path (EMS-T005)."""
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=14),
        weather=WeatherConfig(cloud_mode="overcast"),
        battery=BatteryConfig(initial_soc_pct=22.0),
    )
    return Scenario(
        name="tier0_shed_refused",
        description=(
            "Reserve is low and the EMS reaches too far down the shedding order. "
            "The Tier 0 control core refuses; battery HVAC refuses while cells "
            "are outside their safe band. Each refusal is a rejected ack with a "
            "reason, and the EMS must recalculate reserve with the load still "
            "present (SDD 32.3)."
        ),
        config=config,
        events=(
            ScenarioEvent(
                parse_duration("10m"), "cells forced out of band", _do_cell_temperature(2.0)
            ),
            ScenarioEvent(
                parse_duration("12m"),
                "shed opportunistic compute (accepted)",
                _do_command(
                    "energy.load.site.opportunistic_compute_01", "shed", reason="critical_reserve"
                ),
            ),
            ScenarioEvent(
                parse_duration("15m"),
                "shed the Tier 0 control core (refused)",
                _do_command(
                    "energy.load.site.control_core_01", "shed", reason="critical_reserve"
                ),
            ),
            ScenarioEvent(
                parse_duration("20m"),
                "shed battery HVAC while cells are cold (refused)",
                _do_command(
                    "energy.load.site.battery_hvac_01", "shed", reason="critical_reserve"
                ),
            ),
            ScenarioEvent(
                parse_duration("25m"),
                "shed rack cooling while the rack is warm (refused)",
                _do_command(
                    "energy.load.site.rack_cooling_01", "shed", reason="critical_reserve"
                ),
            ),
            ScenarioEvent(
                parse_duration("30m"),
                "emergency plan sheds rack cooling (permitted)",
                _do_command(
                    "energy.load.site.rack_cooling_01", "shed",
                    reason="emergency_load_shed", operating_mode="emergency",
                ),
            ),
        ),
        duration_s=parse_duration("1h"),
        step_s=5.0,
        verifies=("EMS-T005",),
    )


def _pv_underperformance() -> Scenario:
    config = _base_config(
        start=SUMMER_START + dt.timedelta(hours=3),
        weather=WeatherConfig(cloud_mode="clear"),
        solar=SolarConfig(row_available=(True, True, True, True)),
        battery=BatteryConfig(initial_soc_pct=40.0),
    )
    return Scenario(
        name="pv_underperformance",
        description=(
            "A canopy row goes offline mid-morning and a second follows. "
            "Irradiance-normalised output falls below model, which is the basis "
            "of the pv_generation_underperformance alarm (SDD 37.1)."
        ),
        config=config,
        events=(
            ScenarioEvent(
                parse_duration("1h"), "row 3 string opens",
                lambda site: site.solar.set_row_available(2, False, "string_open"),
            ),
            ScenarioEvent(
                parse_duration("2h"), "row 4 combiner fault",
                lambda site: site.solar.set_row_available(3, False, "combiner_fault"),
            ),
        ),
        duration_s=parse_duration("6h"),
        step_s=10.0,
        verifies=(),
    )


_BUILDERS: tuple[Callable[[], Scenario], ...] = (
    _clear_summer_day,
    _overcast_winter_day,
    _passing_clouds,
    _reserve_decline,
    _generator_support,
    _generator_fails_to_start,
    _generator_fails_during_run,
    _scenario_inverter_fault,
    _thermal_derate,
    _scenario_black_start,
    _blackout_recovery,
    _scenario_comms_loss,
    _sensor_failure,
    _rack_cooling_loss,
    _tier0_shed_refused,
    _pv_underperformance,
)


def all_scenarios() -> dict[str, Scenario]:
    """Build a fresh copy of every scenario (configs are mutable; never share)."""
    return {builder().name: builder() for builder in _BUILDERS}


def scenario_names() -> list[str]:
    return [builder().name for builder in _BUILDERS]


def get_scenario(name: str) -> Scenario:
    """Look up one scenario by name."""
    for builder in _BUILDERS:
        scenario = builder()
        if scenario.name == name:
            return scenario
    available = ", ".join(scenario_names())
    raise KeyError(f"unknown scenario {name!r}; available: {available}")
