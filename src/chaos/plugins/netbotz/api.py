"""``/api/v1/ext/netbotz`` -- what the integration is, and whether it is wired up.

Everything here is read-only. A NetBotz appliance is a monitoring device: the
platform reads it, and there is no command path to add. Nothing in this router
requires more than a viewer, and nothing in it returns a credential -- the
transport describes itself with host and kind, never with a password.

The endpoint worth knowing about is ``/bindings``. It answers the question that
actually comes up during commissioning -- *"why is the NetBotz not showing
anything?"* -- by reporting which registry rows exist, which are still ``TBD``,
and therefore whether the appliance is unreachable or simply not yet bound to
anything. Those two failures look identical on a dashboard and have completely
different fixes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from sqlalchemy import select

from chaos.api.deps import DbSession
from chaos.models.registry import Asset, ExternalIdentifier, Point, PointBinding
from chaos.plugins.mirror import PLACEHOLDER_ADDRESSES
from chaos.plugins.netbotz import sensors as sensor_map
from chaos.plugins.netbotz.mirror import ID_TYPE, PROTOCOL

if TYPE_CHECKING:  # pragma: no cover - imports for typing only
    from chaos.plugins.netbotz import NetBotzPlugin


def build_router(plugin: NetBotzPlugin) -> APIRouter:
    """Build the router for one plugin instance.

    A closure rather than a module-level router: the endpoints report *this*
    plugin's transport and counters, and a module-level singleton would report
    whichever instance happened to be constructed last.
    """
    router = APIRouter(tags=["netbotz"])

    @router.get("", summary="Integration status")
    def status() -> dict[str, Any]:
        health = plugin.health()
        return {
            "manifest": plugin.manifest.as_dict(),
            "health": health.as_dict(),
            "transport": plugin.transport.describe(),
            "sources": [source.as_dict() for source in plugin.mirror_sources()],
        }

    @router.get("/sensor-types", summary="How NetBotz sensors map to canonical points")
    def sensor_types() -> dict[str, Any]:
        catalogue = [sensor.as_dict() for sensor in sensor_map.SENSOR_TYPES]
        return {
            "protocol": PROTOCOL,
            "id_type": ID_TYPE,
            "sensor_types": catalogue,
            "aliases": dict(sorted(sensor_map.TYPE_ALIASES.items())),
            "mirrorable": sum(1 for entry in catalogue if entry["mirrorable"]),
            "unmirrorable": sum(1 for entry in catalogue if not entry["mirrorable"]),
        }

    @router.get("/enclosures", summary="Appliances the transport can see")
    def enclosures() -> dict[str, Any]:
        found = [enclosure.as_dict() for enclosure in plugin.transport.enclosures()]
        return {
            "transport": plugin.transport.describe(),
            "count": len(found),
            "enclosures": found,
        }

    @router.get("/readings", summary="Raw vendor readings, before any mapping")
    def readings() -> dict[str, Any]:
        """The appliance's own view, unmapped.

        Useful precisely because it is unmapped: comparing this against
        ``/bindings`` is how you tell "the appliance is not reporting that
        sensor" from "that sensor is not bound to a point".
        """
        raw = list(plugin.transport.read())
        return {
            "transport": plugin.transport.describe(),
            "count": len(raw),
            "readings": [reading.as_dict() for reading in raw],
        }

    @router.get("/bindings", summary="Registry rows that make NetBotz readings mean something")
    def bindings(session: DbSession) -> dict[str, Any]:
        identifier_rows = session.execute(
            select(ExternalIdentifier.value, ExternalIdentifier.asset_id, Asset.name)
            .join(Asset, Asset.asset_id == ExternalIdentifier.asset_id, isouter=True)
            .where(ExternalIdentifier.id_type == ID_TYPE)
        ).all()

        binding_rows = session.execute(
            select(PointBinding.point_id, PointBinding.source_address, PointBinding.binding_status).where(
                PointBinding.source_protocol == PROTOCOL
            )
        ).all()

        resolvable: list[dict[str, Any]] = []
        placeholders: list[dict[str, Any]] = []
        for point_id, address, binding_status in binding_rows:
            entry = {"point_id": point_id, "source_address": address, "binding_status": binding_status}
            if not address or address.strip().lower() in PLACEHOLDER_ADDRESSES:
                placeholders.append(entry)
            else:
                resolvable.append(entry)

        enclosures_out = []
        for value, asset_id, asset_name in identifier_rows:
            point_names = (
                session.execute(select(Point.point_name).where(Point.asset_id == asset_id)).scalars().all()
            )
            enclosures_out.append(
                {
                    "enclosure_id": value,
                    "asset_id": asset_id,
                    "asset_name": asset_name,
                    "point_count": len(point_names),
                }
            )

        return {
            "protocol": PROTOCOL,
            "id_type": ID_TYPE,
            "external_identifiers": enclosures_out,
            "point_bindings": resolvable,
            "uncommissioned_point_bindings": placeholders,
            "summary": {
                "enclosures_mapped": len(enclosures_out),
                "bindings_resolvable": len(resolvable),
                "bindings_uncommissioned": len(placeholders),
            },
            "detail": _binding_detail(len(enclosures_out), len(resolvable), len(placeholders)),
        }

    return router


def _binding_detail(enclosures: int, resolvable: int, placeholders: int) -> str:
    """Say what the numbers mean, so the answer is not left as an exercise."""
    if resolvable or enclosures:
        parts = []
        if enclosures:
            parts.append(f"{enclosures} appliance(s) mapped to assets by external identifier")
        if resolvable:
            parts.append(f"{resolvable} point binding(s) name a NetBotz sensor address")
        detail = "; ".join(parts) + "."
        if placeholders:
            detail += f" {placeholders} further binding(s) are still uncommissioned placeholders."
        return detail
    if placeholders:
        return (
            f"Nothing is bound yet: {placeholders} NetBotz point binding(s) exist but hold placeholder "
            "addresses. Readings from the appliance would be dropped as unresolved until commissioning "
            "fills in point_bindings.source_address."
        )
    return (
        "Nothing in the registry refers to NetBotz. Add an external_identifiers row of type "
        f"'{ID_TYPE}' mapping the appliance to its asset, or point_bindings rows with "
        f"source_protocol='{PROTOCOL}' and the appliance's own sensor address."
    )
