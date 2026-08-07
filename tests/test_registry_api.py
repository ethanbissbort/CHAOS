"""Tests for the registry HTTP surface (SDD section 41).

The registry endpoints are read-only apart from ``POST /registry/reload``, which
re-runs the idempotent loader and needs a maintainer.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from chaos import topics
from chaos.models import Asset, ConfigurationRevision

INVERTER = "energy.inverter.power_container.01"
SITE = "site.site.primary.01"
BASE = "/api/v1"


@pytest.fixture()
def api(client, loaded_registry):
    """A test client whose database already holds the v0.3 package."""
    return client


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


def test_list_assets_is_paginated(api):
    response = api.get(f"{BASE}/assets", params={"limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 90
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert len(body["items"]) == 5

    first = body["items"][0]
    assert set(first) == {
        "asset_id",
        "domain",
        "asset_class",
        "name",
        "status",
        "criticality",
        "control_authority",
        "functional_position",
        "parent_id",
        "tags",
        "open_fields",
    }

    page_two = api.get(f"{BASE}/assets", params={"limit": 5, "offset": 5}).json()
    assert page_two["total"] == 90
    assert {item["asset_id"] for item in page_two["items"]}.isdisjoint(
        {item["asset_id"] for item in body["items"]}
    )


@pytest.mark.parametrize(
    ("params", "check"),
    [
        ({"domain": "energy"}, lambda item: item["domain"] == "energy"),
        ({"asset_class": "inverter"}, lambda item: item["asset_class"] == "inverter"),
        ({"criticality": "critical"}, lambda item: item["criticality"] == "critical"),
        ({"status": "planned"}, lambda item: item["status"] == "planned"),
        (
            {"q": "inverter"},
            lambda item: "inverter" in (item["asset_id"] + item["name"]).lower(),
        ),
    ],
)
def test_list_assets_filters(api, params, check):
    body = api.get(f"{BASE}/assets", params={**params, "limit": 500}).json()
    assert body["total"] > 0
    assert body["total"] < 90
    assert all(check(item) for item in body["items"])


def test_list_assets_rejects_a_bad_page_size(api):
    assert api.get(f"{BASE}/assets", params={"limit": 0}).status_code == 422
    assert api.get(f"{BASE}/assets", params={"offset": -1}).status_code == 422


def test_get_asset_returns_the_resolved_class_and_open_fields(api):
    response = api.get(f"{BASE}/assets/{INVERTER}")
    assert response.status_code == 200
    body = response.json()

    assert body["asset_id"] == INVERTER
    assert body["name"] == "Hybrid Inverter 1"
    assert body["domain"] == "energy"
    assert body["criticality"] == "critical"
    assert body["parent_id"] == "structure.room.power_container.power_zone"
    assert body["properties"]["rated_power_kw"] == 10.0
    # Unknowns stay visible rather than being filled in.
    assert body["properties"]["make_model"] == "TBD"
    assert "modbus_map" in body["open_fields"]

    definition = body["asset_class_definition"]
    assert definition["name"] == "inverter"
    assert "energy" in definition["allowed_domains"]
    assert "power_ac_output_kw" in definition["default_points"]

    assert body["point_count"] == 19
    assert body["child_count"] == 0


def test_get_unknown_asset_is_404(api):
    response = api.get(f"{BASE}/assets/no.such.asset.01")
    assert response.status_code == 404
    assert "no.such.asset.01" in response.json()["detail"]


def test_asset_points_endpoint(api):
    response = api.get(f"{BASE}/assets/{INVERTER}/points")
    assert response.status_code == 200
    points = response.json()
    assert len(points) == 19

    by_name = {point["point_name"]: point for point in points}
    power = by_name["power_ac_output_kw"]
    assert power["point_id"] == f"{INVERTER}/power_ac_output_kw"
    assert power["point_class"] == "AI"
    assert power["data_type"] == "float"
    assert power["unit"] == "kW"
    assert power["source"] == "binding"

    mode = by_name["mode_requested"]
    assert mode["control_capable"] is True
    # SDD 47: capable, but not permitted until commissioning says so.
    assert mode["automatic_control_allowed"] is False
    assert "automatic" in mode["enum_values"]

    assert api.get(f"{BASE}/assets/no.such.asset.01/points").status_code == 404


def test_asset_relationships_endpoint(api):
    response = api.get(f"{BASE}/assets/{INVERTER}/relationships")
    assert response.status_code == 200
    relationships = response.json()
    assert relationships

    for relationship in relationships:
        assert relationship["counterpart_id"] != INVERTER
        assert relationship["counterpart_name"]
        assert relationship["direction"] in {"incoming", "outgoing"}
        assert INVERTER in {relationship["from_asset_id"], relationship["to_asset_id"]}

    outgoing = api.get(f"{BASE}/assets/{INVERTER}/relationships", params={"direction": "outgoing"}).json()
    assert all(item["from_asset_id"] == INVERTER for item in outgoing)
    assert len(outgoing) < len(relationships)

    assert (
        api.get(f"{BASE}/assets/{INVERTER}/relationships", params={"direction": "sideways"}).status_code
        == 422
    )


def test_asset_tree_endpoint(api):
    response = api.get(f"{BASE}/assets/{SITE}/tree")
    assert response.status_code == 200
    tree = response.json()
    assert tree["asset_id"] == SITE
    assert tree["children"]

    def count(node):
        return 1 + sum(count(child) for child in node["children"])

    assert count(tree) == 90

    shallow = api.get(f"{BASE}/assets/{SITE}/tree", params={"depth": 1}).json()
    assert count(shallow) == 1 + len(shallow["children"])
    assert all(child["children"] == [] for child in shallow["children"])
    # child_count still reports the truth even when the branch is not expanded.
    assert any(child["child_count"] > 0 for child in shallow["children"])

    assert api.get(f"{BASE}/assets/no.such.asset.01/tree").status_code == 404


def test_asset_dependencies_endpoint(api):
    response = api.get(f"{BASE}/assets/{INVERTER}/dependencies")
    assert response.status_code == 200
    direct = {item["asset_id"] for item in response.json()}
    assert "energy.battery_bank.power_container.01" in direct

    transitive = api.get(f"{BASE}/assets/{INVERTER}/dependencies", params={"transitive": True}).json()
    assert {item["asset_id"] for item in transitive} >= direct


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------


def test_list_points_filters(api):
    everything = api.get(f"{BASE}/points", params={"limit": 1000}).json()
    assert everything["total"] == 701
    assert len(everything["items"]) == 701

    for_asset = api.get(f"{BASE}/points", params={"asset_id": INVERTER}).json()
    assert for_asset["total"] == 19
    assert {item["asset_id"] for item in for_asset["items"]} == {INVERTER}

    by_name = api.get(f"{BASE}/points", params={"point_name": "availability_state", "limit": 1000}).json()
    assert by_name["total"] > 1
    assert {item["point_name"] for item in by_name["items"]} == {"availability_state"}

    capable = api.get(f"{BASE}/points", params={"control_capable": True, "limit": 1000}).json()
    assert 0 < capable["total"] < everything["total"]
    assert all(item["control_capable"] for item in capable["items"])


def test_get_point_by_path_id_includes_the_binding(api):
    point_id = f"{INVERTER}/power_ac_output_kw"
    response = api.get(f"{BASE}/points/{point_id}")
    assert response.status_code == 200
    body = response.json()

    assert body["point_id"] == point_id
    assert body["asset_id"] == INVERTER
    assert body["point_name"] == "power_ac_output_kw"

    binding = body["binding"]
    assert binding["mqtt_topic"] == topics.telemetry_topic(INVERTER, "power_ac_output_kw")
    # Never manufacture a register address (SDD 47).
    assert binding["source_address"] == "TBD"
    assert binding["binding_status"] == "tbd"
    assert binding["command_topic"] is None


def test_get_unbound_point_has_no_binding(api):
    response = api.get(f"{BASE}/points/{INVERTER}/mode_requested")
    assert response.status_code == 200
    body = response.json()
    assert body["control_capable"] is True
    assert body["automatic_control_allowed"] is False
    assert body["binding"] is None


def test_get_unknown_point_is_404(api):
    assert api.get(f"{BASE}/points/{INVERTER}/not_a_point").status_code == 404
    assert api.get(f"{BASE}/points/nonsense").status_code == 404


def test_point_route_does_not_shadow_point_sub_resources(app, api):
    """``/points/<id>` must not swallow ``/points/<id>/current`` and friends.

    Telemetry, commands and history hang sub-resources off a point ID that
    itself contains a slash. A greedy path converter here would capture them
    and 404, depending only on router mount order.
    """
    paths = set(api.get("/openapi.json").json()["paths"])
    assert f"{BASE}/points/{{asset_id}}/{{point_name}}" in paths

    point_id = f"{INVERTER}/state_operating"
    sub_resources = [path for path in paths if path.startswith(f"{BASE}/points/{{point_id")]
    assert sub_resources, "another subsystem is expected to hang sub-resources off a point"

    for path in sub_resources:
        suffix = path.rsplit("}", 1)[-1]  # e.g. "/current"
        response = api.get(path.replace("{point_id:path}", point_id).replace("{point_id}", point_id))
        # If this route had captured the sub-resource it would report the whole
        # path as the point ID; the owning subsystem never names the suffix.
        assert suffix not in str(response.json()), (path, response.json())


# ---------------------------------------------------------------------------
# Registry-level views
# ---------------------------------------------------------------------------


def test_registry_summary_endpoint(api):
    response = api.get(f"{BASE}/registry/summary")
    assert response.status_code == 200
    summary = response.json()

    assert summary["assets"] == 90
    assert summary["points"] == 701
    assert summary["relationships"] == 99
    assert summary["bindings"] == 245
    assert sum(summary["assets_by_domain"].values()) == 90
    assert sum(summary["assets_by_status"].values()) == 90
    assert sum(summary["assets_by_criticality"].values()) == 90
    assert sum(summary["assets_by_class"].values()) == 90
    assert sum(summary["bindings_by_status"].values()) == 245
    assert summary["assets_with_open_fields"] == 90
    assert summary["points_control_capable"] > 0


def test_asset_class_dictionary_endpoint(api):
    response = api.get(f"{BASE}/registry/dictionary/asset-classes")
    assert response.status_code == 200
    classes = response.json()
    assert len(classes) == 72

    by_name = {item["name"]: item for item in classes}
    assert "inverter" in by_name
    assert by_name["inverter"]["allowed_domains"] == ["energy"]
    assert by_name["inverter"]["dictionary_status"]

    energy_only = api.get(f"{BASE}/registry/dictionary/asset-classes", params={"domain": "water"}).json()
    assert 0 < len(energy_only) < 72
    assert all("water" in item["allowed_domains"] for item in energy_only)


def test_point_dictionary_endpoint(api):
    response = api.get(f"{BASE}/registry/dictionary/points")
    assert response.status_code == 200
    definitions = response.json()
    assert len(definitions) == 214

    by_name = {item["name"]: item for item in definitions}
    assert by_name["soc_pct"]["unit"] == "%"
    assert by_name["availability_state"]["enum_values"] == [
        "online",
        "offline",
        "degraded",
        "unknown",
    ]

    capable = api.get(f"{BASE}/registry/dictionary/points", params={"control_capable": True}).json()
    assert 0 < len(capable) < 214
    assert all(item["control_capable"] for item in capable)

    searched = api.get(f"{BASE}/registry/dictionary/points", params={"q": "temperature"}).json()
    assert searched
    assert all("temperature" in item["name"] for item in searched)


def test_design_conflicts_endpoint_keeps_open_decisions_visible(api):
    response = api.get(f"{BASE}/registry/design-conflicts")
    assert response.status_code == 200
    body = response.json()

    assert body["site_id"] == SITE
    assert body["package_version"] == "0.3.0"
    conflicts = body["open_design_conflicts"]
    assert len(conflicts) >= 2

    by_id = {item["conflict_id"]: item for item in conflicts}
    assert "energy.capacity_revision" in by_id
    assert "power_container.common_failure_domain" in by_id
    # The superseded design is preserved, not deleted (SDD 46.1).
    assert "26.4" in by_id["energy.capacity_revision"]["predecessor_design"]


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------


def test_reload_requires_a_maintainer(api, operator_headers):
    assert api.post(f"{BASE}/registry/reload").status_code == 403
    assert api.post(f"{BASE}/registry/reload", headers=operator_headers).status_code == 403


def test_reload_reruns_the_loader(api, admin_headers, db_session):
    before = db_session.scalar(sa.select(sa.func.count()).select_from(Asset))

    response = api.post(f"{BASE}/registry/reload", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["assets"] == 90
    assert body["relationships"] == 99
    assert body["points"] == 701
    assert body["bindings"] == 245
    assert body["asset_classes"] == 72
    assert body["point_definitions"] == 214
    assert body["warnings"] == []
    assert body["package_version"] == "0.3.0"

    db_session.expire_all()
    assert db_session.scalar(sa.select(sa.func.count()).select_from(Asset)) == before

    # The reload is audited against the operator who asked for it.
    revisions = db_session.scalars(
        sa.select(ConfigurationRevision).order_by(ConfigurationRevision.revision)
    ).all()
    assert [revision.revision for revision in revisions] == [1, 2]
    assert revisions[-1].changed_by == admin_headers["X-Operator"]
    assert revisions[-1].target_type == "registry"


def test_registry_routes_are_mounted_under_the_versioned_prefix(api):
    paths = set(api.get("/openapi.json").json()["paths"])
    for path in (
        f"{BASE}/assets",
        f"{BASE}/assets/{{asset_id}}",
        f"{BASE}/assets/{{asset_id}}/points",
        f"{BASE}/assets/{{asset_id}}/relationships",
        f"{BASE}/assets/{{asset_id}}/tree",
        f"{BASE}/points",
        f"{BASE}/points/{{asset_id}}/{{point_name}}",
        f"{BASE}/registry/summary",
        f"{BASE}/registry/dictionary/asset-classes",
        f"{BASE}/registry/dictionary/points",
        f"{BASE}/registry/design-conflicts",
        f"{BASE}/registry/reload",
    ):
        assert path in paths, path


def test_openapi_schema_includes_the_registry_tag(api):
    schema = api.get("/openapi.json").json()
    tags = {
        tag
        for path in schema["paths"].values()
        for operation in path.values()
        for tag in operation.get("tags", [])
    }
    assert "registry" in tags
