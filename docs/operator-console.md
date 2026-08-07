# The operator console

The browser interface to Project CHAOS: eight screens, one polled overview
request driving the chrome, and a freshness indicator that makes it impossible
for anything on screen to rot silently.

It is served by the gateway, so it works in the desktop shell's window and in
any browser on the LAN, and it is exactly the same code in both.

Related: [The desktop shell](./desktop-shell.md) ·
[The annunciator panel](./annunciator.md) ·
[Topology and blast radius](./topology-view.md) ·
[Rack elevation](./rack-view.md) · [Control](./control.md)

---

## 1. Reaching the console

| From | How |
|---|---|
| The desktop shell | Launcher → **Open console**, or tray → **Open console** |
| A browser on this machine | `http://localhost:8080/` |
| A browser on the LAN | `http://<node>:8080/` |
| The shell, in your default browser | Command strip → **Open in browser** |

The console is served at both `/` and `/ui/`, and resolves its own root from the
current path, so it also works behind a reverse proxy that adds a prefix.

**Nothing on this page is fetched from anywhere but this server.** No CDN, no
web fonts, no charting library, no map tiles. Charts are hand-drawn inline SVG
and the map is hand-rolled pan-and-zoom. The console has to render on an
isolated network with the uplink down, which is the condition it matters most
in.

---

## 2. The chrome

Along the top:

| Element | What it does |
|---|---|
| **Site line** | The site identity, or *Connecting…* |
| **State chip** | The property's overall state: Nominal, Degraded, Critical alarm active, EMERGENCY, Pre-deployment, or Unknown |
| **Freshness indicator** | A dot and a phrase — how old the data on screen is. It starts at *Never updated* and never lies about it |
| **Auto-refresh** | Manual, 5 s, 15 s (default), 30 s, 60 s |
| **Refresh now** | Immediate refresh |
| **Wall-display mode** | Toggles a larger, denser-contrast layout for a screen read from across a container. The choice is remembered |
| **Annunciator** | Opens the annunciator panel in a second window, with a badge showing lit windows |
| **Operator** | Your identity. Reads *Not signed in* until you set one |

One polled `GET /overview` request drives the top bar and the alarm badge — one
request, because a wall display has to come up on a local network with no
internet and no patience.

When the API is unreachable a banner appears at the top:

> **API unreachable.** Showing the last values received. Do not read this screen
> as live.

Down the left, the navigation: **Home**, **Energy**, **Alarms**, **Map**,
**Topology**, **Rack**, **Assets**, **Control**. The Alarms entry carries a
count badge.

### Three kinds of nothing

The console never draws a zero it does not have. Across every screen:

| Word | Glyph | Meaning |
|---|---|---|
| Live | ● | A real measurement, with its age and quality attached |
| Stale | ◐ | The last value is older than its stale window. Treat it as unknown |
| No data | ○ | Registered, never reported |
| No points | ○ | Registered, and nothing is defined to report |
| Design only | ◇ | Exists in the design; not deployed |
| Not yet deployed | ⬚ | No such asset exists on the property yet |
| Alarm | ▲ | |
| Degraded | ▼ | |
| Unknown | ? | |

**Colour is never the only signal** — every status and severity is drawn as
glyph plus word plus colour. The colour palette is verified by test for contrast
in both themes and under simulated protanopia, deuteranopia and tritanopia, so
that `stale` is never mistaken for `ok` by someone who cannot tell green from
amber.

---

## 3. Operator identity

Press the **operator button**. A dialog asks for a name and a role.

| Role | May |
|---|---|
| `viewer` | Read everything |
| `operator` | Acknowledge alarms, issue commands, request budget leases, set operating modes |
| `maintainer` | Work orders, inspections, calibrations, commissioning records |
| `administrator` | Registry reload, alarm-definition reload |

Roles are a ladder: `administrator` includes everything below it.

**This is not a login.** The dialog says so:

> Every write is audited against a named actor. Authentication terminates at the
> VPN or reverse proxy; this console only tells the API who you are. An unnamed
> caller is a viewer and can never command anything.

That division is deliberate. The platform is local-first and does not implement
its own login; what it *does* implement is the part that matters for a control
system — every audited action records a named actor. Do not expose the API to an
untrusted network on the strength of that header. See
[Network and trust boundaries](./network-and-trust-boundaries.md).

### Every write asks why

Any action that changes something opens a reason dialog before it is sent:

> **Reason (required — recorded in the audit log)**

The reason is mandatory at the API too, so the dialog exists to teach that from
the interface rather than from a rejection.

---

## 4. The screens

### 4.1 Home

The whole page is painted by one `GET /overview`.

- The site state, with the reason it is what it is.
- Active critical and major alarms.
- Energy reserve, with its data quality alongside — a state derived from
  degraded inputs is a guess, and the screen says so rather than presenting it
  as fact.
- Communications and server status.
- A per-domain roll-up.

Every number carries its provenance: which point it came from, how old it is,
and what quality the platform assigned it. A number without its quality is not a
measurement.

### 4.2 Energy

The energy dashboard, laid out to two rules taken from the design document:

