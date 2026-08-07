"""Topology and dependency graph (SDD 16.1, 25.5).

The design package is authoritative and hand-authored; runtime health is a
timestamped observation layered on top of it. This router serves both, in that
order of authority, and never lets the second overwrite the first:

* **Nodes** are assets. Their identity, class, criticality and lifecycle status
  come from the register. Their *health* is a separate, derived field that is
  recomputed on every request and is allowed to be unknown.
* **Edges** are the register's typed relationships (SDD 25.5), carried through
  with their type intact. The canvas draws ``feeds`` differently from
  ``monitors`` because they mean different things, and collapsing them into one
  "connected" line would throw away the only information that makes the graph
  operable.
* **Groups** are derived from the containment hierarchy, so the canvas can draw
  the boundary an operator actually reasons about: *this is the power
  container, and everything inside it shares its fate*.

Two rules, inherited from :mod:`chaos.api.routers.overview`:

1. **A node is never healthy because nothing is wrong with it.** Of the 137
   assets in the package, none is installed. "No alarm" on a `planned` asset is
   not evidence of health, it is the absence of a plant. Health therefore starts
   from the lifecycle status and only reaches ``ok`` when a bound point has
   actually reported inside its stale window.
2. **Nothing is fabricated.** The availability vocabulary is
   :data:`~chaos.api.routers.overview.STATUS_EXPLANATIONS` verbatim,
   specialised from domain scope to node scope; no parallel vocabulary is
   invented here.

Impact analysis
---------------

``GET /topology/impact/{asset_id}`` answers SDD 16.1's central question --
"assume total loss of the power and server container; what is left?" -- as a
computation rather than a paragraph.

It walks the **same** graph the alarm correlator walks. Not a similar one:
:class:`chaos.alarms.correlation.DependencyGraph` and its
:data:`~chaos.alarms.correlation.DOWNSTREAM_EDGE_TYPES` direction table
are imported and used directly, so the blast radius the operator sees on the
canvas is by construction the same set the correlator will fold into one
incident when it actually happens. A second, subtly different traversal would be
worse than no traversal at all: it would train the operator on a model the
platform does not use.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
from collections import defaultdict, deque
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from chaos.alarms.correlation import (
    DEFAULT_MAX_DEPTH,
    DOWNSTREAM_EDGE_TYPES,
    DependencyGraph,
)
from chaos.api.deps import AppSettings, DbSession
from chaos.api.routers.overview import (
    DEPLOYED_STATUSES,
    DESIGN_STATUSES,
    SEVERITY_ORDER,
    SEVERITY_RANK,
    STATUS_DESIGN_ONLY,
    STATUS_NO_DATA,
    STATUS_NO_POINTS,
    STATUS_NOT_DEPLOYED,
    STATUS_OK,
    STATUS_STALE,
    UNCLEARED_ALARM_STATES,
)
from chaos.config import Settings
from chaos.models import (
    Alarm,
    Asset,
    AssetRelationship,
    CurrentState,
    Point,
    PointBinding,
    utcnow,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["topology"])

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Roll-up words the overview module already emits alongside the availability
#: vocabulary. Repeated here as names rather than literals so a reader can see
#: the whole node-scope alphabet in one place.
STATUS_ALARM = "alarm"
STATUS_DEGRADED = "degraded"

#: Lifecycle statuses meaning the asset once existed and no longer does.
RETIRED_STATUSES = frozenset({"retired", "decommissioned", "disposed"})

#: The node-scope reading of the availability vocabulary. Every word is
#: :data:`overview.STATUS_EXPLANATIONS`' word with the same meaning, moved down
#: one scope: overview speaks about a *domain* ("no assets of this kind exist"),
#: this speaks about *one asset* ("this asset does not exist"). The wording is
#: restated rather than reused verbatim because a sentence about a set read
#: against a single node is how vocabularies quietly fork.
NODE_HEALTH: dict[str, str] = {
    STATUS_OK: "A bound point reported inside its stale window. This is live measured data.",
    STATUS_STALE: (
        "The last value from this asset is older than its stale window. Treat it as unknown: "
        "the instrument or its link has stopped reporting, it is not reading zero."
    ),
    STATUS_NO_DATA: "Points are bound to a device but none has ever reported a value.",
    STATUS_NO_POINTS: "The asset is in the registry but carries no point instances at all.",
    STATUS_DESIGN_ONLY: (
        "The asset is installed and has points, but not one of them is bound to a device. "
        "It cannot report, so it exists to the platform as a design record only."
    ),
    STATUS_NOT_DEPLOYED: (
        "This asset does not physically exist. It is a design record with a lifecycle status "
        "of concept, planned, procured, reserve or retired, and nothing about it can be live."
    ),
    STATUS_ALARM: "An emergency or critical alarm is active against this asset.",
    STATUS_DEGRADED: "The asset is recorded as failed, or a major alarm is active against it.",
}

#: How each relationship type behaves under failure propagation, and why.
#:
#: The ``propagates`` value is looked up in the correlator's own
#: :data:`DOWNSTREAM_EDGE_TYPES` rather than restated, so this table can
#: annotate but never contradict the traversal. Types absent from that table
#: are deliberately inert, and each says why -- an edge that does not propagate
#: is a design statement, not an oversight.
EDGE_SEMANTICS: dict[str, str] = {
    "feeds": "Supplies power, water or media. Loss of the source stops the sink.",
    "contains": "Physical containment. Loss of the container is loss of the contents.",
    "hosts": "Runs the workload. Loss of the host stops what it hosts.",
    "controls": "Commands the actuator. Loss of the controller leaves the actuator uncommanded.",
    "serves": "Delivers a service to the consumer. Loss of the provider ends the service.",
    "protects": "Provides protection (breaker, valve, suppression) to the protected asset.",
    "located_in": "The subject sits inside the object. Loss of the place is loss of the occupant.",
    "part_of": "The subject is a component of the object. Loss of the whole is loss of the part.",
    "depends_on": "The subject requires the object to function.",
    "managed_by": "The subject is administered by the object.",
    "powered_by": "The subject draws power from the object.",
    "monitors": (
        "Observation only. A failed monitor does not stop the monitored asset, it blinds you "
        "to it, so this edge never propagates failure. It is reported instead as lost "
        "observability."
    ),
    "measures": (
        "Instrumentation only. Same reasoning as monitors: losing the meter loses the reading, not the flow."
    ),
    "backs_up": (
        "The subject stands in for the object on failure. Propagating along it would invert "
        "its meaning -- losing a backup does not stop the thing it backs up, it removes that "
        "thing's redundancy. Reported separately as lost redundancy."
    ),
    "connected_to": (
        "Symmetric adjacency (a cable, a bus, a network link) with no supply direction. "
        "Propagating both ways would make every asset reach every other and the blast radius "
        "would stop meaning anything."
    ),
    "associated_with": "A loose documentary association with no operational direction.",
    "replaces": "Lifecycle succession between a retired asset and its replacement.",
    "returns_to": "Return path (condensate, graywater, cold-water return) to the object.",
}

#: Relationship types that mean "this asset observes that one". Used to report
#: what goes blind rather than what goes dark.
OBSERVATION_EDGE_TYPES = frozenset({"monitors", "measures"})
#: Relationship types that mean "this asset is that one's fallback".
REDUNDANCY_EDGE_TYPES = frozenset({"backs_up"})

#: Asset classes that draw a boundary on the canvas. A group is a *place or
#: enclosure* that other assets sit inside, which is what an operator points at
#: when saying "if that burns". A distribution panel feeds many things but is
#: not somewhere you can stand, so it is a node, not a group.
GROUP_CLASSES: tuple[str, ...] = (
    "site",
    "geographic_zone",
    "structure",
    "building",
    "room",
    "zone",
    "enclosure",
    "rack",
    "irrigation_zone",
)

#: Package documents that extend the register but that ``registry.loader`` does
#: not yet merge into the database. They are still part of the design, so they
#: are overlaid here and marked as such rather than dropped -- a topology that
#: silently omitted the entire water system would be worse than useless.
EXTENSION_FILES: tuple[str, ...] = ("water_assets.yaml",)

#: Depth bound for the impact walk, matching the correlator's own default so
#: the canvas cannot show a longer reach than the correlator will act on.
MAX_IMPACT_DEPTH = DEFAULT_MAX_DEPTH

_CRITICALITY_ORDER = ("life_safety", "critical", "important", "discretionary")
_CRITICALITY_RANK = {name: index for index, name in enumerate(_CRITICALITY_ORDER)}

#: Parsed extension documents, keyed by path and invalidated on mtime + size.
_EXTENSION_CACHE: dict[Path, tuple[tuple[float, int], dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Package extensions
# ---------------------------------------------------------------------------


def read_extensions(data_dir: Path | str) -> list[dict[str, Any]]:
    """Parse the extension documents that live beside the register.

    A missing or unreadable extension is reported in the returned entry, not
    raised: the topology of what *is* loaded stays useful when one document is
    broken, and the payload says which one and why.
    """
    root = Path(data_dir)
    documents: list[dict[str, Any]] = []
    for name in EXTENSION_FILES:
        path = root / name
        entry: dict[str, Any] = {
            "file": name,
            "assets": [],
            "relationships": [],
            "available": False,
            "error": None,
            "document_status": None,
            "merge_target": None,
        }
        try:
            stat = path.stat()
            fingerprint = (stat.st_mtime, stat.st_size)
            key = path.resolve()
            cached = _EXTENSION_CACHE.get(key)
            if cached is None or cached[0] != fingerprint:
                with path.open(encoding="utf-8") as handle:
                    parsed = yaml.safe_load(handle) or {}
                cached = (
                    fingerprint,
                    {
                        "assets": list(parsed.get("assets") or []),
                        "relationships": list(parsed.get("relationships") or []),
                        "document_status": parsed.get("document_status"),
                        "merge_target": parsed.get("merge_target"),
                    },
                )
                _EXTENSION_CACHE[key] = cached
            # A fresh entry per call: callers annotate it with how much of the
            # document they were able to apply, and that must not leak into the
            # cache and go stale.
            entry.update(cached[1])
            entry["available"] = True
        except FileNotFoundError:
            entry["error"] = f"{name} is not present in the data directory."
        except (OSError, yaml.YAMLError) as exc:  # pragma: no cover - defensive
            entry["error"] = f"{name} could not be parsed: {exc}"
            logger.warning("topology: %s", entry["error"])
        documents.append(entry)
    return documents


# ---------------------------------------------------------------------------
# Node health
# ---------------------------------------------------------------------------


class _PointFacts:
    """Per-asset point counts, gathered in three queries rather than 137."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.total: dict[str, int] = defaultdict(int)
        self.bound: dict[str, int] = defaultdict(int)
        self.reporting: dict[str, int] = defaultdict(int)
        self.stale: dict[str, int] = defaultdict(int)
        self.last_seen: dict[str, dt.datetime] = {}

        try:
            for asset_id in session.scalars(select(Point.asset_id)):
                self.total[asset_id] += 1
            for asset_id in session.scalars(
                select(PointBinding.asset_id).where(PointBinding.binding_status.notin_(("tbd", "unbound")))
            ):
                self.bound[asset_id] += 1
            now = utcnow()
            rows = session.execute(select(CurrentState.asset_id, CurrentState.ts, CurrentState.stale_after_s))
            for asset_id, ts, stale_after in rows:
                self.reporting[asset_id] += 1
                ts = _as_utc(ts)
                window = stale_after or settings.default_stale_after_s
                if ts is None or (window and (now - ts).total_seconds() > window):
                    self.stale[asset_id] += 1
                if ts is not None and (asset_id not in self.last_seen or ts > self.last_seen[asset_id]):
                    self.last_seen[asset_id] = ts
        except SQLAlchemyError:  # pragma: no cover - defensive; empty DB is normal
            logger.exception("topology: point facts unavailable; nodes will read as no_points")


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def node_health(
    status_value: str,
    *,
    points: int,
    bound: int,
    reporting: int,
    stale: int,
    worst_severity: str | None,
) -> str:
    """Node-scope health, in the availability vocabulary.

    The cascade is ``overview._subsystem_rollup``'s, moved from domain scope to
    node scope, with one deliberate difference in order: **deployment is checked
    before alarms.**

    At domain scope an alarm can outrank "nothing is installed", because a
    domain holding forty paper assets and one real alarming device genuinely has
    an alarm. At node scope there is no such mixture. An alarm raised against an
    asset that does not physically exist is a data-quality problem, not a plant
    state, and reporting it as ``alarm`` would fabricate an operational reading
    for a thing that cannot produce one. The alarm is not hidden: the node
    always carries its own alarm counts, and the health note says so.
    """
    if status_value in DESIGN_STATUSES or status_value in RETIRED_STATUSES:
        return STATUS_NOT_DEPLOYED
    if status_value not in DEPLOYED_STATUSES and status_value != "failed":
        # An unrecognised lifecycle word must not fall through to "ok".
        return STATUS_NOT_DEPLOYED
    if worst_severity in ("emergency", "critical"):
        return STATUS_ALARM
    if status_value == "failed" or worst_severity == "major":
        return STATUS_DEGRADED
    if points == 0:
        return STATUS_NO_POINTS
    if bound == 0:
        return STATUS_DESIGN_ONLY
    if reporting == 0:
        return STATUS_NO_DATA
    if stale >= reporting:
        return STATUS_STALE
    return STATUS_OK


