"""The simulated homestead: wiring, energy balance, publishing and commands.

:class:`SimulatedSite` is the whole rig. It

* builds the components from a :class:`SiteConfig`,
* solves an explicit energy balance every step (PV -> inverters -> loads,
  battery, generator),
* publishes every point as a :class:`~homestead_twin.envelope.TelemetryEnvelope`
  at the point's configured interval,
* publishes retained availability on start and ``offline`` on stop, exactly like
  a device with a last will (SDD 8.2), and
* subscribes to the command topics, applies commands to the owning component and
  answers with ``accepted``/``succeeded`` or ``rejected`` plus a reason.

Every topic comes from :mod:`homestead_twin.topics` and every payload from
:mod:`homestead_twin.envelope`; the simulator never formats a topic or a JSON
document itself.

Step order matters and is deliberate::

    weather -> solar physics -> loads -> rack -> generator
            -> ENERGY BALANCE (battery integration happens here)
            -> battery emit -> inverter emit -> solar emit
            -> black-start sequencer -> publish

Two couplings are one step old: the load bank reads the rack's IT power, and the
rack reads whether cooling is connected. Both are slow relative to any step size
this rig uses, and breaking the cycle explicitly is better than hiding it.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from homestead_twin import topics
from homestead_twin.config import Settings, get_settings
from homestead_twin.envelope import (
    AvailabilityEnvelope,
    CommandAckEnvelope,
    CommandEnvelope,
    EventEnvelope,
    TelemetryEnvelope,
    parse_command,
)
from homestead_twin.mqtt import Message, MessageBus
from simulator.clock import DEFAULT_START, DEFAULT_UTC_OFFSET_H, Pacer, SimClock, build_pacer
from simulator.components.base import (
    CommandOutcome,
    Component,
    PointCatalog,
    PointSpec,
    Reading,
    SiteContext,
    load_catalog,
)
from simulator.components.battery import BatteryBank, BatteryConfig
from simulator.components.generator import Generator, GeneratorConfig
from simulator.components.inverter import InverterConfig, InverterFarm
from simulator.components.loads import LoadBank, LoadConfig
from simulator.components.rack import RackConfig, ServerRack
from simulator.components.solar import SolarArray, SolarConfig
from simulator.components.weather import Weather, WeatherConfig

logger = logging.getLogger(__name__)

SITE_SOURCE = "simulator"

#: Assets are grouped behind simulated gateways. A ``comms_loss`` event silences
#: one gateway, which is how staleness detection gets exercised (SDD 26.6).
DEFAULT_GATEWAYS: Mapping[str, str] = {
    "pv": "pv",
    "power": "power",
    "loads": "loads",
    "rack": "rack",
}


@dataclass
class SiteConfig:
    """Everything needed to build one simulated site."""

    seed: int = 1
    start: dt.datetime = DEFAULT_START
    utc_offset_h: float = DEFAULT_UTC_OFFSET_H
    base_topic: str = "homestead"

    weather: WeatherConfig = field(default_factory=WeatherConfig)
    solar: SolarConfig = field(default_factory=SolarConfig)
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    inverter: InverterConfig = field(default_factory=InverterConfig)
    generator: GeneratorConfig = field(default_factory=GeneratorConfig)
    rack: RackConfig = field(default_factory=RackConfig)
    loads: Sequence[LoadConfig] | None = None

    #: Site starts de-energized (black-start scenarios).
    start_de_energized: bool = False
    #: Delay before an unreachable gateway's last will is published, emulating
    #: broker keepalive detection.
    will_delay_s: float = 30.0
    #: Battery charge target the generator aims for while running (SDD 34.4).
    generator_charge_target_soc_pct: float = 80.0
    #: DC-side losses between the array and the inverter DC input.
    dc_wiring_efficiency: float = 0.985

    def seeded(self, seed: int) -> "SiteConfig":
        """Return a copy with ``seed`` pushed into every component config."""
        return replace(
            self,
            seed=seed,
            weather=replace(self.weather, seed=seed),
            solar=replace(self.solar, seed=seed),
            inverter=replace(self.inverter, seed=seed),
            generator=replace(self.generator, seed=seed),
            rack=replace(self.rack, seed=seed),
        )


@dataclass
class EnergyBalance:
    """The result of one balance solve. Published nowhere; inspected by tests."""

    pv_available_dc_kw: float = 0.0
    pv_delivered_dc_kw: float = 0.0
    pv_ac_kw: float = 0.0
    generator_kw: float = 0.0
    load_kw: float = 0.0
    battery_kw: float = 0.0  # + charging
    curtailed_kw: float = 0.0
    unserved_kw: float = 0.0
    inverter_capacity_kw: float = 0.0
    ac_bus_energized: bool = True


@dataclass
class SensorFault:
    """An injected measurement fault applied at publish time."""

    mode: str  # frozen | bad | stale | offset
    quality: str = "bad"
    offset: float = 0.0
    frozen_value: Any = None


@dataclass
class SiteStats:
    """Rolling counters for the CLI summary."""

    steps: int = 0
    telemetry_messages: int = 0
    availability_messages: int = 0
    event_messages: int = 0
    commands_received: int = 0
    commands_accepted: int = 0
    commands_rejected: int = 0
    acks_published: int = 0
    pv_energy_kwh: float = 0.0
    load_energy_kwh: float = 0.0
    curtailed_energy_kwh: float = 0.0
    unserved_energy_kwh: float = 0.0
    generator_energy_kwh: float = 0.0
    soc_min_pct: float = 100.0
    soc_max_pct: float = 0.0
    pv_peak_kw: float = 0.0
    load_peak_kw: float = 0.0
    blackout_s: float = 0.0
    rejections: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


class SimulatedSite:
    """A whole homestead behind an MQTT bus."""

    def __init__(
        self,
        bus: MessageBus,
        settings: Settings | None = None,
        config: SiteConfig | None = None,
        catalog: PointCatalog | None = None,
    ) -> None:
        self.bus = bus
        self.settings = settings or get_settings()
        self.config = config or SiteConfig()
        self.base_topic = self.config.base_topic or self.settings.mqtt_base_topic
        self.catalog = catalog or load_catalog(self.settings.data_dir)
        self.clock = SimClock(start=self.config.start, utc_offset_h=self.config.utc_offset_h)

        cfg = self.config
        self.weather = Weather(self.catalog, cfg.weather)
        self.solar = SolarArray(self.catalog, cfg.solar)
        self.battery = BatteryBank(self.catalog, cfg.battery)
        self.inverters = InverterFarm(self.catalog, cfg.inverter)
        self.generator = Generator(self.catalog, cfg.generator)
        self.loads = LoadBank(
            self.catalog, cfg.loads, seed=cfg.seed, utc_offset_h=cfg.utc_offset_h
        )
        self.rack = ServerRack(self.catalog, cfg.rack)
        self.components: list[Component] = [
            self.weather,
            self.solar,
            self.loads,
            self.rack,
            self.generator,
            self.battery,
            self.inverters,
        ]

        self.context = SiteContext(seed=cfg.seed)
        self.context.battery_soc_pct = self.battery.soc_pct
        self.context.container_temperature_c = self.rack.container_temperature_c
        self.balance = EnergyBalance()
        self.stats = SiteStats()

        # -- publishing state -------------------------------------------------
        self.specs: dict[str, PointSpec] = {}
        for component in self.components:
            for spec in component.points():
                self.specs[spec.point_id] = spec
        self._last_published: dict[str, float] = {}
        self._last_value: dict[str, Any] = {}
        self._sequence = 0
        self.sensor_faults: dict[str, SensorFault] = {}
        self.offline_gateways: dict[str, float] = {}
        self.asset_gateway: dict[str, str] = self._build_gateway_map()
        self._started = False
        self._subscribed = False

        # -- black start ---------------------------------------------------------
        self.black_start_stage = 0
        self.black_start_timer_s = 0.0
        self.black_start_active = False
        self._blackout_reason = ""

        if cfg.start_de_energized:
            self.trigger_blackout("initial_state")

    # ------------------------------------------------------------------
    # topology
    # ------------------------------------------------------------------
    def _build_gateway_map(self) -> dict[str, str]:
        """Assign every simulated asset to a gateway for comms-loss injection."""
        mapping: dict[str, str] = {}
        for asset_id in self.solar.asset_ids():
            mapping[asset_id] = "pv"
        for component in (self.battery, self.inverters, self.generator):
            for asset_id in component.asset_ids():
                mapping[asset_id] = "power"
        for asset_id in self.loads.asset_ids():
            mapping[asset_id] = "loads"
        for asset_id in self.rack.asset_ids():
            mapping[asset_id] = "rack"
        return mapping

    def asset_ids(self) -> list[str]:
        return list(self.asset_gateway)

    def point_ids(self) -> list[str]:
        return list(self.specs)

    def gateway_assets(self, gateway: str) -> list[str]:
        return [a for a, g in self.asset_gateway.items() if g == gateway]

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Publish retained availability and subscribe to the command topics."""
        if self._started:
            return
        self._started = True
        if not self._subscribed:
            self.bus.subscribe(topics.command_subscription(self.base_topic), self._on_command)
            self._subscribed = True
        for asset_id in self.asset_ids():
            self._publish_availability(asset_id, "online")

    def stop(self) -> None:
        """Publish the ``offline`` state a last will would have produced."""
        if not self._started:
            return
        for asset_id in self.asset_ids():
            self._publish_availability(asset_id, "offline")
        self._started = False

    # ------------------------------------------------------------------
    # publishing
    # ------------------------------------------------------------------
    def _publish_availability(self, asset_id: str, state: str) -> None:
        envelope = AvailabilityEnvelope(
            asset_id=asset_id, state=state, ts=self.clock.now(), source=SITE_SOURCE
        )
        self.bus.publish(
            topics.availability_topic(asset_id, self.base_topic),
            envelope.to_payload(),
            qos=1,
            retain=True,
        )
        self.stats.availability_messages += 1

    def publish_event(self, asset_id: str, event: str, detail: dict[str, Any] | None = None) -> None:
        """Publish a discrete occurrence (SDD 37.2)."""
        envelope = EventEnvelope(
            ts=self.clock.now(),
            asset_id=asset_id,
            event=event,
            detail=detail or None,
            source=SITE_SOURCE,
        )
        self.bus.publish(
            topics.event_topic(asset_id, event, self.base_topic), envelope.to_payload(), qos=1
        )
        self.stats.event_messages += 1
        self.stats.events.append(f"{asset_id}/{event}")

    def _apply_sensor_fault(self, point_id: str, value: Any, quality: str) -> tuple[Any, str, bool]:
        """Return ``(value, quality, publish)`` after any injected sensor fault."""
        fault = self.sensor_faults.get(point_id)
        if fault is None:
            return value, quality, True
        if fault.mode == "stale":
            # The point simply stops updating; the consumer must notice.
            return value, quality, False
        if fault.mode == "frozen":
            if fault.frozen_value is None:
                fault.frozen_value = self._last_value.get(point_id, value)
            return fault.frozen_value, fault.quality, True
        if fault.mode == "offset":
            try:
                return float(value) + fault.offset, fault.quality, True
            except (TypeError, ValueError):
                return value, fault.quality, True
        # "bad": an out-of-range reading with an explicit bad quality code.
        return -999.0, fault.quality, True

    def _publish_points(self, values: Mapping[str, Any]) -> int:
        now = self.clock.now()
        elapsed = self.clock.elapsed_s
        published = 0
        for point_id, raw in values.items():
            spec = self.specs.get(point_id)
            if spec is None:
                raise KeyError(f"undeclared point {point_id!r}")
            gateway = self.asset_gateway.get(spec.asset_id)
            if gateway in self.offline_gateways:
                continue
            if isinstance(raw, Reading):
                value, quality = raw.value, raw.quality
            else:
                value, quality = raw, "good"
            value, quality, allowed = self._apply_sensor_fault(point_id, value, quality)
            if not allowed:
                continue
            last = self._last_published.get(point_id)
            changed = spec.is_discrete and self._last_value.get(point_id, object()) != value
            due = last is None or (elapsed - last) >= spec.publish_interval_s
            if not (due or changed):
                continue
            self._sequence += 1
            envelope = TelemetryEnvelope(
                ts=now,
                asset_id=spec.asset_id,
                point=spec.name,
                value=value,
                unit=spec.unit,
                quality=quality,
                source=SITE_SOURCE,
                sequence=self._sequence,
            )
            self.bus.publish(
                topics.telemetry_topic(spec.asset_id, spec.name, self.base_topic),
                envelope.to_payload(),
                qos=0,
            )
            self._last_published[point_id] = elapsed
            self._last_value[point_id] = value
            published += 1
        self.stats.telemetry_messages += published
        return published

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    def _on_command(self, message: Message) -> None:
        """Handle one command message. Never raises into the bus."""
        try:
            command = parse_command(message.payload)
        except Exception as exc:  # malformed payload: dead-letter it
            logger.warning("simulator: undecodable command on %s: %s", message.topic, exc)
            return
        self.stats.commands_received += 1
        self.apply_command(command)

    def apply_command(self, command: CommandEnvelope) -> CommandAckEnvelope:
        """Route a command to its component and publish the acknowledgement(s)."""
        if command.asset_id not in self.asset_gateway:
            return self._ack(command, "rejected", "unknown_asset")
        if command.expires_at is not None and command.expires_at <= self.clock.now():
            return self._ack(command, "expired", "command_expired_before_delivery")

        outcome: CommandOutcome | None = None
        for component in self.components:
            outcome = component.handle_command(command)
            if outcome is not None:
                break
        if outcome is None:
            return self._ack(command, "rejected", f"unsupported_command:{command.command}")
        if not outcome.accepted:
            self.stats.commands_rejected += 1
            self.stats.rejections.append(f"{command.asset_id}/{command.command}:{outcome.detail}")
            return self._ack(command, "rejected", outcome.detail, outcome.payload)

        self.stats.commands_accepted += 1
        # Two-stage acknowledgement: the device accepts, then reports the result.
        self._ack(command, "accepted", outcome.detail, outcome.payload)
        if outcome.event:
            self.publish_event(command.asset_id, outcome.event, {"command_id": command.command_id})
        if outcome.completed:
            return self._ack(command, "succeeded", outcome.detail, outcome.payload)
        # Sequenced actions (shed delays, generator cranking) succeed later; the
        # simulator reports them as accepted now and the state points show the
        # progress, exactly as a real controller with a multi-step sequence does.
        return self._ack(command, "succeeded", f"in_progress:{outcome.detail}", outcome.payload)

    def _ack(
        self,
        command: CommandEnvelope,
        result: str,
        detail: str,
        payload: dict[str, Any] | None = None,
    ) -> CommandAckEnvelope:
        envelope = CommandAckEnvelope(
            command_id=command.command_id,
            asset_id=command.asset_id,
            result=result,  # type: ignore[arg-type]
            detail=detail,
            reported_by=SITE_SOURCE,
            reported_at=self.clock.now(),
            payload=payload,
        )
        self.bus.publish(
            topics.command_ack_topic(command.asset_id, command.command, self.base_topic),
            envelope.to_payload(),
            qos=1,
        )
        self.stats.acks_published += 1
        return envelope

    # ------------------------------------------------------------------
    # fault injection
    # ------------------------------------------------------------------
    def inject_sensor_fault(
        self, point_id: str, mode: str = "bad", quality: str = "bad", offset: float = 0.0
    ) -> None:
        """Corrupt one point at publish time (SDD 39 case ``EMS-T003``)."""
        if point_id not in self.specs:
            raise KeyError(f"unknown point {point_id!r}")
        if mode not in ("frozen", "bad", "stale", "offset"):
            raise ValueError(f"unknown sensor fault mode {mode!r}")
        self.sensor_faults[point_id] = SensorFault(mode=mode, quality=quality, offset=offset)

    def clear_sensor_fault(self, point_id: str) -> None:
        self.sensor_faults.pop(point_id, None)

    def lose_comms(self, gateway: str) -> None:
        """Silence a gateway. Its assets stop publishing; the will follows later."""
        if gateway not in set(self.asset_gateway.values()):
            raise KeyError(f"unknown gateway {gateway!r}")
        self.offline_gateways.setdefault(gateway, self.clock.elapsed_s)

    def restore_comms(self, gateway: str) -> None:
        if self.offline_gateways.pop(gateway, None) is not None:
            for asset_id in self.gateway_assets(gateway):
                self._publish_availability(asset_id, "online")

    def trigger_blackout(self, reason: str = "injected") -> None:
        """Collapse the AC system: every inverter stops and all loads drop."""
        self._blackout_reason = reason
        self.inverters.stop_all(reason)
        self.battery.open_contactor(reason)
        self.loads.lock_out_all("bus_de_energized")
        self.context.ac_bus_energized = False
        self.context.critical_bus_energized = False
        self.black_start_active = False
        self.black_start_stage = 0
        self.context.black_start_stage = "de_energized"
        if self._started:
            self.publish_event(
                self.generator.config.ats_asset_id, "ac_blackout", {"reason": reason}
            )

    def request_black_start(self) -> bool:
        """Begin the SDD 35.3 recovery sequence. Refused while a fault persists."""
        if self.context.ac_bus_energized:
            return False
        if self.battery.fault_active:
            return False
        self.black_start_active = True
        self.black_start_stage = 1
        self.black_start_timer_s = 0.0
        self.context.black_start_stage = "started"
        self.publish_event("energy.bms.power_container.01", "black_start_initiated", {})
        return True

    # ------------------------------------------------------------------
    # black start sequencer
    # ------------------------------------------------------------------
    #: (stage name, dwell seconds before advancing) following SDD 35.3.
    BLACK_START_STAGES: tuple[tuple[str, float], ...] = (
        ("verify_isolation", 10.0),
        ("energize_controls", 10.0),
        ("close_contactor", 15.0),
        ("start_master_inverter", 30.0),
        ("energize_critical_bus", 20.0),
        ("start_control_node", 20.0),
        ("validate_measurements", 15.0),
        ("stagger_critical_loads", 60.0),
        ("restore_general_loads", 60.0),
        ("complete", 0.0),
    )

    #: Loads restored in stage 8, in order (critical survival first, SDD 35.3.8).
    CRITICAL_RESTORE_ORDER = (
        "energy.load.site.control_core_01",
        "energy.load.site.rack_cooling_01",
        "energy.load.site.battery_hvac_01",
        "energy.load.site.server_rack_01",
        "energy.load.site.water_pumping_01",
    )
    #: Restored in stage 9. Attended loads are deliberately excluded -- SDD 35.3
    #: forbids unattended restart of workshop or spa equipment.
    GENERAL_RESTORE_ORDER = (
        "energy.load.site.greenhouse_climate_01",
        "energy.load.site.greenhouse_lighting_01",
        "energy.load.site.irrigation_01",
        "energy.load.site.tool_charging_01",
        "energy.load.site.opportunistic_compute_01",
    )

    def _step_black_start(self, dt_s: float) -> None:
        if not self.black_start_active:
            return
        stage_name, dwell = self.BLACK_START_STAGES[self.black_start_stage - 1]
        if self.black_start_timer_s == 0.0:
            self._enter_black_start_stage(stage_name)
        self.black_start_timer_s += dt_s
        self.context.black_start_stage = stage_name
        if self.black_start_timer_s >= dwell and self.black_start_stage < len(
            self.BLACK_START_STAGES
        ):
            self.black_start_stage += 1
            self.black_start_timer_s = 0.0
            if self.black_start_stage == len(self.BLACK_START_STAGES):
                self.black_start_active = False
                self.context.black_start_stage = "complete"
                self.publish_event(
                    "energy.bms.power_container.01", "black_start_completed", {}
                )

    def _enter_black_start_stage(self, stage_name: str) -> None:
        if stage_name == "close_contactor":
            self.battery.close_contactor()
        elif stage_name == "start_master_inverter":
            self.inverters.start_unit(0)
        elif stage_name == "energize_critical_bus":
            self.context.critical_bus_energized = True
        elif stage_name == "start_control_node":
            self.loads.restore_from_lockout("energy.load.site.control_core_01")
        elif stage_name == "validate_measurements":
            for index in range(1, len(self.inverters.units)):
                self.inverters.start_unit(index)
        elif stage_name == "stagger_critical_loads":
            for asset_id in self.CRITICAL_RESTORE_ORDER:
                self.loads.restore_from_lockout(asset_id)
        elif stage_name == "restore_general_loads":
            for asset_id in self.GENERAL_RESTORE_ORDER:
                self.loads.restore_from_lockout(asset_id)
        self.publish_event(
            "energy.bms.power_container.01", "black_start_stage", {"stage": stage_name}
        )

    # ------------------------------------------------------------------
    # energy balance
    # ------------------------------------------------------------------
    def _generator_dispatch(self, deficit_kw: float) -> float:
        """How hard to load the generator while it carries the bus (SDD 34.4)."""
        generator = self.generator
        if not generator.loaded:
            return 0.0
        charge_target = 0.0
        if self.battery.soc_pct < self.config.generator_charge_target_soc_pct:
            charge_target = min(
                self.battery.limits()[0], generator.config.rated_power_kw - max(deficit_kw, 0.0)
            )
        wanted = max(deficit_kw, 0.0) + max(charge_target, 0.0)
        # Never run the machine below its minimum loading if it is running at all.
        return max(min(wanted, generator.config.rated_power_kw), generator.config.minimum_load_kw)

    def solve_balance(self, dt_s: float) -> EnergyBalance:
        """Solve PV -> inverters -> loads / battery / generator for one step.

        The order of clamping is the physical order: conversion capacity first,
        then BMS limits, then what is left over becomes curtailment (surplus) or
        unserved load (deficit).
        """
        context = self.context
        balance = EnergyBalance()
        cfg = self.config

        capacity_kw = self.inverters.available_capacity_kw(self.rack.container_temperature_c)
        context.inverter_capacity_kw = capacity_kw
        balance.inverter_capacity_kw = capacity_kw

        pv_dc_available = context.pv_available_dc_kw
        balance.pv_available_dc_kw = pv_dc_available
        conversion = cfg.dc_wiring_efficiency * self.inverters.config.efficiency
        # What the array could put on the bus, and what the conversion stage can
        # actually pass. The difference is inverter clipping, which counts as
        # curtailment just as much as backing off for a full battery does.
        pv_ac_potential = pv_dc_available * conversion
        pv_ac_possible = min(pv_ac_potential, capacity_kw)

        load_kw = context.load_actual_kw
        balance.load_kw = load_kw

        if capacity_kw <= 0.0 and not self.generator.loaded:
            # No conversion capability and no generator: the bus is dead.
            balance.ac_bus_energized = False
            balance.unserved_kw = load_kw
            self._settle(balance, dt_s, 0.0, pv_ac_kw=0.0, pv_ac_potential=pv_ac_potential)
            return balance

        generator_kw = self._generator_dispatch(load_kw - pv_ac_possible)
        self.generator.set_load(generator_kw)
        generator_kw = self.generator.output_kw
        balance.generator_kw = generator_kw

        surplus = pv_ac_possible + generator_kw - load_kw
        charge_limit, discharge_limit = self.battery.limits()
        if surplus >= 0:
            # Charge with what is spare, up to the BMS limit and the conversion
            # headroom; whatever the battery will not take is curtailed PV.
            headroom = max(0.0, capacity_kw - max(0.0, load_kw - generator_kw))
            request = min(surplus, charge_limit, headroom)
        else:
            deficit = -surplus
            headroom = max(0.0, capacity_kw - pv_ac_possible - generator_kw)
            request = -min(deficit, discharge_limit, headroom)

        self._settle(
            balance,
            dt_s,
            battery_request_kw=request,
            pv_ac_kw=pv_ac_possible,
            pv_ac_potential=pv_ac_potential,
        )
        return balance

    def _settle(
        self,
        balance: EnergyBalance,
        dt_s: float,
        battery_request_kw: float,
        pv_ac_kw: float,
        pv_ac_potential: float,
    ) -> None:
        """Integrate the battery and reconcile the AC side with what it accepted."""
        accepted = self.battery.integrate(battery_request_kw, dt_s)
        balance.battery_kw = accepted
        # The battery-zone HVAC load is what keeps the cells inside their safe
        # band; shed it and the cells follow container air instead.
        hvac = self.context.loads.get("energy.load.site.battery_hvac_01")
        self.battery.update_thermal(
            dt_s, self.context.container_temperature_c, hvac.power_kw if hvac else 0.0
        )

        served = balance.load_kw
        supply = pv_ac_kw + balance.generator_kw - accepted
        if supply >= served - 1e-9:
            # Surplus: the array backs off by whatever nothing wanted.
            balance.pv_ac_kw = max(0.0, pv_ac_kw - (supply - served))
            balance.unserved_kw = 0.0
        else:
            balance.pv_ac_kw = pv_ac_kw
            balance.unserved_kw = served - supply
        # Curtailment is everything the array could have produced but did not,
        # whether the battery refused it or the inverter block clipped it.
        balance.curtailed_kw = max(0.0, pv_ac_potential - balance.pv_ac_kw)

        conversion = self.config.dc_wiring_efficiency * self.inverters.config.efficiency
        balance.pv_delivered_dc_kw = balance.pv_ac_kw / conversion if conversion else 0.0
        self.solar.apply_delivered(balance.pv_delivered_dc_kw, dt_s, self.clock.now())

        # Net AC out of the inverter block, and the DC that produced it.
        ac_net = balance.pv_ac_kw + max(0.0, -accepted) - max(0.0, accepted)
        dc_net = balance.pv_delivered_dc_kw - accepted
        self.inverters.apply_dispatch(ac_net, dc_net)

        context = self.context
        context.pv_delivered_dc_kw = balance.pv_delivered_dc_kw
        context.pv_curtailed_kw = balance.curtailed_kw
        context.inverter_ac_output_kw = ac_net
        context.unserved_load_kw = balance.unserved_kw
        context.battery_power_kw = accepted

    # ------------------------------------------------------------------
    # stepping
    # ------------------------------------------------------------------
    def step(self, dt_s: float = 5.0) -> EnergyBalance:
        """Advance the whole site by ``dt_s`` seconds and publish what is due."""
        if not self._started:
            self.start()
        now = self.clock.advance(dt_s)
        context = self.context
        context.step_index = self.clock.steps

        values: dict[str, Any] = {}
        values.update(self.weather.step(now, dt_s, context))
        self.solar.compute(now, dt_s, context)
        values.update(self.loads.step(now, dt_s, context))
        values.update(self.rack.step(now, dt_s, context))
        values.update(self.generator.step(now, dt_s, context))

        balance = self.solve_balance(dt_s)
        self.balance = balance
        self._apply_bus_state(balance, dt_s)

        values.update(self.battery.step(now, dt_s, context))
        values.update(self.inverters.step(now, dt_s, context))
        values.update(self.solar.step(now, dt_s, context))

        self._step_black_start(dt_s)
        self._step_wills(dt_s)
        self._drain_component_events()
        self._publish_points(values)
        self._accumulate(balance, dt_s)
        return balance

    def _apply_bus_state(self, balance: EnergyBalance, dt_s: float) -> None:
        """Decide whether the AC bus survived this step."""
        context = self.context
        was_energized = context.ac_bus_energized
        energized = balance.ac_bus_energized and (
            self.inverters.running_count() > 0 or self.generator.loaded
        )
        if energized and balance.unserved_kw > 0.25 * max(balance.load_kw, 1e-9):
            # More than a quarter of the load cannot be served: the bus collapses.
            energized = False
            self._blackout_reason = "supply_deficit"
        context.ac_bus_energized = energized
        if energized:
            context.critical_bus_energized = True
        elif was_energized:
            self.trigger_blackout(self._blackout_reason or "supply_deficit")

    def _step_wills(self, dt_s: float) -> None:
        """Publish the retained ``offline`` will for gateways that stayed silent."""
        for gateway, since in list(self.offline_gateways.items()):
            if since < 0:
                continue
            if self.clock.elapsed_s - since >= self.config.will_delay_s:
                for asset_id in self.gateway_assets(gateway):
                    self._publish_availability(asset_id, "offline")
                self.offline_gateways[gateway] = -1.0

    def _drain_component_events(self) -> None:
        for asset_id, event, detail in self.loads.drain_events():
            self.publish_event(asset_id, event, detail)
        for event, detail in self.generator.drain_events():
            self.publish_event(self.generator.config.asset_id, event, detail)

    def _accumulate(self, balance: EnergyBalance, dt_s: float) -> None:
        stats = self.stats
        hours = dt_s / 3600.0
        stats.steps += 1
        stats.pv_energy_kwh += balance.pv_ac_kw * hours
        stats.load_energy_kwh += balance.load_kw * hours
        stats.curtailed_energy_kwh += balance.curtailed_kw * hours
        stats.unserved_energy_kwh += balance.unserved_kw * hours
        stats.generator_energy_kwh += balance.generator_kw * hours
        soc = self.battery.soc_pct
        stats.soc_min_pct = min(stats.soc_min_pct, soc)
        stats.soc_max_pct = max(stats.soc_max_pct, soc)
        stats.pv_peak_kw = max(stats.pv_peak_kw, balance.pv_ac_kw)
        stats.load_peak_kw = max(stats.load_peak_kw, balance.load_kw)
        if not self.context.ac_bus_energized:
            stats.blackout_s += dt_s

    def run(
        self,
        duration_s: float,
        dt_s: float = 5.0,
        speed: float | None = None,
        pacer: Pacer | None = None,
        on_step: Callable[["SimulatedSite", EnergyBalance], None] | None = None,
    ) -> SiteStats:
        """Run for ``duration_s`` of simulated time.

        ``speed`` is simulated seconds per wall-clock second; ``None`` or ``inf``
        runs as fast as the host allows. ``pacer`` overrides it entirely, which
        is how tests exercise the run loop without sleeping.
        """
        if not self._started:
            self.start()
        pacer = pacer or build_pacer(speed)
        pacer.reset()
        remaining = duration_s
        while remaining > 1e-9:
            this_step = min(dt_s, remaining)
            balance = self.step(this_step)
            if on_step is not None:
                on_step(self, balance)
            pacer.pace(this_step)
            remaining -= this_step
        return self.stats

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Flat view of the interesting state, for CLI summaries and tests."""
        context = self.context
        return {
            "time": self.clock.now().isoformat(),
            "elapsed_s": self.clock.elapsed_s,
            "soc_pct": round(self.battery.soc_pct, 2),
            "battery_kw": round(self.battery.power_kw, 3),
            "cell_temperature_c": round(self.battery.cell_temperature_c, 2),
            "pv_available_dc_kw": round(context.pv_available_dc_kw, 3),
            "pv_ac_kw": round(self.balance.pv_ac_kw, 3),
            "curtailed_kw": round(self.balance.curtailed_kw, 3),
            "load_kw": round(context.load_actual_kw, 3),
            "critical_load_kw": round(context.critical_load_kw, 3),
            "unserved_kw": round(self.balance.unserved_kw, 3),
            "irradiance_w_m2": round(context.poa_irradiance_w_m2, 1),
            "generator_state": self.generator.state,
            "generator_kw": round(self.generator.output_kw, 3),
            "fuel_pct": round(self.generator.fuel_pct, 2),
            "inverters_online": self.inverters.running_count(),
            "container_temperature_c": round(self.rack.container_temperature_c, 2),
            "ac_bus_energized": context.ac_bus_energized,
            "black_start_stage": context.black_start_stage,
        }
