# DD-003: Secondary control node placement

**Status:** proposed — awaiting owner ratification
**Date raised:** 2026-08-07
**Decision owner:** homestead owner
**SDD reference:** §16.1, §16.2, §22 item 13, §46.2; `design_basis.open_design_conflicts["power_container.common_failure_domain"]`
**Blocks:** common-mode failure mitigation; independent alerting; configuration replication design; the node's power source, hardware and service allocation

---

## Context

SDD §16.1 states the risk plainly: housing batteries, power conversion, utilities and the primary server rack in
one 20-foot container creates a common physical failure domain. "Fire, smoke, overheating, water ingress,
electrical fault, or container HVAC failure could remove both the plant being controlled and the master
controller. The software design must therefore assume complete loss of that container."

The required mitigation includes: "A small secondary control node is installed in a separate structure" and "The
secondary node can provide a reduced dashboard, MQTT bridge, and emergency communications."

§22 item 13 asks where: "residence, workshop, or another independent enclosure".

### What the register already says

`it.server.secondary_control_node.01` exists as a `planned` asset with `criticality: critical`. Its
`properties.must_be_outside_common_failure_domain` is `true`, its `location.structure_id` is
`TBD_physically_separate_from_power_container`, its `power.feed_asset_id` is `independent_critical_power_TBD`, and
its roles are `secondary_control`, `configuration_replica` and `independent_alerting`. Its open fields are
`host_structure`, `hardware`, `independent_power`, `replication_method` and `services`.

§46.2 records the escalation: the secondary node "is now a formal planned asset rather than only a narrative
recommendation".

### The finding that matters most

**No candidate host structure exists in the asset register.** The only structure assets are
`structure.structure.power_container.01` and its three interior rooms. There is no residence, no workshop and no
independent enclosure in the register at all — because §22 item 7 (final property location, acreage, terrain and
structure placement) is itself unresolved.

This means DD-003 cannot be fully settled today. What it *can* do is fix the **requirements** the eventual location
must satisfy, so that when structures are sited the node is not an afterthought placed wherever there happens to be
a spare outlet.

### What already depends on the node

- The water control narrative routes freeze-protection alerting through the secondary node, so that a lost
  container does not mean a silently frozen main.
- §16.2 names it as the preferred secondary application host, in preference to the T7820, precisely because the
  T7820 is in the same container.
- §46.2 treats it as the register's answer to the preserved `power_container.common_failure_domain` conflict.

---

## Options

### Option 1 — Residence

**For**

- Occupied, so a fault, a fan failure or a dead node gets noticed by a human within hours rather than months.
- Heated, dry and within the residential thermal envelope: no dedicated environmental control needed.
- The residence needs reliable power anyway, so the independent supply is partly justified by other loads.
- Almost certainly has the best network path to the rest of the property.
- Alerting from the residence reaches the people who need to act, without depending on the container.

**Against**

- The residence is the structure most likely to have its own fire, and a fire there is also the emergency during
  which the node matters most.
- Domestic environment: outlets get unplugged, breakers get reset, equipment gets moved during renovations. A node
  nobody remembers is a node somebody unplugs.
- Fan noise and standby power in living space push toward a fanless, low-power device — which is a constraint, not
  a defect.
- Physical and network security is weaker than a locked equipment space.

### Option 2 — Workshop

**For**

- Genuinely independent of both the container and the residence, which is the strongest argument available: it
  fails independently of the two structures whose loss the node is meant to survive.
- Already an equipment space, so a rack, a UPS, cabling and a proper power feed are unremarkable there.
- Better physical security and fewer accidental disturbances than a residence.
- Heavy workshop loads mean the workshop is already wired for real power.

**Against**

- Often unoccupied for long periods, so a failed node can go unnoticed. This is mitigable with a heartbeat alarm,
  and the heartbeat is required anyway.
- Workshops are dusty, are subject to welding and grinding transients, and often have wide temperature swings.
  Environmental protection becomes a real requirement.
- Workshop machinery is a Tier 3 deferrable load (§31.2), so workshop power may be shed while the node must stay
  up. The node's supply must be explicitly separated from the shedable workshop feed.
- Fire risk in a workshop is not negligible either.

### Option 3 — A dedicated independent enclosure

A small purpose-built weatherproof enclosure — a pedestal near the network hub, or a small utility cabinet — housing
only the secondary node and its power.

**For**

- Failure independence by construction, without inheriting the risks of an occupied or industrial structure.
- Environmental and power design is specified for exactly one purpose, with no competing loads.
- Locatable for network topology (near a switch or a wireless bridge) rather than for human convenience.
- Smallest fire load of the three options, and the easiest to physically separate.

**Against**

- Costs a structure, however small, and adds a maintenance item nobody visits.
- Outdoor enclosures need heating, cooling or at least ventilation, plus condensation management. That is a small
  environmental-control problem of its own, and it can fail.
- Least likely of the three to be noticed when it fails, and hardest to service in bad weather — which is when
  outages happen.
- Needs its own power feed and its own network path, both of which cost more than reusing an existing structure's.

### Option 4 — Two reduced nodes rather than one

Place a minimal node in the residence *and* one in the workshop or enclosure, each capable of alerting and reduced
status.

**For**

- Removes the single point of failure in the mitigation itself. A mitigation with a single point of failure is a
  weak mitigation.
- Low-power industrial computers are cheap relative to the rest of the plant, and cheap relative to a frozen main
  or a destroyed battery bank.
