# Commissioning

SDD section 19: every subsystem integration passes a twelve-step sequence before
it goes under automatic supervisory control.

> No subsystem should be placed under automatic supervisory control until its
> local control and failure modes have been tested.

This document is the working checklist. It is tied to the platform's own
commissioning records, so the sequence is data the software enforces rather than
a paragraph in a document that everyone agrees with and nobody follows.

---

## 0. How the platform enforces it

`src/homestead_twin/maintenance/commissioning.py` holds the canonical step list
and the gate:

- Steps **1–8** are prerequisites.
- Step **9** is the supervisory-control test itself.
- `may_enable_automatic_control(session, asset_id)` returns `False` with a reason
  until 1–8 **and** 9 have passed.
- `commission_binding(...)` refuses to set `automatic_control_allowed` on a point
  binding when that gate says no.

Two independent switches stand between this platform and physical plant:

| Gate | Scope | Set by |
|---|---|---|
| `HOMESTEAD_ALLOW_PHYSICAL_CONTROL` | Whole platform | `deploy/.env` |
| `point_bindings.automatic_control_allowed` | One point | `POST /api/v1/commissioning/bindings/{point_id}`, gated on this sequence |

Turning on the global flag does not arm anything that has not been commissioned.
That is deliberate: one switch should not be able to arm the property.

### The commands you will use

```sh
BASE=http://127.0.0.1:8000/api/v1
AUTH='-H "X-Operator: your.name" -H "X-Operator-Role: maintainer"'

# The canonical twelve steps
curl -s $BASE/commissioning/steps

# Where one asset stands
curl -s $BASE/commissioning/energy.inverter.power_container.01/status

# Record a step outcome (maintainer role required)
curl -s -X POST $BASE/commissioning/energy.inverter.power_container.01/steps \
  -H 'Content-Type: application/json' \
  -H 'X-Operator: your.name' -H 'X-Operator-Role: maintainer' \
  -d '{
        "step": 6,
        "result": "pass",
        "preconditions": "inverter in local automatic, battery SOC 62%",
        "injected_condition": "unplugged gateway uplink for 15 min",
        "expected_sequence": "inverter continues local control; availability LWT fires; platform raises comms alarm",
        "observed": "as expected; alarm at 14:03Z, cleared 14:19Z on reconnect",
        "evidence": ["photo:IMG_4471", "alarm:8f21...", "log:mosquitto-2026-08-07"]
      }'

# Full record for the file
curl -s $BASE/commissioning/energy.inverter.power_container.01/records

# Enable the binding once the gate allows it
curl -s -X POST $BASE/commissioning/bindings/energy.inverter.power_container.01/power_ac_output_kw \
  -H 'Content-Type: application/json' \
  -H 'X-Operator: your.name' -H 'X-Operator-Role: maintainer' \
  -d '{"allow_automatic_control": true, "reason": "commissioning complete, ref WO-118"}'
```

`result` is one of `pass`, `fail`, `blocked`, `not_run`. Recording `blocked` is
useful and honest — it says the step could not be run, which is different from
"it passed" and different from "nobody has looked".

Progress across all assets is on the platform Grafana dashboard, panel
*Commissioning progress by asset*.

---

## 1. Before you start

Per subsystem, have these in hand:

- [ ] The assets exist in the registry with the right `asset_class`, `criticality`
      and `control_authority` (`GET /api/v1/assets/{asset_id}`).
- [ ] The points exist and their bindings have real addresses, not `TBD`
      (`GET /api/v1/points/{asset_id}/{point_name}`).
- [ ] The manual override and failure-state record for each controlled asset
      (SDD 9.3, MVP criterion 9).
- [ ] The equipment manual, and the vendor's own commissioning procedure.
- [ ] A second person, for anything that can move, heat, energise or pressurise.

And know the two rules that override everything below:

1. **Level 0 and Level 1 win.** If the local controller refuses a command, that
   is a passing result, not a fault to work around.
2. **Stop on a fail.** A failed step is not a reason to proceed carefully; it is
   a reason to stop. Record it, fix it, re-run it.

---

## 2. The twelve steps

Each step lists what to do, what "pass" means, and what to record. The platform's
`step_name` is given so records and this document agree.

### Step 1 — `bench_test`

Exercise the device away from the plant: on a bench, or with the simulator.

- Power it, connect it, read a value, write a value if it is controllable.
- For a gateway: publish one telemetry message and confirm it lands.
  `homestead-twin status` → `ingest_dead_letters` should not increment.
- Run the simulator against the subsystem's points:
  `homestead-twin simulate -- --scenario <name> --offline`.

**Pass:** the device communicates and behaves as the manual describes, with no
plant attached.
**Record:** firmware version, protocol, and the exact addressing that worked.

### Step 2 — `point_to_point_verification`

Every point, individually, end to end: physical signal → device → binding →
`point_id` → `current_state`.

- Force or observe a real physical change; confirm the value moves in the twin.
- Confirm the **units** match the point dictionary. A pressure in kPa arriving on
  a point defined in bar is the classic way to build a plausible, wrong twin.
