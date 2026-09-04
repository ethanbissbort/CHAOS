"""``/api/v1/plugins`` -- which integrations exist and whether they work.

This is the screen somebody opens when a vendor system has stopped delivering
data, so it is built around one question: *for each integration, is it working,
and if not, what specifically is missing?* Four states answer that, and they are
deliberately not collapsed into a boolean:

``ok``               the integration is configured and its last exchange worked
``degraded``         configured, and the last exchange failed -- something broke
``not_configured``   never set up; not broken, just not asked to run
``failed``           the plugin itself is broken (import, manifest, setup)
``disabled``         excluded by configuration or by node role

"Not configured" and "degraded" produce the same empty graph and need opposite
responses, which is exactly why a green/red light here would be a worse screen
than the one it replaced.

Nothing in this router returns an option *value*. Options carry appliance
credentials; the endpoint reports which keys are set, which is what diagnosing
needs and is safe to put on a wall display.

Read access is a viewer's. Reloading the plugin set is an administrator's,
because it re-runs third-party ``setup()`` code inside the running platform.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from chaos.api.deps import AdminPrincipal, AppSettings, Plugins
from chaos.plugins.manager import EXT_PREFIX, PluginManager
from chaos.plugins.spec import HEALTH_NOT_WORKING

router = APIRouter(prefix="/plugins", tags=["plugins"])


class ReloadRequest(BaseModel):
    """Re-running third-party setup code is an audited action like any other."""

    reason: str = Field(min_length=3, max_length=500)


def _mirror_stats(request: Request) -> dict[str, Any] | None:
    """The mirror engine's counters, if it is running on this node."""
    manager = getattr(request.app.state, "services", None)
    if manager is None:
        return None
    for service in manager.services:
        if getattr(service, "name", None) == "plugin-mirror":
            try:
                return service.stats()
            except Exception:  # pragma: no cover - defensive
                return None
    return None


@router.get("", summary="Every plugin discovered on this node")
def list_plugins(plugins: Plugins, settings: AppSettings, request: Request) -> dict[str, Any]:
    summary = plugins.summary()
    not_working = [
        entry["name"] for entry in summary["plugins"] if entry["health"]["state"] in HEALTH_NOT_WORKING
    ]
    return {
        **summary,
        "node_role": settings.node_role,
        "mount_prefix": f"/api/v1{EXT_PREFIX}/<plugin>",
        "enabled_setting": settings.enabled_plugin_list,
        "disabled_setting": settings.disabled_plugin_list,
        "mirror_enabled": settings.plugin_mirror_enabled,
        "not_working": not_working,
        "mirror": _mirror_stats(request),
    }


@router.get("/mirror", summary="What the mirror engine is carrying")
def mirror(request: Request, plugins: Plugins, settings: AppSettings) -> dict[str, Any]:
    """Live counters when the engine is running; the configured sources otherwise.

    Both shapes are useful. Before the platform is started -- in the CLI, in a
    test, on a node where the bus is off -- the second one still answers "which
    sources would run, and are any of them able to talk to anything".
    """
    stats = _mirror_stats(request)
    if stats is not None:
        # ``stats`` carries the engine's own ``running`` flag, which is the
        # authoritative one: the service can be built and registered on a node
        # whose background services were never started.
        return {"enabled": settings.plugin_mirror_enabled, **stats}
    sources = [
        source.as_dict() if hasattr(source, "as_dict") else {"name": source.name, "plugin": source.plugin}
        for source in plugins.mirror_sources()
    ]
    return {
        "running": False,
        "enabled": settings.plugin_mirror_enabled,
        "detail": (
            "The mirror engine is not running on this node. "
            "It starts with the platform's background services when a plugin contributes a source "
            "and CHAOS_PLUGIN_MIRROR_ENABLED is true."
        ),
        "sources": sources,
    }


@router.get("/{name}", summary="One plugin in detail")
def get_plugin(name: str, plugins: Plugins) -> dict[str, Any]:
    record = plugins.get(name)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown plugin: {name}")
    payload = record.as_dict()
    payload["mount_prefix"] = f"/api/v1{EXT_PREFIX}/{record.name}" if record.loaded else None
    return payload


@router.post("/reload", summary="Re-discover and re-initialise every plugin")
def reload_plugins(
    body: ReloadRequest,
    request: Request,
    principal: AdminPrincipal,
    settings: AppSettings,
) -> dict[str, Any]:
    """Re-run discovery against the current environment.

    **Routes are not re-mounted.** FastAPI builds its routing table at startup,
    so a plugin that gains or loses an API surface needs a restart to gain or
    lose its routes. Reload refreshes what a plugin *is* -- its options, its
    transport, its health, its mirror sources -- which is what changes when
    somebody fixes a credential, and the response says plainly that the route
    table did not move. A reload that silently left stale routes behind while
    claiming success would be the more dangerous of the two behaviours.
    """
    manager = PluginManager.discover(
        settings,
        session_factory=getattr(request.app.state, "session_factory", None),
        bus=getattr(request.app.state, "bus", None),
    )
    request.app.state.plugins = manager
    summary = manager.summary()
    return {
        "reloaded_by": principal.name,
        "reason": body.reason,
        "routes_remounted": False,
        "detail": (
            "Plugin configuration, transports, health and mirror sources were re-read. "
            "API routes are fixed at startup: restart the platform for a plugin to gain or lose routes."
        ),
        "discovered": summary["discovered"],
        "loaded": summary["loaded"],
        "states": summary["states"],
        "plugins": summary["plugins"],
    }
