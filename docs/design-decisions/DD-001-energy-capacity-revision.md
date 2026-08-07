# DD-001: Authoritative energy design revision

**Status:** proposed — awaiting owner ratification
**Date raised:** 2026-08-07
**Decision owner:** homestead owner
**SDD reference:** §3.1, §22 item 1, §30.2, §46.1; `design_basis.open_design_conflicts["energy.capacity_revision"]`
**Blocks:** PV, inverter, battery, generator and container procurement; the load schedule's reserve assumptions; every EMS threshold; all energy CAPEX in `data/asset_lifecycle.yaml`

---

## Context

The project portfolio contains **more than one** energy design, and the design document is explicit that the
discrepancy "must be formally resolved and versioned rather than silently overwritten" (§3.1).

### The revised design (currently used by the register)

From SDD §3.1 and §30.2, and encoded in `data/homestead_asset_register.yaml`:

| Parameter | Value | Where it comes from |
|---|---|---|
| PV capacity | ~45 kWdc | §3.1; register `energy.pv_array.agrivoltaic_field.01.properties.rated_kwdc` |
| Array composition | 100 modules × 450 W bifacial, 4 rows × 25 modules, 11.25 kW per row | Agrivoltaic field diagram, via the register |
| Inverters | Four × ~10 kW hybrid, ~40 kW continuous | §3.1; four `energy.inverter.power_container.0n` functional positions |
| Battery nominal | ~800 kWh | §3.1; register `energy.battery_bank.power_container.01` |
| Battery usable | ~640 kWh planning capacity | §3.1 |
| Critical baseload target | 1.5 kW average | §3.1, §30.2 |

The register uses this design "because that is the current design basis in v0.2 and the agrivoltaic field diagram"
(§46.1). It is the working assumption, not a ratified decision.

### The predecessor design

The register records the predecessor as **26.4 kWdc PV, two × 10 kW inverters, 240 kWh usable LiFePO4** (§46.1;
`design_basis.open_design_conflicts`), sourced from the *35 MWh-yr Solar and Battery Design* diagram.

### The complication: the predecessor is itself recorded two different ways

This is the part a reviewer needs to see before deciding anything. The design document narrative describes the older
baseline as **12 kW PV / 40 kWh usable** in three separate places:

- §3.1: "The project records also contain an older 12 kW PV / 40 kWh usable battery baseline."
- §22 item 1: "older 12 kW / 40 kWh baseline or revised 45 kWdc / 800 kWh nominal design".
- §30.2: "Earlier baseline: approximately 12 kW PV and 40 kWh usable storage."

But §46.1 and the register's conflict record describe the predecessor as **26.4 kWdc / 240 kWh**.

So there are three capacity figures in circulation, not two:

| Design | PV | Inverters | Battery | Source |
|---|---|---|---|---|
| Revised | ~45 kWdc | 4 × ~10 kW | 800 kWh nominal / 640 kWh usable | §3.1, agrivoltaic diagram |
| Predecessor A | 26.4 kWdc | 2 × 10 kW | 240 kWh usable LiFePO4 | *35 MWh-yr Solar and Battery Design* diagram, §46.1 |
| Predecessor B | 12 kW | not stated | 40 kWh usable | §3.1, §22.1, §30.2 narrative |

The ratio between the revised design and Predecessor B is roughly 3.75× on PV and **20× on storage**. That is not a
refinement; it is a different property. Resolving DD-001 therefore requires first establishing whether Predecessor A
and Predecessor B are two records of the same earlier design, two successive earlier designs, or one of them is a
transcription error. Ratifying a capacity while that is unclear would bury the ambiguity instead of removing it.

### Why it matters beyond procurement

- **Container sizing and fire separation.** 800 kWh and 240 kWh are different buildings. §16.1 already flags the
  combined battery-and-server container as a common-mode risk; the battery mass is what makes it one.
- **Inverter count changes the asset graph.** The register carries four inverter functional positions. Two of them
  are load-bearing only under the revised design.
- **EMS thresholds are capacity-relative but not capacity-free.** §30.2 writes the narrative in percentages
  deliberately, but reserve floors, generator-start margins and autonomy horizons are commissioned against real
  capacity.
- **Generator sizing follows the battery.** A 640 kWh usable bank with a good forecast may need a generator only for
  rare deep-winter events; a 40 kWh bank needs one routinely. §34's start criteria change character entirely.
