"""Load the alarm definition set into the registry (SDD 14.3).

``data/alarm_definitions.yaml`` is the authored artifact and
``data/alarm_definitions.json`` its exact mirror, following the same convention
as the rest of the machine-readable design package. ``schemas/alarm_definitions.schema.json``
validates it; the repository validator pairs ``data/<x>.yaml`` with
``schemas/<x>.schema.json`` by filename.

Two loader responsibilities matter more than the plumbing:

* **Cross-reference.** A definition may only name a point that exists in
  ``data/point_dictionary.yaml`` and an asset that exists in
  ``data/homestead_asset_register.yaml``. An alarm on an invented point is worse
  than no alarm, because it looks like coverage.
* **Threshold provenance.** Every numeric threshold carries a
  ``threshold_status`` and a ``threshold_basis``. The battery chemistry, the
  generator rating and the measured site loads are deliberately unresolved in
  this package (README, "Deliberately unresolved"), so the shipped numbers are
  commissioning defaults and are labelled as such everywhere they surface.

The :class:`~chaos.models.alarms.AlarmDefinition` table has a column for
each SDD 14.3 field. The extra metadata this subsystem needs -- threshold
provenance, correlation window, notification routing, the guard conditions and
the data-quality link -- is carried in the definition's ``notes`` list as a
single ``{"meta": {...}}`` entry, so no contract-layer column had to change.
Use :func:`definition_meta` to read it back rather than indexing ``notes``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from chaos.config import DATA_DIR, SCHEMA_DIR, Settings
from chaos.models.alarms import SEVERITIES, AlarmDefinition
from chaos.models.registry import Asset, PointDefinition

logger = logging.getLogger(__name__)

DEFAULT_DEFINITIONS_PATH = DATA_DIR / "alarm_definitions.yaml"
DEFAULT_SCHEMA_PATH = SCHEMA_DIR / "alarm_definitions.schema.json"

#: Key under which the structured extras live inside ``AlarmDefinition.notes``.
META_KEY = "meta"

MAINTENANCE_BEHAVIOURS = ("suppress", "downgrade", "notify_only", "normal")
PARENT_SCOPES = ("same_asset", "related", "any")
COMPARISON_OPERATORS = ("lt", "le", "gt", "ge", "eq", "ne", "in", "not_in", "quality_in")
SET_OPERATORS = ("in", "not_in", "quality_in")


class DefinitionError(ValueError):
    """Raised when the definition document cannot be trusted."""


@dataclass
class DefinitionSyncResult:
    """Outcome of an idempotent load."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: str | None = None

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated) + len(self.unchanged)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "total": self.total,
            "created": sorted(self.created),
            "updated": sorted(self.updated),
            "unchanged": len(self.unchanged),
            "disabled": sorted(self.disabled),
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Document loading
# ---------------------------------------------------------------------------


def load_document(path: str | Path | None = None) -> dict[str, Any]:
    """Read the definition document from YAML or JSON."""
    path = Path(path) if path is not None else DEFAULT_DEFINITIONS_PATH
    if not path.exists():
        raise DefinitionError(f"Alarm definition document not found: {path}")
    text = path.read_text(encoding="utf-8")
    document = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    if not isinstance(document, dict) or "alarms" not in document:
        raise DefinitionError(f"{path} is not an alarm-definition document")
    return document


