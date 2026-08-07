# Alarms, incidents and notification

How CHAOS decides something is wrong, how it stops one failure becoming a
hundred messages, and what it does — and honestly does not do — about telling a
human.

Related: [The annunciator panel](./annunciator.md) ·
[The operator console § Alarms](./operator-console.md#43-alarms) ·
[Operations § Alarm floods](./operations.md#3-alarm-floods) ·
[Troubleshooting § An alarm never fires](./troubleshooting.md#3-an-alarm-never-fires)

---

## 1. Where the definitions come from

Forty alarm definitions ship in the design package
(`data/alarm_definitions.yaml`) and are loaded into the platform by first-run
setup. Each definition carries severity, scope, a trigger, a reset condition,
operating-context behaviour, correlation hints, escalation and the operator
context.

You see them on the console's **Alarms** screen and, as a fixed grid, on
[the annunciator panel](./annunciator.md).

**The platform is not the safety system.** Nothing in the alarm engine executes
a protective action. Where a definition describes an `automatic_action`, that is
a description of what the *equipment* does on its own, recorded so an operator
knows what to expect — not an instruction the platform carries out.

---

## 2. Severity

`info` · `warning` · `major` · `critical` · `emergency`

Severity chooses notification channels and orders every list and every bay on
the annunciator. It is never carried by colour alone anywhere in the interface.

---

## 3. Lifecycle

```text
detected → active → acknowledged → mitigated → cleared → reviewed
```

| State | Means |
|---|---|
| `detected` | The condition crossed. The on-delay has not elapsed |
| `active` | The condition has held for its on-delay. This is a real alarm |
| `acknowledged` | A named human has seen it. The condition may still be present |
| `mitigated` | A mitigating action was recorded |
| `cleared` | The condition returned to normal |
| `reviewed` | Closed out, with a review note |

### Delays are states, not timers

A threshold crossing creates the alarm in `detected` **immediately**, and it
only reaches `active` once the condition has held for its on-delay. A crossing
that goes away first is closed out as a transient and never notified — **but it
is still a row**, because "the sensor twitched fourteen times last night" is
operational information.

### Reset is a separate condition, not the negation of the trigger

Every definition carries its own reset operator and value; where it does not,
the reset is derived from the trigger plus hysteresis. Either way there is a
real deadband, so a value hovering on the trip point cannot chatter.

### Bad data is not good news

A dead sensor must not look like a healthy in-range reading. A process alarm
whose input quality is `bad` or `stale` is **not evaluated at all**; the
definition's data-quality alarm is raised against that specific point instead.

That rule is why `sensor_data_invalid` and `energy_meter_data_invalid` exist as
definitions in their own right.

### Maintenance mode modifies alarms; it does not delete them

A suppressed alarm is stored with a machine-readable reason, so an operator can
see what was hidden and why. Suppressing an alarm definition, by contrast, hides
the next occurrence too — which is why the runbook for planned work is
maintenance mode, not suppression. See
[Control § Operating modes](./control.md#5-operating-modes).

---

## 4. Operator context — the FR-008 payload

Every definition can be asked for its operating procedure, affected assets,
dependencies and manual controls.

**An alarm that arrives without that is a noise generator.** It is also a
commissioning step: step 10 requires confirming that the alarm links to its
context before the subsystem is signed off.

---

## 5. Correlation and incidents

> A power-container outage should not create hundreds of separate notifications
> without a parent incident.

Three mechanisms, applied in that order of authority:

**1. Declared parentage.** A definition names a parent alarm key. When that
parent is open and in scope, the child is a symptom: it joins the parent's
incident and is not notified on its own.

**2. Dependency correlation.** The registry's typed relationships — `feeds`,
`contains`, `hosts`, `located_in`, `part_of`, `depends_on`, `managed_by` —
describe which assets sit downstream of which. An alarm on an asset downstream
of an already-alarming asset, inside the correlation window, joins that asset's
incident.

This is a **real graph walk over the register**, not a table of alarm keys, so a
relationship added to the register tomorrow correlates without a code change. It
is the same walk the [topology screen's blast radius](./topology-view.md#3-blast-radius)
performs, which is why the set you see on the canvas is the set the correlator
will fold.

**3. Flood guard.** More than *N* open alarms of the same definition, or more
than *N* inside one subtree, inside the window opens a single incident and
suppresses the per-alarm notification.

### Suppressed never means discarded

Incident membership marks member alarms with a `symptom:` or `flood:` reason.
That flag means *do not notify this alarm on its own* — the incident is notified
instead. The alarm is still a row, still queryable, still on the record.

### Some alarms refuse to be anybody's symptom

Folding certain alarms into an incident would hide exactly the thing they exist
to report. Those definitions declare no parent and are marked as incident
anchors instead:

- the **secondary control node unreachable** alarm — the whole point of that
  node is that it is independent;
- the **local alarm beacon unavailable** alarm — it has to be able to speak for
  itself;
- the **BMS discharge inhibit** alarm — a live load with a battery that will not
  discharge is not a symptom of anything.

There is a hard irony worth knowing: the alarm beacon is itself inside the
container it reports on, so it dies with the fire it would announce. See
[Topology § The answer the design document asks for](./topology-view.md#4-the-answer-the-design-document-asks-for).

---

## 6. Serviceability: a dark tile is a claim

The annunciator gives every definition a tile, always. That makes a dark tile a
positive claim that the condition is normal — which is only true if the tile can
light at all.

So serviceability is computed per definition, and a definition that **cannot
fire** is reported `out_of_service` rather than dark. See
[The annunciator panel § Out of service](./annunciator.md#6-out-of-service--the-honest-dark-tile)
for the full rules and the list.

On the shipped definition set, **ten of the forty cannot fire**, including all
three in the SAFETY bay. Each is enabled, appears healthy in the alarm list, and
has a trigger point that does not exist for its asset.

This is the single most important thing to understand about the current alarm
coverage. Full write-up:
[Integration findings F-007](./integration-findings.md#f-007--ten-of-the-forty-alarms-can-never-fire).

---

## 7. Notification, and the honest part

**Only the `log` channel has a working implementation.**

`email`, `push` and `voice` are honest stubs. They record a delivery-log row with
status `not_configured` and a detail explaining exactly what is missing. **They
never return success**, and nothing reports that an alert was delivered when it
was not.

An alerting path you believe in but that does not exist is worse than no
alerting path.

### Routing rules

| Rule | Detail |
|---|---|
| **The incident is the unit of notification, not the alarm** | A member alarm of an incident is suppressed by the correlator and never notified individually. That is what stops a container outage becoming a hundred messages |
| **Severity chooses the channels** | A definition's escalation path can override the routing per stage |
| **Escalation is time-driven and explicit** | Stage *N* fires once its delay has elapsed since the alarm activated, and **only while the alarm is still unacknowledged** |
| **Acknowledging stops escalation** | It does not clear the alarm |
| **Re-notification** | Repeats the highest reached stage at a fixed interval for alarms that stay unacknowledged |

**Every attempt on every channel is a row**, so *"why did nobody get called"* is
answerable after the fact. That log is on the console.

### What this means for the acceptance criteria

One of the platform's MVP criteria requires critical alarms to work during
internet loss. With only `log` wired, that criterion is **not met** — a log line
is not an alert. Meeting it needs a local audible or voice path, or an
independent device path such as the environmental appliance's own alerting.
Recorded as an open item in
[Secondary control node § Open decisions](./secondary-control-node.md#6-open-decisions).

---

## 8. Where to act on an alarm

| Surface | Best for |
|---|---|
| [The annunciator panel](./annunciator.md) | Deciding whether anything is wrong, at a glance, from across the room. Acknowledge and reset |
| [The console's Alarms screen](./operator-console.md#43-alarms) | Working an incident: the member alarms, their history, mitigate and review |
| [Operations § Alarm floods](./operations.md#3-alarm-floods) | What to do — and what not to do — when dozens arrive at once |

Every action collects a mandatory reason. The API requires it, and the dialog
exists so you learn that from the interface rather than from a rejection.

---

## 9. Related reading

| Document | Why |
|---|---|
| [The annunciator panel](./annunciator.md) | The panel view, the sequence, out-of-service tiles |
| [Integration findings](./integration-findings.md) | F-007 and F-007a: the alarms that cannot fire |
| [Control](./control.md) | Operating modes, and why maintenance mode beats suppression |
| [Commissioning § Step 10](./commissioning.md#step-10--alarm_and_notification_test) | Proving an alarm end to end before signing off a subsystem |
| [API reference § Alarms](./api.md#9-alarms) | The endpoints behind all of it |
