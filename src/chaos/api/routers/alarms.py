"""Alarm, incident and notification API (SDD 41, FR-007, FR-008).

``GET /alarms/definitions/{alarm_key}`` is the FR-008 endpoint: every alarm links
to an operating procedure, its affected assets, its dependencies and its manual
controls. Manual controls are read from the affected assets' ``manual_override``
records rather than restated here, so the answer stays true when the register
changes. Where an asset has no documented manual override the response says so
and lists that asset's open fields -- an empty object is reported as
"not documented", never as "no manual control exists".

Write endpoints require the operator role and a mandatory reason, per SDD 41
("Write endpoints require authentication, authorization, an audit reason...").
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from chaos.alarms.correlation import CorrelationEngine
from chaos.alarms.definitions import (
    DefinitionError,
    definition_meta,
    definition_notes,
    sync_definitions,
)
from chaos.alarms.evaluator import (
    ACTIVE_STATES,
    OPEN_STATES,
    AlarmEvaluator,
    AlarmTransitionError,
    derive_reset,
)
from chaos.alarms.notify import notification_detail
from chaos.api.deps import (
    AppSettings,
    Bus,
    DbSession,
    MaintainerPrincipal,
    OperatorPrincipal,
)
from chaos.models.alarms import (
    ALARM_STATES,
    SEVERITIES,
    Alarm,
    AlarmDefinition,
    Incident,
    NotificationLog,
)
from chaos.models.base import utcnow
from chaos.models.registry import Asset, AssetRelationship

router = APIRouter(tags=["alarms"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class LifecycleAction(BaseModel):
    """Mandatory audit note for every lifecycle write (SDD 5.7, 41)."""

    note: str = Field(min_length=3, max_length=2000, description="Why, in the operator's words.")
    force: bool = Field(
        default=False,
        description=(
            "Clear an alarm whose trigger condition is still true. The override is "
            "recorded on the lifecycle event."
        ),
    )

    @field_validator("note")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("note must not be blank")
        return value


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _event_dict(event) -> dict[str, Any]:
    return {
        "from_state": event.from_state,
        "to_state": event.to_state,
        "actor": event.actor,
        "note": event.note,
        "occurred_at": event.occurred_at,
    }


def _alarm_dict(alarm: Alarm, *, include_events: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "alarm_id": alarm.id,
        "alarm_key": alarm.alarm_key,
        "asset_id": alarm.asset_id,
        "point_id": alarm.point_id,
        "severity": alarm.severity,
        "state": alarm.state,
        "message": alarm.message,
        "detected_at": alarm.detected_at,
        "activated_at": alarm.activated_at,
        "acknowledged_at": alarm.acknowledged_at,
        "acknowledged_by": alarm.acknowledged_by,
        "mitigated_at": alarm.mitigated_at,
        "cleared_at": alarm.cleared_at,
        "reviewed_at": alarm.reviewed_at,
        "reviewed_by": alarm.reviewed_by,
        "suppressed": alarm.suppressed,
        "suppression_reason": alarm.suppression_reason,
        "notified": alarm.notified,
        "incident_id": alarm.incident_id,
        "trigger_value": alarm.trigger_value,
    }
    if include_events:
        payload["events"] = [_event_dict(e) for e in alarm.events]
    return payload


def _incident_dict(incident: Incident, members: list[Alarm] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "incident_id": incident.id,
        "title": incident.title,
        "severity": incident.severity,
        "state": incident.state,
        "root_cause_alarm_id": incident.root_cause_alarm_id,
        "opened_at": incident.opened_at,
        "closed_at": incident.closed_at,
        "summary": incident.summary,
    }
    if members is not None:
        payload["member_count"] = len(members)
        payload["alarms"] = [_alarm_dict(a) for a in members]
    return payload


def _definition_summary(definition: AlarmDefinition) -> dict[str, Any]:
    meta = definition_meta(definition)
    reset_operator, reset_value = derive_reset(definition)
    return {
        "alarm_key": definition.alarm_key,
        "name": definition.name,
        "severity": definition.severity,
        "domain": definition.domain,
        "scope": {
            "asset_id": definition.asset_id,
            "asset_class": definition.asset_class,
            "point_name": definition.point_name,
        },
        "trigger": {
            "operator": definition.trigger_operator,
            "value": definition.trigger_value,
            "expression": definition.trigger_expression,
        },
        "reset": {
            "operator": reset_operator,
            "value": reset_value,
            "declared": bool(definition.reset_operator),
            "derived_from_hysteresis": not definition.reset_operator,
        },
        "on_delay_s": definition.on_delay_s,
        "off_delay_s": definition.off_delay_s,
        "hysteresis": definition.hysteresis,
        "requires_manual_reset": definition.requires_manual_reset,
        "maintenance_mode_behaviour": definition.maintenance_mode_behaviour,
        "parent_alarm_key": definition.parent_alarm_key,
        "enabled": definition.enabled,
        "threshold_status": meta.get("threshold_status"),
        "threshold_basis": meta.get("threshold_basis"),
        "source_sections": meta.get("source_sections", []),
    }


def _manual_controls(assets: list[Asset]) -> list[dict[str, Any]]:
    """SDD FR-008 manual controls, read from the register.

    ``manual_override`` is empty for every asset in the v0.3 register, so this
    reports the gap explicitly rather than implying a control exists.
    """
    controls: list[dict[str, Any]] = []
    for asset in assets:
        override = asset.manual_override or {}
        controls.append(
            {
                "asset_id": asset.asset_id,
                "name": asset.name,
                "control_authority": asset.control_authority,
                "manual_override": override,
                "documented": bool(override),
                "status": "documented" if override else "not_documented",
                "open_fields": asset.open_fields or [],
            }
        )
    return controls


def _dependencies(session, assets: list[Asset]) -> dict[str, Any]:
    """Typed registry relationships touching the affected assets (SDD 25.5)."""
    asset_ids = [a.asset_id for a in assets]
    if not asset_ids:
        return {"declared": [], "relationships": []}
    rows = session.scalars(
        select(AssetRelationship).where(
            AssetRelationship.from_asset_id.in_(asset_ids) | AssetRelationship.to_asset_id.in_(asset_ids)
        )
    ).all()
    return {
        "declared": [
            {"asset_id": a.asset_id, "dependencies": a.dependencies or []} for a in assets if a.dependencies
        ],
        "relationships": [
            {
                "from_asset_id": r.from_asset_id,
                "relationship_type": r.relationship_type,
                "to_asset_id": r.to_asset_id,
                "status": r.status,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Alarm reads. Literal paths are declared before /alarms/{alarm_id}.
# ---------------------------------------------------------------------------


@router.get("/alarms/active", summary="Active alarms (SDD section 41)")
def list_active_alarms(
    session: DbSession,
    include_pending: Annotated[
        bool, Query(description="Include 'detected' alarms still inside their on-delay window.")
    ] = False,
    include_suppressed: Annotated[
        bool,
        Query(
            description=(
                "Include alarms whose notification is suppressed (maintenance mode, or a "
                "symptom of a correlated incident). They are always recorded."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    states = list(OPEN_STATES) if include_pending else list(ACTIVE_STATES)
    statement = select(Alarm).where(Alarm.state.in_(states)).order_by(Alarm.detected_at.desc())
    alarms = list(session.scalars(statement).all())
    if not include_suppressed:
        alarms = [a for a in alarms if not a.suppressed]
    by_severity: dict[str, int] = {}
    for alarm in alarms:
        by_severity[alarm.severity] = by_severity.get(alarm.severity, 0) + 1
    return {
        "count": len(alarms),
        "states": states,
        "by_severity": by_severity,
        "suppressed_count": sum(1 for a in alarms if a.suppressed),
        "incident_count": len({a.incident_id for a in alarms if a.incident_id}),
        "alarms": [_alarm_dict(a) for a in alarms],
    }


@router.get("/alarms/definitions", summary="Alarm definition set (SDD 14.3)")
def list_alarm_definitions(
    session: DbSession,
    domain: Annotated[str | None, Query()] = None,
    severity: Annotated[str | None, Query()] = None,
    enabled_only: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    statement = select(AlarmDefinition).order_by(AlarmDefinition.alarm_key)
    if domain:
        statement = statement.where(AlarmDefinition.domain == domain)
    if severity:
        statement = statement.where(AlarmDefinition.severity == severity)
    if enabled_only:
        statement = statement.where(AlarmDefinition.enabled.is_(True))
    definitions = list(session.scalars(statement).all())
    commissioning = [
        d.alarm_key
        for d in definitions
        if definition_meta(d).get("threshold_status") == "commissioning_default"
    ]
    return {
        "count": len(definitions),
        "commissioning_default_count": len(commissioning),
        "commissioning_default_keys": commissioning,
        "definitions": [_definition_summary(d) for d in definitions],
    }


@router.get(
    "/alarms/definitions/{alarm_key}",
    summary="One alarm definition with its SDD FR-008 context",
)
def get_alarm_definition(alarm_key: str, session: DbSession) -> dict[str, Any]:
    definition = session.get(AlarmDefinition, alarm_key)
    if definition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown alarm definition: {alarm_key}")

    meta = definition_meta(definition)
    scoped = [definition.asset_id] if definition.asset_id else []
    affected_ids = list(dict.fromkeys([*scoped, *(definition.affected_assets or [])]))
    affected = (
        list(session.scalars(select(Asset).where(Asset.asset_id.in_(affected_ids))).all())
        if affected_ids
        else []
    )
    if definition.asset_class:
        affected.extend(
            session.scalars(
                select(Asset).where(
                    Asset.asset_class == definition.asset_class,
                    Asset.domain == definition.domain,
                    Asset.asset_id.not_in(affected_ids or [""]),
                )
            ).all()
        )

    parent = (
        session.get(AlarmDefinition, definition.parent_alarm_key) if definition.parent_alarm_key else None
    )
    children = list(
        session.scalars(
            select(AlarmDefinition.alarm_key).where(AlarmDefinition.parent_alarm_key == definition.alarm_key)
        ).all()
    )
    open_count = session.scalar(
        select(Alarm.id).where(Alarm.alarm_key == definition.alarm_key, Alarm.state.in_(OPEN_STATES)).limit(1)
    )

    payload = _definition_summary(definition)
    payload.update(
        {
            # SDD FR-008: procedure, affected assets, dependencies, manual controls.
            "procedure": {
                "reference": definition.procedure_ref,
                "status": meta.get("procedure_status", "not_yet_written"),
            },
            "affected_assets": [
                {
                    "asset_id": a.asset_id,
                    "name": a.name,
                    "domain": a.domain,
                    "asset_class": a.asset_class,
                    "criticality": a.criticality,
                    "status": a.status,
                    "control_authority": a.control_authority,
                }
                for a in affected
            ],
            "dependencies": _dependencies(session, affected),
            "manual_controls": _manual_controls(affected),
            "probable_causes": definition.probable_causes or [],
            "automatic_action": definition.automatic_action,
            "operator_action": definition.operator_action,
            "escalation_path": definition.escalation_path or [],
            "renotify_after_s": meta.get("renotify_after_s", 0),
            "suppression_conditions": definition.suppression_conditions or [],
            "correlation": {
                **(meta.get("correlation") or {}),
                "parent_alarm_key": definition.parent_alarm_key,
                "parent_scope": meta.get("parent_scope"),
                "parent_severity": parent.severity if parent else None,
                "symptom_alarm_keys": sorted(children),
                "data_quality_alarm_key": meta.get("data_quality_alarm_key"),
            },
            "notes": definition_notes(definition),
            "has_open_alarms": open_count is not None,
        }
    )
    return payload


@router.get("/alarms", summary="Alarm history")
def list_alarms(
    session: DbSession,
    state: Annotated[str | None, Query(description=f"One of {ALARM_STATES}")] = None,
    severity: Annotated[str | None, Query(description=f"One of {SEVERITIES}")] = None,
    asset_id: Annotated[str | None, Query()] = None,
    alarm_key: Annotated[str | None, Query()] = None,
    incident_id: Annotated[str | None, Query()] = None,
    since: Annotated[dt.datetime | None, Query(description="Detected at or after this time.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    if state and state not in ALARM_STATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown state: {state}")
    if severity and severity not in SEVERITIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown severity: {severity}")

    statement = select(Alarm).order_by(Alarm.detected_at.desc()).limit(limit)
    if state:
        statement = statement.where(Alarm.state == state)
    if severity:
        statement = statement.where(Alarm.severity == severity)
    if asset_id:
        statement = statement.where(Alarm.asset_id == asset_id)
    if alarm_key:
        statement = statement.where(Alarm.alarm_key == alarm_key)
    if incident_id:
        statement = statement.where(Alarm.incident_id == incident_id)
    if since:
        statement = statement.where(Alarm.detected_at >= since)

    alarms = list(session.scalars(statement).all())
    return {"count": len(alarms), "limit": limit, "alarms": [_alarm_dict(a) for a in alarms]}


@router.get("/alarms/{alarm_id}", summary="One alarm with its lifecycle history")
def get_alarm(alarm_id: str, session: DbSession) -> dict[str, Any]:
    alarm = session.get(Alarm, alarm_id)
    if alarm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown alarm: {alarm_id}")
    payload = _alarm_dict(alarm, include_events=True)
    definition = session.get(AlarmDefinition, alarm.alarm_key)
    if definition is not None:
        meta = definition_meta(definition)
        payload["definition"] = {
            "name": definition.name,
            "procedure_ref": definition.procedure_ref,
            "procedure_status": meta.get("procedure_status"),
            "operator_action": definition.operator_action,
            "automatic_action": definition.automatic_action,
            "probable_causes": definition.probable_causes or [],
            "affected_assets": definition.affected_assets or [],
            "requires_manual_reset": definition.requires_manual_reset,
            "threshold_status": meta.get("threshold_status"),
            "threshold_basis": meta.get("threshold_basis"),
        }
    if alarm.incident_id:
        incident = session.get(Incident, alarm.incident_id)
        if incident is not None:
            payload["incident"] = _incident_dict(incident)
    return payload


# ---------------------------------------------------------------------------
# Lifecycle writes (SDD 14.2). Operator role + mandatory note.
# ---------------------------------------------------------------------------


def _lifecycle(
    session,
    bus,
    settings,
    alarm_id: str,
    action: Literal["acknowledge", "mitigate", "clear", "review"],
    body: LifecycleAction,
    principal,
) -> dict[str, Any]:
    alarm = session.get(Alarm, alarm_id)
    if alarm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown alarm: {alarm_id}")
    evaluator = AlarmEvaluator(session, bus, settings)
    now = utcnow()
    try:
        if action == "acknowledge":
            evaluator.acknowledge(alarm, principal.name, body.note, now)
        elif action == "mitigate":
            evaluator.mitigate(alarm, principal.name, body.note, now)
        elif action == "clear":
            evaluator.clear(alarm, principal.name, body.note, now, force=body.force)
        else:
            evaluator.review(alarm, principal.name, body.note, now)
    except AlarmTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    # Clearing the last open member of an incident closes it.
    if action in ("clear", "review") and alarm.incident_id:
        CorrelationEngine(session, settings).close_incident(alarm.incident_id, now)
    session.commit()
    return _alarm_dict(alarm, include_events=True)


@router.post("/alarms/{alarm_id}/acknowledge", summary="Acknowledge an alarm")
def acknowledge_alarm(
    alarm_id: str,
    body: LifecycleAction,
    session: DbSession,
    bus: Bus,
    settings: AppSettings,
    principal: OperatorPrincipal,
) -> dict[str, Any]:
    return _lifecycle(session, bus, settings, alarm_id, "acknowledge", body, principal)


@router.post("/alarms/{alarm_id}/mitigate", summary="Record mitigating action")
def mitigate_alarm(
    alarm_id: str,
    body: LifecycleAction,
    session: DbSession,
    bus: Bus,
    settings: AppSettings,
    principal: OperatorPrincipal,
) -> dict[str, Any]:
    return _lifecycle(session, bus, settings, alarm_id, "mitigate", body, principal)


@router.post("/alarms/{alarm_id}/clear", summary="Clear an alarm (manual reset)")
def clear_alarm(
    alarm_id: str,
    body: LifecycleAction,
    session: DbSession,
    bus: Bus,
    settings: AppSettings,
    principal: OperatorPrincipal,
) -> dict[str, Any]:
    return _lifecycle(session, bus, settings, alarm_id, "clear", body, principal)


@router.post("/alarms/{alarm_id}/review", summary="Close the lifecycle with a review")
def review_alarm(
    alarm_id: str,
    body: LifecycleAction,
    session: DbSession,
    bus: Bus,
    settings: AppSettings,
    principal: OperatorPrincipal,
) -> dict[str, Any]:
    return _lifecycle(session, bus, settings, alarm_id, "review", body, principal)


@router.post(
    "/alarms/definitions/reload",
    summary="Reload the alarm definition set from the design package",
)
def reload_alarm_definitions(
    session: DbSession,
    settings: AppSettings,
    # Reloading replaces safety-relevant trip thresholds, so this sits at the
    # same level as POST /registry/reload rather than with operator actions.
    principal: MaintainerPrincipal,
    strict: Annotated[
        bool, Query(description="Reject the reload if any point or asset does not resolve.")
    ] = True,
) -> dict[str, Any]:
    try:
        result = sync_definitions(session, settings=settings, strict=strict)
    except DefinitionError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    session.commit()
    return {"reloaded_by": principal.name, **result.as_dict()}


# ---------------------------------------------------------------------------
# Incidents (SDD 14.2)
# ---------------------------------------------------------------------------


@router.get("/incidents", summary="Correlated incidents")
def list_incidents(
    session: DbSession,
    state: Annotated[str | None, Query(description="open | closed")] = None,
    severity: Annotated[str | None, Query()] = None,
    since: Annotated[dt.datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    statement = select(Incident).order_by(Incident.opened_at.desc()).limit(limit)
    if state:
        statement = statement.where(Incident.state == state)
    if severity:
        statement = statement.where(Incident.severity == severity)
    if since:
        statement = statement.where(Incident.opened_at >= since)
    incidents = list(session.scalars(statement).all())

    payload = []
    for incident in incidents:
        members = list(session.scalars(select(Alarm.id).where(Alarm.incident_id == incident.id)).all())
        entry = _incident_dict(incident)
        entry["member_count"] = len(members)
        payload.append(entry)
    return {"count": len(payload), "limit": limit, "incidents": payload}


@router.get("/incidents/{incident_id}", summary="One incident with its member alarms")
def get_incident(incident_id: str, session: DbSession, settings: AppSettings) -> dict[str, Any]:
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown incident: {incident_id}")
    members = CorrelationEngine(session, settings).incident_members(incident_id)
    payload = _incident_dict(incident, members)
    payload["root_cause"] = next(
        (_alarm_dict(a) for a in members if a.id == incident.root_cause_alarm_id), None
    )
    payload["notifications"] = [
        {
            "channel": n.channel,
            "status": n.status,
            "subject": n.subject,
            "sent_at": n.sent_at,
            "detail": notification_detail(n),
        }
        for n in session.scalars(
            select(NotificationLog)
            .where(NotificationLog.incident_id == incident_id)
            .order_by(NotificationLog.sent_at.asc())
        ).all()
    ]
    return payload


# ---------------------------------------------------------------------------
# Notification log (SDD FR-007)
# ---------------------------------------------------------------------------


@router.get("/notifications", summary="Notification delivery log")
def list_notifications(
    session: DbSession,
    channel: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    incident_id: Annotated[str | None, Query()] = None,
    alarm_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    statement = select(NotificationLog).order_by(NotificationLog.sent_at.desc()).limit(limit)
    if channel:
        statement = statement.where(NotificationLog.channel == channel)
    if status_filter:
        statement = statement.where(NotificationLog.status == status_filter)
    if incident_id:
        statement = statement.where(NotificationLog.incident_id == incident_id)
    if alarm_id:
        statement = statement.where(NotificationLog.alarm_id == alarm_id)
    records = list(session.scalars(statement).all())
    return {
        "count": len(records),
        "limit": limit,
        "notifications": [
            {
                "id": n.id,
                "alarm_id": n.alarm_id,
                "incident_id": n.incident_id,
                "channel": n.channel,
                "recipient": n.recipient,
                "subject": n.subject,
                "body": n.body,
                "status": n.status,
                "sent_at": n.sent_at,
                "detail": notification_detail(n),
            }
            for n in records
        ],
    }