# ---------------------------------------------------------------------------
# Model assembly
# ---------------------------------------------------------------------------


def _node_from_asset(
    asset: dict[str, Any],
    facts: _PointFacts,
    alarms: dict[str, list[Alarm]],
    source: str,
) -> dict[str, Any]:
    asset_id = asset["asset_id"]
    active = alarms.get(asset_id, [])
    by_severity = {name: 0 for name in SEVERITY_ORDER}
    for alarm in active:
        if alarm.severity in by_severity:
            by_severity[alarm.severity] += 1
    worst = min((a.severity for a in active), key=lambda s: SEVERITY_RANK.get(s, 9), default=None)

    points = facts.total.get(asset_id, 0)
    bound = facts.bound.get(asset_id, 0)
    reporting = facts.reporting.get(asset_id, 0)
    stale = facts.stale.get(asset_id, 0)
    status_value = asset["status"]
    health = node_health(
        status_value,
        points=points,
        bound=bound,
        reporting=reporting,
        stale=stale,
        worst_severity=worst,
    )
    note = NODE_HEALTH[health]
    if health == STATUS_NOT_DEPLOYED and active:
        note += (
            f" {len(active)} active alarm(s) are recorded against it; an alarm on an asset that "
            "does not exist is a data-quality problem, not a plant state."
        )

    last_seen = facts.last_seen.get(asset_id)
    return {
        "asset_id": asset_id,
        "name": asset["name"],
        "domain": asset["domain"],
        "asset_class": asset["asset_class"],
        "criticality": asset["criticality"],
        "status": status_value,
        "parent_id": asset["parent_id"],
        "control_authority": asset.get("control_authority"),
        "functional_position": bool(asset.get("functional_position", True)),
        # The unresolved design fields travel with the node rather than behind a
        # second request: on this package they are the most actionable thing an
        # asset carries, and the inspector must never have to guess at them.
        "open_fields": list(asset.get("open_fields") or []),
        "open_field_count": len(asset.get("open_fields") or []),
        "health": health,
        "health_note": note,
        "points": {
            "total": points,
            "bound": bound,
            "reporting": reporting,
            "stale": stale,
        },
        "alarms": {
            "active": len(active),
            "worst_severity": worst,
            "by_severity": by_severity,
            "items": [
                {
                    "alarm_id": alarm.id,
                    "alarm_key": alarm.alarm_key,
                    "severity": alarm.severity,
                    "state": alarm.state,
                    "message": alarm.message,
                    "since": _iso(alarm.activated_at or alarm.detected_at),
                    "suppressed": bool(alarm.suppressed),
                    "suppression_reason": alarm.suppression_reason,
                    "incident_id": alarm.incident_id,
                }
                for alarm in sorted(
                    active,
                    key=lambda a: (SEVERITY_RANK.get(a.severity, 9), a.alarm_key),
                )
            ],
        },
        "last_seen": _iso(last_seen),
        "source": source,
    }


