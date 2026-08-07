# Troubleshooting

Organised by **what you are looking at**, not by subsystem — because when
something is wrong you know the symptom, not the cause.

Every entry gives the GUI remedy first. Where a symptom has a known root cause
in the design package rather than in the software, it says so and links to the
finding.

Related: [Operations runbooks](./operations.md) ·
[The desktop shell](./desktop-shell.md) ·
[Getting started](./getting-started.md)

---

## Before anything else

**Two sentences that are never the same thing.**

> *"The shell cannot see the platform"* is not *"the platform has stopped."*

The whole product is written to keep those apart. Every screen that reports a
failure says which one it means, and none of them will tell you the platform is
down on the strength of a failed poll. Read the wording carefully before driving
to a container in the dark.

**And the other one:**

> *"No alarms"* is not *"we cannot tell you about alarms."*

The tray icon has separate artwork for unreachable and stale. The annunciator
says `NO DATA` rather than going quiet. The alarm badge shows a question mark,
never a zero, when the count is unknown.

---

## 1. The shell shows a diagnostic panel instead of the console

The console area is replaced by a titled panel with facts and next steps.

### Read the title first

| Title | Cause |
|---|---|
| **Cannot reach the CHAOS gateway** | The gateway did not answer inside the startup budget |
| **WebView2 runtime is not installed** | The shell cannot render a web view at all |

### If it says "Cannot reach the CHAOS gateway"

The panel already lists the gateway address, the readiness probe URL, how many
attempts were made, how long it waited, the last error, the Windows service name
and the shell log location. It also says, deliberately:

> This means the shell cannot see the platform. It does not by itself mean the
> platform has stopped: control, alarm evaluation and logging run in the Windows
> service, independently of this window.

**Do this, in order:**

1. Press **Retry** on the panel.
2. Press **Start screen**. The launcher's five checks separate the possibilities
   far better than the diagnostic can — in particular whether the Windows
   service is running and whether `Chaos.Host.exe` was found.
3. If the launcher says the gateway is not answering and offers **Start
   platform**, press it.
4. If the launcher says the service is stopped, press **Start platform**; if it
   reports that this needs administrator rights, press **Restart as
   administrator**.
5. If the address is wrong — you moved the platform to another node, or changed
   its port — open **Settings → Gateway** and correct it.
6. Press **Open logs folder** and read the newest shell log.

### If it says "WebView2 runtime is not installed"

The platform is unaffected. The same console is still served to every browser on
the LAN, and the panel gives you the URL.

1. Open that URL in any browser to keep working.
2. Install the Microsoft Edge WebView2 Evergreen Runtime and restart the shell.
   The installer is redistributable and works offline — keep a copy on the node.

### What you should never see

A blank white window. The console is kept collapsed until the gateway answers
precisely so that a hung launch is never indistinguishable from a broken one. If
you do get a blank frame, that is a defect worth reporting, with the shell log.

---

## 2. The annunciator is dark

Work through these in order — they are ordered by how often each is the answer.

### 2.1 Is it dark, or is it not there?

Look at the link state in the top-right corner of the panel.

| It says | Meaning | Do |
|---|---|---|
| `linked` | The panel has current data. The tiles are real | Go to 2.2 |
| `NO DATA · last 34s ago` | Polls are failing. **What you are looking at is 34 seconds old** | Go to section 5 or 6 |
| `NO DATA · <error>` | Never reached the platform | Go to section 5 |
| `PANEL FAULT · <message>` | Data arrived; the panel failed to draw it | This is a rendering fault, not a network fault. Reload the panel; if it recurs, report the message |

A panel that silently freezes would be worse than one that says it is blind,
which is why that indicator exists. Trust it.

### 2.2 The tiles are dark and the link says `linked`

Then the platform genuinely has no active alarms — **for the conditions that are
capable of being detected.** Check the `OUT OF SVC` count in the header.

| Count | Meaning |
|---|---|
| `0` | Every definition can fire. A dark panel is a trustworthy dark panel |
| `10` on the shipped definition set | Ten definitions cannot fire at all, including all three in the SAFETY bay |

Click any `OUT OF SVC` tile. The detail pane names the missing trigger point and
says:

