"""Load the v0.3 machine-readable design package into the registry database.

The package (SDD section 44) is four YAML documents:

``asset_class_dictionary.yaml``
    Approved domains, lifecycle vocabulary, criticality, control authority,
    relationship types, 72 asset classes and 5 reusable point profiles.
``point_dictionary.yaml``
    214 canonical point definitions with class, type, unit and control
    capability.
``homestead_asset_register.yaml``
    90 assets and 99 typed relationships presently known from the plans.
``point_bindings.yaml``
    245 protocol placeholders connecting a canonical point to a device
    interface without touching identity.

The loader is idempotent: every row is written by primary key, so re-running it
converges the database onto the package rather than duplicating it. That makes
``POST /registry/reload`` safe and makes the package -- not the database -- the
source of truth for identity.

Nothing is invented. Unknown addresses, coordinates and models stay ``TBD`` or
``None`` (SDD sections 46 and 47).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from chaos.config import Settings, get_settings
from chaos.models import (
    Asset,
    AssetClass,
    AssetRelationship,
    ConfigurationRevision,
    ExternalIdentifier,
    Location,
    Point,
    PointBinding,
    PointDefinition,
    PointProfile,
    PointSampleIndex,
)
from chaos.registry.points import SOURCE_BINDING, materialize_points

logger = logging.getLogger(__name__)

ASSET_CLASS_FILE = "asset_class_dictionary.yaml"
POINT_DICTIONARY_FILE = "point_dictionary.yaml"
ASSET_REGISTER_FILE = "homestead_asset_register.yaml"
POINT_BINDINGS_FILE = "point_bindings.yaml"

#: Placeholder used throughout the package for a value that is not yet known.
TBD = "TBD"

#: Location keys projected onto dedicated ``locations`` columns.
_LOCATION_ID_KEYS = ("structure_id", "room_id", "rack_id")
#: Location keys kept only in the asset's ``location`` JSON and summarised in
#: ``locations.description`` (there is no column for them).
_LOCATION_CONTEXT_KEYS = ("zone_id", "row_number", "structure_or_zone", "coordinates_status")


class RegistryLoadError(RuntimeError):
    """Raised when the package contains structural problems.

    Structural means the graph cannot be represented faithfully: an unknown
    asset class, a domain the class does not allow, an unknown point profile, a
    dangling parent or relationship endpoint, or a containment cycle. Anything
    the loader had to skip is listed in :attr:`warnings`.
    """

    def __init__(self, message: str, warnings: Sequence[str], result: LoadResult | None = None):
        super().__init__(message)
        self.warnings = list(warnings)
        self.result = result


@dataclass
class LoadResult:
    """Counts and warnings for one package load."""

    asset_classes: int = 0
    point_definitions: int = 0
    point_profiles: int = 0
    assets: int = 0
    relationships: int = 0
    points: int = 0
    bindings: int = 0
    warnings: list[str] = field(default_factory=list)

    # Provenance -- useful in the API response and the audit trail.
    package_version: str | None = None
    data_dir: str | None = None
    revision: int | None = None
    locations: int = 0
    external_identifiers: int = 0
    skipped_assets: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.warnings

    def counts(self) -> dict[str, int]:
        return {
            "asset_classes": self.asset_classes,
            "point_definitions": self.point_definitions,
            "point_profiles": self.point_profiles,
            "assets": self.assets,
            "relationships": self.relationships,
            "points": self.points,
            "bindings": self.bindings,
            "locations": self.locations,
            "external_identifiers": self.external_identifiers,
        }


# ---------------------------------------------------------------------------
# Reading the package off disk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistryPackage:
    """The four parsed YAML documents plus where they came from."""

    data_dir: Path
    asset_class_dictionary: dict[str, Any]
    point_dictionary: dict[str, Any]
    register: dict[str, Any]
    point_bindings: dict[str, Any]

    @property
    def schema_version(self) -> str | None:
        return self.register.get("schema_version")

    @property
    def site_id(self) -> str | None:
        return self.register.get("site_id")

    @property
    def design_basis(self) -> dict[str, Any]:
        return self.register.get("design_basis") or {}

    @property
    def open_design_conflicts(self) -> list[dict[str, Any]]:
        return list(self.design_basis.get("open_design_conflicts") or ())

    @property
    def asset_classes(self) -> dict[str, Any]:
        return self.asset_class_dictionary.get("asset_classes") or {}

    @property
    def point_profiles(self) -> dict[str, list[str]]:
        return self.asset_class_dictionary.get("point_profiles") or {}

    @property
    def point_definitions(self) -> dict[str, Any]:
        return self.point_dictionary.get("points") or {}

    @property
    def assets(self) -> list[dict[str, Any]]:
        return list(self.register.get("assets") or ())

    @property
    def relationships(self) -> list[dict[str, Any]]:
        return list(self.register.get("relationships") or ())

    @property
    def bindings(self) -> list[dict[str, Any]]:
        return list(self.point_bindings.get("bindings") or ())

    def bindings_by_asset(self) -> dict[str, dict[str, dict[str, Any]]]:
        grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for record in self.bindings:
            grouped[record["asset_id"]][record["point_name"]] = record
        return dict(grouped)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RegistryLoadError(f"Design package file missing: {path}", [f"missing file: {path}"])
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RegistryLoadError(f"Design package file is not a mapping: {path}", [str(path)])
    return data


_PACKAGE_FILES = (
    ASSET_CLASS_FILE,
    POINT_DICTIONARY_FILE,
    ASSET_REGISTER_FILE,
    POINT_BINDINGS_FILE,
)

#: Parsed packages keyed by directory, invalidated on file mtime/size.
#: Parsing ~1400 YAML nodes costs about a second, and the dictionary and
#: design-conflict endpoints read the package on every request.
_PACKAGE_CACHE: dict[Path, tuple[tuple, RegistryPackage]] = {}


def _package_fingerprint(root: Path) -> tuple:
    stamps = []
    for name in _PACKAGE_FILES:
        path = root / name
        try:
            stat = path.stat()
        except OSError:
            stamps.append((name, None, None))
        else:
            stamps.append((name, stat.st_mtime_ns, stat.st_size))
    return tuple(stamps)


def read_package(
    data_dir: Path | str | None = None,
    *,
    settings: Settings | None = None,
    use_cache: bool = True,
) -> RegistryPackage:
    """Parse the four package documents from ``data_dir``.

    The returned object is shared between callers and must be treated as
    read-only; it is re-parsed whenever a package file changes on disk.
    """
    if data_dir is None:
        data_dir = (settings or get_settings()).data_dir
    root = Path(data_dir).resolve()

    fingerprint = _package_fingerprint(root)
    if use_cache:
        cached = _PACKAGE_CACHE.get(root)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]

    package = RegistryPackage(
        data_dir=root,
        asset_class_dictionary=_read_yaml(root / ASSET_CLASS_FILE),
        point_dictionary=_read_yaml(root / POINT_DICTIONARY_FILE),
        register=_read_yaml(root / ASSET_REGISTER_FILE),
        point_bindings=_read_yaml(root / POINT_BINDINGS_FILE),
    )
    _PACKAGE_CACHE[root] = (fingerprint, package)
    return package


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_tbd(value: Any) -> bool:
    """``TBD`` is a real answer in this package: 'not decided yet'."""
    return isinstance(value, str) and value.strip().upper() == TBD


def _clean_id(value: Any) -> str | None:
    if value is None or _is_tbd(value) or not isinstance(value, str):
        return None
    return value.strip() or None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def topological_order(assets: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    """Group asset IDs into containment levels, parents first.

    Returns a list of levels; every asset in level *n* has its parent in a
    level < *n*. Assets whose ``parent_id`` is not part of ``assets`` are
    treated as roots here -- the caller reports them as dangling separately.

    Raises :class:`RegistryLoadError` when the containment graph has a cycle.
    """
    known = {record["asset_id"] for record in assets}
    parents: dict[str, str | None] = {}
    for record in assets:
        parent = record.get("parent_id")
        parents[record["asset_id"]] = parent if parent in known else None

    children: dict[str | None, list[str]] = defaultdict(list)
    for asset_id, parent in parents.items():
        children[parent].append(asset_id)

    levels: list[list[str]] = []
    placed: set[str] = set()
    frontier = sorted(children.get(None, []))
    while frontier:
        levels.append(frontier)
        placed.update(frontier)
        next_frontier: list[str] = []
        for asset_id in frontier:
            next_frontier.extend(sorted(children.get(asset_id, [])))
        frontier = next_frontier

    if len(placed) != len(parents):
        stranded = sorted(set(parents) - placed)
        raise RegistryLoadError(
            "Containment cycle detected in the asset register: " + ", ".join(stranded),
            [f"containment cycle involves {asset_id}" for asset_id in stranded],
        )
    return levels


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_package(
    session: Session,
    data_dir: Path | str | None = None,
    *,
    settings: Settings | None = None,
    changed_by: str = "registry.loader",
    reason: str | None = None,
) -> LoadResult:
    """Load the machine-readable design package into ``session``.

    The session is flushed but **not** committed; the caller owns the
    transaction boundary.
    """
    settings = settings or get_settings()
    package = read_package(data_dir if data_dir is not None else settings.data_dir, settings=settings)

    result = LoadResult(
        package_version=package.schema_version,
        data_dir=str(package.data_dir),
    )
    structural: list[str] = []

    def warn(message: str, *, structural_issue: bool = False) -> None:
        result.warnings.append(message)
        if structural_issue:
            structural.append(message)
        logger.warning("registry load: %s", message)

    _check_versions(package, warn)
    write = _RowWriter(session)
    write.prime()

    _load_dictionaries(write, package, result)
    session.flush()

    loaded_assets = _load_assets(session, write, package, result, warn)
    _load_relationships(write, package, result, loaded_assets, warn)
    session.flush()

    _load_points(session, write, package, result, loaded_assets, warn, settings)
    session.flush()

    result.revision = _record_revision(session, package, result, settings, changed_by, reason)
    session.flush()

    if structural:
        raise RegistryLoadError(
            f"Design package has {len(structural)} structural problem(s); "
            f"{len(result.warnings)} warning(s) recorded",
            result.warnings,
            result,
        )
    return result


def _check_versions(package: RegistryPackage, warn) -> None:
    versions = {
        ASSET_CLASS_FILE: package.asset_class_dictionary.get("schema_version"),
        POINT_DICTIONARY_FILE: package.point_dictionary.get("schema_version"),
        ASSET_REGISTER_FILE: package.register.get("schema_version"),
        POINT_BINDINGS_FILE: package.point_bindings.get("schema_version"),
    }
    distinct = {v for v in versions.values() if v is not None}
    if len(distinct) > 1:
        warn(f"Package documents disagree on schema_version: {versions}")


#: Registry tables the loader writes by primary key, with that key's attribute.
_UPSERT_MODELS: tuple[tuple[type, str], ...] = (
    (AssetClass, "name"),
    (PointProfile, "name"),
    (PointDefinition, "name"),
    (Asset, "asset_id"),
    (AssetRelationship, "relationship_id"),
    (Location, "asset_id"),
    (Point, "point_id"),
    (PointBinding, "point_id"),
    (PointSampleIndex, "point_id"),
)


class _RowWriter:
    """Primary-key upsert without a SELECT per row.

    ``Session.merge`` is the natural way to express "insert or update by
    primary key", but it issues a SELECT (and an autoflush) for every object.
    A full package is ~2200 rows, so that is ~2200 round trips per load.

    Priming the identity map once per table makes ``merge`` resolve existing
    rows in memory, and remembering which keys exist lets genuinely new rows go
    straight to ``Session.add``. A repeated primary key inside one load updates
    the pending instance instead of inserting a duplicate.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._pk_attr: dict[type, str] = {}
        self._present: dict[type, set[Any]] = {}
        self._added: dict[type, dict[Any, Any]] = {}

    def prime(self, models: Iterable[tuple[type, str]] = _UPSERT_MODELS) -> None:
        for model, pk_attr in models:
            self._pk_attr[model] = pk_attr
            rows = self._session.scalars(select(model)).all()  # fills the identity map
            self._present[model] = {getattr(row, pk_attr) for row in rows}
            self._added[model] = {}

    def __call__(self, obj: Any) -> Any:
        model = type(obj)
        pk_attr = self._pk_attr.get(model)
        if pk_attr is None:  # pragma: no cover - defensive
            return self._session.merge(obj)

        key = getattr(obj, pk_attr)
        pending = self._added[model].get(key)
        if pending is not None:
            _copy_columns(obj, pending)
            return pending
        if key in self._present[model]:
            return self._session.merge(obj)

        self._session.add(obj)
        self._present[model].add(key)
        self._added[model][key] = obj
        return obj


