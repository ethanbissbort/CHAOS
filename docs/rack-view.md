# Rack elevation

The **Rack** screen draws the primary 42U rack: every unit from 1 to 42, what
occupies it, whether that device is actually reporting, how it is powered — and,
prominently, the fact that none of it has been built.

Related: [The operator console](./operator-console.md) ·
[The design package](./design-package.md) ·
[Topology and blast radius](./topology-view.md)

---

## 1. Read this first: the layout is a proposal

`data/rack_layout.yaml` says so in its own header:

| Field | Value |
|---|---|
| `document_status` | `proposal_for_review` |
| `approval_status` | `not_ratified` |
| `authority` | `none_until_owner_review` |
| `review_required_before_use` | `true` |

Nothing in it has been built, measured or ratified, and the register's rack-unit
field is still `TBD` for every device in it.

So the screen has one job beyond joining data: **make that impossible to
forget.** The ratification metadata is lifted to the top of the payload, every
device carries its own placement status and data status, and the response
advertises `usable_as_built: false`.

A client that renders this as *the* elevation is misrepresenting the document,
and the payload gives it no excuse.

What "not ratified" means in general, and the vocabulary it belongs to, is in
[The design package § Ratification](./design-package.md#4-ratification-and-what-not-ratified-means).

---

## 2. What you should see

A 42U elevation, **numbered bottom to top with unit 1 lowest**, because that is
what the document declares. Beside it: the free blocks, the two power feeds, the
switch port plan, the zero-U devices, the excluded assets and the findings.

On the shipped layout:

| | |
|---|---:|
| Rack units | 42 |
| Units accounted for | 42 |
| Occupied | 24 |
| Free | 18 |
| Largest free block | 18U, units 18–35 |
| Devices consuming units | 16 |
| Zero-U and door-mounted devices | 6 |
| Deliberately excluded assets | 5 |
| Findings raised | 8 |

### The U-grid is served whole

Every unit 1..42 comes back with what occupies it or an explicit *free*. No
client has to infer occupancy from a start-and-height pair and get it subtly
wrong.

### Faceplates carry availability, not decoration

Each device's faceplate shows its health in the platform's own vocabulary —
`ok`, `stale`, `no_data`, `no_points`, `design_only`, `not_deployed` — imported
unchanged from the overview code rather than restated, so a rack faceplate and a
home-screen tile can never disagree about what "stale" means.

- A device with no telemetry reports **`no_data`**, never a healthy green.
- A device that reported once and went quiet reports **`stale`**, which is a
  different and more alarming fact.

### Nothing that exists is dropped

Zero-U and door-mounted gear consumes no rack units but is still in the rack, so
it comes back in its own list. The five assets deliberately excluded from the
elevation come back with their exclusion reasons — for example the wall-mount
environmental appliance ("not a rack-mounted device") and the secondary control
node ("must sit outside this failure domain").

---

## 3. It checks the document rather than trusting it

The layout document says it "is checked for overlap". This screen **is** that
check, re-run on every request: non-overlap, the occupied/free split, and PDU
outlet agreement are all re-derived, and any disagreement is reported in
`findings`.

On the shipped layout, eight findings:

| Severity | Code | What it says |
|---|---|---|
| blocking | `RACK-FEED-UNRESOLVED` | Power feed B has no upstream asset in the register, so it cannot carry load. Every assignment to feed B is aspirational, and the rack transfer switch provides no real redundancy until a second source exists |
| blocking | `RACK-DUAL-FEED-UNREALISABLE` | Four devices record power feed "A and B" while feed B resolves to nothing. Every one of them is effectively single-fed today |
| warning | `RACK-CORDS-SHARE-A-PDU` | Both cords of a dual-fed device land on one PDU |
| warning | `RACK-HEIGHT-ESTIMATED` | 12U of the elevation rests on estimated heights |
| warning | `RACK-NO-AVAILABILITY-POINT` | Some devices cannot report that they have gone silent |
| warning | `RACK-PDU-NOT-MODELLED` | A PDU occupies rack units but has no outlet model |
| info | `RACK-NO-POWER-DATA` | No device carries a power figure |
| info | `RACK-OUTLET-NOT-ON-A-PDU` | A power source is recorded as a device that is not a modelled PDU |

The first two are the ones that matter. **A rack drawn with an A and B feed
looks redundant.** It is not: feed A is the only one with an identified upstream
asset. That is the sort of thing an elevation drawing is very good at hiding and
a computed check is very good at finding.

`RACK-CORDS-SHARE-A-PDU` compounds it: the register also cannot express that a
dual-fed device's two cords land on one PDU, which is the same missing concept
as the N+1 problem in
[Topology § What the analysis will not tell you](./topology-view.md#5-what-the-analysis-will-not-tell-you).

---

## 4. No wattage is invented

The thermal panel reports:

| Field | Value |
|---|---|
| `total_estimated_power_w` | `null` |
| `power_data_status` | `not_estimated` |
| `heat_load_status` | `not_calculated_pending_measured_load` |

with the basis stated in the payload: the design package contains no measured or
nameplate power figures for the rack equipment, and exact measured rack loads are
deliberately unresolved. **No wattage is invented here.**

That is the same rule the whole documentation set follows: where the design
package records `TBD`, the platform reports an open item rather than a plausible
guess. A fabricated heat load would be used to size cooling.

---

## 5. What to use it for

| Question | How |
|---|---|
| *Where does this device live?* | The elevation, or search the device list |
| *Is there room for another 2U server?* | The free blocks. Largest is 18U today |
| *Is this rack actually dual-fed?* | The power panel and the two blocking findings. Today: no |
| *Which devices cannot tell me they have died?* | `RACK-NO-AVAILABILITY-POINT`, and the faceplates reading `no_points` |
| *What happens if I lose this rack?* | [Topology](./topology-view.md) — the rack takes 65 assets with it |

---

## 6. Related reading

| Document | Why |
|---|---|
| [The design package](./design-package.md) | What `not_ratified` means, and the rest of the package |
| [Topology and blast radius](./topology-view.md) | The rack's own blast radius, and the same missing redundancy concept |
| [Integration findings](./integration-findings.md) | Related gaps in the register |
| [API reference](./api.md) | `GET /api/v1/rack` |
