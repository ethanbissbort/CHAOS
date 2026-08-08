# The desktop shell

`Chaos.Shell.exe` is the Windows front end for Project CHAOS: a launcher screen,
the operator console in a window, a tray icon that reports alarm state, native
settings, and the annunciator panel on whichever display you nominate.

It is a **client**. It listens on nothing and controls nothing directly. Every
action it takes goes through the gateway.

Related: [Getting started](./getting-started.md) ·
[The operator console](./operator-console.md) ·
[The annunciator panel](./annunciator.md) ·
[Troubleshooting](./troubleshooting.md)

---

## 1. The one idea the whole shell is built around

**An operator must never be able to close a window without knowing what that
does to their homestead.**

The failure mode is specific: someone learns over weeks that closing the console
is harmless, because the platform is a Windows service. Then, on a machine where
this shell happened to start the platform itself, they close it — and switch off
freeze protection without knowing.

So the shell tracks *who owns the running platform* and repeats it everywhere:

| Run mode | Meaning |
|---|---|
| `WindowsService` | The service is running it. It outlives this shell |
| `ManagedByThisShell` | This shell started `Chaos.Host.exe` and is its parent. It dies with this shell |
| `Foreign` | A gateway answers, but this shell did not start it and no service accounts for it — a console window someone left open, another shell, a debugger. This shell can neither stop it nor promise it will survive |
| `NotRunning` | **Positively established**: no gateway answers *and* the service is installed and stopped |
| `Unknown` | The shell has not looked, or looked and could not tell. Never rendered as "stopped" — not seeing something is not evidence |

That fact appears in four places: the permanent banner on the launcher, the
permanent strip above the console, the wording of the tray's Exit item, and the
exit confirmation itself.

The tray's Exit item is worded for the run mode, so the menu carries the warning
before anything is clicked:

| Run mode | Menu item reads |
|---|---|
| `ManagedByThisShell` | *Exit shell — THIS STOPS THE PLATFORM* |
| `WindowsService` | *Exit shell (platform keeps running)* |
| `Foreign` | *Exit shell (does not stop the platform)* |
| anything else | *Exit shell* |

---

## 2. The launcher window

Title: *Project CHAOS — Start*. It is the first thing you see, and it is where
you come back when the platform stops being usable.

It is not a wall of diagnostics. Top to bottom:

```text
PROJECT CHAOS
<headline>
<one paragraph of summary>

┌──────────────────────────────────────────────────────┐
│ <who owns the running platform>                      │  permanent, never collapsed
│ <the full sentence>                                  │
└──────────────────────────────────────────────────────┘

[progress bar, only while something is running]

<notices>
<the five checks>
<first-run setup rows, when there are any>

[ primary action ] [ other actions… ]

[ Show platform log ]
```

Everything shown is decided by one pure function in `Chaos.Shell.Core` and
rendered verbatim. The XAML lays out boxes; it does not decide what goes in
them. That is why every awkward combination is covered by a unit test.

### Phases

The headline names one of eight phases:

| Phase | Headline | What it means |
|---|---|---|
| Checking | *Starting Project CHAOS* | Contacting the gateway |
| Starting | *The platform is starting* / *is coming up* | Something is starting; waiting for the gateway |
| SettingUp | *Setting up* | First-run setup is running |
| SetupRequired | *This platform has not been set up yet* / *Setup did not finish* / *Setup needs attention* | The gateway is up; the database is not ready |
| Ready | *Project CHAOS is ready* | Gateway answering, backend up, setup ready |
| BackendDown | *The platform is only half up* | The gateway answers; it reports its backend is down |
| PlatformDown | *The platform is not answering* | Nothing answered, and there is something you can press |
| Blocked | *The platform is not answering, and this shell cannot start it* | Nothing answered and no way to start it from here |

`Blocked` deliberately adds: *"The platform may still be running somewhere this
shell cannot see — check the node directly before assuming it has stopped."*

### The five checks

Each is a label, a state, and a sentence. States are pass, warn, fail, checking,
unknown or not-applicable — and **every check starts unknown and only becomes
pass on evidence**.

| Check | Notes |
|---|---|
| **Gateway at `<address>`** | Pass reports the gateway's version. Failure says: *"This means the shell cannot see the platform; it does not by itself mean the platform has stopped"* |
| **Windows service `<name>`** | Running is a pass. **Not installed is a warning, not a fault** — a development checkout or a portable copy is a legitimate way to run this, though on a homestead node it means nothing survives a sign-out. Stopped or paused is a failure |
| **Platform executable** | Not-applicable when a running service already accounts for the platform. Otherwise it reports where `Chaos.Host.exe` was found, or that it was not |
| **Platform backend** | Reported by the gateway. Unknown when the gateway is silent, because *"the shell has no second way to ask"* |
| **First-run setup** | The setup headline and summary. Also carries the "this gateway is too old to say" case rather than treating it as a failure |

