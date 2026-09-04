"""The CHAOS plugin system.

Everything a third-party integration needs, and the boundary it may not cross.

* :mod:`chaos.plugins.spec` -- the contract: manifest, context, health, and the
  :class:`~chaos.plugins.spec.PluginBase` most plugins subclass.
* :mod:`chaos.plugins.manager` -- discovery (built-in packages and installed
  entry points), enablement, and failure isolation.
* :mod:`chaos.plugins.mirror` -- turning vendor readings into canonical
  telemetry through the registry.

A built-in plugin is a **package** in this directory exposing ``PLUGIN``; the
modules listed above are framework and are never mistaken for plugins because
discovery only considers packages. See ``docs/plugins.md``.
"""

from __future__ import annotations

from chaos.plugins.manager import (
    ENTRY_POINT_GROUP,
    EXT_PREFIX,
    LoadedPlugin,
    PluginManager,
    options_for,
)
from chaos.plugins.mirror import (
    IdentityMap,
    MirrorReading,
    MirrorService,
    MirrorSource,
)
from chaos.plugins.spec import (
    CAPABILITIES,
    HEALTH_DEGRADED,
    HEALTH_DISABLED,
    HEALTH_FAILED,
    HEALTH_NOT_CONFIGURED,
    HEALTH_OK,
    HEALTH_STATES,
    Plugin,
    PluginBase,
    PluginContext,
    PluginHealth,
    PluginManifest,
)

__all__ = [
    "CAPABILITIES",
    "ENTRY_POINT_GROUP",
    "EXT_PREFIX",
    "HEALTH_DEGRADED",
    "HEALTH_DISABLED",
    "HEALTH_FAILED",
    "HEALTH_NOT_CONFIGURED",
    "HEALTH_OK",
    "HEALTH_STATES",
    "IdentityMap",
    "LoadedPlugin",
    "MirrorReading",
    "MirrorService",
    "MirrorSource",
    "Plugin",
    "PluginBase",
    "PluginContext",
    "PluginHealth",
    "PluginManager",
    "PluginManifest",
    "options_for",
]
