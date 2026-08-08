# Commissioning

Every subsystem passes a twelve-step sequence before it goes under automatic
supervisory control.

> No subsystem should be placed under automatic supervisory control until its
> local control and failure modes have been tested.

This document is the working checklist. It is tied to the platform's own
commissioning records, so the sequence is **data the software enforces** rather
than a paragraph everyone agrees with and nobody follows.

Related: [Control, interlocks and operating modes](./control.md) ·
[Operations § Enabling physical control](./operations.md#7-enabling-physical-control) ·
[The design package](./design-package.md)

---

## 0. How the platform enforces it

- Steps **1–8** are prerequisites.
- Step **9** is the supervisory-control test itself.
- The platform refuses to permit automatic control, **with a reason**, until 1–8
  and 9 have passed for that asset.
- It refuses to enable automatic control on a point binding when that gate says
  no.

Two independent switches stand between this platform and physical plant:

| Gate | Scope |
|---|---|
| The platform-wide physical-control gate | Everything |
| Per-binding automatic control | One point, gated on this sequence |

Turning on the global flag does not arm anything that has not been commissioned.
That is deliberate: **one switch should not be able to arm the property.**

### Where you do this

| Task | Where |
|---|---|
| See where an asset stands | Console → **Control** → the asset. Its commissioning status is on the screen |
| Record a step outcome | The commissioning endpoint, with the `maintainer` role |
| Enable a binding once the gate allows it | The commissioning endpoint, with the `maintainer` role |
| Track progress across every asset | Console → **Assets**, and the binding statuses |

**There is no dedicated commissioning form in the console today.** Recording a
step is an API call; the request bodies are in
[API reference § Maintenance and commissioning](./api.md#10-maintenance-and-commissioning).
Reading the state — which is what you do far more often — is on screen.

### The four outcomes

`pass` · `fail` · `blocked` · `not_run`

Recording **`blocked`** is useful and honest: it says the step could not be run,
which is different from "it passed" and different from "nobody has looked".

---

## 1. Before you start

Per subsystem, have these in hand:

- [ ] The assets exist in the registry with the right class, criticality and
      control authority. Check on the console's **Assets** screen.
- [ ] The points exist and their bindings have **real addresses, not `TBD`**.
- [ ] The manual override and failure-state record for each controlled asset.
- [ ] The equipment manual, and the vendor's own commissioning procedure.
- [ ] **A second person**, for anything that can move, heat, energise or
      pressurise.

And know the two rules that override everything below:

1. **Level 0 and Level 1 win.** If the local controller refuses a command, that
   is a **passing result**, not a fault to work around.
2. **Stop on a fail.** A failed step is not a reason to proceed carefully; it is
   a reason to stop. Record it, fix it, re-run it.

---

## 2. The twelve steps

Each step lists what to do, what "pass" means, and what to record. The
platform's own step name is given so records and this document agree.

### Step 1 — `bench_test`

Exercise the device away from the plant: on a bench, or against the simulator.

- Power it, connect it, read a value, write a value if it is controllable.
- For a gateway: publish one telemetry message and confirm it lands. **The
  dead-letter count must not increment.**
- Run the simulator against the subsystem's points —
  [Command line § The simulator](./advanced-command-line.md#9-the-simulator).

**Pass:** the device communicates and behaves as the manual describes, with no
plant attached.
**Record:** firmware version, protocol, and the exact addressing that worked.

### Step 2 — `point_to_point_verification`

Every point, individually, end to end: physical signal → device → binding →
point → current state.

- Force or observe a real physical change; confirm the value moves in the twin.
- **Confirm the units** match the point dictionary. A pressure in kPa arriving on
  a point defined in bar is the classic way to build a plausible, wrong twin.
- Confirm scale and offset on the binding.
- Confirm there are no dead letters for this asset.

**Pass:** every point resolves to the intended point, with correct units and
plausible values. No dead letters.
**Record:** the point list with observed values, and any binding corrections.

**This is the step most often rushed, and it is the one that determines whether
everything above it is measuring reality.**

### Step 3 — `sensor_calibration`

Compare each sensor against a reference; apply and record the correction.

- Record the reference instrument, its calibration date, and the readings.
- Apply corrections **at the binding**, not in downstream logic.
- Create a calibration record.

**Pass:** every sensor within its stated accuracy, with the correction recorded.
**Record:** as-found **and** as-left readings. As-found is what tells you next
year whether the sensor is drifting.

### Step 4 — `manual_control_test`

Operate the equipment by hand, at the equipment. No platform involvement.

- Local hand/off/auto switch, manual valve, manual start.
- Confirm the platform **observes** the change without having caused it.
- Confirm the manual-override record in the registry matches what you just did.

**Pass:** the equipment works under local control, and the twin reports it
correctly without commanding it.
**Record:** the override mechanism, its location, and its effect on the twin's
view.

### Step 5 — `local_automatic_control_test`

The subsystem's own automatic control, without supervision.

- Let the local controller run its loop: thermostat, level control, pressure
  control, its own start/stop logic.
- Confirm setpoints, dead-bands and minimum on/off times behave as configured.

**Pass:** the subsystem runs correctly on its own. **This is the state it must
fall back to whenever the platform is unavailable**, so a shaky pass here is a
failure of the whole reliability model.
**Record:** setpoints and timings as configured at the local controller.

### Step 6 — `communications_loss_test`

Take communications away and see what happens.

- Disconnect the gateway uplink, or block the broker at the firewall.
- Confirm the subsystem **continues safe local operation**.
- Confirm the platform notices: the last-will availability message, points going
  stale, a comms alarm at the right severity.
- Restore, and confirm reconciliation: no stale command executes late, and
  retained state does not overwrite reality.

**Pass:** the plant carries on; the platform admits it cannot see. **Both halves
are required** — a subsystem that keeps running while the platform silently
displays its last known value has failed this step.
**Record:** duration, subsystem behaviour, alarm timing, recovery behaviour.

This step is also the property-scale rehearsal for the communications-loss
acceptance criterion.

### Step 7 — `sensor_failure_test`

Break a sensor, not the wire.

- Disconnect a sensor, short it, or drive it out of range.
- Confirm the value is marked bad quality, **not passed through as a number**.
- Confirm the subsystem fails safe: the stale-point list shows it, and any
  control loop using it degrades rather than acting on garbage.
- For the energy manager: confirm it enters `DEGRADED_SENSOR` rather than
  dispatching on an invalid input.

**Pass:** bad data is identified as bad and does not drive control.
**Record:** which failure modes were injected and how each was handled.

**A sensor reading zero is the dangerous case**, because zero is a plausible
number. Confirm the quality model catches it, not just the range check.

### Step 8 — `power_loss_and_restoration_test`

Cut power to the subsystem and restore it.

- Confirm a safe de-energised state.
- Confirm restart behaviour matches the load record's restart policy: does it
  auto-restart, wait, or require a manual reset?
- Confirm **no simultaneous restart** with other loads.
- Confirm the twin reconciles rather than assuming its pre-outage state.

**Pass:** safe on loss, controlled on restoration, honest in the twin.
**Record:** de-energised state, restart delay, whether manual intervention was
needed.

> **Steps 1–8 are the prerequisites.** The platform will not permit automatic
> supervisory control until all eight pass.

### Step 9 — `supervisory_control_test`

The first time the platform commands this equipment.

- Turn on the platform-wide gate and enable **this one binding**.
- Issue one command with a named operator and a reason. **Dry-run it first** —
  it evaluates every interlock and publishes nothing.
- Confirm: the audit record is written *before* dispatch; the command reaches
  the device; the device acknowledges; the twin's requested and actual state
  converge.
- Then confirm the **negative case**: issue a command the local controller
  should refuse. Confirm it is rejected and the rejection is recorded.

**Pass:** commands work, acknowledgements return, and refusals are honoured and
recorded.
**Record:** command ID, audit entry, acknowledgement latency, and the refusal
case with its reason.

**The refusal case is not optional.** A command path that has only ever been
tested on commands that succeed has not been tested.

See [Control § The eight interlocks](./control.md#4-the-eight-interlocks) for
what each refusal means.

### Step 10 — `alarm_and_notification_test`

Make a real alarm fire and follow it all the way out.

- Drive the condition. Confirm the alarm activates at the right severity.
- Confirm the tile lights on [the annunciator](./annunciator.md), with the horn.
- Confirm the notification arrives on **every** configured channel.
- Confirm the alarm links to its operating procedure, affected assets,
  dependencies and manual controls.
- Confirm the lifecycle works: acknowledge → mitigate → clear → review.
- **Confirm it works with the internet down.** If the only channel is `log` or
  `email`, this step fails.

**Pass:** the alarm fires, reaches a human, carries its context, and clears
properly.
**Record:** alarm key, trigger, notification channels and their delivery times.

Before you start: check the tile is not `OUT OF SVC`. Ten of the forty shipped
definitions cannot fire at all —
[The annunciator panel § Out of service](./annunciator.md#6-out-of-service--the-honest-dark-tile).

### Step 11 — `manual_override_test`

Take control back by hand while the platform is running.

- Operate the local override with the platform commanding the equipment.
- Confirm **the override wins**.
- Confirm the twin shows override status and does not fight it.
- Confirm the platform raises the right advisory rather than treating it as a
  fault.

**Pass:** a human at the equipment always beats the platform, and the platform
says so on screen.
**Record:** the override mechanism, twin behaviour, and how the override is
cleared.

**This step is the one that matters at 2am in a storm. Test it as if you mean
it.**

### Step 12 — `documentation_and_baseline_capture`

Capture what "normal" looks like, so a future deviation is visible.

- [ ] Baseline telemetry captured —
      [Command line § Export](./advanced-command-line.md#7-export).
- [ ] Setpoints, dead-bands and timings recorded in the registry, not in
      someone's head.
- [ ] Manual-override and failure-state records complete for every controlled
      asset.
- [ ] Binding addresses, protocol and scaling recorded; the binding status is no
      longer `tbd`.
- [ ] Alarm definitions reviewed against what actually fired during steps 6–10.
- [ ] Photos, wiring notes and vendor documents attached as document records.
- [ ] Broker topic-permission verification re-run for the new identity.
- [ ] Backup taken **and restored into a scratch database** to prove it works.
- [ ] A portable archive copied off the property.

**Pass:** someone who was not there could operate and troubleshoot this
subsystem from the record.
**Record:** the baseline export, and the date it was taken.

---

## 3. After the sequence

Confirm the asset reports supervisory control permitted and fully commissioned.

Then enable the bindings that need automatic control — **one at a time, with a
reason**. The platform returns a refusal, not a success, if the prerequisites
have not passed; that refusal is the point of the endpoint.

Then **watch it for a while** before commissioning the next subsystem. The
failure modes that matter usually appear on the second night, not the first
hour.

### One caution before you bulk-enable anything

The field that says a point is trustworthy as a control *input* is the same
field the command path reads as permission to *actuate*. Twelve of the bindings
that set it true are on the energy manager's power-budget points, across all
twelve load groups. Commissioning one of those would flip a field that meant
"trustworthy input" into a grant to command twelve load groups.

**Do not bulk-set bindings to commissioned.** See
[Integration findings F-004](./integration-findings.md#f-004--automatic_control_allowed-is-semantically-overloaded).

---

## 4. Order of subsystems

Each phase depends on the one before it being observable.

| Phase | Subsystems | Platform state |
|---|---|---|
| A | Digital twin foundation, simulated telemetry, first dashboard | **Done** |
| B | Environmental monitor, UPS, PDUs, rack, network gear; inverter, BMS, meter, generator, critical panel; energy state and load tiers; secondary node; black-start test | **Next.** The energy manager and the register are built for this and have never seen it |
| C | Well, tanks, pumps, pressure, flow, leak detection, treatment; weather; long-range radio; soil and compost; one irrigation zone | Water assets and points exist in the design package; no coordinator is built |
| D | Greenhouse, hydrogel lab, nitrogen storage, spa, mower | Not modelled beyond load groups |
| E | Forecasting, prediction, scenario simulation | Not built |

Within Phase B, commission in **dependency order: monitoring before control,
observation before command.** The environmental monitor and the UPS first — they
are read-only, and they are what tells you the container is in trouble. The
battery and inverters last, because they are the ones that can hurt you.

---

## 5. Common failure modes

| What happens | What it usually means |
|---|---|
| Step 2 passes but values look wrong | Unit mismatch between the device and the point dictionary. **Fix the binding, not the dashboard** |
| Step 6 "passes" because nothing alarmed | The platform did not notice the loss. That is a **fail**, not a pass — check the point's stale window |
| Step 7 passes with a sensor reading zero | Zero is plausible. Confirm the **quality model** marks it bad, rather than the range check accepting it |
| Step 9 acknowledgement never arrives | The gateway has no permission to publish acknowledgements. Check the broker log for denied publishes |
| Step 10 notification never arrives | Only the `log` channel is implemented. A log line is not an alert |
| Step 10 alarm never fires | Check the annunciator tile. It may be `OUT OF SVC` — it cannot fire |
| Everything passes in a day | Steps 6, 7, 8 and 11 require actually breaking things. **If nothing was broken, they were not tested** |

---

## 6. What this platform cannot commission for you

The records, gates and endpoints here **track and enforce** the sequence. They
do not perform it. Steps 1, 3, 4, 5, 8 and 11 happen at the equipment, with
tools, by a person, and much of it needs two people.

And the honest position today: **no subsystem in this repository has been
through this sequence against real hardware.** Every path has been exercised
against the simulator and the in-memory bus, which is step 1 and nothing beyond
it. Every `pass` recorded so far would be a `pass` for a simulation.

---

## 7. Related reading

| Document | Why |
|---|---|
| [Control](./control.md) | What the interlocks mean when a step-9 command is refused |
| [Operations § Enabling physical control](./operations.md#7-enabling-physical-control) | The order of operations, and how to revoke |
| [Alarms](./alarms.md) | What step 10 is proving |
| [The design package](./design-package.md) | Where binding addresses come from, and why they are `TBD` |
| [API reference § Maintenance and commissioning](./api.md#10-maintenance-and-commissioning) | The exact request bodies |
