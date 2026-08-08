# The annunciator panel

A control-room alarm panel: a fixed grid of engraved windows, one per alarm
condition, in a position that never moves — with a horn, a lamp test, and the
conventional acknowledge/reset sequence.

It is the screen an operator looks at to decide whether anything is wrong. Every
design decision in it follows from that.

Related: [Alarms, incidents and notification](./alarms.md) ·
[The operator console](./operator-console.md) ·
[The desktop shell § The annunciator window](./desktop-shell.md#5-the-annunciator-window) ·
[Troubleshooting § The annunciator is dark](./troubleshooting.md#2-the-annunciator-is-dark)

---

## 1. Opening it

| From | How |
|---|---|
| The operator console | The **Annunciator** button, top right. It opens a second browser window and carries a badge of lit windows |
| The desktop shell | Command strip → **Annunciator**, or tray → **Open annunciator** |
| Directly | `http://<node>:8080/ui/annunciator.html` |

Pressing the button again focuses the existing window rather than opening a
second one.

In the shell, the window opens on the display nominated in Settings, optionally
full-screen for a permanently mounted wall panel, and keeps that display awake
while it is full-screen.

---

## 2. Why a panel rather than a list

A hardwired annunciator is a fixed grid. Operators learn it by muscle memory:
the tile in the third row of the energy bay *is* battery reserve, lit or not.

So this panel gives **every alarm definition a tile, always** — whether or not
it is currently in alarm. It is not a list of what is wrong; it is a map of
everything that could be wrong, with the wrong things lit.

The consequence of a fixed grid is the thing that makes the rest of the design
necessary:

> **A dark tile is a positive claim.** It says *this condition is normal*.

That claim is only true if the tile is capable of lighting. Section 6 is about
what happens when it is not.

---

## 3. What you should see

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ ANNUNCIATOR                    0 ALARM  0 ACK'D  0 RINGBACK       14:22:07 │
│ PROJECT CHAOS · MASTER ALARM PANEL   0 INHIBIT  10 OUT OF SVC      linked  │
├───────────────────────────────────────────────────────────────────────────┤
│ ENERGY AND POWER                                                          │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │
│ │BATT SOC  │ │BATT      │ │INVERTER  │ │GEN START │ │SOURCE XFER│  …      │
│ │LOW       │ │RESERVE   │ │FAULT     │ │FAILED    │ │FAILED     │         │
│ └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘          │
│                                                                           │
│ SAFETY                                                                    │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐                                    │
│ │RACK SMOKE│ │PWR CONTNR│ │ALARM     │                                    │
│ │DETECTED  │ │FLUID     │ │BEACON    │                                    │
│ │OUT OF SVC│ │DETECTED  │ │UNAVAILBLE│                                    │
│ └──────────┘ │OUT OF SVC│ │OUT OF SVC│                                    │
│              └──────────┘ └──────────┘                                    │
│ …                                                                         │
├───────────────────────────────────────────────────────────────────────────┤
│ [ACKNOWLEDGE] [SILENCE HORN] [RESET] [LAMP TEST]   [x] Audible  [ ] On-delay │
└───────────────────────────────────────────────────────────────────────────┘
```

**Header:** the panel identity, five live counts (ALARM, ACK'D, RINGBACK,
INHIBIT, OUT OF SVC), a wall clock, and the link state.

**Body:** one section per bay. A bay's heading shows its title and, when
anything in it is lit, `— n LIT`.

**Footer:** the four panel controls, two toggles, and the current operator
identity.

### The bays, in the order they are hung

`ENERGY AND POWER` · `SAFETY` · `CONTAINER AND STRUCTURE` ·
`SERVER, NETWORK AND COMMS` · `SECURITY` · `WATER` · `AGRICULTURE` ·
`STORAGE` · `SPA` · `PLATFORM`

A domain not in that list still gets a bay, appended alphabetically, so a new
domain can never silently vanish from the panel. On the shipped definition set,
six bays are populated:

| Bay | Tiles |
|---|---:|
| ENERGY AND POWER | 21 |
| SERVER, NETWORK AND COMMS | 9 |
| SECURITY | 4 |
| SAFETY | 3 |
| CONTAINER AND STRUCTURE | 2 |
| PLATFORM | 1 |

Within a bay, tiles are sorted worst severity first, then by key — so the eye
lands on the worst thing in each bay and **the position stays stable between
polls**.

### The legends

Tile text is a hand-cut engraved legend, one per shipped alarm: `BATT SOC /
LOW`, `SOURCE XFER / FAILED`, `PWR CONTNR / FLUID / DETECTED`. On a real panel
these are engraved by someone who thought about what an operator needs to read
at three metres in bad light.

Auto-generation was tried first and lost the operative word on 18 of the 40:
*"Source transfer did not complete"* became `SOURCE TRANSFER DID NOT`, which is
worse than no legend, and *"Power container fluid detected"* lost `DETECTED`.
The generator survives as the fallback for definitions added later, and it keeps
the **last** word above all others — because that is where the meaning usually
sits. A legend that drops its verb is not a shorter legend, it is a wrong one.

---

## 4. The sequence

The panel implements the conventional annunciator sequence — ISA-18.1 **F3A**,
"ringback":

```text
normal        →  dark, silent
abnormal      →  fast flash + horn            (unacknowledged)
ACKNOWLEDGE   →  steady lamp, horn silent     (still abnormal)
returns       →  slow flash + ringback tone   (waiting to be reset)
RESET         →  dark
```

| Tile state | Looks like | Means |
|---|---|---|
| `normal` | Dark | Condition normal, and that is a trustworthy statement |
| `alarm` | Fast flash, horn, `ALARM` | Abnormal and unacknowledged |
| `acknowledged` | Steady lit, horn silent, `ACK'D` | Abnormal, acknowledged |
| `ringback` | Slow flash, ringback tone, `RESET REQ` | Returned to normal, not yet reset |
| `inhibited` | `INHIBITED` | Suppressed by maintenance mode, or folded into a correlated incident. Recorded, deliberately not annunciated |
| `out_of_service` | `OUT OF SVC` | **This tile cannot light.** See section 6 |

### Two rules that are load-bearing and not configurable

**1. SILENCE HORN silences the horn and nothing else.** It never changes a lamp
and never acknowledges an alarm. Conflating the two is how a panel ends up quiet
and dark with the condition still present.

**2. ACKNOWLEDGE writes through to the platform.** It records a named operator
and an audit note against the alarm. The panel does not keep a private idea of
"acknowledged" that the rest of the platform cannot see — with one exception: if
the write fails, **the lamp stays unacknowledged** and the failure is shown,
rather than the panel pretending the acknowledgement landed.

### Silencing never masks the next thing

The horn goes quiet the instant ACKNOWLEDGE is pressed, as on a real panel: the
operator has taken the alarm. The lamps only change once the write lands.

And a **newly lit window re-arms the horn** even if it was silenced for an
earlier alarm. Silencing must never mask the *next* thing that happens.

---

## 5. The controls

| Control | Key | Does |
|---|:--:|---|
| **ACKNOWLEDGE** | `A` | Acknowledges every tile currently in `alarm`. Silences the horn immediately; lamps follow the write |
| **SILENCE HORN** | `S` | Audible only. Lamps unchanged. Disabled when nothing is sounding |
| **RESET** | `R` | Clears every `ringback` tile by closing it out with a review note |
| **LAMP TEST** | `T` | Illuminates every window and sounds the horn for four seconds, then restores the previous state — including whether the horn was silenced |
| `Escape` | | Closes the detail pane |

Both ACKNOWLEDGE and RESET require at least the `operator` role, and are disabled
when there is nothing for them to act on. The identity shown in the footer is the
one the console holds; set it from
[the console's operator button](./operator-console.md#3-operator-identity).

| Toggle | Default | Does |
|---|---|---|
| **Audible** | On | Turns the horn and ringback tone off entirely, for a quiet room |
| **Show on-delay** | Off | Also lights tiles for alarms still inside their on-delay window |

### The horn

The horn is **synthesised, not loaded from a file**: the panel must work with no
network, and shipping an audio asset for one tone is not worth the cache and
content-security surface.

| Sound | Character | Why |
|---|---|---|
| Horn | Low, square, insistent, ~3 pulses per second | A sound you cannot ignore or mistake for a chime |
| Ringback | Higher, softer, slower, quieter | "Something returned to normal" is information, not an emergency, and must not sound like one |

Browsers block audio until the user interacts with the page, so **the first
click anywhere unlocks the horn**. Until then the panel is visually complete and
silent — and says so, rather than appearing to have a working horn it does not
have.

### Clicking a tile

A detail pane opens with: the legend, the full name, status, severity, domain,
alarm key, how long it has been in that state, its incident number, how many
other copies are open, its threshold status, and whether it requires a manual
reset. Acknowledge and Reset buttons appear there for that tile alone when your
role allows them.

For an `out_of_service` tile the pane adds the reason, and this sentence:

> This window cannot light, so its dark state is not evidence that the condition
> is normal.

For an `inhibited` tile:

> The alarm is recorded but deliberately not annunciated.

---

## 6. Out of service — the honest dark tile

Because a dark tile is a positive claim, the panel computes **serviceability**
for every definition on every poll, and reports a tile that cannot light as
`out_of_service` rather than `normal`.

That is the software equivalent of the paper OUT OF SERVICE tag taped over a
window on a real panel, and it is what makes the dark tiles trustworthy.

A tile is out of service when:

| Condition | Reason shown |
|---|---|
| The definition is disabled | *Definition is disabled.* |
| It has an asset and the trigger point does not exist for that asset | *Trigger point `<name>` does not exist for `<asset>`. The condition cannot be evaluated, so this tile can never light.* |
| It is class-scoped and no asset of that class is in the register | *No asset of class `<class>` is in the register, so this class-scoped alarm has nothing to watch.* |
| It is class-scoped and the trigger point exists on none of them | *Trigger point `<name>` does not exist on any of the `<n>` `<class>` assets.* |
| It has neither a trigger point nor a trigger expression | *No trigger point and no trigger expression.* |

The check is deliberately conservative: **when scope cannot be resolved the tile
is reported out of service**, because an unlit tile claiming "normal" is the
exact failure this exists to prevent.

### On the shipped definition set, ten of forty are out of service

| Alarm | Severity | Missing trigger point |
|---|---|---|
| `rack_smoke_detected` | critical | `smoke_active` |
| `power_container_water_ingress` | critical | `leak_active` |
| `power_container_ac_bus_lost` | critical | `energized_state` |
| `alarm_beacon_unavailable` | major | `availability_state` |
| `battery_cell_imbalance` | major | `cell_voltage_delta_mv` |
| `inverter_overload_risk` | major | `power_ac_kw` |
| `rack_door_forced_open` | major | `forced_open_active` |
| `rack_door_open_extended` | warning | `door_state` |
| `storage_capacity_high` | warning | `storage_used_pct` |
| `time_sync_drift` | warning | `clock_offset_ms` |

**The whole SAFETY bay is in that list.** Smoke detection and water ingress in
the 20-foot container that holds the batteries, the power conversion equipment
*and* the server rack cannot raise an alarm — and that container is the
common-mode failure domain the design is explicitly written about. The beacon
alarm that would report the local siren dead is inoperable too, so the
independent alerting path has no health check either.

An alarm list showing "40 defined, 0 active" reads as full coverage. A quarter
of it is incapable of firing. The panel is the thing that makes that visible.

Full write-up and the fix:
[Integration findings F-007](./integration-findings.md#f-007--ten-of-the-forty-alarms-can-never-fire).

---

## 7. When the panel cannot see the platform

The link state in the top right is the panel's own honesty about itself. A panel
that silently freezes is worse than one that says it is blind.

| Link state | Meaning |
|---|---|
| `linked` | The last poll succeeded |
| `NO DATA · last 34s ago` | Polls are failing; the tiles you are looking at are that old |
| `NO DATA · <error>` | Never reached the platform at all |
| `PANEL FAULT · <message>` | The data arrived and the panel failed to draw it |

**`PANEL FAULT` is deliberately a different message from `NO DATA`.** A
rendering fault is not a communications fault, and reporting it as "no data"
would send an operator to check the network while the panel quietly failed to
draw the alarm they needed.

The panel polls every three seconds and declares itself stale after twelve.

---

## 8. Accessibility

- Every tile's accessible name carries what colour and flash convey visually —
  *"Battery state of charge low. major. ALARM."* — so a screen reader hears the
  same three facts: what, how bad, what state.
- Status is a word on the tile (`ALARM`, `ACK'D`, `RESET REQ`, `INHIBITED`,
  `OUT OF SVC`), never colour alone.
- The counts region announces changes politely.
- Every control is reachable by keyboard, and the four panel buttons have single
  keys.

---

## 9. Related reading

| Document | Why |
|---|---|
| [Alarms, incidents and notification](./alarms.md) | The model behind the tiles: lifecycle, severity, correlation, notification |
| [Integration findings](./integration-findings.md) | Why ten tiles are out of service, and what closes it |
| [The operator console](./operator-console.md) | The list view of the same alarms, grouped by incident |
| [Operations § Alarm floods](./operations.md#3-alarm-floods) | What to do when the panel lights up |
| [API reference § Alarms](./api.md#9-alarms) | `GET /api/v1/annunciator` and the alarm endpoints |
