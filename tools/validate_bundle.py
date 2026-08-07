#!/usr/bin/env python3
"""Validate the homestead machine-readable design package (SDD section 48).

What this does
--------------
1. **Discovers** every ``data/<name>.yaml`` and pairs it with
   ``schemas/<name>.schema.json``. Nothing is hard-coded, so a document added by
   a parallel work stream is picked up as soon as it lands. A data file with no
   schema yet is a *warning*, not a crash: work in progress must not break the
   build for everyone else.
2. Verifies each ``data/<name>.json`` mirror parses to the same object as its
   YAML source.
3. Runs the section 48 cross-reference checks over the four core documents:
   ID uniqueness, parent references, relationship endpoints, class and domain
   resolution, point-profile resolution, binding asset/point resolution,
   ``point_id == asset_id/point_name``, duplicate detection, and the existence
   of every class and profile point in the point dictionary.
4. Runs cross-document checks for the optional extension documents. Each is
   skipped with a note when the document is absent.
5. Validates asset IDs against the section 25.2 format and point names against
   the dictionary's ``point_name_pattern``.
6. Regenerates ``validation_report.json``.

Usage::

    python3 tools/validate_bundle.py                  # human summary
    python3 tools/validate_bundle.py --json           # machine-readable
    python3 tools/validate_bundle.py --strict         # warnings become errors
    python3 tools/validate_bundle.py --timestamp ...  # pin the report timestamp
    python3 tools/validate_bundle.py --root /tmp/pkg  # validate a copy of the package

The report timestamp comes from ``--timestamp``, then from
``$CHAOS_VALIDATION_TIMESTAMP``, then from the real clock. No clock is
faked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_registry_refs import (
    ASSET_ID_RE,
    ASSET_ID_SCHEMA_RE,
    check_document,
)

#: Package root. ``--root`` rebinds these so a copy of the package can be
#: validated without touching the working tree -- which is what the tests do,
#: and what a release pipeline wants when it validates an extracted artifact.
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SCHEMA_DIR = ROOT / "schemas"
REPORT_PATH = ROOT / "validation_report.json"


def configure_root(root: Path) -> None:
    """Point discovery at ``root`` instead of the repository this script lives in."""
    global ROOT, DATA_DIR, SCHEMA_DIR, REPORT_PATH
    ROOT = root.resolve()
    DATA_DIR = ROOT / "data"
    SCHEMA_DIR = ROOT / "schemas"
    REPORT_PATH = ROOT / "validation_report.json"


#: Documents whose schema file is not named after the data file.
SCHEMA_STEM_ALIASES = {"homestead_asset_register": "asset_register"}

#: The four documents SDD section 44 requires. Missing one is an error.
CORE_DOCUMENTS = (
    "asset_class_dictionary",
    "point_dictionary",
    "homestead_asset_register",
    "point_bindings",
)

#: Extension documents this validator knows how to cross-check. Absent is fine.
EXTENSION_DOCUMENTS = ("rack_layout", "water_assets", "water_points", "asset_lifecycle")

#: Keys that count a document's principal records, for the report.
COUNT_KEYS = (
    ("assets", "assets"),
    ("relationships", "relationships"),
    ("points", "points"),
    ("bindings", "bindings"),
    ("asset_classes", "asset_classes"),
    ("point_profiles", "point_profiles"),
    ("domains", "domains"),
    ("placements", "placements"),
    ("records", "records"),
    ("loads", "loads"),
    ("alarms", "alarms"),
    ("alarm_definitions", "alarm_definitions"),
)


# ---------------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------------


@dataclass
class DocumentResult:
    name: str
    data_path: str
    schema_path: str | None = None
    json_path: str | None = None
    status: str = "OK"
    messages: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    documents: dict[str, DocumentResult] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def error(self, document: str | None, message: str) -> None:
        self.errors.append(message)
        if document and document in self.documents:
            self.documents[document].status = "FAIL"
            self.documents[document].messages.append(f"ERROR: {message}")

    def warn(self, document: str | None, message: str) -> None:
        self.warnings.append(message)
        if document and document in self.documents:
            if self.documents[document].status == "OK":
                self.documents[document].status = "WARN"
            self.documents[document].messages.append(f"WARNING: {message}")

    def note(self, message: str) -> None:
        self.notes.append(message)

    def check(self, message: str) -> None:
        self.checks.append(message)


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Stage 1: discovery, schema validation, JSON mirrors
# ---------------------------------------------------------------------------


def discover_and_validate(report: Report) -> dict[str, Any]:
    """Validate every data/*.yaml against its schema and JSON mirror."""
    documents: dict[str, Any] = {}

    for data_path in sorted(DATA_DIR.glob("*.yaml")):
        stem = data_path.stem
        result = DocumentResult(name=stem, data_path=str(data_path.relative_to(ROOT)))
        report.documents[stem] = result

        try:
            data = load_yaml(data_path)
        except yaml.YAMLError as exc:
            report.error(stem, f"{data_path.name}: YAML parse failure: {exc}")
            continue
        if data is None:
            report.error(stem, f"{data_path.name}: document is empty")
            continue
        documents[stem] = data

        # --- schema ------------------------------------------------------
        schema_stem = SCHEMA_STEM_ALIASES.get(stem, stem)
        schema_path = SCHEMA_DIR / f"{schema_stem}.schema.json"
        if schema_path.exists():
            result.schema_path = str(schema_path.relative_to(ROOT))
            try:
                schema = load_json(schema_path)
            except json.JSONDecodeError as exc:
                report.error(stem, f"{schema_path.name}: JSON parse failure: {exc}")
                schema = None
            if schema is not None:
                try:
                    Draft202012Validator.check_schema(schema)
                except Exception as exc:
                    report.error(stem, f"{schema_path.name}: not a valid Draft 2020-12 schema: {exc}")
                else:
                    errors = sorted(
                        Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path)
                    )
                    for error in errors[:25]:
                        location = "/".join(map(str, error.path)) or "<root>"
                        report.error(stem, f"{data_path.name}: {location}: {error.message}")
                    if len(errors) > 25:
                        report.error(
                            stem, f"{data_path.name}: {len(errors) - 25} further schema errors suppressed"
                        )
        elif stem in CORE_DOCUMENTS:
            report.error(stem, f"{data_path.name}: required schema {schema_path.name} is missing")
        else:
            report.warn(
                stem,
                f"{data_path.name}: no schema at schemas/{schema_stem}.schema.json; "
                "document parsed but not shape-checked",
            )

        # --- JSON mirror --------------------------------------------------
        json_path = data_path.with_suffix(".json")
        if not json_path.exists():
            report.error(stem, f"{data_path.name}: JSON mirror data/{json_path.name} is missing")
        else:
            result.json_path = str(json_path.relative_to(ROOT))
            try:
                mirror = load_json(json_path)
            except json.JSONDecodeError as exc:
                report.error(stem, f"{json_path.name}: JSON parse failure: {exc}")
            else:
                if mirror != data:
                    report.error(
                        stem, f"{json_path.name}: JSON mirror is not content-equal to {data_path.name}"
                    )

        # --- counts -------------------------------------------------------
        if isinstance(data, dict):
            for key, label in COUNT_KEYS:
                value = data.get(key)
                if isinstance(value, (list, dict)):
                    result.counts[label] = len(value)

    for stem in CORE_DOCUMENTS:
        if stem not in documents:
            report.error(None, f"required document data/{stem}.yaml is missing")

    report.check("Every data/*.yaml was discovered and paired with its schema by name.")
    report.check("All JSON Schemas are valid Draft 2020-12 schemas.")
    report.check("All YAML documents validate against their schemas.")
    report.check("Every data/*.json is content-equal to its data/*.yaml source.")
    return documents


# ---------------------------------------------------------------------------
# Stage 2: core cross-reference checks (SDD section 48)
# ---------------------------------------------------------------------------


def check_asset_id_format(report: Report, document: str, asset_id: str, context: str) -> None:
    if not ASSET_ID_SCHEMA_RE.match(asset_id):
        report.error(document, f"{context}: asset ID {asset_id!r} does not match the canonical ID grammar")
    elif not ASSET_ID_RE.match(asset_id):
        report.warn(
            document,
            f"{context}: asset ID {asset_id!r} has more than the four components of the section 25.2 canonical form",
        )


def check_core(report: Report, documents: dict[str, Any]) -> dict[str, Any]:
    """The section 48 cross-reference checks over the four core documents."""
    if not all(stem in documents for stem in CORE_DOCUMENTS):
        report.note("Core cross-reference checks skipped: not all four core documents are present.")
        return {}

    asset_dict = documents["asset_class_dictionary"]
    point_dict = documents["point_dictionary"]
    register = documents["homestead_asset_register"]
    bindings = documents["point_bindings"]

    asset_ids = [asset["asset_id"] for asset in register["assets"]]
    duplicates = {value for value in asset_ids if asset_ids.count(value) > 1}
    if duplicates:
        report.error("homestead_asset_register", f"duplicate asset_id values: {sorted(duplicates)}")
    asset_set = set(asset_ids)

    if register["site_id"] not in asset_set:
        report.error(
            "homestead_asset_register", f"site_id {register['site_id']!r} does not reference an asset"
        )

    class_set = set(asset_dict["asset_classes"])
    profile_set = set(asset_dict["point_profiles"])
    point_set = set(point_dict["points"])
    domain_set = set(asset_dict["domains"])

    point_name_pattern = re.compile(point_dict["point_name_pattern"])
    for point_name in point_set:
        if not point_name_pattern.match(point_name):
            report.error("point_dictionary", f"point name {point_name!r} violates point_name_pattern")

    for asset in register["assets"]:
        asset_id = asset["asset_id"]
        check_asset_id_format(report, "homestead_asset_register", asset_id, "register")
        if asset["asset_class"] not in class_set:
            report.error(
                "homestead_asset_register", f"unknown asset class: {asset_id} -> {asset['asset_class']}"
            )
            continue
        if asset["domain"] not in domain_set:
            report.error("homestead_asset_register", f"unknown domain: {asset_id} -> {asset['domain']}")
        if asset["domain"] not in asset_dict["asset_classes"][asset["asset_class"]]["allowed_domains"]:
            report.error(
                "homestead_asset_register",
                f"domain/class mismatch: {asset_id} uses class {asset['asset_class']} in domain {asset['domain']}",
            )
        if asset["parent_id"] is not None and asset["parent_id"] not in asset_set:
            report.error("homestead_asset_register", f"missing parent: {asset_id} -> {asset['parent_id']}")
        for profile in asset["point_profile_refs"]:
            if profile not in profile_set:
                report.error("homestead_asset_register", f"unknown point profile: {asset_id} -> {profile}")

    relationship_ids: list[str] = []
    for relation in register["relationships"]:
        relationship_ids.append(relation["relationship_id"])
        if relation["from_asset_id"] not in asset_set:
            report.error(
                "homestead_asset_register",
                f"relationship {relation['relationship_id']}: source {relation['from_asset_id']} does not exist",
            )
        if relation["to_asset_id"] not in asset_set:
            report.error(
                "homestead_asset_register",
                f"relationship {relation['relationship_id']}: target {relation['to_asset_id']} does not exist",
            )
        if relation["relationship_type"] not in asset_dict["relationship_types"]:
            report.error(
                "homestead_asset_register",
                f"relationship {relation['relationship_id']}: unknown type {relation['relationship_type']}",
            )
    duplicate_relations = {value for value in relationship_ids if relationship_ids.count(value) > 1}
    if duplicate_relations:
        report.error(
            "homestead_asset_register", f"duplicate relationship_id values: {sorted(duplicate_relations)}"
        )

    binding_ids: list[str] = []
    for binding in bindings["bindings"]:
        binding_ids.append(binding["point_id"])
        if binding["asset_id"] not in asset_set:
            report.error("point_bindings", f"binding {binding['point_id']}: asset does not exist")
        if binding["point_name"] not in point_set:
            report.error("point_bindings", f"binding {binding['point_id']}: point is not in the dictionary")
        if binding["point_id"] != f"{binding['asset_id']}/{binding['point_name']}":
            report.error("point_bindings", f"binding point_id mismatch: {binding['point_id']}")
    duplicate_bindings = {value for value in binding_ids if binding_ids.count(value) > 1}
    if duplicate_bindings:
        report.error("point_bindings", f"duplicate point_id bindings: {sorted(duplicate_bindings)}")

    for class_name, class_def in asset_dict["asset_classes"].items():
        for point_name in class_def["default_points"]:
            if point_name not in point_set:
                report.error(
                    "asset_class_dictionary", f"class {class_name} references undefined point {point_name}"
                )
    for profile_name, profile in asset_dict["point_profiles"].items():
        for point_name in profile:
            if point_name not in point_set:
                report.error(
                    "asset_class_dictionary",
                    f"profile {profile_name} references undefined point {point_name}",
                )

    report.check("All asset IDs are unique and match the section 25.2 identification format.")
    report.check("All parent and relationship references resolve.")
    report.check("All asset classes, domains and relationship types resolve.")
    report.check("All point profiles and class default points resolve.")
    report.check("All point bindings reference existing assets and point definitions.")
    report.check("All point IDs match <asset_id>/<point_name>.")
    report.check("All point names match the dictionary point_name_pattern.")

    report.counts.update(
        {
            "domains": len(domain_set),
            "asset_classes": len(class_set),
            "point_definitions": len(point_set),
            "assets": len(asset_set),
            "relationships": len(register["relationships"]),
            "point_bindings": len(binding_ids),
        }
    )

    return {
        "asset_set": asset_set,
        "class_dict": asset_dict,
        "point_dict": point_dict,
        "register": register,
        "point_set": point_set,
        "profile_set": profile_set,
        "class_set": class_set,
        "domain_set": domain_set,
    }


# ---------------------------------------------------------------------------
# Stage 3: extension-document cross-checks
# ---------------------------------------------------------------------------


def check_water_assets(report: Report, documents: dict[str, Any], core: dict[str, Any]) -> set[str]:
    """Water extension: classes, domains, parents, relationships, ID collisions."""
    name = "water_assets"
    if name not in documents:
        report.note("data/water_assets.yaml absent: water asset extension checks skipped.")
        return set()
    if not core:
        report.note("water asset extension checks skipped: core documents unavailable.")
        return set()

    water = documents[name]
    base_assets: set[str] = core["asset_set"]
    class_dict = core["class_dict"]
    ids = [asset["asset_id"] for asset in water["assets"]]
    duplicates = {value for value in ids if ids.count(value) > 1}
    if duplicates:
        report.error(name, f"duplicate asset_id values within the extension: {sorted(duplicates)}")
    collisions = set(ids) & base_assets
    if collisions:
        report.error(name, f"asset IDs collide with the base register: {sorted(collisions)}")

    universe = base_assets | set(ids)
    for asset in water["assets"]:
        asset_id = asset["asset_id"]
        check_asset_id_format(report, name, asset_id, "water extension")
        parts = asset_id.split(".")
        if parts[0] != asset["domain"]:
            report.error(name, f"{asset_id}: first ID component does not match the domain field")
        if parts[1] != asset["asset_class"]:
            report.error(name, f"{asset_id}: second ID component does not match the asset_class field")
        if asset["asset_class"] not in core["class_set"]:
            report.error(
                name, f"{asset_id}: asset class {asset['asset_class']} is not in the class dictionary"
            )
            continue
        allowed = class_dict["asset_classes"][asset["asset_class"]]["allowed_domains"]
        if asset["domain"] not in allowed:
            report.error(
                name, f"{asset_id}: class {asset['asset_class']} is not allowed in domain {asset['domain']}"
            )
        if asset["parent_id"] is not None and asset["parent_id"] not in universe:
            report.error(
                name, f"{asset_id}: parent {asset['parent_id']} does not exist in register or extension"
            )
        for profile in asset["point_profile_refs"]:
            if profile not in core["profile_set"]:
                report.error(name, f"{asset_id}: unknown point profile {profile}")

    base_relationship_ids = {r["relationship_id"] for r in core["register"]["relationships"]}
    seen: set[str] = set()
    for relation in water["relationships"]:
        relationship_id = relation["relationship_id"]
        if relationship_id in base_relationship_ids:
            report.error(
                name, f"relationship {relationship_id} collides with a base-register relationship ID"
            )
        if relationship_id in seen:
            report.error(name, f"duplicate relationship_id within the extension: {relationship_id}")
        seen.add(relationship_id)
        for endpoint in ("from_asset_id", "to_asset_id"):
            if relation[endpoint] not in universe:
                report.error(
                    name, f"relationship {relationship_id}: {endpoint} {relation[endpoint]} does not exist"
                )
        if relation["relationship_type"] not in class_dict["relationship_types"]:
            report.error(
                name, f"relationship {relationship_id}: unknown type {relation['relationship_type']}"
            )

    if water.get("site_id") not in universe:
        report.error(name, f"site_id {water.get('site_id')!r} does not resolve")

    report.check(
        "Water extension asset IDs are unique, well formed and do not collide with the base register."
    )
    report.check("Water extension classes, domains, parents, profiles and relationships resolve.")
    report.counts["water_assets"] = len(ids)
    report.counts["water_relationships"] = len(water["relationships"])
    return set(ids)


def check_water_points(report: Report, documents: dict[str, Any], core: dict[str, Any]) -> None:
    """Water point extension: naming, duplication, class and unit resolution."""
    name = "water_points"
    if name not in documents:
        report.note("data/water_points.yaml absent: water point extension checks skipped.")
        return
    if not core:
        report.note("water point extension checks skipped: core documents unavailable.")
        return

    extension = documents[name]
    point_dict = core["point_dict"]
    pattern = re.compile(point_dict["point_name_pattern"])
    known_classes = set(point_dict["point_classes"])
    known_units = set(point_dict["canonical_units"]) | set(extension.get("canonical_units_added", {}))
    base_points: set[str] = core["point_set"]

    if extension.get("point_name_pattern") != point_dict["point_name_pattern"]:
        report.error(name, "point_name_pattern does not match the base point dictionary")

    for point_name, definition in extension["points"].items():
        if not pattern.match(point_name):
            report.error(name, f"point name {point_name!r} violates point_name_pattern")
        if point_name in base_points:
            report.error(name, f"point {point_name!r} already exists in the base point dictionary")
        if definition["default_class"] not in known_classes:
            report.error(name, f"{point_name}: unknown point class {definition['default_class']}")
        for allowed in definition["allowed_classes"]:
            if allowed not in known_classes:
                report.error(name, f"{point_name}: unknown allowed class {allowed}")
        if definition["default_class"] not in definition["allowed_classes"]:
            report.error(name, f"{point_name}: default_class is not listed in allowed_classes")
        unit = definition["unit"]
        if unit is not None and unit not in known_units:
            report.error(name, f"{point_name}: unit {unit!r} is not a canonical unit")
        for asset_class in definition["applicable_asset_classes"]:
            if asset_class not in core["class_set"]:
                report.error(
                    name, f"{point_name}: applicable asset class {asset_class} is not in the dictionary"
                )

    for point_name in extension.get("reused_existing_points", {}).get("points", []):
        if point_name not in base_points:
            report.error(
                name, f"reused_existing_points lists {point_name!r}, which is not in the base dictionary"
            )

    report.check("Water point names are unique against the base dictionary and match point_name_pattern.")
    report.check("Water point classes, units and applicable asset classes resolve.")
    report.counts["water_points"] = len(extension["points"])


def check_rack_layout(
    report: Report, documents: dict[str, Any], core: dict[str, Any], extra: set[str]
) -> None:
    """Rack layout: references, rack-unit bounds, overlap, outlet and port uniqueness."""
    name = "rack_layout"
    if name not in documents:
        report.note("data/rack_layout.yaml absent: rack layout checks skipped.")
        return
    if not core:
        report.note("rack layout checks skipped: core documents unavailable.")
        return

    layout = documents[name]
    universe = core["asset_set"] | extra
    register_by_id = {asset["asset_id"]: asset for asset in core["register"]["assets"]}

    result = check_document(layout, universe, "rack_layout.yaml")
    for reference in result.malformed:
        report.error(name, f"malformed asset reference at {reference.path}: {reference.value!r}")
    for reference in result.unresolved:
        report.error(name, f"unresolved asset reference at {reference.path}: {reference.value!r}")

    rack_id = layout["rack"]["rack_asset_id"]
    rack_asset = register_by_id.get(rack_id)
    if rack_asset is None:
        report.error(name, f"rack_asset_id {rack_id!r} is not in the register")
        return
    if rack_asset["asset_class"] != "rack":
        report.error(name, f"rack_asset_id {rack_id} is class {rack_asset['asset_class']}, not rack")

    declared_units = layout["rack"]["rack_unit_count"]
    register_units = rack_asset["properties"].get("u_height")
    if register_units is not None and declared_units != register_units:
        report.error(
            name,
            f"rack_unit_count {declared_units} disagrees with the register u_height {register_units}",
        )

    occupied: dict[int, str] = {}
    for item in layout["placements"]:
        asset_id = item["asset_id"]
        if item["mount_style"] != "rack_unit":
            if item["rack_unit_start"] is not None or item["rack_unit_height"] != 0:
                report.error(name, f"{asset_id}: {item['mount_style']} placement must not claim rack units")
            continue
        start = item["rack_unit_start"]
        height = item["rack_unit_height"]
        if start is None or height < 1:
            report.error(
                name, f"{asset_id}: rack-unit placement needs a start position and a height of at least 1"
            )
            continue
        end = start + height - 1
        if start < 1 or end > declared_units:
            report.error(name, f"{asset_id}: occupies U{start}-U{end}, outside 1-{declared_units}")
            continue
        for unit in range(start, end + 1):
            if unit in occupied:
                report.error(name, f"rack unit U{unit} is claimed by both {occupied[unit]} and {asset_id}")
            occupied[unit] = asset_id

    allocation = layout.get("rack_unit_allocation", {})
    if sorted(occupied) != sorted(allocation.get("occupied_rack_units", [])):
        report.error(name, "rack_unit_allocation.occupied_rack_units disagrees with the placements")
    expected_free = [u for u in range(1, declared_units + 1) if u not in occupied]
    if expected_free != allocation.get("free_rack_units", []):
        report.error(name, "rack_unit_allocation.free_rack_units disagrees with the placements")

    pdu_outlet_counts = {pdu["pdu_asset_id"]: pdu["outlet_count"] for pdu in layout.get("pdus", [])}
    for pdu in layout.get("pdus", []):
        pdu_id = pdu["pdu_asset_id"]
        register_pdu = register_by_id.get(pdu_id)
        if register_pdu is not None:
            register_outlets = register_pdu["properties"].get("outlet_count")
            if register_outlets is not None and pdu["outlet_count"] != register_outlets:
                report.error(
                    name,
                    f"{pdu_id}: outlet_count {pdu['outlet_count']} disagrees with the register value {register_outlets}",
                )
        seen_outlets: set[int] = set()
        for outlet in pdu["outlets"]:
            number = outlet["outlet"]
            if number in seen_outlets:
                report.error(name, f"{pdu_id}: outlet {number} is listed twice")
            seen_outlets.add(number)
            if number < 1 or number > pdu["outlet_count"]:
                report.error(name, f"{pdu_id}: outlet {number} is outside 1-{pdu['outlet_count']}")

    outlet_claims: dict[tuple[str, int], str] = {}
    port_claims: dict[tuple[str, int], str] = {}
    for item in layout["placements"]:
        asset_id = item["asset_id"]
        pdu_id, outlet = item["pdu_asset_id"], item["pdu_outlet"]
        if pdu_id is not None and outlet is not None:
            limit = pdu_outlet_counts.get(pdu_id)
            if limit is not None and not 1 <= outlet <= limit:
                report.error(name, f"{asset_id}: PDU outlet {outlet} is outside 1-{limit} on {pdu_id}")
            key = (pdu_id, outlet)
            if key in outlet_claims:
                report.error(
                    name, f"{pdu_id} outlet {outlet} is claimed by both {outlet_claims[key]} and {asset_id}"
                )
            outlet_claims[key] = asset_id

        connections = [(item["switch_asset_id"], item["switch_port"])] + [
            (c["switch_asset_id"], c["switch_port"]) for c in item["additional_switch_connections"]
        ]
        for switch_id, port in connections:
            if switch_id is None or port is None:
                continue
            key = (switch_id, port)
            if key in port_claims:
                report.error(
                    name, f"{switch_id} port {port} is claimed by both {port_claims[key]} and {asset_id}"
                )
            port_claims[key] = asset_id

        for vlan_id, vlan_asset in zip(item["vlan_ids"], item["vlan_asset_ids"], strict=False):
            vlan_record = register_by_id.get(vlan_asset)
            if vlan_record is None:
                report.error(name, f"{asset_id}: VLAN asset {vlan_asset} is not in the register")
            elif vlan_record["properties"].get("vlan_id") != vlan_id:
                report.error(name, f"{asset_id}: VLAN {vlan_id} does not match asset {vlan_asset}")
        if len(item["vlan_ids"]) != len(item["vlan_asset_ids"]):
            report.error(name, f"{asset_id}: vlan_ids and vlan_asset_ids have different lengths")

        if item["estimated_power_w"] is not None:
            report.error(
                name, f"{asset_id}: estimated_power_w must stay null until real power data is measured"
            )

    if layout.get("document_status") != "proposal_for_review":
        report.error(name, "rack layout must declare document_status proposal_for_review")
    if layout.get("approval_status") == "ratified":
        report.warn(name, "rack layout is marked ratified; confirm an owner actually approved it")

    report.check("Rack layout references resolve and no two devices claim the same rack unit.")
    report.check("Rack layout PDU outlets and switch ports are each claimed at most once.")
    report.check("Rack layout VLAN assignments agree with the register network segments.")
    report.counts["rack_placements"] = len(layout["placements"])
    report.counts["rack_units_occupied"] = len(occupied)
    report.counts["rack_units_free"] = len(expected_free)


def check_asset_lifecycle(
    report: Report, documents: dict[str, Any], core: dict[str, Any], extra: set[str]
) -> None:
    """Lifecycle extension: key resolution, derivation consistency, no invented CAPEX."""
    name = "asset_lifecycle"
    if name not in documents:
        report.note("data/asset_lifecycle.yaml absent: lifecycle extension checks skipped.")
        return
    if not core:
        report.note("lifecycle extension checks skipped: core documents unavailable.")
        return

    lifecycle = documents[name]
    universe = core["asset_set"] | extra
    register_by_id = {asset["asset_id"]: asset for asset in core["register"]["assets"]}
    derivation = lifecycle["ownership_derivation"]["status_to_ownership"]

    money_and_dates = (
        "capex_reference",
        "acquisition_cost",
        "acquisition_currency",
        "purchase_date",
        "in_service_date",
        "warranty_expires",
        "replacement_cost",
        "expected_service_life_years",
    )

    observed = {"owned": 0, "planned": 0, "leased": 0, "unknown": 0}
    for asset_id, record in lifecycle["records"].items():
        check_asset_id_format(report, name, asset_id, "lifecycle")
        if asset_id not in universe:
            report.error(
                name, f"lifecycle record {asset_id} does not resolve to a register or extension asset"
            )
            continue
        observed[record["ownership_state"]] = observed.get(record["ownership_state"], 0) + 1

        register_asset = register_by_id.get(asset_id)
        if register_asset is not None:
            if record["register_status"] != register_asset["status"]:
                report.error(
                    name,
                    f"{asset_id}: register_status {record['register_status']!r} disagrees with the register "
                    f"({register_asset['status']!r})",
                )
            if record["criticality"] != register_asset["criticality"]:
                report.error(name, f"{asset_id}: criticality disagrees with the register")
            if record["ownership_basis"] == "derived_from_register_status":
                expected = derivation.get(register_asset["status"])
                if expected is not None and record["ownership_state"] != expected:
                    report.error(
                        name,
                        f"{asset_id}: ownership_state {record['ownership_state']!r} does not follow the declared "
                        f"derivation for status {register_asset['status']!r} (expected {expected!r})",
                    )
            if record["spares_policy_required"] != (
                register_asset["criticality"] in {"life_safety", "critical"}
            ):
                report.error(
                    name, f"{asset_id}: spares_policy_required does not follow the register criticality"
                )

        if record["data_status"] == "not_yet_imported":
            for key in money_and_dates:
                if record.get(key) is not None:
                    report.error(
                        name,
                        f"{asset_id}: {key} carries a value while data_status is not_yet_imported. "
                        "The package does not invent prices, dates or terms.",
                    )
            if record["spare_parts"]:
                report.error(
                    name, f"{asset_id}: spare_parts is populated while data_status is not_yet_imported"
                )

    declared = lifecycle["ownership_derivation"].get("counts")
    if declared is not None and {k: v for k, v in observed.items() if v or k in declared} != declared:
        report.error(name, f"ownership_derivation.counts {declared} disagrees with the records {observed}")

    missing = sorted(core["asset_set"] - set(lifecycle["records"]))
    if missing:
        report.warn(
            name,
            f"{len(missing)} register assets have no lifecycle record (first: {missing[0]})",
        )

    report.check("Lifecycle records resolve to register assets and follow the declared ownership derivation.")
    report.check("No lifecycle record carries a price, date or warranty term while marked not_yet_imported.")
    report.counts["lifecycle_records"] = len(lifecycle["records"])
    report.counts["lifecycle_owned"] = observed["owned"]
    report.counts["lifecycle_planned"] = observed["planned"]


def note_unknown_extensions(report: Report, documents: dict[str, Any]) -> None:
    known = set(CORE_DOCUMENTS) | set(EXTENSION_DOCUMENTS)
    for stem in sorted(set(documents) - known):
        report.note(
            f"data/{stem}.yaml has no dedicated cross-document check in this validator; "
            "schema and JSON-mirror checks still applied."
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def resolve_timestamp(argument: str | None) -> str:
    if argument:
        return argument
    from_environment = os.environ.get("CHAOS_VALIDATION_TIMESTAMP")
    if from_environment:
        return from_environment
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report_payload(report: Report, documents: dict[str, Any], timestamp: str, strict: bool) -> dict:
    schema_version = "unknown"
    register = documents.get("homestead_asset_register")
    if isinstance(register, dict):
        schema_version = str(register.get("schema_version", schema_version))

    failed = bool(report.errors) or (strict and bool(report.warnings))
    counts = dict(report.counts)
    counts["documents_validated"] = len(report.documents)
    counts["json_schemas"] = len({d.schema_path for d in report.documents.values() if d.schema_path})

    return {
        "validation_timestamp": timestamp,
        "schema_version": schema_version,
        "result": "FAIL" if failed else "PASS",
        "strict": strict,
        "counts": counts,
        "documents": [
            {
                "document": result.name,
                "data": result.data_path,
                "schema": result.schema_path,
                "json_mirror": result.json_path,
                "status": result.status,
                "counts": result.counts,
                "messages": result.messages,
            }
            for result in sorted(report.documents.values(), key=lambda r: r.name)
        ],
        "checks": report.checks,
        "notes": report.notes,
        "warnings": report.warnings,
        "errors": report.errors,
    }


def print_human_summary(payload: dict, report: Report) -> None:
    print("Homestead design-package validation")
    print("-" * 72)
    for entry in payload["documents"]:
        counts = ", ".join(f"{key}={value}" for key, value in sorted(entry["counts"].items()))
        schema = Path(entry["schema"]).name if entry["schema"] else "no schema"
        print(f"{entry['status']:<5} {entry['document']:<28} [{schema}] {counts}")
        for message in entry["messages"]:
            print(f"        {message}")
    print("-" * 72)
    for note in report.notes:
        print(f"NOTE  {note}")
    for warning in report.warnings:
        print(f"WARN  {warning}")
    for error in report.errors:
        print(f"ERROR {error}")
    print("-" * 72)
    summary = ", ".join(f"{key}={value}" for key, value in sorted(payload["counts"].items()))
    print(f"counts: {summary}")
    print(f"result: {payload['result']} ({len(report.errors)} errors, {len(report.warnings)} warnings)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the homestead machine-readable design package.")
    parser.add_argument("--json", action="store_true", help="Print the validation report as JSON.")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors.")
    parser.add_argument(
        "--timestamp",
        help="Report timestamp. Falls back to $CHAOS_VALIDATION_TIMESTAMP, then the current UTC time.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Package root containing data/ and schemas/. Defaults to this script's repository.",
    )
    parser.add_argument("--report", type=Path, help="Where to write validation_report.json.")
    parser.add_argument("--no-report", action="store_true", help="Do not write the report file.")
    args = parser.parse_args(argv)

    if args.root is not None:
        configure_root(args.root)
    report_path = args.report or REPORT_PATH

    report = Report()
    documents = discover_and_validate(report)
    core = check_core(report, documents)
    water_ids = check_water_assets(report, documents, core)
    check_water_points(report, documents, core)
    check_rack_layout(report, documents, core, water_ids)
    check_asset_lifecycle(report, documents, core, water_ids)
    note_unknown_extensions(report, documents)

    timestamp = resolve_timestamp(args.timestamp)
    payload = build_report_payload(report, documents, timestamp, args.strict)

    if not args.no_report:
        report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print_human_summary(payload, report)

    return 1 if payload["result"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