- Confirm scale and offset on the binding.
- Check `GET /api/v1/telemetry/dead-letters` is empty for this asset.

**Pass:** every point resolves to the intended `point_id`, with correct units and
plausible values. No dead letters.
**Record:** the point list with observed values, and any binding corrections.

This is the step most often rushed, and it is the one that determines whether
everything above it is measuring reality.

### Step 3 — `sensor_calibration`

Compare each sensor against a reference; apply and record the correction.

- Record reference instrument, its calibration date, and the readings.
- Apply corrections at the binding (`scale`, `offset`), not in downstream logic.
- Create a `Calibration` record: `POST /api/v1/maintenance/calibrations`.

**Pass:** every sensor within its stated accuracy, with the correction recorded.
**Record:** as-found and as-left readings. As-found is what tells you next year
whether the sensor is drifting.

### Step 4 — `manual_control_test`

Operate the equipment by hand, at the equipment. No platform involvement.

- Local HOA switch, manual valve, manual start.
- Confirm the platform *observes* the change (actual state follows) without
  having caused it.
- Confirm the manual-override record in the registry matches what you just did
  (SDD 9.3, 17.4).

**Pass:** the equipment works under local control, and the twin reports it
correctly without commanding it.
**Record:** override mechanism, its location, and its effect on the twin's view.

### Step 5 — `local_automatic_control_test`

The subsystem's own automatic control, without supervision.

- Let the local controller run its loop: thermostat, level control, pressure
  control, its own start/stop logic.
- Confirm setpoints, dead-bands, minimum on/off times behave as configured.

**Pass:** the subsystem runs correctly on its own. This is the state it must fall
back to whenever the platform is unavailable (SDD 5.2), so a shaky pass here is a
failure of the whole reliability model.
**Record:** setpoints and timings as configured at the local controller.

### Step 6 — `communications_loss_test`

Take communications away and see what happens.

- Disconnect the gateway uplink, or block the broker at the firewall.
- Confirm the subsystem **continues safe local operation** (SDD 5.2).
- Confirm the platform notices: last-will `availability`, points going stale, a
  comms alarm at the right severity.
- Restore, and confirm reconciliation: no stale command executes late, and
  retained state does not overwrite reality (SDD 35.4).

**Pass:** the plant carries on; the platform admits it cannot see. Both halves are
required — a subsystem that keeps running while the platform silently displays
its last known value has failed this step.
**Record:** duration, subsystem behaviour, alarm timing, recovery behaviour.

This step is also the property-scale rehearsal for MVP criterion 7.

### Step 7 — `sensor_failure_test`

Break a sensor, not the wire.

- Disconnect a sensor, short it, or drive it out of range.
- Confirm the value is marked bad quality, not passed through as a number.
- Confirm the subsystem fails safe: `GET /api/v1/telemetry/stale` shows it, and
  any control loop using it degrades rather than acting on garbage.
- For the EMS: confirm it enters `DEGRADED_SENSOR` rather than dispatching on an
  invalid input (SDD 30.5, 30.7).

**Pass:** bad data is identified as bad and does not drive control.
**Record:** which failure modes were injected and how each was handled.

A sensor reading zero is the dangerous case: zero is a plausible number. Confirm
the quality model catches it, not just the range check.

### Step 8 — `power_loss_and_restoration_test`

Cut power to the subsystem and restore it.

- Confirm a safe de-energised state.
- Confirm restart behaviour matches the load record's restart policy: does it
  auto-restart, does it wait, does it require a manual reset?
- Confirm no simultaneous restart with other loads (SDD 33.2, FR-103).
- Confirm the twin reconciles rather than assuming its pre-outage state.

**Pass:** safe on loss, controlled on restoration, honest in the twin.
**Record:** de-energised state, restart delay, whether manual intervention was
needed.

**Steps 1–8 are the prerequisites.** The platform will not permit automatic
supervisory control until all eight pass.

### Step 9 — `supervisory_control_test`

The first time the platform commands this equipment.

- Set `HOMESTEAD_ALLOW_PHYSICAL_CONTROL=true` and enable **this one binding**.
- Issue one command through `POST /api/v1/commands`, with a named operator and a
  reason.
- Confirm: the audit record is written *before* dispatch; the command reaches
  the device; the device acknowledges; the twin's requested and actual state
  converge.
- Then confirm the negative case: issue a command the local controller should
  **refuse** (an interlock, a lockout, a mode conflict). Confirm it is rejected
  and the rejection is recorded.

**Pass:** commands work, acknowledgements return, and refusals are honoured and
recorded.
**Record:** command ID, audit entry, acknowledgement latency, and the refusal
case with its reason.

The refusal case is not optional. A command path that has only ever been tested
on commands that succeed has not been tested.

### Step 10 — `alarm_and_notification_test`

Make a real alarm fire and follow it all the way out.

- Drive the condition. Confirm the alarm activates at the right severity.
- Confirm the notification arrives, on every configured backend.
- Confirm the alarm links to its operating procedure, affected assets,
  dependencies and manual controls (SDD 14.3, FR-008):
  `GET /api/v1/alarms/definitions/{alarm_key}`.
