"""Topology canvas and blast-radius tests (SDD 16.1, 25.5).

Two properties matter more than any other here, and both are negative:

1. **A node must never read as healthy because nothing is wrong with it.** Not
   one of the 137 assets in the v0.3 package is installed. If a `planned` asset
   can show as ``ok``, the canvas is a green wall that means nothing, and the
   first real fault will be read against a background of false assurance.
2. **The blast radius must be the alarm correlator's graph, not a lookalike.**
   If this endpoint and ``alarms/correlation.py`` disagree about what is
   downstream of the power container, the screen trains the operator on a model
   the platform will not act on. The tests below assert the two traversals
   return literally the same set.

As in ``test_annunciator.py``, the fixtures load the real design package rather
than a convenient miniature, because the properties under test are about how the
package and the platform meet.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from homestead_twin.alarms.correlation import DOWNSTREAM_EDGE_TYPES, DependencyGraph
from homestead_twin.api.routers.overview import (
    DEPLOYED_STATUSES,
    STATUS_DESIGN_ONLY,
    STATUS_EXPLANATIONS,
    STATUS_NO_DATA,
    STATUS_NO_POINTS,
    STATUS_NOT_DEPLOYED,
    STATUS_OK,
    STATUS_STALE,
)
from homestead_twin.api.routers.topology import (
    EDGE_SEMANTICS,
    GROUP_CLASSES,
    MAX_IMPACT_DEPTH,
    NODE_HEALTH,
    build_impact_graph,
    collect_nodes,
    node_health,
    reachable_with_paths,
    read_extensions,
)
from homestead_twin.models.registry import Asset

UI = "src/homestead_twin/web"

#: The asset SDD 16.1 tells the whole platform to assume the total loss of.
POWER_CONTAINER = "structure.structure.power_container.01"
RACK = "it.rack.power_container.01"
SERVER_ZONE = "structure.room.power_container.server_zone"
TWIN_API = "it.application_service.rack_01.digital_twin_api_01"
SECONDARY_NODE = "it.server.secondary_control_node.01"
CRITICAL_PANEL = "energy.panel.power_container.critical_01"
WATER_PUMPING_LOAD = "energy.load.site.water_pumping_01"
WELL_PUMP = "water.pump.well.01"

#: The package's real counts, so a silent change to the register is caught here.
REGISTER_ASSETS = 90
EXTENSION_ASSETS = 47
TOTAL_ASSETS = REGISTER_ASSETS + EXTENSION_ASSETS
TOTAL_RELATIONSHIPS = 99 + 92


# --------------------------------------------------------------- fixtures --


@pytest.fixture()
def seeded(db_session):
    """The real v0.3 register loaded into the test database."""
    load_package = pytest.importorskip("homestead_twin.registry.loader").load_package
    load_package(db_session)
    db_session.commit()
    return db_session


@pytest.fixture()
def graph(client, seeded):
    def _fetch(**params):
        response = client.get("/api/v1/topology", params=params)
        assert response.status_code == 200
        return response.json()

    return _fetch


@pytest.fixture()
def impact(client, seeded):
    def _fetch(asset_id, **params):
        response = client.get(f"/api/v1/topology/impact/{asset_id}", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    return _fetch


# ------------------------------------------------------------ node counts --


def test_the_whole_design_package_is_on_the_canvas(graph):
    """90 register assets plus the 47-asset water extension: 137 nodes."""
    data = graph()
    assert data["summary"]["nodes"] == TOTAL_ASSETS
    assert len(data["nodes"]) == TOTAL_ASSETS
    assert data["package"]["registry_nodes"] == REGISTER_ASSETS
    assert data["package"]["extension_nodes"] == EXTENSION_ASSETS


def test_every_typed_relationship_survives_with_its_type(graph):
    data = graph()
    assert data["summary"]["edges"] == TOTAL_RELATIONSHIPS
    # SDD 25.5 is a typed vocabulary; collapsing it to "connected" would throw
    # away the only thing that makes the graph operable.
    assert set(data["summary"]["by_relationship_type"]) == {e["relationship_type"] for e in data["edges"]}
    for edge in data["edges"]:
        assert edge["relationship_type"] in EDGE_SEMANTICS, edge["relationship_type"]
        assert edge["propagates"] == DOWNSTREAM_EDGE_TYPES.get(edge["relationship_type"])


def test_no_edge_dangles_off_the_graph(graph):
    data = graph()
    ids = {node["asset_id"] for node in data["nodes"]}
    for edge in data["edges"]:
        assert edge["from_asset_id"] in ids
        assert edge["to_asset_id"] in ids


def test_extensions_can_be_excluded_and_say_so(graph):
    data = graph(include_extensions=False)
    assert data["summary"]["nodes"] == REGISTER_ASSETS
    assert data["package"]["extension_nodes"] == 0
    assert all(node["source"] == "registry" for node in data["nodes"])


def test_the_water_extension_is_declared_not_smuggled(graph):
    """It is not in the registry database; the payload must admit that."""
    data = graph()
    extension = next(e for e in data["package"]["extensions"] if e["file"] == "water_assets.yaml")
    assert extension["available"] is True
    assert extension["assets_applied"] == EXTENSION_ASSETS
    assert extension["relationships_applied"] == 92
    assert "not merged into the registry database" in extension["note"]
    water = [n for n in data["nodes"] if n["source"].startswith("extension:")]
    assert len(water) == EXTENSION_ASSETS
    # No points, because the loader has never materialised any for them.
    assert all(node["points"]["total"] == 0 for node in water)


def test_node_ordering_is_stable_between_polls(graph):
    first = [node["asset_id"] for node in graph()["nodes"]]
    second = [node["asset_id"] for node in graph()["nodes"]]
    assert first == second
    assert first == sorted(first), "the canvas layout is seeded from this order"


# ---------------------------------------------------------------- groups --


def test_groups_come_from_the_containment_hierarchy(graph):
    data = graph()
    groups = {g["group_id"]: g for g in data["groups"]}
    assert POWER_CONTAINER in groups
    assert RACK in groups
    for group in groups.values():
        assert group["asset_class"] in GROUP_CLASSES
        assert group["member_count"] == len(group["members"])
        assert group["group_id"] not in group["members"]


def test_group_membership_is_the_whole_subtree_not_only_direct_children(graph):
    """Everything in the container shares the container's fate, at any depth."""
    groups = {g["group_id"]: g for g in graph()["groups"]}
    container = groups[POWER_CONTAINER]
    assert SERVER_ZONE in container["direct_children"]
    assert RACK not in container["direct_children"]
    assert RACK in container["members"]
    assert TWIN_API in container["members"], "a service two levels down is still inside"
    assert set(groups[RACK]["members"]) <= set(container["members"])


