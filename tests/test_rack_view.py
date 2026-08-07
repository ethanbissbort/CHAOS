"""Rack elevation tests (SDD 17.2, work-queue item 49.4).

Three properties carry most of the weight here.

**The U-grid is complete and exclusive.** Every unit 1..42 is accounted for
exactly once, and no unit is claimed twice. The source document asserts
non-overlap in prose; these tests assert it in arithmetic, and one of them
feeds the endpoint a deliberately overlapping layout to prove the check is
real rather than decorative.

**A device with no telemetry never reads healthy.** ``no_data`` (registered and
never reported) and ``stale`` (reported and stopped) are different facts, and
the second is the one that means an instrument or a link has failed. Both are
distinct from ``ok``. The vocabulary is the platform's, imported from
``overview`` so the two screens cannot drift apart.

**The proposal status survives every path a reader can take.** The document is
``not_ratified`` with ``authority: none_until_owner_review``; the payload says
so, and the drawing carries it in a watermark and a title block so it survives
a screenshot and an export.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
import yaml

from homestead_twin.api.routers.overview import STATUS_EXPLANATIONS
from homestead_twin.api.routers.rack import CATEGORIES, load_layout
from homestead_twin.models.telemetry import CurrentState

UI = "src/homestead_twin/web"
LAYOUT_PATH = Path("data/rack_layout.yaml")


# --------------------------------------------------------------- fixtures --


@pytest.fixture()
def rack(client, loaded_registry):
    """The endpoint against the real v0.3 package, not a convenient fixture.

    The properties under test are about how the layout proposal and the
    platform disagree, so a synthetic layout would test nothing.
    """

    def _fetch():
        response = client.get("/api/v1/rack")
        assert response.status_code == 200, response.text
        return response.json()

    return _fetch


@pytest.fixture()
def layout_document():
    return yaml.safe_load(LAYOUT_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------- the U-grid --


def test_every_rack_unit_is_accounted_for_exactly_once(rack):
    data = rack()
    unit_count = data["rack"]["rack_unit_count"]
    assert unit_count == 42

    numbers = [unit["unit"] for unit in data["units"]]
    assert numbers == list(range(1, unit_count + 1)), "the grid must be complete and in order"
    assert data["summary"]["units_accounted_for"] == unit_count
    for unit in data["units"]:
        assert unit["state"] in ("occupied", "free")


def test_no_unit_is_claimed_by_two_devices(rack):
    """The document claims non-overlap. This is that claim, checked."""
    data = rack()
    doubled = [unit for unit in data["units"] if unit["conflicting_asset_ids"]]
    assert not doubled, f"units claimed twice: {[u['unit'] for u in doubled]}"
    assert not [f for f in data["findings"] if f["code"] == "RACK-OVERLAP"]


def test_occupied_and_free_partition_the_rack(rack):
    data = rack()
    occupied = {u["unit"] for u in data["units"] if u["state"] == "occupied"}
    free = {u["unit"] for u in data["units"] if u["state"] == "free"}
    assert occupied & free == set()
    assert occupied | free == set(range(1, 43))
    assert data["summary"]["occupied_units"] == len(occupied)
    assert data["summary"]["free_units"] == len(free)


def test_each_device_owns_exactly_the_units_it_claims(rack):
    data = rack()
    for device in data["devices"]:
        start = device["rack_unit_start"]
        height = device["rack_unit_height"]
        assert device["rack_unit_end"] == start + height - 1
        owned = [u for u in data["units"] if u["asset_id"] == device["asset_id"]]
        assert [u["unit"] for u in owned] == list(range(start, start + height))
        bases = [u for u in owned if u["is_device_base"]]
        assert len(bases) == 1 and bases[0]["unit"] == start


def test_free_blocks_cover_every_free_unit_and_call_out_the_contiguous_one(rack):
    data = rack()
    free = {u["unit"] for u in data["units"] if u["state"] == "free"}
    covered: set[int] = set()
    for block in data["free_blocks"]:
        assert block["size"] == block["end"] - block["start"] + 1
        covered |= set(range(block["start"], block["end"] + 1))
    assert covered == free

    largest = data["summary"]["largest_free_block"]
    # The layout deliberately keeps one contiguous block for a future chassis.
    assert largest["start"] == 18 and largest["end"] == 35 and largest["size"] == 18
    assert largest["largest"] is True
    assert "contiguous" in largest["label"].lower()


def test_numbering_direction_is_declared_not_assumed(rack):
    data = rack()
    assert data["rack"]["rack_unit_numbering"] == "bottom_to_top_unit_1_is_lowest"
    assert "unit 1 is the lowest" in data["rack"]["numbering_note"].lower()


def test_an_overlapping_layout_is_reported_rather_than_drawn(client, loaded_registry, tmp_path, settings):
    """Feed the endpoint a broken document and require it to say so."""
    document = yaml.safe_load(LAYOUT_PATH.read_text(encoding="utf-8"))
    racked = [p for p in document["placements"] if p["mount_style"] == "rack_unit"]
    # Drop the second device on top of the first.
    racked[1]["rack_unit_start"] = racked[0]["rack_unit_start"]
    racked[1]["rack_unit_height"] = racked[0]["rack_unit_height"]
    (tmp_path / "rack_layout.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")

    client.app.state.settings = settings.model_copy(update={"data_dir": tmp_path})
    data = client.get("/api/v1/rack").json()

    codes = {f["code"]: f for f in data["findings"]}
    assert "RACK-OVERLAP" in codes
    assert codes["RACK-OVERLAP"]["severity"] == "blocking"
    assert any(u["conflicting_asset_ids"] for u in data["units"])
    # The declared allocation no longer matches the placements either, and the
    # grid served is the re-derived one.
    assert "RACK-ALLOCATION-DRIFT" in codes


def test_a_missing_layout_document_is_not_an_empty_rack(client, loaded_registry, tmp_path, settings):
    """An absent document must not render as 42 free units."""
    client.app.state.settings = settings.model_copy(update={"data_dir": tmp_path / "nowhere"})
    response = client.get("/api/v1/rack")
    assert response.status_code == 503
    assert "rack_layout.yaml" in response.json()["detail"]


# ------------------------------------------------------------- zero-U kit --


def test_zero_u_devices_are_present_and_kept_out_of_the_grid(rack, layout_document):
    data = rack()
    expected = {p["asset_id"] for p in layout_document["placements"] if p["mount_style"] != "rack_unit"}
    assert expected, "the fixture package should contain zero-U placements"

    returned = {d["asset_id"] for d in data["zero_u_devices"]}
    assert returned == expected
    assert data["summary"]["zero_u_device_count"] == len(expected)

    racked = {d["asset_id"] for d in data["devices"]}
    assert racked & returned == set(), "a zero-U device must never occupy a rack unit"
    for device in data["zero_u_devices"]:
        assert device["consumes_rack_units"] is False
        assert device["rack_unit_start"] is None
        assert device["rack_unit_height"] == 0
    assert not [u for u in data["units"] if u["asset_id"] in returned]


def test_every_placement_is_returned_somewhere(rack, layout_document):
    """Nothing in the document may be silently dropped."""
    data = rack()
    declared = {p["asset_id"] for p in layout_document["placements"]}
    returned = {d["asset_id"] for d in data["devices"]} | {d["asset_id"] for d in data["zero_u_devices"]}
    assert returned == declared


def test_excluded_assets_are_listed_with_their_reasons(rack, layout_document):
    data = rack()
    expected = {e["asset_id"] for e in layout_document["excluded_assets"]}
    assert {e["asset_id"] for e in data["excluded_assets"]} == expected
    for entry in data["excluded_assets"]:
        assert entry["reason"], "an exclusion without a reason is an omission"


# ---------------------------------------------------- proposal provenance --


def test_proposal_metadata_is_propagated(rack, layout_document):
    doc = rack()["document"]
    assert doc["document_status"] == layout_document["document_status"] == "proposal_for_review"
    assert doc["approval_status"] == layout_document["approval_status"] == "not_ratified"
    assert doc["authority"] == layout_document["authority"] == "none_until_owner_review"
    assert doc["review_required_before_use"] is True
    assert doc["ratified"] is False
    assert doc["usable_as_built"] is False
    assert doc["source_document"] == layout_document["source_document"]
    assert doc["merge_target"] == layout_document["merge_target"]


def test_the_banner_says_not_ratified_in_words(rack, layout_document):
    banner = rack()["document"]["banner"]
    assert "NOT RATIFIED" in banner["headline"]
    assert "proposal" in banner["detail"].lower()
    # The specific open fields, not a vague warning.
    assert banner["open_fields"] == layout_document["open_document_fields"]
    assert banner["open_field_count"] == len(layout_document["open_document_fields"])
    assert "final_owner_ratification" in banner["open_fields"]


def test_every_device_carries_its_own_provisional_status(rack):
    for device in rack()["devices"]:
        assert device["placement_status"] == "preliminary"
        assert device["data_status"] == "estimated"


def test_open_fields_are_carried_per_device_not_summarised_away(rack, layout_document):
    data = rack()
    by_id = {p["asset_id"]: p for p in layout_document["placements"]}
    for device in data["devices"] + data["zero_u_devices"]:
        assert device["open_fields"] == (by_id[device["asset_id"]].get("open_fields") or [])
    assert data["summary"]["open_field_total"] > 0


# ------------------------------------------------------- no_data vs stale --


def _first_point_id(db_session, asset_id):
    from sqlalchemy import select

    from homestead_twin.models.registry import Point

    point = db_session.scalars(select(Point).where(Point.asset_id == asset_id)).first()
    assert point is not None, f"{asset_id} has no points in the loaded package"
    return point


def _record(db_session, point, *, age_s: float, stale_after_s: int = 60):
    now = dt.datetime.now(dt.UTC)
    db_session.add(
        CurrentState(
            point_id=point.point_id,
            asset_id=point.asset_id,
            point_name=point.point_name,
            value_numeric=1.0,
            quality="good",
            ts=now - dt.timedelta(seconds=age_s),
            received_at=now - dt.timedelta(seconds=age_s),
            stale_after_s=stale_after_s,
        )
    )
    db_session.commit()


def _device(data, asset_id):
    for device in data["devices"] + data["zero_u_devices"]:
        if device["asset_id"] == asset_id:
            return device
    raise AssertionError(f"{asset_id} is not in the payload")


def test_a_device_with_no_telemetry_is_no_data_never_ok(rack):
    """Nothing has ever reported in this package, so nothing may read healthy."""
    data = rack()
    everything = data["devices"] + data["zero_u_devices"]
    assert everything
    for device in everything:
        assert device["health"]["status"] != "ok"
    assert data["summary"]["health_counts"].get("ok", 0) == 0
    assert data["summary"]["reporting_device_count"] == 0


def test_registered_but_silent_points_read_no_data_with_a_reason(rack):
    device = _device(rack(), "it.server.rack_01.r740xd_01")
    health = device["health"]
    assert health["status"] == "no_data"
    assert health["point_count"] > 0
    assert health["reporting_point_count"] == 0
    assert health["last_reported_at"] is None
    assert "none has ever reported" in health["reason"]
    # Planned hardware that has never reported is expected but not healthy.
    assert health["expected_to_report"] is False


def test_a_fresh_value_reads_ok(rack, db_session):
    asset_id = "it.server.rack_01.r740xd_01"
    _record(db_session, _first_point_id(db_session, asset_id), age_s=1)
    health = _device(rack(), asset_id)["health"]
    assert health["status"] == "ok"
    assert health["reporting_point_count"] == 1
    assert health["stale_point_count"] == 0


def test_a_device_that_reported_and_stopped_reads_stale_not_no_data(rack, db_session):
    """The distinction this project treats as load-bearing.

    ``no_data`` means the instrument was never heard from. ``stale`` means it
    was heard from and went quiet, which is an instrument or link failure, not
    an early-deployment state. Collapsing them would hide a real fault.
    """
    asset_id = "it.switch.rack_01.catalyst_2960x_01"
    _record(db_session, _first_point_id(db_session, asset_id), age_s=5000, stale_after_s=60)

    data = rack()
    stale_device = _device(data, asset_id)
    assert stale_device["health"]["status"] == "stale"
    assert stale_device["health"]["reporting_point_count"] == 1
    assert stale_device["health"]["stale_point_count"] == 1
    assert stale_device["health"]["last_reported_at"] is not None
    assert "stopped" in stale_device["health"]["reason"]

    # A neighbouring device with no state at all must stay `no_data`: the two
    # facts must not be reported by the same word.
    silent = _device(data, "it.switch.rack_01.arista_7050qx_01")
    assert silent["health"]["status"] == "no_data"
    assert silent["health"]["last_reported_at"] is None
    assert silent["health"]["status"] != stale_device["health"]["status"]


def test_the_status_vocabulary_is_the_platforms_not_a_parallel_one(rack):
    data = rack()
    assert data["status_vocabulary"] == STATUS_EXPLANATIONS
    seen = {d["health"]["status"] for d in data["devices"] + data["zero_u_devices"]}
    assert seen <= set(STATUS_EXPLANATIONS)


def test_active_alarms_are_joined_to_their_device(rack, db_session):
    from sqlalchemy import select

    from homestead_twin.alarms.definitions import sync_definitions
    from homestead_twin.models.alarms import Alarm, AlarmDefinition

    sync_definitions(db_session, strict=False)
    db_session.commit()
    definition = db_session.scalars(select(AlarmDefinition)).first()
    asset_id = "it.server.rack_01.r740xd_01"
    now = dt.datetime.now(dt.UTC)
    db_session.add(
        Alarm(
            alarm_key=definition.alarm_key,
            asset_id=asset_id,
            severity="critical",
            state="active",
            message="Synthetic alarm for the rack view test",
            detected_at=now,
            activated_at=now,
        )
    )
    db_session.commit()

    alarms = _device(rack(), asset_id)["alarms"]
    assert alarms["active_count"] == 1
    assert alarms["worst_severity"] == "critical"
    assert alarms["items"][0]["message"].startswith("Synthetic alarm")


# --------------------------------------------------------- power and ports --


def test_power_feeds_and_pdus_are_returned_whole(rack, layout_document):
    power = rack()["power"]
    assert {f["feed_id"] for f in power["feeds"]} == {f["feed_id"] for f in layout_document["power_feeds"]}
    assert {p["pdu_asset_id"] for p in power["pdus"]} == {p["pdu_asset_id"] for p in layout_document["pdus"]}
    for pdu in power["pdus"]:
        assert pdu["outlets_modelled"] == pdu["outlet_count"]
        assert pdu["used_outlets"] + pdu["spare_outlets"] == pdu["outlets_modelled"]
        # Spare outlets are listed, not omitted; a missing row would read as a
        # full PDU.
        assert len(pdu["outlets"]) == pdu["outlet_count"]


def test_an_unresolved_feed_is_reported_as_blocking(rack):
    data = rack()
    feed_b = next(f for f in data["power"]["feeds"] if f["feed_id"] == "B")
    assert feed_b["resolved"] is False
    assert feed_b["upstream_asset_id"] is None

    codes = {f["code"]: f for f in data["findings"]}
    assert codes["RACK-FEED-UNRESOLVED"]["severity"] == "blocking"
    # Every A_and_B claim in the document depends on that feed existing.
    assert "RACK-DUAL-FEED-UNREALISABLE" in codes


def test_each_devices_outlet_agrees_with_the_pdu_outlet_model(rack):
    data = rack()
    outlets = {
        (pdu["pdu_asset_id"], outlet["outlet"]): outlet["assigned_asset_id"]
        for pdu in data["power"]["pdus"]
        for outlet in pdu["outlets"]
    }
    for device in data["devices"] + data["zero_u_devices"]:
        power = device["power"]
        if power["pdu_is_modelled"] and power["pdu_outlet"]:
            key = (power["pdu_asset_id"], power["pdu_outlet"])
            assert key in outlets, f"{device['asset_id']} claims an outlet that does not exist"
            assert outlets[key] == device["asset_id"]


def test_switch_ports_are_returned_and_free_of_collisions(rack):
    plan = rack()["switch_port_plan"]
    assert plan["switches"], "the port plan should resolve at least one switch"
    assert plan["port_numbers_are_interface_names"] is False
    for entry in plan["switches"]:
        assert entry["duplicate_ports"] == {}, f"port collision on {entry['switch_asset_id']}"
        for row in entry["assignments"]:
            assert row["asset_id"]


def test_categories_cover_every_asset_class_in_the_layout(rack, layout_document):
    data = rack()
    classes = {p["asset_class"] for p in layout_document["placements"]}
    unmapped = classes - set(CATEGORIES)
    assert not unmapped, f"asset classes with no colour bucket: {sorted(unmapped)}"
    for device in data["devices"] + data["zero_u_devices"]:
        assert device["category"] in data["category_labels"]


def test_thermal_summary_reports_absence_rather_than_zero(rack):
    thermal = rack()["thermal"]
    assert thermal["available"] is False
    assert thermal["total_estimated_power_w"] is None
    assert thermal["heat_load_status"] == "not_calculated_pending_measured_load"
    assert thermal["required_before_use"]


def test_load_layout_is_cached_but_returns_the_document(tmp_path):
    document = {"document_type": "rack_layout", "rack": {"rack_unit_count": 12}}
    (tmp_path / "rack_layout.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    first = load_layout(tmp_path)
    second = load_layout(tmp_path)
    assert first == document
    assert first is second


# --------------------------------------------------------------------- ui --


def _rack_js() -> str:
    return Path(UI, "views/rack.js").read_text(encoding="utf-8")


def test_web_assets_exist_and_are_self_contained():
    """The panel must work with the internet down; that is when it matters."""
    for name in ("views/rack.js", "rack.css"):
        path = Path(UI, name)
        assert path.is_file(), f"{name} missing"
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"https?://(?!www\.w3\.org)", text), f"{name} references an external host"
        # Protocol-relative and bare-host forms slip past the check above.
        for host in ("//cdn", "unpkg", "jsdelivr", "googleapis", "cdnjs"):
            assert host not in text.lower(), f"{name} references {host}"
        assert "@import url(" not in text
        # An actual at-rule, not the word in a comment explaining its absence.
        assert not re.search(r"@font-face\s*\{", text), f"{name} would pull in a font"


def test_the_view_uses_the_existing_request_layer():
    text = _rack_js()
    assert "from '../api.js'" in text
    # A second HTTP layer would have its own idea of offline, identity and
    # timeouts; the console has exactly one.
    assert "fetch(" not in text
    assert "XMLHttpRequest" not in text


def test_the_view_loads_its_own_stylesheet():
    """index.html only loads styles.css, so the view must bring its own."""
    text = _rack_js()
    assert "rack.css" in text
    index = Path(UI, "index.html").read_text(encoding="utf-8")
    assert "rack.css" not in index, "index.html is owned by the shell, not by this view"


def test_the_route_and_sidebar_entry_exist():
    app = Path(UI, "app.js").read_text(encoding="utf-8")
    index = Path(UI, "index.html").read_text(encoding="utf-8")
    assert "/rack" in app and "view: 'rack'" in app
    assert 'data-nav="rack"' in index
    assert 'id="i-rack"' in index


def test_the_drawing_carries_the_proposal_status_into_a_screenshot():
    """A cropped screenshot of the elevation must still say what it is."""
    text = _rack_js()
    assert "PROPOSAL — NOT RATIFIED" in text, "the watermark text is missing"
    assert "drawWatermark" in text
    assert "PROPOSAL · NOT RATIFIED" in text, "the in-drawing title block is missing"
    # And an export must not launder the status out of the filename.
    assert "PROPOSAL-NOT-RATIFIED" in text


def test_status_is_never_carried_by_colour_alone():
    text = _rack_js()
    block = re.search(r"const SVG_STATUS = \{(.*?)\n\};", text, re.DOTALL)
    assert block, "the SVG status vocabulary is missing"
    entries = re.findall(r"(\w+):\s*\{\s*glyph:\s*'([^']+)',\s*word:\s*'([^']+)'", block.group(1))
    assert entries
    for key, glyph, word in entries:
        assert glyph.strip(), f"{key} has no glyph"
        assert word.strip(), f"{key} has no word"
    covered = {key for key, _, _ in entries}
    assert covered == set(STATUS_EXPLANATIONS), "every availability status needs a glyph and a word"


# ------------------------------------------------------------- contrast ----


def _relative_luminance(hex_colour: str) -> float:
    value = hex_colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _palette() -> tuple[str, dict[str, str]]:
    text = _rack_js()
    label = re.search(r"const DEVICE_LABEL = '(#[0-9A-Fa-f]{6})'", text)
    block = re.search(r"const CATEGORY_FILL = \{(.*?)\n\};", text, re.DOTALL)
    assert label and block, "the device palette could not be read from rack.js"
    fills = dict(re.findall(r"(\w+):\s*'(#[0-9A-Fa-f]{6})'", block.group(1)))
    assert fills
    return label.group(1), fills


def test_contrast_maths_matches_the_reference_values():
    """Guards the checker itself against a sign or gamma slip."""
    assert _contrast("#FFFFFF", "#000000") == pytest.approx(21.0, abs=0.01)
    assert _contrast("#777777", "#FFFFFF") == pytest.approx(4.48, abs=0.02)


def test_every_device_colour_carries_the_label_at_wcag_aa():
    label, fills = _palette()
    failures = {
        category: round(_contrast(label, fill), 2)
        for category, fill in fills.items()
        if _contrast(label, fill) < 4.5
    }
    assert not failures, f"faceplate colours below WCAG AA against {label}: {failures}"


def test_the_documented_ratios_are_the_real_ones():
    """The palette comment quotes a ratio per colour. Keep it true.

    A stale number in a comment is worse than no number: it is a claim that the
    contrast was checked.
    """
    label, fills = _palette()
    text = _rack_js()
    checked = 0
    for category, fill in fills.items():
        quoted = re.search(rf"{category}\s+{fill}\s+([0-9.]+):1", text)
        assert quoted, f"{category} has no documented contrast ratio"
        assert _contrast(label, fill) == pytest.approx(float(quoted.group(1)), abs=0.01)
        checked += 1
    assert checked == len(fills)


def test_every_colour_bucket_the_api_can_emit_has_a_fill():
    _, fills = _palette()
    missing = set(CATEGORIES.values()) | {"other"}
    assert not (missing - set(fills)), f"categories with no fill: {sorted(missing - set(fills))}"


def test_chrome_text_clears_aa_on_the_drawings_own_ground():
    """The elevation pins its own dark ground, so these ratios are fixed."""
    text = _rack_js()
    chrome = re.search(r"const CHROME = \{(.*?)\n\};", text, re.DOTALL).group(1)
    values = dict(re.findall(r"(\w+):\s*'(#[0-9A-Fa-f]{6})'", chrome))
    assert _contrast(values["uText"], values["interior"]) >= 4.5
    assert _contrast(values["freeText"], values["free"]) >= 4.5
    assert _contrast(values["title"], values["page"]) >= 4.5
    assert _contrast(values["proposal"], values["page"]) >= 4.5
