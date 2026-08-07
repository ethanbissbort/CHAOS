# API reference

Base path `/api/v1`. Interactive reference: `/docs` (Swagger) and `/redoc`.
The machine-readable contract is `/openapi.json` and it is **authoritative** —
this document is a map, generated from a running instance and maintained by hand,
so trust the schema where they disagree.

SDD sections 41 (initial API surface), 5.7 (human authority), 15.2 (identity and
access), 17.4 (control presentation).

---

## 1. Identity and roles

Every request may carry an identity:

```
X-Operator:      alice
X-Operator-Role: operator
```

| Role | May |
|---|---|
| `viewer` | Read everything |
| `operator` | Acknowledge alarms, issue commands, request budget leases, set operating modes |
| `maintainer` | Work orders, inspections, calibrations, commissioning records |
| `administrator` | Registry reload, alarm-definition reload |

A request with no `X-Operator` is a `viewer` and **can never write**. Roles are a
ladder: `administrator` includes everything below it.

Authentication itself terminates at the reverse proxy or the VPN (SDD 15.3) —
this platform is local-first and does not implement its own login. What it does
implement is the part that matters for a control system: every audited action
records a named actor.

Do not expose this API to an untrusted network on the strength of that header
alone. See `docs/network-and-trust-boundaries.md`.

---

## 2. Errors

| Status | Meaning |
|---|---|
| 400 | Malformed request |
| 403 | Role insufficient, or a command refused on **authority** grounds |
| 404 | Unknown asset, point, alarm or command |
| 409 | Command refused on **target-state** grounds (interlock, mode, lockout) |
| 422 | Body failed validation |
| 503 | A subsystem is unavailable (for example, the message bus) |

A refused command returns the **whole interlock evaluation**, not just the first
objection, because SDD 17.4 requires the operator to be shown the interlocks
preventing operation:

```json
{
  "detail": {
    "message": "...",
    "command_id": "...",
    "state": "rejected",
    "refused_by": "physical_control_disabled",
    "denied_by": ["physical_control_disabled", "binding_not_commissioned"],
    "interlocks_evaluated": [{"code": "...", "allowed": false, "detail": "..."}]
  }
}
```

`403` versus `409` is a real distinction: 403 means *you* may not, 409 means
*nobody* may right now.

---

## 3. Platform

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness, version, node role, site ID, whether physical control is enabled |
| `GET` | `/openapi.json` | The authoritative schema |
| `GET` | `/docs`, `/redoc` | Interactive reference |
| `GET` | `/ui`, `/` | Built-in operator UI |

`/health` is unauthenticated by design and exposes no telemetry, no credentials
and no control state. It is what the container healthcheck polls.

---

## 4. Overview — SDD 17.1, FR-001

| Method | Path | Summary | Query |
|---|---|---|---|
| `GET` | `/api/v1/overview` | Single-request property overview | `alarm_limit` |
| `GET` | `/api/v1/overview/subsystems` | Per-domain health roll-up | |
| `GET` | `/api/v1/overview/map` | Property map as GeoJSON | |
| `GET` | `/api/v1/overview/control/{asset_id}` | SDD 17.4 control presentation for one asset | |

`/overview` is the home screen in one call: operating state, active critical and
major alarms, energy reserve, communications and server status.

`/overview/control/{asset_id}` returns what SDD 17.4 requires before anyone
operates anything: actual state, requested state, local/remote authority,
interlocks currently preventing operation, last command and issuer, manual
override status.

`/overview/map` returns GeoJSON. Nothing in this repository renders it on a map
yet (`docs/architecture.md`).

---

## 5. Registry — SDD 25, 26, 40

| Method | Path | Summary | Query |
|---|---|---|---|
| `GET` | `/api/v1/assets` | List assets | `domain`, `asset_class`, `status`, `criticality`, `tag`, `q` |
| `GET` | `/api/v1/assets/{asset_id}` | One asset | |
| `GET` | `/api/v1/assets/{asset_id}/points` | Points materialised for an asset | |
| `GET` | `/api/v1/assets/{asset_id}/relationships` | Typed relationships | `direction` |
| `GET` | `/api/v1/assets/{asset_id}/dependencies` | What this asset depends on | `transitive` |
| `GET` | `/api/v1/assets/{asset_id}/tree` | Containment subtree | `depth` |
| `GET` | `/api/v1/points` | List point instances | `asset_id`, `point_name`, `control_capable`, `automatic_control_allowed`, `limit`, `offset` |
| `GET` | `/api/v1/points/{asset_id}/{point_name}` | One point and its binding | |
| `GET` | `/api/v1/registry/summary` | Roll-up counts | |
| `GET` | `/api/v1/registry/dictionary/asset-classes` | Approved asset classes | `domain` |
| `GET` | `/api/v1/registry/dictionary/points` | Canonical point definitions | `control_capable`, `q` |
| `GET` | `/api/v1/registry/design-conflicts` | Unresolved design decisions the register records | |
| `POST` | `/api/v1/registry/reload` | Reload the design package (**administrator**) | |

