"""Maintenance, work-order and commissioning endpoints (SDD sections 18, 19, 41)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from chaos.api.deps import DbSession, MaintainerPrincipal, OperatorPrincipal
from chaos.maintenance import commissioning, scheduler
from chaos.models.base import utcnow
from chaos.models.maintenance import (
    Calibration,
    CommissioningRecord,
    Inspection,
    MaintenancePlan,
    SparePart,
    WorkOrder,
)
from chaos.models.registry import Asset

router = APIRouter(tags=["maintenance"])


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class PlanIn(BaseModel):
    asset_id: str
    name: str
    trigger_type: Literal[
        "calendar", "runtime_hours", "cycle_count", "condition", "alarm", "seasonal", "inspection"
    ]
    interval_days: int | None = None
    interval_runtime_h: float | None = None
    interval_cycles: int | None = None
    condition_point: str | None = None
    condition_operator: Literal["lt", "le", "gt", "ge", "eq", "ne"] | None = None
    condition_value: float | None = None
    season: str | None = None
    procedure: str | None = None
    required_parts: list[dict[str, Any]] = Field(default_factory=list)
    estimated_duration_min: int | None = None
    next_due_at: dt.datetime | None = None
    enabled: bool = True


class PlanOut(BaseModel):
    id: str
    asset_id: str
    name: str
    trigger_type: str
    interval_days: int | None
    interval_runtime_h: float | None
    interval_cycles: int | None
    condition_point: str | None
    condition_operator: str | None
    condition_value: float | None
    counter_baseline: float | None
    season: str | None
    procedure: str | None
    required_parts: list
    last_completed_at: dt.datetime | None
    next_due_at: dt.datetime | None
    enabled: bool

    model_config = {"from_attributes": True}


class WorkOrderOut(BaseModel):
    id: str
    asset_id: str | None
    asset_name: str | None = None
    plan_id: str | None
    alarm_id: str | None
    title: str
    description: str | None
    priority: str
    state: str
    opened_at: dt.datetime
    due_at: dt.datetime | None
    assigned_to: str | None
    completed_at: dt.datetime | None
    completion_note: str | None
    parts_used: list

    model_config = {"from_attributes": True}


class WorkOrderIn(BaseModel):
    asset_id: str | None = None
    title: str
    description: str | None = None
    priority: Literal["urgent", "high", "normal", "low"] = "normal"
    due_at: dt.datetime | None = None
    assigned_to: str | None = None


class CompleteIn(BaseModel):
    note: str | None = None
    parts_used: list[dict[str, Any]] = Field(default_factory=list)


class InspectionIn(BaseModel):
    asset_id: str
    result: Literal["pass", "fail", "advisory"]
    findings: str | None = None
    measurements: dict[str, Any] = Field(default_factory=dict)
    work_order_id: str | None = None


class CalibrationIn(BaseModel):
    point_id: str
    method: str | None = None
    reference_standard: str | None = None
    curve: dict[str, Any] = Field(default_factory=dict)
    pre_error: float | None = None
    post_error: float | None = None
    next_due_at: dt.datetime | None = None
    valid: bool = True


class SparePartIn(BaseModel):
    part_number: str
    description: str
    quantity_on_hand: int = 0
    minimum_quantity: int = 0
    location: str | None = None
    linked_assets: list[str] = Field(default_factory=list)
    supplier: str | None = None
    unit_cost: float | None = None


class CommissioningStepIn(BaseModel):
    step: int = Field(ge=1, le=12)
    result: Literal["pass", "fail", "blocked", "not_run"]
    preconditions: str | None = None
    injected_condition: str | None = None
    expected_sequence: str | None = None
    observed: str | None = None
    evidence: list[str] = Field(default_factory=list)


class CommissionBindingIn(BaseModel):
    allow_automatic_control: bool = False
    reason: str


# --------------------------------------------------------------------------
# Maintenance plans
# --------------------------------------------------------------------------


@router.get("/maintenance/plans", response_model=list[PlanOut])
def list_plans(
    session: DbSession,
    asset_id: str | None = None,
    trigger_type: str | None = None,
    enabled: bool | None = None,
) -> list[MaintenancePlan]:
    stmt = select(MaintenancePlan)
    if asset_id:
        stmt = stmt.where(MaintenancePlan.asset_id == asset_id)
    if trigger_type:
        stmt = stmt.where(MaintenancePlan.trigger_type == trigger_type)
    if enabled is not None:
        stmt = stmt.where(MaintenancePlan.enabled.is_(enabled))
    return list(session.execute(stmt).scalars())


@router.post("/maintenance/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(payload: PlanIn, session: DbSession, principal: MaintainerPrincipal) -> MaintenancePlan:
    if session.get(Asset, payload.asset_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown asset: {payload.asset_id}")
    plan = MaintenancePlan(**payload.model_dump())
    session.add(plan)
    session.commit()
    return plan


@router.delete("/maintenance/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(plan_id: str, session: DbSession, principal: MaintainerPrincipal) -> None:
    plan = session.get(MaintenancePlan, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown plan")
    session.delete(plan)
    session.commit()


@router.get("/maintenance/due")
def list_due(session: DbSession, horizon_days: int = 30) -> dict:
    now = utcnow()
    upcoming = scheduler.upcoming_work(session, now, horizon_days)
    due_now = []
    for plan in session.execute(select(MaintenancePlan)).scalars():
        is_due, reason = scheduler.plan_is_due(session, plan, now)
        if is_due:
            due_now.append(
                {"plan_id": plan.id, "asset_id": plan.asset_id, "name": plan.name, "reason": reason}
            )
    return {
        "evaluated_at": now,
        "due_now": due_now,
        "upcoming": [
            {"plan_id": p.id, "asset_id": p.asset_id, "name": p.name, "next_due_at": p.next_due_at}
            for p in upcoming
        ],
    }


@router.post("/maintenance/generate")
def generate(session: DbSession, principal: MaintainerPrincipal) -> dict:
    result = scheduler.generate_work_orders(session, utcnow())
    session.commit()
    return {
        "created": [{"id": o.id, "asset_id": o.asset_id, "title": o.title} for o in result.created],
        "created_count": result.created_count,
        "skipped": [{"plan_id": pid, "reason": reason} for pid, reason in result.skipped],
    }


# --------------------------------------------------------------------------
# Work orders (SDD section 41 surface)
# --------------------------------------------------------------------------


@router.get("/work-orders", response_model=list[WorkOrderOut])
def list_work_orders(
    session: DbSession,
    state: str | None = None,
    asset_id: str | None = None,
    priority: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    stmt = select(WorkOrder).order_by(WorkOrder.opened_at.desc()).limit(limit)
    if state:
        stmt = stmt.where(WorkOrder.state == state)
    if asset_id:
        stmt = stmt.where(WorkOrder.asset_id == asset_id)
    if priority:
        stmt = stmt.where(WorkOrder.priority == priority)
    orders = list(session.execute(stmt).scalars())
    return [_work_order_payload(session, order) for order in orders]


@router.post("/work-orders", response_model=WorkOrderOut, status_code=status.HTTP_201_CREATED)
def create_work_order(payload: WorkOrderIn, session: DbSession, principal: OperatorPrincipal) -> dict:
    if payload.asset_id and session.get(Asset, payload.asset_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown asset: {payload.asset_id}")
    order = WorkOrder(**payload.model_dump(), state="open", opened_at=utcnow(), parts_used=[])
    session.add(order)
    session.commit()
    return _work_order_payload(session, order)


@router.get("/work-orders/{work_order_id}", response_model=WorkOrderOut)
def get_work_order(work_order_id: str, session: DbSession) -> dict:
    order = session.get(WorkOrder, work_order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown work order")
    return _work_order_payload(session, order)


@router.post("/work-orders/{work_order_id}/complete", response_model=WorkOrderOut)
def complete(
    work_order_id: str, payload: CompleteIn, session: DbSession, principal: OperatorPrincipal
) -> dict:
    order = session.get(WorkOrder, work_order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown work order")
    if order.state == "completed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Work order already completed")
    scheduler.complete_work_order(session, order, utcnow(), principal.name, payload.note, payload.parts_used)
    session.commit()
    return _work_order_payload(session, order)


def _work_order_payload(session, order: WorkOrder) -> dict:
    asset = session.get(Asset, order.asset_id) if order.asset_id else None
    data = {column.name: getattr(order, column.name) for column in order.__table__.columns}
    data["asset_name"] = asset.name if asset else None
    return data


# --------------------------------------------------------------------------
# Inspections, calibrations, spares
# --------------------------------------------------------------------------


@router.post("/maintenance/inspections", status_code=status.HTTP_201_CREATED)
def record_inspection(payload: InspectionIn, session: DbSession, principal: OperatorPrincipal) -> dict:
    if session.get(Asset, payload.asset_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown asset: {payload.asset_id}")
    now = utcnow()
    inspection = Inspection(**payload.model_dump(), inspected_at=now, inspector=principal.name)
    session.add(inspection)
    session.flush()
    follow_up = scheduler.raise_for_inspection(session, inspection, now)
    session.commit()
    return {
        "inspection_id": inspection.id,
        "result": inspection.result,
        "follow_up_work_order_id": follow_up.id if follow_up else None,
    }


@router.get("/maintenance/inspections")
def list_inspections(session: DbSession, asset_id: str | None = None, limit: int = 100) -> list[dict]:
    stmt = select(Inspection).order_by(Inspection.inspected_at.desc()).limit(limit)
    if asset_id:
        stmt = stmt.where(Inspection.asset_id == asset_id)
    return [
        {
            "id": i.id,
            "asset_id": i.asset_id,
            "inspected_at": i.inspected_at,
            "inspector": i.inspector,
            "result": i.result,
            "findings": i.findings,
            "measurements": i.measurements,
        }
        for i in session.execute(stmt).scalars()
    ]


@router.post("/maintenance/calibrations", status_code=status.HTTP_201_CREATED)
def record_calibration(payload: CalibrationIn, session: DbSession, principal: MaintainerPrincipal) -> dict:
    calibration = Calibration(**payload.model_dump(), calibrated_at=utcnow(), calibrated_by=principal.name)
    session.add(calibration)
    session.commit()
    return {"calibration_id": calibration.id, "point_id": calibration.point_id}


@router.get("/maintenance/calibrations")
def list_calibrations(session: DbSession, point_id: str | None = None, limit: int = 100) -> list[dict]:
    stmt = select(Calibration).order_by(Calibration.calibrated_at.desc()).limit(limit)
    if point_id:
        stmt = stmt.where(Calibration.point_id == point_id)
    return [
        {
            "id": c.id,
            "point_id": c.point_id,
            "calibrated_at": c.calibrated_at,
            "calibrated_by": c.calibrated_by,
            "method": c.method,
            "pre_error": c.pre_error,
            "post_error": c.post_error,
            "next_due_at": c.next_due_at,
            "valid": c.valid,
        }
        for c in session.execute(stmt).scalars()
    ]


@router.get("/maintenance/spare-parts")
def list_spares(session: DbSession, below_minimum: bool = False) -> list[dict]:
    parts = (
        scheduler.parts_below_minimum(session)
        if below_minimum
        else list(session.execute(select(SparePart)).scalars())
    )
    return [
        {
            "id": p.id,
            "part_number": p.part_number,
            "description": p.description,
            "quantity_on_hand": p.quantity_on_hand,
            "minimum_quantity": p.minimum_quantity,
            "below_minimum": p.quantity_on_hand <= p.minimum_quantity,
            "location": p.location,
            "linked_assets": p.linked_assets,
        }
        for p in parts
    ]


@router.post("/maintenance/spare-parts", status_code=status.HTTP_201_CREATED)
def create_spare(payload: SparePartIn, session: DbSession, principal: MaintainerPrincipal) -> dict:
    part = SparePart(**payload.model_dump())
    session.add(part)
    session.commit()
    return {"id": part.id, "part_number": part.part_number}


# --------------------------------------------------------------------------
# Commissioning (SDD section 19)
# --------------------------------------------------------------------------


@router.get("/commissioning/steps")
def list_steps() -> list[dict]:
    return [{"step": number, "step_name": name} for number, name in commissioning.COMMISSIONING_STEPS]


@router.get("/commissioning/{asset_id:path}/status")
def get_commissioning_status(asset_id: str, session: DbSession) -> dict:
    if session.get(Asset, asset_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown asset: {asset_id}")
    return commissioning.commissioning_status(session, asset_id)


@router.post("/commissioning/{asset_id:path}/steps")
def record_commissioning_step(
    asset_id: str, payload: CommissioningStepIn, session: DbSession, principal: MaintainerPrincipal
) -> dict:
    if session.get(Asset, asset_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown asset: {asset_id}")
    try:
        record = commissioning.record_step(
            session,
            asset_id,
            payload.step,
            payload.result,
            principal.name,
            utcnow(),
            preconditions=payload.preconditions,
            injected_condition=payload.injected_condition,
            expected_sequence=payload.expected_sequence,
            observed=payload.observed,
            evidence=payload.evidence,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    session.commit()
    return {
        "asset_id": asset_id,
        "step": record.step,
        "step_name": record.step_name,
        "result": record.result,
        "status": commissioning.commissioning_status(session, asset_id),
    }


@router.get("/commissioning/{asset_id:path}/records")
def list_commissioning_records(asset_id: str, session: DbSession) -> list[dict]:
    records = session.execute(
        select(CommissioningRecord)
        .where(CommissioningRecord.asset_id == asset_id)
        .order_by(CommissioningRecord.step)
    ).scalars()
    return [
        {
            "step": r.step,
            "step_name": r.step_name,
            "result": r.result,
            "performed_by": r.performed_by,
            "performed_at": r.performed_at,
            "preconditions": r.preconditions,
            "injected_condition": r.injected_condition,
            "expected_sequence": r.expected_sequence,
            "observed": r.observed,
            "evidence": r.evidence,
        }
        for r in records
    ]


@router.post("/commissioning/bindings/{point_id:path}")
def commission_point_binding(
    point_id: str, payload: CommissionBindingIn, session: DbSession, principal: MaintainerPrincipal
) -> dict:
    """Enable a binding, refusing automatic control until commissioning passes."""
    try:
        binding = commissioning.commission_binding(
            session, point_id, principal.name, utcnow(), payload.allow_automatic_control
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return {
        "point_id": binding.point_id,
        "binding_status": binding.binding_status,
        "automatic_control_allowed": binding.automatic_control_allowed,
        "commissioned_by": binding.commissioned_by,
        "commissioned_at": binding.commissioned_at,
    }
