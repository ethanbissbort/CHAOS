"""Annunciator panel state (SDD 14, 17.3).

A hardwired annunciator panel is a fixed grid of engraved windows, one per
alarm condition, in a position that never moves. Operators learn the panel by
muscle memory: the tile in the third row of the energy bay *is* battery reserve,
lit or not. This endpoint serves that model -- every definition gets a tile,
always, whether or not it is currently in alarm.

The consequence of a fixed grid is that **a dark tile is a positive claim**: it
says "this condition is normal". That claim is only true if the tile is capable
of lighting. A definition that is disabled, or whose trigger point does not
exist for its asset, can never light -- and would sit dark and reassuring
forever.

So serviceability is computed per tile and reported as ``out_of_service``
rather than ``normal``. That is the software equivalent of the paper "OUT OF
SERVICE" tag taped over a window on a real panel, and it is what makes the
dark tiles trustworthy. docs/integration-findings.md F-007 is exactly this
case: battery_cell_imbalance is enabled, but its trigger point is not reachable
on the battery bank.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from homestead_twin.alarms.definitions import definition_meta
from homestead_twin.alarms.evaluator import ACTIVE_STATES, OPEN_STATES
from homestead_twin.api.deps import DbSession
from homestead_twin.models.alarms import Alarm, AlarmDefinition
from homestead_twin.models.base import utcnow
from homestead_twin.models.registry import Asset, Point

router = APIRouter(tags=["alarms"])

#: Panel bays, in the order they are hung on the wall. Domains not listed here
#: still get a bay, appended in alphabetical order, so a new domain can never
#: silently vanish from the panel.
BAY_ORDER: tuple[tuple[str, str], ...] = (
    ("energy", "ENERGY AND POWER"),
    ("safety", "SAFETY"),
    ("structure", "CONTAINER AND STRUCTURE"),
    ("it", "SERVER, NETWORK AND COMMS"),
    ("security", "SECURITY"),
    ("water", "WATER"),
    ("agriculture", "AGRICULTURE"),
    ("storage", "STORAGE"),
    ("spa", "SPA"),
    ("platform", "PLATFORM"),
)

#: Tile states, in the annunciator sense rather than the alarm-lifecycle sense.
#: The panel is a lamp, not a database: it shows what the operator must do next.
TILE_STATES: dict[str, str] = {
    "normal": "Condition normal. The tile is dark and that is a trustworthy statement.",
    "alarm": "Abnormal and unacknowledged. Fast flash with horn.",
    "acknowledged": "Abnormal, acknowledged. Steady lit, horn silenced.",
    "ringback": "Returned to normal but not yet reset. Slow flash with ringback tone.",
    "inhibited": (
        "Suppressed by maintenance mode or as the symptom of a correlated incident. "
        "Recorded, deliberately not annunciated."
    ),
    "out_of_service": (
        "This tile cannot light. Disabled, or its trigger point does not exist for its "
        "asset. A dark tile here would be a false assurance."
    ),
}

#: Words dropped when compressing a definition name into an engraved legend.
_LEGEND_STOPWORDS = frozenset({"a", "an", "the", "of", "for", "to", "is", "in", "on", "and", "or"})

_LEGEND_ABBREVIATIONS: tuple[tuple[str, str], ...] = (
    ("STATE OF CHARGE", "SOC"),
    ("TEMPERATURE", "TEMP"),
    ("COMMUNICATIONS", "COMMS"),
    ("COMMUNICATION", "COMMS"),
    ("CONFIGURATION", "CONFIG"),
    ("UNAVAILABLE", "UNAVAIL"),
    ("UNREACHABLE", "UNREACH"),
    ("SECONDARY", "SEC"),
    ("CONTAINER", "CONTNR"),
    ("GENERATOR", "GEN"),
    ("BATTERY", "BATT"),
    ("INVERTER", "INVTR"),
    ("PERCENT", "PCT"),
    ("MAXIMUM", "MAX"),
    ("MINIMUM", "MIN"),
)


#: Hand-cut legends, one per shipped alarm. On a real panel these are engraved
#: by someone who thought about what the operator needs to read at three metres
#: in bad light -- they are not a mechanical transform of a sentence.
#:
#: Auto-generation was tried first and lost the operative word on 18 of the 40:
#: "Source transfer did not complete" became "SOURCE TRANSFER DID NOT", which is
#: worse than no legend, and "Power container fluid detected" lost "DETECTED".
#: The generator below remains as the fallback for definitions added later.
LEGENDS: dict[str, tuple[str, ...]] = {
    "battery_soc_low": ("BATT SOC", "LOW"),
    "battery_reserve_critical": ("BATT RESERVE", "CRITICAL"),
    "battery_temperature_high": ("BATT CELL", "TEMP HIGH"),
    "battery_cell_imbalance": ("BATT CELL", "IMBALANCE"),
    "battery_charge_inhibited": ("BMS CHARGE", "INHIBITED"),
    "battery_discharge_inhibited": ("BMS DISCHRG", "INHIBITED", "LIVE LOAD"),
    "inverter_fault": ("INVERTER", "FAULT"),
    "inverter_overload_risk": ("INVERTER", "OVERLOAD", "RISK"),
    "pv_generation_underperformance": ("PV OUTPUT", "BELOW", "EXPECTED"),
    "generator_start_failed": ("GEN START", "FAILED"),
    "generator_fuel_low": ("GEN FUEL", "LOW"),
    "transfer_failed": ("SOURCE XFER", "FAILED"),
    "load_shed_failed": ("LOAD SHED", "NO EFFECT"),
    "load_restore_failed": ("LOAD DID NOT", "RESTORE"),
    "energy_meter_data_invalid": ("EMS INPUT", "BAD/STALE"),
    "critical_load_growth": ("CRIT BASE", "LOAD HIGH"),
    "ups_runtime_low": ("UPS RUNTIME", "LOW"),
    "power_container_cooling_failed": ("BATT ZONE", "COOLING", "FAILED"),
    "power_container_ac_bus_lost": ("AC BUS", "DE-ENERGIZED"),
    "power_container_water_ingress": ("PWR CONTNR", "FLUID", "DETECTED"),
    "rack_smoke_detected": ("RACK SMOKE", "DETECTED"),
    "server_zone_temperature_high": ("SERVER ZONE", "TEMP HIGH"),
    "secondary_control_node_unreachable": ("SEC CONTROL", "NODE", "UNREACHABLE"),
    "ups_on_battery": ("UPS ON", "BATTERY"),
    "pdu_overload": ("RACK PDU", "OVERLOAD"),
    "rack_ats_source_lost": ("RACK ATS", "PRIMARY", "SOURCE LOST"),
    "core_switch_unreachable": ("CORE SWITCH", "UNREACHABLE"),
    "primary_server_unreachable": ("PRIMARY HOST", "UNREACHABLE"),
    "service_unavailable": ("CORE SERVICE", "UNAVAILABLE"),
    "storage_pool_degraded": ("ARRAY DISK", "FAILED"),
    "storage_capacity_high": ("HOST STORAGE", "HIGH"),
    "network_path_degraded": ("PACKET LOSS", "HIGH"),
    "backup_overdue": ("BACKUP", "OVERDUE"),
    "time_sync_drift": ("HOST CLOCK", "OFFSET HIGH"),
    "rack_door_forced_open": ("RACK DOOR", "FORCED OPEN"),
    "rack_door_open_extended": ("RACK DOOR", "LEFT OPEN"),
    "camera_tamper": ("CAMERA", "TAMPER"),
    "camera_offline": ("CAMERA", "OFFLINE"),
    "alarm_beacon_unavailable": ("ALARM BEACON", "UNAVAILABLE"),
    "sensor_data_invalid": ("SENSOR DATA", "BAD/STALE"),
}


def engrave(name: str, *, key: str | None = None, max_lines: int = 3, width: int = 12) -> list[str]:
    """Return the engraved legend for an alarm.

    Uses the hand-cut legend when one exists. The generated fallback keeps the
    **last** word above all others, because that is where the meaning usually
    sits: "...did not complete", "...detected", "...low". A legend that drops
    its verb is not a shorter legend, it is a wrong one.
    """
    if key and key in LEGENDS:
        return list(LEGENDS[key])
    text = re.sub(r"[^A-Za-z0-9 %/-]", " ", name or "").upper()
    for long, short in _LEGEND_ABBREVIATIONS:
        text = text.replace(long, short)

    words = [w for w in text.split() if w]
    kept = [w for w in words if w.lower() not in _LEGEND_STOPWORDS] or words
    if not kept:
        return ["(UNNAMED)"]

    def wrap(source: list[str]) -> list[str]:
        lines: list[str] = []
        current = ""
        for word in source:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    lines = wrap(kept)
    if len(lines) <= max_lines:
        return lines

    # Too long. Drop qualifiers from the middle rather than the end, so the
    # operative word survives; keep dropping until it fits.
    head, tail = kept[:-1], kept[-1:]
    while head and len(wrap(head + tail)) > max_lines:
        head.pop()
    lines = wrap(head + tail)
    return lines[:max_lines] or [tail[0][:width]]


def _serviceability(
    definition: AlarmDefinition,
    known_points: set[str],
    assets_by_class: dict[str, list[str]],
) -> tuple[bool, str | None]:
    """Can this tile ever light?

    Returns ``(serviceable, reason_if_not)``. The check is deliberately
    conservative: when scope cannot be resolved the tile is reported out of
    service, because an unlit tile that claims "normal" is the failure mode
    this function exists to prevent.
    """
    if not definition.enabled:
        return False, "Definition is disabled."

    point_name = definition.point_name
    if not point_name:
        # Expression-driven alarms have no single trigger point; they are
        # evaluated by the engine and are serviceable by construction.
        if definition.trigger_expression:
            return True, None
        return False, "No trigger point and no trigger expression."

    if definition.asset_id:
        point_id = f"{definition.asset_id}/{point_name}"
        if point_id in known_points:
            return True, None
        return False, (
            f"Trigger point '{point_name}' does not exist for {definition.asset_id}. "
            "The condition cannot be evaluated, so this tile can never light."
        )

    if definition.asset_class:
        candidates = assets_by_class.get(definition.asset_class, [])
        if not candidates:
            return False, (
                f"No asset of class '{definition.asset_class}' is in the register, so this "
                "class-scoped alarm has nothing to watch."
            )
        reachable = [a for a in candidates if f"{a}/{point_name}" in known_points]
        if reachable:
            return True, None
        return False, (
            f"Trigger point '{point_name}' does not exist on any of the "
            f"{len(candidates)} '{definition.asset_class}' assets."
        )

    return False, "Definition has neither an asset nor an asset class to scope it."


def _tile_state(alarm: Alarm | None, serviceable: bool) -> str:
    if not serviceable:
        return "out_of_service"
    if alarm is None:
        return "normal"
    if alarm.suppressed:
        return "inhibited"
    state = alarm.state
    if state in ("cleared", "mitigated"):
        # Condition gone, operator has not closed it out: classic ringback.
        return "ringback"
    if state == "acknowledged":
        return "acknowledged"
    return "alarm"


@router.get("/annunciator", summary="Annunciator panel state (SDD 14, 17.3)")
def annunciator_panel(
    session: DbSession,
    include_pending: Annotated[
        bool,
        Query(description="Light tiles for alarms still inside their on-delay window."),
    ] = False,
) -> dict[str, Any]:
    definitions = list(session.scalars(select(AlarmDefinition).order_by(AlarmDefinition.alarm_key)))

    known_points: set[str] = set(session.scalars(select(Point.point_id)))
    assets_by_class: dict[str, list[str]] = {}
    for asset_id, asset_class in session.execute(select(Asset.asset_id, Asset.asset_class)):
        assets_by_class.setdefault(asset_class, []).append(asset_id)

    states = list(OPEN_STATES) if include_pending else list(ACTIVE_STATES)
    # Ringback needs alarms that have returned to normal but are not yet
    # reviewed, so cleared and mitigated are always in scope.
    states = sorted(set(states) | {"cleared", "mitigated"})
    open_alarms = list(
        session.scalars(select(Alarm).where(Alarm.state.in_(states)).order_by(Alarm.detected_at.desc()))
    )

    # Most recent open alarm wins the tile; the panel is one lamp per condition.
    newest: dict[str, Alarm] = {}
    extra_counts: dict[str, int] = {}
    for alarm in open_alarms:
        if alarm.alarm_key in newest:
            extra_counts[alarm.alarm_key] = extra_counts.get(alarm.alarm_key, 0) + 1
            continue
        newest[alarm.alarm_key] = alarm

    bays: dict[str, dict[str, Any]] = {}
    summary = {
        "total": 0,
        "normal": 0,
        "alarm": 0,
        "acknowledged": 0,
        "ringback": 0,
        "inhibited": 0,
        "out_of_service": 0,
        "horn": False,
        "ringback_tone": False,
    }

    for definition in definitions:
        serviceable, reason = _serviceability(definition, known_points, assets_by_class)
        alarm = newest.get(definition.alarm_key)
        state = _tile_state(alarm, serviceable)
        meta = definition_meta(definition)

        tile = {
            "alarm_key": definition.alarm_key,
            "legend": engrave(definition.name, key=definition.alarm_key),
            "name": definition.name,
            "severity": definition.severity,
            "domain": definition.domain,
            "state": state,
            "serviceable": serviceable,
            "service_note": reason,
            "requires_manual_reset": definition.requires_manual_reset,
            "threshold_status": meta.get("threshold_status"),
            "alarm_id": alarm.id if alarm else None,
            "since": (alarm.activated_at or alarm.detected_at) if alarm else None,
            "message": alarm.message if alarm else None,
            "incident_id": alarm.incident_id if alarm else None,
            "suppression_reason": alarm.suppression_reason if alarm else None,
            "duplicate_open_count": extra_counts.get(definition.alarm_key, 0),
        }

        bay = bays.setdefault(
            definition.domain,
            {"domain": definition.domain, "title": definition.domain.upper(), "tiles": []},
        )
        bay["tiles"].append(tile)

        summary["total"] += 1
        summary[state] += 1

    # Horn sounds for any unacknowledged alarm; ringback tone for any tile
    # waiting to be reset. Both are reported rather than left to the UI to
    # infer, so every client agrees on when the room is making noise.
    summary["horn"] = summary["alarm"] > 0
    summary["ringback_tone"] = summary["ringback"] > 0

    ordered_titles = dict(BAY_ORDER)
    ordered: list[dict[str, Any]] = []
    for domain, title in BAY_ORDER:
        if domain in bays:
            bay = bays.pop(domain)
            bay["title"] = title
            ordered.append(bay)
    for domain in sorted(bays):
        bay = bays.pop(domain)
        bay["title"] = ordered_titles.get(domain, domain.upper())
        ordered.append(bay)

    for bay in ordered:
        # Severity first, then key: the eye should land on the worst thing in
        # each bay, and the position must stay stable between polls.
        bay["tiles"].sort(key=lambda t: (_SEVERITY_ORDER.get(t["severity"], 99), t["alarm_key"]))

    return {
        "generated_at": utcnow(),
        "bays": ordered,
        "summary": summary,
        "tile_states": TILE_STATES,
    }


_SEVERITY_ORDER = {"emergency": 0, "critical": 1, "major": 2, "warning": 3, "info": 4}


__all__ = ["BAY_ORDER", "TILE_STATES", "engrave", "router"]