def test_group_nesting_levels_are_derived_not_declared(graph):
    groups = {g["group_id"]: g for g in graph()["groups"]}
    assert groups["site.site.primary.01"]["level"] == 0
    assert groups[POWER_CONTAINER]["level"] == 1
    assert groups[SERVER_ZONE]["level"] == 2
    assert groups[RACK]["level"] == 3
    assert groups[RACK]["parent_group_id"] == SERVER_ZONE


def test_a_group_only_exists_where_something_is_inside_it(graph):
    for group in graph()["groups"]:
        assert group["member_count"] > 0


# ------------------------------------------------------ health vocabulary --


def test_health_vocabulary_is_the_overview_vocabulary(graph):
    """No parallel vocabulary. Every word must be one overview already speaks."""
    data = graph()
    assert set(data["health_vocabulary"]) == set(NODE_HEALTH)
    known = set(STATUS_EXPLANATIONS) | {"alarm", "degraded"}
    assert set(NODE_HEALTH) == known
    for word, sentence in data["health_vocabulary"].items():
        assert sentence.strip(), word


def test_planned_assets_read_not_deployed_never_ok(graph):
    """The property this whole screen rests on.

    Every asset in v0.3 is concept, planned or reserve. A green canvas here
    would be a lie the operator would learn to trust.
    """
    data = graph()
    assert data["summary"]["deployed_nodes"] == 0
    assert data["summary"]["by_health"][STATUS_OK] == 0
    assert data["summary"]["by_health"][STATUS_NOT_DEPLOYED] == TOTAL_ASSETS
    for node in data["nodes"]:
        assert node["status"] not in DEPLOYED_STATUSES
        assert node["health"] == STATUS_NOT_DEPLOYED
        assert node["health"] != STATUS_OK
        assert node["health_note"] == NODE_HEALTH[STATUS_NOT_DEPLOYED]