def _copy_columns(source: Any, target: Any) -> None:
    """Copy the column values that were actually set on ``source``."""
    mapper = sa_inspect(type(source))
    for attr in mapper.column_attrs:
        if attr.key in source.__dict__:
            setattr(target, attr.key, source.__dict__[attr.key])


# --- dictionaries ----------------------------------------------------------


def _load_dictionaries(write: _RowWriter, package: RegistryPackage, result: LoadResult) -> None:
    for name, definition in package.asset_classes.items():
        write(
            AssetClass(
                name=name,
                purpose=definition.get("purpose"),
                allowed_domains=list(definition.get("allowed_domains") or ()),
                required_properties=list(definition.get("required_properties") or ()),
                default_points=list(definition.get("default_points") or ()),
                source_section=definition.get("source_section"),
                dictionary_status=definition.get("dictionary_status"),
            )
        )
        result.asset_classes += 1

    for name, point_names in package.point_profiles.items():
        write(PointProfile(name=name, point_names=list(point_names or ())))
        result.point_profiles += 1

    for name, definition in package.point_definitions.items():
        enum_values = definition.get("enum_values")
        write(
            PointDefinition(
                name=name,
                default_class=definition["default_class"],
                allowed_classes=list(definition.get("allowed_classes") or ()),
                data_type=definition["data_type"],
                unit=definition.get("unit"),
                description=definition.get("description"),
                control_capable=bool(definition.get("control_capable")),
                automatic_control_default=bool(definition.get("automatic_control_default")),
                enum_values=list(enum_values) if enum_values else None,
                applicable_asset_classes=list(definition.get("applicable_asset_classes") or ()),
                source_section=definition.get("source_section"),
                dictionary_status=definition.get("dictionary_status"),
            )
        )
        result.point_definitions += 1


