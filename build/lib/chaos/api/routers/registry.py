"""Registry API: asset identity, topology, points and vendor bindings.

Implements the read surface of SDD section 41 plus the additions the operations
UI needs: the containment tree, the dictionaries, the registry roll-up and the
unresolved design decisions (SDD section 46.1) -- the last one so that
"battery chemistry is undecided" is visible on a screen instead of buried in a
YAML comment.

Everything here is read-only except ``POST /registry/reload``, which re-runs the
idempotent package loader and requires a maintainer.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from chaos.api.deps import AppSettings, DbSession, MaintainerPrincipal
from chaos.registry import loader, service
from chaos.registry.loader import RegistryLoadError

router = APIRouter(tags=["registry"])


def _require_asset(session, asset_id: str):
    asset = service.get_asset(session, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown asset: {asset_id}")
    return asset


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AssetSummary(BaseModel):
    """The asset fields a list view needs."""

    model_config = ConfigDict(from_attributes=True)

    asset_id: str
    domain: str
    asset_class: str
    name: str
    status: str
    criticality: str
    control_authority: str
    functional_position: bool
    parent_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    open_fields: list[str] = Field(default_factory=list)


class AssetClassOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    purpose: str | None = None
    allowed_domains: list[str] = Field(default_factory=list)
    required_properties: list[str] = Field(default_factory=list)
    default_points: list[str] = Field(default_factory=list)
    source_section: str | None = None
    dictionary_status: str | None = None


class AssetDetail(AssetSummary):
    """Full asset record including the resolved class definition."""

    location: dict[str, Any] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)
    network: dict[str, Any] = Field(default_factory=dict)
    power: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[Any] = Field(default_factory=list)
    manual_override: dict[str, Any] = Field(default_factory=dict)
    documentation: list[Any] = Field(default_factory=list)
    maintenance_plan: dict[str, Any] = Field(default_factory=dict)
    point_profile_refs: list[str] = Field(default_factory=list)
    source_refs: list[Any] = Field(default_factory=list)
    notes: list[Any] = Field(default_factory=list)
    asset_class_definition: AssetClassOut | None = None
    point_count: int = 0
    child_count: int = 0


class AssetPageOut(BaseModel):
    items: list[AssetSummary]
    total: int
    limit: int
    offset: int


class PointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    point_id: str
    asset_id: str
    point_name: str
    point_class: str
    data_type: str
    unit: str | None = None
    enum_values: list[str] | None = None
    control_capable: bool
    automatic_control_allowed: bool
    source: str
    historian_policy: str | None = None
    stale_after_s: int | None = None
    description: str | None = None


class BindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    point_id: str
    asset_id: str
    point_name: str
    binding_status: str
    source_protocol: str | None = None
    source_address: str | None = None
    mqtt_topic: str | None = None
    command_topic: str | None = None
    sample_interval_s: int | None = None
    publish_interval_s: int | None = None
    stale_after_s: int | None = None
    historian_policy: str | None = None
    quality_policy: str | None = None
    automatic_control_allowed: bool
    notes: list[str] = Field(default_factory=list)


class PointDetail(PointOut):
    binding: BindingOut | None = None


class PointPageOut(BaseModel):
    items: list[PointOut]
    total: int
    limit: int
    offset: int


class RelationshipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    relationship_id: str
    relationship_type: str
    direction: str
    from_asset_id: str
    to_asset_id: str
    counterpart_id: str
    counterpart_name: str | None = None
    counterpart_class: str | None = None
    counterpart_domain: str | None = None
    counterpart_status: str | None = None
    status: str
    notes: list[str] = Field(default_factory=list)


class TreeNode(BaseModel):
    asset_id: str | None = None
    name: str | None = None
    domain: str | None = None
    asset_class: str | None = None
    status: str | None = None
    criticality: str | None = None
    child_count: int = 0
    children: list[TreeNode] = Field(default_factory=list)


class PointDefinitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    default_class: str
    allowed_classes: list[str] = Field(default_factory=list)
    data_type: str
    unit: str | None = None
    description: str | None = None
    control_capable: bool
    automatic_control_default: bool
    enum_values: list[str] | None = None
    applicable_asset_classes: list[str] = Field(default_factory=list)
    source_section: str | None = None
    dictionary_status: str | None = None


class RegistrySummaryOut(BaseModel):
    assets: int
    points: int
    relationships: int
    bindings: int
    assets_by_domain: dict[str, int]
    assets_by_status: dict[str, int]
    assets_by_criticality: dict[str, int]
    assets_by_class: dict[str, int]
    relationships_by_type: dict[str, int]
    assets_with_open_fields: int
    open_field_count: int
    bindings_by_status: dict[str, int]
    points_control_capable: int
    points_automatic_control_allowed: int


class DesignConflictsOut(BaseModel):
    """Unresolved design decisions carried by the register (SDD 46.1)."""

    site_id: str | None = None
    package_version: str | None = None
    design_stage: str | None = None
    authoritative_design_document: str | None = None
    open_design_conflicts: list[dict[str, Any]] = Field(default_factory=list)


class LoadResultOut(BaseModel):
    asset_classes: int
    point_definitions: int
    point_profiles: int
    assets: int
    relationships: int
    points: int
    bindings: int
    locations: int
    external_identifiers: int
    warnings: list[str] = Field(default_factory=list)
    skipped_assets: list[str] = Field(default_factory=list)
    package_version: str | None = None
    data_dir: str | None = None
    revision: int | None = None


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


@router.get("/assets", response_model=AssetPageOut, summary="List assets")
def list_assets(
    session: DbSession,
    domain: Annotated[str | None, Query(description="Domain vocabulary value")] = None,
    asset_class: Annotated[str | None, Query(description="Asset-class dictionary name")] = None,
    status_filter: Annotated[str | None, Query(alias="status", description="Lifecycle status")] = None,
    criticality: Annotated[str | None, Query(description="Criticality level")] = None,
    tag: Annotated[str | None, Query(description="Exact tag match")] = None,
    q: Annotated[str | None, Query(description="Substring of asset_id or name")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AssetPageOut:
    page = service.list_assets(
        session,
        domain=domain,
        asset_class=asset_class,
        status=status_filter,
        criticality=criticality,
        tag=tag,
        q=q,
        limit=limit,
        offset=offset,
    )
    return AssetPageOut(
        items=[AssetSummary.model_validate(asset) for asset in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/assets/{asset_id}", response_model=AssetDetail, summary="Get one asset")
def get_asset(session: DbSession, asset_id: str) -> AssetDetail:
    asset = service.get_asset(session, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown asset: {asset_id}")

    detail = AssetDetail.model_validate(asset)
    class_def = service.get_asset_class(session, asset.asset_class)
    if class_def is not None:
        detail.asset_class_definition = AssetClassOut.model_validate(class_def)
    detail.point_count = service.count_asset_points(session, asset_id)
    detail.child_count = service.count_children(session, asset_id)
    return detail


@router.get(
    "/assets/{asset_id}/points",
    response_model=list[PointOut],
    summary="Points materialised for an asset",
)
def get_asset_points(session: DbSession, asset_id: str) -> list[PointOut]:
    _require_asset(session, asset_id)
    return [PointOut.model_validate(point) for point in service.get_asset_points(session, asset_id)]


@router.get(
    "/assets/{asset_id}/relationships",
    response_model=list[RelationshipOut],
    summary="Typed relationships touching an asset",
)
def get_asset_relationships(
    session: DbSession,
    asset_id: str,
    direction: Annotated[Literal["both", "outgoing", "incoming"], Query()] = "both",
) -> list[RelationshipOut]:
    _require_asset(session, asset_id)
    return [
        RelationshipOut.model_validate(view)
        for view in service.get_asset_relationships(session, asset_id, direction=direction)
    ]


@router.get(
    "/assets/{asset_id}/tree",
    response_model=TreeNode,
    summary="Containment subtree below an asset",
)
def get_asset_tree(
    session: DbSession,
    asset_id: str,
    depth: Annotated[int | None, Query(ge=1, description="Generations to expand")] = None,
) -> TreeNode:
    try:
        tree = service.get_asset_tree(session, root_id=asset_id, depth=depth)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown asset: {asset_id}") from None
    return TreeNode.model_validate(tree)


@router.get(
    "/assets/{asset_id}/dependencies",
    response_model=list[AssetSummary],
    summary="Assets this asset depends on",
)
def get_asset_dependencies(
    session: DbSession,
    asset_id: str,
    transitive: Annotated[bool, Query(description="Walk the dependency closure")] = False,
) -> list[AssetSummary]:
    _require_asset(session, asset_id)
    return [
        AssetSummary.model_validate(asset)
        for asset in service.get_dependencies(session, asset_id, transitive=transitive)
    ]


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------


@router.get("/points", response_model=PointPageOut, summary="List point instances")
def list_points(
    session: DbSession,
    asset_id: Annotated[str | None, Query()] = None,
    point_name: Annotated[str | None, Query()] = None,
    control_capable: Annotated[bool | None, Query()] = None,
    automatic_control_allowed: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PointPageOut:
    items, total = service.list_points(
        session,
        asset_id=asset_id,
        point_name=point_name,
        control_capable=control_capable,
        automatic_control_allowed=automatic_control_allowed,
        limit=limit,
        offset=offset,
    )
    return PointPageOut(
        items=[PointOut.model_validate(point) for point in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# A canonical point ID is ``<asset_id>/<point_name>`` (SDD section 26.2), so
# the route has to span a slash -- but it must span *exactly* one. A greedy
# ``{point_id:path}`` would also swallow the sub-resources other subsystems
# hang off a point (``/points/<id>/current``, ``/points/<id>/history``), and
# whichever router is mounted first would win. Two plain segments express the
# same URL, cannot match a third segment, and keep route order irrelevant.
# Declared after ``/points`` so the exact list route keeps precedence.
@router.get(
    "/points/{asset_id}/{point_name}",
    response_model=PointDetail,
    summary="Get one point instance and its binding",
)
def get_point(session: DbSession, asset_id: str, point_name: str) -> PointDetail:
    point_id = f"{asset_id}/{point_name}"
    point = service.get_point(session, point_id)
    if point is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown point: {point_id}")
    detail = PointDetail.model_validate(point)
    binding = service.get_binding(session, point_id)
    if binding is not None:
        detail.binding = BindingOut.model_validate(binding)
    return detail


# ---------------------------------------------------------------------------
# Registry-level views
# ---------------------------------------------------------------------------


@router.get("/registry/summary", response_model=RegistrySummaryOut, summary="Registry roll-up")
def registry_summary(session: DbSession) -> RegistrySummaryOut:
    return RegistrySummaryOut(**service.registry_summary(session))


@router.get(
    "/registry/dictionary/asset-classes",
    response_model=list[AssetClassOut],
    summary="Approved asset classes",
)
def dictionary_asset_classes(
    session: DbSession,
    domain: Annotated[str | None, Query(description="Only classes allowed in this domain")] = None,
) -> list[AssetClassOut]:
    classes = service.list_asset_classes(session)
    if domain:
        classes = [row for row in classes if domain in (row.allowed_domains or ())]
    return [AssetClassOut.model_validate(row) for row in classes]


@router.get(
    "/registry/dictionary/points",
    response_model=list[PointDefinitionOut],
    summary="Canonical point definitions",
)
def dictionary_points(
    session: DbSession,
    control_capable: Annotated[bool | None, Query()] = None,
    q: Annotated[str | None, Query(description="Substring of the point name")] = None,
) -> list[PointDefinitionOut]:
    rows = service.list_point_definitions(session)
    if control_capable is not None:
        rows = [row for row in rows if bool(row.control_capable) is control_capable]
    if q:
        needle = q.strip().lower()
        rows = [row for row in rows if needle in row.name.lower()]
    return [PointDefinitionOut.model_validate(row) for row in rows]


@router.get(
    "/registry/design-conflicts",
    response_model=DesignConflictsOut,
    summary="Unresolved design decisions recorded by the register",
)
def design_conflicts(settings: AppSettings) -> DesignConflictsOut:
    package = loader.read_package(settings.data_dir, settings=settings)
    basis = package.design_basis
    return DesignConflictsOut(
        site_id=package.site_id,
        package_version=package.schema_version,
        design_stage=basis.get("design_stage"),
        authoritative_design_document=basis.get("authoritative_design_document"),
        open_design_conflicts=package.open_design_conflicts,
    )


@router.post(
    "/registry/reload",
    response_model=LoadResultOut,
    summary="Reload the machine-readable design package",
)
def reload_registry(
    session: DbSession,
    settings: AppSettings,
    principal: MaintainerPrincipal,
) -> LoadResultOut:
    """Converge the database onto the package on disk.

    The loader is idempotent, so this is safe to run at any time; it is the
    supported way to pick up an edited register without restarting the service.
    """
    try:
        result = loader.load_package(
            session,
            settings.data_dir,
            settings=settings,
            changed_by=principal.name,
            reason="Registry reload requested through the API",
        )
    except RegistryLoadError as exc:
        session.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"message": str(exc), "warnings": exc.warnings},
        ) from exc
    session.commit()
    return LoadResultOut(**vars(result))
