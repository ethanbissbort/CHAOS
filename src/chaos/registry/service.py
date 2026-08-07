"""Read-side query helpers over the registry.

Every function is pure with respect to the platform: it takes a
:class:`~sqlalchemy.orm.Session` and returns ORM objects or plain data. The API
layer, the EMS, the alarm correlator and the simulator all go through here so
that "what does this asset depend on" has exactly one answer.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from chaos.models import (
    Asset,
    AssetClass,
    AssetRelationship,
    Point,
    PointBinding,
    PointDefinition,
)

#: Relationship types that express operational dependency (SDD section 25.5).
#: ``depends_on`` and ``part_of`` point *from* the dependent asset; ``feeds``
#: points *towards* it, so the supplier is found on the inbound side.
OUTBOUND_DEPENDENCY_TYPES = ("depends_on", "part_of")
INBOUND_DEPENDENCY_TYPES = ("feeds",)
DEPENDENCY_TYPES = tuple(sorted({*OUTBOUND_DEPENDENCY_TYPES, *INBOUND_DEPENDENCY_TYPES}))

DIRECTIONS = ("outgoing", "incoming", "both")


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class AssetPage:
    """A slice of the asset list plus the unfiltered-by-paging total.

    Behaves like a list of assets so callers that only want the rows can
    iterate it directly.
    """

    items: list[Asset] = field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0

    def __iter__(self) -> Iterator[Asset]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]


@dataclass(frozen=True)
class RelationshipView:
    """One relationship seen from the perspective of a specific asset."""

    relationship_id: str
    relationship_type: str
    direction: str  # outgoing | incoming
    from_asset_id: str
    to_asset_id: str
    counterpart_id: str
    counterpart_name: str | None
    counterpart_class: str | None
    counterpart_domain: str | None
    counterpart_status: str | None
    status: str
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


def get_asset(session: Session, asset_id: str) -> Asset | None:
    return session.get(Asset, asset_id)


def list_assets(
    session: Session,
    *,
    domain: str | None = None,
    asset_class: str | None = None,
    status: str | None = None,
    criticality: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AssetPage:
    """Filtered, paginated asset list ordered by canonical ID."""
    statement = select(Asset)
    if domain:
        statement = statement.where(Asset.domain == domain)
    if asset_class:
        statement = statement.where(Asset.asset_class == asset_class)
    if status:
        statement = statement.where(Asset.status == status)
    if criticality:
        statement = statement.where(Asset.criticality == criticality)
    if q:
        pattern = f"%{q.strip().lower()}%"
        statement = statement.where(
            or_(
                func.lower(Asset.asset_id).like(pattern),
                func.lower(Asset.name).like(pattern),
            )
        )

    # ``tags`` is a JSON array; filtering it portably (SQLite and PostgreSQL)
    # means doing the containment test in Python.
    if tag:
        rows = session.scalars(statement.order_by(Asset.asset_id)).all()
        matched = [asset for asset in rows if tag in (asset.tags or ())]
        return AssetPage(
            items=matched[offset : offset + limit],
            total=len(matched),
            limit=limit,
            offset=offset,
        )

    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    items = session.scalars(statement.order_by(Asset.asset_id).limit(limit).offset(offset)).all()
    return AssetPage(items=list(items), total=int(total), limit=limit, offset=offset)


def get_asset_class(session: Session, name: str) -> AssetClass | None:
    return session.get(AssetClass, name)


def list_asset_classes(session: Session) -> list[AssetClass]:
    return list(session.scalars(select(AssetClass).order_by(AssetClass.name)).all())


def list_point_definitions(session: Session) -> list[PointDefinition]:
    return list(session.scalars(select(PointDefinition).order_by(PointDefinition.name)).all())


def count_children(session: Session, asset_id: str) -> int:
    return int(
        session.scalar(select(func.count()).select_from(Asset).where(Asset.parent_id == asset_id)) or 0
    )


def count_asset_points(session: Session, asset_id: str) -> int:
    return int(session.scalar(select(func.count()).select_from(Point).where(Point.asset_id == asset_id)) or 0)


# ---------------------------------------------------------------------------
# Containment tree
# ---------------------------------------------------------------------------


def get_asset_tree(
    session: Session,
    root_id: str | None = None,
    depth: int | None = None,
) -> dict[str, Any]:
    """Nested containment tree.

    ``depth`` limits how many generations below the root are expanded;
    ``None`` means the whole subtree. When ``root_id`` is omitted the register's
    single root asset is used; if the register ever grows several roots they are
    returned under a synthetic node whose ``asset_id`` is ``None``.
    """
    assets = list(session.scalars(select(Asset).order_by(Asset.asset_id)).all())
    children: dict[str | None, list[Asset]] = defaultdict(list)
    by_id: dict[str, Asset] = {}
    for asset in assets:
        by_id[asset.asset_id] = asset
        children[asset.parent_id].append(asset)

    def node(asset: Asset, remaining: int | None) -> dict[str, Any]:
        expand = remaining is None or remaining > 0
        kids = children.get(asset.asset_id, []) if expand else []
        return {
            "asset_id": asset.asset_id,
            "name": asset.name,
            "domain": asset.domain,
            "asset_class": asset.asset_class,
            "status": asset.status,
            "criticality": asset.criticality,
            "child_count": len(children.get(asset.asset_id, [])),
            "children": [node(child, None if remaining is None else remaining - 1) for child in kids],
        }

    if root_id is not None:
        root = by_id.get(root_id)
        if root is None:
            raise KeyError(root_id)
        return node(root, depth)

    roots = children.get(None, [])
    if len(roots) == 1:
        return node(roots[0], depth)
    return {
        "asset_id": None,
        "name": "registry",
        "domain": None,
        "asset_class": None,
        "status": None,
        "criticality": None,
        "child_count": len(roots),
        "children": [node(root, None if depth is None else depth - 1) for root in roots],
    }


def get_descendants(session: Session, asset_id: str) -> list[Asset]:
    """Every asset contained by ``asset_id``, breadth first."""
    assets = list(session.scalars(select(Asset).order_by(Asset.asset_id)).all())
    children: dict[str | None, list[Asset]] = defaultdict(list)
    for asset in assets:
        children[asset.parent_id].append(asset)

    out: list[Asset] = []
    queue = list(children.get(asset_id, []))
    seen: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current.asset_id in seen:
            continue
        seen.add(current.asset_id)
        out.append(current)
        queue.extend(children.get(current.asset_id, []))
    return out


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------


def get_asset_points(session: Session, asset_id: str) -> list[Point]:
    return list(
        session.scalars(select(Point).where(Point.asset_id == asset_id).order_by(Point.point_name)).all()
    )


def get_point(session: Session, point_id: str) -> Point | None:
    return session.get(Point, point_id)


def list_points(
    session: Session,
    *,
    asset_id: str | None = None,
    point_name: str | None = None,
    control_capable: bool | None = None,
    automatic_control_allowed: bool | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[Point], int]:
    statement = select(Point)
    if asset_id:
        statement = statement.where(Point.asset_id == asset_id)
    if point_name:
        statement = statement.where(Point.point_name == point_name)
    if control_capable is not None:
        statement = statement.where(Point.control_capable.is_(control_capable))
    if automatic_control_allowed is not None:
        statement = statement.where(Point.automatic_control_allowed.is_(automatic_control_allowed))

    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    items = session.scalars(statement.order_by(Point.point_id).limit(limit).offset(offset)).all()
    return list(items), int(total)


def get_binding(session: Session, point_id: str) -> PointBinding | None:
    return session.get(PointBinding, point_id)


def resolve_topic(session: Session, topic: str) -> PointBinding | None:
    """Map an MQTT telemetry topic back to its binding.

    SDD section 26.2 is explicit that parsing a topic is *not* the identity
    mechanism: the registry owns the topic -> point mapping, and this is it.
    """
    return session.scalar(select(PointBinding).where(PointBinding.mqtt_topic == topic))


def resolve_command_topic(session: Session, topic: str) -> PointBinding | None:
    return session.scalar(select(PointBinding).where(PointBinding.command_topic == topic))


# ---------------------------------------------------------------------------
# Relationships and dependencies
# ---------------------------------------------------------------------------


def get_asset_relationships(
    session: Session,
    asset_id: str,
    direction: str = "both",
) -> list[RelationshipView]:
    """Typed edges touching ``asset_id`` with the counterpart resolved."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")

    clauses = []
    if direction in ("outgoing", "both"):
        clauses.append(AssetRelationship.from_asset_id == asset_id)
    if direction in ("incoming", "both"):
        clauses.append(AssetRelationship.to_asset_id == asset_id)

    rows = session.scalars(
        select(AssetRelationship).where(or_(*clauses)).order_by(AssetRelationship.relationship_id)
    ).all()

    counterpart_ids = {
        row.to_asset_id if row.from_asset_id == asset_id else row.from_asset_id for row in rows
    }
    counterparts = _assets_by_id(session, counterpart_ids)

    views: list[RelationshipView] = []
    for row in rows:
        outgoing = row.from_asset_id == asset_id
        counterpart_id = row.to_asset_id if outgoing else row.from_asset_id
        counterpart = counterparts.get(counterpart_id)
        views.append(
            RelationshipView(
                relationship_id=row.relationship_id,
                relationship_type=row.relationship_type,
                direction="outgoing" if outgoing else "incoming",
                from_asset_id=row.from_asset_id,
                to_asset_id=row.to_asset_id,
                counterpart_id=counterpart_id,
                counterpart_name=counterpart.name if counterpart else None,
                counterpart_class=counterpart.asset_class if counterpart else None,
                counterpart_domain=counterpart.domain if counterpart else None,
                counterpart_status=counterpart.status if counterpart else None,
                status=row.status,
                notes=list(row.notes or ()),
            )
        )
    return views