def test_no_alarm_is_not_evidence_of_health(graph):
    """ "Nothing is wrong" and "nothing is there" must not render the same."""
    data = graph()
    silent = [n for n in data["nodes"] if n["alarms"]["active"] == 0]
    assert len(silent) == TOTAL_ASSETS
    assert not [n for n in silent if n["health"] == STATUS_OK]


@pytest.mark.parametrize(
    ("status_value", "points", "bound", "reporting", "stale", "severity", "expected"),
    [
        ("planned", 0, 0, 0, 0, None, STATUS_NOT_DEPLOYED),
        ("concept", 8, 8, 8, 0, None, STATUS_NOT_DEPLOYED),
        ("reserve", 0, 0, 0, 0, None, STATUS_NOT_DEPLOYED),
        ("retired", 4, 4, 4, 0, None, STATUS_NOT_DEPLOYED),
        # A planned asset with a critical alarm is a data-quality problem, not a
        # plant state: deployment is checked first at node scope.
        ("planned", 0, 0, 0, 0, "critical", STATUS_NOT_DEPLOYED),
        ("installed", 0, 0, 0, 0, None, STATUS_NO_POINTS),
        ("installed", 6, 0, 0, 0, None, STATUS_DESIGN_ONLY),
        ("commissioned", 6, 6, 0, 0, None, STATUS_NO_DATA),
        ("active", 6, 6, 3, 3, None, STATUS_STALE),
        ("active", 6, 6, 3, 1, None, STATUS_OK),
        ("active", 6, 6, 3, 0, "critical", "alarm"),
        ("active", 6, 6, 3, 0, "major", "degraded"),
        ("failed", 6, 6, 3, 0, None, "degraded"),
        # An unrecognised lifecycle word must never fall through to ok.
        ("teleported", 6, 6, 3, 0, None, STATUS_NOT_DEPLOYED),
    ],
)
def test_node_health_cascade(status_value, points, bound, reporting, stale, severity, expected):
    assert (
        node_health(
            status_value,
            points=points,
            bound=bound,
            reporting=reporting,
            stale=stale,
            worst_severity=severity,
        )
        == expected
    )


def test_a_deployed_reporting_asset_can_reach_ok(db_session, settings, seeded):
    """The cascade is strict, not stuck: real data does produce ``ok``."""
    from homestead_twin.models.registry import Point, PointBinding
    from homestead_twin.models.telemetry import CurrentState

    asset = db_session.scalars(select(Asset).where(Asset.asset_id == RACK)).one()
    asset.status = "commissioned"
    point = db_session.scalars(select(Point).where(Point.asset_id == RACK)).first()
    assert point is not None, "the rack carries points in the v0.3 package"
    binding = db_session.get(PointBinding, point.point_id)
    if binding is None:
        binding = PointBinding(point_id=point.point_id, asset_id=RACK, point_name=point.point_name)
        db_session.add(binding)
    binding.binding_status = "bound"
    db_session.add(
        CurrentState(
            point_id=point.point_id,
            asset_id=RACK,
            point_name=point.point_name,
            value_numeric=21.5,
            quality="good",
            ts=dt.datetime.now(dt.UTC),
            stale_after_s=3600,
        )
    )
    db_session.commit()

    nodes, _ = collect_nodes(db_session, settings)
    rack = next(n for n in nodes if n["asset_id"] == RACK)
    assert rack["health"] == STATUS_OK
    assert rack["points"]["reporting"] == 1


# --------------------------------------------------------------- impact ----


def test_the_container_takes_the_rack_and_its_dependants_with_it(impact):
    """SDD 16.1's premise, made computable."""
    data = impact(POWER_CONTAINER)
    affected = {item["asset_id"]: item for item in data["affected"]}

    assert RACK in affected
    assert affected[RACK]["depth"] == 2, "container -> server zone -> rack"
    for downstream in (TWIN_API, "it.server.rack_01.r740xd_01", "energy.ups.rack_01.01"):
        assert downstream in affected, downstream

    # The container is a common-mode failure for most of the homestead.
    assert data["summary"]["affected_total"] == 98
    assert data["summary"]["unaffected_total"] == 38
    assert data["summary"]["by_criticality"]["critical"] == 50


