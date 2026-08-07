# DD-004: Standard PLC / controller family for critical local control

**Status:** proposed — awaiting owner ratification
**Date raised:** 2026-08-07
**Decision owner:** homestead owner
**SDD reference:** §5.2, §5.3, §16.1, §22 item 5, §30.4; FR-203, FR-402, FR-601, FR-702, FR-703
**Blocks:** every local control loop — water and freeze protection, irrigation, greenhouse, compost, nitrogen storage, spa; the `controller` asset class's `platform_status` field on every controller in the register and the water extension

---

## Context

SDD §22 item 5 asks for "the standard PLC/controller family for critical local control". This is not a
purchasing detail. The architecture's central claim (§5.2, §5.3, §24) is that the homestead is a layered
operational-technology system: hardwired protection at the bottom, **autonomous local control above it**, a message
bus and asset model in the middle, and dashboards and optimisation at the top. The local control layer is the one
that keeps the property alive when the middle and top are gone.

### How much depends on it

The design document assigns real safety-adjacent authority to local controllers:

- **FR-203** — pump inhibits on dry run, low source level, freeze lockout, overcurrent and unavailable discharge
  path. The water control narrative states these are enforced locally, not in the platform.
- **FR-402** — greenhouse freeze protection "independent of the central server".
- **FR-601** — nitrogen purge suspended when access doors are open. This one is a life-safety interlock: the
  storage containers are oxygen-displaced.
- **FR-702 / FR-703** — spa heater blocked without proven flow; pump blocked below minimum reservoir level.
- **§30.4** — "Fast motor interlocks: Local PLC/controller" owns them; the EMS only publishes permissives and
  budgets.
- **§16.1** — "Local controllers continue safe subsystem operation without the server rack."

### What the register says today

The `controller` asset class exists (`dictionary_status: reconciled_v0_3`, source §9.2) with required properties
`controller_role` and `platform_status`. The water extension adds three controllers —
`water.controller.water_system.01`, `water.controller.freeze_protection.01`,
`water.controller.irrigation.01` — each with `platform_status: TBD`, each with
`must_operate_without_central_server: true`, and each carrying `controller_platform` in its open fields. Every
future subsystem will add more.

### Why standardising matters more than the specific choice

One operator, twenty-plus years, a dozen subsystems. Every distinct controller family means another programming
tool, another firmware lifecycle, another spares holding, another set of wiring conventions, another way to be
locked out of your own equipment at 3 a.m. in February. §18 already requires maintenance, spares and calibration
tracking; §5.6 requires versioned configuration. Both get much harder per additional family.

The decision is therefore mostly about **repairability over decades**, and only secondly about capability.

---

## Options

### Option 1 — Industrial PLC family (compact industrial line from one established vendor)

**For**

- Designed for exactly this: DIN rail, 24 VDC, wide temperature range, deterministic scan, decades of service life.
- Native Modbus TCP/RTU, which is what the inverters, BMS and most process instruments will speak, and which the
  point-binding model already anticipates.
- Long product lifecycles and long spare availability. A ten-year-old industrial PLC is still buyable; a ten-year-
  old hobby board is not.
- Safety-rated variants exist for the interlocks that genuinely need them (FR-601 in particular).
- Configuration is a file that can be versioned and restored, satisfying §5.6 and FR-805.

**Against**

- Highest hardware cost per I/O point, by a large margin.
- Vendor programming environments are often proprietary, Windows-only, licensed and sometimes expensive. That is a
  direct threat to the §4.1 repairability goal: a controller you cannot reprogram because the licence lapsed is
  worse than one that was cheaper.
- Steeper learning curve, especially for an owner coming from a software rather than a controls background.
- Some ecosystems make the toolchain a per-seat commercial dependency, which sits badly with the local-first,
  independence-oriented design philosophy.

### Option 2 — Open-standard programmable controller (IEC 61131-3 on open hardware, or an open runtime)

**For**

- Standardised programming languages (ladder, structured text, function block) that are not tied to one vendor, so
  logic is portable if the hardware line is discontinued.
- Industrial form factors and I/O modules are available without a proprietary toolchain.
- Typically lower cost than Option 1 while keeping DIN-rail and 24 VDC conventions.
- Open toolchains have no licence expiry, which matters over a twenty-year horizon more than it seems today.

