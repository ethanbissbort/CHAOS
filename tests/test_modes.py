"""Operating modes: precedence, logging and the emergency latch (SDD section 11)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from homestead_twin.commands.modes import (
    DEFAULT_MODE,
    MODES,
    RESTRICTIVENESS,
    EmergencyLatchedError,
    ModeAuthorizationError,
    ModeError,
    ModeManager,
    UnknownModeError,
    more_restrictive,
)
from homestead_twin.models.commands import AuditLogEntry, ModeTransition, OperatingMode
from homestead_twin.models.registry import Asset, AssetClass

SITE_ID = "site.site.primary.01"
ASSET_ID = "water.pump.orchard.01"
NOW = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.timezone.utc)


@dataclass(frozen=True)
class Actor:
    name: str
    role: str = "operator"
    kind: str = "human"


OPERATOR = Actor("test.operator")
MAINTAINER = Actor("jo.maintainer", role="maintainer")
VIEWER = Actor("nosy.viewer", role="viewer")
ROBOT = Actor("rules.energy_manager", role="administrator", kind="service")


@pytest.fixture()
def modes(db_session):
    db_session.add(AssetClass(name="pump", allowed_domains=["water"]))
    db_session.add(
        Asset(
            asset_id=ASSET_ID,
            domain="water",
            asset_class="pump",
            name="Orchard transfer pump",
            status="active",
        )
    )
    db_session.commit()
    return ModeManager(db_session, site_id=SITE_ID)


def audit_rows(session, action=None):
    stmt = select(AuditLogEntry)
    if action:
        stmt = stmt.where(AuditLogEntry.action == action)
    return list(session.execute(stmt).scalars())


def transitions(session):
    return list(session.execute(select(ModeTransition)).scalars())


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_unset_scopes_default_to_automatic(modes):
    assert modes.resolve("site", SITE_ID) == DEFAULT_MODE
    assert modes.resolve("domain", "water") == DEFAULT_MODE
    assert modes.effective_mode(ASSET_ID) == DEFAULT_MODE


def test_mode_set_covers_the_sdd_section_11_vocabulary():
    assert set(MODES) == {
        "off",
        "manual",
        "automatic",
        "scheduled",
        "maintenance",
        "degraded",
        "emergency",
    }
    assert set(RESTRICTIVENESS) == set(MODES)


def test_unknown_mode_and_scope_are_refused(modes):
    with pytest.raises(UnknownModeError):
        modes.set_mode("asset", ASSET_ID, "party", OPERATOR, "no such mode")
    with pytest.raises(UnknownModeError):
        modes.set_mode("galaxy", ASSET_ID, "off", OPERATOR, "no such scope")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_set_mode_writes_row_transition_and_audit(modes, db_session):
    row = modes.set_mode(
        "asset", ASSET_ID, "maintenance", MAINTAINER, "annual impeller service", now=NOW
    )

    assert row.mode == "maintenance"
    assert row.previous_mode == DEFAULT_MODE
    assert row.changed_by == "jo.maintainer"
    assert row.auto_clear_allowed is True

    transition = transitions(db_session)[0]
    assert (transition.from_mode, transition.to_mode) == (DEFAULT_MODE, "maintenance")
    assert transition.reason == "annual impeller service"

    audit = audit_rows(db_session, "mode.set")[0]
    assert (audit.actor, audit.outcome, audit.target_id) == ("jo.maintainer", "changed", ASSET_ID)
    assert audit.detail["to_mode"] == "maintenance"


def test_mode_change_requires_a_reason_and_the_refusal_is_audited(modes, db_session):
    with pytest.raises(ModeError):
        modes.set_mode("asset", ASSET_ID, "off", OPERATOR, "   ", now=NOW)

    audit = audit_rows(db_session, "mode.set")[0]
    assert audit.outcome == "refused"
    assert audit.detail["refusal"] == "missing_reason"
    assert modes.resolve("asset", ASSET_ID) == DEFAULT_MODE


def test_viewer_cannot_change_a_mode(modes, db_session):
    with pytest.raises(ModeAuthorizationError):
        modes.set_mode("asset", ASSET_ID, "off", VIEWER, "curiosity", now=NOW)

    assert audit_rows(db_session, "mode.set")[0].detail["refusal"] == "insufficient_role"
    assert transitions(db_session) == []


def test_repeating_the_same_mode_logs_but_does_not_transition(modes, db_session):
    modes.set_mode("asset", ASSET_ID, "manual", OPERATOR, "local work", now=NOW)
    modes.set_mode("asset", ASSET_ID, "manual", OPERATOR, "still local work", now=NOW)

    assert len(transitions(db_session)) == 1
    assert [a.outcome for a in audit_rows(db_session, "mode.set")] == ["changed", "unchanged"]


# ---------------------------------------------------------------------------
# The emergency latch
# ---------------------------------------------------------------------------


def test_emergency_is_stored_as_latching(modes, db_session):
    row = modes.set_mode(
        "site",
        SITE_ID,
        "emergency",
        OPERATOR,
        "battery over-temperature",
        expires_at=NOW + dt.timedelta(hours=1),
        now=NOW,
    )

    assert row.mode == "emergency"
    assert row.auto_clear_allowed is False
    # A latch with a timer is not a latch.
    assert row.expires_at is None


def test_emergency_does_not_clear_itself_when_time_passes(modes, db_session):
    modes.set_mode("site", SITE_ID, "emergency", OPERATOR, "battery over-temperature", now=NOW)

    later = NOW + dt.timedelta(days=30)
    assert modes.resolve("site", SITE_ID, later) == "emergency"
    assert modes.expire_modes(later) == []
    assert modes.resolve("site", SITE_ID, later) == "emergency"


def test_clearing_emergency_without_condition_clear_is_refused_and_audited(modes, db_session):
    modes.set_mode("site", SITE_ID, "emergency", OPERATOR, "battery over-temperature", now=NOW)

    with pytest.raises(EmergencyLatchedError):
        modes.set_mode("site", SITE_ID, "automatic", OPERATOR, "looks fine now", now=NOW)

    assert modes.resolve("site", SITE_ID) == "emergency"
    refusals = [a for a in audit_rows(db_session, "mode.set") if a.outcome == "refused"]
    assert refusals[-1].detail["refusal"] == "condition_not_clear"


def test_a_service_may_never_clear_an_emergency(modes, db_session):
    modes.set_mode("site", SITE_ID, "emergency", OPERATOR, "battery over-temperature", now=NOW)

    with pytest.raises(EmergencyLatchedError):
        modes.set_mode(
            "site", SITE_ID, "automatic", ROBOT, "telemetry recovered", condition_clear=True, now=NOW
        )

    assert modes.resolve("site", SITE_ID) == "emergency"
    assert audit_rows(db_session, "mode.set")[-1].detail["refusal"] == "non_human_actor"


def test_a_viewer_may_never_clear_an_emergency(modes, db_session):
    modes.set_mode("site", SITE_ID, "emergency", OPERATOR, "battery over-temperature", now=NOW)

    with pytest.raises(ModeAuthorizationError):
        modes.set_mode(
            "site", SITE_ID, "automatic", VIEWER, "looks fine", condition_clear=True, now=NOW
        )

    assert modes.resolve("site", SITE_ID) == "emergency"


def test_operator_clears_emergency_by_asserting_the_condition_is_gone(modes, db_session):
    modes.set_mode("site", SITE_ID, "emergency", OPERATOR, "battery over-temperature", now=NOW)

    later = NOW + dt.timedelta(minutes=45)
    row = modes.set_mode(
        "site",
        SITE_ID,
        "automatic",
        OPERATOR,
        "cell temperatures back within limits, BMS alarm reset",
        condition_clear=True,
        now=later,
    )

    assert row.mode == "automatic"
    assert row.previous_mode == "emergency"
    assert row.auto_clear_allowed is True

    clear = transitions(db_session)[-1]
    assert (clear.from_mode, clear.to_mode) == ("emergency", "automatic")
    assert audit_rows(db_session, "mode.set")[-1].detail["condition_clear"] is True


def test_emergency_may_be_restated_while_latched(modes):
    modes.set_mode("site", SITE_ID, "emergency", OPERATOR, "battery over-temperature", now=NOW)
    row = modes.set_mode("site", SITE_ID, "emergency", OPERATOR, "still hot", now=NOW)

    assert row.mode == "emergency"


# ---------------------------------------------------------------------------
# Temporary modes
# ---------------------------------------------------------------------------


def test_non_latching_modes_clear_when_their_expiry_passes(modes, db_session):
    modes.set_mode(
        "asset",
        ASSET_ID,
        "maintenance",
        MAINTAINER,
        "seal replacement",
        expires_at=NOW + dt.timedelta(hours=4),
        now=NOW,
    )

    during = NOW + dt.timedelta(hours=1)
    after = NOW + dt.timedelta(hours=5)
    assert modes.resolve("asset", ASSET_ID, during) == "maintenance"
    # Reads are safe even before the sweep runs.
    assert modes.resolve("asset", ASSET_ID, after) == DEFAULT_MODE

    cleared = modes.expire_modes(after)
    assert [row.scope_id for row in cleared] == [ASSET_ID]
    assert db_session.get(OperatingMode, cleared[0].id).mode == DEFAULT_MODE

    expiry_audit = audit_rows(db_session, "mode.expire")[0]
    assert expiry_audit.detail == {"from_mode": "maintenance", "to_mode": DEFAULT_MODE}
    assert transitions(db_session)[-1].to_mode == DEFAULT_MODE


def test_expiry_sweep_is_idempotent(modes):
    modes.set_mode(
        "asset",
        ASSET_ID,
        "degraded",
        OPERATOR,
        "flow sensor suspect",
        expires_at=NOW + dt.timedelta(hours=1),
        now=NOW,
    )
    after = NOW + dt.timedelta(hours=2)

    assert len(modes.expire_modes(after)) == 1
    assert modes.expire_modes(after) == []


# ---------------------------------------------------------------------------
# effective_mode precedence
# ---------------------------------------------------------------------------


def test_restrictiveness_ordering_is_documented_and_total():
    assert more_restrictive("automatic", "scheduled") == "scheduled"
    assert more_restrictive("scheduled", "manual") == "manual"
    assert more_restrictive("manual", "degraded") == "degraded"
    assert more_restrictive("degraded", "maintenance") == "maintenance"
    assert more_restrictive("maintenance", "off") == "off"
    assert more_restrictive("off", "emergency") == "emergency"
    assert more_restrictive() == DEFAULT_MODE


def test_site_emergency_overrides_a_relaxed_asset(modes):
    modes.set_mode("asset", ASSET_ID, "automatic", OPERATOR, "normal", now=NOW)
    modes.set_mode("site", SITE_ID, "emergency", OPERATOR, "grid and PV both lost", now=NOW)

    chain = modes.mode_chain(ASSET_ID)
    assert chain.as_dict() == {"site": "emergency", "domain": "automatic", "asset": "automatic"}
    assert modes.effective_mode(ASSET_ID) == "emergency"


def test_asset_maintenance_overrides_a_relaxed_site(modes):
    modes.set_mode("asset", ASSET_ID, "maintenance", MAINTAINER, "impeller", now=NOW)

    assert modes.effective_mode(ASSET_ID) == "maintenance"


def test_domain_mode_applies_to_every_asset_in_the_domain(modes):
    modes.set_mode("domain", "water", "manual", OPERATOR, "commissioning the water plant", now=NOW)

    chain = modes.mode_chain(ASSET_ID)
    assert chain.domain_id == "water"
    assert chain.domain == "manual"
    assert modes.effective_mode(ASSET_ID) == "manual"


def test_most_restrictive_wins_across_all_three_scopes(modes):
    modes.set_mode("site", SITE_ID, "scheduled", OPERATOR, "seasonal schedule", now=NOW)
    modes.set_mode("domain", "water", "manual", OPERATOR, "manual water ops", now=NOW)
    modes.set_mode("asset", ASSET_ID, "off", OPERATOR, "pump isolated", now=NOW)

    assert modes.effective_mode(ASSET_ID) == "off"

    modes.set_mode("domain", "water", "emergency", OPERATOR, "cistern rupture", now=NOW)
    assert modes.effective_mode(ASSET_ID) == "emergency"


def test_unregistered_asset_falls_back_to_its_domain_prefix(modes):
    modes.set_mode("domain", "energy", "degraded", OPERATOR, "inverter fault", now=NOW)

    assert modes.effective_mode("energy.inverter.power_container.09") == "degraded"


def test_list_modes_reports_stored_and_resolved_state(modes):
    modes.set_mode("site", SITE_ID, "emergency", OPERATOR, "fire", now=NOW)
    modes.set_mode(
        "asset",
        ASSET_ID,
        "manual",
        OPERATOR,
        "local work",
        expires_at=NOW + dt.timedelta(hours=1),
        now=NOW,
    )

    listed = {row["scope_id"]: row for row in modes.list_modes(NOW + dt.timedelta(hours=2))}

    assert listed[SITE_ID]["mode"] == "emergency"
    assert listed[SITE_ID]["latched"] is True
    assert listed[ASSET_ID]["stored_mode"] == "manual"
    assert listed[ASSET_ID]["mode"] == DEFAULT_MODE