# --- assets ----------------------------------------------------------------


def _load_assets(
    session: Session,
    write: _RowWriter,
    package: RegistryPackage,
    result: LoadResult,
    warn,
) -> dict[str, dict[str, Any]]:
    """Validate, order and write the asset rows. Returns the accepted assets."""
    records = {record["asset_id"]: record for record in package.assets}
    if len(records) != len(package.assets):
        warn("Duplicate asset_id values in the register", structural_issue=True)

    classes = package.asset_classes
    profiles = package.point_profiles
    domains = package.asset_class_dictionary.get("domains") or {}
    statuses = set(package.asset_class_dictionary.get("lifecycle_statuses") or ())
    criticalities = set(package.asset_class_dictionary.get("criticality_levels") or ())
    authorities = set(package.asset_class_dictionary.get("control_authorities") or ())

    rejected: set[str] = set()
    for asset_id, record in records.items():
        class_name = record.get("asset_class")
        class_def = classes.get(class_name)
        if class_def is None:
            warn(f"Asset {asset_id}: unknown asset class {class_name!r}", structural_issue=True)
            rejected.add(asset_id)
            continue
        if record.get("domain") not in (class_def.get("allowed_domains") or ()):
            warn(
                f"Asset {asset_id}: domain {record.get('domain')!r} is not allowed for "
                f"asset class {class_name!r}",
                structural_issue=True,
            )
            rejected.add(asset_id)
        for profile_ref in record.get("point_profile_refs") or ():
            if profile_ref not in profiles:
                warn(
                    f"Asset {asset_id}: unknown point profile {profile_ref!r}",
                    structural_issue=True,
                )
                rejected.add(asset_id)
        parent_id = record.get("parent_id")
        if parent_id is not None and parent_id not in records:
            warn(
                f"Asset {asset_id}: parent {parent_id!r} is not in the register",
                structural_issue=True,
            )
            rejected.add(asset_id)

        # Vocabulary drift is worth reporting but does not break the graph.
        if record.get("domain") not in domains:
            warn(f"Asset {asset_id}: domain {record.get('domain')!r} is not in the domain vocabulary")
        if statuses and record.get("status") not in statuses:
            warn(f"Asset {asset_id}: status {record.get('status')!r} is not a lifecycle status")
        if criticalities and record.get("criticality") not in criticalities:
            warn(f"Asset {asset_id}: criticality {record.get('criticality')!r} is not in the vocabulary")
        if authorities and record.get("control_authority") not in authorities:
            warn(
                f"Asset {asset_id}: control_authority {record.get('control_authority')!r} "
                "is not in the vocabulary"
            )

    # A rejected asset takes its whole subtree with it -- a child cannot be
    # inserted without its parent row.
    changed = True
    while changed:
        changed = False
        for asset_id, record in records.items():
            parent_id = record.get("parent_id")
            if asset_id not in rejected and parent_id in rejected:
                rejected.add(asset_id)
                warn(f"Asset {asset_id}: skipped because parent {parent_id!r} was skipped")
                changed = True

    accepted = {aid: rec for aid, rec in records.items() if aid not in rejected}
    result.skipped_assets = sorted(rejected)

    for level in topological_order(list(accepted.values())):
        for asset_id in level:
            record = accepted[asset_id]
            write(_build_asset(record))
            result.assets += 1
        # Parents must exist before their children (assets.parent_id FK).
        session.flush()

    for asset_id, record in accepted.items():
        if _upsert_location(write, record):
            result.locations += 1
        result.external_identifiers += _upsert_external_identifiers(session, record)
    session.flush()
    return accepted


