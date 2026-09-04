"""Discovery, isolation and lifecycle for plugins.

Two origins, one contract:

**Built-in** -- a *package* under ``chaos.plugins`` exposing ``PLUGIN`` (an
instance) or ``get_plugin()`` (a factory). Only packages are considered, which
is why the framework modules beside them (``spec``, ``mirror``, this one) need
no exclusion list and cannot be mistaken for plugins.

**Installed** -- any distribution advertising the ``chaos.plugins`` entry-point
group. This is the path for an integration that should not live in this
repository: a site-specific vendor bridge, or one with a dependency the
platform must not take on.

    .. code-block:: toml

        [project.entry-points."chaos.plugins"]
        netbotz = "my_package.netbotz:PLUGIN"

Ordering is deterministic: built-ins before entry points, each set sorted by
name. Two plugins claiming the same name is a configuration error, and the
second one loses -- loudly, with both origins in the log, rather than silently
replacing whichever happened to import first.

**Nothing here is allowed to raise.** A plugin whose module does not import,
whose manifest is invalid, whose ``setup()`` throws, or whose ``routers()``
returns rubbish, becomes a :class:`LoadedPlugin` in state
:data:`~chaos.plugins.spec.HEALTH_FAILED` carrying the reason. The platform
starts. This is the same rule :class:`~chaos.runtime.ServiceManager` applies to
subsystems, for the same reason: a frozen greenhouse is worse than a missing
integration.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
import pkgutil
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from chaos.config import Settings
from chaos.plugins.spec import (
    Plugin,
    PluginContext,
    PluginHealth,
    PluginManifest,
)

if TYPE_CHECKING:  # pragma: no cover - imports for typing only
    from fastapi import APIRouter
    from sqlalchemy.orm import Session, sessionmaker

    from chaos.mqtt import MessageBus
    from chaos.plugins.mirror import MirrorSource
    from chaos.runtime import BackgroundService

logger = logging.getLogger(__name__)

#: The entry-point group an installed plugin advertises itself under.
ENTRY_POINT_GROUP = "chaos.plugins"

#: Attributes a plugin module may expose, in the order they are tried.
PLUGIN_ATTRIBUTES = ("PLUGIN", "get_plugin")

ORIGIN_BUILTIN = "builtin"
ORIGIN_ENTRY_POINT = "entry_point"

#: Every plugin router is mounted under this prefix plus the plugin name.
#:
#: The prefix is not negotiable and plugins do not choose it. A plugin that
#: could mount at ``/api/v1/alarms`` could shadow the alarm API on a control
#: platform, and a reader of a URL could not tell core from vendor. ``/ext/``
#: says which is which, in the one place an operator actually looks.
EXT_PREFIX = "/ext"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

#: Per-plugin options come from the environment as
#: ``CHAOS_PLUGIN_<NAME>_<KEY>``, so a container gets configured the same way
#: everything else here does.
OPTION_ENV_PREFIX = "CHAOS_PLUGIN_"


def options_for(name: str, settings: Settings, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Resolve one plugin's options.

    Lowest to highest precedence: the ``plugin_options`` JSON document, then the
    environment. Option values routinely contain credentials, so they are held
    as plain strings, never logged, and never returned by the API -- only the
    key names are.
    """
    environ = os.environ if environ is None else environ
    resolved: dict[str, str] = {}

    document = _plugin_options_document(settings)
    section = document.get(name)
    if isinstance(section, Mapping):
        for key, value in section.items():
            if value is not None:
                resolved[str(key).strip().lower()] = str(value)
    elif section is not None:
        logger.warning("plugin_options['%s'] is not an object; ignoring it", name)

    env_prefix = f"{OPTION_ENV_PREFIX}{name.upper()}_"
    for env_key, value in environ.items():
        if env_key.startswith(env_prefix) and len(env_key) > len(env_prefix):
            resolved[env_key[len(env_prefix) :].lower()] = value

    return resolved


