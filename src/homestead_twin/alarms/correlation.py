"""Root-cause correlation into incidents (SDD 14.2).

    "The platform must prevent alarm floods by correlating root causes. A
    power-container outage should not create hundreds of separate notifications
    without a parent incident."

Three mechanisms, applied in that order of authority:

1. **Declared parentage.** A definition names ``parent_alarm_key``. When that
   parent alarm is open and in scope, the child is a symptom: it joins the
   parent's incident and is not notified on its own.

2. **Dependency correlation.** The registry's typed relationships
   (``feeds``, ``contains``, ``hosts``, ``located_in``, ``part_of``,
   ``depends_on``, ``managed_by``, ...) describe which assets sit downstream of
   which. An alarm on an asset downstream of an already-alarming asset, within
   the correlation window, joins that asset's incident. This is a real graph
   walk over :class:`~homestead_twin.models.registry.AssetRelationship`, not a
   table of alarm keys, so a relationship added to the register tomorrow
   correlates without a code change.

3. **Flood guard.** More than N open alarms of the same definition, or more than
   N inside one subtree, within the window opens a single incident and
   suppresses the per-alarm notification.

Incident membership sets ``Alarm.suppressed`` on member alarms with a
``symptom:`` or ``flood:`` reason. That flag means "do not notify this alarm on
its own" -- the incident is notified instead. It never means the alarm was
discarded.

Deliberate non-members: some alarms opt out of being anybody's symptom because
being folded into an incident would hide exactly the thing they exist to report
(the independent secondary control node, the local alarm beacon, the BMS
discharge inhibit). Those definitions simply declare no parent and are marked
``is_root_candidate`` where they should anchor an incident themselves.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin.alarms.definitions import correlation_settings, definition_meta
from homestead_twin.alarms.evaluator import (
    ACTIVE_STATES,
    CLOSED_STATES,
    OPEN_STATES,
    SuppressionReason,
    as_utc,
    severity_rank,
)
from homestead_twin.config import Settings, get_settings
from homestead_twin.models.alarms import Alarm, AlarmDefinition, Incident
from homestead_twin.models.base import utcnow
from homestead_twin.models.registry import Asset, AssetRelationship

logger = logging.getLogger(__name__)

#: How each registry relationship type projects onto the downstream graph.
#:
#: ``forward``  -- ``from`` supplies/contains/hosts ``to``: an outage at ``from``
#:                 propagates to ``to``.
#: ``reverse``  -- ``from`` sits inside / depends on / is managed by ``to``: an
#:                 outage at ``to`` propagates to ``from``.
DOWNSTREAM_EDGE_TYPES: dict[str, str] = {
    "feeds": "forward",
    "contains": "forward",
    "hosts": "forward",
    "controls": "forward",
    "serves": "forward",
    "protects": "forward",
    "located_in": "reverse",
    "part_of": "reverse",
    "depends_on": "reverse",
    "managed_by": "reverse",
    "powered_by": "reverse",
}

DEFAULT_WINDOW_S = 900
DEFAULT_FLOOD_THRESHOLD = 5
DEFAULT_MAX_DEPTH = 8


@dataclass
class DependencyGraph:
    """Directed "an outage here propagates there" view of the registry."""

    downstream: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    upstream: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    neighbours: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    max_depth: int = DEFAULT_MAX_DEPTH

    @classmethod
    def from_session(
        cls,
        session: Session,
        *,
        edge_types: dict[str, str] | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        include_hierarchy: bool = True,
    ) -> "DependencyGraph":
        edge_types = edge_types or DOWNSTREAM_EDGE_TYPES
        graph = cls(defaultdict(set), defaultdict(set), defaultdict(set), max_depth)

        for relationship in session.scalars(select(AssetRelationship)).all():
            direction = edge_types.get(relationship.relationship_type)
            a, b = relationship.from_asset_id, relationship.to_asset_id
            graph.neighbours[a].add(b)
            graph.neighbours[b].add(a)
            if direction == "forward":
                graph.add_edge(a, b)
            elif direction == "reverse":
                graph.add_edge(b, a)

        if include_hierarchy:
            # The containment hierarchy is a dependency even where no explicit
            # relationship row exists: a room's failure reaches what is in it.
            for asset_id, parent_id in session.execute(
                select(Asset.asset_id, Asset.parent_id).where(Asset.parent_id.is_not(None))
            ).all():
                graph.add_edge(parent_id, asset_id)
                graph.neighbours[parent_id].add(asset_id)
                graph.neighbours[asset_id].add(parent_id)

        return graph

    def add_edge(self, source: str, target: str) -> None:
        if source == target:
            return
        self.downstream[source].add(target)
        self.upstream[target].add(source)

    def _walk(self, start: str, adjacency: dict[str, set[str]]) -> dict[str, int]:
        """Breadth-first reachability with depth, cycle-safe."""
        seen: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        visited = {start}
        while queue:
            node, depth = queue.popleft()
            if depth >= self.max_depth:
                continue
            for neighbour in adjacency.get(node, ()):  # type: ignore[arg-type]
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                seen[neighbour] = depth + 1
                queue.append((neighbour, depth + 1))
        return seen

    def descendants(self, asset_id: str) -> dict[str, int]:
        return self._walk(asset_id, self.downstream)

    def ancestors(self, asset_id: str) -> dict[str, int]:
        return self._walk(asset_id, self.upstream)

    def related(self, asset_id: str) -> dict[str, int]:
        """Undirected connectivity, used for ``parent_scope: related``."""
        return self._walk(asset_id, self.neighbours)


@dataclass
class CorrelationResult:
    opened: list[Incident] = field(default_factory=list)
    updated: list[Incident] = field(default_factory=list)
    closed: list[Incident] = field(default_factory=list)
    attached: int = 0
    symptoms: int = 0
    flood_suppressed: int = 0

    def as_dict(self) -> dict:
        return {
            "opened": len(self.opened),
            "updated": len(self.updated),
            "closed": len(self.closed),
            "attached": self.attached,
            "symptoms": self.symptoms,
            "flood_suppressed": self.flood_suppressed,
        }


class CorrelationEngine:
    """Groups open alarms into incidents."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        *,
        window_s: int = DEFAULT_WINDOW_S,
        flood_threshold: int = DEFAULT_FLOOD_THRESHOLD,
        max_depth: int = DEFAULT_MAX_DEPTH,
        edge_types: dict[str, str] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.window_s = window_s
        self.flood_threshold = flood_threshold
        self.max_depth = max_depth
        self.edge_types = edge_types or DOWNSTREAM_EDGE_TYPES
        self._graph: DependencyGraph | None = None
        self._definitions: dict[str, AlarmDefinition] | None = None

    # -- caches ------------------------------------------------------------

    @property
    def graph(self) -> DependencyGraph:
        if self._graph is None:
            self._graph = DependencyGraph.from_session(
                self.session, edge_types=self.edge_types, max_depth=self.max_depth
            )
        return self._graph

    @property
    def definitions(self) -> dict[str, AlarmDefinition]:
        if self._definitions is None:
            self._definitions = {
                d.alarm_key: d for d in self.session.scalars(select(AlarmDefinition)).all()
            }
        return self._definitions

    def invalidate(self) -> None:
        self._graph = None
        self._definitions = None

    # -- helpers -----------------------------------------------------------

    def _settings_for(self, alarm: Alarm) -> dict:
        definition = self.definitions.get(alarm.alarm_key)
        if definition is None:
            return {
                "window_s": self.window_s,
                "flood_threshold": self.flood_threshold,
                "is_root_candidate": False,
            }
        return correlation_settings(definition)

    def _within_window(self, first: Alarm, second: Alarm, window_s: int) -> bool:
        delta = abs((as_utc(first.detected_at) - as_utc(second.detected_at)).total_seconds())
        return delta <= window_s

    def open_alarms(self) -> list[Alarm]:
        # Only alarms that have actually activated participate. A candidate still
        # inside its on-delay may yet turn out to be a transient, and a transient
        # must not open an incident.
        statement = (
            select(Alarm)
            .where(Alarm.state.in_(ACTIVE_STATES))
            .order_by(Alarm.detected_at.asc(), Alarm.id.asc())
        )
        return list(self.session.scalars(statement).all())

    # -- root selection ----------------------------------------------------

    def _declared_parent(self, alarm: Alarm, candidates: list[Alarm]) -> Alarm | None:
        """Find the open parent alarm a definition declares, honouring parent_scope."""
        definition = self.definitions.get(alarm.alarm_key)
        if definition is None or not definition.parent_alarm_key:
            return None
        scope = definition_meta(definition).get("parent_scope", "related")
        window = self._settings_for(alarm)["window_s"]

        matches = [
            c
            for c in candidates
            if c.alarm_key == definition.parent_alarm_key
            and c.id != alarm.id
            and self._within_window(c, alarm, window)
        ]
        if not matches:
            return None

        if scope == "same_asset":
            matches = [c for c in matches if c.asset_id == alarm.asset_id]
        elif scope == "related":
            if alarm.asset_id:
                reachable = set(self.graph.related(alarm.asset_id)) | {alarm.asset_id}
                matches = [c for c in matches if not c.asset_id or c.asset_id in reachable]
        # scope == "any": every open instance qualifies.

        if not matches:
            return None
        return min(matches, key=lambda c: (as_utc(c.detected_at), c.id))

    def _upstream_parent(self, alarm: Alarm, by_asset: dict[str, list[Alarm]]) -> Alarm | None:
        """Nearest alarming asset upstream of this one in the registry graph."""
        if not alarm.asset_id:
            return None
        window = self._settings_for(alarm)["window_s"]
        ancestors = self.graph.ancestors(alarm.asset_id)
        best: tuple[int, dt.datetime, str] | None = None
        chosen: Alarm | None = None
        for ancestor_id, depth in ancestors.items():
            for candidate in by_asset.get(ancestor_id, ()):
                if candidate.id == alarm.id:
                    continue
                if not self._within_window(candidate, alarm, window):
                    continue
                key = (depth, as_utc(candidate.detected_at), candidate.id)
                if best is None or key < best:
                    best, chosen = key, candidate
        return chosen

    # -- main pass ---------------------------------------------------------

    def correlate(self, now: dt.datetime | None = None) -> CorrelationResult:
        """Group the currently open alarms into incidents."""
        now = now or utcnow()
        self.invalidate()
        result = CorrelationResult()
        alarms = self.open_alarms()
        if not alarms:
            self.close_resolved_incidents(now, result)
            return result

        by_id = {a.id: a for a in alarms}
        by_asset: dict[str, list[Alarm]] = defaultdict(list)
        for alarm in alarms:
            if alarm.asset_id:
                by_asset[alarm.asset_id].append(alarm)

        # 1. Parent selection: declared parentage first, then the dependency graph.
        parent_of: dict[str, str | None] = {}
        reason_of: dict[str, str] = {}
        for alarm in alarms:
            parent = self._declared_parent(alarm, alarms)
            if parent is not None:
                parent_of[alarm.id] = parent.id
                reason_of[alarm.id] = SuppressionReason.format(
                    SuppressionReason.SYMPTOM,
                    f"declared symptom of '{parent.alarm_key}' on {parent.asset_id}",
                )
                continue
            upstream = self._upstream_parent(alarm, by_asset)
            if upstream is not None:
                parent_of[alarm.id] = upstream.id
                reason_of[alarm.id] = SuppressionReason.format(
                    SuppressionReason.SYMPTOM,
                    f"downstream of '{upstream.alarm_key}' on {upstream.asset_id}",
                )
                continue
            parent_of[alarm.id] = None

        # Break any cycle the data could produce: an alarm may not be its own ancestor.
        for alarm_id in list(parent_of):
            seen: set[str] = set()
            cursor: str | None = alarm_id
            while cursor is not None and cursor not in seen:
                seen.add(cursor)
                cursor = parent_of.get(cursor)
            if cursor is not None:
                logger.warning("Correlation cycle at alarm %s; treating it as a root", cursor)
                parent_of[cursor] = None
                reason_of.pop(cursor, None)

        def root_of(alarm_id: str) -> str:
            cursor = alarm_id
            guard = 0
            while parent_of.get(cursor) is not None and guard < 64:
                cursor = parent_of[cursor]  # type: ignore[assignment]
                guard += 1
            return cursor

        # 2. Flood guard, part one: more than N alarms of the same definition with
        #    no common upstream cause still belong in one incident. Eleven services
        #    dying at once is one event to a human, whatever the graph says.
        self._merge_definition_floods(alarms, parent_of, reason_of, root_of, by_id)

        groups: dict[str, list[Alarm]] = defaultdict(list)
        for alarm in alarms:
            groups[root_of(alarm.id)].append(alarm)

        # 3. Which groups crossed a flood threshold and must not notify per member.
        flood_members = self._flood_members(alarms, groups, by_id)

        # 4. Materialise incidents.
        for root_id, members in groups.items():
            root = by_id[root_id]
            if len(members) == 1 and root_id not in flood_members:
                # A lone alarm only needs an incident when it is severe enough to
                # be worth a durable record for the post-event review.
                if severity_rank(root.severity) < severity_rank("critical"):
                    self._detach_if_stale(root, result)
                    continue
            flooded = root_id in flood_members
            incident = self._ensure_incident(root, members, now, result, flooded=flooded)
            for member in members:
                changed = member.incident_id != incident.id
                member.incident_id = incident.id
                if member.id == root_id:
                    # The root anchors the incident and is what gets notified.
                    if changed:
                        result.attached += 1
                    continue
                if flooded:
                    reason = SuppressionReason.format(
                        SuppressionReason.FLOOD,
                        f"flood guard: {len(members)} correlated alarms in incident {incident.id}",
                    )
                else:
                    reason = reason_of.get(
                        member.id,
                        SuppressionReason.format(
                            SuppressionReason.SYMPTOM, f"member of incident {incident.id}"
                        ),
                    )
                if not member.suppressed:
                    member.suppressed = True
                    member.suppression_reason = reason
                    result.symptoms += 1
                    if flooded:
                        result.flood_suppressed += 1
                elif SuppressionReason.kind(member.suppression_reason) in (
                    SuppressionReason.SYMPTOM,
                    SuppressionReason.FLOOD,
                ):
                    member.suppression_reason = reason
                if changed:
                    result.attached += 1

        self.close_resolved_incidents(now, result)
        self.session.flush()
        return result

    def _merge_definition_floods(
        self,
        alarms: list[Alarm],
        parent_of: dict[str, str | None],
        reason_of: dict[str, str],
        root_of,
        by_id: dict[str, Alarm],
    ) -> None:
        """Re-parent independent roots of one flooding definition onto the earliest.

        Only roots that are themselves alarms of the flooding definition are
        merged, so an unrelated incident that happens to contain one member is
        never swallowed.
        """
        by_key: dict[str, list[Alarm]] = defaultdict(list)
        for alarm in alarms:
            by_key[alarm.alarm_key].append(alarm)

        for key, members in by_key.items():
            definition = self.definitions.get(key)
            threshold = (
                correlation_settings(definition)["flood_threshold"]
                if definition is not None
                else self.flood_threshold
            )
            if len(members) <= threshold:
                continue
            ordered = sorted(members, key=lambda a: (as_utc(a.detected_at), a.id))
            anchor = next(
                (m for m in ordered if parent_of.get(root_of(m.id)) is None), None
            )
            if anchor is None:
                continue
            anchor_root = root_of(anchor.id)
            window = self._settings_for(anchor)["window_s"]
            for member in ordered:
                member_root = root_of(member.id)
                if member_root == anchor_root or parent_of.get(member_root) is not None:
                    continue
                if by_id[member_root].alarm_key != key:
                    continue
                if not self._within_window(by_id[member_root], by_id[anchor_root], window):
                    continue
                parent_of[member_root] = anchor_root
                reason_of[member_root] = SuppressionReason.format(
                    SuppressionReason.FLOOD,
                    f"flood guard: more than {threshold} concurrent '{key}' alarms",
                )

    def _flood_members(
        self,
        alarms: Iterable[Alarm],
        groups: dict[str, list[Alarm]],
        by_id: dict[str, Alarm],
    ) -> set[str]:
        """Root ids whose group crossed a flood threshold.

        "More than N", strictly: a root plus one symptom is a correlated pair,
        not a flood. Crossing the threshold does not silence the root -- the
        incident is still notified once. It marks the *members* so that fifty
        alarms cannot become fifty messages.
        """
        flooded: set[str] = set()
        root_by_alarm = {
            alarm.id: root_id for root_id, group in groups.items() for alarm in group
        }

        # (a) many alarms of the same definition
        by_key: dict[str, list[Alarm]] = defaultdict(list)
        for alarm in alarms:
            by_key[alarm.alarm_key].append(alarm)
        for key, members in by_key.items():
            definition = self.definitions.get(key)
            threshold = (
                correlation_settings(definition)["flood_threshold"]
                if definition is not None
                else self.flood_threshold
            )
            if len(members) > threshold:
                for member in members:
                    root_id = root_by_alarm.get(member.id)
                    if root_id is not None:
                        flooded.add(root_id)

        # (b) many alarms in one subtree
        for root_id, members in groups.items():
            threshold = self._settings_for(by_id[root_id])["flood_threshold"]
            if len(members) > threshold:
                flooded.add(root_id)
        return flooded

    # -- incident lifecycle -------------------------------------------------

    def _ensure_incident(
        self,
        root: Alarm,
        members: list[Alarm],
        now: dt.datetime,
        result: CorrelationResult,
        *,
        flooded: bool = False,
    ) -> Incident:
        incident: Incident | None = None
        if root.incident_id:
            incident = self.session.get(Incident, root.incident_id)
            if incident is not None and incident.state != "open":
                incident = None
        if incident is None:
            # Reuse an open incident already anchored on this alarm.
            incident = self.session.scalars(
                select(Incident).where(
                    Incident.root_cause_alarm_id == root.id, Incident.state == "open"
                )
            ).first()

        severity = max((m.severity for m in members), key=severity_rank)
        title = self._title(root, members)
        summary = self._summary(root, members, flooded=flooded)

        if incident is None:
            incident = Incident(
                title=title,
                severity=severity,
                root_cause_alarm_id=root.id,
                opened_at=now,
                state="open",
                summary=summary,
            )
            incident.created_at = now
            incident.updated_at = now
            self.session.add(incident)
            self.session.flush()
            result.opened.append(incident)
        else:
            if (incident.severity, incident.title, incident.summary) != (severity, title, summary):
                incident.severity = severity
                incident.title = title
                incident.summary = summary
                incident.updated_at = now
                result.updated.append(incident)
        return incident

    def _title(self, root: Alarm, members: list[Alarm]) -> str:
        definition = self.definitions.get(root.alarm_key)
        name = definition.name if definition else root.alarm_key
        scope = f" on {root.asset_id}" if root.asset_id else ""
        if len(members) == 1:
            return f"{name}{scope}"
        return f"{name}{scope} (+{len(members) - 1} correlated)"

    def _summary(self, root: Alarm, members: list[Alarm], *, flooded: bool = False) -> str:
        keys = sorted({m.alarm_key for m in members})
        assets = sorted({m.asset_id for m in members if m.asset_id})
        summary = (
            f"Root cause: {root.alarm_key} on {root.asset_id}. "
            f"{len(members)} member alarm(s) across {len(assets)} asset(s); "
            f"definitions involved: {', '.join(keys)}."
        )
        if flooded:
            summary += (
                " Flood guard engaged: member alarms are recorded but not notified "
                "individually (SDD 14.2)."
            )
        return summary

    def _detach_if_stale(self, alarm: Alarm, result: CorrelationResult) -> None:
        """A lone, no-longer-correlated alarm should not keep a stale incident link."""
        if alarm.incident_id is None:
            return
        incident = self.session.get(Incident, alarm.incident_id)
        if incident is not None and incident.root_cause_alarm_id == alarm.id:
            return
        alarm.incident_id = None
        if SuppressionReason.kind(alarm.suppression_reason) in (
            SuppressionReason.SYMPTOM,
            SuppressionReason.FLOOD,
        ):
            alarm.suppressed = False
            alarm.suppression_reason = None
        result.attached += 1

    def close_incident(
        self,
        incident: Incident | str,
        now: dt.datetime | None = None,
        *,
        note: str | None = None,
    ) -> Incident | None:
        """Close an incident once every member alarm has cleared."""
        now = now or utcnow()
        if isinstance(incident, str):
            found = self.session.get(Incident, incident)
            if found is None:
                return None
            incident = found
        members = list(
            self.session.scalars(select(Alarm).where(Alarm.incident_id == incident.id)).all()
        )
        if any(m.state not in CLOSED_STATES for m in members):
            return None
        incident.state = "closed"
        incident.closed_at = now
        incident.updated_at = now
        if note:
            incident.summary = f"{incident.summary or ''} {note}".strip()
        return incident

    def close_resolved_incidents(
        self, now: dt.datetime | None = None, result: CorrelationResult | None = None
    ) -> list[Incident]:
        now = now or utcnow()
        closed: list[Incident] = []
        for incident in self.session.scalars(
            select(Incident).where(Incident.state == "open")
        ).all():
            if self.close_incident(incident, now) is not None:
                closed.append(incident)
        if result is not None:
            result.closed.extend(closed)
        return closed

    # -- reporting ----------------------------------------------------------

    def incident_members(self, incident_id: str) -> list[Alarm]:
        return list(
            self.session.scalars(
                select(Alarm)
                .where(Alarm.incident_id == incident_id)
                .order_by(Alarm.detected_at.asc())
            ).all()
        )

    def notifiable_alarms(self) -> list[Alarm]:
        """Open, unsuppressed alarms that still deserve their own notification."""
        return [
            alarm
            for alarm in self.open_alarms()
            if alarm.state in ACTIVE_STATES and not alarm.suppressed
        ]