def _load_package_index(data_dir: Path) -> tuple[set[str], dict[str, dict], set[str]]:
    """Return (point names, assets by id, asset classes) from the design package."""
    point_path = data_dir / "point_dictionary.yaml"
    register_path = data_dir / "homestead_asset_register.yaml"
    points: set[str] = set()
    assets: dict[str, dict] = {}
    classes: set[str] = set()
    if point_path.exists():
        points = set(yaml.safe_load(point_path.read_text(encoding="utf-8"))["points"])
    if register_path.exists():
        register = yaml.safe_load(register_path.read_text(encoding="utf-8"))
        assets = {a["asset_id"]: a for a in register.get("assets", [])}
        classes = {a["asset_class"] for a in assets.values()}
    return points, assets, classes


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_document(
    document: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    session: Session | None = None,
) -> list[str]:
    """Cross-reference the document against the registry. Returns problem strings.

    Points and assets are checked against the design package on disk. When a
    populated database session is supplied, the loaded registry is checked too,
    which catches a definition that names an asset the deployment has not
    actually registered.
    """
    problems: list[str] = []
    data_dir = Path(data_dir) if data_dir is not None else DATA_DIR
    package_points, package_assets, package_classes = _load_package_index(data_dir)

    db_points: set[str] = set()
    db_assets: set[str] = set()
    db_classes: set[str] = set()
    if session is not None:
        db_points = set(session.scalars(select(PointDefinition.name)).all())
        db_assets = set(session.scalars(select(Asset.asset_id)).all())
        db_classes = set(session.scalars(select(Asset.asset_class)).all())

    known_points = package_points | db_points
    known_assets = set(package_assets) | db_assets
    known_classes = package_classes | db_classes

    alarms = document.get("alarms") or []
    keys = [a.get("alarm_key") for a in alarms]
    duplicates = {k for k in keys if keys.count(k) > 1}
    for key in sorted(duplicates):
        problems.append(f"duplicate alarm_key: {key}")

    def check_point(key: str, name: str | None, label: str) -> None:
        if name and known_points and name not in known_points:
            problems.append(f"{key}: {label} '{name}' is not in the point dictionary")

    def check_asset(key: str, asset_id: str | None, label: str) -> None:
        if asset_id and known_assets and asset_id not in known_assets:
            problems.append(f"{key}: {label} '{asset_id}' is not in the asset register")

    for alarm in alarms:
        key = alarm.get("alarm_key", "<missing alarm_key>")
        if alarm.get("severity") not in SEVERITIES:
            problems.append(f"{key}: severity {alarm.get('severity')!r} is not an SDD 14.1 severity")
        behaviour = alarm.get("maintenance_mode_behaviour")
        if behaviour not in MAINTENANCE_BEHAVIOURS:
            problems.append(f"{key}: unknown maintenance_mode_behaviour {behaviour!r}")
        scope = alarm.get("parent_scope", "related")
        if scope not in PARENT_SCOPES:
            problems.append(f"{key}: unknown parent_scope {scope!r}")

        # SDD 14.1: an emergency alarm reports a hazard whose protection runs
        # independently. Silencing it in maintenance mode, or letting it clear
        # itself, would misrepresent what the platform is for.
        if alarm.get("severity") == "emergency":
            if behaviour not in ("notify_only", "normal"):
                problems.append(
                    f"{key}: emergency severity must not be suppressed or downgraded in maintenance "
                    f"(SDD 14.1); got {behaviour!r}"
                )
            if not alarm.get("requires_manual_reset"):
                problems.append(f"{key}: emergency severity must require a manual reset")

        if alarm.get("asset_id") and alarm.get("asset_class"):
            problems.append(f"{key}: asset_id and asset_class are mutually exclusive scopes")

        check_point(key, alarm.get("point_name"), "point_name")
        check_asset(key, alarm.get("asset_id"), "asset_id")

        asset_class = alarm.get("asset_class")
        if asset_class and known_classes and asset_class not in known_classes:
            problems.append(f"{key}: asset_class '{asset_class}' matches no registered asset")
        if asset_class and package_assets:
            matches = [
                a
                for a in package_assets.values()
                if a["asset_class"] == asset_class and a["domain"] == alarm.get("domain")
            ]
            if not matches:
                problems.append(
                    f"{key}: asset_class '{asset_class}' in domain '{alarm.get('domain')}' "
                    "matches no asset in the register"
                )

        for label in ("trigger", "reset"):
            condition = alarm.get(label) or {}
            operator = condition.get("operator")
            if operator not in COMPARISON_OPERATORS:
                problems.append(f"{key}: unknown {label} operator {operator!r}")
            if operator in SET_OPERATORS and not condition.get("values"):
                problems.append(f"{key}: {label} operator '{operator}' requires 'values'")
            if operator not in SET_OPERATORS and "value" not in condition:
                problems.append(f"{key}: {label} operator '{operator}' requires 'value'")
            for guard in condition.get("guards") or []:
                check_asset(key, guard.get("asset_id"), f"{label} guard asset")
                check_point(key, guard.get("point_name"), f"{label} guard point")

        for asset_id in alarm.get("affected_assets") or []:
            check_asset(key, asset_id, "affected asset")
        for condition in alarm.get("suppression_conditions") or []:
            check_asset(key, condition.get("asset_id"), "suppression asset")
            check_point(key, condition.get("point_name"), "suppression point")
            # A definition that says "downgrade in maintenance" and also lists
            # maintenance as a suppression condition says two different things.
            # The evaluator would suppress; the document claims it would not.
            if (
                condition.get("type") == "operating_mode"
                and "maintenance" in {str(m).lower() for m in condition.get("modes") or []}
                and behaviour != "suppress"
            ):
                problems.append(
                    f"{key}: maintenance_mode_behaviour is '{behaviour}' but a suppression "
                    "condition also lists the 'maintenance' mode; the two contradict each other"
                )

        parent = alarm.get("parent_alarm_key")
        if parent and parent not in keys:
            problems.append(f"{key}: parent_alarm_key '{parent}' is not defined")
        if parent == key:
            problems.append(f"{key}: parent_alarm_key points at itself")
        quality_key = alarm.get("data_quality_alarm_key")
        if quality_key and quality_key not in keys:
            problems.append(f"{key}: data_quality_alarm_key '{quality_key}' is not defined")

        # A numeric trip point that does not say where it came from reads as a
        # decided value. It is not one.
        if alarm.get("trigger", {}).get("operator") in ("lt", "le", "gt", "ge"):
            if alarm.get("threshold_status") not in (
                "commissioning_default",
                "derived_from_design_document",
            ):
                problems.append(
                    f"{key}: numeric threshold must declare threshold_status "
                    "'commissioning_default' or 'derived_from_design_document'"
                )
            if not (alarm.get("threshold_basis") or "").strip():
                problems.append(f"{key}: numeric threshold must declare a threshold_basis")

    # Parent cycles would make correlation loop forever.
    parents = {a.get("alarm_key"): a.get("parent_alarm_key") for a in alarms}
    for key in parents:
        seen: set[str] = set()
        cursor = key
        while cursor and cursor not in seen:
            seen.add(cursor)
            cursor = parents.get(cursor)
        if cursor is not None and cursor in seen:
            problems.append(f"{key}: parent_alarm_key chain contains a cycle")

    for item in document.get("open_items") or []:
        for affected in item.get("affects") or []:
            if affected not in keys:
                problems.append(f"open_item '{item.get('id')}' names undefined alarm '{affected}'")

    return problems