def test_the_path_explains_how_the_failure_arrived(impact):
    data = impact(POWER_CONTAINER)
    rack = next(item for item in data["affected"] if item["asset_id"] == RACK)
    assert [step["asset_id"] for step in rack["path"]] == [POWER_CONTAINER, SERVER_ZONE, RACK]
    assert rack["path"][0]["via"] is None
    for step in rack["path"][1:]:
        assert step["via"], "every hop after the origin names the relationship that carried it"


def test_impact_is_ordered_worst_criticality_first(impact):
    data = impact(POWER_CONTAINER)
    order = ["life_safety", "critical", "important", "discretionary"]
    ranks = [order.index(item["criticality"]) for item in data["affected"]]
    assert ranks == sorted(ranks)


def test_propagation_follows_supply_direction_not_the_other_way(impact):
    """`feeds` runs from source to sink; the sink's loss must not kill the source."""
    downstream = {item["asset_id"] for item in impact(CRITICAL_PANEL)["affected"]}
    assert WATER_PUMPING_LOAD in downstream

    upstream = {item["asset_id"] for item in impact(WATER_PUMPING_LOAD)["affected"]}
    assert CRITICAL_PANEL not in upstream, "a load failing must not take out its panel"


def test_containment_edges_are_walked_in_reverse(impact):
    """`located_in` points from occupant to place; failure runs the other way."""
    zone = "structure.room.power_container.power_zone"
    inverter = "energy.inverter.power_container.01"
    assert inverter in {item["asset_id"] for item in impact(zone)["affected"]}
    assert zone not in {item["asset_id"] for item in impact(inverter)["affected"]}


def test_depends_on_is_walked_in_reverse(impact):
    """`water.pump.well.01 depends_on the water pumping load`: lose the load, lose the pump."""
    assert WELL_PUMP in {item["asset_id"] for item in impact(WATER_PUMPING_LOAD)["affected"]}
    assert WATER_PUMPING_LOAD not in {item["asset_id"] for item in impact(WELL_PUMP)["affected"]}


def test_monitoring_edges_do_not_propagate_failure(impact):
    """A failed smoke detector does not set the rack on fire."""
    sensor = "safety.safety_sensor.rack_01.ap9512thblk_01"
    assert RACK not in {item["asset_id"] for item in impact(sensor)["affected"]}
    assert "monitors" in impact(sensor)["rules"]["excluded"]


def test_a_lost_monitor_is_reported_as_lost_observability(impact):
    monitor = "it.environmental_monitor.power_container.netbotz_500_01"
    data = impact(monitor)
    blinded = {item["asset_id"] for item in data["observability_lost"]}
    assert POWER_CONTAINER in blinded, "the container survives but loses its eyes"


def test_a_lost_backup_is_reported_as_lost_redundancy_not_as_an_outage(impact):
    """`backs_up` inverted would be a lie: losing a spare is not losing the plant."""
    data = impact(SECONDARY_NODE)
    assert TWIN_API not in {item["asset_id"] for item in data["affected"]}
    lost = {item["asset_id"]: item for item in data["redundancy_lost"]}
    assert TWIN_API in lost
    assert SECONDARY_NODE in lost[TWIN_API]["backed_up_by"]
    assert "backs_up" in data["rules"]["excluded"]


def test_symmetric_links_do_not_propagate(impact):
    assert "connected_to" in impact(RACK)["rules"]["excluded"]
    assert "connected_to" not in DOWNSTREAM_EDGE_TYPES


def test_impact_is_cycle_safe(impact):
    """The register really does contain cycles.

    ``water.valve.orchard.zone_01 serves agriculture.irrigation_zone.orchard.01``
    and the zone contains the valve, so the graph loops. The walk must terminate
    and must never report an asset as downstream of itself.
    """
    valve = "water.valve.orchard.zone_01"
    data = impact(valve)
    ids = [item["asset_id"] for item in data["affected"]]
    assert valve not in ids
    assert len(ids) == len(set(ids))
    assert "agriculture.irrigation_zone.orchard.01" in ids