`/registry/design-conflicts` is worth knowing about. The register deliberately
preserves contradictions rather than resolving them silently — the 12 kW / 40 kWh
versus 45 kWdc / 800 kWh energy design conflict (SDD 30.2) is the big one. This
endpoint surfaces them so a dashboard can show what is still undecided.

Note the point path shape: `/points/{asset_id}/{point_name}` here, versus
`/points/{point_id}/current` under telemetry, where `point_id` is
`<asset_id>/<point_name>`. The telemetry router provides unshadowed aliases
(`/telemetry/points/{point_id}/...`) for clients that would otherwise trip over
the overlap.

---

## 6. Telemetry — SDD 40, 26.6

| Method | Path | Summary | Query |
|---|---|---|---|
| `GET` | `/api/v1/telemetry/current` | Bulk current state | `asset_id`, `domain`, `point_name`, `quality`, `limit` |
| `GET` | `/api/v1/points/{point_id}/current` | Current value of one point | |
| `GET` | `/api/v1/points/{point_id}/history` | Historian samples | `start`, `end`, `limit`, `source` |
| `GET` | `/api/v1/telemetry/points/{point_id}/current` | Unshadowed alias | |
| `GET` | `/api/v1/telemetry/points/{point_id}/history` | Unshadowed alias | `start`, `end`, `limit`, `source` |
| `GET` | `/api/v1/telemetry/stale` | Points whose value cannot be trusted | `quality`, `asset_id`, `limit` |
| `GET` | `/api/v1/telemetry/dead-letters` | Recent ingest failures | `limit`, `topic`, `reason` |
| `GET` | `/api/v1/telemetry/stats` | Ingest counters and historian volume | |
| `POST` | `/api/v1/telemetry/simulate` | Inject a value without hardware | |

Every current-state response carries value, unit, quality, source, timestamp and
staleness, because a number without its quality is not a measurement (SDD 26.6,
FR-003).

`/telemetry/stale` and `/telemetry/dead-letters` are the two endpoints to reach
for when something looks wrong. Stale means the platform knows it cannot see;
dead letters mean a message arrived and could not be resolved to a registry
point — which during commissioning is far more common than a dead sensor.

`POST /telemetry/simulate` writes a value as if it had arrived from the bus. For
bench testing and demonstrations. It is a write path into the historian, so treat
access to it as you would any other write.

---

## 7. Control — SDD 5.7, 10.3, 11, FR-004

| Method | Path | Summary | Role |
|---|---|---|---|
| `POST` | `/api/v1/commands` | Issue a supervisory command | operator |
| `GET` | `/api/v1/commands` | List commands (`asset_id`, `state`, `since`, `limit`) | viewer |
| `GET` | `/api/v1/commands/{command_id}` | One command | viewer |
| `POST` | `/api/v1/commands/{command_id}/ack` | Report a result from a non-MQTT integration | operator |
| `POST` | `/api/v1/commands/{command_id}/cancel` | Cancel a command | operator |
| `GET` | `/api/v1/operating-modes` | List operating modes | viewer |
| `GET` | `/api/v1/operating-modes/{scope_type}/{scope_id}` | One scope's mode | viewer |
| `POST` | `/api/v1/operating-modes/{domain}` | Set a mode (SDD 11) | operator |
| `GET` | `/api/v1/audit` | The control audit trail (`actor`, `action`, `since`, `limit`) | viewer |

### Issuing a command

```http
POST /api/v1/commands
X-Operator: alice
X-Operator-Role: operator
Content-Type: application/json

{
  "asset_id": "energy.pdu.rack_01.switched_01",
  "command": "outlet_state",
  "value": {"outlet": 3, "state": "on"},
  "reason": "restoring rack switch after maintenance, WO-118",
  "ttl_s": 300,
  "idempotency_key": "wo118-outlet3-on",
  "dry_run": false
}
```

