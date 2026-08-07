"""Commissioning sequence tracking (SDD section 19).

No subsystem may be placed under automatic supervisory control until its local
control and failure modes have been tested. This module turns that rule into
data the platform can enforce, rather than a paragraph in a document.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from homestead_twin.models.maintenance import CommissioningRecord
from homestead_twin.models.registry import PointBinding

#: The twelve-step sequence, in the order SDD section 19 requires.
COMMISSIONING_STEPS: tuple[tuple[int, str], ...] = (
    (1, "bench_test"),
    (2, "point_to_point_verification"),
    (3, "sensor_calibration"),
    (4, "manual_control_test"),
    (5, "local_automatic_control_test"),
    (6, "communications_loss_test"),
    (7, "sensor_failure_test"),
    (8, "power_loss_and_restoration_test"),
    (9, "supervisory_control_test"),
    (10, "alarm_and_notification_test"),
    (11, "manual_override_test"),
    (12, "documentation_and_baseline_capture"),
)

STEP_NAMES = {number: name for number, name in COMMISSIONING_STEPS}

#: Steps that must pass before supervisory control may be enabled for an asset.
#: Step 9 is the supervisory test itself, so everything up to it is prerequisite.
PREREQUISITE_STEPS = tuple(number for number, _ in COMMISSIONING_STEPS if number <= 8)


def ensure_records(session: Session, asset_id: str) -> list[CommissioningRecord]:
    """Create the twelve not-yet-run records for an asset, idempotently."""
    existing = {
        record.step: record
        for record in session.execute(
            select(CommissioningRecord).where(CommissioningRecord.asset_id == asset_id)
        ).scalars()
    }
    created: list[CommissioningRecord] = []
    for number, name in COMMISSIONING_STEPS:
        if number in existing:
            continue
        record = CommissioningRecord(
            asset_id=asset_id, step=number, step_name=name, result="not_run", evidence=[]
        )
        session.add(record)
        created.append(record)
    session.flush()
    return created


def record_step(
    session: Session,
    asset_id: str,
    step: int,
    result: str,
    performed_by: str,
    now: dt.datetime,
    *,
    preconditions: str | None = None,
    injected_condition: str | None = None,
    expected_sequence: str | None = None,
    observed: str | None = None,
    evidence: list | None = None,
) -> CommissioningRecord:
    """Record the outcome of one commissioning step.

    SDD section 39 requires each test record to carry preconditions, the
    injected condition, the expected sequence, what was actually observed and a
    pass/fail result -- so those are captured here, not just a boolean.
    """
    if step not in STEP_NAMES:
        raise ValueError(f"Unknown commissioning step: {step}")
    if result not in {"pass", "fail", "blocked", "not_run"}:
        raise ValueError(f"Unknown commissioning result: {result}")

    ensure_records(session, asset_id)
    record = session.execute(
        select(CommissioningRecord).where(
            CommissioningRecord.asset_id == asset_id, CommissioningRecord.step == step
        )
    ).scalars().one()

    record.result = result
    record.performed_by = performed_by
    record.performed_at = now
    record.preconditions = preconditions or record.preconditions
    record.injected_condition = injected_condition or record.injected_condition
    record.expected_sequence = expected_sequence or record.expected_sequence
    record.observed = observed or record.observed
    if evidence:
        record.evidence = [*(record.evidence or []), *evidence]
    session.flush()
    return record


def commissioning_status(session: Session, asset_id: str) -> dict:
    """Summarise progress and whether supervisory control may be enabled."""
    records = {
        record.step: record
        for record in session.execute(
            select(CommissioningRecord).where(CommissioningRecord.asset_id == asset_id)
        ).scalars()
    }
    passed = {step for step, record in records.items() if record.result == "pass"}
    failed = {step for step, record in records.items() if record.result == "fail"}
    outstanding = [
        {"step": number, "step_name": name, "result": records.get(number).result if number in records else "not_run"}
        for number, name in COMMISSIONING_STEPS
        if number not in passed
    ]
    prerequisites_met = all(step in passed for step in PREREQUISITE_STEPS)
    return {
        "asset_id": asset_id,
        "steps_total": len(COMMISSIONING_STEPS),
        "steps_passed": len(passed),
        "steps_failed": sorted(failed),
        "outstanding": outstanding,
        "prerequisites_met": prerequisites_met,
        "supervisory_control_permitted": prerequisites_met and 9 in passed,
        "fully_commissioned": len(passed) == len(COMMISSIONING_STEPS),
    }


def may_enable_automatic_control(session: Session, asset_id: str) -> tuple[bool, str]:
    """Gate for enabling a binding's automatic control (SDD sections 19, 47)."""
    status = commissioning_status(session, asset_id)
    if status["steps_failed"]:
        return False, f"commissioning steps failed: {status['steps_failed']}"
    if not status["prerequisites_met"]:
        pending = [item["step"] for item in status["outstanding"] if item["step"] <= 8]
        return False, f"commissioning steps not passed: {pending}"
    if not status["supervisory_control_permitted"]:
        return False, "supervisory control test (step 9) not passed"
    return True, "commissioning prerequisites satisfied"


def commission_binding(
    session: Session, point_id: str, principal_name: str, now: dt.datetime, allow_automatic: bool
) -> PointBinding:
    """Mark a binding commissioned, refusing if the asset is not ready.

    This is the enforcement point for SDD section 47: a binding cannot be
    enabled for automatic control merely because the point dictionary marks the
    point control-capable.
    """
    binding = session.get(PointBinding, point_id)
    if binding is None:
        raise ValueError(f"Unknown point binding: {point_id}")

    if allow_automatic:
        permitted, reason = may_enable_automatic_control(session, binding.asset_id)
        if not permitted:
            raise PermissionError(f"Cannot enable automatic control for {point_id}: {reason}")

    binding.binding_status = "commissioned"
    binding.automatic_control_allowed = allow_automatic
    binding.commissioned_at = now
    binding.commissioned_by = principal_name
    session.flush()
    return binding