> This window cannot light, so its dark state is not evidence that the condition
> is normal.

That is section 3.

### 2.3 The whole panel is black with no tiles at all

The panel has not loaded its data yet. In the shell it shows `ANNUNCIATOR /
Loading panel…` on the same black background. If it stays there, the gateway is
not answering — section 5.

### 2.4 The horn is silent when a tile is flashing

| Cause | Fix |
|---|---|
| Nobody has clicked the page yet | Browsers block audio until the user interacts. **Click anywhere.** Until then the panel says it is silent rather than pretending to have a working horn |
| The **Audible** toggle is off | Turn it on, bottom right |
| SILENCE HORN was pressed | Correct behaviour. A *newly* lit window re-arms the horn automatically |
| The tile is `acknowledged`, not `alarm` | Correct behaviour. Acknowledged means someone took it |

---

## 3. An alarm never fires

The condition happened. Nothing lit, nothing was logged, nothing was sent.

### 3.1 Check serviceability first — this is usually the answer

Open the annunciator and find the alarm's tile.

- Reads **`OUT OF SVC`** → it *cannot* fire. Click it; the pane names the exact
  reason. This is not a fault in the alarm engine.
- Reads **`INHIBITED`** → it fired and was deliberately not annunciated, because
  maintenance mode is active for its scope or the correlator folded it into an
  incident as a symptom. Check the console's Alarms screen: it is there, with its
  reason.
- Is **dark** and serviceable → go to 3.2.