def _plugin_options_document(settings: Settings) -> dict[str, Any]:
    raw = (getattr(settings, "plugin_options", "") or "").strip()
    if not raw:
        return {}
    try:
        document = json.loads(raw)
    except ValueError as exc:
        logger.error("CHAOS_PLUGIN_OPTIONS is not valid JSON (%s); no options were read from it", exc)
        return {}
    if not isinstance(document, dict):
        logger.error("CHAOS_PLUGIN_OPTIONS must be a JSON object keyed by plugin name; ignoring it")
        return {}
    return document


def _name_list(raw: str | None) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in (raw or "").split(",") if part.strip())


# ---------------------------------------------------------------------------
# Loaded records
# ---------------------------------------------------------------------------


@dataclass
class LoadedPlugin:
    """One plugin as the platform found it -- working or not.

    A failed plugin is still a record. "It is not in the list" and "it is in the
    list, red, with the import error" are very different things to be looking at
    when an integration has stopped delivering data.
    """

    name: str
    origin: str
    manifest: PluginManifest | None = None
    plugin: Plugin | None = None
    module: str | None = None
    enabled: bool = True
    error: str | None = None
    #: Only key names, never values -- options carry credentials.
    option_keys: tuple[str, ...] = ()
    _health: PluginHealth | None = field(default=None, repr=False)

    @property
    def loaded(self) -> bool:
        return self.plugin is not None and self.error is None and self.enabled

    def health(self) -> PluginHealth:
        if self._health is not None:
            return self._health
        if self.error is not None:
            return PluginHealth.failed(self.error)
        if not self.enabled:
            return PluginHealth.disabled("Not enabled on this node")
        if self.plugin is None:  # pragma: no cover - defensive
            return PluginHealth.failed("The plugin object is missing")
        try:
            health = self.plugin.health()
        except Exception as exc:
            logger.exception("Plugin %s failed its health check", self.name)
            return PluginHealth.failed(f"health() raised {type(exc).__name__}: {_short(exc)}")
        if not isinstance(health, PluginHealth):
            return PluginHealth.failed(f"health() returned {type(health).__name__}, not a PluginHealth")
        return health

    def as_dict(self) -> dict[str, Any]:
        health = self.health()
        payload: dict[str, Any] = {
            "name": self.name,
            "origin": self.origin,
            "module": self.module,
            "enabled": self.enabled,
            "loaded": self.loaded,
            "configured_option_keys": list(self.option_keys),
            "health": health.as_dict(),
        }
        payload["manifest"] = self.manifest.as_dict() if self.manifest else None
        return payload


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A plugin found but not yet loaded."""

    name: str
    origin: str
    module: str
    factory: Any  # module attribute or EntryPoint


def iter_builtin_candidates() -> Iterator[Candidate]:
    """Packages under ``chaos.plugins``. Modules are framework, not plugins."""
    import chaos.plugins as package

    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        if not info.ispkg or info.name.startswith("_"):
            continue
        yield Candidate(
            name=info.name,
            origin=ORIGIN_BUILTIN,
            module=f"{package.__name__}.{info.name}",
            factory=None,
        )


def iter_entry_point_candidates() -> Iterator[Candidate]:
    """Installed distributions advertising ``chaos.plugins``."""
    from importlib.metadata import entry_points

    try:
        found = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover - a broken distribution's metadata
        logger.exception("Could not read %s entry points", ENTRY_POINT_GROUP)
        return
    for entry in sorted(found, key=lambda item: item.name):
        yield Candidate(
            name=entry.name.strip().lower(),
            origin=ORIGIN_ENTRY_POINT,
            module=entry.value,
            factory=entry,
        )


def _instantiate(candidate: Candidate) -> Plugin:
    """Turn a candidate into a plugin object, or raise with a usable message."""
    if candidate.origin == ORIGIN_ENTRY_POINT:
        target = candidate.factory.load()
    else:
        module = importlib.import_module(candidate.module)
        target = None
        for attribute in PLUGIN_ATTRIBUTES:
            target = getattr(module, attribute, None)
            if target is not None:
                break
        if target is None:
            raise AttributeError(
                f"{candidate.module} is a package under chaos.plugins but exposes neither "
                f"{' nor '.join(PLUGIN_ATTRIBUTES)}"
            )

    # Three shapes are accepted, and the order matters. A class carries its
    # manifest as a class attribute, so testing for the attribute first would
    # hand back the class itself -- which passes the manifest check below and
    # then fails obscurely at the first unbound ``setup(context)`` call.
    if inspect.isclass(target):
        plugin = target()
    elif callable(target) and not hasattr(target, "manifest"):
        plugin = target()  # a get_plugin() factory
    else:
        plugin = target  # already an instance

    manifest = getattr(plugin, "manifest", None)
    if not isinstance(manifest, PluginManifest):
        raise TypeError(
            f"{candidate.module} produced {type(plugin).__name__}, which has no PluginManifest "
            "attribute named 'manifest'"
        )
    return plugin


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class PluginManager:
    """Owns the loaded plugin set and hands their contributions to the platform."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.records: list[LoadedPlugin] = []
        self._by_name: dict[str, LoadedPlugin] = {}

    # -- construction ----------------------------------------------------

    @classmethod
    def discover(
        cls,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session] | None = None,
        bus: MessageBus | None = None,
        candidates: Sequence[Candidate] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> PluginManager:
        manager = cls(settings)
        found = (
            list(candidates)
            if candidates is not None
            else [
                *iter_builtin_candidates(),
                *iter_entry_point_candidates(),
            ]
        )
        for candidate in found:
            manager._load(candidate, session_factory=session_factory, bus=bus, environ=environ)
        enabled = sum(1 for record in manager.records if record.loaded)
        logger.info("Plugins: %d discovered, %d loaded", len(manager.records), enabled)
        return manager

    def _load(
        self,
        candidate: Candidate,
        *,
        session_factory: sessionmaker[Session] | None,
        bus: MessageBus | None,
        environ: Mapping[str, str] | None,
    ) -> LoadedPlugin:
        record = LoadedPlugin(name=candidate.name, origin=candidate.origin, module=candidate.module)

        existing = self._by_name.get(candidate.name)
        if existing is not None:
            record.enabled = False
            record.error = (
                f"Duplicate plugin name: already provided by {existing.module} "
                f"({existing.origin}). Rename one of them; nothing from this copy is loaded."
            )
            logger.error("%s", record.error)
            self.records.append(record)
            return record

        try:
            plugin = _instantiate(candidate)
        except Exception as exc:
            record.error = f"Could not load {candidate.module}: {type(exc).__name__}: {_short(exc)}"
            logger.exception("Plugin %s failed to load", candidate.name)
            return self._register(record)

        manifest = plugin.manifest
        record.manifest = manifest
        record.plugin = plugin

        if manifest.name != candidate.name:
            # The discovered name is what configuration, URLs and environment
            # variables use. A manifest that disagrees would make
            # CHAOS_PLUGIN_<NAME>_* silently target nothing.
            record.error = (
                f"Manifest name {manifest.name!r} does not match the name it was discovered under "
                f"({candidate.name!r}). Make them equal."
            )
            logger.error("%s", record.error)
            return self._register(record)

        if not self._is_enabled(manifest):
            record.enabled = False
            record._health = PluginHealth.disabled(self._disabled_reason(manifest))
            return self._register(record)

        options = options_for(manifest.name, self.settings, environ)
        record.option_keys = tuple(sorted(options))
        context = PluginContext(
            settings=self.settings,
            options=options,
            session_factory=session_factory,
            bus=bus,
        )
        try:
            plugin.setup(context)
        except Exception as exc:
            record.error = f"setup() raised {type(exc).__name__}: {_short(exc)}"
            logger.exception("Plugin %s failed during setup", manifest.name)
        return self._register(record)

    def _register(self, record: LoadedPlugin) -> LoadedPlugin:
        self.records.append(record)
        self._by_name[record.name] = record
        return record

    # -- enablement ------------------------------------------------------

    def _is_enabled(self, manifest: PluginManifest) -> bool:
        role = self.settings.node_role.lower()
        if role not in manifest.node_roles:
            return False
        disabled = _name_list(getattr(self.settings, "plugins_disabled", ""))
        if manifest.name in disabled:
            return False
        enabled = _name_list(getattr(self.settings, "plugins_enabled", "*"))
        if not enabled:
            return False
        return "*" in enabled or manifest.name in enabled

    def _disabled_reason(self, manifest: PluginManifest) -> str:
        role = self.settings.node_role.lower()
        if role not in manifest.node_roles:
            return f"Not loaded on a {role} node: this plugin declares node_roles={list(manifest.node_roles)}"
        if manifest.name in _name_list(getattr(self.settings, "plugins_disabled", "")):
            return "Listed in CHAOS_PLUGINS_DISABLED"
        return "Not listed in CHAOS_PLUGINS_ENABLED"

    # -- lookups ---------------------------------------------------------

    def get(self, name: str) -> LoadedPlugin | None:
        return self._by_name.get(name.strip().lower())

    @property
    def loaded(self) -> list[LoadedPlugin]:
        return [record for record in self.records if record.loaded]

    # -- contributions ---------------------------------------------------
    #
    # Each collector isolates per plugin: one integration returning nonsense
    # must not cost the others their routes, services or sources.

    def routers(self) -> list[tuple[LoadedPlugin, APIRouter, str]]:
        """``(record, router, prefix)`` for every router a plugin contributes."""
        from fastapi import APIRouter as _APIRouter

        collected: list[tuple[LoadedPlugin, _APIRouter, str]] = []
        for record in self.loaded:
            prefix = f"{EXT_PREFIX}/{record.name}"
            for router in self._collect(record, "routers"):
                if not isinstance(router, _APIRouter):
                    logger.error(
                        "Plugin %s returned %s from routers(), not an APIRouter; skipping it",
                        record.name,
                        type(router).__name__,
                    )
                    continue
                collected.append((record, router, prefix))
        return collected

    def services(self) -> list[BackgroundService]:
        collected: list[BackgroundService] = []
        for record in self.loaded:
            for service in self._collect(record, "services"):
                if not hasattr(service, "start") or not hasattr(service, "stop"):
                    logger.error(
                        "Plugin %s returned %s from services(), which is not a BackgroundService",
                        record.name,
                        type(service).__name__,
                    )
                    continue
                collected.append(service)
        return collected

    def mirror_sources(self) -> list[MirrorSource]:
        from chaos.plugins.mirror import MirrorSource as _MirrorSource

        collected: list[_MirrorSource] = []
        seen: set[str] = set()
        for record in self.loaded:
            for source in self._collect(record, "mirror_sources"):
                if not isinstance(source, _MirrorSource):
                    logger.error(
                        "Plugin %s returned %s from mirror_sources(), which is not a MirrorSource",
                        record.name,
                        type(source).__name__,
                    )
                    continue
                if source.plugin != record.name:
                    # A source claiming another plugin's name would be filed
                    # under that plugin on every screen and in every counter.
                    logger.error(
                        "Plugin %s contributed a mirror source claiming plugin=%r; skipping it",
                        record.name,
                        source.plugin,
                    )
                    continue
                qualified = f"{source.plugin}.{source.name}"
                if qualified in seen:
                    logger.error("Duplicate mirror source %s; keeping the first", qualified)
                    continue
                seen.add(qualified)
                collected.append(source)
        return collected

    def _collect(self, record: LoadedPlugin, method: str) -> Sequence[Any]:
        assert record.plugin is not None  # guarded by .loaded
        try:
            produced = getattr(record.plugin, method)()
        except Exception:
            logger.exception("Plugin %s failed in %s()", record.name, method)
            return ()
        if produced is None:
            return ()
        try:
            return list(produced)
        except TypeError:
            logger.error("Plugin %s returned a non-iterable from %s()", record.name, method)
            return ()

    # -- introspection ---------------------------------------------------

    def summary(self) -> dict[str, Any]:
        records = [record.as_dict() for record in self.records]
        states: dict[str, int] = {}
        for record in records:
            state = record["health"]["state"]
            states[state] = states.get(state, 0) + 1
        return {
            "discovered": len(records),
            "loaded": len(self.loaded),
            "states": states,
            "plugins": records,
        }


def _short(exc: Exception, limit: int = 200) -> str:
    return " ".join(str(exc).split())[:limit]
