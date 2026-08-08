# API reference

A map of the platform's HTTP surface. **The machine-readable contract at
`/openapi.json` is authoritative** — this document is maintained by hand, so
trust the schema where they disagree.

Interactive references: `/docs` (Swagger) and `/redoc`, both reachable through
the gateway.

Related: [Architecture](./architecture.md) ·
[Control](./control.md) · [Alarms](./alarms.md) ·
[Network and trust boundaries](./network-and-trust-boundaries.md)

---

## 1. Where the API lives

| Deployment | Reach it at |
|---|---|
| The Windows product | `http://<node>:8080/api/v1/…` — through the gateway |
| The container stack | `http://<node>:8000/api/v1/…` — the platform directly |
| A development checkout | Wherever the platform was told to listen |

In the Windows product the Python backend binds `127.0.0.1:8081` and is **not**
reachable from the LAN. It is an unauthenticated control API; it is reached
through the gateway or not at all.

### Gateway-owned routes

These are served by the .NET gateway itself and are never proxied.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Gateway health **including the backend**. 200 only when the backend is up; 503 when it is down or starting, with the full body either way |
| `GET` | `/health/live` | Process liveness only. 200 whenever the gateway is running. Says nothing about the platform |
| `GET` | `/host/info` | Identity, versions, listen and backend addresses, web-root resolution, supervisor state, route counts |
| `GET` | `/host/routes` | The live route-ownership manifest: who serves what |
| `GET` | `/host/setup` | First-run setup state. **Always 200** — a report that a machine needs setting up is a successful report. Safe to poll |
| `POST` | `/host/setup/run` | Run setup now. 202 when this call started a run; 200 with `accepted: false` and a reason when it did not. `?force=true` proceeds when automatic setup is off or the database needs attention, and **never makes setup destructive** |
| `GET` | `/`, `/ui/**` | The operator console and the annunciator panel |

Everything under `/api/v1` proxies to the Python backend today. The `/api/v1`
row is a deliberate catch-all so a new endpoint nobody listed still reaches the
backend rather than 404-ing.

---

## 2. Identity and roles

Every request may carry an identity:

```http
X-Operator:      alice
X-Operator-Role: operator
```

| Role | May |
|---|---|
| `viewer` | Read everything |
| `operator` | Acknowledge alarms, issue commands, request budget leases, set operating modes |
| `maintainer` | Work orders, inspections, calibrations, commissioning records |
| `administrator` | Registry reload, alarm-definition reload |

A request with no operator header is a `viewer` and **can never write**. Roles
are a ladder: `administrator` includes everything below it.

**Authentication itself terminates at the reverse proxy or the VPN.** This
platform is local-first and does not implement its own login. What it *does*
implement is the part that matters for a control system: every audited action
records a named actor.

**Do not expose this API to an untrusted network on the strength of that header
alone.**

---

## 3. Errors

| Status | Meaning |
|---|---|
| 400 | Malformed request |
| 403 | Role insufficient, or a command refused on **authority** grounds |
| 404 | Unknown asset, point, alarm or command |
| 409 | Command refused on **target-state** grounds (interlock, mode, lockout) |
| 422 | Body failed validation |
| 503 | A subsystem is unavailable (for example, the message bus) |

`403` versus `409` is a real distinction: **403 means *you* may not; 409 means
*nobody* may right now.**

A refused command returns the **whole interlock evaluation**, not just the first
objection, because an operator must be shown the interlocks preventing
operation:

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

---

## 4. Overview

| Method | Path | Summary | Query |
|---|---|---|---|
| `GET` | `/api/v1/overview` | Single-request property overview | `alarm_limit` |
| `GET` | `/api/v1/overview/subsystems` | Per-domain health roll-up | |
| `GET` | `/api/v1/overview/map` | Property map as GeoJSON | |
| `GET` | `/api/v1/overview/control/{asset_id}` | Control presentation for one asset | |

`/overview` is the console's home screen in one call: operating state, active
critical and major alarms, energy reserve, communications and server status.

`/overview/control/{asset_id}` returns the seven things required before anyone
operates anything — actual state, requested state, local/remote authority,
interlocks currently preventing operation, last command and issuer, manual
override status, and budget impact. See [Control](./control.md).

`/overview/map` returns GeoJSON. Nothing renders it over a basemap.

---

