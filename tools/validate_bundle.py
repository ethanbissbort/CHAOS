#!/usr/bin/env python3
from __future__ import annotations
import json
import sys
from pathlib import Path
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
PAIRS = [
    (ROOT / "data/asset_class_dictionary.yaml", ROOT / "schemas/asset_class_dictionary.schema.json"),
    (ROOT / "data/point_dictionary.yaml", ROOT / "schemas/point_dictionary.schema.json"),
    (ROOT / "data/homestead_asset_register.yaml", ROOT / "schemas/asset_register.schema.json"),
    (ROOT / "data/point_bindings.yaml", ROOT / "schemas/point_bindings.schema.json"),
]

def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))

def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def fail(msg: str):
    print(f"ERROR: {msg}")
    sys.exit(1)

for data_path, schema_path in PAIRS:
    data = load_yaml(data_path)
    schema = load_json(schema_path)
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        for error in errors:
            print(f"{data_path.name}: {'/'.join(map(str,error.path))}: {error.message}")
        fail(f"Schema validation failed for {data_path.name}")
    print(f"OK schema: {data_path.name}")

asset_dict = load_yaml(ROOT / "data/asset_class_dictionary.yaml")
point_dict = load_yaml(ROOT / "data/point_dictionary.yaml")
register = load_yaml(ROOT / "data/homestead_asset_register.yaml")
bindings = load_yaml(ROOT / "data/point_bindings.yaml")

asset_ids = [a["asset_id"] for a in register["assets"]]
if len(asset_ids) != len(set(asset_ids)):
    fail("Duplicate asset_id values")
asset_set = set(asset_ids)
if register["site_id"] not in asset_set:
    fail("site_id does not reference an asset")

class_set = set(asset_dict["asset_classes"])
profile_set = set(asset_dict["point_profiles"])
point_set = set(point_dict["points"])
for asset in register["assets"]:
    if asset["asset_class"] not in class_set:
        fail(f"Unknown asset class: {asset['asset_id']} -> {asset['asset_class']}")
    if asset["domain"] not in asset_dict["asset_classes"][asset["asset_class"]]["allowed_domains"]:
        fail(f"Domain/class mismatch: {asset['asset_id']}")
    if asset["parent_id"] is not None and asset["parent_id"] not in asset_set:
        fail(f"Missing parent: {asset['asset_id']} -> {asset['parent_id']}")
    for profile in asset["point_profile_refs"]:
        if profile not in profile_set:
            fail(f"Unknown point profile: {asset['asset_id']} -> {profile}")

relationship_ids = []
for relation in register["relationships"]:
    relationship_ids.append(relation["relationship_id"])
    if relation["from_asset_id"] not in asset_set:
        fail(f"Relationship source missing: {relation}")
    if relation["to_asset_id"] not in asset_set:
        fail(f"Relationship target missing: {relation}")
if len(relationship_ids) != len(set(relationship_ids)):
    fail("Duplicate relationship_id values")

binding_ids = []
for binding in bindings["bindings"]:
    binding_ids.append(binding["point_id"])
    if binding["asset_id"] not in asset_set:
        fail(f"Binding asset missing: {binding['point_id']}")
    if binding["point_name"] not in point_set:
        fail(f"Binding point missing from dictionary: {binding['point_id']}")
    if binding["point_id"] != f"{binding['asset_id']}/{binding['point_name']}":
        fail(f"Binding point_id mismatch: {binding['point_id']}")
if len(binding_ids) != len(set(binding_ids)):
    fail("Duplicate point_id bindings")

for class_name, class_def in asset_dict["asset_classes"].items():
    for point_name in class_def["default_points"]:
        if point_name not in point_set:
            fail(f"Class {class_name} references undefined point {point_name}")
for profile_name, profile in asset_dict["point_profiles"].items():
    for point_name in profile:
        if point_name not in point_set:
            fail(f"Profile {profile_name} references undefined point {point_name}")

print(f"OK cross-reference: {len(asset_set)} assets, {len(register['relationships'])} relationships, {len(point_set)} point definitions, {len(binding_ids)} bindings")
