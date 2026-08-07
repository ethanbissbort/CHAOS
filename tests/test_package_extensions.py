"""Tests for the v0.4 extension documents and the generalised bundle validator.

These tests exercise the machine-readable design package directly. They read the
YAML and JSON files rather than the database, so they run without any service,
and they enforce the rules the package exists to protect:

* every YAML has a content-equal JSON mirror;
* no two devices claim the same rack unit;
* every asset reference resolves against the register;
* every point name matches the dictionary pattern and is not a duplicate;
* every asset ID matches the SDD section 25.2 format;
* no lifecycle record carries an invented price, date or warranty term.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SCHEMA_DIR = ROOT / "schemas"
TOOLS_DIR = ROOT / "tools"

#: SDD section 25.2: <domain>.<asset_class>.<location_or_system>.<instance>.
SECTION_25_2_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){3}$")

RACK_LAYOUT = DATA_DIR / "rack_layout.yaml"
WATER_ASSETS = DATA_DIR / "water_assets.yaml"
WATER_POINTS = DATA_DIR / "water_points.yaml"
ASSET_LIFECYCLE = DATA_DIR / "asset_lifecycle.yaml"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def register():
    return load_yaml(DATA_DIR / "homestead_asset_register.yaml")


@pytest.fixture(scope="module")
def class_dictionary():
    return load_yaml(DATA_DIR / "asset_class_dictionary.yaml")


@pytest.fixture(scope="module")
def point_dictionary():
    return load_yaml(DATA_DIR / "point_dictionary.yaml")


@pytest.fixture(scope="module")
def rack_layout():
    return load_yaml(RACK_LAYOUT)


@pytest.fixture(scope="module")
def water_assets():
    return load_yaml(WATER_ASSETS)


@pytest.fixture(scope="module")
def water_points():
    return load_yaml(WATER_POINTS)


@pytest.fixture(scope="module")
def asset_lifecycle():
    return load_yaml(ASSET_LIFECYCLE)


@pytest.fixture(scope="module")
def asset_universe(register, water_assets):
    """Every asset identity the package declares, base register plus extensions."""
    return {asset["asset_id"] for asset in register["assets"]} | {
        asset["asset_id"] for asset in water_assets["assets"]
    }


# ---------------------------------------------------------------------------
# Every document: YAML/JSON mirror equality and a schema
# ---------------------------------------------------------------------------


def yaml_documents() -> list[Path]:
    return sorted(DATA_DIR.glob("*.yaml"))


@pytest.mark.parametrize("data_path", yaml_documents(), ids=lambda p: p.stem)
def test_json_mirror_is_content_equal(data_path: Path):
    """Every data/<name>.yaml has a data/<name>.json that parses to the same object."""
    json_path = data_path.with_suffix(".json")
    assert json_path.exists(), f"{data_path.name} has no JSON mirror"
    assert load_json(json_path) == load_yaml(data_path), f"{json_path.name} drifted from {data_path.name}"


@pytest.mark.parametrize(
    "stem", ["rack_layout", "water_assets", "water_points", "asset_lifecycle"]
)
def test_extension_has_draft_2020_12_schema(stem: str):
    from jsonschema import Draft202012Validator

    schema_path = SCHEMA_DIR / f"{stem}.schema.json"
    assert schema_path.exists(), f"{stem} has no schema"
    schema = load_json(schema_path)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(load_yaml(DATA_DIR / f"{stem}.yaml")))
    assert not errors, [f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]]


# ---------------------------------------------------------------------------
# Asset ID format (SDD section 25.2)
# ---------------------------------------------------------------------------


def test_register_asset_ids_match_section_25_2(register):
    bad = [a["asset_id"] for a in register["assets"] if not SECTION_25_2_ID.match(a["asset_id"])]
    assert not bad, bad


def test_water_asset_ids_match_section_25_2(water_assets):
    bad = [a["asset_id"] for a in water_assets["assets"] if not SECTION_25_2_ID.match(a["asset_id"])]
    assert not bad, bad


def test_water_asset_ids_encode_domain_and_class(water_assets):
    """Component 1 is the domain and component 2 is the asset class, as in the base register."""
    for asset in water_assets["assets"]:
        domain, asset_class, _, _ = asset["asset_id"].split(".")
        assert domain == asset["domain"], asset["asset_id"]
        assert asset_class == asset["asset_class"], asset["asset_id"]


def test_lifecycle_keys_match_section_25_2(asset_lifecycle):
    bad = [key for key in asset_lifecycle["records"] if not SECTION_25_2_ID.match(key)]
    assert not bad, bad


# ---------------------------------------------------------------------------
# Rack layout
# ---------------------------------------------------------------------------


def test_rack_layout_declares_itself_a_proposal(rack_layout):
    """The README lists rack positions as deliberately unresolved; the document must say so."""
    assert rack_layout["document_status"] == "proposal_for_review"
    assert rack_layout["approval_status"] != "ratified"
    assert rack_layout["review_required_before_use"] is True


def test_rack_units_are_within_range_and_never_overlap(rack_layout):
    capacity = rack_layout["rack"]["rack_unit_count"]
    occupied: dict[int, str] = {}
    for item in rack_layout["placements"]:
        if item["mount_style"] != "rack_unit":
            assert item["rack_unit_start"] is None, item["asset_id"]
            assert item["rack_unit_height"] == 0, item["asset_id"]
            continue
        start = item["rack_unit_start"]
        height = item["rack_unit_height"]
        assert start is not None and height >= 1, item["asset_id"]
        end = start + height - 1
        assert 1 <= start <= capacity, f"{item['asset_id']} starts at U{start}, outside 1-{capacity}"
        assert end <= capacity, f"{item['asset_id']} ends at U{end}, outside 1-{capacity}"
        for unit in range(start, end + 1):
            assert unit not in occupied, f"U{unit} claimed by both {occupied[unit]} and {item['asset_id']}"
            occupied[unit] = item["asset_id"]
    assert occupied, "the layout places nothing"


def test_rack_unit_allocation_matches_the_placements(rack_layout):
    capacity = rack_layout["rack"]["rack_unit_count"]
    occupied = {
        unit
        for item in rack_layout["placements"]
        if item["mount_style"] == "rack_unit"
        for unit in range(item["rack_unit_start"], item["rack_unit_start"] + item["rack_unit_height"])
    }
    allocation = rack_layout["rack_unit_allocation"]
    assert sorted(occupied) == allocation["occupied_rack_units"]
    assert [u for u in range(1, capacity + 1) if u not in occupied] == allocation["free_rack_units"]
    assert allocation["free_rack_unit_count"] == len(allocation["free_rack_units"])


def test_rack_capacity_agrees_with_the_register(rack_layout, register):
    by_id = {a["asset_id"]: a for a in register["assets"]}
    rack = by_id[rack_layout["rack"]["rack_asset_id"]]
    assert rack["asset_class"] == "rack"
    assert rack_layout["rack"]["rack_unit_count"] == rack["properties"]["u_height"]


def test_rack_layout_asset_references_resolve(rack_layout, asset_universe):
    sys.path.insert(0, str(TOOLS_DIR))
    from validate_registry_refs import check_document

    result = check_document(rack_layout, asset_universe, "rack_layout.yaml")
    assert result.checked > 0
    assert not result.malformed, [vars(r) for r in result.malformed]
    assert not result.unresolved, [vars(r) for r in result.unresolved]


def test_pdu_outlets_and_switch_ports_are_claimed_once(rack_layout):
    outlet_claims: dict[tuple[str, int], str] = {}
    port_claims: dict[tuple[str, int], str] = {}
    for item in rack_layout["placements"]:
        if item["pdu_asset_id"] and item["pdu_outlet"] is not None:
            key = (item["pdu_asset_id"], item["pdu_outlet"])
            assert key not in outlet_claims, f"{key} claimed twice"
            outlet_claims[key] = item["asset_id"]
        connections = [(item["switch_asset_id"], item["switch_port"])]
        connections += [(c["switch_asset_id"], c["switch_port"]) for c in item["additional_switch_connections"]]
        for switch, port in connections:
            if switch is None or port is None:
                continue
            key = (switch, port)
            assert key not in port_claims, f"{key} claimed twice"
            port_claims[key] = item["asset_id"]
    assert outlet_claims and port_claims


def test_pdu_outlet_counts_come_from_the_register(rack_layout, register):
    by_id = {a["asset_id"]: a for a in register["assets"]}
    for pdu in rack_layout["pdus"]:
        register_count = by_id[pdu["pdu_asset_id"]]["properties"]["outlet_count"]
        assert pdu["outlet_count"] == register_count
        numbers = [outlet["outlet"] for outlet in pdu["outlets"]]
        assert numbers == list(range(1, register_count + 1))


def test_rack_layout_vlans_agree_with_the_register(rack_layout, register):
    by_id = {a["asset_id"]: a for a in register["assets"]}
    for item in rack_layout["placements"]:
        assert len(item["vlan_ids"]) == len(item["vlan_asset_ids"]), item["asset_id"]
        for vlan_id, vlan_asset in zip(item["vlan_ids"], item["vlan_asset_ids"], strict=True):
            assert by_id[vlan_asset]["properties"]["vlan_id"] == vlan_id, item["asset_id"]
        register_net = by_id[item["asset_id"]].get("network") or {}
        expected = sorted(
            {
                value
                for key in ("vlan_id", "management_vlan_id")
                if isinstance(value := register_net.get(key), int)
            }
        )
        assert item["vlan_ids"] == expected, f"{item['asset_id']} VLANs drifted from the register"


def test_rack_layout_invents_no_power_figures(rack_layout):
    """The README lists measured rack loads as deliberately unresolved."""
    for item in rack_layout["placements"]:
        assert item["estimated_power_w"] is None, item["asset_id"]
        assert item["power_data_status"] == "not_estimated", item["asset_id"]
    assert rack_layout["thermal_summary"]["total_estimated_power_w"] is None


def test_estimated_rack_heights_are_declared_open(rack_layout):
    for item in rack_layout["placements"]:
        if item["rack_unit_height_basis"] == "estimated_pending_model_confirmation":
            assert "rack_unit_height" in item["open_fields"], item["asset_id"]
        assert item["placement_status"] == "preliminary", item["asset_id"]


# ---------------------------------------------------------------------------
# Water assets
# ---------------------------------------------------------------------------


def test_water_assets_are_all_concept(water_assets):
    assert {a["status"] for a in water_assets["assets"]} == {"concept"}
    assert {r["status"] for r in water_assets["relationships"]} == {"concept"}


def test_water_asset_ids_do_not_collide_with_the_register(water_assets, register):
    base = {a["asset_id"] for a in register["assets"]}
    water = [a["asset_id"] for a in water_assets["assets"]]
    assert len(water) == len(set(water)), "duplicate IDs within the extension"
    assert not set(water) & base


def test_water_relationship_ids_do_not_collide_with_the_register(water_assets, register):
    base = {r["relationship_id"] for r in register["relationships"]}
    water = [r["relationship_id"] for r in water_assets["relationships"]]
    assert len(water) == len(set(water))
    assert not set(water) & base


def test_water_classes_and_domains_exist_in_the_dictionary(water_assets, class_dictionary):
    classes = class_dictionary["asset_classes"]
    for asset in water_assets["assets"]:
        assert asset["asset_class"] in classes, asset["asset_id"]
        assert asset["domain"] in classes[asset["asset_class"]]["allowed_domains"], asset["asset_id"]


def test_water_asset_parents_and_relationships_resolve(water_assets, asset_universe):
    for asset in water_assets["assets"]:
        if asset["parent_id"] is not None:
            assert asset["parent_id"] in asset_universe, asset["asset_id"]
    for relation in water_assets["relationships"]:
        assert relation["from_asset_id"] in asset_universe, relation["relationship_id"]
        assert relation["to_asset_id"] in asset_universe, relation["relationship_id"]


def test_water_point_profiles_resolve(water_assets, class_dictionary):
    profiles = set(class_dictionary["point_profiles"])
    for asset in water_assets["assets"]:
        for profile in asset["point_profile_refs"]:
            assert profile in profiles, asset["asset_id"]


def test_every_water_asset_records_its_unknowns(water_assets):
    """A concept asset with no open fields would be claiming knowledge nobody has."""
    for asset in water_assets["assets"]:
        assert asset["open_fields"], asset["asset_id"]


def test_water_assets_cover_the_section_3_4_systems(water_assets):
    tags = {tag for asset in water_assets["assets"] for tag in asset["tags"]}
    for expected in ("potable_chain", "rainwater", "irrigation", "graywater", "freeze_protection", "leak_detection"):
        assert expected in tags, expected


# ---------------------------------------------------------------------------
# Water points
# ---------------------------------------------------------------------------


def test_water_point_names_match_the_dictionary_pattern(water_points, point_dictionary):
    pattern = re.compile(point_dictionary["point_name_pattern"])
    assert water_points["point_name_pattern"] == point_dictionary["point_name_pattern"]
    for name in water_points["points"]:
        assert pattern.match(name), name


def test_no_water_point_duplicates_an_existing_dictionary_point(water_points, point_dictionary):
    duplicates = set(water_points["points"]) & set(point_dictionary["points"])
    assert not duplicates, sorted(duplicates)


def test_water_points_declare_v0_4_provenance(water_points):
    assert water_points["provenance"]["dictionary_status"] == "reconciled_v0_4"
    for name, definition in water_points["points"].items():
        assert definition["dictionary_status"] == "reconciled_v0_4", name


def test_water_point_classes_and_units_resolve(water_points, point_dictionary):
    classes = set(point_dictionary["point_classes"])
    units = set(point_dictionary["canonical_units"]) | set(water_points["canonical_units_added"])
    for name, definition in water_points["points"].items():
        assert definition["default_class"] in classes, name
        assert definition["default_class"] in definition["allowed_classes"], name
        assert set(definition["allowed_classes"]) <= classes, name
        if definition["unit"] is not None:
            assert definition["unit"] in units, f"{name}: {definition['unit']}"


def test_water_point_applicable_classes_exist(water_points, class_dictionary):
    known = set(class_dictionary["asset_classes"])
    for name, definition in water_points["points"].items():
        unknown = set(definition["applicable_asset_classes"]) - known
        assert not unknown, f"{name}: {sorted(unknown)}"


def test_reused_points_really_exist_in_the_base_dictionary(water_points, point_dictionary):
    missing = set(water_points["reused_existing_points"]["points"]) - set(point_dictionary["points"])
    assert not missing, sorted(missing)


def test_leak_isolation_is_not_automatic_by_default(water_points):
    """Removing water from a residence is an owner decision, not a platform default."""
    assert water_points["points"]["leak_isolation_requested"]["automatic_control_default"] is False


def test_water_points_cover_every_water_requirement(water_points):
    covered = " ".join(d["source_section"] for d in water_points["points"].values())
    for requirement in ("FR-200", "FR-201", "FR-202", "FR-203", "FR-204", "FR-205"):
        assert requirement in covered, requirement


# ---------------------------------------------------------------------------
# Asset lifecycle
# ---------------------------------------------------------------------------


def test_every_lifecycle_key_resolves(asset_lifecycle, asset_universe):
    unresolved = sorted(set(asset_lifecycle["records"]) - asset_universe)
    assert not unresolved, unresolved


def test_lifecycle_covers_every_register_asset(asset_lifecycle, register):
    missing = sorted({a["asset_id"] for a in register["assets"]} - set(asset_lifecycle["records"]))
    assert not missing, missing


def test_no_lifecycle_record_invents_money_or_dates(asset_lifecycle):
    """The single most important rule: the package does not manufacture CAPEX."""
    forbidden = (
        "capex_reference",
        "acquisition_cost",
        "acquisition_currency",
        "purchase_date",
        "in_service_date",
        "warranty_expires",
        "replacement_cost",
        "expected_service_life_years",
    )
    for asset_id, record in asset_lifecycle["records"].items():
        if record["data_status"] != "not_yet_imported":
            continue
        for key in forbidden:
            assert record[key] is None, f"{asset_id}.{key} carries a value"
        assert record["spare_parts"] == [], asset_id
        assert record["warranty_status"] == "not_yet_imported", asset_id
        assert record["replacement_cost_status"] == "not_yet_imported", asset_id


def test_ownership_state_follows_the_declared_derivation(asset_lifecycle, register):
    derivation = asset_lifecycle["ownership_derivation"]["status_to_ownership"]
    by_id = {a["asset_id"]: a for a in register["assets"]}
    for asset_id, record in asset_lifecycle["records"].items():
        asset = by_id.get(asset_id)
        if asset is None or record["ownership_basis"] != "derived_from_register_status":
            continue
        assert record["register_status"] == asset["status"], asset_id
        assert record["ownership_state"] == derivation[asset["status"]], asset_id


def test_ownership_counts_are_accurate(asset_lifecycle):
    declared = asset_lifecycle["ownership_derivation"]["counts"]
    observed = {key: 0 for key in declared}
    for record in asset_lifecycle["records"].values():
        observed[record["ownership_state"]] += 1
    assert observed == declared


def test_spares_policy_follows_register_criticality(asset_lifecycle, register):
    by_id = {a["asset_id"]: a for a in register["assets"]}
    for asset_id, record in asset_lifecycle["records"].items():
        asset = by_id.get(asset_id)
        if asset is None:
            continue
        assert record["spares_policy_required"] == (
            asset["criticality"] in {"life_safety", "critical"}
        ), asset_id


def test_import_instructions_tell_a_human_what_to_supply(asset_lifecycle):
    instructions = asset_lifecycle["import_instructions"]
    assert len(instructions["required_from_the_owner"]) >= 5
    assert len(instructions["import_rules"]) >= 3


# ---------------------------------------------------------------------------
# The validator itself
# ---------------------------------------------------------------------------


def test_validate_bundle_passes():
    result = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "validate_bundle.py"), "--json", "--no-report"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["result"] == "PASS"
    assert not payload["errors"]


def test_validate_bundle_covers_every_extension_document():
    result = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "validate_bundle.py"), "--json", "--no-report"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    payload = json.loads(result.stdout)
    validated = {entry["document"] for entry in payload["documents"]}
    for stem in ("rack_layout", "water_assets", "water_points", "asset_lifecycle"):
        assert stem in validated, stem
        entry = next(e for e in payload["documents"] if e["document"] == stem)
        assert entry["status"] == "OK", entry
        assert entry["schema"] is not None, stem
        assert entry["json_mirror"] is not None, stem


@pytest.fixture()
def package_copy(tmp_path: Path) -> Path:
    """A throwaway copy of data/ and schemas/, so tests never mutate the working tree.

    Other agents run this validator against the same checkout; a stray schemaless
    document in data/ would break their --strict build.
    """
    import shutil

    root = tmp_path / "package"
    (root).mkdir()
    shutil.copytree(DATA_DIR, root / "data")
    shutil.copytree(SCHEMA_DIR, root / "schemas")
    return root


def run_validator(root: Path, *flags: str) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "validate_bundle.py"), "--root", str(root), "--json", *flags],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return result.returncode, json.loads(result.stdout)


def test_validator_warns_rather_than_crashing_on_a_schemaless_document(package_copy: Path):
    """Auto-discovery: a data/*.yaml with no schema yet is a warning, not a build break.

    Parallel work streams add a data document before its schema lands. That must
    not fail the build for everyone else.
    """
    (package_copy / "data" / "_probe.yaml").write_text("document_type: probe\nvalue: 1\n", encoding="utf-8")
    (package_copy / "data" / "_probe.json").write_text(
        '{\n  "document_type": "probe",\n  "value": 1\n}\n', encoding="utf-8"
    )

    code, payload = run_validator(package_copy)
    assert code == 0
    assert payload["result"] == "PASS"
    assert any("_probe" in warning for warning in payload["warnings"])
    assert "_probe" in {entry["document"] for entry in payload["documents"]}

    strict_code, strict_payload = run_validator(package_copy, "--strict")
    assert strict_code == 1, "--strict must turn warnings into failures"
    assert strict_payload["result"] == "FAIL"


def test_validator_fails_when_a_json_mirror_drifts(package_copy: Path):
    mirror = package_copy / "data" / "water_points.json"
    document = json.loads(mirror.read_text(encoding="utf-8"))
    document["schema_version"] = "0.0.0-drifted"
    mirror.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    code, payload = run_validator(package_copy)
    assert code == 1
    assert any("not content-equal" in error for error in payload["errors"]), payload["errors"]


def test_validator_fails_when_a_json_mirror_is_missing(package_copy: Path):
    (package_copy / "data" / "rack_layout.json").unlink()
    code, payload = run_validator(package_copy)
    assert code == 1
    assert any("mirror" in error and "missing" in error for error in payload["errors"]), payload["errors"]


def test_validator_writes_a_report_with_the_supplied_timestamp(package_copy: Path):
    stamp = "2099-01-02T03:04:05Z"
    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS_DIR / "validate_bundle.py"),
            "--root",
            str(package_copy),
            "--timestamp",
            stamp,
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads((package_copy / "validation_report.json").read_text(encoding="utf-8"))
    assert payload["validation_timestamp"] == stamp
    assert payload["result"] == "PASS"
    assert payload["counts"]["documents_validated"] >= 10


def test_validator_detects_overlapping_rack_units():
    """Negative test: the overlap check must actually fire."""
    import copy

    sys.path.insert(0, str(TOOLS_DIR))
    import validate_bundle

    documents = {
        stem: load_yaml(DATA_DIR / f"{stem}.yaml")
        for stem in (
            "asset_class_dictionary",
            "point_dictionary",
            "homestead_asset_register",
            "point_bindings",
            "water_assets",
            "rack_layout",
        )
    }
    mutated = copy.deepcopy(documents)
    stacked = [p for p in mutated["rack_layout"]["placements"] if p["mount_style"] == "rack_unit"]
    stacked[1]["rack_unit_start"] = stacked[0]["rack_unit_start"]

    report = validate_bundle.Report()
    for stem in mutated:
        report.documents[stem] = validate_bundle.DocumentResult(name=stem, data_path=f"data/{stem}.yaml")
    core = validate_bundle.check_core(report, mutated)
    water_ids = validate_bundle.check_water_assets(report, mutated, core)
    validate_bundle.check_rack_layout(report, mutated, core, water_ids)

    assert any("claimed by both" in error for error in report.errors), report.errors
