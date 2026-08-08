"""Work generation from maintenance plans (SDD section 18).

Each asset can generate work from a calendar interval, runtime hours, cycle
count, a condition threshold, an alarm occurrence, a seasonal procedure or an
inspection result. The scheduler is deliberately pure: every entry point takes
``now`` so seasonal and interval logic is testable without waiting a year.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from chaos.models.alarms import Alarm
from chaos.models.maintenance import Inspection, MaintenancePlan, SparePart, WorkOrder
from chaos.models.registry import Asset
from chaos.models.telemetry import CurrentState

#: Trigger types understood by the scheduler, in SDD section 18 order.
TRIGGER_TYPES = (
    "calendar",
    "runtime_hours",
    "cycle_count",
    "condition",
    "alarm",
    "seasonal",
    "inspection",
)

#: Northern-hemisphere season windows keyed by month, used by seasonal plans.
_SEASON_MONTHS = {
    "spring": {3, 4, 5},
    "summer": {6, 7, 8},
    "autumn": {9, 10, 11},
    "winter": {12, 1, 2},
}

_OPERATORS = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
}


class MaintenanceError(RuntimeError):
    pass


@dataclass
class ScheduleResult:
    created: list[WorkOrder] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (plan_id, reason)

    @property
    def created_count(self) -> int:
        return len(self.created)


def _as_aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite round-trips datetimes without tzinfo; normalise to UTC-aware."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def _numeric_current_value(session: Session, point_id: str) -> tuple[float | None, str]:
    state = session.get(CurrentState, point_id)
    if state is None:
        return None, "missing"
    if state.value_numeric is None:
        return None, "non_numeric"
    return state.value_numeric, state.quality


def plan_is_due(session: Session, plan: MaintenancePlan, now: dt.datetime) -> tuple[bool, str]:
    """Decide whether a plan should raise work, and explain why."""
    if not plan.enabled:
        return False, "plan_disabled"

    last = _as_aware(plan.last_completed_at)
    next_due = _as_aware(plan.next_due_at)

    if plan.trigger_type == "calendar":
        if plan.interval_days is None:
            return False, "missing_interval_days"
        due = next_due or ((last + dt.timedelta(days=plan.interval_days)) if last else now)
        return (now >= due, "calendar_due" if now >= due else "not_yet_due")

    if plan.trigger_type in {"runtime_hours", "cycle_count"}:
        if not plan.condition_point:
            return False, "missing_counter_point"
        value, quality = _numeric_current_value(session, plan.condition_point)
        if value is None:
            return False, f"counter_unavailable:{quality}"
        if quality in {"bad", "stale"}:
            # A frozen counter must not be read as "no wear accumulated".
            return False, f"counter_quality:{quality}"
        interval = plan.interval_runtime_h if plan.trigger_type == "runtime_hours" else plan.interval_cycles
        if not interval:
            return False, "missing_counter_interval"
        elapsed = value - (plan.counter_baseline or 0.0)
        return (elapsed >= interval, "counter_due" if elapsed >= interval else "not_yet_due")

    if plan.trigger_type == "condition":
        if not (plan.condition_point and plan.condition_operator):
            return False, "missing_condition"
        value, quality = _numeric_current_value(session, plan.condition_point)
        if value is None:
            return False, f"condition_unavailable:{quality}"
        if quality in {"bad", "stale"}:
            return False, f"condition_quality:{quality}"
        op = _OPERATORS.get(plan.condition_operator)
        if op is None:
            return False, f"unknown_operator:{plan.condition_operator}"
        met = op(value, plan.condition_value)
        return met, "condition_met" if met else "condition_not_met"

    if plan.trigger_type == "seasonal":
        months = _SEASON_MONTHS.get((plan.season or "").lower())
        if not months:
            return False, "unknown_season"
        if now.month not in months:
            return False, "out_of_season"
        # Once per season: suppress if already completed within this season window.
        if last and last.year == now.year and last.month in months:
            return False, "already_done_this_season"
        return True, "season_started"

    if plan.trigger_type in {"alarm", "inspection"}:
        # Event-driven: raised by raise_for_alarm / raise_for_inspection, not polled.
        return False, "event_driven"

    return False, f"unknown_trigger:{plan.trigger_type}"


def _has_open_work(session: Session, plan_id: str) -> bool:
    stmt = select(WorkOrder.id).where(
        WorkOrder.plan_id == plan_id, WorkOrder.state.in_(("open", "in_progress", "blocked"))
    )
    return session.execute(stmt).first() is not None


def generate_work_orders(
    session: Session, now: dt.datetime, plan_ids: list[str] | None = None
) -> ScheduleResult:
    """Create work orders for every due plan. Idempotent while work stays open."""
    result = ScheduleResult()
    stmt = select(MaintenancePlan)
    if plan_ids:
        stmt = stmt.where(MaintenancePlan.id.in_(plan_ids))

    for plan in session.execute(stmt).scalars():
        due, reason = plan_is_due(session, plan, now)
        if not due:
            result.skipped.append((plan.id, reason))
            continue
        if _has_open_work(session, plan.id):
            # Never stack duplicate work for the same plan.
            result.skipped.append((plan.id, "work_already_open"))
            continue

        asset = session.get(Asset, plan.asset_id)
        order = WorkOrder(
            asset_id=plan.asset_id,
            plan_id=plan.id,
            title=f"{plan.name} — {asset.name if asset else plan.asset_id}",
            description=plan.procedure,
            priority=_priority_for(asset),
            state="open",
            opened_at=now,
            due_at=_as_aware(plan.next_due_at) or now,
            parts_used=[],
        )
        session.add(order)
        result.created.append(order)

    session.flush()
    return result


def _priority_for(asset: Asset | None) -> str:
    if asset is None:
        return "normal"
    return {
        "life_safety": "urgent",
        "critical": "high",
        "important": "normal",
        "discretionary": "low",
    }.get(asset.criticality, "normal")


def raise_for_alarm(session: Session, alarm: Alarm, now: dt.datetime) -> WorkOrder | None:
    """Create corrective work from an alarm occurrence (SDD section 18)."""
    existing = (
        session.execute(
            select(WorkOrder).where(
                WorkOrder.alarm_id == alarm.id, WorkOrder.state.in_(("open", "in_progress", "blocked"))
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return None

    # Priority comes from the alarm's severity here, not the asset's
    # criticality: a critical alarm on a discretionary asset still needs
    # attention now.
    order = WorkOrder(
        asset_id=alarm.asset_id,
        alarm_id=alarm.id,
        title=f"Corrective work: {alarm.alarm_key}",
        description=alarm.message,
        priority="urgent" if alarm.severity in {"critical", "emergency"} else "high",
        state="open",
        opened_at=now,
        parts_used=[],
    )
    session.add(order)
    session.flush()
    return order


def raise_for_inspection(session: Session, inspection: Inspection, now: dt.datetime) -> WorkOrder | None:
    """A failed or advisory inspection generates follow-up work."""
    if inspection.result == "pass":
        return None
    order = WorkOrder(
        asset_id=inspection.asset_id,
        title=f"Follow-up from inspection ({inspection.result})",
        description=inspection.findings,
        priority="high" if inspection.result == "fail" else "normal",
        state="open",
        opened_at=now,
        parts_used=[],
    )
    session.add(order)
    session.flush()
    return order


def complete_work_order(
    session: Session,
    order: WorkOrder,
    now: dt.datetime,
    completed_by: str,
    note: str | None = None,
    parts_used: list[dict] | None = None,
) -> WorkOrder:
    """Close work, advance its plan, and decrement any spares consumed."""
    order.state = "completed"
    order.completed_at = now
    order.assigned_to = order.assigned_to or completed_by
    order.completion_note = note
    if parts_used:
        order.parts_used = parts_used
        _consume_parts(session, parts_used)

    if order.plan_id:
        plan = session.get(MaintenancePlan, order.plan_id)
        if plan is not None:
            plan.last_completed_at = now
            if plan.trigger_type == "calendar" and plan.interval_days:
                plan.next_due_at = now + dt.timedelta(days=plan.interval_days)
            if plan.trigger_type in {"runtime_hours", "cycle_count"} and plan.condition_point:
                value, quality = _numeric_current_value(session, plan.condition_point)
                # Only rebaseline from a trustworthy reading; a bad counter would
                # otherwise push the next service arbitrarily far into the future.
                if value is not None and quality not in {"bad", "stale"}:
                    plan.counter_baseline = value
    session.flush()
    return order


def _consume_parts(session: Session, parts_used: list[dict]) -> None:
    for entry in parts_used:
        part_number = entry.get("part_number")
        quantity = int(entry.get("quantity", 0) or 0)
        if not part_number or quantity <= 0:
            continue
        part = (
            session.execute(select(SparePart).where(SparePart.part_number == part_number)).scalars().first()
        )
        if part is not None:
            part.quantity_on_hand = max(0, part.quantity_on_hand - quantity)


def parts_below_minimum(session: Session) -> list[SparePart]:
    """Spares at or below their minimum stock level (SDD section 18)."""
    stmt = select(SparePart).where(SparePart.quantity_on_hand <= SparePart.minimum_quantity)
    return list(session.execute(stmt).scalars())


def upcoming_work(session: Session, now: dt.datetime, horizon_days: int = 30) -> list[MaintenancePlan]:
    """Calendar plans falling due inside the horizon, for the operator UI."""
    horizon = now + dt.timedelta(days=horizon_days)
    plans = session.execute(select(MaintenancePlan).where(MaintenancePlan.enabled.is_(True))).scalars()
    due: list[MaintenancePlan] = []
    for plan in plans:
        next_due = _as_aware(plan.next_due_at)
        if next_due is not None and next_due <= horizon:
            due.append(plan)
    return sorted(due, key=lambda p: _as_aware(p.next_due_at) or now)