def get_dependencies(
    session: Session,
    asset_id: str,
    transitive: bool = False,
) -> list[Asset]:
    """Assets ``asset_id`` operationally depends on.

    An asset depends on what it declares (``depends_on``), what it is part of
    (``part_of``), and whatever *feeds* it -- ``feeds`` is recorded from the
    supplier towards the consumer, so the supplier is on the inbound side.
    Legacy ``dependencies`` entries carried in the asset record are included
    when they name a known asset.

    With ``transitive=True`` the closure is walked breadth first with a cycle
    guard, which is what alarm correlation needs to answer "is this alarm a
    consequence of the upstream failure".
    """
    resolved: list[str] = []
    seen: set[str] = {asset_id}
    frontier = [asset_id]

    while frontier:
        direct = _direct_dependency_ids(session, frontier)
        frontier = []
        for candidate in direct:
            if candidate in seen:
                continue
            seen.add(candidate)
            resolved.append(candidate)
            frontier.append(candidate)
        if not transitive:
            break

    assets = _assets_by_id(session, resolved)
    return [assets[a] for a in resolved if a in assets]


def _direct_dependency_ids(session: Session, asset_ids: Sequence[str]) -> list[str]:
    if not asset_ids:
        return []
    ids = list(asset_ids)

    outgoing = session.execute(
        select(AssetRelationship.to_asset_id)
        .where(
            AssetRelationship.from_asset_id.in_(ids),
            AssetRelationship.relationship_type.in_(OUTBOUND_DEPENDENCY_TYPES),
        )
        .order_by(AssetRelationship.relationship_id)
    ).scalars()
    inbound = session.execute(
        select(AssetRelationship.from_asset_id)
        .where(
            AssetRelationship.to_asset_id.in_(ids),
            AssetRelationship.relationship_type.in_(INBOUND_DEPENDENCY_TYPES),
        )
        .order_by(AssetRelationship.relationship_id)
    ).scalars()

    declared: list[str] = []
    for asset in session.scalars(select(Asset).where(Asset.asset_id.in_(ids))).all():
        declared.extend(str(dep) for dep in (asset.dependencies or ()) if isinstance(dep, str))

    ordered: list[str] = []
    for candidate in [*outgoing, *inbound, *declared]:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def get_dependents(session: Session, asset_id: str, transitive: bool = False) -> list[Asset]:
    """The inverse of :func:`get_dependencies` -- who breaks if this breaks."""
    resolved: list[str] = []
    seen: set[str] = {asset_id}
    frontier = [asset_id]

    while frontier:
        ids = list(frontier)
        frontier = []
        inbound = session.execute(
            select(AssetRelationship.from_asset_id).where(
                AssetRelationship.to_asset_id.in_(ids),
                AssetRelationship.relationship_type.in_(OUTBOUND_DEPENDENCY_TYPES),
            )
        ).scalars()
        outgoing = session.execute(
            select(AssetRelationship.to_asset_id).where(
                AssetRelationship.from_asset_id.in_(ids),
                AssetRelationship.relationship_type.in_(INBOUND_DEPENDENCY_TYPES),
            )
        ).scalars()
        for candidate in [*inbound, *outgoing]:
            if candidate in seen:
                continue
            seen.add(candidate)
            resolved.append(candidate)
            frontier.append(candidate)
        if not transitive:
            break

    assets = _assets_by_id(session, resolved)
    return [assets[a] for a in resolved if a in assets]


