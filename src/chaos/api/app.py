"""FastAPI application factory.

Routers are discovered from ``chaos.api.routers`` so that subsystems
can be added without editing a central import list.

Plugins add a second, deliberately separate surface. Their routers are mounted
under ``/api/v1/ext/<plugin>`` and are included *after* every core router, so a
third-party integration can neither shadow a core route nor change the order in
which core routes are matched. On a platform where a URL can start a pump, which
half of the API you are talking to should be visible in the URL.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from chaos import __version__
from chaos.config import Settings, get_settings
from chaos.db import build_engine, create_all, get_session_factory
from chaos.mqtt import MessageBus, build_bus
from chaos.plugins.manager import PluginManager
from chaos.runtime import ServiceManager, build_services

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


#: Routers are matched in registration order, and a ``{x:path}`` parameter is
#: greedy -- ``/points/{point_id:path}`` would otherwise swallow
#: ``/points/{point_id:path}/current``. Modules may declare ``ROUTER_PRIORITY``
#: (lower = registered earlier) so the more specific route wins.
DEFAULT_ROUTER_PRIORITY = 100


def discover_routers() -> list[APIRouter]:
    """Import every module under ``api.routers`` and collect its ``router``."""
    import chaos.api.routers as routers_pkg

    found: list[tuple[int, str, APIRouter]] = []
    for module_info in pkgutil.iter_modules(routers_pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        module_name = f"{routers_pkg.__name__}.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception:
            logger.exception("Failed to import router module %s", module_name)
            continue
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            priority = getattr(module, "ROUTER_PRIORITY", DEFAULT_ROUTER_PRIORITY)
            found.append((priority, module_info.name, router))
        else:
            logger.warning("Router module %s exposes no APIRouter named 'router'", module_name)

    found.sort(key=lambda item: (item[0], item[1]))
    return [router for _, _, router in found]


def create_app(
    settings: Settings | None = None,
    *,
    bus: MessageBus | None = None,
    start_services: bool | None = None,
    init_db: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    should_start = settings.mqtt_enabled if start_services is None else start_services

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if init_db:
            create_all(app.state.engine)
        manager: ServiceManager = build_services(
            settings,
            app.state.session_factory,
            app.state.bus,
            plugins=getattr(app.state, "plugins", None),
        )
        app.state.services = manager
        if should_start:
            app.state.bus.start()
            manager.start_all()
        try:
            yield
        finally:
            if should_start:
                manager.stop_all()
                app.state.bus.stop()

    app = FastAPI(
        title=settings.api_title,
        version=__version__,
        root_path=settings.api_root_path,
        lifespan=lifespan,
        description=(
            "Local-first operational control plane for the homestead. "
            "Write endpoints require a named operator and an audit reason."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    engine = build_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    from chaos import db as _db

    _db.configure(engine)
    app.state.session_factory = get_session_factory()
    app.state.bus = bus if bus is not None else build_bus(settings)
    app.state.plugins = PluginManager.discover(
        settings,
        session_factory=app.state.session_factory,
        bus=app.state.bus,
    )

    for router in discover_routers():
        app.include_router(router, prefix="/api/v1")

    for record, plugin_router, prefix in app.state.plugins.routers():
        try:
            app.include_router(plugin_router, prefix=f"/api/v1{prefix}")
        except Exception:
            # A malformed router -- a duplicate operation ID, an unresolvable
            # response model -- must cost that plugin its API and nothing else.
            logger.exception("Plugin %s contributed a router that could not be mounted", record.name)

    @app.get("/health", tags=["platform"])
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "node_role": settings.node_role,
            "site_id": settings.site_id,
            "physical_control_enabled": settings.allow_physical_control,
        }

    if WEB_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=WEB_DIR, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def index():
            index_file = WEB_DIR / "index.html"
            if index_file.is_file():
                return FileResponse(index_file)
            # A missing operator UI must not present as a server fault -- the
            # API is the control path and stays usable on its own.
            return JSONResponse(
                {
                    "status": "ok",
                    "detail": "Operator UI is not installed; the API is available at /api/v1.",
                    "docs": "/docs",
                    "health": "/health",
                },
                status_code=200,
            )

    return app