#: Registry rows and extension dictionaries are flattened to the same mapping
#: shape so both go through exactly one node-building code path. A second path
#: is how the water half of the graph would quietly acquire different rules.
def _asset_mapping(asset: Asset) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "name": asset.name,
        "domain": asset.domain,
        "asset_class": asset.asset_class,
        "criticality": asset.criticality,
        "status": asset.status,
        "parent_id": asset.parent_id,
        "control_authority": asset.control_authority,
        "functional_position": asset.functional_position,
        "open_fields": asset.open_fields,
    }


def _extension_mapping(raw: dict[str, Any]) -> dict[str, Any] | None:
    asset_id = raw.get("asset_id")
    if not asset_id:
        return None
    return {
        "asset_id": asset_id,
        "name": raw.get("name") or asset_id,
        "domain": raw.get("domain") or "site",
        "asset_class": raw.get("asset_class") or "unknown",
        "criticality": raw.get("criticality") or "discretionary",
        "status": raw.get("status") or "concept",
        "parent_id": raw.get("parent_id"),
        "control_authority": raw.get("control_authority"),
        "functional_position": raw.get("functional_position", True),
        "open_fields": raw.get("open_fields") or [],
    }


def collect_nodes(session: Session, settings: Settings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every asset the design package knows about, registry first."""
    facts = _PointFacts(session, settings)
    alarms: dict[str, list[Alarm]] = defaultdict(list)
    try:
        for alarm in session.scalars(select(Alarm).where(Alarm.state.in_(UNCLEARED_ALARM_STATES))):
            if alarm.asset_id:
                alarms[alarm.asset_id].append(alarm)
    except SQLAlchemyError:  # pragma: no cover - defensive
        logger.exception("topology: alarms unavailable")

    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        registry_assets = list(session.scalars(select(Asset).order_by(Asset.asset_id)))
    except SQLAlchemyError:  # pragma: no cover - defensive
        logger.exception("topology: asset registry unavailable")
        registry_assets = []
    for asset in registry_assets:
        nodes.append(_node_from_asset(_asset_mapping(asset), facts, alarms, "registry"))
        seen.add(asset.asset_id)

    extensions = read_extensions(settings.data_dir)
    for document in extensions:
        added = 0
        for raw in document["assets"]:
            mapping = _extension_mapping(raw)
            if mapping is None or mapping["asset_id"] in seen:
                continue
            nodes.append(_node_from_asset(mapping, facts, alarms, f"extension:{document['file']}"))
            seen.add(mapping["asset_id"])
            added += 1
        document["applied_assets"] = added

    # One total order over the whole graph, registry and extension alike. The
    # canvas seeds its deterministic layout from this list, so "sorted by
    # asset_id" is a contract, not a convenience.
    nodes.sort(key=lambda node: node["asset_id"])
    return nodes, extensions


def collect_edges(session: Session, extensions: list[dict[str, Any]], known: set[str]) -> list[dict]:
    """Every typed relationship, with its type and its propagation direction."""
    edges: list[dict[str, Any]] = []
    emitted: set[tuple[str, str, str]] = set()

    def add(
        relationship_id: str,
        from_id: str,
        rel_type: str,
        to_id: str,
        rel_status: str | None,
        source: str,
    ) -> None:
        triple = (from_id, rel_type, to_id)
        if triple in emitted:
            return
        # A relationship whose endpoints are not both in the graph cannot be
        # drawn; dropping it silently would misrepresent the register, so it is
        # counted in the summary instead.
        if from_id not in known or to_id not in known:
            return
        emitted.add(triple)
        edges.append(
            {
                "relationship_id": relationship_id,
                "from_asset_id": from_id,
                "to_asset_id": to_id,
                "relationship_type": rel_type,
                "status": rel_status or "planned",
                "propagates": DOWNSTREAM_EDGE_TYPES.get(rel_type),
                "semantic": EDGE_SEMANTICS.get(rel_type),
                "source": source,
            }
        )

    try:
        rows = list(session.scalars(select(AssetRelationship).order_by(AssetRelationship.relationship_id)))
    except SQLAlchemyError:  # pragma: no cover - defensive
        logger.exception("topology: relationships unavailable")
        rows = []
    for row in rows:
        add(
            row.relationship_id,
            row.from_asset_id,
            row.relationship_type,
            row.to_asset_id,
            row.status,
            "registry",
        )

    for document in extensions:
        applied = 0
        for raw in document["relationships"]:
            from_id = raw.get("from_asset_id")
            to_id = raw.get("to_asset_id")
            rel_type = raw.get("relationship_type")
            if not (from_id and to_id and rel_type):
                continue
            before = len(edges)
            add(
                raw.get("relationship_id") or f"{from_id}|{rel_type}|{to_id}",
                from_id,
                rel_type,
                to_id,
                raw.get("status"),
                f"extension:{document['file']}",
            )
            applied += len(edges) - before
        document["applied_relationships"] = applied

    edges.sort(key=lambda e: (e["from_asset_id"], e["relationship_type"], e["to_asset_id"]))
    return edges


def collect_groups(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Boundaries derived from the containment hierarchy.

    A group is an asset of a spatial class that has at least one asset inside
    it. Members are the *whole* containment subtree, because that is what shares
    the group's fate: everything in the power container is lost with the power
    container, not only its direct children.
    """
    by_id = {node["asset_id"]: node for node in nodes}
    children: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        if node["parent_id"] and node["parent_id"] in by_id:
            children[node["parent_id"]].append(node["asset_id"])

    def subtree(root: str) -> list[str]:
        out: list[str] = []
        queue = deque(sorted(children.get(root, ())))
        seen = {root}
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            out.append(current)
            queue.extend(sorted(children.get(current, ())))
        return sorted(out)

    def group_ancestor(asset_id: str) -> str | None:
        cursor = by_id[asset_id]["parent_id"]
        guard = 0
        while cursor and guard < 64:
            parent = by_id.get(cursor)
            if parent is None:
                return None
            if parent["asset_class"] in GROUP_CLASSES:
                return cursor
            cursor = parent["parent_id"]
            guard += 1
        return None

    groups: list[dict[str, Any]] = []
    for node in nodes:
        if node["asset_class"] not in GROUP_CLASSES:
            continue
        members = subtree(node["asset_id"])
        if not members:
            continue  # an empty room is a node, not a boundary
        parent_group = group_ancestor(node["asset_id"])
        level = 0
        cursor = parent_group
        guard = 0
        while cursor and guard < 64:
            level += 1
            cursor = group_ancestor(cursor)
            guard += 1
        groups.append(
            {
                "group_id": node["asset_id"],
                "name": node["name"],
                "asset_class": node["asset_class"],
                "domain": node["domain"],
                "criticality": node["criticality"],
                "status": node["status"],
                "health": node["health"],
                "parent_group_id": parent_group,
                "level": level,
                "direct_children": sorted(children.get(node["asset_id"], ())),
                "members": members,
                "member_count": len(members),
            }
        )
    groups.sort(key=lambda g: (g["level"], g["group_id"]))
    return groups


# ---------------------------------------------------------------------------
# Impact graph
# ---------------------------------------------------------------------------


def build_impact_graph(
    session: Session,
    extensions: list[dict[str, Any]],
    known: set[str],
    *,
    max_depth: int = MAX_IMPACT_DEPTH,
) -> tuple[DependencyGraph, dict[tuple[str, str], list[str]]]:
    """The correlator's dependency graph, extended with the package overlay.

    :class:`DependencyGraph` is built by the alarm correlator from the registry
    tables. The extension documents are not in those tables yet, so their edges
    are added through the graph's own :meth:`~DependencyGraph.add_edge` using the
    correlator's own direction table. Nothing here decides *which way* failure
    flows -- that decision lives in ``DOWNSTREAM_EDGE_TYPES`` and is imported.

    Returns the graph and a label map ``(source, target) -> reasons`` so a path
    can be explained edge by edge. The label map is built from the same rows in
    the same order, and ``test_topology`` asserts the two agree exactly.
    """
    graph = DependencyGraph.from_session(session, max_depth=max_depth)
    labels: dict[tuple[str, str], set[str]] = defaultdict(set)

    def label(source: str, target: str, reason: str) -> None:
        if source != target and source in known and target in known:
            labels[(source, target)].add(reason)

    try:
        rows = list(session.scalars(select(AssetRelationship)))
    except SQLAlchemyError:  # pragma: no cover - defensive
        rows = []
    for row in rows:
        direction = DOWNSTREAM_EDGE_TYPES.get(row.relationship_type)
        if direction == "forward":
            label(row.from_asset_id, row.to_asset_id, row.relationship_type)
        elif direction == "reverse":
            label(row.to_asset_id, row.from_asset_id, row.relationship_type)

    try:
        hierarchy = list(
            session.execute(select(Asset.asset_id, Asset.parent_id).where(Asset.parent_id.is_not(None)))
        )
    except SQLAlchemyError:  # pragma: no cover - defensive
        hierarchy = []
    for asset_id, parent_id in hierarchy:
        label(parent_id, asset_id, "contains (hierarchy)")

    # Overlay: extension assets and their relationships, same rules.
    for document in extensions:
        for raw in document["assets"]:
            asset_id = raw.get("asset_id")
            parent_id = raw.get("parent_id")
            if not asset_id or not parent_id:
                continue
            if asset_id not in known or parent_id not in known:
                continue
            graph.add_edge(parent_id, asset_id)
            graph.neighbours[parent_id].add(asset_id)
            graph.neighbours[asset_id].add(parent_id)
            label(parent_id, asset_id, "contains (hierarchy)")
        for raw in document["relationships"]:
            from_id, to_id = raw.get("from_asset_id"), raw.get("to_asset_id")
            rel_type = raw.get("relationship_type")
            if not (from_id and to_id and rel_type):
                continue
            if from_id not in known or to_id not in known:
                continue
            graph.neighbours[from_id].add(to_id)
            graph.neighbours[to_id].add(from_id)
            direction = DOWNSTREAM_EDGE_TYPES.get(rel_type)
            if direction == "forward":
                graph.add_edge(from_id, to_id)
                label(from_id, to_id, rel_type)
            elif direction == "reverse":
                graph.add_edge(to_id, from_id)
                label(to_id, from_id, rel_type)

    return graph, {key: sorted(value) for key, value in labels.items()}


def reachable_with_paths(
    adjacency: dict[str, set[str]], start: str, max_depth: int
) -> tuple[dict[str, int], dict[str, str]]:
    """Breadth-first reach with a predecessor, cycle-safe and depth-bounded.

    Deliberately identical in shape to ``DependencyGraph._walk``: same queue
    discipline, same ``depth >= max_depth`` cut-off, same visited-set cycle
    guard. The only addition is the predecessor map, which is what lets the API
    show *how* a failure reaches each asset instead of only that it does. A test
    asserts this returns exactly ``graph.descendants(start)``.
    """
    depths: dict[str, int] = {}
    predecessor: dict[str, str] = {}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    visited = {start}
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for neighbour in sorted(adjacency.get(node, ())):
            if neighbour in visited:
                continue
            visited.add(neighbour)
            depths[neighbour] = depth + 1
            predecessor[neighbour] = node
            queue.append((neighbour, depth + 1))
    return depths, predecessor


def _explain_path(
    start: str,
    target: str,
    predecessor: dict[str, str],
    labels: dict[tuple[str, str], list[str]],
) -> list[dict[str, Any]]:
    chain = [target]
    guard = 0
    while chain[-1] != start and guard < 64:
        previous = predecessor.get(chain[-1])
        if previous is None:
            break
        chain.append(previous)
        guard += 1
    chain.reverse()
    steps: list[dict[str, Any]] = [{"asset_id": chain[0], "via": None}]
    for source, target_id in itertools.pairwise(chain):
        steps.append(
            {"asset_id": target_id, "via": labels.get((source, target_id), ["contains (hierarchy)"])}
        )
    return steps


def _criticality_rank(value: str | None) -> int:
    return _CRITICALITY_RANK.get(value or "", len(_CRITICALITY_ORDER))


def _iso(value: dt.datetime | None) -> str | None:
    aware = _as_utc(value)
    return aware.isoformat() if aware else None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/topology", summary="Asset topology, typed relationships and live health (SDD 16.1, 25.5)")
def topology(
    session: DbSession,
    settings: AppSettings,
    include_extensions: Annotated[
        bool,
        Query(
            description=(
                "Include design-package extension documents that the registry loader does not "
                "merge into the database yet (the water package). Their nodes are marked with "
                "an 'extension:' source."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    nodes, extensions = collect_nodes(session, settings)
    if not include_extensions:
        nodes = [node for node in nodes if node["source"] == "registry"]
        extensions = []
    known = {node["asset_id"] for node in nodes}
    edges = collect_edges(session, extensions, known)
    groups = collect_groups(nodes)

    health_counts: dict[str, int] = {name: 0 for name in NODE_HEALTH}
    domain_counts: dict[str, int] = defaultdict(int)
    criticality_counts: dict[str, int] = defaultdict(int)
    status_counts: dict[str, int] = defaultdict(int)
    for node in nodes:
        health_counts[node["health"]] = health_counts.get(node["health"], 0) + 1
        domain_counts[node["domain"]] += 1
        criticality_counts[node["criticality"]] += 1
        status_counts[node["status"]] += 1

    edge_type_counts: dict[str, int] = defaultdict(int)
    for edge in edges:
        edge_type_counts[edge["relationship_type"]] += 1

    return {
        "generated_at": utcnow(),
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "summary": {
            "nodes": len(nodes),
            "edges": len(edges),
            "groups": len(groups),
            "by_health": health_counts,
            "by_domain": dict(sorted(domain_counts.items())),
            "by_criticality": dict(sorted(criticality_counts.items())),
            "by_status": dict(sorted(status_counts.items())),
            "by_relationship_type": dict(sorted(edge_type_counts.items())),
            "propagating_edges": sum(1 for e in edges if e["propagates"]),
            "deployed_nodes": sum(1 for n in nodes if n["status"] in DEPLOYED_STATUSES),
        },
        "package": {
            "registry_nodes": sum(1 for n in nodes if n["source"] == "registry"),
            "extension_nodes": sum(1 for n in nodes if n["source"] != "registry"),
            "extensions": [
                {
                    "file": document["file"],
                    "available": document["available"],
                    "error": document["error"],
                    "document_status": document.get("document_status"),
                    "assets_applied": document.get("applied_assets", 0),
                    "relationships_applied": document.get("applied_relationships", 0),
                    "note": (
                        "Present in the design package but not merged into the registry database "
                        "by the loader yet, so these assets carry no points and no bindings."
                    ),
                }
                for document in extensions
            ],
        },
        "health_vocabulary": NODE_HEALTH,
        "edge_semantics": EDGE_SEMANTICS,
        "group_classes": list(GROUP_CLASSES),
        "propagation_rules": dict(DOWNSTREAM_EDGE_TYPES),
    }


@router.get(
    "/topology/impact/{asset_id}",
    summary="Common-mode impact: what is lost when this asset fails (SDD 16.1)",
)
def topology_impact(
    asset_id: str,
    session: DbSession,
    settings: AppSettings,
    max_depth: Annotated[
        int,
        Query(ge=1, le=32, description="Hop limit for the propagation walk."),
    ] = MAX_IMPACT_DEPTH,
) -> dict[str, Any]:
    nodes, extensions = collect_nodes(session, settings)
    by_id = {node["asset_id"]: node for node in nodes}
    if asset_id not in by_id:
        raise HTTPException(
            http_status.HTTP_404_NOT_FOUND,
            f"Asset '{asset_id}' is not in the topology graph.",
        )
    known = set(by_id)
    graph, labels = build_impact_graph(session, extensions, known, max_depth=max_depth)
    depths, predecessor = reachable_with_paths(graph.downstream, asset_id, max_depth)

    affected: list[dict[str, Any]] = []
    for target, depth in depths.items():
        node = by_id.get(target)
        if node is None:
            continue
        path = _explain_path(asset_id, target, predecessor, labels)
        affected.append(
            {
                "asset_id": target,
                "name": node["name"],
                "domain": node["domain"],
                "asset_class": node["asset_class"],
                "criticality": node["criticality"],
                "status": node["status"],
                "health": node["health"],
                "depth": depth,
                "reached_by": path[-1]["via"] if len(path) > 1 else None,
                "path": path,
            }
        )
    affected.sort(key=lambda item: (_criticality_rank(item["criticality"]), item["depth"], item["asset_id"]))

    lost = set(depths) | {asset_id}

    # What survives but goes blind, and what survives but loses its fallback.
    # Both are read one hop out of the lost set over the deliberately
    # non-propagating edge types, which is exactly where their meaning lives.
    observability: dict[str, list[str]] = defaultdict(list)
    redundancy: dict[str, list[str]] = defaultdict(list)
    edges = collect_edges(session, extensions, known)
    for edge in edges:
        source, target = edge["from_asset_id"], edge["to_asset_id"]
        rel_type = edge["relationship_type"]
        if source not in lost or target in lost:
            continue
        if rel_type in OBSERVATION_EDGE_TYPES:
            observability[target].append(source)
        elif rel_type in REDUNDANCY_EDGE_TYPES:
            redundancy[target].append(source)

    def _surviving(mapping: dict[str, list[str]], key: str) -> list[dict[str, Any]]:
        out = []
        for target, sources in mapping.items():
            node = by_id[target]
            out.append(
                {
                    "asset_id": target,
                    "name": node["name"],
                    "domain": node["domain"],
                    "criticality": node["criticality"],
                    key: sorted(sources),
                }
            )
        out.sort(key=lambda item: (_criticality_rank(item["criticality"]), item["asset_id"]))
        return out

    by_criticality: dict[str, int] = {name: 0 for name in _CRITICALITY_ORDER}
    by_domain: dict[str, int] = defaultdict(int)
    for item in affected:
        by_criticality[item["criticality"]] = by_criticality.get(item["criticality"], 0) + 1
        by_domain[item["domain"]] += 1

    survivors = [node for node in nodes if node["asset_id"] not in lost]
    survivors_by_domain: dict[str, int] = defaultdict(int)
    for node in survivors:
        survivors_by_domain[node["domain"]] += 1

    origin = by_id[asset_id]
    max_reached = max(depths.values(), default=0)
    return {
        "generated_at": utcnow(),
        "origin": {
            "asset_id": origin["asset_id"],
            "name": origin["name"],
            "domain": origin["domain"],
            "asset_class": origin["asset_class"],
            "criticality": origin["criticality"],
            "status": origin["status"],
            "health": origin["health"],
        },
        "max_depth": max_depth,
        "affected": affected,
        "summary": {
            "affected_total": len(affected),
            "graph_total": len(nodes),
            "unaffected_total": len(survivors),
            "by_criticality": by_criticality,
            "by_domain": dict(sorted(by_domain.items())),
            "survivors_by_domain": dict(sorted(survivors_by_domain.items())),
            "max_depth_reached": max_reached,
            "depth_bounded": max_reached >= max_depth,
        },
        "observability_lost": _surviving(observability, "monitored_by"),
        "redundancy_lost": _surviving(redundancy, "backed_up_by"),
        "rules": {
            "forward": sorted(t for t, d in DOWNSTREAM_EDGE_TYPES.items() if d == "forward"),
            "reverse": sorted(t for t, d in DOWNSTREAM_EDGE_TYPES.items() if d == "reverse"),
            "hierarchy": (
                "The containment hierarchy propagates from parent to child even where no "
                "explicit relationship row exists: a room's failure reaches what is in it."
            ),
            "excluded": {
                name: EDGE_SEMANTICS[name]
                for name in sorted(EDGE_SEMANTICS)
                if name not in DOWNSTREAM_EDGE_TYPES
            },
        },
        "caveats": [
            "The register records no redundancy grouping, so a set of N parallel devices "
            "(the four hybrid inverters, the paired storage arrays) is modelled as N assets "
            "each feeding the same bus. The blast radius of one of them therefore reads as a "
            "full outage. That overstates the loss and is a gap in the design package, not in "
            "this traversal.",
            "The walk is bounded at "
            f"{max_depth} hops, matching the alarm correlator. A graph deeper than that is "
            "reported as depth_bounded so the answer is never silently truncated.",
            "Impact is structural. It says what is downstream of a failure, not how long a "
            "battery reserve or a tank of water will keep the downstream assets alive.",
        ],
    }


__all__ = [
    "EDGE_SEMANTICS",
    "EXTENSION_FILES",
    "GROUP_CLASSES",
    "MAX_IMPACT_DEPTH",
    "NODE_HEALTH",
    "build_impact_graph",
    "collect_edges",
    "collect_groups",
    "collect_nodes",
    "node_health",
    "reachable_with_paths",
    "read_extensions",
    "router",
]