def validate_schema(document: dict[str, Any], schema_path: str | Path | None = None) -> list[str]:
    """Validate against the JSON Schema. Returns problem strings (empty when valid)."""
    schema_path = Path(schema_path) if schema_path is not None else DEFAULT_SCHEMA_PATH
    if not schema_path.exists():
        return [f"schema not found: {schema_path}"]
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - jsonschema is a declared dependency
        return []
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    return [
        f"{'/'.join(str(p) for p in error.path)}: {error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    ]


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _condition_payload(condition: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise a trigger/reset block into the JSON payload stored on the row."""
    if not condition:
        return None
    payload: dict[str, Any] = {}
    if "value" in condition:
        payload["value"] = condition["value"]
    if "values" in condition:
        payload["values"] = list(condition["values"])
    if condition.get("guards"):
        payload["guards"] = [dict(g) for g in condition["guards"]]
    return payload


def _meta_payload(alarm: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    defaults = document.get("correlation_defaults") or {}
    correlation = dict(alarm.get("correlation") or {})
    return {
        "threshold_status": alarm.get("threshold_status", "not_applicable"),
        "threshold_basis": alarm.get("threshold_basis"),
        "procedure_status": alarm.get("procedure_status", "not_yet_written"),
        "parent_scope": alarm.get("parent_scope", "related"),
        "data_quality_alarm_key": alarm.get("data_quality_alarm_key"),
        "renotify_after_s": int(alarm.get("renotify_after_s") or 0),
        "source_sections": list(alarm.get("source_sections") or []),
        "correlation": {
            "window_s": int(correlation.get("window_s", defaults.get("window_s", 900))),
            "flood_threshold": int(correlation.get("flood_threshold", defaults.get("flood_threshold", 5))),
            "is_root_candidate": bool(correlation.get("is_root_candidate", False)),
        },
    }


def normalise_definition(alarm: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """Map one document entry onto :class:`AlarmDefinition` column values."""
    notes: list[Any] = list(alarm.get("notes") or [])
    notes.append({META_KEY: _meta_payload(alarm, document)})
    trigger = alarm.get("trigger") or {}
    reset = alarm.get("reset") or {}
    return {
        "alarm_key": alarm["alarm_key"],
        "name": alarm["name"],
        "severity": alarm["severity"],
        "domain": alarm.get("domain"),
        "point_name": alarm.get("point_name"),
        "asset_id": alarm.get("asset_id"),
        "asset_class": alarm.get("asset_class"),
        "trigger_operator": trigger.get("operator"),
        "trigger_value": _condition_payload(trigger),
        "reset_operator": reset.get("operator"),
        "reset_value": _condition_payload(reset),
        "trigger_expression": alarm.get("trigger_expression"),
        "on_delay_s": int(alarm.get("on_delay_s") or 0),
        "off_delay_s": int(alarm.get("off_delay_s") or 0),
        "hysteresis": alarm.get("hysteresis"),
        "affected_assets": list(alarm.get("affected_assets") or []),
        "probable_causes": list(alarm.get("probable_causes") or []),
        "automatic_action": alarm.get("automatic_action"),
        "operator_action": alarm.get("operator_action"),
        "procedure_ref": alarm.get("procedure_ref"),
        "escalation_path": [dict(s) for s in alarm.get("escalation_path") or []],
        "suppression_conditions": [dict(c) for c in alarm.get("suppression_conditions") or []],
        "maintenance_mode_behaviour": alarm.get("maintenance_mode_behaviour", "suppress"),
        "parent_alarm_key": alarm.get("parent_alarm_key"),
        "requires_manual_reset": bool(alarm.get("requires_manual_reset", False)),
        "enabled": bool(alarm.get("enabled", True)),
        "notes": notes,
    }


def definition_meta(definition: AlarmDefinition) -> dict[str, Any]:
    """Read back the structured metadata carried in ``notes``."""
    for note in definition.notes or []:
        if isinstance(note, dict) and META_KEY in note:
            return dict(note[META_KEY])
    return {}


def definition_notes(definition: AlarmDefinition) -> list[str]:
    """The human-readable notes, without the metadata block."""
    return [n for n in (definition.notes or []) if isinstance(n, str)]


def correlation_settings(definition: AlarmDefinition) -> dict[str, Any]:
    meta = definition_meta(definition)
    correlation = meta.get("correlation") or {}
    return {
        "window_s": int(correlation.get("window_s", 900)),
        "flood_threshold": int(correlation.get("flood_threshold", 5)),
        "is_root_candidate": bool(correlation.get("is_root_candidate", False)),
    }


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

_MUTABLE_COLUMNS = (
    "name",
    "severity",
    "domain",
    "point_name",
    "asset_id",
    "asset_class",
    "trigger_operator",
    "trigger_value",
    "reset_operator",
    "reset_value",
    "trigger_expression",
    "on_delay_s",
    "off_delay_s",
    "hysteresis",
    "affected_assets",
    "probable_causes",
    "automatic_action",
    "operator_action",
    "procedure_ref",
    "escalation_path",
    "suppression_conditions",
    "maintenance_mode_behaviour",
    "parent_alarm_key",
    "requires_manual_reset",
    "enabled",
    "notes",
)


def sync_definitions(
    session: Session,
    *,
    path: str | Path | None = None,
    data_dir: str | Path | None = None,
    settings: Settings | None = None,
    strict: bool = True,
    validate_registry: bool = True,
    disable_missing: bool = True,
) -> DefinitionSyncResult:
    """Load the definition document into ``alarm_definitions``, idempotently.

    Running this twice over an unchanged document reports every definition as
    ``unchanged`` and writes nothing.

    A definition that has been removed from the document is disabled rather than
    deleted: alarm history references it by foreign key, and SDD 16.4 keeps the
    alarm audit indefinitely.
    """
    if path is None and settings is not None:
        candidate = Path(settings.data_dir) / "alarm_definitions.yaml"
        if candidate.exists():
            path = candidate
    if data_dir is None and settings is not None:
        data_dir = settings.data_dir

    resolved = Path(path) if path is not None else DEFAULT_DEFINITIONS_PATH
    document = load_document(resolved)
    result = DefinitionSyncResult(source=str(resolved))

    if validate_registry:
        problems = validate_document(document, data_dir=data_dir, session=session)
        if problems:
            if strict:
                raise DefinitionError(
                    "Alarm definitions failed cross-reference validation:\n  - " + "\n  - ".join(problems)
                )
            result.warnings.extend(problems)

    existing = {d.alarm_key: d for d in session.scalars(select(AlarmDefinition)).all()}
    seen: set[str] = set()

    for entry in document.get("alarms") or []:
        values = normalise_definition(entry, document)
        key = values["alarm_key"]
        seen.add(key)
        row = existing.get(key)
        if row is None:
            session.add(AlarmDefinition(**values))
            result.created.append(key)
            continue
        changed = False
        for column in _MUTABLE_COLUMNS:
            if getattr(row, column) != values[column]:
                setattr(row, column, values[column])
                changed = True
        (result.updated if changed else result.unchanged).append(key)

    if disable_missing:
        for key, row in existing.items():
            if key not in seen and row.enabled:
                row.enabled = False
                result.disabled.append(key)

    session.flush()
    logger.info(
        "Alarm definitions synced from %s: %d created, %d updated, %d unchanged, %d disabled",
        resolved,
        len(result.created),
        len(result.updated),
        len(result.unchanged),
        len(result.disabled),
    )
    return result


def ensure_definitions(
    session: Session,
    *,
    path: str | Path | None = None,
    settings: Settings | None = None,
    strict: bool = False,
) -> DefinitionSyncResult | None:
    """Load definitions only if the table is empty. Safe to call on every cycle."""
    if session.scalar(select(AlarmDefinition.alarm_key).limit(1)) is not None:
        return None
    return sync_definitions(session, path=path, settings=settings, strict=strict)


def load_definitions(session: Session, *, enabled_only: bool = True) -> list[AlarmDefinition]:
    statement = select(AlarmDefinition)
    if enabled_only:
        statement = statement.where(AlarmDefinition.enabled.is_(True))
    return list(session.scalars(statement.order_by(AlarmDefinition.alarm_key)).all())
