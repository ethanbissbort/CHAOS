"""The plugin contract.

A plugin is how a third-party system becomes part of CHAOS without any core
module learning that the system exists. It may contribute four things, and
nothing else:

``api``
    FastAPI routers, mounted under ``/api/v1/ext/<plugin>``. A plugin can never
    define, shadow or reorder a core route -- see
    :mod:`chaos.plugins.manager` for why the prefix is not negotiable.
``mirror``
    :class:`~chaos.plugins.mirror.MirrorSource` objects that read a vendor
    system and hand back readings. The mirror engine resolves those readings to
    canonical points through the registry and publishes ordinary telemetry
    envelopes. A plugin never writes to the database and never invents identity.
``service``
    :class:`~chaos.runtime.BackgroundService` objects started with the rest of
    the platform, subject to the same failure isolation.
``health``
    One honest answer to "is this integration actually working". A plugin that
    cannot reach its device reports :data:`HEALTH_DEGRADED`; one that has never
    been given credentials reports :data:`HEALTH_NOT_CONFIGURED`. Neither is
    ``ok``, and no plugin is asked to guess.

Two rules make the whole thing safe to run on a control platform:

1. **The registry still owns identity.** A plugin supplies vendor addresses.
   Which canonical point a vendor address means is a registry fact
   (``point_bindings.source_address`` or an ``external_identifiers`` row), and a
   reading that resolves to nothing is dropped and counted, never guessed at.
2. **A plugin cannot take the platform down.** Import, setup, router collection
   and every poll are individually isolated. A broken vendor integration
   degrades to a red row on the plugins screen; the greenhouse still gets heat.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from chaos.models.base import utcnow

if TYPE_CHECKING:  # pragma: no cover - imports for typing only
    from fastapi import APIRouter
    from sqlalchemy.orm import Session, sessionmaker

    from chaos.config import Settings
    from chaos.mqtt import MessageBus
    from chaos.plugins.mirror import MirrorSource
    from chaos.runtime import BackgroundService

logger = logging.getLogger(__name__)

#: What a plugin may contribute. Declared in the manifest so the plugins screen
#: can say what an integration is *for* without importing or starting it.
CAPABILITIES = ("api", "mirror", "service")

#: A plugin name is a slug because it becomes a URL segment
#: (``/api/v1/ext/<name>``) and an environment-variable fragment
#: (``CHAOS_PLUGIN_<NAME>_<KEY>``). Both constrain it more than Python does.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

#: The integration is configured and its last exchange with the vendor system
#: succeeded.
HEALTH_OK = "ok"
#: Configured, but the last exchange failed. Something that used to work does
#: not. This is the state that should page somebody.
HEALTH_DEGRADED = "degraded"
#: The plugin has not been given what it needs (host, credentials, a transport
#: that exists). It is not broken; it has never been asked to run.
HEALTH_NOT_CONFIGURED = "not_configured"
#: The plugin itself failed -- import error, bad manifest, an exception out of
#: ``setup()``. Nothing it contributes is loaded.
HEALTH_FAILED = "failed"
#: Excluded by configuration or by node role. Loaded nothing, on purpose.
HEALTH_DISABLED = "disabled"

HEALTH_STATES = (
    HEALTH_OK,
    HEALTH_DEGRADED,
    HEALTH_NOT_CONFIGURED,
    HEALTH_FAILED,
    HEALTH_DISABLED,
)

#: States in which a plugin is not delivering data. Kept as a set so the API and
#: the console agree on what counts as "working" without restating the list.
HEALTH_NOT_WORKING = frozenset({HEALTH_DEGRADED, HEALTH_NOT_CONFIGURED, HEALTH_FAILED, HEALTH_DISABLED})


@dataclass(frozen=True)
class PluginHealth:
    """One plugin's honest self-assessment.

    ``detail`` is written for whoever is standing in front of the rack, not for
    a log parser: it says what is missing or what failed, in a sentence.
    """

    state: str
    detail: str
    facts: dict[str, Any] = field(default_factory=dict)
    checked_at: dt.datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.state not in HEALTH_STATES:
            raise ValueError(f"Unknown plugin health state: {self.state!r} (expected one of {HEALTH_STATES})")

    @property
    def working(self) -> bool:
        return self.state == HEALTH_OK

    @classmethod
    def ok(cls, detail: str, **facts: Any) -> PluginHealth:
        return cls(HEALTH_OK, detail, facts)

    @classmethod
    def degraded(cls, detail: str, **facts: Any) -> PluginHealth:
        return cls(HEALTH_DEGRADED, detail, facts)

    @classmethod
    def not_configured(cls, detail: str, **facts: Any) -> PluginHealth:
        return cls(HEALTH_NOT_CONFIGURED, detail, facts)

    @classmethod
    def failed(cls, detail: str, **facts: Any) -> PluginHealth:
        return cls(HEALTH_FAILED, detail, facts)

    @classmethod
    def disabled(cls, detail: str, **facts: Any) -> PluginHealth:
        return cls(HEALTH_DISABLED, detail, facts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "facts": dict(self.facts),
            "checked_at": self.checked_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginManifest:
    """What a plugin says about itself before anything of it is started.

    The manifest is deliberately inert: reading it must not open a socket, touch
    the database or import a vendor SDK, because the plugins screen renders it
    for integrations that are disabled or broken.
    """

    name: str
    version: str
    summary: str
    vendor: str | None = None
    capabilities: tuple[str, ...] = ()
    #: Node roles this plugin is allowed to load on. Mirroring a rack sensor is
    #: safe on both nodes; anything that commands equipment belongs on the
    #: primary only (SDD 16.1 -- one supervisory control source).
    node_roles: tuple[str, ...] = ("primary", "secondary")
    #: Option keys that must be present before the plugin can do its job. The
    #: manager reports the missing ones instead of letting the plugin fail
    #: obscurely at its first poll.
    required_options: tuple[str, ...] = ()
    #: Relative path into the help site, for the "read more" link.
    docs: str | None = None

    def __post_init__(self) -> None:
        if not NAME_PATTERN.match(self.name):
            raise ValueError(
                f"Invalid plugin name {self.name!r}: it becomes a URL segment and an environment "
                "variable fragment, so it must match " + NAME_PATTERN.pattern
            )
        unknown = tuple(c for c in self.capabilities if c not in CAPABILITIES)
        if unknown:
            raise ValueError(
                f"Plugin {self.name!r} declares unknown capabilities {unknown}: expected {CAPABILITIES}"
            )
        unknown_roles = tuple(r for r in self.node_roles if r not in ("primary", "secondary"))
        if unknown_roles:
            raise ValueError(f"Plugin {self.name!r} declares unknown node roles {unknown_roles}")
        if not self.node_roles:
            raise ValueError(f"Plugin {self.name!r} declares no node roles and could never load")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "vendor": self.vendor,
            "capabilities": list(self.capabilities),
            "node_roles": list(self.node_roles),
            "required_options": list(self.required_options),
            "docs": self.docs,
        }


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginContext:
    """Everything a plugin is given, and nothing more.

    ``session_factory`` and ``bus`` are optional so that discovery works in the
    CLI and in tests, where neither exists. A plugin that needs one must check;
    ``setup()`` is the place to say so.
    """

    settings: Settings
    options: Mapping[str, str] = field(default_factory=dict)
    session_factory: sessionmaker[Session] | None = None
    bus: MessageBus | None = None

    def option(self, key: str, default: str | None = None) -> str | None:
        value = self.options.get(key)
        return default if value is None or value == "" else value

    def int_option(self, key: str, default: int) -> int:
        raw = self.option(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            logger.warning("Plugin option %s=%r is not an integer; using %d", key, raw, default)
            return default

    def float_option(self, key: str, default: float) -> float:
        raw = self.option(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            logger.warning("Plugin option %s=%r is not a number; using %s", key, raw, default)
            return default

    def bool_option(self, key: str, default: bool = False) -> bool:
        raw = self.option(key)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def missing(self, keys: Sequence[str]) -> tuple[str, ...]:
        """Which of ``keys`` have not been supplied."""
        return tuple(key for key in keys if not self.option(key))


# ---------------------------------------------------------------------------
# The plugin itself
# ---------------------------------------------------------------------------


@runtime_checkable
class Plugin(Protocol):
    """What the manager requires of a plugin object.

    Only :attr:`manifest` is mandatory. Everything else has a default in
    :class:`PluginBase`, so a plugin contributing one capability writes one
    method.
    """

    manifest: PluginManifest

    def setup(self, context: PluginContext) -> None: ...

    def routers(self) -> Sequence[APIRouter]: ...

    def services(self) -> Sequence[BackgroundService]: ...

    def mirror_sources(self) -> Sequence[MirrorSource]: ...

    def health(self) -> PluginHealth: ...


class PluginBase:
    """Convenience base: declare a manifest, override what you contribute.

    Subclasses that override :meth:`setup` should call ``super().setup(context)``
    -- :attr:`context` is what every other method reads.
    """

    manifest: PluginManifest

    def __init__(self) -> None:
        self.context: PluginContext | None = None

    def setup(self, context: PluginContext) -> None:
        self.context = context

    def routers(self) -> Sequence[APIRouter]:
        return ()

    def services(self) -> Sequence[BackgroundService]:
        return ()

    def mirror_sources(self) -> Sequence[MirrorSource]:
        return ()

    def health(self) -> PluginHealth:
        """Default: configured if every required option is present.

        A plugin with a transport of its own should override this and report
        what the transport actually did. "Every setting is present" is not the
        same claim as "it works", and only the plugin can tell the difference.
        """
        if self.context is None:
            return PluginHealth.failed("setup() has not run, so the plugin has no configuration")
        missing = self.context.missing(self.manifest.required_options)
        if missing:
            return PluginHealth.not_configured(
                "Not configured: "
                + ", ".join(missing)
                + " "
                + ("is" if len(missing) == 1 else "are")
                + " unset",
                missing_options=list(missing),
            )
        return PluginHealth.ok("Configured; this plugin reports no transport of its own")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} {self.manifest.name}@{self.manifest.version}>"
