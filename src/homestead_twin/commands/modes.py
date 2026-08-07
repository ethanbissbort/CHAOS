"""Operating modes for the site, each domain and each asset (SDD section 11).

Three scopes nest: ``site`` contains every ``domain``, a domain contains its
assets. A command is judged against the **most restrictive** mode in that chain,
so putting the site into emergency locks every asset down without touching a
single asset record.

Restrictiveness ordering
------------------------

"Restrictive" here means *how much supervisory authority the platform retains*,
from most to least:

======  ==============  =========================================================
 Rank    Mode            Meaning for Level 3 supervisory control
======  ==============  =========================================================
  6      ``emergency``   Predefined protective state. Latching. Only the
                         protective allow-list runs (SDD section 11).
  5      ``off``         Intentionally unavailable. Nothing should run at all.
  4      ``maintenance`` Automatic starts inhibited, lockout visible. A human
                         maintainer may still act, with an explicit override.
  3      ``degraded``    Reduced capability: failed sensor, comms, power or
                         equipment. Automation continues but is not trusted
                         with discretionary work.
  2      ``manual``      Local or operator-directed control; the platform yields.
  1      ``scheduled``   Runs to a calendar; supervisory targets still apply.
  0      ``automatic``   Normal local control with supervisory targets.
======  ==============  =========================================================

``emergency`` outranks ``off`` because an off asset in an emergency site must
still refuse a start command with the emergency reason -- the operator needs to
see *why* the property is locked down, not merely that one asset is idle.

Latching
--------

SDD section 11: "Emergency mode must not be cleared automatically unless the
triggering condition and reset policy explicitly permit it." This module
implements that literally:

* setting ``emergency`` stores ``auto_clear_allowed=False`` and refuses to carry
  an expiry -- a latch with a timer is not a latch;
* :meth:`ModeManager.expire_modes` skips any row that is not auto-clearable;
* clearing emergency requires a *named human* with at least the ``operator``
  role, a reason, and ``condition_clear=True`` asserting that the triggering
  condition is gone. The default is False, so the default is refusal.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin.commands.interlocks import role_at_least, utc
from homestead_twin.models.base import utcnow
from homestead_twin.models.commands import AuditLogEntry, ModeTransition, OperatingMode
from homestead_twin.models.registry import Asset

logger = logging.getLogger(__name__)

#: SDD section 11 mode set.
MODES = (
    "off",
    "manual",
    "automatic",
    "scheduled",
    "maintenance",
    "degraded",
    "emergency",
)

SCOPE_TYPES = ("site", "domain", "asset")

DEFAULT_MODE = "automatic"
EMERGENCY = "emergency"

#: Higher number == more restrictive. See the module docstring.
RESTRICTIVENESS = {
    "automatic": 0,
    "scheduled": 1,
    "manual": 2,
    "degraded": 3,
    "maintenance": 4,
    "off": 5,
    "emergency": 6,
}

#: Modes that never clear themselves.
LATCHING_MODES = frozenset({EMERGENCY})

#: Minimum role permitted to change any operating mode.
MODE_CHANGE_ROLE = "operator"

#: Default scope id for the site row when the caller supplies no site identity.
SITE_SCOPE_ID = "site"


class ModeError(Exception):
    """Base class for mode-management refusals."""


class UnknownModeError(ModeError):
    """The requested mode or scope type is not part of the SDD mode set."""


class ModeAuthorizationError(ModeError):
    """The caller lacks the role required for this mode change."""


class EmergencyLatchedError(ModeError):
    """Emergency is latched and the caller has not satisfied the reset policy."""


class _PrincipalLike(Protocol):  # pragma: no cover - structural typing only
    name: str
    role: str
    kind: str


@dataclass(frozen=True)
class ModeChain:
    """Resolved site/domain/asset modes for one asset."""

    site: str
    domain: str
    asset: str
    domain_id: str
    asset_id: str

    @property
    def effective(self) -> str:
        return max(
            (self.site, self.domain, self.asset),
            key=lambda mode: RESTRICTIVENESS.get(mode, 0),
        )

    def as_dict(self) -> dict[str, str]:
        return {"site": self.site, "domain": self.domain, "asset": self.asset}


def more_restrictive(*modes: str) -> str:
    """Return the most restrictive of the given modes."""
    known = [mode for mode in modes if mode]
    if not known:
        return DEFAULT_MODE
    return max(known, key=lambda mode: RESTRICTIVENESS.get(mode, 0))


class ModeManager:
    """Reads and changes operating modes, logging every transition.

    The manager never commits on read. Every write path produces exactly one
    :class:`OperatingMode` row per scope, one :class:`ModeTransition` row per
    actual change and one :class:`AuditLogEntry` per attempt -- including
    refusals, which are the entries that matter after an incident.
    """

    def __init__(
        self,
        session: Session,
        *,
        site_id: str = SITE_SCOPE_ID,
        clock=None,
    ) -> None:
        self.session = session
        self.site_id = site_id or SITE_SCOPE_ID
        self._clock = clock or utcnow

    # -- helpers ---------------------------------------------------------
    def _now(self, now: dt.datetime | None = None) -> dt.datetime:
        return now or self._clock()

    @staticmethod
    def _validate_scope(scope_type: str) -> str:
        if scope_type not in SCOPE_TYPES:
            raise UnknownModeError(
                f"Unknown scope type {scope_type!r}; expected one of {', '.join(SCOPE_TYPES)}"
            )
        return scope_type

    @staticmethod
    def _validate_mode(mode: str) -> str:
        if mode not in MODES:
            raise UnknownModeError(f"Unknown mode {mode!r}; expected one of {', '.join(MODES)}")
        return mode

    def _audit(
        self,
        *,
        actor: str,
        actor_role: str | None,
        action: str,
        target_type: str,
        target_id: str,
        outcome: str,
        reason: str | None,
        detail: dict[str, Any] | None,
        occurred_at: dt.datetime,
    ) -> AuditLogEntry:
        entry = AuditLogEntry(
            actor=actor,
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            reason=reason,
            detail=detail or {},
            occurred_at=occurred_at,
        )
        self.session.add(entry)
        return entry

    # -- reads -----------------------------------------------------------
    def get_mode(self, scope_type: str, scope_id: str) -> OperatingMode | None:
        """The stored row for a scope, or ``None`` when it has never been set."""
        self._validate_scope(scope_type)
        stmt = select(OperatingMode).where(
            OperatingMode.scope_type == scope_type, OperatingMode.scope_id == scope_id
        )
        return self.session.execute(stmt).scalars().first()

    def resolve(self, scope_type: str, scope_id: str, now: dt.datetime | None = None) -> str:
        """The mode a scope is *actually* in right now.

        A non-latching mode whose ``expires_at`` has passed reads back as its
        previous mode even before :meth:`expire_modes` rewrites the row, so a
        stale sweep can never leave the platform over-restricted. A latching
        mode ignores expiry entirely.
        """
        row = self.get_mode(scope_type, scope_id)
        return self._resolve_row(row, self._now(now))

    def _resolve_row(self, row: OperatingMode | None, now: dt.datetime) -> str:
        if row is None:
            return DEFAULT_MODE
        if not self._is_expired(row, now):
            return row.mode
        return row.previous_mode or DEFAULT_MODE

    @staticmethod
    def _is_expired(row: OperatingMode, now: dt.datetime) -> bool:
        if row.mode in LATCHING_MODES or not row.auto_clear_allowed:
            return False
        expires_at = utc(row.expires_at)
        return expires_at is not None and expires_at <= now

    def list_modes(self, now: dt.datetime | None = None) -> list[dict[str, Any]]:
        """Every stored scope with its resolved mode, for the API and the UI."""
        now = self._now(now)
        rows = self.session.execute(select(OperatingMode)).scalars().all()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "scope_type": row.scope_type,
                    "scope_id": row.scope_id,
                    "mode": self._resolve_row(row, now),
                    "stored_mode": row.mode,
                    "previous_mode": row.previous_mode,
                    "changed_by": row.changed_by,
                    "changed_at": utc(row.changed_at),
                    "reason": row.reason,
                    "expires_at": utc(row.expires_at),
                    "auto_clear_allowed": bool(row.auto_clear_allowed),
                    "latched": row.mode in LATCHING_MODES,
                }
            )
        out.sort(key=lambda item: (SCOPE_TYPES.index(item["scope_type"]), item["scope_id"]))
        return out

    def domain_of(self, asset_id: str) -> str:
        """Domain for an asset, from the registry when possible."""
        asset = self.session.get(Asset, asset_id)
        if asset is not None:
            return asset.domain
        # Canonical IDs are <domain>.<class>.<location>.<instance> (SDD 25.2),
        # so the first segment is a usable fallback for an unregistered asset.
        return asset_id.split(".", 1)[0]

    def mode_chain(self, asset_id: str, now: dt.datetime | None = None) -> ModeChain:
        """Resolved site / domain / asset modes for one asset."""
        now = self._now(now)
        domain = self.domain_of(asset_id)
        return ModeChain(
            site=self.resolve("site", self.site_id, now),
            domain=self.resolve("domain", domain, now),
            asset=self.resolve("asset", asset_id, now),
            domain_id=domain,
            asset_id=asset_id,
        )

    def effective_mode(self, asset_id: str, now: dt.datetime | None = None) -> str:
        """Most restrictive of the asset, its domain and the site."""
        return self.mode_chain(asset_id, now).effective

    # -- writes ----------------------------------------------------------
    def set_mode(
        self,
        scope_type: str,
        scope_id: str,
        mode: str,
        principal: _PrincipalLike,
        reason: str,
        expires_at: dt.datetime | None = None,
        *,
        condition_clear: bool = False,
        now: dt.datetime | None = None,
        commit: bool = True,
    ) -> OperatingMode:
        """Change a scope's operating mode, or refuse and say why.

        ``condition_clear`` is the caller's assertion that the condition which
        triggered an emergency is no longer present. It defaults to False, so
        clearing an emergency without asserting it is refused.
        """
        now = self._now(now)
        self._validate_scope(scope_type)
        self._validate_mode(mode)
        reason = (reason or "").strip()
        actor = getattr(principal, "name", "unknown")
        actor_role = getattr(principal, "role", "viewer")
        actor_kind = getattr(principal, "kind", "human")

        audit = lambda outcome, detail, why=None: self._audit(  # noqa: E731 - local shorthand
            actor=actor,
            actor_role=actor_role,
            action="mode.set",
            target_type=scope_type,
            target_id=scope_id,
            outcome=outcome,
            reason=why or reason or None,
            detail=detail,
            occurred_at=now,
        )

        if not reason:
            audit("refused", {"requested_mode": mode, "refusal": "missing_reason"})
            if commit:
                self.session.commit()
            raise ModeError("A mode change requires a reason (SDD section 5.7)")

        if not role_at_least(actor_role, MODE_CHANGE_ROLE):
            audit("refused", {"requested_mode": mode, "refusal": "insufficient_role"})
            if commit:
                self.session.commit()
            raise ModeAuthorizationError(
                f"Changing an operating mode requires the {MODE_CHANGE_ROLE!r} role "
                f"(caller has {actor_role!r})"
            )

        row = self.get_mode(scope_type, scope_id)
        current = self._resolve_row(row, now)

        # --- the emergency latch ---------------------------------------
        if current == EMERGENCY and mode != EMERGENCY:
            if actor_kind != "human":
                audit(
                    "refused",
                    {"requested_mode": mode, "refusal": "non_human_actor", "actor_kind": actor_kind},
                )
                if commit:
                    self.session.commit()
                raise EmergencyLatchedError(
                    "Emergency mode may only be cleared by a named human operator; "
                    f"actor kind is {actor_kind!r}"
                )
            if not condition_clear:
                audit(
                    "refused",
                    {"requested_mode": mode, "refusal": "condition_not_clear"},
                )
                if commit:
                    self.session.commit()
                raise EmergencyLatchedError(
                    "Emergency mode is latched: the caller must assert condition_clear "
                    "after verifying the triggering condition is no longer present "
                    "(SDD section 11)"
                )

        # --- no-op ------------------------------------------------------
        if row is not None and current == mode:
            row.reason = reason
            row.changed_by = actor
            if mode not in LATCHING_MODES:
                row.expires_at = expires_at
            audit("unchanged", {"mode": mode})
            if commit:
                self.session.commit()
            return row

        latching = mode in LATCHING_MODES
        if latching and expires_at is not None:
            logger.warning(
                "Ignoring expires_at on a latching %s mode for %s:%s", mode, scope_type, scope_id
            )
            expires_at = None

        if row is None:
            row = OperatingMode(scope_type=scope_type, scope_id=scope_id, mode=DEFAULT_MODE)
            self.session.add(row)
            current = DEFAULT_MODE

        row.previous_mode = current
        row.mode = mode
        row.changed_by = actor
        row.reason = reason
        row.changed_at = now
        row.expires_at = expires_at
        row.auto_clear_allowed = not latching

        self.session.add(
            ModeTransition(
                scope_type=scope_type,
                scope_id=scope_id,
                from_mode=current,
                to_mode=mode,
                changed_by=actor,
                reason=reason,
                occurred_at=now,
            )
        )
        audit(
            "changed",
            {
                "from_mode": current,
                "to_mode": mode,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "auto_clear_allowed": not latching,
                "condition_clear": bool(condition_clear),
                "latched": latching,
            },
        )
        if commit:
            self.session.commit()
        logger.info(
            "Operating mode %s:%s %s -> %s by %s (%s)", scope_type, scope_id, current, mode, actor, reason
        )
        return row

    def expire_modes(
        self,
        now: dt.datetime | None = None,
        *,
        actor: str = "system.mode_expiry",
        commit: bool = True,
    ) -> list[OperatingMode]:
        """Clear temporary modes whose expiry has passed.

        Latching modes are skipped unconditionally: an emergency never times
        out. Everything cleared here is logged as a transition and an audit
        entry, so an automatic clear is as visible as a human one.
        """
        now = self._now(now)
        cleared: list[OperatingMode] = []
        rows = self.session.execute(select(OperatingMode)).scalars().all()
        for row in rows:
            if not self._is_expired(row, now):
                continue
            previous = row.mode
            target = row.previous_mode or DEFAULT_MODE
            row.previous_mode = previous
            row.mode = target
            row.changed_by = actor
            row.changed_at = now
            row.expires_at = None
            row.reason = f"Automatic clear: {previous} mode expiry elapsed"
            self.session.add(
                ModeTransition(
                    scope_type=row.scope_type,
                    scope_id=row.scope_id,
                    from_mode=previous,
                    to_mode=target,
                    changed_by=actor,
                    reason=row.reason,
                    occurred_at=now,
                )
            )
            self._audit(
                actor=actor,
                actor_role="service",
                action="mode.expire",
                target_type=row.scope_type,
                target_id=row.scope_id,
                outcome="changed",
                reason=row.reason,
                detail={"from_mode": previous, "to_mode": target},
                occurred_at=now,
            )
            cleared.append(row)
        if cleared and commit:
            self.session.commit()
        return cleared