## 5. Registry

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
| `GET` | `/api/v1/topology` | The dependency graph: nodes, edges, groups, extensions | |
| `GET` | `/api/v1/topology/impact/{asset_id}` | Blast radius for one asset, with caveats | |
| `GET` | `/api/v1/rack` | The 42U elevation, feeds, port plan and findings | |

`/registry/design-conflicts` is worth knowing about. The register deliberately
preserves contradictions rather than resolving them silently — see
[The design package § Preserved conflicts](./design-package.md#5-preserved-conflicts).

`/topology` and `/topology/impact/{asset_id}` are covered in
[Topology and blast radius](./topology-view.md); `/rack` in
[Rack elevation](./rack-view.md).

Note the point path shape: `/points/{asset_id}/{point_name}` here, versus
`/points/{point_id}/current` under telemetry, where the point ID is
`<asset_id>/<point_name>`. The telemetry router provides unshadowed aliases for
clients that would otherwise trip over the overlap.

---

## 6. Telemetry

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
staleness, because **a number without its quality is not a measurement**.

`/telemetry/stale` and `/telemetry/dead-letters` are the two to reach for when
something looks wrong. **Stale** means the platform knows it cannot see; **dead
letters** mean a message arrived and could not be resolved to a registry point —
which during commissioning is far more common than a dead sensor.

`POST /telemetry/simulate` writes a value as if it had arrived from the bus, for
bench testing and demonstrations. It is a write path into the historian, so
treat access to it as you would any other write.

---

## 7. Control

| Method | Path | Summary | Role |
|---|---|---|---|
| `POST` | `/api/v1/commands` | Issue a supervisory command | operator |
| `GET` | `/api/v1/commands` | List commands (`asset_id`, `state`, `since`, `limit`) | viewer |
| `GET` | `/api/v1/commands/{command_id}` | One command | viewer |
| `POST` | `/api/v1/commands/{command_id}/ack` | Report a result from a non-bus integration | operator |
| `POST` | `/api/v1/commands/{command_id}/cancel` | Cancel a command | operator |
| `GET` | `/api/v1/operating-modes` | List operating modes | viewer |
| `GET` | `/api/v1/operating-modes/{scope_type}/{scope_id}` | One scope's mode | viewer |
| `POST` | `/api/v1/operating-modes/{domain}` | Set a mode | operator |
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
| `reason` | **Mandatory.** A command without a recorded reason is not auditable |
| `ttl_s` / `expires_at` | A command that cannot be delivered must expire, not queue forever |
| `idempotency_key` | Unique. A retried request returns the original command instead of issuing a second |
| `dry_run` | Evaluates every interlock and publishes nothing. **Use it first** |
| `maintenance_override` | Maintainer bypass of a maintenance lockout. Recorded in the audit log |
| `depends_on` | Point IDs whose measurements this decision relies on, recorded with the command |

**The audit record is written before dispatch**, so a command that was issued and
then lost in the network is still on the record.

Operating modes are `off`, `manual`, `automatic`, `scheduled`, `maintenance`,
`degraded`, `emergency`. Emergency latches and does not clear automatically. See
[Control § Operating modes](./control.md#5-operating-modes).

---

## 8. Energy

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
| `GET` | `/api/v1/energy/dashboard` | Aggregate for the energy screen | viewer |

States: `COMMISSIONING`, `MAINTENANCE`, `SURPLUS`, `NORMAL`, `CONSERVE`,
`CRITICAL_RESERVE`, `GENERATOR_SUPPORT`, `EMERGENCY`, `BLACK_START`,
`DEGRADED_SENSOR`. `EMERGENCY`, `MAINTENANCE` and `COMMISSIONING` latch and need
an explicit clear — automatic recovery from an emergency state is precisely what
you do not want.

`/energy/state` carries its **data quality** alongside the state. A state derived
from degraded inputs is a guess, and the API says so rather than presenting it as
fact.

---

## 9. Alarms

| Method | Path | Summary | Role |
|---|---|---|---|
| `GET` | `/api/v1/annunciator` | The whole annunciator panel: bays, tiles, states, serviceability, summary (`include_pending`) | viewer |
| `GET` | `/api/v1/alarms/active` | Active alarms (`include_pending`, `include_suppressed`) | viewer |
| `GET` | `/api/v1/alarms` | History (`state`, `severity`, `asset_id`, `alarm_key`, `incident_id`, `since`) | viewer |
| `GET` | `/api/v1/alarms/{alarm_id}` | One alarm with its lifecycle | viewer |
| `POST` | `/api/v1/alarms/{alarm_id}/acknowledge` | Acknowledge | operator |
| `POST` | `/api/v1/alarms/{alarm_id}/mitigate` | Record mitigating action | operator |
| `POST` | `/api/v1/alarms/{alarm_id}/clear` | Clear (manual reset) | operator |
| `POST` | `/api/v1/alarms/{alarm_id}/review` | Close with a review | operator |
| `GET` | `/api/v1/alarms/definitions` | Definition set (`domain`, `severity`, `enabled_only`) | viewer |
| `GET` | `/api/v1/alarms/definitions/{alarm_key}` | One definition with its operator context | viewer |
| `POST` | `/api/v1/alarms/definitions/reload` | Reload from the design package (`strict`) | maintainer |
| `GET` | `/api/v1/incidents` | Correlated incidents (`state`, `severity`, `since`, `limit`) | viewer |
| `GET` | `/api/v1/incidents/{incident_id}` | One incident with member alarms | viewer |
| `GET` | `/api/v1/notifications` | Delivery log (`channel`, `status`, `incident_id`, `alarm_id`, `limit`) | viewer |

`/annunciator` returns **every definition as a tile**, whether or not it is in
alarm, with a computed serviceability flag. A tile that cannot light is reported
`out_of_service` rather than dark, because a dark tile is a positive claim that
the condition is normal. See [The annunciator panel](./annunciator.md).

The panel's **ACKNOWLEDGE** writes to `/alarms/{id}/acknowledge` and its
**RESET** writes to `/alarms/{id}/review`.

`/alarms/definitions/{alarm_key}` returns the operator context — the operating
procedure, affected assets, dependencies and manual controls. An alarm that
arrives without that is a noise generator.

**Incidents matter during a flood.** One power-container event produces dozens of
alarms; correlation groups them so a human sees one incident. See
[Alarms § Correlation](./alarms.md#5-correlation-and-incidents).

---

## 10. Maintenance and commissioning

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
| `GET` | `/api/v1/commissioning/{asset_id}/status` | Progress, and whether control is permitted | viewer |
| `GET` | `/api/v1/commissioning/{asset_id}/records` | Full records | viewer |
| `POST` | `/api/v1/commissioning/{asset_id}/steps` | Record a step outcome | maintainer |
| `POST` | `/api/v1/commissioning/bindings/{point_id}` | Enable a binding | maintainer |

Recording a step outcome:

```json
{
  "step": 6,
  "result": "pass",
  "preconditions": "inverter in local automatic, battery SOC 62%",
  "injected_condition": "unplugged gateway uplink for 15 min",
  "expected_sequence": "local control continues; availability message fires; comms alarm raised",
  "observed": "as expected; alarm 14:03Z, cleared 14:19Z",
  "evidence": ["photo:IMG_4471", "alarm:8f21..."]
}
```

`result` is `pass`, `fail`, `blocked` or `not_run`.

Enabling a binding:

```json
{"allow_automatic_control": true, "reason": "SDD 19 sequence complete, ref WO-118"}
```

That endpoint returns **409** when automatic control is requested and the
prerequisites have not passed. **That refusal is the point of the endpoint.** See
[Commissioning](./commissioning.md).

---

## 11. Surface size

**83 operations under `/api/v1`**, plus the platform's own `/health`. Everything
the design document named exists, and the implementation goes further.

Write endpoints carry authorization, an audit reason, an idempotency key and an
expiry where applicable. **Authentication is not implemented here** — it
terminates upstream at the proxy or VPN, and that architecture is still an open
decision.

---

## 12. Not in the API

Nothing here polls SNMP or Modbus, renders a map over a basemap, integrates a
home-automation platform, forecasts anything, or exposes Prometheus metrics. See
[Architecture § Status summary](./architecture.md#8-status-summary).

In the container deployment, Grafana reads PostgreSQL directly rather than going
through this API — it is a read path, and putting a dashboard's query load
through the control plane buys nothing.

---

## 13. Related reading

| Document | Why |
|---|---|
| [Architecture](./architecture.md) | Route ownership, and what the gateway does |
| [Control](./control.md) | The command path and the eight interlocks |
| [Alarms](./alarms.md) | The alarm model behind section 9 |
| [Commissioning](./commissioning.md) | What section 10 is enforcing |
| [Network and trust boundaries](./network-and-trust-boundaries.md) | Who may reach this API at all |
