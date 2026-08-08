"""The twelve site load groups, and the two distribution panels that feed them.

Models every ``energy.load.site.*`` asset in the v0.3 register plus
``energy.panel.power_container.critical_01`` / ``general_01``.

Each group carries a daily profile and a shed/restore state machine with the
timing SDD section 30.9 demands: minimum on time, minimum off time, restart
delay and inrush. Nothing here is instantaneous, because an EMS that assumes
instantaneous load response is an EMS that oscillates.

**Local authority is real.** SDD 5.3 and 31.2 say the supervisory layer does not
get to switch protective loads off. Three refusals are modelled:

===========================  ==================================================
``control_core_01``          never shed (SDD Tier 0 -- control survival)
``rack_cooling_01``          refuses while the rack is energized and warm
``battery_hvac_01``          refuses while cells are outside their safe band
===========================  ==================================================

A refusal is published as a ``rejected`` command ack with a reason, which is
exactly the input SDD 39 case ``EMS-T005`` ("shed command rejected") needs.

Register note: the register numbers load tiers 1..5 while SDD 31.2 describes
tiers 0..4. Both are carried -- ``load_tier`` is published verbatim from the
register (it is the source of truth) and ``sdd_tier`` is kept alongside for the
SDD 31.2 shedding semantics.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from chaos.envelope import CommandEnvelope
from simulator.clock import hour_of_day
from simulator.components.base import (
    CommandOutcome,
    Component,
    LoadSnapshot,
    PointCatalog,
    SiteContext,
    clamp,
)

CRITICAL_PANEL = "energy.panel.power_container.critical_01"
GENERAL_PANEL = "energy.panel.power_container.general_01"

# Shed state machine values (point dictionary ``shed_state`` enum).
CONNECTED = "connected"
SHED_PENDING = "shed_pending"
SHED = "shed"
RESTORE_PENDING = "restore_pending"
LOCKED_OUT = "locked_out"

LOAD_POINTS = (
    "power_kw",
    "enabled_actual",
    "enabled_requested",
    "load_tier",
    "priority_effective",
    "shed_state",
    "shed_reason",
    "restart_delay_s",
    "minimum_on_time_s",
    "minimum_off_time_s",
    "power_budget_kw",
    "power_request_kw",
)

#: 24 hourly factors, index = local hour. Interpolated linearly between hours.
Profile = Sequence[float]

FLAT = tuple([1.0] * 24)


def _profile(**hours: float) -> tuple[float, ...]:
    """Build a 24-hour profile from ``hour=factor`` pairs; unnamed hours are 0."""
    values = [0.0] * 24
    for key, value in hours.items():
        values[int(key.lstrip("h"))] = value
    return tuple(values)


@dataclass
class LoadConfig:
    """Static description of one load group."""

    asset_id: str
    tier: int  # register ``load_tier`` (1..5), published verbatim
    sdd_tier: int  # SDD 31.2 tier (0..4), used for shedding semantics
    base_kw: float
    profile: Profile = FLAT
    #: Loads whose local controller refuses every shed command (SDD Tier 0).
    never_shed: bool = False
    #: Named local interlock evaluated at shed time; returns a reason or "".
    interlock: str = ""
    minimum_on_time_s: float = 60.0
    minimum_off_time_s: float = 300.0
    restart_delay_s: float = 30.0
    shed_delay_s: float = 5.0
    #: Multiplier and duration of the current inrush on restore.
    inrush_factor: float = 1.0
    inrush_s: float = 10.0
    #: Loads that can be throttled to a power budget rather than switched.
    throttleable: bool = False
    #: Loads that must not restart unattended after a blackout (SDD 35.3).
    attended: bool = False
    #: When set, power comes from another component instead of the profile.
    driven_by: str = ""
    #: Random walk amplitude as a fraction of base power.
    noise_fraction: float = 0.05
    initial_shed: bool = False


#: The twelve load groups. Tiers and restoration policies come from the register;
#: powers and profiles are simulator planning values (the register records
#: ``rated_power_kw: TBD`` for every group except the control core).
DEFAULT_LOADS: tuple[LoadConfig, ...] = (
    LoadConfig(
        asset_id="energy.load.site.control_core_01",
        tier=1,
        sdd_tier=0,
        base_kw=0.25,  # register ``planning_baseline_kw``
        profile=FLAT,
        never_shed=True,
        minimum_off_time_s=0.0,
        noise_fraction=0.02,
    ),
    LoadConfig(
        asset_id="energy.load.site.server_rack_01",
        tier=2,
        sdd_tier=1,
        base_kw=1.5,  # register ``planning_baseline_kw``
        driven_by="rack_it",
        minimum_on_time_s=300.0,
        minimum_off_time_s=600.0,
        restart_delay_s=180.0,
        inrush_factor=1.4,
        inrush_s=30.0,
        noise_fraction=0.04,
    ),
    LoadConfig(
        asset_id="energy.load.site.rack_cooling_01",
        tier=1,
        sdd_tier=1,
        base_kw=0.9,
        driven_by="rack_cooling",
        interlock="rack_energized_and_warm",
        minimum_off_time_s=120.0,
        restart_delay_s=20.0,
        inrush_factor=2.5,
        inrush_s=8.0,
    ),
    LoadConfig(
        asset_id="energy.load.site.battery_hvac_01",
        tier=1,
        sdd_tier=1,
        base_kw=1.2,
        driven_by="battery_hvac",
        interlock="battery_outside_safe_band",
        minimum_off_time_s=180.0,
        restart_delay_s=30.0,
        inrush_factor=2.2,
        inrush_s=8.0,
    ),
    LoadConfig(
        asset_id="energy.load.site.water_pumping_01",
        tier=2,
        sdd_tier=1,
        base_kw=1.8,
        profile=_profile(
            h5=0.4, h6=1.0, h7=1.0, h8=0.6, h11=0.5, h12=0.6, h17=0.9, h18=1.0, h19=0.7, h20=0.3
        ),
        minimum_off_time_s=300.0,
        restart_delay_s=60.0,
        inrush_factor=3.0,
        inrush_s=5.0,
    ),
    LoadConfig(
        asset_id="energy.load.site.irrigation_01",
        tier=3,
        sdd_tier=3,
        base_kw=2.2,
        profile=_profile(h5=0.8, h6=1.0, h7=0.6, h19=0.6, h20=1.0, h21=0.5),
        minimum_off_time_s=600.0,
        restart_delay_s=120.0,
        inrush_factor=2.8,
        inrush_s=6.0,
    ),
    LoadConfig(
        asset_id="energy.load.site.greenhouse_climate_01",
        tier=2,
        sdd_tier=2,
        base_kw=2.6,
        profile=_profile(
            h0=0.5,
            h1=0.5,
            h2=0.55,
            h3=0.6,
            h4=0.6,
            h5=0.5,
            h6=0.4,
            h7=0.3,
            h8=0.3,
            h9=0.4,
            h10=0.6,
            h11=0.8,
            h12=0.9,
            h13=1.0,
            h14=1.0,
            h15=0.9,
            h16=0.7,
            h17=0.5,
            h18=0.4,
            h19=0.4,
            h20=0.45,
            h21=0.5,
            h22=0.5,
            h23=0.5,
        ),
        minimum_off_time_s=300.0,
        restart_delay_s=60.0,
        throttleable=True,
    ),
    LoadConfig(
        asset_id="energy.load.site.greenhouse_lighting_01",
        tier=3,
        sdd_tier=3,
        base_kw=3.0,
        profile=_profile(h4=0.8, h5=1.0, h6=1.0, h7=0.5, h17=0.5, h18=1.0, h19=1.0, h20=0.8),
        minimum_off_time_s=300.0,
        restart_delay_s=45.0,
        throttleable=True,
    ),
    LoadConfig(
        asset_id="energy.load.site.spa_01",
        tier=4,
        sdd_tier=3,
        base_kw=6.0,
        profile=_profile(h19=0.6, h20=1.0, h21=1.0, h22=0.5),
        minimum_off_time_s=900.0,
        restart_delay_s=120.0,
        throttleable=True,
        attended=True,
        inrush_factor=1.6,
        inrush_s=15.0,
    ),
    LoadConfig(
        asset_id="energy.load.site.workshop_heavy_01",
        tier=4,
        sdd_tier=3,
        base_kw=5.0,
        profile=_profile(h9=0.4, h10=0.9, h11=1.0, h13=0.8, h14=1.0, h15=0.9, h16=0.4),
        minimum_off_time_s=600.0,
        restart_delay_s=300.0,
        attended=True,
        noise_fraction=0.25,
        inrush_factor=2.0,
        inrush_s=4.0,
    ),
    LoadConfig(
        asset_id="energy.load.site.tool_charging_01",
        tier=4,
        sdd_tier=3,
        base_kw=1.5,
        profile=_profile(h10=0.6, h11=1.0, h12=1.0, h13=1.0, h14=1.0, h15=0.8, h16=0.4),
        minimum_off_time_s=300.0,
        restart_delay_s=60.0,
        throttleable=True,
    ),
    LoadConfig(
        asset_id="energy.load.site.opportunistic_compute_01",
        tier=5,
        sdd_tier=4,
        base_kw=3.0,
        profile=FLAT,
        minimum_on_time_s=0.0,
        minimum_off_time_s=30.0,
        restart_delay_s=15.0,
        throttleable=True,
        noise_fraction=0.10,
    ),
)


@dataclass
class LoadGroup:
    """Runtime state of one load group."""

    config: LoadConfig
    shed_state: str = CONNECTED
    shed_reason: str = "none"
    enabled_requested: bool = True
    power_kw: float = 0.0
    requested_kw: float = 0.0
    power_budget_kw: float | None = None
    equipment_failed: bool = False
    priority_offset: int = 0

    _timer_s: float = 0.0  # time in the current pending state
    _time_since_change_s: float = 1e6
    _inrush_remaining_s: float = 0.0

    @property
    def asset_id(self) -> str:
        return self.config.asset_id

    @property
    def connected(self) -> bool:
        return self.shed_state in (CONNECTED, SHED_PENDING)

    @property
    def enabled_actual(self) -> bool:
        return self.connected and not self.equipment_failed

    @property
    def priority_effective(self) -> int:
        """Lower is more important. Base priority is the register tier x 100."""
        return self.config.tier * 100 + self.priority_offset


class LoadBank(Component):
    """All twelve load groups plus the critical and general distribution panels."""

    name = "loads"

    def __init__(
        self,
        catalog: PointCatalog,
        configs: Sequence[LoadConfig] | None = None,
        seed: int = 0,
        utc_offset_h: float = -5.0,
    ) -> None:
        super().__init__(catalog)
        self.random = random.Random(f"{seed}:loads")
        self.utc_offset_h = utc_offset_h
        self.groups: dict[str, LoadGroup] = {}
        for config in configs or DEFAULT_LOADS:
            group = LoadGroup(config=config)
            if config.initial_shed:
                group.shed_state = SHED
                group.shed_reason = "initial_state"
                group.enabled_requested = False
            self.groups[config.asset_id] = group
            self.declare(config.asset_id, LOAD_POINTS)
        self._noise: dict[str, float] = {a: 0.0 for a in self.groups}
        # Interlock evaluation needs the site state as of the last step; a
        # command can arrive between steps, so the context is retained.
        self._last_context = SiteContext()
        self.panel_energy_kwh = {CRITICAL_PANEL: 0.0, GENERAL_PANEL: 0.0}
        self.panel_power_kw = {CRITICAL_PANEL: 0.0, GENERAL_PANEL: 0.0}
        self.pending_events: list[tuple[str, str, dict[str, Any]]] = []
        for panel in (CRITICAL_PANEL, GENERAL_PANEL):
            self.declare(panel, ["power_total_kw", "energy_total_kwh", "breaker_trip_active"])

    # -- helpers -------------------------------------------------------------
    def group(self, asset_id: str) -> LoadGroup:
        return self.groups[asset_id]

    def total_demand_kw(self) -> float:
        return sum(g.requested_kw for g in self.groups.values())

    def total_actual_kw(self) -> float:
        return sum(g.power_kw for g in self.groups.values())

    def panel_of(self, group: LoadGroup) -> str:
        """Register tiers 1-2 sit on the critical panel, 3+ on the general panel."""
        return CRITICAL_PANEL if group.config.tier <= 2 else GENERAL_PANEL

    def _profile_factor(self, config: LoadConfig, now: dt.datetime) -> float:
        """Linearly interpolate the hourly profile so power never steps."""
        hour = hour_of_day(now, self.utc_offset_h)
        low = int(hour) % 24
        high = (low + 1) % 24
        blend = hour - int(hour)
        return config.profile[low] * (1 - blend) + config.profile[high] * blend

    # -- local interlocks -------------------------------------------------------
    def _interlock_reason(self, group: LoadGroup, context: SiteContext, emergency: bool) -> str:
        """Return a blocking reason, or "" when the shed may proceed."""
        config = group.config
        if config.never_shed:
            # SDD Tier 0: physical protection and control survival. No mode,
            # not even EMERGENCY, gets to drop this from the supervisory layer.
            return "tier0_control_survival_never_shed"
        if emergency:
            return ""
        if config.interlock == "rack_energized_and_warm":
            rack_load = context.loads.get("energy.load.site.server_rack_01")
            rack_energized = bool(rack_load and rack_load.power_kw > 0.2)
            if rack_energized and context.rack_inlet_temperature_c > 22.0:
                return "rack_energized_temperature_governed"
        if config.interlock == "battery_outside_safe_band":
            if not 5.0 <= context.cell_temperature_c <= 35.0:
                return "battery_temperature_outside_safe_band"
        return ""

    # -- commands ----------------------------------------------------------------
    def handle_command(self, command: CommandEnvelope) -> CommandOutcome | None:
        group = self.groups.get(command.asset_id)
        if group is None:
            return None
        name = command.command
        emergency = (command.operating_mode or "").lower() == "emergency"
        if name == "shed" or (name == "enabled_requested" and bool(command.value) is False):
            return self._command_shed(group, command, emergency)
        if name == "restore" or (name == "enabled_requested" and bool(command.value) is True):
            return self._command_restore(group, command)
        if name == "power_budget_kw":
            try:
                budget = float(command.value)
            except (TypeError, ValueError):
                return CommandOutcome(False, f"invalid_budget:{command.value!r}")
            if budget < 0:
                return CommandOutcome(False, "negative_budget")
            if not group.config.throttleable:
                return CommandOutcome(False, "load_not_throttleable")
            group.power_budget_kw = budget
            return CommandOutcome(True, f"budget_kw={budget:g}", event="power_budget_granted")
        if name == "clear_power_budget":
            group.power_budget_kw = None
            return CommandOutcome(True, "budget_cleared", event="power_budget_revoked")
        if name == "set_priority":
            try:
                group.priority_offset = int(command.value)
            except (TypeError, ValueError):
                return CommandOutcome(False, f"invalid_priority:{command.value!r}")
            return CommandOutcome(True, f"priority_offset={group.priority_offset}")
        return None

    def _command_shed(self, group: LoadGroup, command: CommandEnvelope, emergency: bool) -> CommandOutcome:
        context = self._last_context
        reason = self._interlock_reason(group, context, emergency)
        if reason:
            return CommandOutcome(False, reason)
        if group.shed_state in (SHED, SHED_PENDING):
            return CommandOutcome(True, "already_shed")
        if group.shed_state == LOCKED_OUT:
            return CommandOutcome(True, "already_locked_out")
        if group._time_since_change_s < group.config.minimum_on_time_s:
            remaining = group.config.minimum_on_time_s - group._time_since_change_s
            return CommandOutcome(False, f"minimum_on_time_not_elapsed:{remaining:.0f}s")
        group.shed_state = SHED_PENDING
        group.shed_reason = command.reason or "supervisory_shed"
        group.enabled_requested = False
        group._timer_s = 0.0
        self.pending_events.append((group.asset_id, "load_shed_requested", {"reason": group.shed_reason}))
        return CommandOutcome(True, f"shedding_in:{group.config.shed_delay_s:.0f}s", completed=False)

    def _command_restore(self, group: LoadGroup, command: CommandEnvelope) -> CommandOutcome:
        if group.shed_state in (CONNECTED, RESTORE_PENDING):
            return CommandOutcome(True, "already_connected")
        if group._time_since_change_s < group.config.minimum_off_time_s:
            remaining = group.config.minimum_off_time_s - group._time_since_change_s
            return CommandOutcome(False, f"minimum_off_time_not_elapsed:{remaining:.0f}s")
        group.shed_state = RESTORE_PENDING
        group.shed_reason = command.reason or "supervisory_restore"
        group.enabled_requested = True
        group._timer_s = 0.0
        self.pending_events.append((group.asset_id, "load_restore_requested", {"reason": group.shed_reason}))
        return CommandOutcome(True, f"restoring_in:{group.config.restart_delay_s:.0f}s", completed=False)

    # -- scenario control ----------------------------------------------------------
    def fail_equipment(self, asset_id: str, failed: bool = True) -> None:
        """Model the equipment itself failing while still commanded on.

        ``enabled_requested`` stays true while ``enabled_actual`` goes false --
        the mismatch an alarm rule should catch.
        """
        group = self.groups[asset_id]
        group.equipment_failed = failed
        if failed:
            group.shed_reason = "equipment_failure"
            self.pending_events.append((asset_id, "load_equipment_failed", {}))

    def force_shed(self, asset_id: str, reason: str = "scenario") -> None:
        group = self.groups[asset_id]
        group.shed_state = SHED
        group.shed_reason = reason
        group.enabled_requested = False
        group._time_since_change_s = 0.0

    def lock_out_all(self, reason: str = "bus_de_energized") -> None:
        """Blackout: every group drops and attended loads stay out until commanded."""
        for group in self.groups.values():
            group.shed_state = LOCKED_OUT
            group.shed_reason = reason
            group.power_kw = 0.0
            group._time_since_change_s = 0.0

    def restore_from_lockout(self, asset_id: str) -> bool:
        """Black-start restoration of one group (staggered by the site)."""
        group = self.groups[asset_id]
        if group.shed_state != LOCKED_OUT:
            return False
        group.shed_state = RESTORE_PENDING
        group.shed_reason = "black_start_restoration"
        group.enabled_requested = True
        group._timer_s = 0.0
        return True

    def drain_events(self) -> list[tuple[str, str, dict[str, Any]]]:
        events, self.pending_events = self.pending_events, []
        return events

    # -- step ------------------------------------------------------------------------
    def step(self, now: dt.datetime, dt_s: float, context: SiteContext) -> dict[str, Any]:
        self._last_context = context
        energized = context.ac_bus_energized
        out: dict[str, Any] = {}
        panel_totals = {CRITICAL_PANEL: 0.0, GENERAL_PANEL: 0.0}
        context.loads = {}

        for asset_id, group in self.groups.items():
            config = group.config
            group._timer_s += dt_s
            group._time_since_change_s += dt_s

            # -- state machine -------------------------------------------------
            if group.shed_state == SHED_PENDING and group._timer_s >= config.shed_delay_s:
                group.shed_state = SHED
                group._time_since_change_s = 0.0
                self.pending_events.append((asset_id, "load_shed", {"reason": group.shed_reason}))
            elif group.shed_state == RESTORE_PENDING and group._timer_s >= config.restart_delay_s:
                group.shed_state = CONNECTED
                group._time_since_change_s = 0.0
                group._inrush_remaining_s = config.inrush_s
                self.pending_events.append((asset_id, "load_restored", {"reason": group.shed_reason}))
            if not energized and group.shed_state != LOCKED_OUT:
                group.shed_state = LOCKED_OUT
                group.shed_reason = "bus_de_energized"
                group._time_since_change_s = 0.0

            # -- demand ---------------------------------------------------------
            self._noise[asset_id] = clamp(
                self._noise[asset_id] * 0.85 + self.random.gauss(0.0, 0.35) * 0.15, -1.0, 1.0
            )
            if config.driven_by == "rack_it":
                demand = context.rack_it_kw
            elif config.driven_by == "rack_cooling":
                demand = context.rack_cooling_kw
            elif config.driven_by == "battery_hvac":
                demand = self._battery_hvac_demand(config, context)
            else:
                demand = config.base_kw * self._profile_factor(config, now)
            demand *= 1.0 + config.noise_fraction * self._noise[asset_id]
            demand = max(0.0, demand)
            group.requested_kw = demand

            # -- actual power ------------------------------------------------------
            if group.shed_state in (SHED, LOCKED_OUT) or group.equipment_failed:
                power = 0.0
            else:
                power = demand
                if group.power_budget_kw is not None and config.throttleable:
                    power = min(power, group.power_budget_kw)
                if group._inrush_remaining_s > 0 and config.inrush_factor > 1.0:
                    # Linear decay of the inrush multiplier over ``inrush_s``.
                    fraction = group._inrush_remaining_s / max(config.inrush_s, 1e-9)
                    power *= 1.0 + (config.inrush_factor - 1.0) * fraction
                    group._inrush_remaining_s = max(0.0, group._inrush_remaining_s - dt_s)
            group.power_kw = power

            panel = self.panel_of(group)
            panel_totals[panel] += power
            context.loads[asset_id] = LoadSnapshot(
                asset_id=asset_id,
                tier=config.tier,
                power_kw=power,
                requested_kw=demand,
                enabled=group.enabled_actual,
                shed_state=group.shed_state,
                critical=config.sdd_tier <= 1,
            )

            # -- emission ------------------------------------------------------------
            self.emit(out, asset_id, "power_kw", round(power, 3))
            self.emit(out, asset_id, "enabled_actual", group.enabled_actual)
            self.emit(out, asset_id, "enabled_requested", group.enabled_requested)
            self.emit(out, asset_id, "load_tier", config.tier)
            self.emit(out, asset_id, "priority_effective", group.priority_effective)
            self.emit(out, asset_id, "shed_state", group.shed_state)
            self.emit(out, asset_id, "shed_reason", group.shed_reason)
            self.emit(out, asset_id, "restart_delay_s", int(config.restart_delay_s))
            self.emit(out, asset_id, "minimum_on_time_s", int(config.minimum_on_time_s))
            self.emit(out, asset_id, "minimum_off_time_s", int(config.minimum_off_time_s))
            self.emit(
                out,
                asset_id,
                "power_budget_kw",
                round(group.power_budget_kw, 3) if group.power_budget_kw is not None else -1.0,
            )
            self.emit(out, asset_id, "power_request_kw", round(demand, 3))

        # -- panels -----------------------------------------------------------------
        for panel, total in panel_totals.items():
            self.panel_power_kw[panel] = total
            self.panel_energy_kwh[panel] += total * dt_s / 3600.0
            self.emit(out, panel, "power_total_kw", round(total, 3))
            self.emit(out, panel, "energy_total_kwh", round(self.panel_energy_kwh[panel], 3))
            self.emit(out, panel, "breaker_trip_active", False)

        context.load_demand_kw = self.total_demand_kw()
        context.load_actual_kw = self.total_actual_kw()
        context.critical_load_kw = sum(g.power_kw for g in self.groups.values() if g.config.sdd_tier <= 1)
        context.rack_cooling_available = self.groups["energy.load.site.rack_cooling_01"].enabled_actual
        return out

    @staticmethod
    def _battery_hvac_demand(config: LoadConfig, context: SiteContext) -> float:
        """Battery-zone HVAC works harder the further cells are from 20 C."""
        error = abs(context.cell_temperature_c - 20.0)
        return config.base_kw * clamp(0.25 + error / 12.0, 0.0, 1.4)
