# Design Decision Records

This directory formalises the open design decisions that the Homestead Digital Twin design document lists but does
not settle: SDD section 22 (fourteen open decisions) and `design_basis.open_design_conflicts` in
`data/homestead_asset_register.yaml` (two preserved conflicts).

## What these records are for

SDD sections 3.1 and 45 are explicit that conflicts are **preserved rather than silently reconciled**. The purpose
of a record here is therefore not to decide. It is to make a decision *decidable*:

- state the context accurately, including where the source material disagrees with itself;
- lay out the real options with the trade-offs the design document actually implies;
- give a recommendation **with reasoning**, so the owner is arguing with a position rather than starting from a
  blank page;
- state the consequences of each option, including what becomes hard to reverse;
- name the **evidence that would settle it** — the measurement, quote, survey or test that turns an opinion into a
  decision;
- record what is blocked until it is settled.

## Status vocabulary

| Status | Meaning |
|---|---|
| `proposed — awaiting owner ratification` | A recommendation exists. Nothing has been decided. |
| `accepted` | The owner has ratified the decision and dated it. |
| `superseded by DD-nnn` | A later record replaces this one. |
| `rejected` | Considered and declined, with the reasoning retained. |

**Every record in this directory is currently `proposed — awaiting owner ratification`.** None of them may be
treated as settled, and no downstream document — asset register, load schedule, rack layout, lifecycle records —
may encode a decided value that depends on one of them.

## Index

| ID | Decision | SDD reference | Status | Blocks |
|---|---|---|---|---|
| [DD-001](DD-001-energy-capacity-revision.md) | Authoritative energy design: 45 kWdc / 800 kWh revision or its predecessor | §3.1, §22.1, §30.2, §46.1; `energy.capacity_revision` conflict | proposed | PV, inverter, battery and generator procurement; EMS threshold commissioning; all energy CAPEX |
| [DD-002](DD-002-historian-selection.md) | Time-series historian: InfluxDB or TimescaleDB | §8.5, §16.4, §22.3, §40.1 | proposed | Historian deployment, retention policy, dashboard queries, backup design |
| [DD-003](DD-003-secondary-control-node-placement.md) | Where the physically separate secondary control node lives | §16.1, §16.2, §22.13, §46.2 | proposed | Common-mode failure mitigation; independent alerting; replication design |
| [DD-004](DD-004-controller-family.md) | Standard PLC / controller family for critical local control | §5.2, §22.5, §30.4; FR-402, FR-203 | proposed | Every local control loop: water, freeze protection, greenhouse, irrigation, nitrogen storage |

## Decisions not yet given a record

The remaining SDD section 22 items and the second preserved conflict still need records. They are listed here so
the gap is visible rather than forgotten:

| SDD §22 | Decision | Why it is not yet recorded |
|---|---|---|
| 2 | Virtualization platform and container/VM deployment model | Lower blast radius; reversible with migration effort |
| 4 | LoRaWAN architecture and frequency plan | Depends on final property geography |
| 6 | Inverter / BMS / generator product selection and local interfaces | Downstream of DD-001 |
| 7 | Final property location, acreage, terrain and structure placement | Owner decision outside the software design |
| 8 | Which system owns scheduling when Home Assistant, Node-RED and subsystem controllers overlap | Downstream of DD-004 |
| 9 | Remote-access and offsite-backup architecture | Needs the network and trust-boundary package |
| 10 | Data-retention limits | Downstream of DD-002 |
| 11 | Acceptable command latency by subsystem | Needs the subsystem control narratives to be complete |
| 12 | Physical separation and independent cooling inside the power/server container | The `power_container.common_failure_domain` conflict; partially mitigated by DD-003 |
| 14 | Integration boundaries for Aircela, beekeeping, avian systems, workshop machinery and future businesses | Out of scope for the first release |

## Record template

```markdown
# DD-nnn: <decision>

**Status:** proposed — awaiting owner ratification
**Date raised:** <date>
**Decision owner:** homestead owner
**SDD reference:** <sections>
**Blocks:** <what cannot proceed>

## Context
## Options
## Recommendation
## Consequences
## What would settle this
## Related records
```
