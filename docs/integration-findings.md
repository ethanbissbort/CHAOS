# Integration Findings

Gaps found by running the whole platform end to end — the simulated homestead
publishing real envelopes on real topics, ingest resolving them through the
registry, and the EMS evaluating the resulting state.

Every item here is a gap in the **design package**, not a platform defect. In
each case the software behaved correctly by refusing to guess. They are
recorded rather than silently patched because resolving them is a design
decision belonging to the property owner, and because SDD sections 3.1 and 45
are explicit that conflicts are preserved rather than quietly reconciled.

`tests/test_end_to_end.py` asserts that no *new* failure category appears, so
this list cannot grow without someone noticing.

---

## F-001 — Five asset classes cannot represent a device going silent

**Severity: high — affects safety monitoring**

`availability_state` exists in the point dictionary, but these asset classes do
not include it in their `default_points`, and the affected assets do not
reference the `common_monitored` profile that would supply it:

| Class | Affected assets |
|---|---|
| `load` | the 12 `energy.load.site.*` groups |
| `panel` | `critical_01`, `general_01` |
| `rack` | `it.rack.power_container.01` |
| `safety_sensor` | 4 NetBotz smoke/leak/temperature sensors |
| `alarm_output` | `safety.alarm_output.rack_01.beacon_01` |

For the load groups this is arguably correct — they are EMS abstractions, not
devices with their own gateway, so there is nothing to be unreachable.

**For the safety sensors it is not.** A smoke, leak or temperature sensor that
stops reporting is currently indistinguishable from one reporting "no alarm."
SDD 5.5 requires a defined communications-loss state for every controlled
output, and SDD 8.2 specifies last-will availability per device. A silent leak
sensor in the battery container is exactly the failure this platform exists to
catch.

**Recommendation:** add `availability_state` (and `heartbeat_age_s`) to
`safety_sensor`, `alarm_output`, `panel` and `rack`, or give those assets the
`common_monitored` profile. Leave `load` as-is and stop the simulator
publishing availability for logical groups.

**Do not** resolve this by making ingest accept availability for points that do
not exist — that would hide the gap rather than close it.

---

## F-002 — `command_last_result` has no idle value

**Severity: low**

The dictionary defines `command_last_result` with
`enum_values: [accepted, rejected, completed, failed, expired]`. There is no
value meaning "no command has been issued yet," so an asset that has never been
commanded has nothing valid to publish. The simulator emits `"none"`, which
ingest correctly rejects as an enum violation and records as `bad` quality.

**Recommendation:** either add an idle value (`none` / `never_issued`) to the
enum as a `reconciled_v0_4` entry, or have devices publish nothing for this
point until a command produces a result. The second is cleaner — absence of a
value already means "no result."

---

## F-003 — `energy_state` is published but not defined

**Severity: medium — produces recurring noise**

The EMS publishes the site energy state to
`homestead/site/primary/site_01/energy_state` every tick (SDD section 13
requires the EMS to publish a state that subsystems consume). `energy_state` is
not in `data/point_dictionary.yaml`, so ingest dead-letters it on every
evaluation.

Nothing breaks, but a steady stream of dead letters is exactly the noise that
masks genuine commissioning errors — which is the one thing the dead-letter
queue exists to make visible.

**Recommendation:** define `energy_state` as a `CALC` point with the ten SDD
30.7 state names as its enum, via an extension document following the
`water_points.yaml` pattern, and give the site asset a profile that includes it.

---

## F-004 — `automatic_control_allowed` is semantically overloaded

**Severity: medium — latent safety consequence**

SDD section 29 defines `automatic_control_allowed` as "may be used for
automatic control," i.e. whether a point is trustworthy as an *input* to
control decisions. The command subsystem's `binding_not_commissioned` interlock
reads the same field as permission to *actuate*.

86 of 245 bindings set it true. Twelve of those are on `power_budget_kw`, which
is genuinely control-capable — on all twelve EMS load groups.

Today nothing can be actuated: every binding is `binding_status: tbd`, and
`allow_physical_control` defaults to false, so three independent gates hold.
But commissioning a `power_budget_kw` binding would flip a field that was set
to mean "trustworthy input" into a grant to command twelve load groups.