**Against**

- Smaller ecosystems, so hardware availability and long-term support are less certain than Option 1's.
- Safety-rated options are rarer; a genuinely safety-rated interlock may still need a separate device.
- More integration work: the owner assembles a stack rather than buying one.
- Community support rather than vendor support when something is broken at 3 a.m.

### Option 3 — Industrial-grade embedded Linux controllers (edge controllers, industrial single-board computers)

**For**

- Same operating system, same language and same tooling as the rest of the platform, so there is one skill set
  rather than two. On a one-operator site this is a serious advantage.
- Excellent protocol flexibility: MQTT, Modbus, HTTP, LoRa and one-off vendor protocols all live comfortably in the
  same runtime.
- Cheap, easy to replicate, easy to image and restore, easy to version in the same repository as everything else.
- Fits the existing platform's deployment and configuration-management approach with no new machinery.

**Against**

- **A general-purpose OS is not a deterministic control platform.** Filesystem corruption on power loss, unattended
  updates, kernel panics, memory pressure and multi-second scheduling jitter are all ordinary Linux behaviours and
  all unacceptable for an interlock.
- Weaker environmental tolerance unless specifically industrial hardware is chosen, and SD-card-based storage fails
  in ways that are hard to detect until they matter.
- Using this class of device for FR-601 oxygen-displacement interlocks would be an error. That interlock protects a
  person entering a low-oxygen container.
- Long-term hardware availability is much worse than Option 1's.

### Option 4 — Tiered: hardwired safety, industrial PLCs for critical process, Linux edge controllers for the rest

Three explicit tiers with a stated rule for which tier a function belongs to.

**For**

- Matches the architecture the design document already describes (§5.3, §5.5, §24, §30.4): hardwired protection,
  then autonomous local control, then supervisory.
- Puts cost where risk is: a life-safety interlock gets a rated device, a compost blower does not.
- Keeps the number of *critical* control families to one, while allowing cheap flexible hardware everywhere the
  failure consequence is an inconvenience.
- Directly answers §5.5's "fail safe and fail understandable": the tier boundary tells an operator what fails
  gracefully and what does not.

**Against**

- Two toolchains and two spares holdings, which is exactly what standardising was meant to avoid.
- Requires a written and enforced tier-assignment rule, or everything drifts into the cheap tier over time. That
  drift is the real risk, and it is a governance problem, not a technical one.
- More design work up front: every control function must be classified before it is built.

---

## Recommendation

**Option 4 — a tiered standard, with exactly one industrial PLC family chosen for the critical tier.**

Reasoning:

1. **The design document already specifies a tiered architecture; this decision should implement it, not flatten
   it.** §5.3 explicitly warns against fragile central control and §30.4 assigns fast interlocks to local
   controllers while keeping equipment-native protection sovereign. Forcing every control function into one
   technology contradicts a design whose central idea is that different layers have different failure obligations.

2. **The tier boundary should be drawn by consequence of failure, not by subsystem.** Proposed rule:

   | Tier | Rule | Technology | Examples |
   |---|---|---|---|
   | 0 | Failure can injure a person or destroy plant, and must work with all software dead | Hardwired / safety-rated device | Nitrogen container entry interlock (FR-601), emergency stop, spa heater flow proof (FR-702), high-limit thermostats |
   | 1 | Failure causes unrecoverable or expensive damage, or loss of an essential service | One industrial PLC family | Pump interlocks (FR-203), freeze protection and winter drainage (FR-204), greenhouse freeze protection (FR-402), battery-zone environmental protection |
   | 2 | Failure is an inconvenience recoverable by a human within hours | Linux edge controllers | Irrigation scheduling, compost aeration, mower coordination, environmental monitoring, LoRa field gateways |

3. **One family in tier 1, chosen for toolchain longevity over feature set.** The selection criterion should be
   "can I still program this in fifteen years without a licence server", not "which has the best IDE today". That
   argues for a vendor with a free or perpetually-licensed toolchain, or for the Option 2 open-standard route
   *within* tier 1. Either is acceptable; two different tier-1 families are not.