- **CAPEX.** `data/asset_lifecycle.yaml` lists this decision as a known blocker: no PV, inverter or battery cost
  line can be correct while the capacity is unresolved.

---

## Options

### Option 1 — Ratify the revised 45 kWdc / 800 kWh design

**For**

- It is the design the agrivoltaic field diagram supports, with a module count and row layout that reconcile
  arithmetically (100 × 450 W = 45 kW; 4 × 25 × 450 W = 45 kW).
- It is what the register, the load schedule and the EMS narrative already assume, so nothing needs rework.
- 640 kWh usable against a 1.5 kW critical baseload gives very long critical autonomy, which is the point of an
  off-grid homestead that must survive extended low-solar periods.
- It leaves headroom for the loads the design document keeps adding: nitrogen generation, spa heating, workshop
  machinery, greenhouse lighting, opportunistic compute.

**Against**

- It is the largest and most expensive of the three, and nothing in the package justifies the capacity against a
  computed load. There is no measured load anywhere in the design package — every `rated_power_kw` in the register
  is `TBD`.
- 800 kWh of storage is a serious fire-engineering and code problem in a 20-foot container, and no fire test
  certification is recorded (`energy.battery_bank.power_container.01.open_fields` includes
  `fire_test_certification`).
- Four parallel hybrid inverters is a materially harder commissioning and firmware-compatibility problem than two.

### Option 2 — Ratify a predecessor design (26.4 kWdc / 240 kWh, or 12 kW / 40 kWh)

**For**

- Far lower capital cost, smaller container, simpler fire case, fewer inverters to keep in firmware lockstep.
- The *35 MWh-yr* framing suggests the predecessor was sized against an actual annual energy estimate, which is
  more evidence than the revised design currently has.
- A 40 kWh bank is a normal, well-understood residential off-grid system with abundant product choice.

**Against**

- 40 kWh usable against a 1.5 kW critical baseload is roughly a day of critical autonomy with no solar. That forces
  routine generator operation, which contradicts EMS objective 5 ("avoid unnecessary generator operation").
- It almost certainly cannot carry the discretionary and process loads the rest of the design document assumes.
- The register, load schedule and EMS narrative would all need revision, and four inverter functional positions
  would need retiring — and §25.3 rule 5 forbids reusing retired IDs, so that cost is permanent.

### Option 3 — Ratify a staged design: build to the predecessor, engineer for the revision

Install a first phase near the predecessor's capacity, but size the container, DC bus, conductors, disconnects,
grounding and fire separation for the revised design.

**For**

- Defers the largest capital commitment until measured load data exists, without designing in a dead end.
- Matches the phased implementation plan in §20, which already sequences the power container ahead of the water,
  land and food-production phases.
- The register already models functional positions separately from equipment (§46). Inverters 3 and 4 stay as
  `planned` positions with no equipment behind them, which the data model handles natively.
- The EMS is written capacity-agnostically (§30.2), so a capacity change becomes a recommissioning of thresholds
  rather than a redesign.

**Against**

- Higher total cost than building once at the final size, through duplicated labour and possible equipment
  mismatch between phases.
- Requires the phase boundary to be specified now: what is oversized on day one and what is not.
- Battery chemistry and voltage must be locked at phase 1 to allow later expansion, which constrains phase-2
  product choice years ahead.

### Option 4 — Defer, and size from a measured load model

Refuse to ratify until a bottom-up load model exists.

**For**

- It is the only option that replaces an assumption with evidence, and load modelling is already work-queue item
  49.1 (the load schedule).
- Every capacity argument above is currently unfalsifiable because no load figure exists.

**Against**

- Nothing can be procured meanwhile, and the property may be bought and built around a power system that has not
  been sized.
- A model built from nameplate ratings rather than measurements is itself an assumption, just a better-dressed one.

---

## Recommendation

**Option 3 — ratify a staged design, with the revised 45 kWdc / 800 kWh figure as the engineering envelope and a
phase-1 build sized from the load schedule** — and, as a precondition, resolve which predecessor figure is real.

Reasoning:

1. **The infrastructure decisions are the irreversible ones.** Container size, fire separation, DC bus and conductor
   sizing, grounding, disconnect ratings and battery-zone ventilation are extremely expensive to change later.
   Modules and battery modules are incremental. Committing the envelope while staging the fill puts the irreversible
   decisions on the safe side and keeps the reversible ones open.

