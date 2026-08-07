"""End-to-end integration across every subsystem.

SDD section 49 work-queue item 8 requires the EMS state machine to be
prototyped against simulated MQTT telemetry before any physical control is
enabled, and Phase A (section 20) requires a simulated MQTT environment before
property deployment. These tests are that proof: the simulated homestead
publishes real envelopes on real topics, ingest resolves them through the
registry, and the EMS evaluates the resulting current state.

Nothing here is mocked between the subsystems -- only the broker (in-process)
and the equipment (simulated) stand in for hardware.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from homestead_twin.ems.service import EnergyManagerService
from homestead_twin.ingest.service import IngestService
from homestead_twin.models.energy import EnergyStateSnapshot, PowerLoadProfile
from homestead_twin.models.registry import Asset, Point, PointBinding
from homestead_twin.models.telemetry import CurrentState, IngestDeadLetter, TelemetrySample
from homestead_twin.mqtt import InMemoryBus
from simulator.site import SimulatedSite


@pytest.fixture()
def platform(session_factory, db_session, settings):
    """Registry + load schedule loaded, ingest wired to an in-process broker."""
    from homestead_twin.ems.loader import load_schedule
    from homestead_twin.registry.loader import load_package

    load_package(db_session)
    load_schedule(db_session)
    db_session.commit()

    bus = InMemoryBus()
    ingest = IngestService(session_factory, bus, settings, auto_sweep=False)
    ingest.start()
    site = SimulatedSite(bus, settings)
    return {"bus": bus, "ingest": ingest, "site": site, "session": db_session}


def test_registry_and_load_schedule_load_together(platform):
    session = platform["session"]
    assert session.scalar(select(func.count()).select_from(Asset)) == 90
    assert session.scalar(select(func.count()).select_from(PointBinding)) == 245
    assert session.scalar(select(func.count()).select_from(PowerLoadProfile)) == 12


def test_simulated_telemetry_reaches_current_state(platform):
    """Simulator -> MQTT -> ingest -> registry-resolved current state."""
    site, ingest, session = platform["site"], platform["ingest"], platform["session"]

    site.start()
    for _ in range(60):
        site.step(dt_s=10)

    stats = ingest.stats()["counters"]
    assert stats["messages_received"] > 0
    assert stats["messages_written"] > 0

    session.expire_all()
    written = session.scalar(select(func.count()).select_from(CurrentState))
    assert written > 50, f"expected many points populated, got {written}"

    # Every message the simulator published must resolve to a registry point.
    # An unresolved topic means the simulator and the register disagree about
    # identity, which is exactly what this rig exists to catch.
    unresolved = stats["unresolved_topics"]
    assert unresolved == 0, f"{unresolved} simulated topics did not resolve against the registry"


#: Dead-letter categories traced to gaps in the v0.3 design package rather than
#: to platform defects. Both are recorded in docs/integration-findings.md. The
#: test below fails on any *new* category, so this list cannot quietly grow.
KNOWN_PACKAGE_GAPS = {
    # Loads, panels, racks, safety sensors and alarm outputs carry no
    # availability_state point, so a device on one of those classes going
    # silent is not representable.
    "no_availability_point",
    # command_last_result has no enum value meaning "no command issued yet".
    "enum_violation",
}


def test_dead_letters_are_only_known_package_gaps(platform):
    """Ingest failures must stay traceable to recorded design gaps."""
    site, session = platform["site"], platform["session"]

    site.start()
    for _ in range(30):
        site.step(dt_s=10)

    session.expire_all()
    letters = session.execute(select(IngestDeadLetter)).scalars().all()
    categories = {letter.reason.split(":")[0] for letter in letters}
    unexpected = categories - KNOWN_PACKAGE_GAPS
    assert not unexpected, (
        f"new dead-letter categories appeared: {sorted(unexpected)}. "
        "Either the platform regressed or the design package changed."
    )


def test_battery_soc_is_ingested_with_good_quality(platform):
    site, session = platform["site"], platform["session"]
    site.start()
    for _ in range(30):
        site.step(dt_s=10)

    session.expire_all()
    soc = session.get(CurrentState, "energy.battery_bank.power_container.01/soc_pct")
    assert soc is not None, "battery SOC never arrived"
    assert soc.quality == "good"
    assert soc.unit == "%"
    assert 0.0 <= soc.value_numeric <= 100.0
    assert soc.source is not None


def test_historian_records_samples(platform):
    site, session = platform["site"], platform["session"]
    site.start()
    for _ in range(30):
        site.step(dt_s=10)

    session.expire_all()
    samples = session.scalar(select(func.count()).select_from(TelemetrySample))
    assert samples > 0


def test_ems_evaluates_against_simulated_telemetry(platform, session_factory, settings):
    """SDD 49 item 8: the EMS state machine driven by simulated MQTT telemetry.

    The EMS is ticked at *simulated* time. The simulator runs on its own clock
    (a June solstice day), so evaluating at wall-clock time would age every
    reading past its staleness window and the EMS would correctly -- but
    uselessly -- report that nothing is observable.
    """
    from homestead_twin.ems import RecordingCommandPort

    site, session, bus = platform["site"], platform["session"], platform["bus"]

    site.start()
    for _ in range(120):  # 20 simulated minutes
        site.step(dt_s=10)

    # The shared fixture disables the EMS so other tests get no background
    # evaluation; this test is specifically about it.
    ems_settings = settings.model_copy(update={"ems_enabled": True})

    # Capture dispatch instead of actuating -- nothing in this test may command.
    port = RecordingCommandPort()
    ems = EnergyManagerService(session_factory, bus, ems_settings, command_port=port)
    ems.tick(now=site.clock.now())

    session.expire_all()
    snapshot = session.get(EnergyStateSnapshot, 1)
    assert snapshot is not None, "EMS produced no state snapshot"
    assert snapshot.state, "EMS state is empty"
    assert snapshot.last_evaluated_at is not None
    assert snapshot.inputs, "EMS recorded no inputs"
    assert snapshot.derived, "EMS derived no values"

    # The point of this test: the EMS must actually see the simulated site, not
    # merely run and declare everything unobservable.
    assert snapshot.inputs.get("observable") is True, (
        "EMS could not observe the simulated site; "
        f"invalid required inputs: {snapshot.inputs.get('invalid_required')}"
    )
    assert snapshot.data_quality != "bad", f"EMS data quality is {snapshot.data_quality}"

    # And it must have derived real numbers from that telemetry.
    values = snapshot.derived.get("values", {})
    soc = values.get("reserve_pct") or values.get("usable_reserve_kwh")
    assert soc and soc.get("valid") is True, f"EMS derived no valid reserve: {soc}"


def test_no_physical_control_is_dispatched_by_default(platform, session_factory, settings):
    """The platform must not actuate anything while control is disabled."""
    from homestead_twin.models.commands import Command

    site, session, bus = platform["site"], platform["session"], platform["bus"]
    assert settings.allow_physical_control is False

    site.start()
    for _ in range(120):
        site.step(dt_s=10)

    ems = EnergyManagerService(session_factory, bus, settings)
    ems.tick()

    session.expire_all()
    dispatched = (
        session.execute(select(Command).where(Command.state.in_(("dispatched", "acknowledged", "succeeded"))))
        .scalars()
        .all()
    )
    assert dispatched == [], f"commands reached equipment with control disabled: {dispatched}"

    # And the simulator confirms it received nothing.
    assert site.stats.commands_received == 0


def test_comms_loss_marks_points_stale(platform):
    """A gateway going quiet must read as stale, never as a healthy value."""
    site, ingest, session = platform["site"], platform["ingest"], platform["session"]

    site.start()
    for _ in range(30):
        site.step(dt_s=10)
    session.expire_all()
    before = session.get(CurrentState, "energy.battery_bank.power_container.01/soc_pct")
    assert before.quality == "good"

    # Silence the power gateway (battery, BMS, inverters), then sweep past the timeout.
    site.lose_comms("power")
    for _ in range(30):
        site.step(dt_s=10)
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    ingest.sweep_stale(now=later)

    session.expire_all()
    after = session.get(CurrentState, "energy.battery_bank.power_container.01/soc_pct")
    assert after.quality == "stale", "a silent gateway still reads as good data"


def test_every_simulated_point_exists_in_the_registry(platform):
    """The simulator must not invent identity the design package does not define."""
    site, session = platform["site"], platform["session"]
    registered = {row[0] for row in session.execute(select(Point.point_id))}
    simulated = set(site.point_ids())
    unknown = simulated - registered
    assert not unknown, f"simulator publishes points absent from the registry: {sorted(unknown)[:10]}"