### Actions

Every button is either pressable or carries a sentence saying why it is not.
The primary action for the current phase leads.

| Action | Offered when | Refusal wording, when it is not |
|---|---|---|
| **Start platform** | The shell has a way to start it and nothing is answering | *"Something is already answering at `<address>`. Starting another platform on the same address would fail on the port, so this is not offered"* |
| **Set up now** / **Run again** | The gateway offers a setup endpoint | Names the reason: not yet asked, unreachable, the gateway is too old to have a setup endpoint, or the answer was unreadable |
| **Open console** | The gateway is reachable | *"Nothing is answering at `<address>`, so the console has nothing to load. Opening it would show a blank window, which would tell you nothing"* |
| **Stop platform** | This shell started it, or a service it can control is running | *"This shell did not start what is answering at this address and no Windows service accounts for it, so it has no way to stop it"* |
| **Settings** | Always | — |
| **Open logs** | Always | Opens `%ProgramData%\Project CHAOS\logs` in Explorer |
| **Restart as administrator** | Elevation is required, or a previous attempt was refused | *"…The platform is not touched by this: whatever is running keeps running"* |
| **Check again** | Always | Repeats every check now instead of waiting for the next poll |

**Stop platform** always carries the consequence in capitals: *THE HOMESTEAD
WILL STOP BEING CONTROLLED and alarms will stop being evaluated until it is
started again.*

### It comes back on its own — but not rudely

The launcher is not only a startup screen. If the console loses the platform it
returns, but only after **three** consecutive failed polls, no sooner than **two
minutes** since it was last shown, and never if you dismissed it deliberately.
Shoving it in front of a working console on one dropped poll would be its own
fault, and an operator watching a known outage does not need the window pushed
at them every two minutes.

---

## 3. The console window

Title: *Project CHAOS — Operator Console*. The operator console rendered in
WebView2, plus a thin command strip.

| Button | Does |
|---|---|
| **Reload** | Reloads the console |
| **Annunciator** | Opens the annunciator window |
| **Open in browser** | Opens the same console URL in the default browser |
| **Start screen** | Brings the launcher back |
| **Settings** | Opens the settings window |

The strip is deliberately thin: the console inside the WebView2 is the operator
surface, and a second navigation chrome competing with it would be noise. To the
right of the buttons sit the link state and the gateway address.

Below the strip is the permanent run-mode strip — above the console rather than
in a status bar nobody reads.

### It never shows you a blank frame

The WebView2 stays collapsed until the gateway answers. Until then the same cell
holds a titled panel: a heading, a plain-English summary, a determinate progress
bar, a progress line that always names the address and the attempt number, the
facts, and the next steps.

*"Starting…"* with no detail is what makes a hung launch indistinguishable from
a broken one, so it is never shown.

