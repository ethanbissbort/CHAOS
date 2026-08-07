"""Rack elevation for the primary 42U rack (SDD 17.2, work-queue item 49.4).

``data/rack_layout.yaml`` is a **proposal**. Its own header says so:
``document_status: proposal_for_review``, ``approval_status: not_ratified``,
``authority: none_until_owner_review``, ``review_required_before_use: true``.
Nothing in it has been built, measured or ratified, and the register's
``location.rack_units`` field is still TBD for every device in it.

So this endpoint has one job beyond joining data: it must make that
impossible to forget. The ratification metadata is lifted to the top of the
payload as :func:`_document_block`, every device carries its own
``placement_status`` and ``data_status``, and the response advertises
``usable_as_built: false``. A client that renders this as *the* elevation is
misrepresenting the document, and the payload gives it no excuse.

Three further rules, inherited from ``overview.py`` and applied here:

1. **The U-grid is served whole.** Every unit 1..42 is returned with what
   occupies it or an explicit "free", so no client has to infer occupancy from
   a start/height pair and get it subtly wrong. Numbering is bottom-to-top,
   unit 1 lowest, because that is what the document declares.
2. **Availability uses the platform vocabulary, unchanged.** ``ok``, ``stale``,
   ``no_data``, ``no_points``, ``design_only``, ``not_deployed`` are imported
   from ``overview.py`` rather than restated, so a rack faceplate and a home
   screen tile can never disagree about what "stale" means. A device with no
   telemetry reports ``no_data`` -- never a healthy green -- and a device that
   reported once and went quiet reports ``stale``, which is a different and
   more alarming fact.
3. **Nothing that exists is dropped.** Zero-U and door-mounted gear consumes no
   rack units but is still in the rack; it is returned in its own list. The
   five deliberately excluded assets are returned with their exclusion reasons.

The endpoint also re-derives the document's own claims -- non-overlap, the
occupied/free split, PDU outlet agreement -- instead of trusting them, and
reports any disagreement in ``findings``. The document says the layout "is
checked for overlap"; this is that check, run on every request.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from homestead_twin.api.deps import AppSettings, DbSession
from homestead_twin.api.routers.overview import (
    DEPLOYED_STATUSES,
    SEVERITY_RANK,
    STATUS_DESIGN_ONLY,
    STATUS_EXPLANATIONS,
    STATUS_NO_DATA,
    STATUS_NO_POINTS,
    STATUS_NOT_DEPLOYED,
    STATUS_OK,
    STATUS_STALE,
    UNCLEARED_ALARM_STATES,
)
from homestead_twin.config import Settings
from homestead_twin.models import Alarm, Asset, CurrentState, Point, utcnow

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rack"])

LAYOUT_FILENAME = "rack_layout.yaml"

#: Asset classes grouped into the colour/legend buckets the elevation draws.
#: Kept server-side so the API, the SVG and any exported drawing agree on which
#: bucket a device belongs to. The colours themselves are a rendering concern
#: and live in the client.
CATEGORIES: dict[str, str] = {
    "ups": "power",
    "ats": "transfer",
    "pdu": "power_distribution",
    "server": "compute",
    "storage_array": "storage",
    "switch": "network",
    "router": "network_edge",
    "wireless_controller": "wireless",
    "environmental_monitor": "environment",
    "access_controller": "security",
    "camera": "security",
    "safety_sensor": "safety",
    "alarm_output": "safety",
}
DEFAULT_CATEGORY = "other"

CATEGORY_LABELS: dict[str, str] = {
    "power": "UPS and power source",
    "transfer": "Automatic transfer switch",
    "power_distribution": "Power distribution",
    "compute": "Compute",
    "storage": "Storage",
    "network": "Network switching",
    "network_edge": "Routing and WAN",
    "wireless": "Wireless",
    "environment": "Environmental monitoring",
    "security": "Security",
    "safety": "Safety",
    DEFAULT_CATEGORY: "Other",
}

#: Height bases that mean the U count is a guess. Units claimed on one of these
#: are provisional: if the guess is wrong the elevation below and above it moves.
ESTIMATED_HEIGHT_BASES = frozenset({"estimated_pending_model_confirmation"})

#: Severity ladder for findings. ``blocking`` means the document cannot be used
#: for the thing it looks like it is for until the field is resolved.
FINDING_SEVERITIES = ("blocking", "warning", "info")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

#: (path, mtime_ns, size) -> parsed document. The console polls this endpoint on
#: the same timer as the home screen; re-parsing a thousand-line YAML file every
#: fifteen seconds is waste, and the key includes mtime so an edited file is
#: picked up on the next request without a restart.
_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}


def load_layout(data_dir: Path) -> dict[str, Any]:
    """Read and parse the rack layout proposal.

    Raises ``HTTPException`` rather than returning an empty rack. An absent
    layout document is a deployment fault, and a blank 42U elevation is exactly
    the kind of confident-looking lie this platform exists to avoid: it would
    read as "the rack is empty", not as "the document is missing".
    """
    path = Path(data_dir) / LAYOUT_FILENAME
    try:
        stat = path.stat()
    except OSError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{path} is not readable ({exc.strerror}). The rack elevation cannot be drawn "
            "without it, and an empty rack would be a false statement, not a degraded one.",
        ) from exc

    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        with path.open(encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{path} could not be parsed: {exc}",
        ) from exc

    if not isinstance(document, dict):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"{path} did not parse to a mapping; it is not a rack layout document.",
        )

    _CACHE.clear()
    _CACHE[key] = document
    return document


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    aware = _as_utc(value)
    return aware.isoformat() if aware else None


def _is_stale(state: CurrentState, point: Point | None, settings: Settings, now: dt.datetime) -> bool:
    """Same rule as the overview aggregate: a value past its window is unknown.

    Deliberately reimplemented rather than imported from ``overview``: the
    shared thing that must not drift is the *vocabulary*, and that is imported.
    Duplicating seven lines of arithmetic is cheaper than a rack elevation that
    disappears from the API because a private helper was renamed elsewhere.
    """
    ts = _as_utc(state.ts) or _as_utc(state.received_at)
    if ts is None:
        return True
    window = state.stale_after_s
    if window is None and point is not None:
        window = point.stale_after_s
    if window is None:
        window = settings.default_stale_after_s
    if not window or window <= 0:
        return False
    return (now - ts).total_seconds() > window


def _finding(code: str, severity: str, title: str, detail: str, **extra: Any) -> dict[str, Any]:
    entry = {"code": code, "severity": severity, "title": title, "detail": detail}
    entry.update({key: value for key, value in extra.items() if value})
    return entry


def _is_racked(placement: dict[str, Any]) -> bool:
    """Does this placement claim rack units?

    Both tests matter. ``mount_style`` is the document's declaration, and the
    height is what actually consumes the grid; a door-mounted device records
    ``rack_unit_height: 0`` and a null start. Requiring both keeps a
    half-completed row out of the elevation instead of drawing it at U0.
    """
    return (
        placement.get("mount_style") == "rack_unit"
        and placement.get("rack_unit_start") is not None
        and int(placement.get("rack_unit_height") or 0) > 0
    )


# ---------------------------------------------------------------------------
# Live state
# ---------------------------------------------------------------------------


class LiveState:
    """Registry, telemetry and alarms for the assets named by the layout.

    Read in four queries, scoped to the layout's asset ids. Every read is
    guarded: a rack elevation is a screen an operator opens *because* something
    is wrong, which sometimes includes the database, and a 500 there would hide
    the layout as well as the fault.
    """

    def __init__(self, session, settings: Settings, asset_ids: list[str]) -> None:
        self.settings = settings
        self.now = utcnow()
        self.problems: list[dict[str, str]] = []
        self.assets: dict[str, Asset] = {}
        self.points: dict[str, list[Point]] = defaultdict(list)
        self.states: dict[str, CurrentState] = {}
        self.alarms: dict[str, list[Alarm]] = defaultdict(list)
        if asset_ids:
            self._load(session, asset_ids)

    def _guarded(self, session, stmt, source: str) -> list:
        try:
            return list(session.execute(stmt).scalars())
        except SQLAlchemyError as exc:
            detail = str(getattr(exc, "orig", exc))
            self.problems.append({"source": source, "error": type(exc).__name__, "detail": detail[:240]})
            try:
                session.rollback()
            except Exception:  # pragma: no cover - defensive
                logger.exception("rollback failed after read error on %s", source)
            return []

    def _load(self, session, asset_ids: list[str]) -> None:
        # Chunked so a large layout never exceeds SQLite's variable limit.
        for start in range(0, len(asset_ids), 200):
            chunk = asset_ids[start : start + 200]
            for asset in self._guarded(session, select(Asset).where(Asset.asset_id.in_(chunk)), "assets"):
                self.assets[asset.asset_id] = asset
            for point in self._guarded(session, select(Point).where(Point.asset_id.in_(chunk)), "points"):
                self.points[point.asset_id].append(point)
            stmt = select(CurrentState).where(CurrentState.asset_id.in_(chunk))
            for state in self._guarded(session, stmt, "current_state"):
                self.states[state.point_id] = state
            stmt = select(Alarm).where(Alarm.asset_id.in_(chunk), Alarm.state.in_(UNCLEARED_ALARM_STATES))
            for alarm in self._guarded(session, stmt, "alarms"):
                self.alarms[alarm.asset_id or ""].append(alarm)

    @property
    def degraded(self) -> bool:
        return bool(self.problems)

    def health(self, asset_id: str) -> dict[str, Any]:
        """Availability of one device, in the platform's shared vocabulary.

        The ordering of the branches is the whole point:

        * a device the register has never heard of is ``not_deployed``;
        * a device with no points at all is ``design_only`` when it is still a
          paper record and ``no_points`` when it is supposedly installed --
          the second is an instrumentation gap, the first is just early;
        * points that have never reported are ``no_data``;
        * points that reported and stopped are ``stale``.

        None of those is ``ok``, and none of them is a zero.
        """
        asset = self.assets.get(asset_id)
        if asset is None:
            return {
                "status": STATUS_NOT_DEPLOYED,
                "reason": (
                    "The layout names an asset that is not in the register. Nothing can be "
                    "known about it, including whether it exists."
                ),
                "lifecycle_status": None,
                "criticality": None,
                "expected_to_report": False,
                "point_count": 0,
                "reporting_point_count": 0,
                "stale_point_count": 0,
                "last_reported_at": None,
                "availability_point": None,
                "points": [],
            }

        deployed = asset.status in DEPLOYED_STATUSES
        points = sorted(self.points.get(asset_id, []), key=lambda p: p.point_name)
        rows: list[dict[str, Any]] = []
        reporting = 0
        stale = 0
        newest: dt.datetime | None = None
        availability: dict[str, Any] | None = None

        for point in points:
            state = self.states.get(point.point_id)
            if state is None:
                row = {
                    "point_name": point.point_name,
                    "point_id": point.point_id,
                    "status": STATUS_NO_DATA,
                    "value": None,
                    "unit": point.unit,
                    "quality": None,
                    "ts": None,
                }
            else:
                reporting += 1
                is_stale = _is_stale(state, point, self.settings, self.now)
                stale += 1 if is_stale else 0
                ts = _as_utc(state.ts)
                if ts is not None and (newest is None or ts > newest):
                    newest = ts
                row = {
                    "point_name": point.point_name,
                    "point_id": point.point_id,
                    "status": STATUS_STALE if is_stale else STATUS_OK,
                    "value": state.value,
                    "unit": state.unit or point.unit,
                    "quality": state.quality,
                    "ts": _iso(state.ts),
                }
            rows.append(row)
            if point.point_name == "availability_state":
                availability = row

        if not points:
            status_name = STATUS_DESIGN_ONLY if not deployed else STATUS_NO_POINTS
            reason = (
                "No points are registered for this asset, and it is a design record only."
                if not deployed
                else (
                    "This asset is recorded as installed but carries no points, so nothing "
                    "about it can be measured. That is an instrumentation gap."
                )
            )
        elif reporting == 0:
            status_name = STATUS_NO_DATA
            reason = f"{len(points)} points are registered and none has ever reported a value." + (
                " The asset is not installed yet, so this is expected -- but it is still"
                " an absence of data, not a healthy reading."
                if not deployed
                else " The asset is recorded as installed, so this is a live fault."
            )
        elif stale == reporting:
            status_name = STATUS_STALE
            reason = (
                f"All {reporting} reporting points are past their stale window. This device "
                "reported and then stopped: treat every value as unknown, not as last-known-good."
            )
        else:
            status_name = STATUS_OK
            reason = f"{reporting - stale} of {len(points)} points are reporting inside their stale window."

        return {
            "status": status_name,
            "reason": reason,
            "lifecycle_status": asset.status,
            "criticality": asset.criticality,
            # Only an installed device is *expected* to report. Both a planned
            # device and a commissioned one read `no_data`; this flag is what
            # separates "not built yet" from "built and silent".
            "expected_to_report": deployed,
            "point_count": len(points),
            "reporting_point_count": reporting,
            "stale_point_count": stale,
            "last_reported_at": _iso(newest),
            "availability_point": availability,
            "points": rows,
        }

    def alarm_block(self, asset_id: str) -> dict[str, Any]:
        alarms = sorted(
            self.alarms.get(asset_id, []),
            key=lambda a: (SEVERITY_RANK.get(a.severity, 9), a.detected_at or self.now),
        )
        return {
            "active_count": len(alarms),
            "worst_severity": alarms[0].severity if alarms else None,
            "items": [
                {
                    "id": alarm.id,
                    "alarm_key": alarm.alarm_key,
                    "severity": alarm.severity,
                    "state": alarm.state,
                    "message": alarm.message,
                    "detected_at": _iso(alarm.detected_at),
                    "acknowledged_by": alarm.acknowledged_by,
                    "suppressed": bool(alarm.suppressed),
                }
                for alarm in alarms
            ],
        }

    def asset_brief(self, asset_id: str | None) -> dict[str, Any] | None:
        if not asset_id:
            return None
        asset = self.assets.get(asset_id)
        if asset is None:
            return {"asset_id": asset_id, "in_register": False}
        properties = asset.properties or {}
        return {
            "asset_id": asset.asset_id,
            "in_register": True,
            "name": asset.name,
            "domain": asset.domain,
            "asset_class": asset.asset_class,
            "status": asset.status,
            "criticality": asset.criticality,
            "manufacturer": properties.get("manufacturer"),
            "model": properties.get("model"),
            "open_fields": list(asset.open_fields or []),
        }


# ---------------------------------------------------------------------------
# Document blocks
# ---------------------------------------------------------------------------


def _document_block(document: dict[str, Any]) -> dict[str, Any]:
    """Ratification status, hoisted to where a client cannot miss it."""
    approval = document.get("approval_status")
    ratified = approval == "ratified"
    open_fields = list(document.get("open_document_fields") or [])
    return {
        "schema_version": document.get("schema_version"),
        "document_type": document.get("document_type"),
        "document_status": document.get("document_status"),
        "approval_status": approval,
        "authority": document.get("authority"),
        "review_required_before_use": bool(document.get("review_required_before_use")),
        "generated_at": document.get("generated_at"),
        "source_document": document.get("source_document"),
        "merge_target": document.get("merge_target"),
        "extends": document.get("extends"),
        "supersedes": document.get("supersedes"),
        "work_queue_item": document.get("work_queue_item"),
        "purpose": document.get("purpose"),
        "ratified": ratified,
        # The single field a client should branch on. Everything drawn from
        # this document is a proposal until an owner says otherwise.
        "usable_as_built": ratified and not document.get("review_required_before_use"),
        "banner": {
            "headline": (
                "RATIFIED LAYOUT" if ratified else "PROPOSAL — NOT RATIFIED — DO NOT BUILD OR PATCH FROM THIS"
            ),
            "detail": (
                "This elevation is a proposal awaiting owner and commissioning review "
                f"(document_status: {document.get('document_status')}, "
                f"approval_status: {approval}, authority: {document.get('authority')}). "
                "No device has been installed at these positions, and the asset register still "
                "records location.rack_units as TBD for every one of them."
            ),
            "open_fields": open_fields,
            "open_field_count": len(open_fields),
        },
        "open_document_fields": open_fields,
        "placement_principles": list(document.get("placement_principles") or []),
        "layout_notes": list(document.get("layout_notes") or []),
    }


def _rack_block(document: dict[str, Any], live: LiveState) -> dict[str, Any]:
    rack = dict(document.get("rack") or {})
    numbering = rack.get("rack_unit_numbering") or "bottom_to_top_unit_1_is_lowest"
    return {
        "rack_asset_id": rack.get("rack_asset_id"),
        "asset": live.asset_brief(rack.get("rack_asset_id")),
        "rack_unit_count": int(rack.get("rack_unit_count") or 0),
        "rack_unit_count_source": rack.get("rack_unit_count_source"),
        "rack_unit_numbering": numbering,
        "numbering_note": (
            "Unit 1 is the lowest unit and unit N the highest. Draw the grid bottom-to-top; "
            "reversing it puts the UPS on the roof."
            if numbering == "bottom_to_top_unit_1_is_lowest"
            else f"Unrecognised numbering declaration '{numbering}'. Do not assume a direction."
        ),
        "open_fields": list(rack.get("open_fields") or []),
    }


# ---------------------------------------------------------------------------
# Devices and the U-grid
# ---------------------------------------------------------------------------


def _device(placement: dict[str, Any], live: LiveState, pdu_index: dict[str, dict]) -> dict[str, Any]:
    asset_id = placement.get("asset_id") or ""
    height = int(placement.get("rack_unit_height") or 0)
    start = placement.get("rack_unit_start")
    racked = _is_racked(placement)
    asset = live.assets.get(asset_id)
    properties = (asset.properties or {}) if asset else {}

    pdu_asset_id = placement.get("pdu_asset_id")
    pdu_entry = pdu_index.get(pdu_asset_id or "")
    feed = placement.get("power_feed")

    return {
        "asset_id": asset_id,
        "name": placement.get("name"),
        "asset_class": placement.get("asset_class"),
        "category": CATEGORIES.get(placement.get("asset_class") or "", DEFAULT_CATEGORY),
        "manufacturer": properties.get("manufacturer"),
        "model": properties.get("model"),
        "mount_style": placement.get("mount_style"),
        "consumes_rack_units": racked,
        "rack_unit_start": start if racked else None,
        "rack_unit_end": (start + height - 1) if racked else None,
        "rack_unit_height": height,
        "rack_unit_height_basis": placement.get("rack_unit_height_basis"),
        "rack_unit_height_is_estimated": placement.get("rack_unit_height_basis") in ESTIMATED_HEIGHT_BASES,
        "depth_class": placement.get("depth_class"),
        "register_status": placement.get("register_status"),
        "placement_status": placement.get("placement_status"),
        "data_status": placement.get("data_status"),
        "power": {
            "feed": feed,
            "feed_basis": placement.get("power_feed_basis"),
            "feed_is_dual_claim": feed == "A_and_B",
            "power_source_asset_id": placement.get("power_source_asset_id"),
            "pdu_asset_id": pdu_asset_id,
            "pdu_is_modelled": bool(pdu_entry),
            "pdu_feed": pdu_entry.get("power_feed") if pdu_entry else None,
            "pdu_outlet": placement.get("pdu_outlet"),
            "pdu_outlet_basis": placement.get("pdu_outlet_basis"),
            "estimated_power_w": placement.get("estimated_power_w"),
            "power_data_status": placement.get("power_data_status"),
            "heat_load_status": placement.get("heat_load_status"),
        },
        "network": {
            "switch_asset_id": placement.get("switch_asset_id"),
            "switch_port": placement.get("switch_port"),
            "switch_port_basis": placement.get("switch_port_basis"),
            "additional_switch_connections": list(placement.get("additional_switch_connections") or []),
            "vlan_ids": list(placement.get("vlan_ids") or []),
            "vlan_asset_ids": list(placement.get("vlan_asset_ids") or []),
            "vlan_basis": placement.get("vlan_basis"),
        },
        "health": live.health(asset_id),
        "alarms": live.alarm_block(asset_id),
        "open_fields": list(placement.get("open_fields") or []),
        "notes": list(placement.get("notes") or []),
    }


def _unit_grid(unit_count: int, devices: list[dict[str, Any]]) -> tuple[list[dict], list[dict], list[dict]]:
    """Every unit 1..N, occupied or explicitly free, plus contiguous free blocks.

    Overlap is detected here rather than assumed. The document claims "No device
    is placed in a rack unit that another device already claims"; a claim that is
    only ever asserted is not a check.
    """
    claims: dict[int, list[dict]] = defaultdict(list)
    out_of_range: list[dict[str, Any]] = []

    for device in devices:
        start = device["rack_unit_start"]
        end = device["rack_unit_end"]
        if start is None or end is None:
            continue
        if start < 1 or end > unit_count:
            out_of_range.append(
                {"asset_id": device["asset_id"], "rack_unit_start": start, "rack_unit_end": end}
            )
        for unit in range(max(1, start), min(unit_count, end) + 1):
            claims[unit].append(device)

    units: list[dict[str, Any]] = []
    for unit in range(1, unit_count + 1):
        occupants = claims.get(unit, [])
        primary = occupants[0] if occupants else None
        units.append(
            {
                "unit": unit,
                "state": "occupied" if occupants else "free",
                "asset_id": primary["asset_id"] if primary else None,
                "device_name": primary["name"] if primary else None,
                "category": primary["category"] if primary else None,
                "is_device_base": bool(primary and primary["rack_unit_start"] == unit),
                "unit_index_in_device": (unit - primary["rack_unit_start"] + 1) if primary else None,
                "conflicting_asset_ids": [d["asset_id"] for d in occupants[1:]],
            }
        )

    free_blocks: list[dict[str, Any]] = []
    run: list[int] = []
    for unit in units:
        if unit["state"] == "free":
            run.append(unit["unit"])
            continue
        if run:
            free_blocks.append({"start": run[0], "end": run[-1], "size": len(run)})
            run = []
    if run:
        free_blocks.append({"start": run[0], "end": run[-1], "size": len(run)})
    free_blocks.sort(key=lambda block: (-block["size"], block["start"]))
    for index, block in enumerate(free_blocks):
        block["largest"] = index == 0
        block["label"] = (
            f"U{block['start']} free"
            if block["size"] == 1
            else f"U{block['start']}–U{block['end']} free ({block['size']}U contiguous)"
        )

    return units, free_blocks, out_of_range


# ---------------------------------------------------------------------------
# Power, network, thermal
# ---------------------------------------------------------------------------


def _power_block(document: dict[str, Any], live: LiveState, devices: list[dict]) -> dict[str, Any]:
    feeds_raw = list(document.get("power_feeds") or [])
    pdus_raw = list(document.get("pdus") or [])
    device_by_id = {d["asset_id"]: d for d in devices}

    feeds = []
    for feed in feeds_raw:
        upstream = feed.get("upstream_asset_id")
        feeds.append(
            {
                "feed_id": feed.get("feed_id"),
                "upstream_asset_id": upstream,
                "upstream_asset": live.asset_brief(upstream),
                "description": feed.get("description"),
                "data_status": feed.get("data_status"),
                # The single fact that decides whether "dual feed" means anything.
                "resolved": bool(upstream),
                "open_fields": list(feed.get("open_fields") or []),
            }
        )

    pdus = []
    for pdu in pdus_raw:
        pdu_id = pdu.get("pdu_asset_id")
        outlets = []
        for outlet in pdu.get("outlets") or []:
            assigned = outlet.get("assigned_asset_id")
            outlets.append(
                {
                    "outlet": outlet.get("outlet"),
                    "assigned_asset_id": assigned,
                    "assigned_name": (device_by_id.get(assigned) or {}).get("name")
                    or (live.asset_brief(assigned) or {}).get("name"),
                    "purpose": outlet.get("purpose"),
                    "status": outlet.get("status"),
                }
            )
        declared = pdu.get("outlet_count")
        pdus.append(
            {
                "pdu_asset_id": pdu_id,
                "asset": live.asset_brief(pdu_id),
                "outlet_count": declared,
                "outlet_count_source": pdu.get("outlet_count_source"),
                "outlets_modelled": len(outlets),
                "nominal_voltage_v": pdu.get("nominal_voltage_v"),
                "power_feed": pdu.get("power_feed"),
                "upstream_asset_id": pdu.get("upstream_asset_id"),
                "upstream_basis": pdu.get("upstream_basis"),
                "outlets": outlets,
                "used_outlets": sum(1 for o in outlets if o["assigned_asset_id"]),
                "spare_outlets": sum(1 for o in outlets if not o["assigned_asset_id"]),
                "health": live.health(pdu_id or ""),
                "open_fields": list(pdu.get("open_fields") or []),
                "notes": list(pdu.get("notes") or []),
            }
        )

    return {"feeds": feeds, "pdus": pdus}


def _switch_port_block(document: dict[str, Any], live: LiveState, devices: list[dict]) -> dict[str, Any]:
    plan = dict(document.get("switch_port_plan") or {})
    ports: dict[str, list[dict]] = defaultdict(list)

    for device in devices:
        network = device["network"]
        if network["switch_asset_id"] and network["switch_port"] is not None:
            ports[network["switch_asset_id"]].append(
                {
                    "port": network["switch_port"],
                    "asset_id": device["asset_id"],
                    "name": device["name"],
                    "vlan_ids": network["vlan_ids"],
                    "basis": network["switch_port_basis"],
                    "purpose": "primary",
                }
            )
        for extra in network["additional_switch_connections"]:
            ports[extra.get("switch_asset_id") or ""].append(
                {
                    "port": extra.get("switch_port"),
                    "asset_id": device["asset_id"],
                    "name": device["name"],
                    "vlan_ids": list(extra.get("vlan_ids") or []),
                    "basis": extra.get("status"),
                    "purpose": extra.get("purpose") or "additional",
                }
            )

    switches = []
    for switch_id in (plan.get("access_switch_asset_id"), plan.get("backbone_switch_asset_id")):
        if not switch_id:
            continue
        assigned = sorted(ports.pop(switch_id, []), key=lambda row: (row["port"] is None, row["port"]))
        seen: dict[Any, list[str]] = defaultdict(list)
        for row in assigned:
            seen[row["port"]].append(row["asset_id"])
        switches.append(
            {
                "switch_asset_id": switch_id,
                "asset": live.asset_brief(switch_id),
                "role": "access" if switch_id == plan.get("access_switch_asset_id") else "backbone",
                "assignments": assigned,
                "assigned_count": len(assigned),
                "duplicate_ports": {str(k): v for k, v in seen.items() if len(v) > 1},
            }
        )
    # Any switch referenced by a device but not named in the plan still has to
    # appear; silently dropping a patched port would be worse than an odd row.
    for switch_id, assigned in ports.items():
        if not switch_id:
            continue
        switches.append(
            {
                "switch_asset_id": switch_id,
                "asset": live.asset_brief(switch_id),
                "role": "not_named_in_plan",
                "assignments": sorted(assigned, key=lambda row: (row["port"] is None, row["port"])),
                "assigned_count": len(assigned),
                "duplicate_ports": {},
            }
        )

    return {
        "access_switch_asset_id": plan.get("access_switch_asset_id"),
        "backbone_switch_asset_id": plan.get("backbone_switch_asset_id"),
        "numbering_basis": plan.get("numbering_basis"),
        "interface_naming_status": plan.get("interface_naming_status"),
        "port_numbers_are_interface_names": False,
        "open_fields": list(plan.get("open_fields") or []),
        "notes": list(plan.get("notes") or []),
        "switches": switches,
    }


def _thermal_block(document: dict[str, Any]) -> dict[str, Any]:
    thermal = dict(document.get("thermal_summary") or {})
    return {
        "total_estimated_power_w": thermal.get("total_estimated_power_w"),
        "power_data_status": thermal.get("power_data_status"),
        "heat_load_status": thermal.get("heat_load_status"),
        "basis": thermal.get("basis"),
        "open_fields": list(thermal.get("open_fields") or []),
        "required_before_use": list(thermal.get("required_before_use") or []),
        # No wattage is invented, so nothing downstream should render a total.
        "available": thermal.get("total_estimated_power_w") is not None,
    }


# ---------------------------------------------------------------------------
# Findings: where the document disagrees with itself or with the register
# ---------------------------------------------------------------------------


def _findings(
    document: dict[str, Any],
    devices: list[dict],
    zero_u: list[dict],
    units: list[dict],
    free_blocks: list[dict],
    out_of_range: list[dict],
    power: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    allocation = dict(document.get("rack_unit_allocation") or {})

    # -- 0. A placement that does not fit the rack ---------------------------
    if out_of_range:
        findings.append(
            _finding(
                "RACK-OUT-OF-RANGE",
                "blocking",
                "A device is placed outside the rack",
                "The placement claims units beyond the declared rack_unit_count, so part of it "
                "cannot be drawn and part of the elevation is unaccounted for.",
                assets=[row["asset_id"] for row in out_of_range],
            )
        )

    # -- 1. Overlap, checked rather than trusted -----------------------------
    overlaps = [u for u in units if u["conflicting_asset_ids"]]
    if overlaps:
        findings.append(
            _finding(
                "RACK-OVERLAP",
                "blocking",
                "Two devices claim the same rack unit",
                "The document states that no device is placed in a unit another device claims. "
                "Re-deriving the grid from the placements contradicts that.",
                units=[u["unit"] for u in overlaps],
                assets=sorted(
                    {a for u in overlaps for a in [u["asset_id"], *u["conflicting_asset_ids"]] if a}
                ),
            )
        )

    # -- 2. Declared allocation vs re-derived allocation ---------------------
    declared_occupied = set(allocation.get("occupied_rack_units") or [])
    declared_free = set(allocation.get("free_rack_units") or [])
    computed_occupied = {u["unit"] for u in units if u["state"] == "occupied"}
    computed_free = {u["unit"] for u in units if u["state"] == "free"}
    if declared_occupied and declared_occupied != computed_occupied:
        findings.append(
            _finding(
                "RACK-ALLOCATION-DRIFT",
                "blocking",
                "The declared occupied units do not match the placements",
                "rack_unit_allocation.occupied_rack_units has drifted from the placement list. "
                "The grid drawn here is derived from the placements; the summary is stale.",
                units=sorted(declared_occupied.symmetric_difference(computed_occupied)),
            )
        )
    if declared_free and declared_free != computed_free:
        findings.append(
            _finding(
                "RACK-FREE-DRIFT",
                "warning",
                "The declared free units do not match the placements",
                "rack_unit_allocation.free_rack_units disagrees with the re-derived grid.",
                units=sorted(declared_free.symmetric_difference(computed_free)),
            )
        )

    # -- 3. Feed B, and what that does to every dual-feed claim --------------
    unresolved_feeds = [f for f in power["feeds"] if not f["resolved"]]
    for feed in unresolved_feeds:
        findings.append(
            _finding(
                "RACK-FEED-UNRESOLVED",
                "blocking",
                f"Power feed {feed['feed_id']} has no upstream asset",
                "The feed resolves to nothing in the register, so it cannot carry load. Every "
                f"assignment to feed {feed['feed_id']} below is aspirational, and the rack ATS "
                "provides no real redundancy until a second source exists.",
                feed=feed["feed_id"],
                open_fields=feed["open_fields"],
            )
        )

    unresolved_ids = {f["feed_id"] for f in unresolved_feeds}
    dual_claims = [d for d in devices + zero_u if d["power"]["feed_is_dual_claim"]]
    if dual_claims and unresolved_ids:
        findings.append(
            _finding(
                "RACK-DUAL-FEED-UNREALISABLE",
                "blocking",
                "Devices claim A and B while a feed is unresolved",
                f"{len(dual_claims)} devices record power_feed A_and_B, but feed(s) "
                f"{', '.join(sorted(unresolved_ids))} resolve to nothing. Every one of them is "
                "effectively single-fed today.",
                assets=[d["asset_id"] for d in dual_claims],
            )
        )

    # -- 4. Redundant cords that land on one PDU on one feed -----------------
    pdu_feed = {p["pdu_asset_id"]: p["power_feed"] for p in power["pdus"]}
    single_pdu_dual: list[str] = []
    for device in dual_claims:
        pdu_id = device["power"]["pdu_asset_id"]
        if pdu_id and pdu_feed.get(pdu_id) and pdu_feed[pdu_id] != "A_and_B":
            single_pdu_dual.append(device["asset_id"])
    if single_pdu_dual:
        findings.append(
            _finding(
                "RACK-CORDS-SHARE-A-PDU",
                "warning",
                "Both cords of a dual-fed device land on one PDU",
                "These devices claim A and B, but their primary and reserved redundant outlets "
                "are on a single PDU that is itself recorded on one feed. A PDU failure takes "
                "both cords, so the redundancy is nominal even once feed B exists.",
                assets=sorted(single_pdu_dual),
            )
        )

    # -- 5. A PDU in the rack that has no outlet model -----------------------
    modelled = {p["pdu_asset_id"] for p in power["pdus"]}
    placed_pdus = {d["asset_id"] for d in devices if d["asset_class"] == "pdu"}
    unmodelled = sorted(placed_pdus - modelled)
    if unmodelled:
        findings.append(
            _finding(
                "RACK-PDU-NOT-MODELLED",
                "warning",
                "A PDU occupies rack units but has no outlet model",
                "This PDU is placed in the elevation but does not appear in the document's pdus "
                "list, so it has no outlets, no feed and no load. It consumes rack units without "
                "distributing anything.",
                assets=unmodelled,
            )
        )

    # -- 6. pdu_asset_id that is not a PDU -----------------------------------
    misdirected = sorted(
        {
            d["asset_id"]
            for d in devices + zero_u
            if d["power"]["pdu_asset_id"] and not d["power"]["pdu_is_modelled"]
        }
    )
    if misdirected:
        findings.append(
            _finding(
                "RACK-OUTLET-NOT-ON-A-PDU",
                "info",
                "Power source recorded as a device that is not a modelled PDU",
                "These devices record a pdu_asset_id that is not in the pdus list -- in this "
                "package it is the rack ATS, whose outlet count is unknown. Their outlet number "
                "cannot be assigned, so the patching schedule for them is incomplete by "
                "construction, not by omission.",
                assets=misdirected,
            )
        )

    # -- 7. Rack units resting on a guessed height ---------------------------
    estimated = [d for d in devices if d["rack_unit_height_is_estimated"]]
    if estimated:
        estimated_units = sum(d["rack_unit_height"] for d in estimated)
        findings.append(
            _finding(
                "RACK-HEIGHT-ESTIMATED",
                "warning",
                f"{estimated_units}U of the elevation rests on estimated heights",
                f"{len(estimated)} devices have rack_unit_height_basis "
                "'estimated_pending_model_confirmation'. If a guess is wrong, every device above "
                "it moves and the free block changes size.",
                assets=[d["asset_id"] for d in estimated],
                units_at_risk=estimated_units,
            )
        )

    # -- 8. No wattage anywhere ----------------------------------------------
    if all(d["power"]["estimated_power_w"] is None for d in devices + zero_u):
        findings.append(
            _finding(
                "RACK-NO-POWER-DATA",
                "info",
                "No device carries a power figure",
                "Neither measured nor nameplate wattage exists for any device, so rack load and "
                "heat cannot be computed and none is shown. This is the document refusing to "
                "invent numbers, not a gap in this endpoint.",
            )
        )

    # -- 9. Devices that cannot report being unreachable ---------------------
    silent_capable = [
        d
        for d in devices + zero_u
        if d["health"]["point_count"] and d["health"]["availability_point"] is None
    ]
    if silent_capable:
        findings.append(
            _finding(
                "RACK-NO-AVAILABILITY-POINT",
                "warning",
                "Some devices cannot report that they have gone silent",
                "These assets carry points but no availability_state point, so a device that "
                "stops communicating is indistinguishable from one reporting nothing wrong. "
                "This is docs/integration-findings.md F-001 seen from the rack.",
                assets=[d["asset_id"] for d in silent_capable],
            )
        )

    # -- 10. Free block, reported as the design intent it is -----------------
    declared_block = list(allocation.get("contiguous_free_block") or [])
    if declared_block and free_blocks:
        largest = free_blocks[0]
        if [largest["start"], largest["end"]] != declared_block:
            findings.append(
                _finding(
                    "RACK-FREE-BLOCK-DRIFT",
                    "warning",
                    "The declared contiguous free block is not the largest free run",
                    f"The document declares U{declared_block[0]}–U{declared_block[-1]}; the "
                    f"re-derived largest run is U{largest['start']}–U{largest['end']}.",
                )
            )

    findings.sort(key=lambda f: (FINDING_SEVERITIES.index(f["severity"]), f["code"]))
    return findings


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/rack",
    summary="Rack elevation proposal joined to live state",
    description=(
        "The full U-grid for the primary rack -- every unit occupied or explicitly free -- "
        "joined to each device's register entry, telemetry availability and active alarms, "
        "plus power feeds, PDU outlets, the switch-port plan, thermal status and the zero-U "
        "devices that consume no rack units. The source document is a PROPOSAL that has not "
        "been ratified; that status is carried in `document` and must be shown, not footnoted."
    ),
)
def get_rack(session: DbSession, settings: AppSettings) -> dict[str, Any]:
    document = load_layout(settings.data_dir)

    placements = [p for p in (document.get("placements") or []) if isinstance(p, dict)]
    asset_ids = [p["asset_id"] for p in placements if p.get("asset_id")]
    for pdu in document.get("pdus") or []:
        if pdu.get("pdu_asset_id"):
            asset_ids.append(pdu["pdu_asset_id"])
    for feed in document.get("power_feeds") or []:
        if feed.get("upstream_asset_id"):
            asset_ids.append(feed["upstream_asset_id"])
    for excluded in document.get("excluded_assets") or []:
        if excluded.get("asset_id"):
            asset_ids.append(excluded["asset_id"])
    rack_asset_id = (document.get("rack") or {}).get("rack_asset_id")
    if rack_asset_id:
        asset_ids.append(rack_asset_id)

    live = LiveState(session, settings, sorted(set(asset_ids)))

    pdu_index = {p.get("pdu_asset_id"): p for p in (document.get("pdus") or []) if p.get("pdu_asset_id")}
    all_placements = [_device(p, live, pdu_index) for p in placements]
    devices = [d for d in all_placements if d["consumes_rack_units"]]
    zero_u = [d for d in all_placements if not d["consumes_rack_units"]]
    devices.sort(key=lambda d: d["rack_unit_start"])
    zero_u.sort(key=lambda d: (d["asset_class"] or "", d["asset_id"]))

    provenance = _document_block(document)
    rack = _rack_block(document, live)
    unit_count = rack["rack_unit_count"] or 0
    units, free_blocks, out_of_range = _unit_grid(unit_count, devices)
    power = _power_block(document, live, all_placements)
    switch_ports = _switch_port_block(document, live, all_placements)
    thermal = _thermal_block(document)
    findings = _findings(document, devices, zero_u, units, free_blocks, out_of_range, power)

    excluded = [
        {
            "asset_id": entry.get("asset_id"),
            "reason": entry.get("reason"),
            "asset": live.asset_brief(entry.get("asset_id")),
        }
        for entry in (document.get("excluded_assets") or [])
    ]

    health_counts: dict[str, int] = defaultdict(int)
    for device in all_placements:
        health_counts[device["health"]["status"]] += 1

    occupied_units = sum(1 for u in units if u["state"] == "occupied")
    free_units = unit_count - occupied_units

    return {
        "generated_at": _iso(live.now),
        "status_vocabulary": STATUS_EXPLANATIONS,
        "category_labels": CATEGORY_LABELS,
        "data_sources": {
            "layout_document": str(Path(settings.data_dir) / LAYOUT_FILENAME),
            "unavailable": live.problems,
            "degraded": live.degraded,
            "note": (
                None
                if not live.degraded
                else "Some tables could not be read, so live status below is incomplete. The "
                "layout itself is read from disk and is unaffected."
            ),
        },
        "document": provenance,
        "rack": rack,
        "units": units,
        "devices": devices,
        "zero_u_devices": zero_u,
        "free_blocks": free_blocks,
        "power": power,
        "switch_port_plan": switch_ports,
        "thermal": thermal,
        "excluded_assets": excluded,
        "findings": findings,
        "summary": {
            "rack_unit_count": unit_count,
            "units_accounted_for": len(units),
            "occupied_units": occupied_units,
            "free_units": free_units,
            "device_count": len(devices),
            "zero_u_device_count": len(zero_u),
            "excluded_asset_count": len(excluded),
            "largest_free_block": free_blocks[0] if free_blocks else None,
            "health_counts": dict(sorted(health_counts.items())),
            "reporting_device_count": health_counts.get(STATUS_OK, 0),
            "findings_blocking": sum(1 for f in findings if f["severity"] == "blocking"),
            "findings_warning": sum(1 for f in findings if f["severity"] == "warning"),
            "open_field_total": sum(len(d["open_fields"]) for d in all_placements)
            + len(rack["open_fields"])
            + len(provenance["open_document_fields"]),
        },
    }


__all__ = ["CATEGORIES", "CATEGORY_LABELS", "get_rack", "load_layout", "router"]