def _build_asset(record: Mapping[str, Any]) -> Asset:
    return Asset(
        asset_id=record["asset_id"],
        domain=record["domain"],
        asset_class=record["asset_class"],
        name=record["name"],
        status=record.get("status") or "concept",
        criticality=record.get("criticality") or "discretionary",
        control_authority=record.get("control_authority") or "none",
        functional_position=bool(record.get("functional_position", True)),
        parent_id=record.get("parent_id"),
        location=dict(record.get("location") or {}),
        properties=dict(record.get("properties") or {}),
        network=dict(record.get("network") or {}),
        power=dict(record.get("power") or {}),
        dependencies=list(record.get("dependencies") or ()),
        manual_override=dict(record.get("manual_override") or {}),
        documentation=list(record.get("documentation") or ()),
        maintenance_plan=dict(record.get("maintenance_plan") or {}),
        point_profile_refs=list(record.get("point_profile_refs") or ()),
        source_refs=list(record.get("source_refs") or ()),
        open_fields=list(record.get("open_fields") or ()),
        tags=list(record.get("tags") or ()),
        notes=list(record.get("notes") or ()),
    )


def _upsert_location(write: _RowWriter, record: Mapping[str, Any]) -> bool:
    """Project the asset's ``location`` dict onto a ``locations`` row.

    Coordinates are written only when the source actually supplies numbers.
    The register currently records ``coordinates_status: TBD`` for every
    outdoor asset, so latitude/longitude stay ``None`` (SDD section 46).
    """
    location = record.get("location") or {}
    if not location:
        return False

    row = Location(asset_id=record["asset_id"])
    for key in _LOCATION_ID_KEYS:
        setattr(row, key, _clean_id(location.get(key)))

    row.latitude = _as_float(location.get("latitude"))
    row.longitude = _as_float(location.get("longitude"))
    row.elevation_m = _as_float(location.get("elevation_m"))
    coordinates = location.get("coordinates")
    if row.latitude is None and isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
        row.latitude = _as_float(coordinates[0])
        row.longitude = _as_float(coordinates[1])
    geometry = location.get("geometry")
    row.geometry = geometry if isinstance(geometry, dict) else None

    rack_units = location.get("rack_units")
    if isinstance(rack_units, Mapping):
        row.rack_unit_start = _as_int(rack_units.get("start"))
        row.rack_unit_height = _as_int(rack_units.get("height"))
    else:
        # ``rack_units: TBD`` is the current state of the register -- no
        # rack-unit positions have been assigned yet (SDD section 49 item 4).
        row.rack_unit_start = _as_int(rack_units)
        row.rack_unit_height = _as_int(location.get("rack_unit_height"))

    context = [
        f"{key}={location[key]}"
        for key in _LOCATION_CONTEXT_KEYS
        if key in location and location[key] is not None
    ]
    row.description = "; ".join(context) or None

    write(row)
    return True


