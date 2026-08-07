"""Command lifecycle, dispatch service and control API.

The through-line of this file is SDD section 5.7: every command records who or
what issued it, why, when, under which operating mode, and whether it was
accepted, rejected, timed out or overridden. Each test checks the outcome *and*
the audit row that proves it happened.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from homestead_twin.commands.interlocks import (
    BINDING_NOT_COMMISSIONED,
    PHYSICAL_CONTROL_DISABLED,
    reset_global_registry,
)
from homestead_twin.commands.manager import (
    CommandManager,
    CommandRequest,
    CommandStateError,
    CommandValidationError,
    ServiceActor,
    UnknownCommandError,
    UnknownTargetError,
    new_command_id,
)
from homestead_twin.commands.modes import ModeManager
from homestead_twin.commands.service import CommandDispatchService, ack_subscription
from homestead_twin.config import Settings
from homestead_twin.envelope import CommandAckEnvelope, parse_command
from homestead_twin.models.commands import AuditLogEntry, Command
from homestead_twin.models.registry import Asset, AssetClass, Point, PointBinding, PointDefinition
from homestead_twin.models.telemetry import CurrentState

SITE_ID = "site.site.primary.01"
ASSET_ID = "water.pump.orchard.01"
POINT_ID = f"{ASSET_ID}/start"
COMMAND_TOPIC = "homestead/water/orchard/pump_01/cmd/start"
ACK_TOPIC = f"{COMMAND_TOPIC}/ack"
NOW = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_interlock_registry():
    """Keep a test that extends the global registry from leaking into the next."""
    reset_global_registry()
    yield
    reset_global_registry()


def make_settings(**overrides) -> Settings:
    base = {
        "database_url": "sqlite://",
        "mqtt_enabled": False,
        "ems_enabled": False,
        "alarm_engine_enabled": False,
        "allow_physical_control": True,
        "command_default_ttl_s": 300,
        "site_id": SITE_ID,
        "node_role": "primary",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def control_settings() -> Settings:
    """Settings with the master switch on, as after SDD section 19 commissioning."""
    return make_settings()


def seed_pump(
    session,
    *,
    asset_id: str = ASSET_ID,
    point_name: str = "start",
    status: str = "active",
    binding_status: str = "commissioned",
    automatic_control_allowed: bool = True,
    control_capable: bool = True,
    command_topic: str | None = None,
    with_binding: bool = True,
) -> None:
    if session.get(AssetClass, "pump") is None:
        session.add(AssetClass(name="pump", allowed_domains=["water"]))
    if session.get(PointDefinition, point_name) is None:
        session.add(
            PointDefinition(
                name=point_name, default_class="CMD", data_type="boolean", control_capable=True
            )
        )
    session.add(
        Asset(
            asset_id=asset_id,
            domain="water",
            asset_class="pump",
            name="Orchard transfer pump",
            status=status,
        )
    )
    point_id = f"{asset_id}/{point_name}"
    session.add(
        Point(
            point_id=point_id,
            asset_id=asset_id,
            point_name=point_name,
            point_class="CMD",
            data_type="boolean",
            control_capable=control_capable,
            automatic_control_allowed=True,
        )
    )
    if with_binding:
        session.add(
            PointBinding(
                point_id=point_id,
                asset_id=asset_id,
                point_name=point_name,
                binding_status=binding_status,
                source_protocol="mqtt",
                command_topic=command_topic,
                automatic_control_allowed=automatic_control_allowed,
            )
        )
    session.commit()


@pytest.fixture()
def pump(db_session):
    seed_pump(db_session)
    return db_session


def manager(session, bus, settings) -> CommandManager:
    return CommandManager(session, bus, settings)


def request_start(**overrides) -> CommandRequest:
    payload = {
        "asset_id": ASSET_ID,
        "command": "start",
        "reason": "orchard block A irrigation window",
        "value": True,
    }
    payload.update(overrides)
    return CommandRequest(**payload)


OPERATOR = ServiceActor("test.operator", role="operator", kind="human")
MAINTAINER = ServiceActor("jo.maintainer", role="maintainer", kind="human")
EMS = ServiceActor("rules.energy_manager")


def audit(session, action=None, outcome=None):
    stmt = select(AuditLogEntry)
    if action:
        stmt = stmt.where(AuditLogEntry.action == action)
    if outcome:
        stmt = stmt.where(AuditLogEntry.outcome == outcome)
    return list(session.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def test_command_ids_are_unique_and_sort_by_issue_time():
    early = new_command_id(NOW)
    late = new_command_id(NOW + dt.timedelta(seconds=1))

    assert len(early) == 26
    assert early < late
    assert new_command_id(NOW) != new_command_id(NOW)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_master_switch_refuses_dispatch(pump, bus):
    """The platform ships refusing to actuate (Settings.allow_physical_control)."""
    settings = make_settings(allow_physical_control=False)

    command = manager(pump, bus, settings).issue(request_start(), OPERATOR, now=NOW)

    assert command.state == "rejected"
    assert command.dispatched_at is None
    assert bus.published == []
    assert PHYSICAL_CONTROL_DISABLED in command.state_reason

    codes = {record["code"] for record in command.interlocks_evaluated if not record["allowed"]}
    assert codes == {PHYSICAL_CONTROL_DISABLED}

    entry = audit(pump, "command.issue")[0]
    assert entry.outcome == "rejected"
    assert entry.actor == "test.operator"
    assert entry.reason == "orchard block A irrigation window"
    assert entry.detail["denied_by"] == [PHYSICAL_CONTROL_DISABLED]
    assert [r.result for r in command.results] == ["rejected"]


def test_uncommissioned_binding_refuses_dispatch(db_session, bus, control_settings):
    seed_pump(db_session, binding_status="tbd")

    command = manager(db_session, bus, control_settings).issue(request_start(), OPERATOR, now=NOW)

    assert command.state == "rejected"
    assert bus.published == []
    assert BINDING_NOT_COMMISSIONED in command.state_reason


def test_control_capability_without_permission_refuses(db_session, bus, control_settings):
    """SDD section 47: a control-capable point is not an authorised one."""
    seed_pump(db_session, automatic_control_allowed=False)

    command = manager(db_session, bus, control_settings).issue(request_start(), OPERATOR, now=NOW)

    assert command.state == "rejected"
    assert bus.published == []


def test_unknown_asset_is_rejected_before_anything_is_written(pump, bus, control_settings):
    with pytest.raises(UnknownTargetError):
        manager(pump, bus, control_settings).issue(
            request_start(asset_id="water.pump.nowhere.99"), OPERATOR, now=NOW
        )


def test_a_command_without_a_reason_is_refused(pump, bus, control_settings):
    with pytest.raises(CommandValidationError):
        manager(pump, bus, control_settings).issue(request_start(reason="  "), OPERATOR, now=NOW)


def test_command_with_no_addressable_topic_fails_rather_than_guessing(db_session, bus, control_settings):
    # A five-part asset id cannot be projected onto the topic convention and the
    # binding declares no explicit command topic.
    weird = "water.pump.orchard.north.01"
    seed_pump(db_session, asset_id=weird)

    command = manager(db_session, bus, control_settings).issue(
        request_start(asset_id=weird), OPERATOR, now=NOW
    )

    assert command.state == "failed"
    assert bus.published == []
    assert "No dispatch topic" in command.state_reason
    assert audit(db_session, "command.dispatch")[0].outcome == "failed"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_issue_dispatch_ack_succeeded(pump, bus, control_settings):
    mgr = manager(pump, bus, control_settings)

    command = mgr.issue(request_start(), OPERATOR, now=NOW)

    # -- issued and dispatched ------------------------------------------
    assert command.state == "dispatched"
    assert command.dispatch_topic == COMMAND_TOPIC
    assert command.operating_mode == "automatic"
    assert command.issued_by == "test.operator"
    assert command.issued_by_kind == "human"
    assert command.expires_at is not None

    # -- the envelope really is on the bus ------------------------------
    message = bus.last(COMMAND_TOPIC)
    assert message is not None
    envelope = parse_command(message.payload)
    assert envelope.command_id == command.command_id
    assert envelope.asset_id == ASSET_ID
    assert envelope.command == "start"
    assert envelope.value is True
    assert envelope.reason == "orchard block A irrigation window"
    assert envelope.operating_mode == "automatic"
    assert envelope.requires_ack is True

    # -- requested vs actual (SDD sections 10.3 and 17.4) ---------------
    state = pump.get(CurrentState, POINT_ID)
    assert state.requested_value["value"] is True
    assert state.requested_value["command_id"] == command.command_id
    assert state.requested_at is not None
    assert state.value is None  # nothing measured yet: requested is not actual

    # -- the controller accepts ------------------------------------------
    accepted_at = NOW + dt.timedelta(seconds=2)
    mgr.record_ack(
        CommandAckEnvelope(
            command_id=command.command_id,
            asset_id=ASSET_ID,
            result="accepted",
            reported_by="plc.orchard",
            reported_at=accepted_at,
        )
    )
    assert command.state == "acknowledged"
    assert command.completed_at is None

    # -- and then reports success ----------------------------------------
    done_at = NOW + dt.timedelta(seconds=9)
    mgr.record_ack(
        CommandAckEnvelope(
            command_id=command.command_id,
            asset_id=ASSET_ID,
            result="succeeded",
            detail="pump running, discharge pressure normal",
            reported_by="plc.orchard",
            reported_at=done_at,
        )
    )
    assert command.state == "succeeded"
    assert command.is_terminal is True
    assert command.completed_at is not None
    assert [r.result for r in command.results] == ["accepted", "succeeded"]

    # -- and every step is audited ---------------------------------------
    actions = [entry.action for entry in audit(pump)]
    assert actions == ["command.issue", "command.dispatch", "command.ack", "command.ack"]
    assert [entry.outcome for entry in audit(pump)] == [
        "accepted",
        "dispatched",
        "accepted",
        "succeeded",
    ]


def test_binding_command_topic_overrides_the_derived_topic(db_session, bus, control_settings):
    seed_pump(db_session, command_topic="vendor/plc7/coil/12/write")

    command = manager(db_session, bus, control_settings).issue(request_start(), OPERATOR, now=NOW)

    assert command.dispatch_topic == "vendor/plc7/coil/12/write"
    assert bus.last("vendor/plc7/coil/12/write") is not None


def test_level_one_may_reject_what_level_three_requests(pump, bus, control_settings):
    """SDD section 6: the local controller keeps the last word."""
    mgr = manager(pump, bus, control_settings)
    command = mgr.issue(request_start(), OPERATOR, now=NOW)

    mgr.record_ack(
        CommandAckEnvelope(
            command_id=command.command_id,
            asset_id=ASSET_ID,
            result="rejected",
            detail="dry-run lockout: cistern level below minimum",
            reported_by="plc.orchard",
        )
    )

    assert command.state == "rejected"
    assert command.is_terminal is True
    assert audit(pump, "command.ack")[0].outcome == "rejected"


def test_fire_and_forget_commands_complete_at_dispatch(pump, bus, control_settings):
    command = manager(pump, bus, control_settings).issue(
        request_start(requires_ack=False), OPERATOR, now=NOW
    )

    assert command.state == "succeeded"
    assert command.dispatched_at is not None
    assert bus.last(COMMAND_TOPIC) is not None


def test_service_issued_commands_are_recorded_as_such(pump, bus, control_settings):
    command = manager(pump, bus, control_settings).issue(
        request_start(reason="battery_reserve_protection"), EMS, now=NOW
    )

    assert command.issued_by == "rules.energy_manager"
    assert command.issued_by_kind == "service"
    assert command.state == "dispatched"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_evaluates_everything_and_publishes_nothing(pump, bus, control_settings):
    command = manager(pump, bus, control_settings).issue(
        request_start(dry_run=True), OPERATOR, now=NOW
    )

    assert bus.published == []
    assert command.dispatched_at is None
    assert command.is_terminal is True
    assert "Not dispatched" in command.state_reason
    assert all(record["allowed"] for record in command.interlocks_evaluated)
    assert audit(pump, "command.dry_run")[0].outcome == "not_dispatched"


def test_dry_run_still_reports_the_interlocks_that_would_refuse(pump, bus):
    settings = make_settings(allow_physical_control=False)
    seed = pump.get(PointBinding, POINT_ID)
    seed.binding_status = "tbd"
    pump.commit()

    command = manager(pump, bus, settings).issue(request_start(dry_run=True), OPERATOR, now=NOW)

    assert command.state == "rejected"
    assert BINDING_NOT_COMMISSIONED in command.state_reason
    assert bus.published == []


# ---------------------------------------------------------------------------
# Idempotency and supersede
# ---------------------------------------------------------------------------


def test_idempotency_key_returns_the_original_command(pump, bus, control_settings):
    mgr = manager(pump, bus, control_settings)

    first = mgr.issue(request_start(idempotency_key="irrigation-2026-08-07-A"), OPERATOR, now=NOW)
    second = mgr.issue(
        request_start(idempotency_key="irrigation-2026-08-07-A"),
        OPERATOR,
        now=NOW + dt.timedelta(seconds=5),
    )

    assert second.command_id == first.command_id
    assert len(pump.execute(select(Command)).scalars().all()) == 1
    assert len([m for m in bus.published if m.topic == COMMAND_TOPIC]) == 1
    assert audit(pump, "command.issue", "idempotent_replay")


def test_a_newer_command_supersedes_the_older_one(pump, bus, control_settings):
    mgr = manager(pump, bus, control_settings)

    first = mgr.issue(request_start(value=True), OPERATOR, now=NOW)
    assert first.state == "dispatched"

    second = mgr.issue(
        request_start(value=False, reason="rain sensor tripped"),
        OPERATOR,
        now=NOW + dt.timedelta(seconds=30),
    )

    assert first.state == "superseded"
    assert first.is_terminal is True
    assert first.state_reason == f"Superseded by {second.command_id}"
    assert second.state == "dispatched"
    assert [r.result for r in first.results] == ["superseded"]

    entry = audit(pump, "command.supersede")[0]
    assert entry.outcome == "superseded"
    assert entry.detail["superseded_by"] == second.command_id


def test_superseded_commands_ignore_late_acks(pump, bus, control_settings):
    mgr = manager(pump, bus, control_settings)
    first = mgr.issue(request_start(), OPERATOR, now=NOW)
    mgr.issue(request_start(reason="second thoughts"), OPERATOR, now=NOW + dt.timedelta(seconds=1))

    mgr.record_ack(
        CommandAckEnvelope(command_id=first.command_id, asset_id=ASSET_ID, result="succeeded")
    )

    assert first.state == "superseded"
    assert audit(pump, "command.ack")[0].outcome == "ignored_terminal"


# ---------------------------------------------------------------------------
# Expiry, cancellation and stray acknowledgements
# ---------------------------------------------------------------------------


def test_commands_expire_when_the_ttl_elapses(pump, bus, control_settings):
    mgr = manager(pump, bus, control_settings)
    command = mgr.issue(request_start(ttl_s=60), OPERATOR, now=NOW)

    assert mgr.expire_due(NOW + dt.timedelta(seconds=30)) == []
    assert command.state == "dispatched"

    expired = mgr.expire_due(NOW + dt.timedelta(seconds=61))

    assert [c.command_id for c in expired] == [command.command_id]
    assert command.state == "expired"
    assert command.completed_at is not None
    assert [r.result for r in command.results] == ["expired"]
    assert audit(pump, "command.expire")[0].outcome == "expired"


def test_default_ttl_comes_from_settings(pump, bus):
    settings = make_settings(command_default_ttl_s=45)
    mgr = manager(pump, bus, settings)

    command = mgr.issue(request_start(), OPERATOR, now=NOW)

    assert mgr.expire_due(NOW + dt.timedelta(seconds=46))
    assert command.state == "expired"


def test_expiry_leaves_terminal_commands_alone(pump, bus, control_settings):
    mgr = manager(pump, bus, control_settings)
    command = mgr.issue(request_start(ttl_s=60), OPERATOR, now=NOW)
    mgr.record_ack(
        CommandAckEnvelope(command_id=command.command_id, asset_id=ASSET_ID, result="succeeded")
    )

    assert mgr.expire_due(NOW + dt.timedelta(hours=1)) == []
    assert command.state == "succeeded"


def test_cancel_records_who_stopped_waiting_and_why(pump, bus, control_settings):
    mgr = manager(pump, bus, control_settings)
    command = mgr.issue(request_start(), OPERATOR, now=NOW)

    mgr.cancel(command.command_id, OPERATOR, "operator walked to the pump house", now=NOW)

    assert command.state == "cancelled"
    assert command.is_terminal is True
    assert "test.operator" in command.state_reason
    entry = audit(pump, "command.cancel")[0]
    assert entry.outcome == "cancelled"
    assert entry.reason == "operator walked to the pump house"


def test_cancelling_a_terminal_command_is_refused(pump, bus, control_settings):
    mgr = manager(pump, bus, control_settings)
    command = mgr.issue(request_start(), OPERATOR, now=NOW)
    mgr.record_ack(
        CommandAckEnvelope(command_id=command.command_id, asset_id=ASSET_ID, result="succeeded")
    )

    with pytest.raises(CommandStateError):
        mgr.cancel(command.command_id, OPERATOR, "too late", now=NOW)

    assert audit(pump, "command.cancel")[0].outcome == "refused"


def test_cancelling_an_unknown_command_raises(pump, bus, control_settings):
    with pytest.raises(UnknownCommandError):
        manager(pump, bus, control_settings).cancel("01NOPE", OPERATOR, "typo", now=NOW)


def test_ack_for_an_unknown_command_is_logged_not_fatal(pump, bus, control_settings):
    result = manager(pump, bus, control_settings).record_ack(
        CommandAckEnvelope(command_id="01GHOST", asset_id=ASSET_ID, result="succeeded")
    )

    assert result is None
    assert audit(pump, "command.ack")[0].outcome == "unknown_command"


def test_ack_claiming_the_wrong_asset_is_refused(pump, bus, control_settings):
    mgr = manager(pump, bus, control_settings)
    command = mgr.issue(request_start(), OPERATOR, now=NOW)

    mgr.record_ack(
        CommandAckEnvelope(
            command_id=command.command_id, asset_id="energy.inverter.power_container.01", result="succeeded"
        )
    )

    assert command.state == "dispatched"
    assert audit(pump, "command.ack")[0].outcome == "asset_mismatch"


def test_repeated_acks_are_idempotent(pump, bus, control_settings):
    mgr = manager(pump, bus, control_settings)
    command = mgr.issue(request_start(), OPERATOR, now=NOW)
    ack = CommandAckEnvelope(
        command_id=command.command_id, asset_id=ASSET_ID, result="succeeded", reported_by="plc"
    )

    mgr.record_ack(ack)
    mgr.record_ack(ack)

    assert command.state == "succeeded"
    assert [r.result for r in command.results] == ["succeeded"]
    assert [e.outcome for e in audit(pump, "command.ack")] == ["succeeded", "ignored_terminal"]


# ---------------------------------------------------------------------------
# Operating modes on the command path
# ---------------------------------------------------------------------------


def test_emergency_mode_blocks_a_start_but_not_a_stop(pump, bus, control_settings):
    ModeManager(pump, site_id=SITE_ID).set_mode(
        "site", SITE_ID, "emergency", OPERATOR, "PV and generator both offline", now=NOW
    )
    mgr = manager(pump, bus, control_settings)

    blocked = mgr.issue(request_start(), OPERATOR, now=NOW)
    assert blocked.state == "rejected"
    assert blocked.operating_mode == "emergency"
    assert bus.published == []

    seed_pump(pump, asset_id="water.pump.orchard.02", point_name="stop")
    protective = mgr.issue(
        CommandRequest(
            asset_id="water.pump.orchard.02",
            command="stop",
            reason="emergency shutdown",
        ),
        OPERATOR,
        now=NOW,
    )
    assert protective.state == "dispatched"


def test_maintenance_blocks_the_ems_but_a_maintainer_may_override(pump, bus, control_settings):
    ModeManager(pump, site_id=SITE_ID).set_mode(
        "asset", ASSET_ID, "maintenance", MAINTAINER, "impeller service", now=NOW
    )
    mgr = manager(pump, bus, control_settings)

    refused = mgr.issue(request_start(reason="scheduled irrigation"), EMS, now=NOW)
    assert refused.state == "rejected"
    assert refused.operating_mode == "maintenance"

    allowed = mgr.issue(
        request_start(reason="bump test after reassembly", maintenance_override=True),
        MAINTAINER,
        now=NOW,
    )
    assert allowed.state == "dispatched"

    override = audit(pump, "command.interlock_override")[0]
    assert override.outcome == "overridden"
    assert override.actor == "jo.maintainer"
    assert override.detail["code"] == "asset_in_maintenance"


# ---------------------------------------------------------------------------
# Background service
# ---------------------------------------------------------------------------


def test_ack_subscription_matches_the_command_ack_topic():
    from homestead_twin.mqtt import topic_matches

    assert topic_matches(ack_subscription("homestead"), ACK_TOPIC)
    assert not topic_matches(ack_subscription("homestead"), COMMAND_TOPIC)


def test_service_records_acknowledgements_arriving_on_the_bus(
    session_factory, bus, control_settings
):
    session = session_factory()
    seed_pump(session)
    command = manager(session, bus, control_settings).issue(request_start(), OPERATOR, now=NOW)
    command_id = command.command_id
    session.close()

    service = CommandDispatchService(session_factory, bus, control_settings, sweep_interval_s=0)
    service.start()
    try:
        bus.publish(
            ACK_TOPIC,
            CommandAckEnvelope(
                command_id=command_id,
                asset_id=ASSET_ID,
                result="succeeded",
                reported_by="plc.orchard",
            ).to_payload(),
        )
    finally:
        service.stop()

    check = session_factory()
    assert check.get(Command, command_id).state == "succeeded"
    check.close()


def test_service_survives_a_malformed_ack(session_factory, bus, control_settings):
    service = CommandDispatchService(session_factory, bus, control_settings, sweep_interval_s=0)
    service.start()
    try:
        bus.publish(ACK_TOPIC, b"{not json")
        bus.publish(ACK_TOPIC, json.dumps({"command_id": "01X"}).encode())  # missing fields
        bus.publish(ACK_TOPIC, b"")
    finally:
        service.stop()

    session = session_factory()
    assert session.execute(select(Command)).scalars().all() == []
    session.close()


def test_service_sweep_expires_commands_and_clears_temporary_modes(
    session_factory, bus, control_settings
):
    session = session_factory()
    seed_pump(session)
    command = manager(session, bus, control_settings).issue(
        request_start(ttl_s=30), OPERATOR, now=NOW
    )
    command_id = command.command_id
    ModeManager(session, site_id=SITE_ID).set_mode(
        "asset",
        ASSET_ID,
        "manual",
        OPERATOR,
        "local work",
        expires_at=NOW + dt.timedelta(minutes=5),
        now=NOW,
    )
    session.close()

    service = CommandDispatchService(session_factory, bus, control_settings, sweep_interval_s=0)
    outcome = service.sweep(NOW + dt.timedelta(minutes=10))

    assert outcome == {"expired_commands": 1, "cleared_modes": 1}

    check = session_factory()
    assert check.get(Command, command_id).state == "expired"
    assert ModeManager(check, site_id=SITE_ID).resolve("asset", ASSET_ID) == "automatic"
    check.close()


def test_service_sweep_never_clears_an_emergency(session_factory, bus, control_settings):
    session = session_factory()
    seed_pump(session)
    ModeManager(session, site_id=SITE_ID).set_mode(
        "site", SITE_ID, "emergency", OPERATOR, "flood", now=NOW
    )
    session.close()

    service = CommandDispatchService(session_factory, bus, control_settings, sweep_interval_s=0)
    assert service.sweep(NOW + dt.timedelta(days=365))["cleared_modes"] == 0

    check = session_factory()
    assert ModeManager(check, site_id=SITE_ID).resolve("site", SITE_ID) == "emergency"
    check.close()


def test_service_start_and_stop_are_clean(session_factory, bus, control_settings):
    """The sweep thread waits on an event, so stop() returns without sleeping."""
    service = CommandDispatchService(session_factory, bus, control_settings, sweep_interval_s=3600)
    service.start()
    assert service._thread is not None and service._thread.is_alive()

    service.stop()
    assert service._thread is None

    # Restarting must not double-subscribe.
    service.start()
    service.stop()
    assert len([f for f, _ in bus.subscriptions if f == ack_subscription("homestead")]) == 1


def test_service_matches_the_runtime_background_service_protocol(
    session_factory, bus, control_settings
):
    from homestead_twin.runtime import BackgroundService

    service = CommandDispatchService(session_factory, bus, control_settings)
    assert isinstance(service, BackgroundService)
    assert service.name == "commands"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture()
def control_client(control_settings, engine, session_factory, bus) -> TestClient:
    """A client whose app has physical control enabled."""
    from homestead_twin.api.app import create_app

    app = create_app(control_settings, bus=bus, start_services=False, init_db=False)
    app.state.engine = engine
    app.state.session_factory = session_factory
    with TestClient(app) as test_client:
        yield test_client


def post_command(client, headers, **overrides):
    body = {
        "asset_id": ASSET_ID,
        "command": "start",
        "value": True,
        "reason": "orchard block A irrigation window",
    }
    body.update(overrides)
    return client.post("/api/v1/commands", json=body, headers=headers)


def test_api_refuses_and_explains_when_the_master_switch_is_off(
    pump, client, operator_headers, bus
):
    response = post_command(client, operator_headers)

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["refused_by"] == PHYSICAL_CONTROL_DISABLED
    assert detail["command_id"]
    assert bus.published == []
    # The whole evaluation comes back, not just the first objection.
    assert len(detail["interlocks_evaluated"]) == 8
    assert detail["command"]["state"] == "rejected"


def test_api_uncommissioned_binding_is_a_409(db_session, control_client, operator_headers):
    seed_pump(db_session, binding_status="tbd")

    response = post_command(control_client, operator_headers)

    assert response.status_code == 409
    assert response.json()["detail"]["refused_by"] == BINDING_NOT_COMMISSIONED


def test_api_issues_and_dispatches(pump, control_client, operator_headers, bus):
    response = post_command(control_client, operator_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "dispatched"
    assert body["dispatch_topic"] == COMMAND_TOPIC
    assert body["issued_by"] == "test.operator"
    assert body["dispatched"] is True
    assert len(body["interlocks_evaluated"]) == 8
    assert parse_command(bus.last(COMMAND_TOPIC).payload).command_id == body["command_id"]


def test_api_requires_an_operator(pump, control_client):
    assert post_command(control_client, {}).status_code == 403
    viewer = {"X-Operator": "nosy", "X-Operator-Role": "viewer"}
    assert post_command(control_client, viewer).status_code == 403


def test_api_reason_is_mandatory(pump, control_client, operator_headers):
    response = control_client.post(
        "/api/v1/commands",
        json={"asset_id": ASSET_ID, "command": "start", "value": True},
        headers=operator_headers,
    )
    assert response.status_code == 422


def test_api_unknown_asset_is_a_404(pump, control_client, operator_headers):
    response = post_command(control_client, operator_headers, asset_id="water.pump.nowhere.99")
    assert response.status_code == 404


def test_api_dry_run_publishes_nothing(pump, control_client, operator_headers, bus):
    response = post_command(control_client, operator_headers, dry_run=True)

    assert response.status_code == 201
    assert response.json()["dispatched"] is False
    assert bus.published == []


def test_api_idempotency_key_is_honoured(pump, control_client, operator_headers, bus):
    first = post_command(control_client, operator_headers, idempotency_key="k-1")
    second = post_command(control_client, operator_headers, idempotency_key="k-1")

    assert first.json()["command_id"] == second.json()["command_id"]
    assert len([m for m in bus.published if m.topic == COMMAND_TOPIC]) == 1


def test_api_get_list_cancel_and_ack(pump, control_client, operator_headers):
    command_id = post_command(control_client, operator_headers).json()["command_id"]

    fetched = control_client.get(f"/api/v1/commands/{command_id}", headers=operator_headers)
    assert fetched.status_code == 200
    assert fetched.json()["command_id"] == command_id

    listed = control_client.get(
        "/api/v1/commands", params={"asset_id": ASSET_ID, "state": "dispatched"}
    )
    assert [item["command_id"] for item in listed.json()] == [command_id]

    acked = control_client.post(
        f"/api/v1/commands/{command_id}/ack",
        json={"result": "succeeded", "detail": "pump running", "reported_by": "scada.bridge"},
        headers=operator_headers,
    )
    assert acked.status_code == 200
    assert acked.json()["state"] == "succeeded"
    assert acked.json()["results"][-1]["reported_by"] == "scada.bridge"

    refused = control_client.post(
        f"/api/v1/commands/{command_id}/cancel",
        json={"reason": "too late"},
        headers=operator_headers,
    )
    assert refused.status_code == 409

    assert control_client.get("/api/v1/commands/01NOPE").status_code == 404


def test_api_cancel_stops_an_in_flight_command(pump, control_client, operator_headers):
    command_id = post_command(control_client, operator_headers).json()["command_id"]

    response = control_client.post(
        f"/api/v1/commands/{command_id}/cancel",
        json={"reason": "rain arrived"},
        headers=operator_headers,
    )

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"


def test_api_operating_modes_round_trip(pump, control_client, operator_headers):
    response = control_client.post(
        "/api/v1/operating-modes/water",
        json={"mode": "maintenance", "reason": "water plant commissioning"},
        headers=operator_headers,
    )
    assert response.status_code == 200
    assert response.json()["scope_type"] == "domain"
    assert response.json()["mode"] == "maintenance"

    listed = control_client.get("/api/v1/operating-modes").json()
    assert {row["scope_id"] for row in listed} == {"water"}

    detail = control_client.get(f"/api/v1/operating-modes/asset/{ASSET_ID}").json()
    assert detail["effective_mode"] == "maintenance"
    assert detail["mode_chain"]["domain"] == "maintenance"

    assert control_client.get("/api/v1/operating-modes/galaxy/x").status_code == 404


def test_api_emergency_latches_and_refuses_to_clear(pump, control_client, operator_headers):
    control_client.post(
        f"/api/v1/operating-modes/{SITE_ID}",
        json={"mode": "emergency", "reason": "battery over-temperature"},
        headers=operator_headers,
    )

    refused = control_client.post(
        f"/api/v1/operating-modes/{SITE_ID}",
        json={"mode": "automatic", "reason": "looks fine now"},
        headers=operator_headers,
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["latched"] is True

    cleared = control_client.post(
        f"/api/v1/operating-modes/{SITE_ID}",
        json={
            "mode": "automatic",
            "reason": "cell temperatures within limits, BMS reset",
            "condition_clear": True,
        },
        headers=operator_headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["mode"] == "automatic"
    assert cleared.json()["previous_mode"] == "emergency"


def test_api_mode_change_requires_an_operator_and_a_known_mode(pump, control_client, operator_headers):
    viewer = {"X-Operator": "nosy", "X-Operator-Role": "viewer"}
    assert (
        control_client.post(
            "/api/v1/operating-modes/water",
            json={"mode": "off", "reason": "why not"},
            headers=viewer,
        ).status_code
        == 403
    )
    assert (
        control_client.post(
            "/api/v1/operating-modes/water",
            json={"mode": "party", "reason": "no such mode"},
            headers=operator_headers,
        ).status_code
        == 422
    )


def test_api_audit_trail_is_maintainer_only(pump, control_client, operator_headers, admin_headers):
    post_command(control_client, operator_headers)

    assert control_client.get("/api/v1/audit", headers=operator_headers).status_code == 403

    entries = control_client.get(
        "/api/v1/audit", params={"action": "command.issue"}, headers=admin_headers
    ).json()
    assert [entry["action"] for entry in entries] == ["command.issue"]
    assert entries[0]["actor"] == "test.operator"
    assert entries[0]["outcome"] == "accepted"

    by_actor = control_client.get(
        "/api/v1/audit", params={"actor": "nobody"}, headers=admin_headers
    ).json()
    assert by_actor == []