**Recommendation:** split the concept into two fields — `usable_as_control_input`
and `actuation_permitted` — and have the interlock read only the latter. Until
then, do not bulk-set `binding_status: commissioned`.

---

## F-005 — Asset-ID schema is looser than the identification standard

**Severity: low — latent**

SDD section 25.2 specifies exactly four components:
`<domain>.<asset_class>.<location_or_system>.<instance>`. The JSON Schema
permits `{3,}`, so a five-part ID validates but has no deterministic MQTT
projection.

All 137 current assets (90 baseline + 47 water) are four-part, so nothing is
broken. Both the registry loader and the command manager fail safe: the loader
warns and stores the binding without a topic, and the command manager refuses
the command rather than guessing.

**Recommendation:** tighten the schema pattern to `{3}` to match the standard,
or amend section 25.2 if deeper hierarchies are genuinely wanted.

---

## F-007 — Ten of the forty alarms can never fire

**Severity: high — alarms that look configured and are not**

*Widened from a single alarm after the annunciator panel (`GET /api/v1/annunciator`)
computed serviceability for every definition. `battery_cell_imbalance` was the
first case found; it is one of ten.*

Each of these is **enabled**, appears healthy in the alarm list, and has a
trigger point that does not exist for its asset. None can ever raise.

| Alarm | Severity | Missing trigger point |
|---|---|---|
| `rack_smoke_detected` | critical | `smoke_active` on the NetBotz sensor |
| `power_container_water_ingress` | critical | `leak_active` on the container sensor |
| `power_container_ac_bus_lost` | critical | `energized_state` on the AC main |
| `alarm_beacon_unavailable` | major | `availability_state` on the beacon |
| `battery_cell_imbalance` | major | `cell_voltage_delta_mv` on the bank |
| `inverter_overload_risk` | major | `power_ac_kw` on all 4 inverters |
| `rack_door_forced_open` | major | `forced_open_active` on the controller |
| `rack_door_open_extended` | warning | `door_state` on the controller |
| `storage_capacity_high` | warning | `storage_used_pct` on all 3 servers |
| `time_sync_drift` | warning | `clock_offset_ms` on all 3 servers |

**The whole SAFETY bay is dark and none of it works.** All three safety alarms
are in this list. Smoke detection and water ingress in the 20-foot container
that holds the batteries, the power conversion equipment *and* the server rack
cannot raise an alarm — and that container is the common-mode failure domain
SDD section 16.1 is explicitly written about. The beacon alarm that would report
the local siren dead is also inoperable, so the independent alerting path
section 16.1 relies on has no health check either.

An alarm list showing "40 defined, 0 active" reads as full coverage. A quarter
of it is incapable of firing.

**Recommendation:** add the ten trigger points to their asset classes or
bindings. Until then the platform reports them as `out_of_service` on the
annunciator rather than dark, which is honest but is not coverage.

---

## F-008 — Losing the power container takes 71% of the homestead, including potable water pressure

**Severity: high — the common-mode domain is larger than the design assumes**

*Found by the topology view's blast-radius analysis (`GET /api/v1/topology/impact/{asset_id}`),
which walks the same relationship graph the alarm correlator uses. Verified
directly against the register.*

SDD section 16.1 instructs the design to assume complete loss of the combined
power, battery, utilities and server container. Computing that loss gives:

| | |
|---|---|
| Assets lost | **98 of 137 (71%)** |
| Of which `critical` criticality | **50** |
| Survivors | 38 |

Section 16.1 is not being conservative. It is barely adequate.

**The actionable part is the water system.** 21 water assets survive — the well,
the freeze-protection controller, both winter drain valves, the field pipework.
But **22 do not**, and the path to them is only four hops:

```
power container → power zone → critical loads panel
                → water pumping load → potable pressure pump
```

**Lose the container and you lose potable water pressure**, even though the
well, the tank and the pipework are all outside it and physically unharmed.
That is a single electrical dependency defeating an otherwise independent
subsystem, and it is exactly the kind of coupling section 16.1 exists to find.

