"""Energy API (SDD sections 41 and 38).

``GET`` endpoints are readable by anyone the deployment lets through. Every
write requires a named operator and an audit reason (SDD 41, 5.7), and the two
that change control behaviour -- freezing the state and clearing a latch --
also carry the safety constraints from SDD 30.9 and 30.7:

* a freeze is bounded and never suppresses ``EMERGENCY``;
* a latch is only cleared with a reason *and* an explicit confirmation that the
  originating condition is gone, and the EMS re-enters at a conservative state.

Reads compute inputs and derived values on demand rather than requiring the
background service, so the dashboard still works on a node where the EMS is not
the dispatcher (for example the secondary control node).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from chaos.api.deps import AppSettings, Bus, CurrentPrincipal, DbSession, OperatorPrincipal
from chaos.config import Settings
from chaos.ems.config import DEFAULT_CONFIG, EmsConfig, energy_state_topic
from chaos.ems.derived import DerivedEnergyState, LoadSnapshot, compute_derived
from chaos.ems.inputs import EmsInputs, gather_inputs
from chaos.ems.leases import LeaseManager, summarise_leases
from chaos.ems.loader import effective_tier, load_profiles
from chaos.ems.shedding import current_load_states, recent_actions, summarise_loads
from chaos.ems.state_machine import (
    EnergyStateMachine,
    LatchError,
    ensure_snapshot,
    publish_state,
    recent_transitions,
)
from chaos.models.base import utcnow
from chaos.models.energy import LATCHING_ENERGY_STATES, PowerBudgetLease

router = APIRouter(prefix="/energy", tags=["energy"])


def _config() -> EmsConfig:
    return DEFAULT_CONFIG


def _evaluate(session: Session, now: dt.datetime) -> tuple[EmsInputs, DerivedEnergyState, list]:
    """Read-only evaluation used by the dashboard and the lease endpoints."""
    config = _config()
    profiles = load_profiles(session)
    inputs = gather_inputs(session, now, config, load_asset_ids=[profile.asset_id for profile in profiles])
    derived = compute_derived(
        inputs,
        config,
        now=now,
        loads=[
            LoadSnapshot(
                asset_id=profile.asset_id,
                tier=effective_tier(profile, now),
                estimated_power_kw=profile.estimated_power_kw,
            )
            for profile in profiles
        ],
    )
    return inputs, derived, profiles


def _snapshot_payload(snapshot, settings: Settings) -> dict[str, Any]:
    return {
        "state": snapshot.state,
        "entered_at": snapshot.entered_at,
        "candidate_state": snapshot.candidate_state,
        "candidate_since": snapshot.candidate_since,
        "frozen_until": snapshot.frozen_until,
        "frozen_by": snapshot.frozen_by,
        "data_quality": snapshot.data_quality,
        "shed_groups_active": list(snapshot.shed_groups_active or []),
        "generator_request": snapshot.generator_request,
        "last_evaluated_at": snapshot.last_evaluated_at,
        "latching": snapshot.state in LATCHING_ENERGY_STATES,
        "published_topic": energy_state_topic(settings),
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@router.get("/state", summary="Current site energy state")
def get_state(session: DbSession, settings: AppSettings) -> dict[str, Any]:
    now = utcnow()
    snapshot = ensure_snapshot(session, now=now)
    session.commit()
    payload = _snapshot_payload(snapshot, settings)
    payload["derived"] = snapshot.derived or {}
    payload["inputs"] = snapshot.inputs or {}
    return payload


@router.get("/state/history", summary="Energy state transition history")
def get_state_history(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    since: dt.datetime | None = None,
) -> dict[str, Any]:
    transitions = recent_transitions(session, limit=limit, since=since)
    return {
        "count": len(transitions),
        "transitions": [
            {
                "id": t.id,
                "from_state": t.from_state,
                "to_state": t.to_state,
                "trigger": t.trigger,
                "reason": t.reason,
                "actor": t.actor,
                "occurred_at": t.occurred_at,
                "inputs_snapshot": t.inputs_snapshot,
                "derived_snapshot": t.derived_snapshot,
            }
            for t in transitions
        ],
    }


class FreezeRequest(BaseModel):
    reason: str = Field(min_length=3, description="Audit reason (SDD 41)")
    duration_s: int | None = Field(
        default=None, gt=0, description="Bounded freeze duration; clamped to EmsConfig.freeze_max_s"
    )
    release: bool = Field(default=False, description="Set true to release an existing freeze")


@router.post("/state/freeze", summary="Freeze or release the energy state (operator)")
def freeze_state(
    payload: FreezeRequest,
    session: DbSession,
    settings: AppSettings,
    bus: Bus,
    principal: OperatorPrincipal,
) -> dict[str, Any]:
    now = utcnow()
    machine = EnergyStateMachine(_config(), settings)
    try:
        if payload.release:
            snapshot = machine.unfreeze(session, actor=principal.name, reason=payload.reason, now=now)
        else:
            snapshot = machine.freeze(
                session,
                actor=principal.name,
                reason=payload.reason,
                now=now,
                duration_s=payload.duration_s,
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    session.commit()
    publish_state(bus, settings, snapshot, now=now, reason=payload.reason)
    result = _snapshot_payload(snapshot, settings)
    result["note"] = "A freeze never suppresses EMERGENCY and never overrides equipment-native limits."
    return result


class ClearLatchRequest(BaseModel):
    reason: str = Field(min_length=3)
    condition_clear: bool = Field(
        description="Operator confirmation that the originating condition has been removed"
    )
    to_state: str = Field(
        default="CONSERVE",
        description="Conservative state to re-enter; the EMS earns its way back to NORMAL",
    )


@router.post("/state/clear-latch", summary="Clear a latching energy state (operator)")
def clear_latch(
    payload: ClearLatchRequest,
    session: DbSession,
    settings: AppSettings,
    bus: Bus,
    principal: OperatorPrincipal,
) -> dict[str, Any]:
    now = utcnow()
    machine = EnergyStateMachine(_config(), settings)
    try:
        snapshot = machine.clear_latch(
            session,
            actor=principal.name,
            reason=payload.reason,
            condition_clear=payload.condition_clear,
            now=now,
            to_state=payload.to_state,
        )
    except LatchError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    session.commit()
    publish_state(bus, settings, snapshot, now=now, reason=payload.reason)
    return _snapshot_payload(snapshot, settings)


# ---------------------------------------------------------------------------
# Load budgets and leases (SDD 31.4)
# ---------------------------------------------------------------------------


@router.get("/load-budgets", summary="Power budgets and active leases")
def get_load_budgets(session: DbSession, settings: AppSettings) -> dict[str, Any]:
    now = utcnow()
    config = _config()
    manager = LeaseManager(config)
    inputs, derived, profiles = _evaluate(session, now)
    snapshot = ensure_snapshot(session, now=now)
    session.commit()

    active = manager.active_leases(session, now)
    grantable = manager.grantable_kw(derived)
    granted = manager.granted_kw(session, now)
    states = current_load_states(session, inputs, now=now, config=config)

    return {
        "energy_state": snapshot.state,
        "grantable_surplus_kw": grantable,
        "granted_kw": granted,
        "remaining_kw": None if grantable is None else grantable - granted,
        "surplus_fraction": config.lease_surplus_fraction,
        "grants_permitted": snapshot.state in config.lease_grant_states,
        "leases": summarise_leases(active),
        "budgets": [
            {
                "asset_id": asset_id,
                "budget_kw": (
                    0.0
                    if state.is_shed
                    else manager.budgets(session, now).get(asset_id)
                    or state.profile.estimated_power_kw
                    or 0.0
                ),
                "is_shed": state.is_shed,
                "effective_tier": state.tier,
            }
            for asset_id, state in sorted(states.items())
        ],
        "note": "A power budget is an allocation, never a safety permissive (SDD 31.4).",
    }


class ReservationRequest(BaseModel):
    asset_id: str
    requested_kw: float = Field(gt=0)
    reason: str = Field(min_length=3)
    duration_s: int | None = Field(default=None, gt=0)
    priority: int = Field(default=3, ge=0, le=4)
    revocable: bool = True


@router.post(
    "/load-budgets/reservations",
    status_code=status.HTTP_201_CREATED,
    summary="Request a power-budget lease (operator)",
)
def create_reservation(
    payload: ReservationRequest,
    session: DbSession,
    settings: AppSettings,
    principal: OperatorPrincipal,
) -> dict[str, Any]:
    now = utcnow()
    manager = LeaseManager(_config())
    _, derived, _ = _evaluate(session, now)
    snapshot = ensure_snapshot(session, now=now)
    try:
        decision = manager.request(
            session,
            asset_id=payload.asset_id,
            requested_kw=payload.requested_kw,
            reason=payload.reason,
            requested_by=principal.name,
            energy_state=snapshot.state,
            derived=derived,
            now=now,
            duration_s=payload.duration_s,
            priority=payload.priority,
            revocable=payload.revocable,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    session.commit()

    body = decision.as_dict()
    body["lease"] = summarise_leases([decision.lease])[0] if decision.lease else None
    body["energy_state"] = snapshot.state
    if not decision.granted:
        # A denial is a recorded, explained answer, not an error.
        return body
    return body


@router.delete("/load-budgets/reservations/{lease_id}", summary="Revoke a power-budget lease (operator)")
def revoke_reservation(
    lease_id: str,
    session: DbSession,
    principal: OperatorPrincipal,
    reason: Annotated[str, Query(min_length=3, description="Audit reason")],
    force: bool = False,
) -> dict[str, Any]:
    now = utcnow()
    manager = LeaseManager(_config())
    try:
        lease = manager.revoke(session, lease_id, reason=reason, actor=principal.name, now=now, force=force)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown lease {lease_id}") from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return {
        "lease": summarise_leases([lease])[0],
        "note": (
            "Revocation withdraws an allocation only. No stop command is issued; the subsystem "
            "remains safe under its own controller (SDD 31.4)."
        ),
    }


# ---------------------------------------------------------------------------
# Loads and shed actions
# ---------------------------------------------------------------------------


@router.get("/loads", summary="The load schedule with current shed state")
def get_loads(session: DbSession) -> dict[str, Any]:
    now = utcnow()
    config = _config()
    inputs, _, profiles = _evaluate(session, now)
    states = current_load_states(session, inputs, now=now, config=config)
    session.commit()
    rows = summarise_loads(states.values())
    return {
        "count": len(rows),
        "protected_tier": config.protected_tier,
        "loads": sorted(rows, key=lambda row: (row["effective_tier"], row["asset_id"])),
        "note": (
            "rated_power_kw and branch circuits are deliberately unresolved; see each load's "
            "open_fields (SDD 49 item 1)."
        ),
    }


@router.get("/shed-actions", summary="Shed and restore action history")
def get_shed_actions(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    asset_id: str | None = None,
) -> dict[str, Any]:
    actions = recent_actions(session, limit=limit, asset_id=asset_id)
    return {
        "count": len(actions),
        "actions": [
            {
                "id": action.id,
                "asset_id": action.asset_id,
                "action": action.action,
                "group": action.group,
                "energy_state": action.energy_state,
                "reason": action.reason,
                "command_id": action.command_id,
                "outcome": action.outcome,
                "occurred_at": action.occurred_at,
            }
            for action in actions
        ],
    }


# ---------------------------------------------------------------------------
# Dashboard (SDD 38)
# ---------------------------------------------------------------------------


@router.get("/dashboard", summary="Energy dashboard aggregate (SDD section 38)")
def get_dashboard(session: DbSession, settings: AppSettings, principal: CurrentPrincipal) -> dict[str, Any]:
    now = utcnow()
    config = _config()
    inputs, derived, profiles = _evaluate(session, now)
    snapshot = ensure_snapshot(session, now=now)
    manager = LeaseManager(config)
    states = current_load_states(session, inputs, now=now, config=config)
    session.commit()

    measured = {
        key: reading.as_dict()
        for key, reading in sorted(inputs.readings.items())
        if not key.startswith("load:")
    }
    calculated = {
        key: value.as_dict() for key, value in sorted(derived.values.items()) if value.kind == "calculated"
    }
    forecast = {
        key: value.as_dict() for key, value in sorted(derived.values.items()) if value.kind == "forecast"
    }

    generator_blob = (snapshot.derived or {}).get("generator", {})
    black_start_blob = (snapshot.derived or {}).get("black_start", {})

    return {
        "generated_at": now,
        "viewer": principal.name,
        # --- one-line power flow -------------------------------------
        "power_flow": {
            "pv_kw": derived.value("pv_power_kw"),
            "battery_kw": inputs.numeric("battery_power_kw"),
            "generator_kw": inputs.numeric("generator_power_kw"),
            "inverter_ac_kw": derived.value("inverter_ac_output_kw"),
            "critical_panel_kw": derived.value("critical_load_kw"),
            "site_load_kw": derived.value("site_load_kw"),
            "balance_kw": derived.value("energy_balance_kw"),
            "source_selected": inputs.text("ats_source_selected"),
        },
        # --- state and reason ----------------------------------------
        "state": _snapshot_payload(snapshot, settings),
        # --- reserve --------------------------------------------------
        "reserve": {
            "soc_pct": derived.value("reserve_pct"),
            "usable_energy_kwh": derived.value("usable_energy_kwh"),
            "emergency_reserve_kwh": derived.value("emergency_reserve_kwh"),
            "energy_above_emergency_reserve_kwh": derived.value("energy_above_emergency_reserve_kwh"),
            "autonomy_critical_h": derived.value("autonomy_critical_h"),
            "autonomy_current_h": derived.value("autonomy_current_h"),
        },
        "bms_limits": {
            "charge_limit_kw": inputs.numeric("battery_charge_limit_kw"),
            "discharge_limit_kw": inputs.numeric("battery_discharge_limit_kw"),
            "available_charge_kw": derived.value("available_charge_kw"),
            "available_discharge_kw": derived.value("available_discharge_kw"),
            "charge_permissive": inputs.flag("bms_charge_permissive"),
            "discharge_permissive": inputs.flag("bms_discharge_permissive"),
        },
        # --- measured vs calculated vs forecast (SDD 38 final rule) ---
        "measured": measured,
        "calculated": calculated,
        "forecast": forecast,
        "tier_load_kw": {str(k): v for k, v in sorted(derived.tier_load_kw.items())},
        "tier_load_is_measured": derived.tier_load_valid,
        # --- generator ------------------------------------------------
        "generator": {
            **generator_blob,
            "observed_state": inputs.text("generator_state"),
            "fuel_level_pct": inputs.numeric("generator_fuel_pct"),
            "runtime_total_h": inputs.numeric("generator_runtime_h"),
            "available": inputs.flag("generator_available"),
        },
        "black_start": black_start_blob,
        # --- shedding and restoration ---------------------------------
        "shed_groups_active": list(snapshot.shed_groups_active or []),
        "shed_loads": [
            {
                "asset_id": state.asset_id,
                "group": state.profile.shed_group,
                "expected_reduction_kw": state.expected_reduction_kw(),
                "measured_kw": state.measured_kw,
                "confirmed": state.confirmed_shed,
                "failed": state.shed_failed,
                "locked_out": state.locked_out,
            }
            for state in states.values()
            if state.is_shed or state.shed_failed
        ],
        "pending_restoration": [
            {
                "asset_id": state.asset_id,
                "group": state.profile.restoration_group,
                "order": state.profile.restoration_order,
                "minimum_off_time_s": state.profile.minimum_off_time_s,
                "restart_delay_s": (state.profile.restart or {}).get("delay_s"),
                "inrush_class": state.profile.inrush_class,
                "automatic_restart_permitted": state.automatic_restart_permitted,
            }
            for state in sorted(states.values(), key=lambda s: s.profile.restoration_order or 0)
            if state.is_shed
        ],
        "restoration_headroom_kw": derived.value("restoration_headroom_kw"),
        # --- leases and backlog ---------------------------------------
        "leases": summarise_leases(manager.active_leases(session, now)),
        "grantable_surplus_kw": manager.grantable_kw(derived),
        "deferrable_backlog": (
            derived.get("deferrable_backlog_kwh").as_dict() if derived.get("deferrable_backlog_kwh") else None
        ),
        # --- container environment ------------------------------------
        "power_container": {
            "temperature_c": inputs.numeric("container_temperature_c"),
            "humidity_pct": inputs.numeric("container_humidity_pct"),
            "alarm_summary": inputs.text("container_alarm_summary"),
            "thermal_derate_pct": derived.value("thermal_derate_pct"),
        },
        # --- data quality for every state-machine value ---------------
        "data_quality": {
            "summary": inputs.data_quality,
            "observable": inputs.observable,
            "invalid_required": [r.key for r in inputs.invalid_required()],
            "missing_points": inputs.missing_points,
            "per_input": {key: reading.status for key, reading in sorted(inputs.readings.items())},
            "per_derived": {key: value.valid for key, value in sorted(derived.values.items())},
        },
        "notes": [
            "Measured, calculated and forecast values are reported separately (SDD 38).",
            "Every threshold shown is a commissioning parameter (SDD 30.2).",
        ],
    }


@router.get("/leases", summary="All power-budget leases including denials")
def get_leases(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    state: str | None = None,
) -> dict[str, Any]:
    statement = select(PowerBudgetLease).order_by(PowerBudgetLease.starts_at.desc()).limit(limit)
    if state:
        statement = statement.where(PowerBudgetLease.state == state)
    leases = list(session.scalars(statement))
    return {"count": len(leases), "leases": summarise_leases(leases)}