4. **Tier 2 is where the platform's existing skills pay off.** Irrigation scheduling and compost aeration in the
   same language and deployment pipeline as the rest of the platform is a genuine productivity win, and their
   failure mode is a dry zone or a cool pile — recoverable, visible, and not dangerous.

5. **Write the tier rule down and enforce it at review.** The failure mode of Option 4 is drift: everything
   gradually lands in tier 2 because tier 2 is easier. The counter is that the `controller` asset class already
   requires a `controller_role` property; add a required `control_tier` property so a tier-1 function cannot be
   implemented on tier-2 hardware without the register showing it.

**One thing this record should not defer:** FR-601 oxygen-displacement entry interlocking is a life-safety
function. It belongs in tier 0 on a rated device regardless of how DD-004 is otherwise resolved, and it should not
wait for this decision.

---

## Consequences

### If accepted

- The `controller` asset class needs a `control_tier` required property, and every existing controller record needs
  a tier assigned. This is a change to `data/asset_class_dictionary.yaml`, which is a v0.3 baseline document and
  therefore itself needs a versioned revision.
- The three water controllers get tiers: `water.controller.water_system.01` and
  `water.controller.freeze_protection.01` in tier 1, `water.controller.irrigation.01` in tier 2.
- Spares policy splits: tier-1 spares are held on site (they are already flagged by `spares_policy_required` in
  `data/asset_lifecycle.yaml` for every critical asset); tier-2 spares can be next-day.
- Configuration backup (FR-805) must cover both toolchains, and the tier-1 backup must be restorable without
  network access.
- Commissioning (§19) must test each tier's independence separately: tier 1 with the platform stopped, tier 0 with
  everything stopped.
- The point-binding model already separates identity from vendor binding, so a tier-1 controller can be replaced
  without disturbing any `asset_id` or `point_name`. That protection only holds if the binding discipline is kept.

### If a single family is chosen instead (Options 1, 2 or 3 alone)

- One toolchain and one spares holding, which is simpler to operate.
- Option 1 alone means paying industrial prices for compost blower control, which will create pressure to
  improvise, and improvisation outside the standard is worse than a planned second tier.
- Option 3 alone means an oxygen-displacement interlock on a general-purpose operating system. That should not
  happen.

### Common to every option

The specific vendor and model remain open. DD-004 recommends a *structure*, not a product. The
`controller_platform` open field stays open on every controller asset until a family is named.

---

## What would settle this

1. **Classify every known control function into tiers 0, 1 and 2.** The water narrative already lists its
   interlocks; do the same for greenhouse, nitrogen storage, spa and compost. Count the tier-1 I/O points. If tier
   1 turns out to be small, a single premium family is affordable and Option 1 becomes attractive on its own.
2. **Establish the owner's controls background.** Someone comfortable with ladder logic and someone comfortable
   only with Python will maintain these systems very differently, and the maintainable choice beats the elegant one.
3. **Test the toolchain licensing terms before buying anything.** Download the programming environment for each
   candidate, confirm it runs offline, confirm it does not expire and confirm a configuration can be exported to a
   plain file and restored to a replacement unit. A controller that cannot be restored offline fails §5.1.
4. **Confirm Modbus and protocol coverage** against the actual instrument list once DD-001 settles inverter and BMS
   selection, since those are the highest-value integrations.
5. **Get a code and insurance opinion on the tier-0 functions**, specifically the nitrogen-displacement entry
   interlock. That answer may be prescriptive, and if so it decides tier 0 without further debate.
6. **Price a fifteen-year spares holding** for each candidate tier-1 family. Long-term parts availability is the
   criterion most likely to be decisive and least likely to be checked.

---

## Related records

- [DD-003](DD-003-secondary-control-node-placement.md) — local controllers are the other half of the §16.1
  common-mode mitigation, and matter more than the secondary node when the container is lost.
- [DD-001](DD-001-energy-capacity-revision.md) — inverter and BMS selection determines the protocols tier 1 must
  speak.
- SDD §22 item 8 — which system owns scheduling when Home Assistant, Node-RED and subsystem controllers overlap.
  That question is downstream of the tier rule proposed here.
- `docs/water-control-narrative.md` §2 and §6 — the first concrete set of tier-1 interlocks.