def _assets_by_id(session: Session, asset_ids: Iterable[str]) -> dict[str, Asset]:
    ids = list(dict.fromkeys(asset_ids))
    if not ids:
        return {}
    return {
        asset.asset_id: asset for asset in session.scalars(select(Asset).where(Asset.asset_id.in_(ids))).all()
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def registry_summary(session: Session) -> dict[str, Any]:
    """Registry roll-up for the API and the operations dashboard."""

    def counts(column) -> dict[str, int]:
        rows = session.execute(select(column, func.count()).group_by(column).order_by(column)).all()
        return {str(key): int(value) for key, value in rows}

    assets_total = session.scalar(select(func.count()).select_from(Asset)) or 0
    points_total = session.scalar(select(func.count()).select_from(Point)) or 0
    relationships_total = session.scalar(select(func.count()).select_from(AssetRelationship)) or 0
    bindings_total = session.scalar(select(func.count()).select_from(PointBinding)) or 0

    open_field_assets = sum(
        1 for open_fields in session.scalars(select(Asset.open_fields)).all() if open_fields
    )
    open_field_count = sum(
        len(open_fields or ()) for open_fields in session.scalars(select(Asset.open_fields)).all()
    )

    control_capable = (
        session.scalar(select(func.count()).select_from(Point).where(Point.control_capable.is_(True))) or 0
    )
    automatic_control = (
        session.scalar(
            select(func.count()).select_from(Point).where(Point.automatic_control_allowed.is_(True))
        )
        or 0
    )

    return {
        "assets": int(assets_total),
        "points": int(points_total),
        "relationships": int(relationships_total),
        "bindings": int(bindings_total),
        "assets_by_domain": counts(Asset.domain),
        "assets_by_status": counts(Asset.status),
        "assets_by_criticality": counts(Asset.criticality),
        "assets_by_class": counts(Asset.asset_class),
        "relationships_by_type": counts(AssetRelationship.relationship_type),
        "assets_with_open_fields": open_field_assets,
        "open_field_count": open_field_count,
        "bindings_by_status": counts(PointBinding.binding_status),
        "points_control_capable": int(control_capable),
        "points_automatic_control_allowed": int(automatic_control),
    }


__all__ = [
    "DEPENDENCY_TYPES",
    "AssetPage",
    "RelationshipView",
    "count_asset_points",
    "count_children",
    "get_asset",
    "get_asset_class",
    "get_asset_points",
    "get_asset_relationships",
    "get_asset_tree",
    "get_binding",
    "get_dependencies",
    "get_dependents",
    "get_descendants",
    "get_point",
    "list_asset_classes",
    "list_assets",
    "list_point_definitions",
    "list_points",
    "registry_summary",
    "resolve_command_topic",
    "resolve_topic",
]