The ten definitions that cannot fire on the shipped package are listed in
[The annunciator panel § Out of service](./annunciator.md#6-out-of-service--the-honest-dark-tile).
Fixing them is a change to the design package — add the trigger points to their
asset classes or bindings — not a change to the platform.

### 3.2 The alarm is serviceable and still did not fire

| Check | Where | What you are looking for |
|---|---|---|
| Did the value arrive at all? | Console → Assets → the asset → its points | A current value with a recent timestamp |
| Is the value's quality usable? | Same screen | **A process alarm whose input quality is `bad` or `stale` is not evaluated at all.** The data-quality alarm is raised against the point instead |
| Did it cross far enough, for long enough? | The definition's threshold and on-delay | A crossing that goes away before the on-delay elapses is closed out as a transient — still recorded, never notified |
| Did the message arrive but fail to resolve? | Console → the dead-letter list | A message that arrived and could not be matched to a registry point. **During commissioning this is far more common than a dead sensor** |
| Was the whole scope in maintenance? | Console → Control → operating modes | Maintenance mode modifies alarms with the lockout visible |

### 3.3 It fired and nobody was told

Check the notification delivery log on the console. Every attempt on every
channel is a row, so *"why did nobody get called"* is answerable after the fact.

Rows with status `not_configured` mean exactly that: only the `log` channel has
a working implementation. `email`, `push` and `voice` record the attempt and
never claim success. See [Alarms § Notification](./alarms.md#7-notification-and-the-honest-part).

---

## 4. Setup says `needs_attention`

**This is the gateway refusing to touch a database it does not fully understand.
Nothing has been changed.** That is the whole point of the state.

Open the launcher. The setup summary names which of the four cases you are in.

| The summary says | It found | Do |
|---|---|---|
| *"could not open its database"* | The platform could not open the database at all | Check the database path in **Settings → Where the data lives**. Confirm the target exists and is writable. If a service is running the platform, the service reads its own configuration — see the warning below |
| *"holds N row(s) but is missing M table(s)"* | Not a fresh install and not a complete one | This database may hold alarm and telemetry history. Do not force. Restore a known-good backup, or move this database aside and let setup build a fresh one |
| *"the design package is only partly loaded"* | What an interrupted import looks like | Press **Run again**. The import upserts and never drops, so re-running it is safe — the gateway simply will not do it unattended |
| *"the registry is empty, but this database already holds N row(s) in other tables"* | Something else is already recording here | Decide deliberately. If this is the right database, press **Run again** |
| *"The design package cannot be loaded"* | `data/` is missing or holds no YAML documents | Point **Settings → Data directory** at the platform's `data/` folder, or repair the install |

### The Settings warning that matters

**Database and data-directory settings only apply to a platform this shell
starts itself.** A Windows service reads its own configuration, installed once
by the installer, and the shell does not rewrite it. The settings page says so
at the time you save. If a service is running the platform, changing the path in
the shell will not move anything.

### `Run again` versus forcing

**Run again** re-checks and repairs anything the platform needs, and is safe on a
working system. Forcing proceeds when automatic setup is off, or when the
database was assessed as needing attention — and it **never makes setup
destructive**: the same table creation and design-package import run either way,
and neither drops anything.

### The other setup states

| State | Meaning | Do |
|---|---|---|
| `not_started` | The gateway has not looked. Not evidence either way | Press **Set up now**, or **Check again** |
| `checking` / `running` | In flight | Wait. The launcher shows progress |
| `failed` | A step failed | Press **Retry**. Read the setup log via **Open logs** |
| `ready` | Nothing to do | — |

---

## 5. The platform will not start

Open the launcher and read the five checks. They are designed to separate
exactly this.

### 5.1 Gateway check fails, service check says "running"

Windows thinks the service is running; nothing answers on the address.

1. Confirm the address in **Settings → Gateway**. The gateway listens on port
   **8080** by default.
2. Check whether the port is right for *this* node — the installer's firewall
   rule opens exactly the configured port, inbound, private profile only.
   Changing the port without changing the rule leaves the service running and
   unreachable.
3. Press **Check again**.
4. Read the newest gateway log via **Open logs**.

### 5.2 Gateway check fails, service check says "stopped"

Press **Start platform**. If it reports that this needs administrator rights,
press **Restart as administrator** and try again — that action closes the shell
and reopens it elevated, and **does not touch the platform**: whatever is running
keeps running.

### 5.3 Gateway check fails, service check says "not installed"

That is a **warning, not a fault**. A development checkout or portable copy is a
legitimate way to run this — though on a homestead node it means nothing
survives a sign-out.

- If the **Platform executable** check found `Chaos.Host.exe`, press **Start
  platform**. The shell starts it as a child process, and the run-mode banner
  will then read *managed by this shell* — which means **closing the shell stops
  the platform**.
- If it did not find the executable, set its location in **Settings →
  `Chaos.Host.exe` location**, or install the product properly.
- If the service *is* installed under a different name, correct **Settings →
  Windows service name**. This is worth checking first: the shell looks for
  `ChaosPlatform` by default, while the gateway currently registers itself under
  `ChaosHost`. A running service under the other name reads as *not installed*.

### 5.4 The launcher says it cannot start it at all

The headline reads *"The platform is not answering, and this shell cannot start
it"*, and the summary explains why. It also adds, correctly:

> The platform may still be running somewhere this shell cannot see — check the
> node directly before assuming it has stopped.

### 5.5 The gateway answers but the backend is down

Headline: *"The platform is only half up"*. The console will open and have
nothing to show; `/health` answers 503 with `backend: "down"`.

1. Press **Check again** — a cold start on a low-power node takes a minute or
   so, and the backend check distinguishes *starting* from *down*.
2. Read the backend detail on the check row. It carries the gateway's own last
   error.
3. **On a development build this is expected.** The gateway's shipped entry
   point does not register a backend supervisor, so nothing starts the Python
   backend for it. See
   [Visual Studio § The gap you should know about](./visual-studio.md#the-gap-you-should-know-about).

### 5.6 It starts, then stops, then starts again

Restart behaviour is deliberate: a homestead does not have someone watching the
console, so the supervisor keeps restarting with a backoff rather than giving up
after a counter. The alarm for *"the backend is crash-looping"* is the operator's
signal, not a give-up threshold.

Read the platform log via **Open logs** — the repeated failure is at the top of
each restart.

---

## 6. The console loads but has no data

### 6.1 Look at the freshness indicator first

Top right of the console. It starts at *Never updated* and never lies. If a
banner says **API unreachable — showing the last values received. Do not read
this screen as live**, then you are looking at a snapshot, not the property.

Go to section 5.

### 6.2 The data is current and the screens are empty

Then the platform is up and genuinely has nothing to show. Which is correct on a
fresh install: nothing has ever reported.

| Screen | Empty means |
|---|---|
| **Home** | No telemetry has arrived. The site state will read *Pre-deployment* |
| **Assets** | Setup has not run — check the launcher. After setup, 90 assets, mostly `planned` |
| **Energy** | The energy manager has not published a state snapshot yet |
| **Map** | Nothing has coordinates. The screen shows the **survey backlog** instead of an empty rectangle, and that list is the actionable information |
| **Rack** | The layout document could not be read. The payload names the file and why |

### 6.3 Numbers are there but stale

Every value carries its age and quality, so this is visible rather than
insidious. A whole screen going stale at once usually means telemetry stopped,
not that the console broke — see
[Operations § Communications loss](./operations.md#2-communications-loss).

**Check clocks before anything else** if a time-filtered view is empty while
counts are rising. Clock skew makes alarm correlation wrong, command expiries
wrong, and time-windowed queries silently return nothing.

---

## 7. A command was refused

This is usually the platform working, not failing.

The Control screen shows the **whole interlock evaluation**, not just the first
objection. Read which one denied:

| Interlock | Means | Remedy |
|---|---|---|
| `physical_control_disabled` | The platform-wide gate is off. It ships off | Do not turn it on to make an error go away. Follow [Commissioning](./commissioning.md) |
| `asset_not_operational` | The asset does not physically exist yet, or must not be commanded | Check its lifecycle status on the Assets screen |
| `point_not_control_capable` | You are commanding a measurement | Command the setpoint or command point instead |
| `binding_not_commissioned` | No verified address, or the binding is not commissioned | This is the interlock commissioning exists to satisfy |
| `emergency_mode_lockout` | A scope is latched into emergency | Only protective commands run. Clearing the latch needs a named human asserting the condition is gone |
| `asset_in_maintenance` | A maintenance lockout is in the mode chain | Either take it out of maintenance, or use a maintainer override — which is recorded |
| `stale_input` | A measurement the decision depends on is missing, bad or stale | Fix the measurement. Do not command a valve whose position feedback has failed |

**Use dry run first, always.** It evaluates every interlock and publishes
nothing.

---

## 8. Something else

| Symptom | Look at |
|---|---|
| The tray icon shows a question mark | The shell cannot see the platform, so it has no alarm count. Never read as zero. Section 5 |
| Closing the window seems to have stopped everything | Read the run-mode banner. If it said *managed by this shell*, it did. Section 5.3 |
| Settings saved but nothing changed | The settings page's **warnings** say what will not take effect and why. Re-read them |
| The shell opens a second window | It should not — a second launch is redirected to the running instance |
| Two `CHAOS_` variables seem to conflict | They are two namespaces. The gateway binds `CHAOS_<PascalCase>`; the platform binds `CHAOS_<SCREAMING_SNAKE_CASE>` |
| An endpoint 404s that should exist | `GET /host/routes` is the authoritative live view of who serves what |

---

## 9. Getting more detail

| Want | Where |
|---|---|
| The shell and platform logs | Launcher → **Open logs**, or the diagnostic panel's **Open logs folder** |
| What the gateway thinks it is | `http://<node>:8080/host/info` — version, listen and backend addresses, web-root resolution, supervisor state, route counts |
| Who serves which route | `http://<node>:8080/host/routes` |
| The full setup report | `http://<node>:8080/host/setup` — always 200; a report that a machine needs setting up is a successful report |
| Gateway health including the backend | `http://<node>:8080/health` — 200 only when the backend is up |
| Whether this process is merely alive | `http://<node>:8080/health/live` |

For a node with no display at all, the command line is the right tool and it is
documented in [Command line](./advanced-command-line.md).

---

## 10. Related reading

| Document | Why |
|---|---|
| [Operations runbooks](./operations.md) | Procedures for events, not symptoms: black start, comms loss, floods, backup |
| [Getting started](./getting-started.md) | What a healthy first launch looks like |
| [The desktop shell](./desktop-shell.md) | Every state the launcher and diagnostics can show |
| [Integration findings](./integration-findings.md) | The known gaps that cause several of the symptoms above |