- **Measured values are shown separately from calculated and forecast values.**
  Measured tiles carry their point provenance; derived tiles sit under a heading
  that says they are derived.
- **Every value the state machine uses carries its data-quality indicator**, so
  an energy manager running on stale inputs is obvious rather than reassuring.

You get the current energy state and its history, the load schedule with each
load's shed state, budgets and active leases, and the shed/restore history.

The ten states are `COMMISSIONING`, `MAINTENANCE`, `SURPLUS`, `NORMAL`,
`CONSERVE`, `CRITICAL_RESERVE`, `GENERATOR_SUPPORT`, `EMERGENCY`, `BLACK_START`
and `DEGRADED_SENSOR`. `EMERGENCY`, `MAINTENANCE` and `COMMISSIONING` **latch**
and have to be cleared deliberately — automatic recovery from an emergency state
is precisely what you do not want.

The energy manager is an **allocator, not a relay board**: it publishes a state
and grants time-limited power budgets. The BMS, inverters, generator controller
and local PLCs keep immediate equipment authority.

### 4.3 Alarms

Grouped **by incident first**, then by member alarm. Alarms with no incident are
collected under one honest *"not correlated"* group rather than scattered.

That ordering is the point of the screen: a power-container outage should not
create hundreds of separate notifications without a parent incident. See
[Alarms § Correlation](./alarms.md#5-correlation-and-incidents).

Each alarm shows its severity as glyph plus word plus colour, its lifecycle
state spelled out, and the operator actions available: **acknowledge**,
**mitigate**, **clear**, **review** — each collecting a reason first.

For the panel view of the same data, with a fixed grid and a horn, see
[The annunciator panel](./annunciator.md).

### 4.4 Map

The property map, drawn as inline SVG with hand-rolled pan and zoom. No tile
server, no mapping library, nothing that needs the internet.

**There is no basemap, on purpose.** The property has not been surveyed, and a
decorative satellite backdrop under unplaced assets would imply a precision that
does not exist.

When nothing has coordinates the screen does not show an empty rectangle — it
shows the **survey backlog**, because that is the actionable information.

### 4.5 Topology

The dependency graph as a canvas, plus blast-radius analysis: pick an asset and
see everything that would go with it.

Full treatment: [Topology and blast radius](./topology-view.md).

### 4.6 Rack

The 42U elevation for the primary rack, with live device health on each
faceplate, the power feeds, the switch port plan, and the layout document's own
findings.

Full treatment: [Rack elevation](./rack-view.md).

### 4.7 Assets

The registry browser. Filter by domain, class, lifecycle status, criticality,
tag or free text; open one asset for its points, relationships, dependencies and
containment subtree.

`open_fields` is a **first-class column here, not a footnote**. An asset whose
branch circuit or coordinates are still `TBD` has to be visible as such, because
that is exactly what a commissioning engineer needs to know before touching it.

Lifecycle status is drawn honestly: `planned`, `concept`, `procured` and
`reserve` are all *design only*, not green.

### 4.8 Control

The control presentation, which lays out all seven things the design document
requires before anyone operates anything:

1. actual state
2. requested state
3. local/remote authority
4. interlocks currently preventing operation
5. last command and who issued it
6. manual override status
7. impact on energy and resource budgets

**None of them is a tooltip, a hover, or hidden behind a disclosure triangle.**
Each is a reason an operator might decide *not* to press the button, and a
control screen that hides its refusals teaches people to distrust it.

Below that sits the command console, which shows its **preflight**: exactly what
will be written to the audit log, and every reason the platform would refuse.
Use the dry run first — it evaluates every interlock and publishes nothing.

Full treatment: [Control, interlocks and operating modes](./control.md).

---

## 5. Wall-display mode

The toggle in the top-right switches the whole console to a larger type scale
and a wider contrast budget, for a screen read from across a shipping container
rather than at arm's length. The choice is remembered per browser.

The annunciator panel is the other half of this: it is designed to be readable
at three metres in bad light, and the shell can put it full-screen on a
nominated display. See
[The desktop shell § The annunciator window](./desktop-shell.md#5-the-annunciator-window).

---

## 6. What the console cannot do

- **It does not authenticate you.** See section 3.
- **It does not render the property map on a real map.** GeoJSON is served;
  nothing draws it over a basemap.
- **It does not show Grafana dashboards.** Those exist for the container
  deployment and read the database directly.
- **It cannot make an alarm fire that has no trigger point.** Ten of the forty
  shipped alarm definitions are in that position today — see
  [Alarms § Serviceability](./alarms.md#6-serviceability-a-dark-tile-is-a-claim).

---

## 7. Related reading

| Document | Why |
|---|---|
| [The annunciator panel](./annunciator.md) | The alarm panel, its sequence and its controls |
| [Topology and blast radius](./topology-view.md) | The Topology screen in depth |
| [Rack elevation](./rack-view.md) | The Rack screen in depth |
| [Control](./control.md) | The Control screen in depth, and the eight interlocks |
| [Alarms](./alarms.md) | The alarm model behind the Alarms screen |
| [API reference](./api.md) | The endpoints every screen reads |
