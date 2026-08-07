"""Annunciator panel tests (SDD 14, 17.3).

The property that matters most here is negative: a dark tile must be a
trustworthy claim that the condition is normal. A definition that cannot
evaluate must not present as dark.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from homestead_twin.api.routers.annunciator import BAY_ORDER, LEGENDS, engrave
from homestead_twin.models.alarms import Alarm, AlarmDefinition
from homestead_twin.models.registry import Point

UI = "src/homestead_twin/web"


# --------------------------------------------------------------- legends --


def test_hand_cut_legend_is_used_when_one_exists():
    assert engrave("Source transfer did not complete", key="transfer_failed") == [
        "SOURCE XFER",
        "FAILED",
    ]


def test_every_shipped_alarm_has_a_hand_cut_legend():
    """A generated legend is a fallback, not the shipping standard."""
    import yaml

    with open("data/alarm_definitions.yaml", encoding="utf-8") as handle:
        keys = {a["alarm_key"] for a in yaml.safe_load(handle)["alarms"]}
    missing = keys - set(LEGENDS)
    assert not missing, f"alarms without an engraved legend: {sorted(missing)}"


def test_legends_fit_the_window():
    for key, lines in LEGENDS.items():
        assert 1 <= len(lines) <= 3, f"{key}: {len(lines)} lines"
        for line in lines:
            assert len(line) <= 12, f"{key}: '{line}' is {len(line)} chars"
            assert line == line.upper(), f"{key}: '{line}' is not uppercase"


@pytest.mark.parametrize(
    ("name", "must_contain"),
    [
        ("Source transfer did not complete", "COMPLETE"),
        ("Power container fluid detected", "DETECTED"),
        ("Some entirely new condition has failed", "FAILED"),
    ],
)
def test_generated_fallback_keeps_the_operative_word(name, must_contain):
    """The meaning of an alarm name usually sits in its last word.

    Dropping it produces legends like "SOURCE TRANSFER DID NOT", which is not a
    shorter legend but a wrong one.
    """
    assert must_contain in " ".join(engrave(name))


def test_generated_fallback_respects_the_window():
    lines = engrave("Power container cooling failed and temperature is rising fast")
    assert len(lines) <= 3
    for line in lines:
        assert len(line) <= 12
        assert not line.endswith("-")


def test_engrave_survives_an_empty_name():
    assert engrave("") == ["(UNNAMED)"]


# ----------------------------------------------------------------- panel --


@pytest.fixture()
def seeded_registry_and_alarms(db_session, settings):
    """The real v0.3 package: 90 assets and the full 40-alarm definition set.

    The panel is tested against the actual design package rather than a
    convenient fixture, because the properties under test are about how the
    package and the platform disagree.
    """
    load_package = pytest.importorskip("homestead_twin.registry.loader").load_package
    sync_definitions = pytest.importorskip("homestead_twin.alarms.definitions").sync_definitions

    load_package(db_session)
    sync_definitions(db_session, settings=settings, strict=False)
    db_session.commit()
    return db_session


@pytest.fixture()
def panel(client, seeded_registry_and_alarms):
    def _fetch(**params):
        response = client.get("/api/v1/annunciator", params=params)
        assert response.status_code == 200
        return response.json()

    return _fetch


def test_every_definition_gets_a_tile(panel, db_session):
    from sqlalchemy import func

    data = panel()
    total_defs = db_session.scalar(select(func.count()).select_from(AlarmDefinition))
    tiles = [t for bay in data["bays"] for t in bay["tiles"]]
    assert len(tiles) == data["summary"]["total"]
    assert data["summary"]["total"] == total_defs


def test_tiles_hold_a_stable_position_between_polls(panel):
    first = [t["alarm_key"] for bay in panel()["bays"] for t in bay["tiles"]]
    second = [t["alarm_key"] for bay in panel()["bays"] for t in bay["tiles"]]
    # An engraved window does not move. If ordering drifted between polls the
    # operator's muscle memory would be worse than useless.
    assert first == second


def test_bays_follow_the_declared_wall_order(panel):
    order = [bay["domain"] for bay in panel()["bays"]]
    declared = [domain for domain, _ in BAY_ORDER if domain in order]
    assert order[: len(declared)] == declared


def test_unreachable_trigger_point_reads_out_of_service_not_normal(panel, db_session):
    """The finding that motivated this panel (docs/integration-findings.md F-007)."""
    data = panel()
    tiles = {t["alarm_key"]: t for bay in data["bays"] for t in bay["tiles"]}

    imbalance = tiles.get("battery_cell_imbalance")
    assert imbalance is not None
    assert imbalance["state"] == "out_of_service"
    assert imbalance["serviceable"] is False
    assert "cell_voltage_delta_mv" in imbalance["service_note"]
    # The critical property: it must never be reported as a normal, dark tile.
    assert imbalance["state"] != "normal"


def test_out_of_service_tiles_are_counted_separately_from_normal(panel):
    summary = panel()["summary"]
    assert summary["out_of_service"] > 0
    assert summary["normal"] + summary["out_of_service"] <= summary["total"]


def test_disabled_definition_is_out_of_service(panel, db_session):
    definition = db_session.scalars(select(AlarmDefinition)).first()
    definition.enabled = False
    db_session.commit()

    tiles = {t["alarm_key"]: t for bay in panel()["bays"] for t in bay["tiles"]}
    tile = tiles[definition.alarm_key]
    assert tile["state"] == "out_of_service"
    assert "disabled" in tile["service_note"].lower()


def test_a_serviceable_definition_with_no_alarm_is_normal(panel, db_session):
    tiles = {t["alarm_key"]: t for bay in panel()["bays"] for t in bay["tiles"]}
    normal = [t for t in tiles.values() if t["state"] == "normal"]
    assert normal, "expected at least one genuinely normal tile"
    for tile in normal:
        assert tile["serviceable"] is True
        assert tile["alarm_id"] is None


# ---------------------------------------------------------------- states --


def _raise(db_session, definition_key, state, **kwargs):
    definition = db_session.scalars(
        select(AlarmDefinition).where(AlarmDefinition.alarm_key == definition_key)
    ).one()
    now = dt.datetime.now(dt.UTC)
    alarm = Alarm(
        alarm_key=definition.alarm_key,
        asset_id=definition.asset_id,
        severity=definition.severity,
        state=state,
        message=f"{definition.name} (test)",
        detected_at=now,
        activated_at=now if state != "detected" else None,
        **kwargs,
    )
    db_session.add(alarm)
    db_session.commit()
    return alarm


def _serviceable_key(db_session):
    """An alarm_key whose trigger point actually exists, so it can light."""
    points = set(db_session.scalars(select(Point.point_id)))
    for definition in db_session.scalars(select(AlarmDefinition)):
        if definition.asset_id and definition.point_name:
            if f"{definition.asset_id}/{definition.point_name}" in points:
                return definition.alarm_key
    pytest.skip("no serviceable definition in the seeded fixture")


def test_active_alarm_lights_the_tile_and_sounds_the_horn(panel, db_session):
    key = _serviceable_key(db_session)
    _raise(db_session, key, "active")

    data = panel()
    tiles = {t["alarm_key"]: t for bay in data["bays"] for t in bay["tiles"]}
    assert tiles[key]["state"] == "alarm"
    assert data["summary"]["horn"] is True


def test_acknowledged_alarm_stays_lit_but_silences_the_horn(panel, db_session):
    key = _serviceable_key(db_session)
    _raise(db_session, key, "acknowledged", acknowledged_by="test.operator")

    data = panel()
    tiles = {t["alarm_key"]: t for bay in data["bays"] for t in bay["tiles"]}
    assert tiles[key]["state"] == "acknowledged"
    assert data["summary"]["horn"] is False


def test_cleared_alarm_produces_ringback_not_darkness(panel, db_session):
    key = _serviceable_key(db_session)
    _raise(db_session, key, "cleared")

    data = panel()
    tiles = {t["alarm_key"]: t for bay in data["bays"] for t in bay["tiles"]}
    # The condition is gone but the operator has not closed it out. Going
    # straight to dark would lose the fact that it happened at all.
    assert tiles[key]["state"] == "ringback"
    assert data["summary"]["ringback_tone"] is True


def test_suppressed_alarm_reads_inhibited_rather_than_alarm(panel, db_session):
    key = _serviceable_key(db_session)
    _raise(db_session, key, "active", suppressed=True, suppression_reason="maintenance:test")

    data = panel()
    tiles = {t["alarm_key"]: t for bay in data["bays"] for t in bay["tiles"]}
    assert tiles[key]["state"] == "inhibited"
    assert data["summary"]["horn"] is False, "an inhibited alarm must not sound the horn"


def test_tile_states_vocabulary_is_returned_for_the_ui(panel):
    vocabulary = panel()["tile_states"]
    for required in ("normal", "alarm", "acknowledged", "ringback", "inhibited", "out_of_service"):
        assert required in vocabulary
        assert vocabulary[required].strip()


# -------------------------------------------------------------------- ui --


def test_annunciator_ui_files_exist_and_are_self_contained():
    import re
    from pathlib import Path

    root = Path(UI)
    for name in ("annunciator.html", "annunciator.css", "annunciator.js"):
        path = root / name
        assert path.is_file(), f"{name} missing"
        text = path.read_text(encoding="utf-8")
        # The panel must work with the internet down; that is the condition it
        # matters most in.
        assert not re.search(r"https?://(?!www\.w3\.org)", text), f"{name} references an external host"


def test_main_console_can_open_the_panel():
    from pathlib import Path

    index = Path(UI, "index.html").read_text(encoding="utf-8")
    app = Path(UI, "app.js").read_text(encoding="utf-8")
    assert "open-annunciator" in index
    assert "annunciator.html" in app
    # A second click must focus the existing window rather than opening a rival
    # panel with its own idea of the horn.
    assert "closed" in app and "focus()" in app