| Field | Notes |
|---|---|
| `reason` | **Mandatory.** SDD 5.7: a command without a recorded reason is not auditable |
| `ttl_s` / `expires_at` | A command that cannot be delivered must expire, not queue forever |
| `idempotency_key` | Unique. A retried request returns the original command instead of issuing a second |
| `dry_run` | Evaluates every interlock and publishes nothing. Use it first |
| `maintenance_override` | Maintainer bypass of a maintenance lockout. Recorded in the audit log |
| `depends_on` | Point IDs whose measurements this decision relies on, recorded with the command |

The audit record is written **before** dispatch, so a command that was issued and
then lost in the network is still on the record.

Two gates stand in front of physical actuation: the global
`HOMESTEAD_ALLOW_PHYSICAL_CONTROL`, and the per-binding
`automatic_control_allowed` that only commissioning can set. A command against an
un-commissioned binding is refused with the interlock that refused it. See
`docs/commissioning.md`.

Operating modes are `off`, `manual`, `automatic`, `scheduled`, `maintenance`,
`degraded`, `emergency` (SDD 11). Emergency does not clear automatically unless
the triggering condition and reset policy explicitly permit it.

---

## 8. Energy — SDD 13, 30–38

| Method | Path | Summary | Role |
|---|---|---|---|
| `GET` | `/api/v1/energy/state` | Current site energy state | viewer |
| `GET` | `/api/v1/energy/state/history` | Transition history (`limit`, `since`) | viewer |
| `POST` | `/api/v1/energy/state/freeze` | Freeze or release the state | operator |
| `POST` | `/api/v1/energy/state/clear-latch` | Clear a latching state | operator |
| `GET` | `/api/v1/energy/load-budgets` | Budgets and active leases | viewer |
| `POST` | `/api/v1/energy/load-budgets/reservations` | Request a lease | operator |
| `DELETE` | `/api/v1/energy/load-budgets/reservations/{lease_id}` | Revoke a lease (`reason`, `force`) | operator |
| `GET` | `/api/v1/energy/leases` | All leases including denials | viewer |
| `GET` | `/api/v1/energy/loads` | Load schedule with current shed state | viewer |
| `GET` | `/api/v1/energy/shed-actions` | Shed and restore history | viewer |
| `GET` | `/api/v1/energy/dashboard` | Aggregate for SDD 38 | viewer |

States: `COMMISSIONING`, `MAINTENANCE`, `SURPLUS`, `NORMAL`, `CONSERVE`,
`CRITICAL_RESERVE`, `GENERATOR_SUPPORT`, `EMERGENCY`, `BLACK_START`,
`DEGRADED_SENSOR` (SDD 30.7). `EMERGENCY`, `MAINTENANCE` and `COMMISSIONING`
latch and need `clear-latch` — automatic recovery from an emergency state is
precisely what you do not want.

`/energy/state` carries `data_quality` alongside the state. A state derived from
degraded inputs is a guess, and the API says so rather than presenting it as
fact (SDD 30.5).

The EMS is an allocator, not a relay board: it publishes a state and grants
time-limited power budgets. The BMS, inverters, generator controller and local
PLCs keep immediate equipment authority.

---

## 9. Alarms — SDD 14

| Method | Path | Summary | Role |
|---|---|---|---|
| `GET` | `/api/v1/alarms/active` | Active alarms (`include_pending`, `include_suppressed`) | viewer |
| `GET` | `/api/v1/alarms` | History (`state`, `severity`, `asset_id`, `alarm_key`, `incident_id`, `since`) | viewer |
| `GET` | `/api/v1/alarms/{alarm_id}` | One alarm with its lifecycle | viewer |
| `POST` | `/api/v1/alarms/{alarm_id}/acknowledge` | Acknowledge | operator |
| `POST` | `/api/v1/alarms/{alarm_id}/mitigate` | Record mitigating action | operator |
| `POST` | `/api/v1/alarms/{alarm_id}/clear` | Clear (manual reset) | operator |
| `POST` | `/api/v1/alarms/{alarm_id}/review` | Close with a review | operator |
| `GET` | `/api/v1/alarms/definitions` | Definition set (`domain`, `severity`, `enabled_only`) | viewer |
| `GET` | `/api/v1/alarms/definitions/{alarm_key}` | One definition with FR-008 context | viewer |
| `POST` | `/api/v1/alarms/definitions/reload` | Reload from the design package (`strict`) | maintainer |
| `GET` | `/api/v1/incidents` | Correlated incidents (`state`, `severity`, `since`, `limit`) | viewer |
| `GET` | `/api/v1/incidents/{incident_id}` | One incident with member alarms | viewer |
| `GET` | `/api/v1/notifications` | Delivery log (`channel`, `status`, `incident_id`, `alarm_id`, `limit`) | viewer |