def test_every_node_can_be_asked_without_looping(client, seeded):
    """A cycle anywhere in the register must not hang any single query."""
    nodes = client.get("/api/v1/topology").json()["nodes"]
    for node in nodes:
        response = client.get(f"/api/v1/topology/impact/{node['asset_id']}", params={"max_depth": 4})
        assert response.status_code == 200, node["asset_id"]
        payload = response.json()
        assert node["asset_id"] not in {i["asset_id"] for i in payload["affected"]}


def test_the_walk_is_depth_bounded_and_says_when_it_bound(impact):
    shallow = impact("site.site.primary.01", max_depth=2)
    deep = impact("site.site.primary.01", max_depth=MAX_IMPACT_DEPTH)
    assert shallow["summary"]["affected_total"] < deep["summary"]["affected_total"]
    assert shallow["summary"]["max_depth_reached"] <= 2
    assert shallow["summary"]["depth_bounded"] is True
    assert deep["summary"]["depth_bounded"] is False


def test_unknown_asset_is_a_404_not_an_empty_blast_radius(client, seeded):
    response = client.get("/api/v1/topology/impact/energy.inverter.does_not_exist.99")
    assert response.status_code == 404
    assert "not in the topology graph" in response.json()["detail"]


def test_impact_declares_the_rules_it_used(impact):
    rules = impact(POWER_CONTAINER)["rules"]
    assert set(rules["forward"]) == {t for t, d in DOWNSTREAM_EDGE_TYPES.items() if d == "forward"}
    assert set(rules["reverse"]) == {t for t, d in DOWNSTREAM_EDGE_TYPES.items() if d == "reverse"}
    assert "hierarchy" in rules
    for name, why in rules["excluded"].items():
        assert name not in DOWNSTREAM_EDGE_TYPES
        assert why.strip(), name


def test_caveats_admit_what_the_traversal_cannot_know(impact):
    caveats = " ".join(impact(POWER_CONTAINER)["caveats"]).lower()
    # Four parallel inverters are modelled as four assets feeding one bus, so
    # one inverter's blast radius overstates the loss. Saying so is the point.
    assert "redundancy" in caveats
    assert "structural" in caveats


# ------------------------------------------- the correlator is the source --


def test_impact_walk_returns_exactly_the_correlators_descendants(db_session, settings, seeded):
    """The reuse guarantee, asserted rather than assumed.

    ``reachable_with_paths`` adds a predecessor map to the correlator's walk.
    If it ever returned a different set, the canvas would be showing an operator
    a blast radius the alarm engine will not act on.
    """
    nodes, extensions = collect_nodes(db_session, settings)
    known = {node["asset_id"] for node in nodes}
    graph, _ = build_impact_graph(db_session, extensions, known)
    for asset_id in (POWER_CONTAINER, RACK, CRITICAL_PANEL, WATER_PUMPING_LOAD, "site.site.primary.01"):
        depths, _ = reachable_with_paths(graph.downstream, asset_id, graph.max_depth)
        assert depths == graph.descendants(asset_id), asset_id


def test_the_registry_half_of_the_graph_is_the_correlators_graph_untouched(db_session, settings, seeded):
    """The overlay may only add. It must never re-point a registry edge."""
    nodes, extensions = collect_nodes(db_session, settings)
    known = {node["asset_id"] for node in nodes}
    extended, _ = build_impact_graph(db_session, extensions, known)
    plain = DependencyGraph.from_session(db_session)
    for source, targets in plain.downstream.items():
        assert targets <= extended.downstream[source], source


def test_edge_labels_agree_with_the_graph_that_is_walked(db_session, settings, seeded):
    """A label map that disagreed with the adjacency would explain a path that
    was never taken."""
    nodes, extensions = collect_nodes(db_session, settings)
    known = {node["asset_id"] for node in nodes}
    graph, labels = build_impact_graph(db_session, extensions, known)
    from_adjacency = {(src, dst) for src, targets in graph.downstream.items() for dst in targets}
    assert from_adjacency == set(labels)
    for reasons in labels.values():
        assert reasons and all(reason.strip() for reason in reasons)


