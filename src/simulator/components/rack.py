"""Server rack, UPS, PDUs, NetBotz environmental monitoring and safety sensors.

Models the IT half of the power container: ``it.rack.power_container.01``, the
Dell hosts, the secondary control node, ``energy.ups.rack_01.01``, the three rack
PDUs, both NetBotz appliances, the four APC safety sensors and the alarm beacon.

The reason this component exists in an *energy* simulator is the thermal
coupling. SDD 16.1 flags the shared power/battery/rack container as a
common-mode risk, SDD 37.1 lists ``power_container_cooling_failed`` as a
critical alarm, and SDD 30.5 makes "battery/inverter room temperature and
cooling availability" an EMS input. So:

    IT load -> container heat -> container air temperature
             -> inverter thermal derate and battery cell temperature
             -> less available power

Lose cooling and the electrical system degrades. That loop is the point of the
``rack_cooling_loss`` scenario.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass
from typing import Any

from homestead_twin.envelope import CommandEnvelope
from simulator.components.base import (
    CommandOutcome,
    Component,
    PointCatalog,
    SiteContext,
    approach,
    clamp,
)

RACK_ASSET = "it.rack.power_container.01"
UPS_ASSET = "energy.ups.rack_01.01"
PDU_SWITCHED = "energy.pdu.rack_01.switched_01"
PDU_BASIC = "energy.pdu.rack_01.basic_01"
SERVER_R740XD = "it.server.rack_01.r740xd_01"
SERVER_T7820 = "it.server.rack_01.t7820_01"
SECONDARY_NODE = "it.server.secondary_control_node.01"
NETBOTZ_CONTAINER = "it.environmental_monitor.power_container.netbotz_500_01"
NETBOTZ_RACK = "it.environmental_monitor.rack_01.nbrk0550_01"
SENSOR_TEMP_HUMIDITY = "safety.safety_sensor.rack_01.ap9512thblk_01"
SENSOR_TEMP = "safety.safety_sensor.rack_01.ap9512tblk_01"
SENSOR_SMOKE = "safety.safety_sensor.rack_01.nbes0307_01"
SENSOR_FLUID = "safety.safety_sensor.power_container.nbes0308_01"
BEACON = "safety.alarm_output.rack_01.beacon_01"
PDU_RESERVE = "energy.pdu.rack_01.ap9570_01"
SWITCH_ACCESS = "it.switch.rack_01.catalyst_2960x_01"
SWITCH_CORE = "it.switch.rack_01.arista_7050qx_01"
ROUTER = "it.router.rack_01.isr4321_01"
WLC = "it.wireless_controller.rack_01.wlc5508_01"


@dataclass
class ServerSpec:
    asset_id: str
    idle_w: float
    peak_w: float
    base_utilization_pct: float
    #: Amplitude of the diurnal utilisation swing, in percentage points.
    swing_pct: float = 20.0
    memory_pct: float = 45.0


@dataclass
class RackConfig:
    """Rack, UPS, PDU and container thermal parameters."""

    servers: tuple[ServerSpec, ...] = (
        ServerSpec(SERVER_R740XD, idle_w=280.0, peak_w=620.0, base_utilization_pct=35.0),
        ServerSpec(SERVER_T7820, idle_w=150.0, peak_w=420.0, base_utilization_pct=20.0, swing_pct=25.0),
        ServerSpec(SECONDARY_NODE, idle_w=12.0, peak_w=35.0, base_utilization_pct=8.0, swing_pct=5.0),
    )
    #: Switches, router, WLC, storage shelves and phones, lumped.
    network_load_w: float = 320.0
    #: UPS nameplate (register: ``rated_w: 1500``) and its efficiency.
    ups_rated_w: float = 1500.0
    ups_efficiency: float = 0.94
    #: Runtime at full load, minutes; scaled inversely with actual load.
    ups_full_load_runtime_min: float = 6.0
    ups_recharge_time_s: float = 1800.0

    #: Container thermal model. The capacity is that of the *air* and the light
    #: internal mass -- the battery has its own, much slower, thermal model --
    #: and the conductance is an insulated 20-foot container (~65 m2 at
    #: ~1.4 W/m2K). Together they give a time constant near 100 minutes and an
    #: equilibrium 15-20 C above ambient with cooling lost, which is what pushes
    #: the inverter block past its 40 C derate threshold.
    container_thermal_capacity_kwh_per_c: float = 0.15
    container_conductance_kw_per_c: float = 0.09  # envelope loss to outside air
    cooling_capacity_kw_thermal: float = 6.0
    cooling_setpoint_c: float = 24.0
    cooling_deadband_c: float = 2.0
    cooling_electrical_kw: float = 0.9
    #: Extra container heat from conversion and battery losses, as a fraction of
    #: the electrical power flowing through the power zone.
    conversion_loss_fraction: float = 0.03

    rack_delta_t_per_kw: float = 6.0  # inlet-to-exhaust rise per kW of IT load
    initial_container_temperature_c: float = 22.0
    outlet_count_switched: int = 10
    outlet_count_basic: int = 8
    #: 4 x C19 at 208 V (register); the reserve feed carries a light standing load.
    outlet_count_reserve: int = 4
    reserve_pdu_kw: float = 0.35

    #: Network gear. The ``switch`` and ``router`` classes carry no power point,
    #: so their draw stays inside ``network_load_w``; what they publish is
    #: reachability and health, which is what the EMS data-quality state and the
    #: SDD 39 case EMS-T001 ("lose internet") depend on.
    access_switch_ports: int = 44
    core_switch_ports: int = 28
    planned_ap_count: int = 5  # register: ``planned_ap_count``
    seed: int = 0


class ServerRack(Component):
    """Rack IT load, UPS behaviour, PDU metering and container temperature."""

    name = "rack"

    def __init__(self, catalog: PointCatalog, config: RackConfig | None = None) -> None:
        super().__init__(catalog)
        self.config = config or RackConfig()
        cfg = self.config
        self.random = random.Random(f"{cfg.seed}:rack")
        self.container_temperature_c = cfg.initial_container_temperature_c
        self.rack_inlet_c = cfg.initial_container_temperature_c
        self.rack_exhaust_c = cfg.initial_container_temperature_c + 6.0
        self.humidity_pct = 45.0
        self.it_load_kw = 0.0
        self.cooling_kw = 0.0
        self.cooling_active = False
        self.cooling_failed = False
        #: Whether the load bank currently has rack cooling connected; refreshed
        #: from the context at the start of every step.
        self._cooling_permitted = True
        self.ups_on_battery = False
        self.ups_charge_pct = 100.0
        self.door_open = False
        self.smoke_active = False
        self.leak_active = False
        self.server_utilization: dict[str, float] = {
            spec.asset_id: spec.base_utilization_pct for spec in cfg.servers
        }
        self._outlets_switched = [True] * cfg.outlet_count_switched
        self._outlets_basic = [True] * cfg.outlet_count_basic
        self._outlets_reserve = [True] * cfg.outlet_count_reserve
        # Network state. ``wan_up`` is the site's internet link -- dropping it is
        # SDD 39 case EMS-T001, which must change nothing about local control.
        self.wan_up = True
        self.vpn_up = True
        self.ap_online_count = cfg.planned_ap_count
        self.client_count = 8
        self.router_cpu_pct = 18.0

        for spec in cfg.servers:
            points = [
                "power_w",
                "cpu_utilization_pct",
                "memory_used_pct",
                "temperature_cpu_c",
                "availability_state",
            ]
            if spec.asset_id != SECONDARY_NODE:
                # Only the rack Dells carry the ``network_device`` profile.
                points += ["response_time_ms", "packet_loss_pct"]
            self.declare(spec.asset_id, points)
        self.declare(
            UPS_ASSET,
            [
                "load_pct",
                "runtime_remaining_min",
                "input_available",
                "on_battery",
                "battery_replace_due",
                "availability_state",
                "state_operating",
                "mode_actual",
                "alarm_summary",
                "fault_active",
            ],
        )
        self.declare(
            PDU_SWITCHED,
            [
                "power_total_kw",
                "current_total_a",
                "outlet_state",
                "overload_active",
                "availability_state",
                "state_operating",
                "alarm_summary",
            ],
        )
        self.declare(
            PDU_BASIC,
            ["power_total_kw", "current_total_a", "outlet_state", "overload_active", "availability_state"],
        )
        self.declare(
            PDU_RESERVE,
            ["power_total_kw", "current_total_a", "outlet_state", "overload_active", "availability_state"],
        )
        for switch in (SWITCH_ACCESS, SWITCH_CORE):
            self.declare(
                switch,
                ["availability_state", "port_up_count", "temperature_c", "packet_error_rate"],
            )
        self.declare(ROUTER, ["wan_state", "vpn_state", "cpu_utilization_pct", "voice_gateway_state"])
        self.declare(WLC, ["ap_online_count", "client_count", "alarm_summary"])
        self.declare(
            RACK_ASSET,
            [
                "temperature_inlet_c",
                "temperature_exhaust_c",
                "humidity_relative_pct",
                "door_state",
                "smoke_active",
                "leak_active",
                "alarm_summary",
            ],
        )
        for monitor in (NETBOTZ_CONTAINER, NETBOTZ_RACK):
            self.declare(
                monitor,
                [
                    "temperature_air_c",
                    "humidity_relative_pct",
                    "availability_state",
                    "alarm_summary",
                    "heartbeat_age_s",
                ],
            )
        for sensor in (SENSOR_TEMP_HUMIDITY, SENSOR_TEMP, SENSOR_SMOKE, SENSOR_FLUID):
            self.declare(sensor, ["value", "alarm_active", "self_test_due", "battery_pct"])
        self.declare(BEACON, ["state_operating", "enabled_requested", "fault_active"])

    # -- scenario control ----------------------------------------------------
    def fail_cooling(self, failed: bool = True) -> None:
        """Cooling plant failure: the container heats with no relief."""
        self.cooling_failed = failed

    def set_smoke(self, active: bool) -> None:
        self.smoke_active = active

    def set_leak(self, active: bool) -> None:
        self.leak_active = active

    def set_door(self, open_: bool) -> None:
        self.door_open = open_

    def set_wan(self, up: bool) -> None:
        """Drop or restore the internet link (SDD 39 case EMS-T001).

        Local control must be entirely unaffected; only the router's reported
        state and the VPN change.
        """
        self.wan_up = up
        if not up:
            self.vpn_up = False

    def set_outlet(self, index: int, on: bool) -> None:
        self._outlets_switched[index] = on

    # -- commands ---------------------------------------------------------------
    def handle_command(self, command: CommandEnvelope) -> CommandOutcome | None:
        if command.asset_id == PDU_SWITCHED and command.command == "outlet_state":
            payload = command.value if isinstance(command.value, dict) else {}
            try:
                index = int(payload["outlet"])
                state = bool(payload["state"])
            except (KeyError, TypeError, ValueError):
                return CommandOutcome(False, "expected_value={'outlet': int, 'state': bool}")
            if not 0 <= index < len(self._outlets_switched):
                return CommandOutcome(False, f"outlet_out_of_range:{index}")
            if index == 0:
                # Outlet 1 feeds the core switch and the secondary control node.
                return CommandOutcome(False, "outlet_reserved_for_control_core")
            self._outlets_switched[index] = state
            return CommandOutcome(True, f"outlet_{index}={'on' if state else 'off'}")
        if command.asset_id == BEACON and command.command == "enabled_requested":
            return CommandOutcome(True, f"beacon={'on' if command.value else 'off'}")
        return None

    # -- model -----------------------------------------------------------------
    def _server_power_w(self, spec: ServerSpec, now: dt.datetime, dt_s: float) -> float:
        """Utilisation follows a daily curve with bounded noise; power follows it."""
        hour = now.hour + now.minute / 60.0
        diurnal = spec.swing_pct * 0.5 * math.sin(2 * math.pi * (hour - 9.0) / 24.0)
        target = clamp(spec.base_utilization_pct + diurnal, 2.0, 98.0)
        current = self.server_utilization[spec.asset_id]
        blended = approach(current, target, dt_s, 900.0) + self.random.gauss(0.0, 0.6)
        self.server_utilization[spec.asset_id] = clamp(blended, 1.0, 100.0)
        fraction = self.server_utilization[spec.asset_id] / 100.0
        return spec.idle_w + (spec.peak_w - spec.idle_w) * fraction

    def _cooling(self, dt_s: float) -> float:
        """Thermostatic cooling with a deadband; returns thermal kW removed."""
        cfg = self.config
        if self.cooling_failed or not self._cooling_permitted:
            self.cooling_active = False
            self.cooling_kw = 0.0
            return 0.0
        upper = cfg.cooling_setpoint_c + cfg.cooling_deadband_c / 2.0
        lower = cfg.cooling_setpoint_c - cfg.cooling_deadband_c / 2.0
        if self.container_temperature_c > upper:
            self.cooling_active = True
        elif self.container_temperature_c < lower:
            self.cooling_active = False
        if not self.cooling_active:
            self.cooling_kw = 0.0
            return 0.0
        # Modulating: harder the further above setpoint, capped at nameplate.
        excess = self.container_temperature_c - cfg.cooling_setpoint_c
        duty = clamp(0.4 + excess / 4.0, 0.0, 1.0)
        self.cooling_kw = cfg.cooling_electrical_kw * duty
        return cfg.cooling_capacity_kw_thermal * duty

    def step(self, now: dt.datetime, dt_s: float, context: SiteContext) -> dict[str, Any]:
        cfg = self.config
        # The load bank owns whether rack cooling is connected; the rack owns
        # whether the plant works. The coupling is one step old by construction,
        # which is harmless for a thermal mass measured in kWh per degree.
        self._cooling_permitted = context.rack_cooling_available

        # -- IT load ------------------------------------------------------------
        server_powers = {spec.asset_id: self._server_power_w(spec, now, dt_s) for spec in cfg.servers}
        it_w = sum(server_powers.values()) + cfg.network_load_w
        self.it_load_kw = it_w / 1000.0 / cfg.ups_efficiency
        context.rack_it_kw = self.it_load_kw

        # -- container thermal balance -------------------------------------------
        cooling_thermal_kw = self._cooling(dt_s)
        context.rack_cooling_kw = self.cooling_kw
        conversion_heat_kw = cfg.conversion_loss_fraction * (
            abs(context.battery_power_kw) + max(0.0, context.inverter_ac_output_kw)
        )
        heat_in_kw = self.it_load_kw + conversion_heat_kw
        envelope_kw = cfg.container_conductance_kw_per_c * (
            self.container_temperature_c - context.ambient_temperature_c
        )
        net_kw = heat_in_kw - cooling_thermal_kw - envelope_kw
        self.container_temperature_c += net_kw * (dt_s / 3600.0) / cfg.container_thermal_capacity_kwh_per_c
        self.container_temperature_c = clamp(self.container_temperature_c, -30.0, 90.0)
        self.rack_inlet_c = self.container_temperature_c + (2.0 if self.door_open else 0.5)
        self.rack_exhaust_c = self.rack_inlet_c + cfg.rack_delta_t_per_kw * self.it_load_kw
        # Warm air holds relative humidity down for a fixed absolute humidity.
        self.humidity_pct = clamp(
            context.humidity_pct
            * math.exp(-0.045 * (self.container_temperature_c - context.ambient_temperature_c)),
            8.0,
            95.0,
        )
        context.container_temperature_c = self.container_temperature_c
        context.rack_inlet_temperature_c = self.rack_inlet_c

        # -- UPS ------------------------------------------------------------------
        self.ups_on_battery = not context.critical_bus_energized
        load_pct = clamp(100.0 * it_w / cfg.ups_rated_w, 0.0, 150.0)
        if self.ups_on_battery:
            drain_pct = (
                100.0
                * (dt_s / 60.0)
                / max(cfg.ups_full_load_runtime_min * (100.0 / max(load_pct, 1.0)), 1e-6)
            )
            self.ups_charge_pct = clamp(self.ups_charge_pct - drain_pct, 0.0, 100.0)
        else:
            self.ups_charge_pct = clamp(
                self.ups_charge_pct + 100.0 * dt_s / cfg.ups_recharge_time_s, 0.0, 100.0
            )
        runtime_min = (
            cfg.ups_full_load_runtime_min * (self.ups_charge_pct / 100.0) * (100.0 / max(load_pct, 1.0))
        )

        out: dict[str, Any] = {}
        # -- servers ---------------------------------------------------------------
        for spec in cfg.servers:
            watts = server_powers[spec.asset_id]
            utilization = self.server_utilization[spec.asset_id]
            powered = context.critical_bus_energized or self.ups_charge_pct > 0
            self.emit(out, spec.asset_id, "power_w", round(watts if powered else 0.0, 1))
            self.emit(out, spec.asset_id, "cpu_utilization_pct", round(utilization, 1))
            self.emit(
                out,
                spec.asset_id,
                "memory_used_pct",
                round(clamp(spec.memory_pct + utilization * 0.25, 5.0, 99.0), 1),
            )
            self.emit(
                out,
                spec.asset_id,
                "temperature_cpu_c",
                round(self.rack_inlet_c + 18.0 + 0.35 * utilization, 1),
            )
            self.emit(out, spec.asset_id, "availability_state", "online" if powered else "offline")
            if spec.asset_id != SECONDARY_NODE:
                self.emit(out, spec.asset_id, "response_time_ms", round(2.0 + utilization * 0.08, 2))
                self.emit(out, spec.asset_id, "packet_loss_pct", 0.0)

        # -- UPS --------------------------------------------------------------------
        self.emit(out, UPS_ASSET, "load_pct", round(load_pct, 1))
        self.emit(out, UPS_ASSET, "runtime_remaining_min", round(runtime_min, 2))
        self.emit(out, UPS_ASSET, "input_available", context.critical_bus_energized)
        self.emit(out, UPS_ASSET, "on_battery", self.ups_on_battery)
        self.emit(out, UPS_ASSET, "battery_replace_due", False)
        self.emit(out, UPS_ASSET, "availability_state", "online")
        self.emit(
            out,
            UPS_ASSET,
            "state_operating",
            "on_battery" if self.ups_on_battery else "online_normal",
        )
        self.emit(out, UPS_ASSET, "mode_actual", "automatic")
        self.emit(
            out,
            UPS_ASSET,
            "alarm_summary",
            "critical"
            if self.ups_on_battery and runtime_min < 2.0
            else "warning"
            if self.ups_on_battery
            else "none",
        )
        self.emit(out, UPS_ASSET, "fault_active", False)

        # -- PDUs ---------------------------------------------------------------------
        switched_kw = self.it_load_kw * 0.65
        basic_kw = self.it_load_kw * 0.35
        for asset, kw, outlets, voltage, limit_a in (
            (PDU_SWITCHED, switched_kw, self._outlets_switched, 120.0, 15.0),
            (PDU_BASIC, basic_kw, self._outlets_basic, 120.0, 15.0),
            # The AP9570 is a 208 V 30 A reserve feed carrying a standing load.
            (PDU_RESERVE, cfg.reserve_pdu_kw, self._outlets_reserve, 208.0, 24.0),
        ):
            on_count = sum(1 for state in outlets if state)
            scale = on_count / len(outlets) if outlets else 0.0
            actual_kw = kw * scale
            self.emit(out, asset, "power_total_kw", round(actual_kw, 3))
            self.emit(out, asset, "current_total_a", round(actual_kw * 1000.0 / voltage, 2))
            self.emit(
                out,
                asset,
                "outlet_state",
                {f"outlet_{i + 1}": ("on" if state else "off") for i, state in enumerate(outlets)},
            )
            self.emit(out, asset, "overload_active", actual_kw * 1000.0 / voltage > limit_a)
            self.emit(out, asset, "availability_state", "online")
            if asset == PDU_SWITCHED:
                self.emit(out, asset, "state_operating", "distributing")
                self.emit(out, asset, "alarm_summary", "none")

        # -- network gear --------------------------------------------------------------
        # Switch health tracks rack air: a hot rack shows up first as rising
        # optics temperature and packet errors, well before anything trips.
        switch_temp = self.rack_inlet_c + 8.0
        error_rate = 0.0 if switch_temp < 45.0 else min(0.02, (switch_temp - 45.0) * 0.001)
        powered = context.critical_bus_energized or self.ups_charge_pct > 0
        for switch, ports in (
            (SWITCH_ACCESS, cfg.access_switch_ports),
            (SWITCH_CORE, cfg.core_switch_ports),
        ):
            self.emit(out, switch, "availability_state", "online" if powered else "offline")
            self.emit(out, switch, "port_up_count", ports if powered else 0)
            self.emit(out, switch, "temperature_c", round(switch_temp, 1))
            self.emit(out, switch, "packet_error_rate", round(error_rate, 5))

        self.router_cpu_pct = clamp(
            approach(self.router_cpu_pct, 18.0 + 4.0 * len(server_powers), dt_s, 600.0)
            + self.random.gauss(0.0, 0.5),
            2.0,
            99.0,
        )
        self.emit(out, ROUTER, "wan_state", "up" if (self.wan_up and powered) else "down")
        self.emit(out, ROUTER, "vpn_state", "up" if (self.vpn_up and powered) else "down")
        self.emit(out, ROUTER, "cpu_utilization_pct", round(self.router_cpu_pct, 1))
        # Voice keeps working without the WAN: internal calls are switched by the
        # on-site CUCM, only DID termination is lost (SDD 3.3, EMS-T001).
        if not powered:
            voice_state = "down"
        elif self.wan_up:
            voice_state = "registered"
        else:
            voice_state = "degraded_internal_only"
        self.emit(out, ROUTER, "voice_gateway_state", voice_state)

        # Wireless clients follow the working day; APs stay up while powered.
        self.ap_online_count = cfg.planned_ap_count if powered else 0
        target_clients = 4 + 8 * max(0.0, math.sin(math.pi * (now.hour + now.minute / 60.0 - 6) / 16))
        self.client_count = round(approach(self.client_count, target_clients, dt_s, 900.0))
        self.emit(out, WLC, "ap_online_count", self.ap_online_count)
        self.emit(out, WLC, "client_count", max(0, self.client_count) if powered else 0)
        self.emit(
            out,
            WLC,
            "alarm_summary",
            "none" if self.ap_online_count == cfg.planned_ap_count else "warning",
        )

        # -- rack enclosure and environmental monitors ------------------------------
        if self.smoke_active:
            rack_alarm = "critical"
        elif self.rack_inlet_c > 35.0 or self.leak_active:
            rack_alarm = "alarm"
        elif self.rack_inlet_c > 30.0:
            rack_alarm = "warning"
        else:
            rack_alarm = "none"
        self.emit(out, RACK_ASSET, "temperature_inlet_c", round(self.rack_inlet_c, 2))
        self.emit(out, RACK_ASSET, "temperature_exhaust_c", round(self.rack_exhaust_c, 2))
        self.emit(out, RACK_ASSET, "humidity_relative_pct", round(self.humidity_pct, 1))
        self.emit(out, RACK_ASSET, "door_state", "open" if self.door_open else "closed")
        self.emit(out, RACK_ASSET, "smoke_active", self.smoke_active)
        self.emit(out, RACK_ASSET, "leak_active", self.leak_active)
        self.emit(out, RACK_ASSET, "alarm_summary", rack_alarm)

        for monitor, temperature in (
            (NETBOTZ_CONTAINER, self.container_temperature_c),
            (NETBOTZ_RACK, self.rack_inlet_c),
        ):
            self.emit(out, monitor, "temperature_air_c", round(temperature, 2))
            self.emit(out, monitor, "humidity_relative_pct", round(self.humidity_pct, 1))
            self.emit(out, monitor, "availability_state", "online")
            self.emit(out, monitor, "alarm_summary", rack_alarm)
            self.emit(out, monitor, "heartbeat_age_s", 0.0)

        # -- safety sensors ------------------------------------------------------------
        # The ``safety_sensor`` class carries a single generic ``value`` point, so
        # the combined temperature/humidity probe reports temperature only.
        for sensor, value, alarm in (
            (SENSOR_TEMP_HUMIDITY, self.rack_inlet_c, self.rack_inlet_c > 35.0),
            (SENSOR_TEMP, self.rack_exhaust_c, self.rack_exhaust_c > 45.0),
            (SENSOR_SMOKE, 1.0 if self.smoke_active else 0.0, self.smoke_active),
            (SENSOR_FLUID, 1.0 if self.leak_active else 0.0, self.leak_active),
        ):
            self.emit(out, sensor, "value", round(float(value), 2))
            self.emit(out, sensor, "alarm_active", bool(alarm))
            self.emit(out, sensor, "self_test_due", False)
            self.emit(out, sensor, "battery_pct", 100.0)

        beacon_on = self.smoke_active or self.leak_active or rack_alarm == "critical"
        self.emit(out, BEACON, "state_operating", "active" if beacon_on else "idle")
        self.emit(out, BEACON, "enabled_requested", beacon_on)
        self.emit(out, BEACON, "fault_active", False)
        return out