def _upsert_external_identifiers(session: Session, record: Mapping[str, Any]) -> int:
    """Serials, MACs, IPs, Modbus unit IDs -- never the canonical identity.

    Accepts both the explicit ``{id_type, value}`` form and the flat
    ``{serial_number: ...}`` form. ``TBD`` values are not stored: an unknown
    serial number is not an identifier.
    """
    asset_id = record["asset_id"]
    written = 0
    for entry in record.get("external_identifiers") or ():
        if not isinstance(entry, Mapping):
            continue
        pairs: list[tuple[str, Any]] = []
        if "value" in entry:
            id_type = entry.get("id_type") or entry.get("type")
            if id_type:
                pairs.append((str(id_type), entry["value"]))
        else:
            reserved = {"valid_from", "valid_to", "notes", "id_type", "type"}
            pairs.extend((str(k), v) for k, v in entry.items() if k not in reserved)

        for id_type, value in pairs:
            if value is None or _is_tbd(value):
                continue
            existing = session.scalar(
                select(ExternalIdentifier).where(
                    ExternalIdentifier.asset_id == asset_id,
                    ExternalIdentifier.id_type == id_type,
                    ExternalIdentifier.value == str(value),
                )
            )
            if existing is None:
                session.add(
                    ExternalIdentifier(
                        asset_id=asset_id,
                        id_type=id_type,
                        value=str(value),
                        notes=entry.get("notes") if isinstance(entry.get("notes"), str) else None,
                    )
                )
            written += 1
    return written


