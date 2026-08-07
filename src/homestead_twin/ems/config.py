"""EMS tunables (SDD sections 30.2, 30.8, 30.9, 32-35).

Every number in this file is a **commissioning parameter**, not a fact about
the homestead. SDD 30.2 is explicit: the design portfolio still holds two
incompatible energy baselines (roughly 12 kW PV / 40 kWh usable versus roughly
45 kWdc / 800 kWh nominal / 640 kWh usable planning capacity), so the control
narrative is kept capacity-agnostic and works in percentages, reserve energy
and measured limits. The defaults below are documented starting points that
must be re-commissioned against the selected battery chemistry, inverter
capability, generator, climate and the actual site loads.

No threshold may be written at a call site. Every module takes an
:class:`EmsConfig` so that a threshold change is one auditable configuration
revision (SDD 39 test ``EMS-T015``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

from homestead_twin.config import Settings
from homestead_twin.topics import DEFAULT_BASE, telemetry_topic

#: Canonical point name the EMS publishes the site energy state on.
#:
#: OPEN ITEM: ``energy_state`` is not yet in ``data/point_dictionary.yaml``
#: (that file belongs to the registry package). Until it is added, an ingest
#: service that resolves topics against the registry will dead-letter this
#: message. The state is still published so subsystems can subscribe today.
ENERGY_STATE_POINT = "energy_state"

#: Canonical point name for the per-load budget published by the EMS. This one
#: *is* in the point dictionary (class AO, unit kW) and is part of the
#: ``ems_load`` point profile.
POWER_BUDGET_POINT = "power_budget_kw"

#: Point the EMS writes when it wants the generator to run. SDD 34 / FR-104:
#: the platform *requests* a start through the equipment-native interface.
GENERATOR_START_REQUEST_POINT = "generator_start_request"


def energy_state_topic(settings: Settings) -> str:
    """``homestead/site/primary/site_01/energy_state``.

    One retained message carrying the site energy state, so that every
    subsystem can translate it into its own bounded operating profile
    (SDD 13) instead of the EMS switching each load itself.
    """
    return telemetry_topic(settings.site_id, ENERGY_STATE_POINT, base=settings.mqtt_base_topic or DEFAULT_BASE)


def load_budget_topic(settings: Settings, asset_id: str) -> str:
    """``homestead/energy/site/load_spa_01/power_budget_kw`` for one load."""
    return telemetry_topic(asset_id, POWER_BUDGET_POINT, base=settings.mqtt_base_topic or DEFAULT_BASE)


@dataclass(frozen=True)
class EmsConfig:
    """Commissioning parameters for the whole energy manager."""

    # ------------------------------------------------------------------
    # Battery planning basis (SDD 30.2)
    # ------------------------------------------------------------------
    #: Usable planning capacity. Used only to convert SOC% into kWh when the
    #: BMS does not publish ``energy_available_kwh``. Derived values computed
    #: this way are flagged with the assumption, never presented as measured.
    planning_usable_capacity_kwh: float = 640.0
    #: Fraction of usable capacity held back for physical protection and
    #: emergency communications (SDD 30.3 objective 4).
    emergency_reserve_pct: float = 20.0
    #: Planning figure for the critical baseload when the critical panel meter
    #: is unavailable. Never substituted silently: autonomy computed from it is
    #: marked with the assumption.
    planning_critical_load_kw: float = 1.5

    # ------------------------------------------------------------------
    # State entry thresholds (SDD 30.8) and recovery thresholds (SDD 30.9)
    # ------------------------------------------------------------------
    surplus_soc_pct: float = 85.0
    #: Recovery threshold is separate from the entry threshold so a passing
    #: cloud cannot walk the site in and out of SURPLUS (SDD 30.9).
    surplus_exit_soc_pct: float = 75.0
    #: PV must exceed demand by this margin before SURPLUS qualifies.
    surplus_pv_headroom_kw: float = 1.0
    #: Minimum useful run window for a surplus load (SDD 36: do not start a
    #: large thermal process for a five-minute irradiance spike).
    surplus_minimum_window_min: int = 30

    normal_reserve_soc_pct: float = 50.0
    conserve_recovery_soc_pct: float = 60.0
    conserve_recovery_margin_kwh: float = 10.0

    #: Energy above the emergency reserve below which CRITICAL_RESERVE qualifies.
    critical_margin_kwh: float = 20.0
    critical_recovery_margin_kwh: float = 40.0
    #: SDD 30.8: "estimated critical-load autonomy is below the configured
    #: response horizon".
    response_horizon_h: float = 12.0
    critical_autonomy_recovery_h: float = 24.0
    #: Fraction of present load the battery discharge limit must be able to
    #: carry; below it the battery power limit cannot support the load safely.
    discharge_limit_margin: float = 1.1

    # Emergency entry (SDD 30.8). These are protection-adjacent and have no
    # qualification delay.
    battery_temp_emergency_c: float = 55.0
    battery_temp_warning_c: float = 45.0
    container_temp_emergency_c: float = 50.0
    container_temp_warning_c: float = 40.0
    #: Fault codes that classify as shutdown-class inverter faults.
    inverter_shutdown_fault_codes: tuple[str, ...] = (
        "shutdown",
        "fault_shutdown",
        "critical",
        "trip",
    )
    inverter_fault_states: tuple[str, ...] = ("fault", "shutdown", "tripped")
    #: ``alarm_summary`` values from a monitored zone that request power
    #: isolation (SDD 30.8: fire/smoke/emergency-stop logic).
    emergency_alarm_summaries: tuple[str, ...] = ("critical",)
    #: Load above which "AC loads remain" is true for the discharge-inhibited
    #: emergency test.
    residual_load_kw: float = 0.1

    # ------------------------------------------------------------------
    # Dwell / qualification delays (SDD 30.9)
    # ------------------------------------------------------------------
    #: Every automatic transition has a qualification delay: a candidate state
    #: must persist this long before it is adopted.
    dwell_default_s: int = 300
    dwell_surplus_s: int = 900
    dwell_normal_s: int = 600
    dwell_conserve_s: int = 300
    dwell_critical_reserve_s: int = 120
    dwell_generator_support_s: int = 60
    dwell_degraded_sensor_s: int = 60
    dwell_black_start_s: int = 0
    #: Emergency is a protection response and is adopted immediately.
    dwell_emergency_s: int = 0

    #: Operator freeze (SDD 30.9). Bounded, and it never suppresses EMERGENCY.
    freeze_default_s: int = 900
    freeze_max_s: int = 3600

    # ------------------------------------------------------------------
    # Input validity (SDD 30.7 DEGRADED_SENSOR)
    # ------------------------------------------------------------------
    #: Fallback staleness when the point's own ``stale_after_s`` is unset.
    input_max_age_s: int = 60
    #: Qualities the EMS will act on. Anything else is invalid: SDD 30.5/30.7
    #: forbid substituting a default for a missing safety-relevant input.
    valid_qualities: tuple[str, ...] = ("good", "calculated")
    #: Time the inputs must stay healthy before leaving DEGRADED_SENSOR.
    degraded_recovery_s: int = 120

    # ------------------------------------------------------------------
    # Load shedding (SDD 32)
    # ------------------------------------------------------------------
    #: States in which automatic shedding is permitted at all.
    shed_states: tuple[str, ...] = ("CONSERVE", "CRITICAL_RESERVE", "EMERGENCY", "GENERATOR_SUPPORT")
    #: Highest tier the EMS may shed. Tier 0 is physical protection and control
    #: survival and is refused independently of the load schedule (SDD 31.2).
    protected_tier: int = 0
    #: Groups are shed one at a time with a confirmation window between them.
    shed_group_interval_s: int = 60
    #: How long to wait for measured load reduction before calling a shed failed.
    shed_confirm_delay_s: int = 60
    #: A load is considered still running above this fraction of its expected
    #: draw (SDD 32.2: measured power confirms the result).
    shed_confirm_power_kw: float = 0.05
    #: Do not re-issue a shed to the same asset faster than this
    #: (SDD 32.3: "do not repeatedly issue rapid on/off commands").
    shed_reissue_interval_s: int = 300
    #: Attempts before the asset is locked out for operator inspection.
    shed_attempt_limit: int = 2

    #: The deepest group the EMS will drive automatically. S7 is a controlled IT
    #: shutdown and S8 is emergency isolation through equipment-native controls;
    #: neither is unlocked until commissioning says so (SDD 32.2, 39).
    max_automatic_shed_group: str = "S6"

    # ------------------------------------------------------------------
    # Load restoration (SDD 33, FR-103)
    # ------------------------------------------------------------------
    #: The energy state must stay improved this long before restoration starts.
    restore_qualification_s: int = 900
    #: Forecast margin required to qualify restoration (SDD 33.1).
    restore_margin_kwh: float = 20.0
    restore_soc_pct: float = 55.0
    #: Wait between restoration groups so each group can be verified (SDD 33.2 rule 5).
    restore_group_interval_s: int = 120
    #: Anti-simultaneous-restart stagger by inrush class (FR-103, SDD 33.2 rule 3).
    restore_stagger_s: dict[str, int] = field(
        default_factory=lambda: {"none": 5, "low": 15, "medium": 60, "high": 180}
    )
    #: How many loads may be started in one restoration step.
    restore_max_per_step: int = 1
    #: Headroom kept free while restoring so a restart cannot overload the bus.
    restore_headroom_reserve_kw: float = 1.0

    # ------------------------------------------------------------------
    # Generator (SDD 34)
    # ------------------------------------------------------------------
    generator_asset_id: str = "energy.generator.site.01"
    #: Start when critical autonomy falls below this (SDD 34.1).
    generator_start_autonomy_h: float = 8.0
    #: ... or when energy above the emergency reserve falls below this.
    generator_start_margin_kwh: float = 15.0
    #: ... or when the forecast margin stays materially negative.
    generator_start_forecast_margin_kwh: float = -20.0
    #: Fuel below this blocks a start (SDD 34.2 permissive).
    generator_min_fuel_pct: float = 20.0
    #: Stop targets (SDD 34.5).
    generator_stop_soc_pct: float = 80.0
    generator_stop_margin_kwh: float = 60.0
    #: Timers (SDD 30.9, 34.4).
    generator_min_run_s: int = 1800
    generator_max_continuous_run_s: int = 28800
    generator_cooldown_s: int = 300
    #: Quiet period after a stop before a new start request may be made.
    generator_restart_inhibit_s: int = 900
    #: How long to wait for the native controller to report a running state.
    generator_start_timeout_s: int = 120
    #: Repeated cranking is the generator controller's business; the EMS stops
    #: asking after this many failed sequences and requires a reset (SDD 34.7).
    generator_start_attempt_limit: int = 3
    generator_running_states: tuple[str, ...] = ("running", "online", "loaded", "run")
    generator_stopped_states: tuple[str, ...] = ("stopped", "off", "standby", "ready")

    # ------------------------------------------------------------------
    # Black start (SDD 35)
    # ------------------------------------------------------------------
    blackstart_min_soc_pct: float = 15.0
    blackstart_battery_temp_min_c: float = -10.0
    blackstart_battery_temp_max_c: float = 45.0
    blackstart_step_timeout_s: int = 300
    #: Points not updated within this window after recovery are unknown, not
    #: assumed unchanged (SDD 35.4).
    blackstart_reconcile_stale_s: int = 300

    # ------------------------------------------------------------------
    # Power-budget leases (SDD 31.4) and dynamic priority (SDD 31.3)
    # ------------------------------------------------------------------
    lease_max_duration_s: int = 14400
    lease_default_duration_s: int = 3600
    #: Only this fraction of the computed surplus is grantable, so a lease can
    #: never consume the whole margin.
    lease_surplus_fraction: float = 0.8
    #: States in which new leases may be granted at all.
    lease_grant_states: tuple[str, ...] = ("SURPLUS", "NORMAL")
    #: States in which every revocable lease is pulled back.
    lease_revoke_states: tuple[str, ...] = (
        "CONSERVE",
        "CRITICAL_RESERVE",
        "EMERGENCY",
        "BLACK_START",
        "DEGRADED_SENSOR",
    )
    lease_min_priority: int = 0
    lease_max_priority: int = 4
    #: A dynamic tier override must expire (SDD 31.3).
    tier_override_max_s: int = 21600

    # ------------------------------------------------------------------
    # Forecast (SDD 30.6)
    # ------------------------------------------------------------------
    forecast_short_horizon_h: float = 6.0
    forecast_day_horizon_h: float = 24.0
    #: Naive persistence: assume the present PV power holds for this many hours.
    forecast_persistence_window_h: float = 3.0
    #: Derate applied to the persistence estimate. Placeholder, not a model.
    forecast_persistence_derate: float = 0.7
    #: Equivalent full-load hours used only when today's PV energy total is
    #: unavailable. A placeholder for a real forecast service.
    forecast_daily_equivalent_full_load_h: float = 4.0

    # ------------------------------------------------------------------
    # Rolling windows (SDD 30.6)
    # ------------------------------------------------------------------
    rolling_window_s: int = 300

    # ------------------------------------------------------------------
    # Service loop
    # ------------------------------------------------------------------
    publish_retained: bool = True
    actor: str = "ems"

    def dwell_s(self, state: str) -> int:
        """Qualification delay for adopting ``state`` (SDD 30.9)."""
        return {
            "SURPLUS": self.dwell_surplus_s,
            "NORMAL": self.dwell_normal_s,
            "CONSERVE": self.dwell_conserve_s,
            "CRITICAL_RESERVE": self.dwell_critical_reserve_s,
            "GENERATOR_SUPPORT": self.dwell_generator_support_s,
            "DEGRADED_SENSOR": self.dwell_degraded_sensor_s,
            "BLACK_START": self.dwell_black_start_s,
            "EMERGENCY": self.dwell_emergency_s,
        }.get(state, self.dwell_default_s)

    def stagger_s(self, inrush_class: str | None) -> int:
        """Anti-simultaneous-restart delay for an inrush class (FR-103)."""
        return self.restore_stagger_s.get(inrush_class or "medium", self.restore_stagger_s["medium"])

    def emergency_reserve_kwh(self, usable_capacity_kwh: float) -> float:
        return usable_capacity_kwh * self.emergency_reserve_pct / 100.0

    def as_dict(self) -> dict[str, Any]:
        """Flat view for the API and for configuration-revision records."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


#: Process-wide default. Override by constructing an ``EmsConfig`` and passing
#: it to the service; the defaults are documented starting points only.
DEFAULT_CONFIG = EmsConfig()
