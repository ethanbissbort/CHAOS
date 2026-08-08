# Water-system control narrative

**Status: proposed — awaiting owner ratification and commissioning.**

The full control design for the water system: sources, storage, treatment,
distribution, irrigation, graywater, freeze protection and leak detection.

**Nothing in this document is implemented.** No water coordinator exists in the
platform. The water assets are in the design package but are not merged into the
registry, so they carry no points and no bindings — they appear on
[the topology screen](./topology-view.md#2-the-137-nodes-and-why-the-registry-says-90)
as an overlay and nowhere else. This is a specification to build against and to
argue with, not a description of running software.

**Revision:** v0.4 draft
**Companion documents:** `data/water_assets.yaml`, `data/water_points.yaml`, `schemas/water_assets.schema.json`, `schemas/water_points.schema.json`
**Modelled on:** the design document's energy control narratives

Related: [The design package](./design-package.md) ·
[Control](./control.md) · [Alarms](./alarms.md) ·
[Topology and blast radius](./topology-view.md)

---

## 0. How to read this document

Every threshold, delay, volume, level, pressure and temperature named here is a **commissioning parameter**, not a
decided value. This narrative states *what must be decided, where the decision lives, and what happens once it is
made*. It does not state numbers, because no property has been selected, no well has been drilled, no tank has been
sized and no raw-water analysis exists.

The same discipline applies as in section 30.2 of the design document: the logic is written to be capacity-agnostic,
using proportions, proven states and measured limits rather than hard-coded quantities.

Where a value is genuinely unknown, the companion data documents record it as `TBD` with an entry in the owning
asset's `open_fields`. That is the whole point of the machine-readable package: uncertainty is recorded rather than
disguised as a decision.

---

## 1. Scope

The Water Management System (WMS) supervises:

- The well, well pump, pressure tank and well instrumentation.
- Rainwater collection: first-flush diversion, inlet screening, cisterns and the transfer pump.
- Potable storage, the treatment train (sediment, carbon, disinfection, polishing) and potable quality monitoring.
- The potable pressure pump, service isolation and the emergency reserve isolation valve.
- Irrigation buffer storage, the irrigation pump, filtration, the main isolation valve and orchard zone valves.
- Graywater collection, surge storage, filtration, transfer and the reuse-or-disposal diverter.
- Freeze protection, winter drainage and the seasonal state of every outdoor line.
- Leak monitoring across all of the above.

### 1.1 What the WMS is not

The WMS is not a pump protection relay, a motor starter, a well control panel, a potability certification, or a
plumbing code authority. It issues operating requests and publishes state within the limits enforced by local
controllers and hardwired protection. It never bypasses an interlock.

Specifically:

- **Dry-run, low-source, overcurrent and freeze interlocks are enforced locally**, at the water system controller
  (`water.controller.water_system.01`) or in the motor starter, not in the supervisory platform. SDD section 16.1
  requires that the platform survive complete loss of the power/server container; a pump interlock that lives only
  in a central rule engine does not survive that.
- **Freeze protection is a local function.** `water.controller.freeze_protection.01` is a physically separate
  controller precisely because freezing a distribution main is an unrecoverable, expensive failure that must not
  depend on a server being up.
- **Potability is a laboratory result, not a sensor reading.** Continuous instrumentation raises alarms and inhibits
  distribution; it does not certify water as safe to drink.

### 1.2 Spa pavilion

The hydrothermal spa pavilion (SDD sections 3.4 and 12.8, FR-700 to FR-705) has its own operating modes,
sanitation state machine and heater interlocks. It draws from and returns to the water systems described here, but
its internal control narrative is a separate document. This narrative covers only the supply and return boundary.

---

## 2. Control ownership

| Function | Primary owner | WMS role |
|---|---|---|
| Motor overload, phase loss, locked rotor | Motor starter / VFD | Observe; never bypass |
| Dry-run and low-source-level pump inhibit | Local water controller | Publish permissive state and reason |
| Freeze detection, heat trace, drain sequencing | Freeze-protection controller | Publish season request; observe result |
| Zone maximum runtime and minimum rest | Irrigation controller | Request irrigation; accept refusal |
| Backflow prevention and cross-connection control | Physical devices and plumbing code | No control authority whatsoever |
| Disinfection dose or contact proof | Treatment equipment | Observe `disinfection_valid`; inhibit distribution when false |
| Source selection between well, rainwater and storage | WMS | Authoritative supervisory owner |
| Emergency potable reserve protection | WMS with local valve enforcement | Authoritative supervisory owner |
| Source-to-use accounting and leak inference | WMS | Authoritative supervisory owner |
| Leak isolation valve closure | WMS request, local valve action | Requests only; automatic closure is opt-in |
| Seasonal winterization decision | Operator, with WMS recommendation | Advises and sequences; does not decide alone |
| Energy budget for pumping | EMS | Consumes the published energy state and budget |

The last row matters. Pumping is a deferrable load in most conditions and a Tier 1 essential load in some. The WMS
must request power through the EMS load-budget mechanism (SDD section 31.4) rather than assuming pumping is always
available, and the EMS must not shed water protection loads below the survival floor defined in section 30.3.

---

## 3. Required inputs

### 3.1 Source state

- Well water level and, where instrumented, drawdown behaviour during pumping.
- Well pump run state, discharge pressure, motor current, runtime and start count.
- Well discharge flow, instantaneous and totalised.
- Rainwater cistern levels and rainwater inflow totals.
- Rainfall increment and rainfall since local midnight, from the weather station.
- First-flush diverter position.

### 3.2 Storage state

- Potable storage level, derived volume, and volume above the reserve floor.
- Irrigation buffer level and derived volume.
- Graywater surge level and retention time.
- Liquid temperature in any vessel exposed to freezing.

### 3.3 Treatment and quality state

- Differential pressure across each filter and its throughput total.
- Treatment stage operating state, service-due state and bypass state.
- `disinfection_valid` from the disinfection stage.
- Potable quality parameters as instrumented: turbidity, pH, conductivity, temperature.
- The date and result of the most recent laboratory potability test.

### 3.4 Distribution and use state

- Potable service flow (instantaneous, totalised, and continuous-flow duration) and line pressure.
- Irrigation main flow and pressure, and each zone valve's position and limit switches.
- Graywater reuse flow and diverter position.
- Soil moisture and modelled irrigation demand from the orchard sensor nodes.

### 3.5 Protection and context state

- Leak detector states from the physical leak sensors.
- Line and ambient temperature, and the short-term freeze forecast.
- Scheduled demand: irrigation windows, laundry, spa fill, operator-acknowledged manual draws.
- Occupancy and residence mode.
- Published site energy state and the water subsystem's power budget.
- Active maintenance and lockout states.
- Data quality for every value above.

### 3.6 Data-quality rule

Every input carries a quality code (SDD section 26.6). A permissive derived from a `stale`, `bad` or missing input
evaluates **false**. A protection function derived from a `stale`, `bad` or missing input evaluates **protective**.
The asymmetry is deliberate: losing a level sensor must stop a pump, not authorise it.

---

## 4. Derived values

| Derived value | Description |
|---|---|
| `reserve_volume_l` | Potable volume above the configured emergency reserve floor |
| `reserve_autonomy_d` | Days of essential potable supply at the configured essential demand rate |
| `volume_source_today_m3` | Volume delivered from each source since local midnight |
| `volume_use_today_m3` | Volume consumed by each metered use since local midnight |
| `volume_unaccounted_today_m3` | Source minus use minus storage change over the accounting window |
| `volume_balance_error_pct` | Unaccounted volume as a proportion of metered source volume |
| `water_balance_state` | `balanced`, `drifting`, `unbalanced`, `insufficient_metering`, `unknown` |
| `flow_continuous_duration_min` | How long flow has been continuously above the no-flow threshold |
| `pressure_decay_rate_kpa_min` | Rate of pressure loss in a quiescent line |
| `level_loss_rate_pct_h` | Vessel level loss corrected for known draw, evaporation and thermal contraction |
| `leak_detection_state` | Aggregate leak assessment for a subsystem |
| `freeze_risk_state` | `none`, `watch`, `protect`, `lockout` |
| `starts_per_hour` | Rolling pump start rate, for short-cycle detection |
| `suction_source_available` | Source level or suction pressure valid and above minimum |
| `discharge_path_available` | At least one downstream path open |
| `potable_dispense_permitted` | Combined potability permissive |

As with the EMS (SDD section 30.6), the algorithm must expose the components of each derived value. An operator who
cannot see *why* the platform believes there is a leak will eventually ignore the alarm.

---

## 5. Operating modes

The WMS publishes one site water state, `water_state_site`. Subsystems translate it into their own bounded
profiles, exactly as subsystems translate the EMS energy state.

| State | Intent | Typical entry basis | Typical exit basis |
|---|---|---|---|
| `COMMISSIONING` | Point verification and controlled testing | Explicit operator selection | Operator completion |
| `MAINTENANCE` | Prevent autonomous transitions during work | Explicit lockout or maintenance window | Authorized release |
| `SURPLUS` | Fill storage and run deferrable water work | Storage high, source healthy, energy surplus | Storage full or energy margin falls |
| `NORMAL` | Full normal operation | Healthy reserve, valid quality, no faults | Reserve, quality or equipment deteriorates |
| `CONSERVE` | Reduce discretionary water use | Reserve declining, source yield poor, or dry forecast | Sustained recovery |
| `CRITICAL_RESERVE` | Protect the emergency potable reserve | Reserve floor approached, or source unavailable | Sustained refill above the recovery threshold |
| `WINTERIZED` | Outdoor systems drained and locked out | Seasonal transition completed | Operator-authorized spring refill |
| `EMERGENCY` | Contamination, major leak or total source loss | Contamination event, confirmed major leak, source failure | Manual reset after cause removed |
| `DEGRADED_SENSOR` | Operate conservatively with impaired observability | A required input is invalid without needing shutdown | Data quality restored or operator action |

`MAINTENANCE`, `WINTERIZED`, `EMERGENCY` and `DEGRADED_SENSOR` override normal dispatch. A single level threshold is
insufficient for state selection: the machine must consider available source yield, treatment validity, energy
availability, freeze risk and the validity of the measurements themselves.

### 5.1 Mode profiles

Each subsystem declares what it does in each state. Preliminary intent:

| Subsystem | `SURPLUS` | `NORMAL` | `CONSERVE` | `CRITICAL_RESERVE` |
|---|---|---|---|---|
| Potable supply | Normal | Normal | Normal, with usage advisory | Essential draw only; reserve valve protects the floor |
| Irrigation | Full schedule, deficit make-up | Scheduled by soil moisture and forecast | Reduced to crop-survival watering | Suspended |
| Graywater reuse | Full reuse | Reuse when quality permits | Reuse preferred over potable | Reuse only, or divert to disposal |
| Cistern transfer | Fill irrigation buffer | Fill on demand | Fill only from surplus rainwater | Suspended unless it serves potable |
| Spa fill | Permitted | Permitted | Deferred | Blocked |
| Storage top-up from well | Aggressive | Scheduled | Scheduled, energy-aware | Priority over all other draws |

### 5.2 Hysteresis and dwell

- Every automatic state transition has a qualification delay.
- Recovery thresholds are separated from entry thresholds.
- Pumps have minimum on and minimum off times, and a maximum starts-per-hour limit.
- A refill event must be sustained before the state leaves `CONSERVE` or `CRITICAL_RESERVE`.
- A single tank-level reading crossing a threshold must not transition the site state; wave action, thermal
  expansion and sensor noise all produce brief excursions.

---

## 6. Pump interlocks (FR-203)

> **FR-203:** Inhibit pumps on dry-run, low-source level, freeze lockout, overcurrent, or unavailable discharge path.

Every pump in `data/water_assets.yaml` carries the same five interlocks in its `properties.interlocks` list. They are
enforced by the local controller. The platform observes them and explains them; it does not implement them.

### 6.1 The five inhibits

| Inhibit | Evidence | Latching | Point |
|---|---|---|---|
| Dry run | No flow proven within the configured time after start, or suction pressure below minimum while running | Latched | `dry_run_active` |
| Low source level | Source level or suction pressure below `minimum_source_level_pct` | Non-latching, with hysteresis | `suction_source_available` |
| Freeze lockout | `freeze_risk_state` is `lockout`, or the served line is not proven drained when it should be | Non-latching, seasonal | `freeze_lockout_active` |
| Overcurrent | Motor current above the configured limit for the configured time | Latched | `overcurrent_active` |
| Unavailable discharge path | No downstream valve open, or discharge pressure above the deadhead limit | Non-latching | `discharge_path_available` |

### 6.2 Start permissive

A pump may start only when **all** of the following are true:

1. `interlock_permissive` is true at the local controller.
2. `suction_source_available` is true.
3. `discharge_path_available` is true.
4. `freeze_lockout_active` is false.
5. `pump_lockout_active` is false.
6. `starts_per_hour` is below the configured limit and the minimum off time has elapsed.
7. The subsystem holds a power budget sufficient for the pump, or the site energy state permits the load tier.
8. Every input above has acceptable quality.

If any is false, `interlock_block_reason` names the **first** failing condition in this order, so the operator sees a
single actionable cause rather than a list.

### 6.3 Running protection

Once running:

- Flow must be proven within the configured proving time, or the pump stops and latches `dry_run_active`.
- Loss of proven flow while running stops the pump.
- Motor current above the limit for the configured time stops the pump and latches `overcurrent_active`.
- Continuous run beyond `maximum_runtime_min` stops the pump and raises an alarm; a pump that never stops is
  either serving a leak or has lost its level feedback.
- Deadhead operation (discharge pressure high with no flow) stops the pump.

### 6.4 Lockout and reset

Latched conditions set `pump_lockout_active` with `pump_lockout_reason`. A lockout clears only on
`pump_lockout_reset_request` from an operator or an authorised supervisory action, never automatically. Self-clearing
protection hides the fault that caused it, and a well pump that restarts itself into a dry well destroys itself
slowly enough that nobody notices until it fails.

---

## 7. Leak detection (FR-202)

> **FR-202:** Detect probable leaks using unexpected flow, pressure decay, tank-level loss, and occupancy/schedule
> context.

### 7.1 Four independent lines of evidence

1. **Unexpected flow.** Flow is present at the potable service or irrigation main while `demand_scheduled_active` is
   false and no operator-acknowledged draw is in progress. Sets `flow_unexpected_active`.
2. **Continuous flow.** `flow_continuous_duration_min` exceeds the configured limit for that meter. A fixture is
   used for minutes; a leak runs for hours.
3. **Pressure decay.** With the line quiescent, `pressure_decay_rate_kpa_min` exceeds the configured limit. This is
   the only test that finds a leak while nothing is flowing through a meter.
4. **Vessel level loss.** `level_loss_rate_pct_h` exceeds the configured limit after correction for metered draw,
   evaporation and thermal contraction.

Physical leak sensors (`safety.safety_sensor.water_service.leak_01`,
`safety.safety_sensor.water_treatment.leak_02`) are a fifth, independent input. A wetted detector is direct
evidence and outranks the inference model; the absence of a wetted detector is not evidence of no leak, because a
buried line leak never reaches a floor sensor.

### 7.2 State machine

```text
normal ──(one line of evidence, qualification delay)──▶ suspected
suspected ──(second independent line, or wetted detector)──▶ confirmed
suspected ──(evidence clears for the recovery period)──▶ normal
confirmed ──(isolation valve closed and flow stops)──▶ isolated
confirmed ──(operator acknowledges as expected use)──▶ suppressed
isolated ──(operator repair and reset)──▶ normal
```

`leak_evidence_summary` carries the human-readable list of what fired. An operator must be able to see the evidence
before acting on it.

### 7.3 Context suppression

The following must not be reported as leaks:

- An active irrigation zone, matched to the expected flow signature for that zone.
- A scheduled or acknowledged manual draw: laundry, spa fill, livestock watering, vehicle washing, hose use.
- A cistern-to-buffer transfer in progress.
- A commissioning or maintenance activity with an active lockout.

Suppression is **contextual, not permanent**. A suppressed condition still logs, and suppression expires with the
context that created it.

### 7.4 Response

| `leak_detection_state` | Response |
|---|---|
| `suspected` | Warning alarm; log evidence; no automatic action |
| `confirmed`, minor | Major alarm; recommend isolation; require operator decision |
| `confirmed`, major | Critical alarm; request isolation if automatic isolation is enabled; notify by every configured path |
| `isolated` | Critical alarm persists until repaired and reset; potable supply is unavailable |

**Automatic isolation is opt-in.** `leak_isolation_requested` has `automatic_control_default: false` in the point
dictionary extension. Automatically removing water from an occupied residence — in winter, at night, while the
occupants are away and the heating depends on wet systems — is an owner policy decision made at commissioning, not
a platform default. The point exists so the capability is defined; enabling it is a separate act.

---

## 8. Winterization (FR-204)

> **FR-204:** Support winterization state for outdoor lines and equipment.

### 8.1 Freeze risk state

`freeze_risk_state` is derived from line temperature, ambient temperature and the short-term forecast:

| State | Meaning | Action |
|---|---|---|
| `none` | No freeze risk | Normal operation |
| `watch` | Forecast approaching the freeze threshold | Advisory; prepare for protection |
| `protect` | Line temperature approaching freezing | Heat trace energized; circulation or drip protection where designed |
| `lockout` | Freeze conditions present | `freeze_lockout_active` inhibits pump and valve operation on affected lines |

### 8.2 Winterization state machine

```text
summer ──(operator or seasonal request)──▶ transition_to_winter
transition_to_winter ──(isolate, then open drains)──▶ draining
draining ──(drain proven)──▶ winterized
winterized ──(operator spring request)──▶ refilling
refilling ──(pressure held, no unexpected flow)──▶ summer
```

### 8.3 Winterize sequence

1. Confirm no irrigation zone or outdoor draw is active; stop and lock out any that are.
2. Close the irrigation main isolation valve.
3. Stop and lock out every pump serving an outdoor line.
4. Open the winter drain valves.
5. Verify drain-down: `drain_complete` requires **proof**, not merely a commanded valve position.
6. Set `winterization_state` to `winterized` and hold `freeze_lockout_active` on all affected assets.
7. Record what was drained and what was not, so spring recommissioning knows the state it is recovering from.

### 8.4 Spring refill sequence

1. Confirm `freeze_risk_state` is `none` for a sustained period.
2. Close drain valves and confirm closed limits.
3. Pressurise slowly through the main isolation valve, watching flow and pressure.
4. Hold pressure with all zones closed and verify no decay. A line that froze and split announces itself here.
5. Open zones one at a time, verifying each zone's flow signature.
6. Set `winterization_state` to `summer` only after every zone has been proven.

### 8.5 Fail-safe positions

Winter drain valves fail **open to drain**. Loss of power or control leaves the line draining rather than frozen and
full. This is recorded as `fail_position: fail_open_to_drain` in the asset records, and it is the opposite of the
usual fail-closed convention for a reason: a drained line is an inconvenience, a burst main is a rebuild.

### 8.6 What is not decided

Freeze thresholds, drain slopes, drain discharge points, heat-trace circuit layout, whether heat trace exists at
all, and the proof method for `drain_complete` are all open. They depend on frost depth, burial depth and pipe
routing at a property that has not been selected.

---

## 9. Emergency potable reserve (FR-205)

> **FR-205:** Preserve a configurable emergency potable-water reserve.

### 9.1 Model

The reserve is defined the same way the EMS defines battery reserve (SDD section 30.6):

```text
reserve_volume_l  = potable volume available  −  reserve_floor_l
reserve_autonomy_d = reserve_floor_l ÷ essential_demand_l_d
```

`reserve_floor_l` and `essential_demand_l_d` are `CFG` points with no value. They depend on household size,
realistic resupply time and the owner's risk tolerance, none of which the design package knows.

### 9.2 Protection

When available volume approaches the floor:

1. `reserve_protection_active` is set and the site water state moves to `CRITICAL_RESERVE`.
2. Discretionary draws — irrigation, spa fill, vehicle washing, bulk transfers — are suspended.
3. The well pump and any available source top-up gains priority over every other water and deferrable energy task.
4. The emergency reserve isolation valve isolates discretionary distribution; it does not stop essential supply.
5. If the source cannot refill, the operator is told **how many days remain**, not merely that a tank is low.

### 9.3 Interaction with energy

The reserve is only meaningful if it can be pumped. During an extended energy shortage the WMS must:

- Declare well pumping an essential load while the reserve is below target, so the EMS does not shed it.
- Prefer filling potable storage during PV surplus, converting surplus energy into stored water.
- Report clearly when the water reserve is intact but *unavailable* because pumping cannot be powered. A full tank
  and a dead pump is not a reserve.

### 9.4 Interaction with potability

`potable_dispense_permitted` is false when treatment cannot be proven, quality data is stale or bad, or an
unresolved contamination event exists. **A reserve of unpotable water is not a potable reserve.** The reserve
accounting must therefore report treated, dispensable volume, not merely stored volume.

---

## 10. Source-to-use accounting (FR-201)

> **FR-201:** Maintain source-to-use accounting for well, rainwater, potable, irrigation, greenhouse, graywater and
> spa water.

### 10.1 Balance

Over an accounting window:

```text
Σ metered source volume
  − Σ metered use volume
  − net storage change
  = volume_unaccounted_today_m3
```

`volume_balance_error_pct` expresses the residual as a proportion of metered source volume, and
`water_balance_state` classifies it.

### 10.2 Honest classification

`water_balance_state` includes `insufficient_metering` deliberately. Where a source or use has no meter, the
balance cannot close, and the platform must say so rather than report `balanced`. A balance that closes only because
half the terms are missing is worse than no balance at all, because it is trusted.

### 10.3 Metering coverage

The water asset extension defines meters on the well discharge, the rainwater inflow, the potable service, the
irrigation main and the graywater reuse line. Greenhouse and spa draws are **not** metered by this extension; their
meters belong to the greenhouse and spa packages. Until those exist, the site balance carries an unmetered term and
`water_balance_state` reports `insufficient_metering`.

### 10.4 Uses of the balance

- Long-run drift in `volume_balance_error_pct` is slow-leak evidence that no single instantaneous test finds.
- Per-source totals show whether rainwater is actually displacing well draw, which is the design intent.
- Per-zone irrigation volumes support the FR-302 low-flow and high-flow fault detection.
- Seasonal source totals are the only evidence that will ever exist about sustainable well yield.

---

## 11. Alarms

Severity follows SDD section 14.1. Every alarm links to a procedure, the affected assets, dependencies and the
manual controls that remain available (FR-008).

| Alarm | Preliminary severity | Trigger basis |
|---|---|---|
| `water_pump_dry_run` | Major | Flow not proven after start, or lost while running |
| `water_pump_overcurrent` | Major | Motor current above limit for the configured time |
| `water_pump_lockout` | Major | Latched lockout active |
| `water_pump_short_cycling` | Warning | `starts_per_hour` above limit |
| `water_source_level_low` | Warning/Major | Source level below the pump-start minimum |
| `water_source_unavailable` | Critical | No source can supply potable storage |
| `water_leak_suspected` | Warning | One line of leak evidence, qualified |
| `water_leak_confirmed` | Critical | Two independent lines, or a wetted detector |
| `water_leak_isolated` | Critical | Isolation valve closed on a confirmed leak |
| `potable_quality_invalid` | Critical | Quality outside limits, or required data stale/bad |
| `disinfection_not_proven` | Critical | `disinfection_valid` false while distribution is open |
| `treatment_bypass_active` | Major | A treatment stage is bypassed |
| `filter_service_due` | Warning | Differential pressure or throughput past the service threshold |
| `potable_reserve_low` | Major | Available volume approaching `reserve_floor_l` |
| `potable_reserve_breached` | Critical | Available volume below `reserve_floor_l` |
| `freeze_risk_protect` | Warning | `freeze_risk_state` is `protect` |
| `freeze_lockout_active` | Major | Freeze lockout inhibiting operation |
| `winterization_drain_not_proven` | Major | Drain commanded but `drain_complete` false |
| `irrigation_zone_no_flow` | Warning | Zone commanded open with no expected flow |
| `irrigation_zone_overflow` | Major | Flow continues after the zone valve is commanded closed |
| `water_balance_unaccounted` | Warning | `volume_balance_error_pct` above limit over the window |
| `water_data_invalid` | Major | A value required by the state machine is stale or bad |

### 11.1 Events

Recorded but not alarms: water state transitions, source selection changes, pump start/stop with cause, zone
irrigation start/stop with volume, winterization state changes, filter and treatment service actions, leak-state
transitions with evidence, reserve threshold changes, operator suppression of a leak condition, laboratory test
results.

---

## 12. Verification and commissioning cases

No pump, valve or isolation function goes under automatic supervisory control until its local control and failure
modes have been tested (SDD section 19). Each test record must include preconditions, injected condition, expected
sequence, actual telemetry, commands, alarms, operator observations and pass/fail.

| Test ID | Test | Expected result |
|---|---|---|
| `WMS-T001` | Lose internet while the site is normal | No control loss; local dashboards and alerting remain functional |
| `WMS-T002` | Stop the primary digital-twin host | Local water and freeze controllers continue; secondary node reports reduced status |
| `WMS-T003` | Stale tank-level input | State machine enters `DEGRADED_SENSOR`; does not assume a healthy reserve |
| `WMS-T004` | Isolate pump suction and start the pump | Flow not proven; pump stops within the proving time; `dry_run_active` latches |
| `WMS-T005` | Drive source level below the configured minimum | Start is inhibited; `interlock_block_reason` names low source level |
| `WMS-T006` | Close all discharge paths and request pump start | Start inhibited; no deadhead operation occurs |
| `WMS-T007` | Inject simulated overcurrent | Pump stops; lockout latches; no automatic restart occurs |
| `WMS-T008` | Attempt reset with the cause still present | Reset refused; reason unchanged |
| `WMS-T009` | Open a tap for longer than the continuous-flow limit | `leak_detection_state` reaches `suspected`; evidence summary names continuous flow |
| `WMS-T010` | Run a scheduled irrigation zone | No leak state is raised; the zone flow matches its expected signature |
| `WMS-T011` | Introduce a controlled pressure decay with the line quiescent | Decay evidence fires within the qualification delay |
| `WMS-T012` | Wet a physical leak sensor | `leak_detection_state` moves to `confirmed` regardless of the inference model |
| `WMS-T013` | Confirm a leak with automatic isolation disabled | Alarm and recommendation only; no valve moves |
| `WMS-T014` | Confirm a leak with automatic isolation enabled | Valve closes; flow stops; alarm persists until reset |
| `WMS-T015` | Draw potable storage toward the reserve floor | Discretionary draws suspend; site state reaches `CRITICAL_RESERVE`; autonomy in days is reported |
| `WMS-T016` | Invalidate disinfection while distribution is open | `potable_dispense_permitted` false; critical alarm; distribution inhibited |
| `WMS-T017` | Simulate freeze conditions on an outdoor line | `freeze_risk_state` reaches `lockout`; affected pumps and valves inhibit |
| `WMS-T018` | Execute the winterize sequence | Zones stop, main closes, drains open, `drain_complete` proven before `winterized` is set |
| `WMS-T019` | Remove power from a winter drain valve | Valve fails open to drain |
| `WMS-T020` | Execute the spring refill sequence | Pressure holds with zones closed; each zone proves individually before `summer` is set |
| `WMS-T021` | Remove a source meter from service | `water_balance_state` reports `insufficient_metering`, not `balanced` |
| `WMS-T022` | Enter energy `CRITICAL_RESERVE` with the water reserve below target | Well pumping is preserved as essential; the conflict is reported, not silently resolved |
| `WMS-T023` | Full loss of the power/server container | Freeze protection and pump interlocks continue; alerting originates independently |
| `WMS-T024` | Change a commissioning threshold | Revision, author, old and new values, and approval are recorded |

---

## 13. Open items

These must be resolved before the narrative can be parameterised. They are recorded in
`data/water_assets.yaml` under each asset's `open_fields` and in `design_basis.unresolved`.

1. Property location, terrain, aquifer, frost depth and climate. Every water decision depends on these.
2. Well depth, static level, drawdown behaviour and sustainable yield, from a driller's log.
3. Raw-water chemistry, from a laboratory analysis. This determines the entire treatment train.
4. Roof and catchment areas plus the local rainfall record, which determine cistern sizing.
5. Household and irrigation demand, which determine the reserve floor and pump duty.
6. Pipe routing, burial depth and line slope, which determine what can actually be drained.
7. Whether automatic leak isolation is enabled, and for which valves.
8. Graywater reuse permissions in the final jurisdiction.
9. Every numeric threshold in this document.
10. The controller family for local water and freeze control — see `docs/design-decisions/DD-004-controller-family.md`.

---

## 14. Related reading

| Document | Why |
|---|---|
| [The design package](./design-package.md) | Why the water documents are extensions, and what their status means |
| [Topology and blast radius](./topology-view.md) | Losing the power container costs 22 water assets, including potable pressure |
| [Control](./control.md) | The interlock and operating-mode machinery this narrative would run on |
| [Alarms](./alarms.md) | The alarm model section 11 is written against |
| [Design decision DD-004](./design-decisions/DD-004-controller-family.md) | The controller family every local water loop depends on. Status: *proposed* |
