"""Materialise point instances, historian pointers and vendor bindings.

The SDD keeps three things apart (section 43):

1. *Point identity* -- ``<asset_id>/<point_name>``, defined once in the point
   dictionary and never changed by an equipment swap.
2. *Point instance* -- one dictionary definition applied to one asset. That is
   the :class:`~homestead_twin.models.registry.Point` row built here.
3. *Vendor binding* -- how the device that currently occupies the functional
   position exposes the point. That is the ``PointBinding`` row.

The point set of an asset is the union of three sources:

* the asset class's ``default_points``
* every point named by a referenced ``point_profile``
* every ``point_name`` bound to the asset in ``point_bindings.yaml``

Nothing here invents an address, a register or a coordinate. ``TBD`` in the
source stays ``TBD``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin import topics
from homestead_twin.config import Settings, get_settings
from homestead_twin.models import (
    Asset,
    AssetClass,
    Point,
    PointBinding,
    PointDefinition,
    PointProfile,
    PointSampleIndex,
)

logger = logging.getLogger(__name__)

#: Where a materialised point came from. ``binding`` wins over ``class``,
#: which wins over ``profile`` -- a bound point is the most specific statement
#: about the asset, a profile point the most generic.
SOURCE_CLASS = "class"
SOURCE_PROFILE = "profile"
SOURCE_BINDING = "binding"
_SOURCE_RANK = {SOURCE_PROFILE: 0, SOURCE_CLASS: 1, SOURCE_BINDING: 2}

#: Historian retention used when the binding does not state a policy.
DEFAULT_RETENTION_POLICY = "standard"

#: Binding attributes carried verbatim from ``point_bindings.yaml``.
BINDING_FIELDS: tuple[str, ...] = (
    "binding_status",
    "source_protocol",
    "source_address",
    "scale",
    "offset",
    "sample_interval_s",
    "publish_interval_s",
    "stale_after_s",
    "historian_policy",
    "quality_policy",
    "automatic_control_allowed",
    "commissioned_at",
    "commissioned_by",
    "notes",
)

WarnFn = Callable[[str], None]
#: Primary-key upsert. Defaults to ``Session.merge``; the package loader passes
#: a batching variant that avoids one SELECT per row.
UpsertFn = Callable[[Any], Any]


def _noop(message: str) -> None:  # pragma: no cover - trivial
    logger.warning("%s", message)


# ---------------------------------------------------------------------------
# Point-set resolution
# ---------------------------------------------------------------------------


def collect_point_names(
    *,
    default_points: Iterable[str] | None = None,
    profiles: Mapping[str, Sequence[str]] | None = None,
    profile_refs: Iterable[str] | None = None,
    binding_names: Iterable[str] | None = None,
    warn: WarnFn | None = None,
) -> dict[str, str]:
    """Return ``{point_name: source}`` for one asset.

    Pure function -- no database access -- so the union rule can be unit
    tested and reused by the simulator and the commissioning tooling.
    """
    warn = warn or _noop
    resolved: dict[str, str] = {}

    def offer(name: str, source: str) -> None:
        current = resolved.get(name)
        if current is None or _SOURCE_RANK[source] > _SOURCE_RANK[current]:
            resolved[name] = source

    for name in profile_refs or ():
        profile_points = (profiles or {}).get(name)
        if profile_points is None:
            warn(f"Unknown point profile: {name}")
            continue
        for point_name in profile_points:
            offer(point_name, SOURCE_PROFILE)

    for point_name in default_points or ():
        offer(point_name, SOURCE_CLASS)

    for point_name in binding_names or ():
        offer(point_name, SOURCE_BINDING)

    return resolved


# ---------------------------------------------------------------------------
# Materialisation
# ---------------------------------------------------------------------------


def materialize_points(
    session: Session,
    asset: Asset,
    *,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
    definitions: Mapping[str, PointDefinition] | None = None,
    class_def: AssetClass | None = None,
    profiles: Mapping[str, Sequence[str]] | None = None,
    settings: Settings | None = None,
    warn: WarnFn | None = None,
    upsert: UpsertFn | None = None,
) -> list[Point]:
    """Create or refresh every :class:`Point` belonging to ``asset``.

    Also writes the ``point_samples_index`` pointer for each point and, when a
    binding record is available, the ``point_bindings`` row.

    ``bindings`` maps ``point_name`` to a binding record straight out of
    ``point_bindings.yaml``. When omitted, bindings already stored for the
    asset are re-applied, so calling this twice is a no-op.

    The optional ``definitions``/``class_def``/``profiles`` arguments let the
    package loader pass its pre-read dictionaries instead of re-querying once
    per asset; every one of them falls back to a database lookup.
    """
    settings = settings or get_settings()
    warn = warn or _noop
    upsert = upsert or session.merge

    if class_def is None:
        class_def = session.get(AssetClass, asset.asset_class)
    default_points = list(class_def.default_points or ()) if class_def is not None else []
    if class_def is None:
        warn(f"Asset {asset.asset_id} references unknown asset class {asset.asset_class!r}")

    profile_refs = list(asset.point_profile_refs or ())
    if profiles is None:
        profiles = _profiles_from_db(session, profile_refs)

    binding_records = dict(bindings) if bindings is not None else _bindings_from_db(session, asset.asset_id)

    wanted = collect_point_names(
        default_points=default_points,
        profiles=profiles,
        profile_refs=profile_refs,
        binding_names=binding_records.keys(),
        warn=warn,
    )

    created: list[Point] = []
    for point_name in sorted(wanted):
        source = wanted[point_name]
        definition = _definition(session, definitions, point_name)
        if definition is None:
            warn(
                f"Asset {asset.asset_id} references point {point_name!r} which is not in the point dictionary"
            )
            continue
        record = binding_records.get(point_name)
        point = _upsert_point(upsert, asset, point_name, definition, source, record)
        _upsert_sample_index(upsert, point, record, settings)
        if record is not None:
            _upsert_binding(upsert, point, record, warn)
        created.append(point)
    return created


def _definition(
    session: Session,
    definitions: Mapping[str, PointDefinition] | None,
    point_name: str,
) -> PointDefinition | None:
    if definitions is not None:
        return definitions.get(point_name)
    return session.get(PointDefinition, point_name)


def _profiles_from_db(session: Session, refs: Sequence[str]) -> dict[str, list[str]]:
    if not refs:
        return {}
    rows = session.scalars(select(PointProfile).where(PointProfile.name.in_(list(refs)))).all()
    return {row.name: list(row.point_names or ()) for row in rows}


def _bindings_from_db(session: Session, asset_id: str) -> dict[str, dict[str, Any]]:
    rows = session.scalars(select(PointBinding).where(PointBinding.asset_id == asset_id)).all()
    return {row.point_name: {name: getattr(row, name) for name in BINDING_FIELDS} for row in rows}


def _upsert_point(
    upsert: UpsertFn,
    asset: Asset,
    point_name: str,
    definition: PointDefinition,
    source: str,
    binding: Mapping[str, Any] | None,
) -> Point:
    point_id = topics.point_id(asset.asset_id, point_name)

    # SDD section 47: control capability is a property of the *point*; whether
    # automatic control may actually use it is a property of the *binding*, and
    # commissioning has to say so explicitly. Default deny.
    automatic_control_allowed = bool(binding.get("automatic_control_allowed")) if binding else False

    return upsert(
        Point(
            point_id=point_id,
            asset_id=asset.asset_id,
            point_name=point_name,
            point_class=definition.default_class,
            data_type=definition.data_type,
            unit=definition.unit,
            enum_values=list(definition.enum_values) if definition.enum_values else None,
            control_capable=bool(definition.control_capable),
            automatic_control_allowed=automatic_control_allowed,
            source=source,
            historian_policy=(binding or {}).get("historian_policy"),
            stale_after_s=(binding or {}).get("stale_after_s"),
            description=definition.description,
        )
    )


def _upsert_sample_index(
    upsert: UpsertFn,
    point: Point,
    binding: Mapping[str, Any] | None,
    settings: Settings,
) -> PointSampleIndex:
    """Registry -> historian pointer (SDD section 40.1)."""
    retention = (binding or {}).get("historian_policy") or DEFAULT_RETENTION_POLICY
    return upsert(
        PointSampleIndex(
            point_id=point.point_id,
            historian_backend=settings.historian_backend,
            series_key=point.point_id,
            measurement=point.point_name,
            retention_policy=retention,
        )
    )


def _upsert_binding(
    upsert: UpsertFn,
    point: Point,
    record: Mapping[str, Any],
    warn: WarnFn,
) -> PointBinding:
    """Store the vendor binding, adding the derived MQTT projection.

    ``source_address`` is copied verbatim: an unverified address stays ``TBD``
    (SDD section 47). Only the topics are derived, because they follow
    deterministically from the canonical identity (SDD section 26.2).

    An identity that cannot be projected onto a topic -- the JSON Schema allows
    asset IDs deeper than the four components section 25.2 defines -- is
    reported and left without a topic rather than aborting the whole load.
    """
    binding = PointBinding(
        point_id=point.point_id,
        asset_id=point.asset_id,
        point_name=point.point_name,
        mqtt_topic=None,
        command_topic=None,
    )
    try:
        binding.mqtt_topic = topics.telemetry_topic(point.asset_id, point.point_name)
        if point.control_capable:
            binding.command_topic = topics.command_topic(point.asset_id, point.point_name)
    except topics.TopicError as exc:
        warn(f"Binding {point.point_id}: cannot derive an MQTT topic ({exc})")
    for field_name in BINDING_FIELDS:
        if field_name in record:
            value = record[field_name]
            if field_name == "notes":
                value = list(value or ())
            elif field_name == "automatic_control_allowed":
                value = bool(value)
            setattr(binding, field_name, value)
    return upsert(binding)
