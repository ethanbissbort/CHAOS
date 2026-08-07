"""Interlock behaviour: what the platform refuses, and why.

Every test here asserts on the machine-readable ``code`` as well as the verdict,
because other subsystems (the EMS load-shedding path in particular) branch on
those codes.
"""

from __future__ import annotations

import datetime as dt

import pytest

from homestead_twin.commands.interlocks import (
    ASSET_IN_MAINTENANCE,
    ASSET_NOT_OPERATIONAL,
    BINDING_NOT_COMMISSIONED,
    BUILTIN_INTERLOCK_CODES,
    DUPLICATE_IN_FLIGHT,
    EMERGENCY_MODE_LOCKOUT,
    INTERLOCK_ERROR,
    PHYSICAL_CONTROL_DISABLED,
    POINT_NOT_CONTROL_CAPABLE,
    STALE_INPUT,
    EmergencyLockout,
    InterlockContext,
    InterlockRegistry,
    InterlockResult,
    StaleInputInterlock,
    asset_in_maintenance,
    asset_not_operational,
    binding_not_commissioned,
    default_registry,
    duplicate_in_flight,
    physical_control_disabled,
    point_not_control_capable,
    role_at_least,
)
from homestead_twin.commands.manager import CommandRequest
from homestead_twin.config import Settings
from homestead_twin.models.commands import Command
from homestead_twin.models.registry import Asset, AssetClass, Point, PointBinding, PointDefinition
from homestead_twin.models.telemetry import CurrentState