def test_hierarchy_propagates_even_without_a_relationship_row(db_session, settings, seeded):
    """Parentage is a dependency in its own right (the correlator says so)."""
    nodes, extensions = collect_nodes(db_session, settings)
    known = {node["asset_id"] for node in nodes}
    graph, labels = build_impact_graph(db_session, extensions, known)
    assert SERVER_ZONE in graph.downstream[POWER_CONTAINER]
    assert "contains (hierarchy)" in labels[(POWER_CONTAINER, SERVER_ZONE)]


def test_extension_assets_join_the_graph_through_their_parents(db_session, settings, seeded):
    nodes, extensions = collect_nodes(db_session, settings)
    known = {node["asset_id"] for node in nodes}
    graph, _ = build_impact_graph(db_session, extensions, known)
    # The well pump is a water-extension asset; the site contains the well.
    assert WELL_PUMP in graph.descendants("site.site.primary.01")


# ----------------------------------------------------------- package read --


def test_read_extensions_reports_a_missing_document_rather_than_raising(tmp_path):
    documents = read_extensions(tmp_path)
    assert documents
    for document in documents:
        assert document["available"] is False
        assert "not present" in document["error"]
        assert document["assets"] == []


def test_read_extensions_parses_the_real_water_package():
    documents = read_extensions("data")
    water = next(d for d in documents if d["file"] == "water_assets.yaml")
    assert water["available"] is True
    assert len(water["assets"]) == EXTENSION_ASSETS
    assert len(water["relationships"]) == 92


# -------------------------------------------------------------------- ui --


def test_topology_ui_files_exist_and_are_self_contained():
    import re
    from pathlib import Path

    root = Path(UI)
    for name in ("views/topology.js", "topology.css"):
        path = root / name
        assert path.is_file(), f"{name} missing"
        text = path.read_text(encoding="utf-8")
        # The canvas must work with the internet down; that is the condition the
        # homestead is designed for (SDD 5.1, FR-006).
        # Not one external reference, not even in a comment: the same rule
        # test_overview_api enforces across every web asset (SDD 5.1, FR-006).
        assert not re.search(r"https?://(?!www\.w3\.org)", text), f"{name} references an external host"
        assert "cdn" not in text.lower().replace("cdnote", "")
        for forbidden in ("import(", "importScripts", "eval("):
            assert forbidden not in text, f"{name} uses {forbidden}"


def test_topology_view_uses_the_shared_result_envelope():
    from pathlib import Path

    text = Path(UI, "views/topology.js").read_text(encoding="utf-8")
    assert "from '../api.js'" in text
    # api.js never throws; the view must read result.ok rather than try/catch.
    assert "result.ok" in text
    assert "resultProblem" in text
    assert "/topology/impact/" in text


def test_topology_css_uses_the_consoles_tokens_not_a_second_palette():
    import re
    from pathlib import Path

    text = Path(UI, "topology.css").read_text(encoding="utf-8")
    # A hard-coded hex here would drift out of the console's light mode.
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", text)
    assert not literals, f"hard-coded colours in topology.css: {literals}"
    for token in ("--st-ok", "--st-notdeployed", "--sev-critical", "--bg-raised"):
        assert token in text, token


def test_the_view_is_registered_and_deep_linkable():
    from pathlib import Path

    app = Path(UI, "app.js").read_text(encoding="utf-8")
    index = Path(UI, "index.html").read_text(encoding="utf-8")
    assert "/topology" in app and "assetId" in app
    assert 'data-nav="topology"' in index
    assert "i-topology" in index


def test_layout_is_documented_as_deterministic():
    """Operators build muscle memory from stable positions (SDD 17.3's premise
    for the annunciator, applied here)."""
    from pathlib import Path

    text = Path(UI, "views/topology.js").read_text(encoding="utf-8")
    assert "deterministic" in text.lower()
    # No physics: a simulation that settles differently on each load would move
    # the battery bank between shifts.
    for banned in ("Math.random", "forceSimulation", "d3.force", "Date.now() *"):
        assert banned not in text, f"{banned} would make the layout non-reproducible"