2. **The revised figure is the better envelope even if it is the worse phase-1 target.** Nothing in the design
   document suggests load will shrink: §3.5 to §3.7 keep adding nitrogen generation, spa heating, controlled-
   environment agriculture and workshop machinery. Engineering to 45 kWdc costs conduit and steel today; discovering
   in year three that the container cannot hold the bank costs a rebuild.

3. **The data model already supports this.** §46 separates functional positions from equipment. Inverters 3 and 4
   can stay `planned` with no equipment for years without disturbing a single asset ID, relationship or point
   binding. Option 3 is the one option that requires no change to the machine-readable package.

4. **It preserves the conflict honestly.** Ratifying the envelope is not the same as ratifying the capacity, and the
   `energy.capacity_revision` conflict record stays open until the phase-1 capacity is set from the load schedule.
   This is what §45 asks for.

5. **The predecessor discrepancy must be settled first.** 26.4 kWdc / 240 kWh and 12 kW / 40 kWh cannot both be the
   predecessor. Until someone opens the source diagrams and says which is which, any ratification is guesswork
   dressed as a decision.

This is a recommendation, not a decision. **Do not mark it accepted without owner ratification.**

---

## Consequences

### If accepted

- `design_basis.open_design_conflicts["energy.capacity_revision"]` gains a `staged` treatment and a phase boundary,
  and stays open until phase-1 capacity is set.
- The register keeps four inverter positions; inverters 3 and 4 are annotated as phase-2 with no equipment.
- The load schedule becomes a hard dependency of phase-1 sizing rather than a parallel deliverable.
- `data/asset_lifecycle.yaml` gains two CAPEX phases per energy asset instead of one, and the import instructions
  must say which phase a purchase belongs to.
- The battery bank's `chemistry`, `nominal_dc_voltage` and `module_count` open fields become **phase-1 blocking**,
  because expandability depends on them.

### If rejected in favour of Option 1

- Procurement can start immediately. Fire certification and container fire separation become the critical path.
- The absence of a measured load model becomes a permanent, unexamined assumption in the design.

### If rejected in favour of Option 2

- Four inverter positions must be reduced to two. Under §25.3 rule 5 the retired IDs can never be reused, and the
  register, bindings, load schedule and EMS narrative all need revision.
- Generator sizing, fuel storage and exercise scheduling become primary design problems rather than backup ones.

### Common to every option

Nothing about **battery chemistry, DC voltage, module count, procurement configuration or fire certification** is
settled by this record. Those remain open in `energy.battery_bank.power_container.01.open_fields`, and DD-001 does
not close them.

---

## What would settle this

In order of value:

1. **Open the two source diagrams** — *Argivoltaic System Design* and *35 MWh-yr Solar and Battery Design* — and
   record which capacity each actually states, with revision dates. This resolves the 26.4 / 12 kW contradiction
   and costs nothing. It should happen before anything else on this list.
2. **Complete the load schedule** (work-queue item 49.1) with rated and, where possible, measured power for every
   load. Capacity arguments are unfalsifiable until this exists.
3. **A twelve-month energy model** for the candidate property: solar resource, seasonal load, autonomy target and
   acceptable generator run-hours per year. The *35 MWh-yr* figure suggests such a model once existed; find it.
4. **Budget envelope from the owner.** The three designs differ by roughly an order of magnitude in cost. A stated
   ceiling eliminates options faster than any technical argument.
5. **A container fire-engineering opinion** for the candidate battery capacities. If 800 kWh cannot be permitted in
   the planned container, Option 1 is decided by external constraint and the common-mode-failure conflict changes
   shape too.
6. **Quotes for phase-1 and phase-2 equipment** to price the staging premium in Option 3. If the premium is small,
   Option 3 dominates; if it is large, the argument shifts back to Option 1.

---

## Related records

- [DD-003](DD-003-secondary-control-node-placement.md) — the common-mode failure domain that battery capacity makes
  worse.
- SDD §22 item 6 — inverter, BMS and generator product selection, which is downstream of this record.
- SDD §22 item 12 — physical separation and independent cooling inside the container, also downstream.
- `data/asset_lifecycle.yaml` → `import_instructions.known_blockers` — records this decision as a CAPEX blocker.
