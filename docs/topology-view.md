# Topology and blast radius

The **Topology** screen draws the property as a dependency graph and answers one
question the design document asks and never computes:

> Assume total loss of the combined power, battery, utilities and server
> container. What is left?

Related: [The operator console](./operator-console.md) ·
[Alarms § Correlation](./alarms.md#5-correlation-and-incidents) ·
[Secondary control node](./secondary-control-node.md) ·
[Integration findings](./integration-findings.md)

---

## 1. What the canvas shows

| Element | Comes from | Notes |
|---|---|---|
| **Nodes** | Assets in the register | Identity, class, criticality and lifecycle status are authoritative. *Health* is a separate derived field, recomputed on every request, and allowed to be unknown |
| **Edges** | The register's typed relationships | Carried through **with their type intact**. `feeds` is drawn differently from `monitors` because they mean different things |
| **Groups** | The containment hierarchy | So the canvas can draw the boundary an operator actually reasons about: *this is the power container, and everything inside it shares its fate* |

Collapsing edge types into one "connected" line would throw away the only
information that makes the graph operable. So the console does not.

### Edge meanings

| Type | Means |
|---|---|
| `feeds` | Supplies power, water or media. Loss of the source stops the sink |
| `contains` | Physical containment. Loss of the container is loss of the contents |
| `hosts` | Runs the workload. Loss of the host stops what it hosts |
| `controls` | Commands the actuator. Loss of the controller leaves the actuator uncommanded |
| `serves` | Delivers a service. Loss of the provider ends the service |
| `protects` | Provides protection — breaker, valve, suppression |
| `located_in` | The subject sits inside the object |
| `part_of` | The subject is a component of the object |
| `depends_on`, `managed_by`, `powered_by` | Stated from the dependent end; the walk follows them in reverse |

Groups are drawn for containment classes: `site`, `geographic_zone`,
`structure`, `building`, `room`, `zone`, `enclosure`, `rack`,
`irrigation_zone`.

### A node is never healthy because nothing is wrong with it

Of the assets in the package, **none is installed**. "No alarm" on a `planned`
asset is not evidence of health, it is the absence of a plant.

Health therefore starts from the lifecycle status and only reaches `ok` when a
bound point has actually reported inside its stale window. On a fresh install
every node reads `not deployed`, and that is the correct answer.

The vocabulary is the platform's own — `ok`, `stale`, `no_data`, `no_points`,
`design_only`, `not_deployed`, `alarm`, `degraded` — used unchanged, so a
topology node and a home-screen tile can never disagree about what "stale"
means.

---

## 2. The 137 nodes, and why the registry says 90

The graph shows **137 nodes and 191 edges** on the shipped package. The
registry database holds **90 assets**.

The difference is the water system. `data/water_assets.yaml` is part of the
design package — 47 assets and 92 relationships — but the registry loader does
not merge it yet. Rather than draw a topology that silently omits the entire
water system, this screen **overlays the extension at render time** and marks it
as such:

```json
"package": {
  "registry_nodes": 90,
  "extension_nodes": 47,
  "extensions": [{
    "file": "water_assets.yaml",
    "document_status": "concept_extension_for_review",
    "assets_applied": 47,
    "relationships_applied": 92,
    "note": "Present in the design package but not merged into the registry database
             by the loader yet, so these assets carry no points and no bindings."
  }]
}
```

Those 47 assets therefore have **no points and no bindings**. They can be
reasoned about structurally; they cannot report anything. A missing or unreadable
extension is reported in the payload, not raised — the topology of what *is*
loaded stays useful when one document is broken, and the screen says which one
and why.

---

## 3. Blast radius

Select an asset and ask for its impact. The result is the set of assets that go
with it.

### It walks the same graph the alarm correlator walks

Not a similar one — literally the correlator's own dependency graph and its
direction table. The blast radius you see on the canvas is by construction the
same set the correlator will fold into one incident when it actually happens.

A second, subtly different traversal would be worse than no traversal at all: it
would train an operator on a model the platform does not use.

The walk is bounded at **8 hops**, matching the correlator's own default. A
graph deeper than that is reported as `depth_bounded`, so the answer is never
silently truncated.

### What comes back

| Field | Meaning |
|---|---|
| `origin` | The asset you asked about |
| `affected` | Everything downstream of its loss, with the path that reaches each one |
| `summary` | Totals, and breakdowns by criticality and by domain, plus survivors by domain |
| `observability_lost` | What you would stop being able to *see* |
| `redundancy_lost` | Backup relationships that the loss removes |
| `rules` | The propagation rules used |
| `caveats` | What this answer does not account for — see section 5 |

---

## 4. The answer the design document asks for

Computing the loss of `structure.structure.power_container.01` on the shipped
package gives:

| | |
|---|---:|
| Assets lost | **98 of 137 (71%)** |
| Of which `critical` criticality | **50** |
| Survivors | **38** |

The design document's instruction to assume complete loss of that container is
**not being conservative. It is barely adequate.**

### The actionable part is water

21 water assets survive: the well, the freeze-protection controller, both winter
drain valves, the field pipework. But **22 do not**, and the path to them is only
four hops:

```text
power container → power zone → critical loads panel
                → water pumping load → potable pressure pump
```

**Lose the container and you lose potable water pressure**, even though the
well, the tank and the pipework are all outside it and physically unharmed. That
is a single electrical dependency defeating an otherwise independent subsystem —
exactly the kind of coupling the analysis exists to find.

### Two safety assets die with the thing they exist to watch

- the fluid-detection sensor monitoring for flooding *in that container*;
- the local alarm beacon, which the alarm correlator deliberately exempts from
  incident folding so it can always speak for itself. It cannot speak if it is
  inside the fire.

### The dependency structure is top-heavy, and the top is spatial

Nineteen single assets each take out 30 or more others; 73 take out nobody.

| Asset | Takes out |
|---|---:|
| Power container | 98 |
| Server zone | 67 |
| Primary rack | 65 |
| Power zone | 45 |
| Battery bank | 40 |
| One hybrid inverter | 37 |
| Critical loads panel | 26 |

The server zone and the rack **outrank the battery bank**. Containment, not
power, is the dominant coupling on this property.

Full write-up:
[Integration findings F-008](./integration-findings.md#f-008--losing-the-power-container-takes-71-of-the-homestead-including-potable-water-pressure).

---

## 5. What the analysis will not tell you

The payload carries its own caveats rather than presenting a clean number:

**It cannot express N+1 redundancy.** There is no way in the asset register to
say that a set of devices is a redundant group. The four hybrid inverters are
modelled as four separate assets each feeding the same AC combiner, so losing
any one reads as a full outage of everything downstream — 37 assets apiece. The
traversal is correct; the model is missing a concept. See
[Integration findings F-009](./integration-findings.md#f-009--the-register-cannot-express-n1-redundancy).

**It is structural, not temporal.** It says what is downstream of a failure, not
how long a battery reserve or a tank of water will keep those assets alive.

**It is bounded at 8 hops**, and says so when it hits that bound.

Reporting the caveats is mitigation, not a fix. The platform can only reason
about redundancy that the data model is capable of stating.

---

## 6. What to use it for

| Question | How |
|---|---|
| *What does this container failure take with it?* | Impact on the structure. This is the design document's own question |
| *Is this subsystem really independent?* | Impact on its power source. The water finding above was found exactly this way |
| *What will one alarm turn into?* | The same set the correlator will fold into one incident |
| *Where should the secondary node go?* | Anything whose blast radius includes the secondary node is a placement that has not achieved separation. See [Secondary control node](./secondary-control-node.md) |
| *What would I stop being able to see?* | `observability_lost` — the monitoring that dies with the thing it monitors |

---

## 7. Related reading

| Document | Why |
|---|---|
| [Integration findings](./integration-findings.md) | F-008 and F-009 came out of this screen |
| [Secondary control node](./secondary-control-node.md) | What the common-mode failure domain means for deployment |
| [Alarms § Correlation](./alarms.md#5-correlation-and-incidents) | The same graph, used to fold alarms into incidents |
| [The design package](./design-package.md) | Why the water extension is not in the registry yet |
| [API reference § Registry](./api.md#5-registry) | `GET /api/v1/topology` and `/topology/impact/{asset_id}` |