If the startup budget is spent, the panel becomes a diagnostic — see
[Troubleshooting § The shell shows a diagnostic](./troubleshooting.md#1-the-shell-shows-a-diagnostic-panel-instead-of-the-console).

### Closing it

Closing the console window hides it to the tray. The first time, a balloon says
so:

> **Project CHAOS is still running** — The window closed; the platform did not.
> Control, alarm evaluation and logging continue in the Windows service. Use the
> tray icon to reopen the console, or Exit to close this shell (which still does
> not stop the platform).

---

## 4. The tray icon

The tray icon is drawn at runtime from the live alarm state. Its whole purpose
is that **"we cannot vouch for this" is never drawn like "nothing is wrong"**.

| Icon state | Meaning |
|---|---|
| Starting | Shell is up, gateway not yet reached. Alarm state unknown |
| Unreachable | Contact lost. Alarm state unknown — **not** "no alarms" |
| Stale | The last answer is older than the freshness budget. Shown as last-known |
| Normal | Current answer: nothing active |
| Warning | Current answer: worst active severity is warning |
| Alarm | Current answer: worst active severity is major or critical |
| Emergency | Current answer: at least one emergency alarm is active |

The numeric overlay is a count of active alarms, or a **question mark** when the
shell cannot see the platform. It is never substituted with `0`.

### The tray menu

```text
Start screen…
───────────────
Open console
Open annunciator
Open in browser
───────────────
Settings…
───────────────
Exit shell …            (wording depends on the run mode — see section 1)
```

**There is no "acknowledge" item.** Acknowledgement is an audited action taken
by a named operator on the annunciator panel; the shell opens the panel and
never acknowledges for you.

---

## 5. The annunciator window

Title: *Project CHAOS — Annunciator*. A second window holding the annunciator
panel, sized and placed for a wall display.

- It opens on the display you nominate in Settings — by index, by device name,
  or the primary display.
- Full-screen is a setting, for a permanently mounted panel. While it is
  full-screen the display is kept awake.
- Until the panel has data, the window shows `ANNUNCIATOR / Loading panel…` on
  the panel's own black background. The annunciator is the screen an operator
  looks at to decide whether anything is wrong, so it must never present as
  "nothing wrong" before it has data.

Everything the panel *does* is described in
[The annunciator panel](./annunciator.md).

---

## 6. Settings

Title: *Project CHAOS — Settings*. Everything an operator needs to make the
product work is here, so that nothing about running it requires a config file, a
terminal or the registry.

The file being edited is named at the top of the window. It is
`%APPDATA%\ProjectCHAOS\Shell\settings.json`, and it is a different file from
the window layout on purpose: layout is disposable cosmetic state rewritten on
every window move; settings are deliberate operator choices that must not be
lost because a layout write raced with a crash.

### Gateway

| Field | Notes |
|---|---|
| **Address** | Host name or IP. No scheme, no port. A bare IPv6 literal is accepted and bracketed for you — an operator copying an address off a router page will not add the brackets |
| **Port** | 1–65535. Default 8080 |
| **Use HTTPS** | Off by default |

The address is stored decomposed rather than as one URL string, because that is
the shape the page presents and because validating three small fields gives you
a message naming the field you got wrong instead of *"that URL is invalid"*.

### Starting and stopping

| Field | Default | Notes |
|---|---|---|
| **Start the platform when this shell opens** | On | If nothing is answering, the shell starts the platform itself instead of waiting for you. This is what makes launching the executable the whole procedure |
| **Open this shell when I sign in to Windows** | Off | Opens the window, not the platform. A platform installed as a Windows service runs whether or not anyone signs in |
| **Prefer the Windows service when it is installed** | On | A service keeps the homestead controlled when nobody is logged in |
| **Windows service name** | `ChaosPlatform` | What the shell looks for and controls. **The gateway currently registers itself under `ChaosHost`**, so if the service check reads *not installed* on a machine where the service is plainly running, this is the field to correct |
| **`Chaos.Host.exe` location** | empty | Leave empty to let the shell find it. When found, the page says where |

### Where the data lives

| Field | Notes |
|---|---|
| **Database location** | A file path or a full connection URL. Empty leaves the platform's own default alone |
| **Data directory** | The design package the registry is built from. Empty leaves the platform's default alone |

**These only apply to a platform this shell starts itself.** A Windows service
reads its own configuration, installed once by the installer, and this shell does
not rewrite it. The page says so, on the page, at the time — the difference
between an operator who moves their database and one who *believes* they moved
their database.

### Appearance and the annunciator

| Field | Notes |
|---|---|
| **Theme** | System, Dark or Light. System follows the Windows app theme |
| **Open the annunciator on** | A display: by index, by device name, or the primary |
| **Open the annunciator full-screen** | For a wall panel. The display is kept awake while it is full-screen |

### Saving

**Save**, **Cancel**, **Reset to defaults**.

Saving tells you what it did, in one of four forms:

| Headline | When |
|---|---|
| *Saving this needs the platform restarted before it takes effect.* | e.g. you moved the address of a platform this shell started |
| *Saved. The shell will reconnect at the new address.* | The gateway address changed |
| *Saved.* | Something changed that needs neither |
| *Nothing changed.* | Nothing changed |

Alongside those come **notes** (what will happen) and **warnings** (what will
*not* happen, and why). The warnings matter more: a setting that looks saved but
is ignored is worse than one that was refused.

---

## 7. Where the shell keeps things

| What | Path |
|---|---|
| Settings | `%APPDATA%\ProjectCHAOS\Shell\settings.json` |
| Window layout | `%APPDATA%\ProjectCHAOS\Shell\layout.json` |
| Logs (shell and platform) | `%ProgramData%\Project CHAOS\logs` |

**Open logs** on the launcher and **Open logs folder** on the diagnostic panel
open that directory in Explorer. You are never asked to type a path.

Window placement is restored across restarts and validated against the displays
actually present, so a window saved on a monitor that has since been unplugged
comes back somewhere you can see it.

---

## 8. Running the shell against another node

Three ways, in order of preference:

1. **Settings → Gateway.** Change the address and port. Persistent.
2. **`--host <address>`** on the shell's command line. One launch only; the
   stored address is untouched.
3. **An environment variable.** Same effect, same scope.

Options 2 and 3 exist for shortcuts and scripted deployments. Option 1 is the
one to use.

A second launch of the shell is redirected to the running instance rather than
opening a second window.

---

## 9. Related reading

| Document | Why |
|---|---|
| [Getting started](./getting-started.md) | First launch, first-run setup, what you should see |
| [The operator console](./operator-console.md) | What is inside the console window |
| [The annunciator panel](./annunciator.md) | What the annunciator window shows and how it behaves |
| [Troubleshooting](./troubleshooting.md) | When the shell shows a diagnostic instead of the console |
| [Building in Visual Studio 2026](./visual-studio.md) | Building and debugging the shell |