ASSET_ID = "water.pump.orchard.01"
POINT_ID = f"{ASSET_ID}/start"
SOC_POINT_ID = "energy.battery.power_container.01/soc_pct"
NOW = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_settings(**overrides) -> Settings:
    base = {
        "database_url": "sqlite://",
        "mqtt_enabled": False,
        "ems_enabled": False,
        "alarm_engine_enabled": False,
        "allow_physical_control": True,
        "node_role": "primary",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def world(db_session):
    """A commissioned, control-capable pump ready to accept a start command."""
    db_session.add(AssetClass(name="pump", allowed_domains=["water"]))
    db_session.add(
        PointDefinition(
            name="start", default_class="CMD", data_type="boolean", control_capable=True
        )
    )
    db_session.add(
        PointDefinition(name="soc_pct", default_class="AI", data_type="float", unit="%")
    )
    db_session.add(
        Asset(
            asset_id=ASSET_ID,
            domain="water",
            asset_class="pump",
            name="Orchard transfer pump",
            status="active",
        )
    )
    db_session.add(
        Point(
            point_id=POINT_ID,
            asset_id=ASSET_ID,
            point_name="start",
            point_class="CMD",
            data_type="boolean",
            control_capable=True,
            automatic_control_allowed=True,
        )
    )
    db_session.add(
        PointBinding(
            point_id=POINT_ID,
            asset_id=ASSET_ID,
            point_name="start",
            binding_status="commissioned",
            source_protocol="mqtt",
            automatic_control_allowed=True,
        )
    )
    db_session.commit()
    return db_session


def make_request(**overrides) -> CommandRequest:
    payload = {
        "asset_id": ASSET_ID,
        "command": "start",
        "reason": "irrigation cycle",
        "value": True,
    }
    payload.update(overrides)
    return CommandRequest(**payload)


def make_context(session, settings=None, **overrides) -> InterlockContext:
    settings = settings or make_settings()
    context = InterlockContext(
        settings=settings,
        now=NOW,
        principal_name="test.operator",
        principal_role="operator",
        issuer_kind="human",
        asset=session.get(Asset, ASSET_ID),
        point=session.get(Point, POINT_ID),
        binding=session.get(PointBinding, POINT_ID),
        point_id=POINT_ID,
        effective_mode="automatic",
        mode_chain={"site": "automatic", "domain": "automatic", "asset": "automatic"},
    )
    for key, value in overrides.items():
        setattr(context, key, value)
    return context


# ---------------------------------------------------------------------------
# 1. physical_control_disabled -- the master safety gate
# ---------------------------------------------------------------------------


def test_master_switch_denies_every_command_by_default(world):
    settings = make_settings(allow_physical_control=False)
    result = physical_control_disabled(world, make_request(), make_context(world, settings))

    assert result.allowed is False
    assert result.code == PHYSICAL_CONTROL_DISABLED
    assert result.detail["allow_physical_control"] is False


def test_master_switch_allows_when_commissioning_has_enabled_control(world):
    result = physical_control_disabled(world, make_request(), make_context(world))

    assert result.allowed is True
    assert result.blocks_dispatch is False


def test_dry_run_passes_the_master_switch_but_never_dispatches(world):
    settings = make_settings(allow_physical_control=False)
    result = physical_control_disabled(
        world, make_request(dry_run=True), make_context(world, settings)
    )

    assert result.allowed is True
    assert result.blocks_dispatch is True


def test_dry_run_still_blocks_dispatch_when_control_is_enabled(world):
    result = physical_control_disabled(world, make_request(dry_run=True), make_context(world))

    assert result.allowed is True
    assert result.blocks_dispatch is True


# ---------------------------------------------------------------------------
# 2. binding_not_commissioned -- SDD section 47
# ---------------------------------------------------------------------------


def test_missing_binding_is_a_denial_not_a_shrug(world):
    context = make_context(world, binding=None)
    result = binding_not_commissioned(world, make_request(), context)

    assert result.allowed is False
    assert result.code == BINDING_NOT_COMMISSIONED
    assert result.detail["binding"] is None


def test_uncommissioned_binding_refuses_control(world):
    binding = world.get(PointBinding, POINT_ID)
    binding.binding_status = "tbd"
    world.commit()

    result = binding_not_commissioned(world, make_request(), make_context(world))

    assert result.allowed is False
    assert result.detail["binding_status"] == "tbd"


def test_control_capability_is_not_permission(world):
    """Commissioned, but automatic control was never permitted (SDD section 47)."""
    binding = world.get(PointBinding, POINT_ID)
    binding.automatic_control_allowed = False
    world.commit()

    result = binding_not_commissioned(world, make_request(), make_context(world))

    assert result.allowed is False
    assert "automatic control has not been permitted" in result.reason


def test_commissioned_binding_allows(world):
    assert binding_not_commissioned(world, make_request(), make_context(world)).allowed is True


# ---------------------------------------------------------------------------
# 3. point_not_control_capable
# ---------------------------------------------------------------------------


def test_measurement_point_cannot_be_commanded(world):
    point = world.get(Point, POINT_ID)
    point.control_capable = False
    world.commit()

    result = point_not_control_capable(world, make_request(), make_context(world))
    assert result.allowed is False
    assert result.code == POINT_NOT_CONTROL_CAPABLE


def test_unknown_point_is_denied(world):
    result = point_not_control_capable(world, make_request(), make_context(world, point=None))
    assert result.allowed is False
    assert "not in the registry" in result.reason


# ---------------------------------------------------------------------------
# 4. asset_in_maintenance
# ---------------------------------------------------------------------------


MAINTENANCE_CHAIN = {"site": "automatic", "domain": "automatic", "asset": "maintenance"}


def test_maintenance_inhibits_service_issued_commands(world):
    context = make_context(
        world, issuer_kind="service", mode_chain=MAINTENANCE_CHAIN, effective_mode="maintenance"
    )
    result = asset_in_maintenance(world, make_request(), context)

    assert result.allowed is False
    assert result.code == ASSET_IN_MAINTENANCE
    assert result.detail["scopes_in_maintenance"] == ["asset"]


def test_maintenance_inhibits_humans_without_an_explicit_override(world):
    context = make_context(
        world, mode_chain=MAINTENANCE_CHAIN, effective_mode="maintenance", principal_role="maintainer"
    )
    result = asset_in_maintenance(world, make_request(), context)

    assert result.allowed is False
    assert "maintenance_override" in result.reason


def test_maintenance_override_requires_the_maintainer_role(world):
    context = make_context(
        world, mode_chain=MAINTENANCE_CHAIN, effective_mode="maintenance", principal_role="operator"
    )
    result = asset_in_maintenance(world, make_request(maintenance_override=True), context)

    assert result.allowed is False
    assert "'maintainer' role" in result.reason


def test_maintainer_may_override_and_the_override_is_recorded(world):
    context = make_context(
        world,
        mode_chain=MAINTENANCE_CHAIN,
        effective_mode="maintenance",
        principal_role="maintainer",
        principal_name="jo.maintainer",
    )
    result = asset_in_maintenance(world, make_request(maintenance_override=True), context)

    assert result.allowed is True
    assert result.override_by == "jo.maintainer"
    assert result.as_record()["override_by"] == "jo.maintainer"


def test_no_maintenance_lockout_allows(world):
    assert asset_in_maintenance(world, make_request(), make_context(world)).allowed is True


# ---------------------------------------------------------------------------
# 5. emergency_mode_lockout
# ---------------------------------------------------------------------------


EMERGENCY_CHAIN = {"site": "emergency", "domain": "automatic", "asset": "automatic"}


def test_emergency_denies_anything_off_the_protective_allow_list(world):
    context = make_context(world, mode_chain=EMERGENCY_CHAIN, effective_mode="emergency")
    result = EmergencyLockout()(world, make_request(command="start"), context)

    assert result.allowed is False
    assert result.code == EMERGENCY_MODE_LOCKOUT
    assert result.detail["scopes_in_emergency"] == ["site"]


def test_emergency_permits_protective_commands(world):
    context = make_context(world, mode_chain=EMERGENCY_CHAIN, effective_mode="emergency")
    result = EmergencyLockout()(world, make_request(command="stop"), context)

    assert result.allowed is True


def test_emergency_allow_list_is_configurable_per_subsystem(world):
    context = make_context(world, mode_chain=EMERGENCY_CHAIN, effective_mode="emergency")
    lockout = EmergencyLockout(allowed_commands=frozenset({"black_start"}))

    assert lockout(world, make_request(command="black_start"), context).allowed is True
    assert lockout(world, make_request(command="stop"), context).allowed is False


def test_no_emergency_allows_everything(world):
    context = make_context(world)
    assert EmergencyLockout()(world, make_request(command="start"), context).allowed is True


# ---------------------------------------------------------------------------
# 6. asset_not_operational
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_status", ["concept", "planned", "procured", "failed", "retired"])
def test_non_operational_assets_refuse_commands(world, bad_status):
    asset = world.get(Asset, ASSET_ID)
    asset.status = bad_status
    world.commit()

    result = asset_not_operational(world, make_request(), make_context(world))
    assert result.allowed is False
    assert result.code == ASSET_NOT_OPERATIONAL


@pytest.mark.parametrize("good_status", ["installed", "commissioned", "active", "degraded"])
def test_operational_assets_accept_commands(world, good_status):
    asset = world.get(Asset, ASSET_ID)
    asset.status = good_status
    world.commit()

    assert asset_not_operational(world, make_request(), make_context(world)).allowed is True


def test_unknown_asset_is_denied(world):
    result = asset_not_operational(world, make_request(), make_context(world, asset=None))
    assert result.allowed is False


# ---------------------------------------------------------------------------
# 7. stale_input -- SDD sections 5.5 and 26.6
# ---------------------------------------------------------------------------


def _add_state(session, quality="good", ts=NOW, point_id=SOC_POINT_ID):
    session.add(
        CurrentState(
            point_id=point_id,
            asset_id=point_id.split("/")[0],
            point_name=point_id.split("/")[1],
            value_numeric=61.0,
            quality=quality,
            ts=ts,
            stale_after_s=300,
        )
    )
    session.commit()


def test_declared_dependency_with_no_measurement_denies(world):
    result = StaleInputInterlock()(
        world, make_request(depends_on=[SOC_POINT_ID]), make_context(world)
    )

    assert result.allowed is False
    assert result.code == STALE_INPUT
    assert result.detail["problems"][0]["quality"] == "missing"


@pytest.mark.parametrize("quality", ["bad", "stale"])
def test_unusable_quality_denies(world, quality):
    _add_state(world, quality=quality)
    result = StaleInputInterlock()(
        world, make_request(depends_on=[SOC_POINT_ID]), make_context(world)
    )

    assert result.allowed is False
    assert result.detail["problems"][0]["quality"] == quality


def test_measurement_older_than_the_point_timeout_denies(world):
    _add_state(world, ts=NOW - dt.timedelta(seconds=900))
    result = StaleInputInterlock()(
        world, make_request(depends_on=[SOC_POINT_ID]), make_context(world)
    )

    assert result.allowed is False
    assert result.detail["problems"][0]["reason"] == "older than the point timeout"


def test_fresh_good_measurement_allows(world):
    _add_state(world)
    result = StaleInputInterlock()(
        world, make_request(depends_on=[SOC_POINT_ID]), make_context(world)
    )

    assert result.allowed is True
    assert SOC_POINT_ID in result.detail["checked"]


def test_failed_feedback_on_the_target_point_denies(world):
    _add_state(world, quality="bad", point_id=POINT_ID)
    result = StaleInputInterlock()(world, make_request(), make_context(world))

    assert result.allowed is False
    assert result.detail["problems"][0]["point_id"] == POINT_ID


def test_absent_target_feedback_is_normal_and_allows(world):
    assert StaleInputInterlock()(world, make_request(), make_context(world)).allowed is True


def test_quality_tolerance_is_configurable(world):
    _add_state(world, quality="uncertain")
    strict = StaleInputInterlock(unusable_qualities=frozenset({"bad", "stale", "uncertain"}))

    assert (
        strict(world, make_request(depends_on=[SOC_POINT_ID]), make_context(world)).allowed is False
    )
    assert (
        StaleInputInterlock()(
            world, make_request(depends_on=[SOC_POINT_ID]), make_context(world)
        ).allowed
        is True
    )


# ---------------------------------------------------------------------------
# 8. duplicate_in_flight
# ---------------------------------------------------------------------------


def _add_command(session, command_id, state="dispatched", command="start"):
    session.add(
        Command(
            command_id=command_id,
            asset_id=ASSET_ID,
            point_id=POINT_ID,
            command=command,
            issued_by="test",
            reason="r",
            issued_at=NOW,
            state=state,
            interlocks_evaluated=[],
        )
    )
    session.commit()


def test_duplicate_reports_the_in_flight_commands_to_supersede(world):
    _add_command(world, "01AAA", state="dispatched")
    _add_command(world, "01BBB", state="pending")

    result = duplicate_in_flight(world, make_request(), make_context(world))

    assert result.allowed is True
    assert result.code == DUPLICATE_IN_FLIGHT
    assert set(result.detail["supersedes"]) == {"01AAA", "01BBB"}


def test_terminal_commands_are_not_superseded(world):
    _add_command(world, "01CCC", state="succeeded")
    _add_command(world, "01DDD", state="rejected")

    result = duplicate_in_flight(world, make_request(), make_context(world))
    assert result.detail == {}


def test_other_commands_on_the_same_asset_are_untouched(world):
    _add_command(world, "01EEE", state="dispatched", command="stop")

    result = duplicate_in_flight(world, make_request(command="start"), make_context(world))
    assert result.detail == {}


# ---------------------------------------------------------------------------
# Registry behaviour
# ---------------------------------------------------------------------------


def test_default_registry_holds_every_builtin_in_evaluation_order(world):
    registry = default_registry()

    assert registry.names()[0] == PHYSICAL_CONTROL_DISABLED
    assert set(registry.names()) == set(BUILTIN_INTERLOCK_CODES)


def test_every_interlock_is_recorded_not_just_the_first_denial(world):
    """SDD 17.4: the operator must see the interlocks, plural."""
    settings = make_settings(allow_physical_control=False)
    world.get(Asset, ASSET_ID).status = "planned"
    world.commit()

    evaluation = default_registry().evaluate(world, make_request(), make_context(world, settings))

    assert evaluation.allowed is False
    assert len(evaluation.records) == len(BUILTIN_INTERLOCK_CODES)
    assert set(evaluation.denial_codes) == {PHYSICAL_CONTROL_DISABLED, ASSET_NOT_OPERATIONAL}
    assert evaluation.first_denial.code == PHYSICAL_CONTROL_DISABLED
    assert all(set(record) >= {"code", "allowed", "reason"} for record in evaluation.records)


def test_clean_world_passes_every_interlock(world):
    evaluation = default_registry().evaluate(world, make_request(), make_context(world))

    assert evaluation.allowed is True
    assert evaluation.dispatch_allowed is True
    assert evaluation.denials == ()


def test_dry_run_is_allowed_but_not_dispatchable(world):
    evaluation = default_registry().evaluate(
        world, make_request(dry_run=True), make_context(world)
    )

    assert evaluation.allowed is True
    assert evaluation.dispatch_allowed is False
    assert evaluation.blocking_dispatch[0].code == PHYSICAL_CONTROL_DISABLED


def test_subsystems_can_register_extra_interlocks(world):
    registry = default_registry()

    @registry.register(name="power_budget", order=900)
    def power_budget(session, request, context):
        return InterlockResult.deny("power_budget", "No power budget available")

    evaluation = registry.evaluate(world, make_request(), make_context(world))

    assert "power_budget" in registry.names()
    assert evaluation.allowed is False
    assert evaluation.denial_codes == ("power_budget",)


def test_a_raising_interlock_denies_rather_than_opening(world):
    registry = InterlockRegistry()

    @registry.register(name="explodes")
    def explodes(session, request, context):
        raise RuntimeError("sensor bus offline")

    evaluation = registry.evaluate(world, make_request(), make_context(world))

    assert evaluation.allowed is False
    assert evaluation.first_denial.code == INTERLOCK_ERROR
    assert evaluation.first_denial.detail["interlock"] == "explodes"


def test_registry_rejects_duplicate_names_unless_replacing(world):
    registry = default_registry()

    with pytest.raises(ValueError):
        registry.register(physical_control_disabled, name=PHYSICAL_CONTROL_DISABLED)

    registry.register(
        lambda s, r, c: InterlockResult.allow(PHYSICAL_CONTROL_DISABLED, "stubbed"),
        name=PHYSICAL_CONTROL_DISABLED,
        replace=True,
    )
    assert len(registry) == len(BUILTIN_INTERLOCK_CODES)

    assert registry.unregister(PHYSICAL_CONTROL_DISABLED) is True
    assert PHYSICAL_CONTROL_DISABLED not in registry


def test_role_ranking_matches_the_sdd_role_model():
    assert role_at_least("administrator", "maintainer") is True
    assert role_at_least("operator", "maintainer") is False
    assert role_at_least(None, "operator") is False