- The residence node gets the human attention; the independent node gets the failure independence. Each covers the
  other's main weakness.

**Against**

- Doubles the configuration replication, patching and certificate-management burden, and split-brain between two
  secondaries must be designed out.
- §4.2 lists avoiding unnecessary complexity in the first release as a non-goal, and this is more complexity.
- Ambiguity about which node is authoritative during a partial outage is exactly the kind of "contradictory
  control authority" §30.8 treats as an emergency condition.

---

## Recommendation

**Option 1 (residence) for the first release, with the enclosure/workshop option engineered as a later addition** —
and, as a precondition, treat the node's *independence requirements* as the ratified part of this decision rather
than its address.

Reasoning:

1. **Ratify the requirements now, the address later.** No candidate structure exists in the register. What can be
   fixed today is the specification: independent power, independent network path, independent alerting egress,
   thermal envelope, monitored heartbeat, physical separation from the container. Any structure that meets those
   is acceptable; any that does not is not — regardless of which room it is in.

2. **Occupancy is worth more than fire independence at this stage.** The design document's own reasoning (§16.1)
   is about *unattended* failure: the fear is that the container is lost and nobody knows. A node in an occupied
   structure fails loudly. A node in an unvisited enclosure fails quietly, and a secondary control node that has
   been dead for four months is not a mitigation, it is a comforting entry in a register.

3. **The residence is where alerting lands anyway.** §12.1 FR-007 requires email and push alerts with optional
   voice escalation, and §16.1 requires that essential alerts can originate from independent local devices. If
   alerts must reach people, originating them where the people are removes one dependency.

4. **It is the cheapest option that actually satisfies §16.1 today.** The requirement is a *separate structure*,
   and the residence is one. Options 2 and 3 are better on failure independence but need structures that do not
   exist. Building a dedicated enclosure before knowing the property layout is premature.

5. **Option 4 is right eventually and wrong now.** Two nodes is the correct end state for a site whose master
   controller sits in a container full of batteries. But split-brain and replication complexity are real, and
   §16.2 explicitly defers cluster complexity in the first release. Design the residence node so a second peer can
   be added — a replication method that is not point-to-point, a clear authority model — and add the second node
   when the workshop exists.

**The residence node must not share a distribution panel with the container.** If the residence is fed from the
same critical panel that the container feeds, the mitigation is defeated at the electrical layer no matter where
the box physically sits. This is the single most important consequence of choosing Option 1.

---

## Consequences

### If accepted

- `it.server.secondary_control_node.01.location.structure_id` waits on a residence structure asset; the node's
  `open_fields` gain an explicit `independent_distribution_panel` requirement.
- The node's power source must be demonstrably independent of `energy.panel.power_container.critical_01`, with its
  own local UPS or battery. This becomes a Tier 0 load in the load schedule (§31.2), never shed.
- An independent alerting egress is required: a cellular or radio path that does not traverse the container's ISR
  or WAN. Without it the node can detect a container loss and be unable to say so.
- A heartbeat alarm on the node itself is mandatory, monitored by something that is not the primary platform.
- Replication method must support a future second peer without redesign.
- The `power_container.common_failure_domain` conflict record moves from "unmitigated" to "partially mitigated" —
  not to "resolved". §16.1 requires five other mitigations besides the node, including physically separating the
  battery and server zones and keeping them off a shared uncontrolled airflow path.

### If a workshop or dedicated enclosure is chosen instead

- Environmental protection becomes a design requirement: dust, temperature range and condensation.
- Occupancy-based detection is lost, so heartbeat monitoring becomes the only failure detection and must be
  monitored off-site or by an independent device.
- The node's supply must be explicitly separated from any shedable workshop feed.

### Common to every option

Hardware, replication method, service allocation and independent power all remain open fields on the asset. DD-003
does not select a computer.

---

## What would settle this

1. **Confirm the property and the structure plan** (§22 item 7). Until structures exist, only the requirements can
   be ratified.
2. **Produce the property electrical single-line diagram**, showing whether the candidate structure can be fed
   independently of the container's critical panel. If it cannot, that option fails on the decisive criterion.
3. **Decide the independent alerting egress.** Confirm cellular coverage or an alternative radio path at each
   candidate location. A node that cannot raise an alarm during a container loss does not mitigate anything.
4. **Write the failure-mode test in advance.** `EMS-T002` already covers stopping the primary host; extend it to
   full container loss, and specify what the secondary must still do: reduced dashboard, MQTT bridge, alerting,
   freeze-protection visibility. The answer determines how much machine the node needs.
5. **Establish the recovery-time objective.** How long may the site run on the secondary alone? Hours changes the
   answer from days, and days changes it from weeks.
6. **Price two nodes against one.** If the delta is small relative to the plant, Option 4's failure independence
   may be worth accepting the complexity for immediately.

---

## Related records

- [`docs/secondary-control-node.md`](../secondary-control-node.md) — what the node runs, what it can and cannot do
  during a container loss, and how its read-only role is enforced. That document specifies the node's *behaviour*;
  this record decides its *location*, which §6 of that document lists as its first open decision.
- [DD-001](DD-001-energy-capacity-revision.md) — battery capacity determines how severe the common-mode risk is.
- [DD-004](DD-004-controller-family.md) — local controllers are the other half of the §16.1 mitigation, and they
  matter more than the node when the container is lost.
- SDD §22 item 12 — physical separation and independent cooling inside the container.
- SDD §22 item 9 — remote-access and offsite-backup architecture, which shares the independent-egress requirement.