- Confirm the lifecycle works: acknowledge → mitigate → clear → review.
- **Confirm it works with the internet down** (MVP criterion 4). If the only
  backend is `log` or `email`, this step fails.

**Pass:** the alarm fires, reaches a human, carries its context, and clears
properly.
**Record:** alarm key, trigger, notification channels and their delivery times.

### Step 11 — `manual_override_test`

Take control back by hand while the platform is running.

- Operate the local override with the platform commanding the equipment.
- Confirm the override wins.
- Confirm the twin shows override status and does not fight it (SDD 17.4).
- Confirm the platform raises the right advisory rather than treating it as a
  fault.

**Pass:** a human at the equipment always beats the platform, and the platform
says so on screen.
**Record:** override mechanism, twin behaviour, how the override is cleared.

This step is the one that matters at 2am in a storm. Test it as if you mean it.

### Step 12 — `documentation_and_baseline_capture`

Capture what "normal" looks like, so a future deviation is visible.

- [ ] Baseline telemetry captured: `homestead-twin export --include state,history --since ...`
- [ ] Setpoints, dead-bands and timings recorded in the registry, not in someone's head
- [ ] Manual-override and failure-state records complete for every controlled asset
- [ ] Binding addresses, protocol and scaling recorded; `binding_status` no longer `tbd`
- [ ] Alarm definitions reviewed against what actually fired during steps 6–10
- [ ] Photos, wiring notes and vendor documents attached as `Document` rows
- [ ] `deploy/mosquitto/acl.example` section 4 verification re-run for the new identity
- [ ] Backup taken and **restored into a scratch database** to prove it works
- [ ] `homestead-twin backup` archive copied off the property

**Pass:** someone who was not there could operate and troubleshoot this subsystem
from the record.
**Record:** the baseline export, and the date it was taken.

---

## 3. After the sequence

```sh
curl -s $BASE/commissioning/{asset_id}/status
```

Expect `"supervisory_control_permitted": true` and
`"fully_commissioned": true`. Then enable the bindings that need automatic
control — one at a time, with a reason:

```sh
curl -s -X POST $BASE/commissioning/bindings/{point_id} \
  -H 'Content-Type: application/json' \
  -H 'X-Operator: your.name' -H 'X-Operator-Role: maintainer' \
  -d '{"allow_automatic_control": true, "reason": "SDD 19 sequence complete, ref WO-118"}'
```

Then watch it for a while before commissioning the next subsystem. The failure
modes that matter usually appear on the second night, not the first hour.

---

## 4. Order of subsystems

SDD section 20 sets the phase order, and it is a good order for a reason: each
phase depends on the one before it being observable.

| Phase | Subsystems | Platform state |
|---|---|---|
| A | Digital twin foundation, simulated MQTT, first dashboard | **Done** — this repository |
| B | NetBotz, UPS, PDUs, rack, network gear; inverter, BMS, meter, generator, critical panel; energy state and load tiers; secondary node; black-start test | **Next.** The EMS and the register are built for this and have never seen it |
| C | Well, tanks, pumps, pressure, flow, leak detection, treatment; weather; LoRaWAN; soil and compost; one irrigation zone | Water assets and points exist in the design package; no coordinator is built |
| D | Greenhouse, hydrogel lab, nitrogen storage, spa, mower | Not modelled beyond load groups |
| E | Forecasting, prediction, scenario simulation | Not built |

Within Phase B, commission in dependency order: monitoring before control,
observation before command. NetBotz and the UPS first — they are read-only, and
they are what tells you the container is in trouble. The battery and inverters
last, because they are the ones that can hurt you.

---

## 5. Common failure modes

| What happens | What it usually means |
|---|---|
| Step 2 passes but values look wrong | Unit mismatch between the device and the point dictionary. Fix the binding, not the dashboard |
| Step 6 "passes" because nothing alarmed | The platform did not notice the loss. That is a fail, not a pass — check `stale_after_s` on the points |
| Step 7 passes with a sensor reading zero | Zero is plausible. Confirm the quality model marks it bad, rather than the range check accepting it |
| Step 9 acknowledgement never arrives | The gateway has no ACL grant for `cmd/+/ack`. Check the broker log for `Denied PUBLISH` |
| Step 10 notification never arrives | `HOMESTEAD_NOTIFICATION_BACKENDS` is `log`. A log line is not an alert |
| Everything passes in a day | Steps 6, 7, 8 and 11 require actually breaking things. If nothing was broken, they were not tested |

---

## 6. What this platform cannot commission for you

The records, gates and API here track and enforce the sequence. They do not
perform it. Steps 1, 3, 4, 5, 8 and 11 happen at the equipment, with tools, by a
person, and much of it needs two people.

And the honest position today: **no subsystem in this repository has been through
this sequence against real hardware.** Every path has been exercised against the
simulator and the in-memory bus, which is step 1 and nothing beyond it. Every
`pass` recorded so far would be a `pass` for a simulation.
