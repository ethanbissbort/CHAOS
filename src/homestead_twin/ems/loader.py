"""Load ``data/load_schedule.yaml`` into ``power_load_profiles``.

SDD 49 item 1 asks for a preliminary load schedule carrying branch, rated
power, measured power, tier, control method, minimum on/off time and
restoration policy. The file deliberately leaves the electrical facts
unresolved: ``rated_power_kw`` is null for every load and every unknown is
listed in ``open_fields``. This loader preserves that honesty -- it never
invents a power figure and it never drops an ``open_fields`` entry.

The load is idempotent: running it twice produces the same rows, and running it
after an edit updates them in place so the tier override state (``effective_tier``
and its expiry) set at runtime by SDD 31.3 is not silently clobbered by a
reload unless the base tier itself changed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin.config import DATA_DIR
from homestead_twin.models.energy import PowerLoadProfile
from homestead_twin.models.registry import Asset

logger = logging.getLogger(__name__)

LOAD_SCHEDULE_YAML = "load_schedule.yaml"
LOAD_SCHEDULE_JSON = "load_schedule.json"


@dataclass
class LoadScheduleResult:
    """What a load run did, so the caller can log or assert on it."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    #: Loads whose asset is not in the registry. Reported, never invented.
    missing_assets: list[str] = field(default_factory=list)
    shed_groups: list[dict[str, Any]] = field(default_factory=list)
    restoration_groups: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "total": self.total,
            "missing_assets": list(self.missing_assets),
        }


def schedule_path(data_dir: Path | None = None) -> Path:
    directory = Path(data_dir or DATA_DIR)
    yaml_path = directory / LOAD_SCHEDULE_YAML
    if yaml_path.exists():
        return yaml_path
    return directory / LOAD_SCHEDULE_JSON


def read_schedule(path: Path | None = None, data_dir: Path | None = None) -> dict[str, Any]:
    """Read the schedule from YAML or from its byte-equivalent JSON mirror."""
    target = Path(path) if path else schedule_path(data_dir)
    text = target.read_text(encoding="utf-8")
    if target.suffix == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _profile_fields(record: dict[str, Any]) -> dict[str, Any]:
    restart = dict(record.get("restart") or {})
    return {
        "name": record["name"],
        "base_tier": record["base_tier"],
        "criticality": record["criticality"],
        "rated_power_kw": record.get("rated_power_kw"),
        "measured_power_point": record.get("measured_power_point"),
        "estimated_power_kw": record.get("estimated_power_kw"),
        "branch_circuit": record.get("branch_circuit"),
        "panel_asset_id": record.get("panel_asset_id"),
        "control_method": record.get("control_method", "not_controllable"),
        "minimum_service": dict(record.get("minimum_service") or {}),
        "restart": restart,
        "shed": dict(record.get("shed") or {}),
        "restoration_group": record.get("restoration_group"),
        "restoration_order": record.get("restoration_order"),
        "shed_group": record.get("shed_group"),
        "shed_order": record.get("shed_order"),
        "minimum_on_time_s": record.get("minimum_on_time_s"),
        "minimum_off_time_s": record.get("minimum_off_time_s"),
        "inrush_class": restart.get("inrush_class"),
        "data_status": record.get("data_status", "estimated"),
        "open_fields": list(record.get("open_fields") or []),
        "notes": list(record.get("notes") or []),
    }


def load_schedule(
    session: Session,
    *,
    path: Path | None = None,
    data_dir: Path | None = None,
    require_assets: bool = False,
) -> LoadScheduleResult:
    """Upsert every load record. Safe to run repeatedly.

    ``require_assets`` raises when a load references an asset that is not in the
    registry. The default reports it instead, so the EMS can still be loaded on
    a node whose registry import has not finished.
    """
    document = read_schedule(path=path, data_dir=data_dir)
    result = LoadScheduleResult(
        shed_groups=list(document.get("shed_groups") or []),
        restoration_groups=list(document.get("restoration_groups") or []),
    )

    known_assets = set(session.scalars(select(Asset.asset_id)))

    for record in document.get("loads", []):
        asset_id = record["asset_id"]
        if known_assets and asset_id not in known_assets:
            result.missing_assets.append(asset_id)
            if require_assets:
                raise ValueError(f"Load schedule references unknown asset {asset_id}")
            continue

        values = _profile_fields(record)
        profile = session.get(PowerLoadProfile, asset_id)
        if profile is None:
            profile = PowerLoadProfile(asset_id=asset_id, **values)
            session.add(profile)
            result.created += 1
            continue

        changed = False
        base_tier_changed = profile.base_tier != values["base_tier"]
        for key, value in values.items():
            if getattr(profile, key) != value:
                setattr(profile, key, value)
                changed = True
        if base_tier_changed and profile.effective_tier is not None:
            # A base-tier revision invalidates a running SDD 31.3 override.
            profile.effective_tier = None
            profile.tier_override_reason = None
            profile.tier_override_expires_at = None
        if changed:
            result.updated += 1
        else:
            result.unchanged += 1

    session.flush()
    if result.missing_assets:
        logger.warning(
            "Load schedule references %d asset(s) not present in the registry: %s",
            len(result.missing_assets),
            ", ".join(result.missing_assets),
        )
    return result


def load_profiles(session: Session) -> list[PowerLoadProfile]:
    """All load records, ordered by tier then name for stable presentation."""
    return list(
        session.scalars(
            select(PowerLoadProfile).order_by(PowerLoadProfile.base_tier, PowerLoadProfile.asset_id)
        )
    )


def effective_tier(profile: PowerLoadProfile, now) -> int:
    """Tier after any unexpired dynamic-priority override (SDD 31.3)."""
    expires_at = profile.tier_override_expires_at
    if profile.effective_tier is None or expires_at is None:
        return profile.base_tier
    if expires_at.tzinfo is None:
        import datetime as _dt

        expires_at = expires_at.replace(tzinfo=_dt.timezone.utc)
    if expires_at <= now:
        return profile.base_tier
    return profile.effective_tier