# --- relationships ---------------------------------------------------------


def _load_relationships(
    write: _RowWriter,
    package: RegistryPackage,
    result: LoadResult,
    assets: Mapping[str, Any],
    warn,
) -> None:
    known_types = set(package.asset_class_dictionary.get("relationship_types") or ())
    seen: set[str] = set()
    for record in package.relationships:
        relationship_id = record["relationship_id"]
        if relationship_id in seen:
            warn(f"Duplicate relationship_id {relationship_id!r}", structural_issue=True)
            continue
        seen.add(relationship_id)

        source, target = record["from_asset_id"], record["to_asset_id"]
        if source not in assets:
            warn(
                f"Relationship {relationship_id}: source asset {source!r} is not loaded",
                structural_issue=True,
            )
            continue
        if target not in assets:
            warn(
                f"Relationship {relationship_id}: target asset {target!r} is not loaded",
                structural_issue=True,
            )
            continue
        if known_types and record["relationship_type"] not in known_types:
            warn(
                f"Relationship {relationship_id}: type {record['relationship_type']!r} "
                "is not in the relationship vocabulary"
            )

        write(
            AssetRelationship(
                relationship_id=relationship_id,
                from_asset_id=source,
                relationship_type=record["relationship_type"],
                to_asset_id=target,
                status=record.get("status") or "planned",
                notes=list(record.get("notes") or ()),
            )
        )
        result.relationships += 1


