# The design package

`data/` is not documentation. It is the **input** the registry is built from,
validated against JSON Schema in CI before anything loads it, and imported by
first-run setup.

Understanding it explains most of what the console shows you — including why so
much of the property reads as *design only* and why the platform keeps saying
`TBD` instead of a number.

Related: [Getting started § What first-run setup does](./getting-started.md#4-what-first-run-setup-does) ·
[The operator console § Assets](./operator-console.md#47-assets) ·
[Integration findings](./integration-findings.md) ·
[Design decision records](./design-decisions/README.md)

---

## 1. The documents

| File | Contents | Loaded into the registry? |
|---|---|---|
| `asset_class_dictionary.yaml` | Domains, relationship types, lifecycle vocabularies, asset classes, point profiles | Yes |
| `point_dictionary.yaml` | Canonical point names, classes, types, units, provenance | Yes |
| `homestead_asset_register.yaml` | 90 assets and 99 relationships: site, container, PV, inverters, battery, rack, network, services, sensors, load groups | Yes |
| `point_bindings.yaml` | 245 vendor/protocol bindings. Addresses are `TBD` unless the source supplied them | Yes |
| `load_schedule.yaml` | 12 load records with tier, control method and restoration policy | Yes, by the load-schedule loader |
| `alarm_definitions.yaml` | 40 alarm definitions with severity and operating context | Yes, by the alarm loader |
| `water_assets.yaml` | 47 water assets and 92 relationships | **No** — overlaid by the topology screen at render time |
| `water_points.yaml` | Water point definitions | **No** |
| `rack_layout.yaml` | The 42U rack proposal: 22 placements, feeds, PDUs, port plan | **No** — read directly by the rack screen |
| `asset_lifecycle.yaml` | Ownership and lifecycle record structure | **No** — structure defined, no data imported |

The four documents in the first block are what the registry loader reads. The
extension documents are part of the design and are consumed directly by the
screens that need them, each marked as an extension in its own payload, so a
screen never silently omits an entire subsystem — and never claims those assets
have points or bindings, because they do not.

See [Topology § The 137 nodes](./topology-view.md#2-the-137-nodes-and-why-the-registry-says-90) for
what that looks like in practice.

---

## 2. What lands after setup

| Table | Rows |
|---|---:|
| Assets | 90 |
| Relationships | 99 |
| Points | 701 |
| Point bindings | 245 |
| Locations | 30 |
| Load profiles | 12 |
| Alarm definitions | 40 |

Loading is **idempotent and additive**: it upserts and never drops, and records
a configuration revision for the load.

---

## 3. It deliberately does not invent things

The package does not invent IP addresses, MAC addresses, serial numbers, breaker
numbers, wire sizes, Modbus registers, SNMP OIDs or exact ratings. Those appear
as `open_fields` on each asset.

The documentation follows the same rule: **where the design package says TBD,
the docs say open item, not a plausible guess.**

You see the consequence everywhere:

- most bindings read `binding_status: tbd`, and that is the honest measure of
  how much of the property is wired up rather than merely modelled;
- the rack's thermal panel reports `not_estimated` rather than a made-up
  wattage;
- most assets are lifecycle status `planned`, so the console draws them as
  *design only* rather than green.

`open_fields` is a first-class column on the console's Assets screen, not a
footnote, because it is exactly what a commissioning engineer needs before
touching anything.

---

## 4. Ratification, and what "not ratified" means

Extension documents carry their own status, and the platform surfaces it rather
than flattening it away.

| `document_status` | Seen on | Means |
|---|---|---|
| `proposal_for_review` | `rack_layout.yaml` | A worked proposal. Nothing in it has been built or measured |
| `concept_extension_for_review` | `water_assets.yaml` | A concept-level extension to the register |
| `extension_for_review` | `water_points.yaml` | An extension to the point dictionary, awaiting review |
| `structure_defined_no_data_imported` | `asset_lifecycle.yaml` | The shape exists; no records have been imported |

`rack_layout.yaml` goes further and states its authority explicitly:

| Field | Value | Means |
|---|---|---|
| `approval_status` | `not_ratified` | **The owner has not accepted this.** It is a position to argue with |
| `authority` | `none_until_owner_review` | Nothing downstream may treat it as decided |
| `review_required_before_use` | `true` | Read it before acting on it |

**"Not ratified" is a statement about authority, not about quality.** The
document may be correct, carefully reasoned and internally consistent — and
still not be a decision. Nothing downstream may encode a decided value that
depends on it, and any screen that renders it says so. The rack screen reports
`usable_as_built: false` for exactly this reason.

The same vocabulary governs the
[design decision records](./design-decisions/README.md):

| Status | Meaning |
|---|---|
| `proposed — awaiting owner ratification` | A recommendation exists. **Nothing has been decided** |
| `accepted` | The owner has ratified the decision and dated it |
| `superseded by DD-nnn` | A later record replaces this one |
| `rejected` | Considered and declined, with the reasoning retained |

**Every record in that directory is currently `proposed`.** None may be treated
as settled.

---

## 5. Preserved conflicts

Where the design contradicts itself, the register **preserves both readings**
rather than quietly reconciling them. The platform exposes them so a dashboard
can show what is still undecided.

| Conflict | The disagreement | Treatment |
|---|---|---|
| `energy.capacity_revision` | 45 kWdc PV / four 10 kW hybrid inverters / 800 kWh nominal, 640 kWh usable — versus a predecessor design of 26.4 kWdc / two 10 kW inverters / 240 kWh usable | Preserved as unresolved. The register uses the revised target for functional asset *count* and leaves chemistry, module count, topology and procurement model unresolved. **No threshold in the energy manager assumes either is correct** |
| `power_container.common_failure_domain` | Power conversion, battery, utilities and the primary rack share one 20-foot container | The register includes a physically separate secondary control node as a required planned asset |

The second one is not an abstraction. Computing it gives **98 of 137 assets
lost, 50 of them critical** — see
[Topology § The answer the design document asks for](./topology-view.md#4-the-answer-the-design-document-asks-for).

---

## 6. Validation

Every document is validated against a JSON Schema (Draft 2020-12) in `schemas/`,
plus cross-reference checks — every asset class referenced exists, every point
name resolves, every relationship names real assets.

**This gates everything else.** If the design package does not validate, nothing
that loads it can be trusted, so CI runs the validator before it runs anything
else. The tracked validation report at the repository root records the last
full run.

You do not need to run it by hand: it runs in CI on every push, and first-run
setup imports only what validates.

---

## 7. Design decision records

`docs/design-decisions/` formalises the open decisions the design document lists
but does not settle. The purpose of a record is **not to decide**; it is to make
a decision *decidable*:

- state the context accurately, including where the source material disagrees
  with itself;
- lay out the real options with the trade-offs;
- give a recommendation **with reasoning**, so the owner argues with a position
  rather than starting from a blank page;
- state the consequences, including what becomes hard to reverse;
- name the **evidence that would settle it**;
- record what is blocked until it is.

| Record | Decision | Blocks |
|---|---|---|
| [DD-001](./design-decisions/DD-001-energy-capacity-revision.md) | Which energy design is authoritative | PV, inverter, battery and generator procurement; every energy threshold |
| [DD-002](./design-decisions/DD-002-historian-selection.md) | Time-series historian: InfluxDB or TimescaleDB | Historian deployment, retention, dashboard queries, backup design |
| [DD-003](./design-decisions/DD-003-secondary-control-node-placement.md) | Where the secondary control node lives | Common-mode failure mitigation, independent alerting, replication |
| [DD-004](./design-decisions/DD-004-controller-family.md) | Standard PLC/controller family | Every local control loop |

The record index also lists the decisions that do **not** yet have a record, so
the gap is visible rather than forgotten.

---

## 8. Changing the package

The design package lives in version control alongside the code, and that is the
point: registry content is reconstructible from Git even if the database is
lost.

After the files change, the launcher's setup row reports that `data/` has moved
on:

> `data/` has changed since this gateway loaded it. The registry still holds the
> earlier load. Nothing is re-imported automatically.

Press **Run again** on the launcher to import the current package. The import
upserts and never drops — but the gateway will not start it unattended against a
database that already holds registry rows, because that is an operator's
decision rather than a startup decision.

---

## 9. Related reading

| Document | Why |
|---|---|
| [Integration findings](./integration-findings.md) | Nine gaps in this package, found by running the platform |
| [Topology and blast radius](./topology-view.md) | Where the water extension shows up |
| [Rack elevation](./rack-view.md) | A `not_ratified` document rendered honestly |
| [Commissioning](./commissioning.md) | How `TBD` bindings become commissioned ones |
| [Design decision records](./design-decisions/README.md) | The open decisions in full |
