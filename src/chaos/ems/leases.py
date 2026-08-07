"""Power-budget leases (SDD 31.4) and dynamic priority (SDD 31.3).

    "Subsystems must remain safe if a lease expires or is revoked. A power
    budget is not a safety permissive." -- SDD 31.4

That sentence is the whole design of this module. A lease is an **allocation**:
it says how much power a subsystem may draw and until when. It is not a
permission to be unsafe, and it is not an interlock. Concretely:

* Granting a lease issues **no** equipment command. It records an allocation and
  publishes a budget; the subsystem's own controller decides what to do with it.
* Expiring or revoking a lease issues **no** stop command either. The budget
  falls back to zero and the subsystem is expected to wind itself down safely
  under its own control. If the EMS also wants a load *off*, that is a shed
  action through :mod:`chaos.ems.shedding`, recorded separately.
* Therefore a lease that expires while the platform is offline leaves every
  local controller exactly as safe as it was: nothing depended on the lease for
  protection.

Dynamic priority (SDD 31.3) works the same way: a temporary tier override must
carry a reason and an expiry, and when it expires the load simply returns to its
base tier.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from chaos.ems.config import EmsConfig
from chaos.ems.derived import DerivedEnergyState
from chaos.models.energy import PowerBudgetLease, PowerLoadProfile

logger = logging.getLogger(__name__)

LEASE_STATES = ("active", "expired", "revoked", "denied")


@dataclass
class LeaseDecision:
    granted: bool
    lease: PowerBudgetLease | None = None
    reason: str = ""
    available_kw: float | None = None
    already_granted_kw: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "lease_id": self.lease.lease_id if self.lease else None,
            "reason": self.reason,
            "available_kw": self.available_kw,
            "already_granted_kw": self.already_granted_kw,
        }


@dataclass
class LeaseSweepResult:
    expired: list[str] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    tier_overrides_expired: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "expired": list(self.expired),
            "revoked": list(self.revoked),
            "tier_overrides_expired": list(self.tier_overrides_expired),
        }

    @property
    def changed(self) -> bool:
        return bool(self.expired or self.revoked or self.tier_overrides_expired)


def new_lease_id(now: dt.datetime) -> str:
    return f"power-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:12]}"


class LeaseManager:
    """Grants, denies, revokes and expires time-limited power budgets."""

    def __init__(self, config: EmsConfig) -> None:
        self.config = config

    # -- queries ---------------------------------------------------------
    def active_leases(self, session: Session, now: dt.datetime) -> list[PowerBudgetLease]:
        leases = session.scalars(select(PowerBudgetLease).where(PowerBudgetLease.state == "active"))
        return [
            lease for lease in leases if _aware(lease.expires_at) > now and _aware(lease.starts_at) <= now
        ]

    def granted_kw(self, session: Session, now: dt.datetime) -> float:
        return sum(lease.granted_kw for lease in self.active_leases(session, now))

    def grantable_kw(self, derived: DerivedEnergyState) -> float | None:
        """Surplus the EMS is willing to hand out (SDD 31.4, 36).

        Only a fraction of the computed surplus is grantable, so a lease can
        never consume the entire margin and the site keeps room to absorb a
        cloud without immediately clawing budgets back.
        """
        surplus = derived.value("surplus_power_kw")
        if surplus is None:
            return None
        return max(surplus, 0.0) * self.config.lease_surplus_fraction

    # -- grant -----------------------------------------------------------
    def request(
        self,
        session: Session,
        *,
        asset_id: str,
        requested_kw: float,
        reason: str,
        requested_by: str,
        energy_state: str,
        derived: DerivedEnergyState,
        now: dt.datetime,
        duration_s: int | None = None,
        priority: int = 3,
        revocable: bool = True,
        starts_at: dt.datetime | None = None,
    ) -> LeaseDecision:
        """Grant or deny a power-budget lease.

        Denials are recorded too: SDD 37.2 lists lease grant/revoke/expiry as
        events, and an operator whose welding window was refused deserves the
        reason in the record rather than a silent nothing.
        """
        if requested_kw <= 0:
            return self._deny(
                session,
                asset_id,
                requested_kw,
                reason,
                requested_by,
                now,
                priority,
                "requested power must be positive",
            )
        if not reason:
            raise ValueError("A power-budget lease requires a reason")
        if not (self.config.lease_min_priority <= priority <= self.config.lease_max_priority):
            raise ValueError(
                f"priority must be between {self.config.lease_min_priority} and "
                f"{self.config.lease_max_priority}"
            )

        requested_duration = duration_s if duration_s is not None else self.config.lease_default_duration_s
        if requested_duration <= 0:
            raise ValueError("A lease must have a positive duration; leases always expire")
        duration = min(requested_duration, self.config.lease_max_duration_s)

        if energy_state not in self.config.lease_grant_states:
            return self._deny(
                session,
                asset_id,
                requested_kw,
                reason,
                requested_by,
                now,
                priority,
                f"{energy_state} does not permit new power-budget grants",
            )

        available = self.grantable_kw(derived)
        if available is None:
            return self._deny(
                session,
                asset_id,
                requested_kw,
                reason,
                requested_by,
                now,
                priority,
                "surplus power is not observable, so no budget can be allocated",
            )

        already = self.granted_kw(session, now)
        headroom = available - already
        if requested_kw > headroom:
            return self._deny(
                session,
                asset_id,
                requested_kw,
                reason,
                requested_by,
                now,
                priority,
                f"requested {requested_kw:.2f} kW exceeds the {headroom:.2f} kW remaining of the "
                f"{available:.2f} kW grantable surplus ({already:.2f} kW already granted)",
                available=available,
                already=already,
            )

        start = starts_at or now
        lease = PowerBudgetLease(
            lease_id=new_lease_id(now),
            asset_id=asset_id,
            granted_kw=requested_kw,
            starts_at=start,
            expires_at=start + dt.timedelta(seconds=duration),
            priority=priority,
            reason=reason,
            revocable=revocable,
            requested_by=requested_by,
            state="active",
        )
        session.add(lease)
        session.flush()
        logger.info(
            "Power lease %s granted: %s %.2f kW until %s (%s)",
            lease.lease_id,
            asset_id,
            requested_kw,
            lease.expires_at,
            reason,
        )
        return LeaseDecision(
            granted=True,
            lease=lease,
            reason=f"granted {requested_kw:.2f} kW of {headroom:.2f} kW available",
            available_kw=available,
            already_granted_kw=already,
        )

    def _deny(
        self,
        session: Session,
        asset_id: str,
        requested_kw: float,
        reason: str,
        requested_by: str,
        now: dt.datetime,
        priority: int,
        why: str,
        *,
        available: float | None = None,
        already: float = 0.0,
    ) -> LeaseDecision:
        lease = PowerBudgetLease(
            lease_id=new_lease_id(now),
            asset_id=asset_id,
            granted_kw=max(requested_kw, 0.0),
            starts_at=now,
            expires_at=now,
            priority=priority,
            reason=reason,
            revocable=True,
            requested_by=requested_by,
            state="denied",
            revoked_at=now,
            revoked_reason=why,
        )
        session.add(lease)
        session.flush()
        return LeaseDecision(
            granted=False, lease=lease, reason=why, available_kw=available, already_granted_kw=already
        )

    # -- revoke / expire --------------------------------------------------
    def revoke(
        self,
        session: Session,
        lease_id: str,
        *,
        reason: str,
        actor: str,
        now: dt.datetime,
        force: bool = False,
    ) -> PowerBudgetLease:
        """Withdraw an allocation.

        Revocation never issues a stop command. The subsystem loses budget, not
        protection, and is expected to wind down under its own control.
        """
        lease = session.get(PowerBudgetLease, lease_id)
        if lease is None:
            raise KeyError(lease_id)
        if lease.state != "active":
            return lease
        if not lease.revocable and not force:
            raise PermissionError(f"Lease {lease_id} is not revocable")
        lease.state = "revoked"
        lease.revoked_at = now
        lease.revoked_reason = f"{reason} (by {actor})"
        session.flush()
        logger.info("Power lease %s revoked by %s: %s", lease_id, actor, reason)
        return lease

    def sweep(
        self,
        session: Session,
        *,
        now: dt.datetime,
        energy_state: str,
        derived: DerivedEnergyState | None = None,
    ) -> LeaseSweepResult:
        """Expire due leases, enforce the state policy and the surplus budget.

        Called every tick. Order matters: expire first, then apply the state
        policy, then trim to the available surplus lowest-priority-first.
        """
        result = LeaseSweepResult()

        for lease in session.scalars(select(PowerBudgetLease).where(PowerBudgetLease.state == "active")):
            if _aware(lease.expires_at) <= now:
                lease.state = "expired"
                result.expired.append(lease.lease_id)
                logger.info("Power lease %s expired", lease.lease_id)

        if energy_state in self.config.lease_revoke_states:
            for lease in self.active_leases(session, now):
                if not lease.revocable:
                    continue
                lease.state = "revoked"
                lease.revoked_at = now
                lease.revoked_reason = f"energy state {energy_state} withdraws discretionary allocations"
                result.revoked.append(lease.lease_id)
        elif derived is not None:
            available = self.grantable_kw(derived)
            if available is not None:
                # Trim to fit, cheapest priority first (higher number = lower
                # priority, mirroring the tier model in SDD 31.2).
                active = sorted(
                    self.active_leases(session, now),
                    key=lambda lease: (-lease.priority, _aware(lease.starts_at)),
                )
                total = sum(lease.granted_kw for lease in active)
                for lease in active:
                    if total <= available:
                        break
                    if not lease.revocable:
                        continue
                    lease.state = "revoked"
                    lease.revoked_at = now
                    lease.revoked_reason = (
                        f"granted {total:.2f} kW exceeds the {available:.2f} kW grantable surplus"
                    )
                    total -= lease.granted_kw
                    result.revoked.append(lease.lease_id)

        result.tier_overrides_expired = self.expire_tier_overrides(session, now=now)
        session.flush()
        return result

    def budgets(self, session: Session, now: dt.datetime) -> dict[str, float]:
        """Total active granted kW per asset (SDD 13's "load budget")."""
        budgets: dict[str, float] = {}
        for lease in self.active_leases(session, now):
            budgets[lease.asset_id] = budgets.get(lease.asset_id, 0.0) + lease.granted_kw
        return budgets

    # -- dynamic priority (SDD 31.3) --------------------------------------
    def set_tier_override(
        self,
        session: Session,
        asset_id: str,
        *,
        effective_tier: int,
        reason: str,
        actor: str,
        now: dt.datetime,
        duration_s: int | None = None,
        expires_at: dt.datetime | None = None,
    ) -> PowerLoadProfile:
        """Temporarily change a load's tier. Expiry and reason are mandatory."""
        if not reason:
            raise ValueError("A dynamic priority change requires a reason (SDD 31.3)")
        profile = session.get(PowerLoadProfile, asset_id)
        if profile is None:
            raise KeyError(asset_id)
        if effective_tier < 0 or effective_tier > 4:
            raise ValueError("effective_tier must be between 0 and 4")
        if effective_tier <= self.config.protected_tier and profile.base_tier > self.config.protected_tier:
            raise ValueError(
                "A dynamic override may not promote a load into the protected tier; Tier 0 is "
                "physical protection and control survival, not a priority setting"
            )

        if expires_at is None:
            requested = duration_s if duration_s is not None else self.config.tier_override_max_s
            if requested <= 0:
                raise ValueError("A dynamic priority change must expire (SDD 31.3)")
            expires_at = now + dt.timedelta(seconds=min(requested, self.config.tier_override_max_s))
        elif _aware(expires_at) <= now:
            raise ValueError("A dynamic priority change must expire in the future")
        elif (_aware(expires_at) - now).total_seconds() > self.config.tier_override_max_s:
            raise ValueError(f"A dynamic priority change may not exceed {self.config.tier_override_max_s} s")

        profile.effective_tier = effective_tier
        profile.tier_override_reason = f"{reason} (by {actor})"
        profile.tier_override_expires_at = expires_at
        session.flush()
        logger.info(
            "Tier override on %s: base %s -> %s until %s (%s)",
            asset_id,
            profile.base_tier,
            effective_tier,
            expires_at,
            reason,
        )
        return profile

    def clear_tier_override(self, session: Session, asset_id: str) -> PowerLoadProfile:
        profile = session.get(PowerLoadProfile, asset_id)
        if profile is None:
            raise KeyError(asset_id)
        profile.effective_tier = None
        profile.tier_override_reason = None
        profile.tier_override_expires_at = None
        session.flush()
        return profile

    def expire_tier_overrides(self, session: Session, *, now: dt.datetime) -> list[str]:
        expired: list[str] = []
        for profile in session.scalars(
            select(PowerLoadProfile).where(PowerLoadProfile.effective_tier.is_not(None))
        ):
            expires_at = profile.tier_override_expires_at
            if expires_at is None or _aware(expires_at) <= now:
                profile.effective_tier = None
                profile.tier_override_reason = None
                profile.tier_override_expires_at = None
                expired.append(profile.asset_id)
        return expired


def summarise_leases(leases: list[PowerBudgetLease]) -> list[dict[str, Any]]:
    return [
        {
            "lease_id": lease.lease_id,
            "asset_id": lease.asset_id,
            "granted_kw": lease.granted_kw,
            "starts_at": lease.starts_at.isoformat() if lease.starts_at else None,
            "expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
            "priority": lease.priority,
            "reason": lease.reason,
            "revocable": lease.revocable,
            "requested_by": lease.requested_by,
            "state": lease.state,
            "revoked_at": lease.revoked_at.isoformat() if lease.revoked_at else None,
            "revoked_reason": lease.revoked_reason,
            "note": "an allocation, never a safety permissive (SDD 31.4)",
        }
        for lease in leases
    ]


def _aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value