# --- points ----------------------------------------------------------------


def _load_points(
    session: Session,
    write: _RowWriter,
    package: RegistryPackage,
    result: LoadResult,
    assets: Mapping[str, Any],
    warn,
    settings: Settings,
) -> None:
    definitions = {
        row.name: row
        for row in session.scalars(
            select(PointDefinition).where(PointDefinition.name.in_(list(package.point_definitions)))
        ).all()
    }
    profiles = {name: list(points or ()) for name, points in package.point_profiles.items()}
    class_defs = {
        row.name: row
        for row in session.scalars(
            select(AssetClass).where(AssetClass.name.in_(list(package.asset_classes)))
        ).all()
    }
    bindings_by_asset = package.bindings_by_asset()

    for record in package.bindings:
        asset_id = record["asset_id"]
        if asset_id not in assets:
            warn(
                f"Binding {record['point_id']}: asset {asset_id!r} is not loaded",
                structural_issue=True,
            )
        elif record["point_name"] not in package.point_definitions:
            warn(
                f"Binding {record['point_id']}: point {record['point_name']!r} is not in "
                "the point dictionary",
                structural_issue=True,
            )

    for asset_id in sorted(assets):
        asset = session.get(Asset, asset_id)
        if asset is None:  # pragma: no cover - defensive
            warn(f"Asset {asset_id} vanished before point materialisation", structural_issue=True)
            continue
        points = materialize_points(
            session,
            asset,
            bindings=bindings_by_asset.get(asset_id, {}),
            definitions=definitions,
            class_def=class_defs.get(asset.asset_class),
            profiles=profiles,
            settings=settings,
            warn=lambda message: warn(message, structural_issue=True),
            upsert=write,
        )
        result.points += len(points)
        # ``source == binding`` is set for exactly the points that carry a
        # binding record, so this counts the ``point_bindings`` rows written.
        result.bindings += sum(1 for point in points if point.source == SOURCE_BINDING)


# --- audit -----------------------------------------------------------------


def _record_revision(
    session: Session,
    package: RegistryPackage,
    result: LoadResult,
    settings: Settings,
    changed_by: str,
    reason: str | None,
) -> int:
    """Append the load to ``configuration_revisions`` (SDD section 5.6).

    Registry content is idempotent; the audit trail deliberately is not -- each
    load is a distinct event and gets its own revision number.
    """
    target_id = package.site_id or settings.site_id
    previous = session.scalar(
        select(func.count())
        .select_from(ConfigurationRevision)
        .where(
            ConfigurationRevision.target_type == "registry",
            ConfigurationRevision.target_id == target_id,
        )
    )
    revision = int(previous or 0) + 1
    session.add(
        ConfigurationRevision(
            target_type="registry",
            target_id=target_id,
            revision=revision,
            changed_by=changed_by,
            reason=reason or f"Loaded machine-readable design package from {package.data_dir}",
            diff={
                "counts": result.counts(),
                "warnings": list(result.warnings),
                "skipped_assets": list(result.skipped_assets),
                "data_dir": str(package.data_dir),
            },
            source_package_version=package.schema_version,
        )
    )
    return revision


__all__ = [
    "ASSET_CLASS_FILE",
    "ASSET_REGISTER_FILE",
    "POINT_BINDINGS_FILE",
    "POINT_DICTIONARY_FILE",
    "LoadResult",
    "RegistryLoadError",
    "RegistryPackage",
    "clear_package_cache",
    "load_package",
    "read_package",
    "topological_order",
]


def clear_package_cache() -> None:
    """Drop the parsed-package cache (tests and hot-reload tooling)."""
    _PACKAGE_CACHE.clear()