**Two safety assets die with the thing they exist to watch:**
- the fluid-detection sensor monitoring for flooding *in that container*
- `safety.alarm_output.rack_01.beacon_01`, the local alarm beacon that
  `alarms/correlation.py` deliberately exempts from incident folding so it can
  always speak for itself. It cannot speak if it is inside the fire.

**The dependency structure is top-heavy, and the top is spatial rather than
electrical**: 19 single assets each take out 30 or more others, while 73 take
out nobody. The server zone (67) and the rack (65) outrank the battery bank
(40) — containment, not power, is the dominant coupling.

**Recommendation:** decide whether potable water pressure should survive the
container. If it should, it needs a supply path that does not pass through the
critical loads panel. Separately, relocate the container's own leak sensor and
the alarm beacon outside the failure domain they report on.

---

## F-009 — The register cannot express N+1 redundancy

**Severity: medium — it makes every redundancy claim unverifiable**

There is no way in the asset register to say that a set of devices is a
redundant group. The four hybrid inverters are modelled as four separate assets
each feeding the same AC combiner, so a failure of any one reads as a full
outage of everything downstream — a blast radius of 37 assets apiece. The same
applies to the paired storage arrays.

The traversal is correct; the model is missing a concept. The impact endpoint
reports this in its own `caveats` rather than quietly overstating the loss, but
that is mitigation, not a fix.

This compounds F-007's rack finding: the register also could not express that
the dual-fed devices' two cords land on one PDU. In both cases the platform can
only reason about redundancy that the data model is capable of stating.

**Recommendation:** add a redundancy grouping to the asset schema — a group id
plus an `n_plus` degree — so that "four inverters, N+1" and "two cords, one
PDU" are both statable and checkable.

---

## F-007a — `battery_cell_imbalance` (the original case)

**Severity: high — an alarm that looks configured and is not**

SDD section 37.1 lists `battery_cell_imbalance` as a core energy alarm, and
`data/alarm_definitions.yaml` defines it as **enabled**, against
`energy.battery_bank.power_container.01`, triggering on `cell_voltage_delta_mv`.

That point exists in the point dictionary but is **not reachable for the
battery bank**: it is absent from the `battery_bank` class `default_points`,
from every profile the asset references, and from all 245 point bindings. The
alarm therefore has no possible input and will never raise, while appearing
healthy and enabled in the alarm list.

This is worse than a missing alarm. A missing alarm is visibly missing; this
one reads as covered.

Cell imbalance is a genuine failure mode for a large LiFePO4 bank — it is an
early indicator of a failing cell or module, and the design package leaves
chemistry and module count unresolved, so the bank's construction is not yet
known.

**Recommendation:** add `cell_voltage_delta_mv` (and probably
`temperature_cell_min_c`, `voltage_dc_v`, `current_dc_a`) to the `battery_bank`
class default points and give it a binding. Until then, mark the alarm disabled
with a note, so the alarm list does not overstate coverage.

**Related, same root cause — points an alarm or the EMS wants but cannot reach:**

| Point | Asset | Consequence |
|---|---|---|
| `generator_available`, `generator_start_request` | `energy.generator.site.01` | SDD 30.5 lists generator availability as a required EMS input |
| `energized_state` | `energy.panel.*` | black-start bus verification is not directly observable |
| `battery_pct` | `energy.ups.rack_01.01` | UPS charge state has nowhere to publish |
| `power_w` | `it.switch.*`, `it.router.*` | per-device rack draw is not observable |

---

## F-006 — Load tiers are one-based in the register, zero-based in the SDD

**Severity: medium — an off-by-one here decides what gets shed**

SDD section 31.2 numbers load tiers from **Tier 0** (physical protection and
control survival, never shed). The asset register's `load_tier` property is
**one-based**, so Tier 0 appears as `load_tier: 1`.

`data/load_schedule.yaml` records the conversion (`base_tier = load_tier − 1`)
in its notes and the EMS uses the zero-based value throughout.

This is worth an explicit confirmation because the consequence of getting it
wrong is not cosmetic: an off-by-one would place freeze protection and BMS
control power in a sheddable tier.

**Recommendation:** confirm the intended base, then make the register's
property name unambiguous (`load_tier_1based`, or restate it zero-based).
