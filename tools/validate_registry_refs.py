#!/usr/bin/env python3
"""Resolve asset-ID references made by extension documents against the register.

The v0.3 package keeps identity in one place: ``data/homestead_asset_register.yaml``.
Extension documents -- rack layout, water assets, lifecycle records, load schedule,
alarm definitions -- all point back at that register by ``asset_id``. This module
answers one question for any of them: does every asset ID this document mentions
actually exist?

It is used by ``tools/validate_bundle.py`` and is also runnable on its own::

    python3 tools/validate_registry_refs.py data/rack_layout.yaml
    python3 tools/validate_registry_refs.py --json data/*.yaml

Placeholder strings are skipped rather than reported. The package deliberately
records unknowns as ``TBD`` (SDD sections 46 and 47), so ``"TBD_secondary_source"``
in a ``feed_asset_id`` is a correctly recorded unknown, not a dangling reference.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

#: Section 25.2 canonical form: ``<domain>.<asset_class>.<location_or_system>.<instance>``.
ASSET_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){3}$")
#: What the v0.3 JSON Schemas actually permit: four components or deeper.
ASSET_ID_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){3,}$")

#: Keys whose string values are asset references.
REFERENCE_KEYS = {
    "asset_id",
    "parent_id",
    "from_asset_id",
    "to_asset_id",
    "site_id",
    "rack_asset_id",
    "pdu_asset_id",
    "switch_asset_id",
    "upstream_asset_id",
    "power_source_asset_id",
    "assigned_asset_id",
    "access_switch_asset_id",
    "backbone_switch_asset_id",
    "source_asset_id",
    "destination_asset_id",
    "feed_asset_id",
    "source_a_id",
    "source_b_id",
    "measured_power_point",
}

#: Keys whose value is a list of asset references.
REFERENCE_LIST_KEYS = {"vlan_asset_ids", "dependencies", "affected_asset_ids", "asset_ids"}


def looks_like_placeholder(value: str) -> bool:
    """True for the package's deliberate 'not decided yet' markers."""
    return "TBD" in value or value.strip() == "" or value.lower() in {"none", "null", "n/a"}


@dataclass
class Reference:
    path: str
    key: str
    value: str


@dataclass
class RefResult:
    document: str
    checked: int = 0
    skipped_placeholders: int = 0
    unresolved: list[Reference] = field(default_factory=list)
    malformed: list[Reference] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved and not self.malformed


def iter_references(node: Any, path: str = "$") -> Iterator[Reference]:
    """Walk a parsed document and yield every asset reference it makes."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if key in REFERENCE_KEYS and isinstance(value, str):
                yield Reference(child, key, value)
            elif key in REFERENCE_LIST_KEYS and isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, str):
                        yield Reference(f"{child}[{index}]", key, item)
            else:
                yield from iter_references(value, child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from iter_references(item, f"{path}[{index}]")


def load_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def register_asset_ids(register: dict) -> set[str]:
    return {asset["asset_id"] for asset in register.get("assets", [])}


def check_document(document: Any, known_ids: set[str], name: str) -> RefResult:
    """Resolve every asset reference in ``document`` against ``known_ids``."""
    result = RefResult(document=name)
    for reference in iter_references(document, "$"):
        if looks_like_placeholder(reference.value):
            result.skipped_placeholders += 1
            continue
        result.checked += 1
        if not ASSET_ID_SCHEMA_RE.match(reference.value):
            result.malformed.append(reference)
        elif reference.value not in known_ids:
            result.unresolved.append(reference)
    return result


def build_asset_universe(*documents: Any) -> set[str]:
    """Union of the asset IDs declared by a register and any extensions."""
    known: set[str] = set()
    for document in documents:
        if isinstance(document, dict):
            known |= register_asset_ids(document)
    return known


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("documents", nargs="*", type=Path, help="Extension documents to check.")
    parser.add_argument(
        "--register",
        type=Path,
        default=DATA_DIR / "homestead_asset_register.yaml",
        help="Base asset register (default: data/homestead_asset_register.yaml).",
    )
    parser.add_argument(
        "--extension",
        type=Path,
        action="append",
        default=[],
        help="Additional document whose assets also count as known identities. Repeatable.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    args = parser.parse_args(argv)

    register = load_document(args.register)
    extensions = [load_document(path) for path in args.extension]
    known = build_asset_universe(register, *extensions)

    targets = args.documents or [DATA_DIR / "rack_layout.yaml"]
    results = [check_document(load_document(path), known, path.name) for path in targets if path.exists()]

    if args.json:
        print(
            json.dumps(
                {
                    "known_asset_ids": len(known),
                    "documents": [
                        {
                            "document": r.document,
                            "checked": r.checked,
                            "skipped_placeholders": r.skipped_placeholders,
                            "unresolved": [vars(x) for x in r.unresolved],
                            "malformed": [vars(x) for x in r.malformed],
                            "ok": r.ok,
                        }
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    else:
        for result in results:
            status = "OK" if result.ok else "FAIL"
            print(
                f"{status:<4} {result.document}: {result.checked} references checked, "
                f"{result.skipped_placeholders} placeholders skipped"
            )
            for reference in result.malformed:
                print(f"       malformed  {reference.path} = {reference.value!r}")
            for reference in result.unresolved:
                print(f"       unresolved {reference.path} = {reference.value!r}")

    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