Lifecycle: detected → active → acknowledged → mitigated → cleared → reviewed
(SDD 14.2). Severities: `info`, `warning`, `major`, `critical`, `emergency`.

`/alarms/definitions/{alarm_key}` returns the FR-008 context — the operating
procedure, affected assets, dependencies and manual controls. An alarm that
arrives without that is a noise generator.

**Incidents matter during a flood.** One power-container event produces dozens of
alarms; correlation groups them so a human sees one incident. See
`docs/operations.md` section 3.

---

## 10. Maintenance and commissioning — SDD 18, 19

| Method | Path | Summary | Role |
|---|---|---|---|
| `GET` | `/api/v1/maintenance/plans` | List plans (`asset_id`, `trigger_type`, `enabled`) | viewer |
| `POST` | `/api/v1/maintenance/plans` | Create a plan | maintainer |
| `DELETE` | `/api/v1/maintenance/plans/{plan_id}` | Delete a plan | maintainer |
| `GET` | `/api/v1/maintenance/due` | Work due (`horizon_days`) | viewer |
| `POST` | `/api/v1/maintenance/generate` | Generate work orders from due plans | maintainer |
| `GET` | `/api/v1/work-orders` | List (`state`, `asset_id`, `priority`, `limit`) | viewer |
| `POST` | `/api/v1/work-orders` | Create | maintainer |
| `GET` | `/api/v1/work-orders/{work_order_id}` | One work order | viewer |
| `POST` | `/api/v1/work-orders/{work_order_id}/complete` | Complete | maintainer |
| `GET`/`POST` | `/api/v1/maintenance/inspections` | Inspections (`asset_id`, `limit`) | viewer / maintainer |
| `GET`/`POST` | `/api/v1/maintenance/calibrations` | Calibrations (`point_id`, `limit`) | viewer / maintainer |
| `GET`/`POST` | `/api/v1/maintenance/spare-parts` | Spares (`below_minimum`) | viewer / maintainer |
| `GET` | `/api/v1/commissioning/steps` | The twelve canonical steps | viewer |
| `GET` | `/api/v1/commissioning/{asset_id}/status` | Progress and whether control is permitted | viewer |
| `GET` | `/api/v1/commissioning/{asset_id}/records` | Full records | viewer |
| `POST` | `/api/v1/commissioning/{asset_id}/steps` | Record a step outcome | maintainer |
| `POST` | `/api/v1/commissioning/bindings/{point_id}` | Enable a binding | maintainer |

`POST /commissioning/{asset_id}/steps` body:

```json
{
  "step": 6,
  "result": "pass",
  "preconditions": "inverter in local automatic, battery SOC 62%",
  "injected_condition": "unplugged gateway uplink for 15 min",
  "expected_sequence": "local control continues; LWT fires; comms alarm raised",
  "observed": "as expected; alarm 14:03Z, cleared 14:19Z",
  "evidence": ["photo:IMG_4471", "alarm:8f21..."]
}
```

`result` is `pass`, `fail`, `blocked` or `not_run`.

`POST /commissioning/bindings/{point_id}` returns **409** when
`allow_automatic_control` is requested and the SDD 19 prerequisites have not
passed. That refusal is the point of the endpoint. See `docs/commissioning.md`.

---

## 11. Coverage against SDD section 41

Every endpoint the SDD named exists, and the implementation goes further.

| SDD 41 | Implemented as |
|---|---|
| `GET /assets`, `/assets/{id}`, `/assets/{id}/points`, `/assets/{id}/relationships` | Same paths |
| `GET /points/{point_id}/current` | Same path, plus `/telemetry/points/{point_id}/current` alias |
| `GET /alarms/active` | Same path |
| `POST /commands`, `GET /commands/{id}` | Same paths |
| `POST /operating-modes/{domain}` | Same path |
| `GET /energy/state`, `/energy/load-budgets`, `POST /energy/load-budgets/reservations` | Same paths |
| `GET /work-orders`, `POST /work-orders` | Same paths |

SDD 41 also requires that write endpoints carry authentication, authorization, an
audit reason, an idempotency key and an expiry time where applicable. Authorization,
reason, idempotency and expiry are implemented on the command path. **Authentication
is not** — it terminates upstream at the proxy or VPN, and that architecture is
SDD open decision 22.9.

## 12. Not in the API

Nothing here polls SNMP or Modbus, renders a map, integrates Home Assistant or
Node-RED, forecasts anything, or exposes Prometheus metrics. See
`docs/architecture.md` section 7 for the full list of what is and is not built.

Grafana reads PostgreSQL directly rather than going through this API — it is a
read path, and putting a dashboard's query load through the control plane buys
nothing.
