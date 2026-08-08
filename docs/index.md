# Project CHAOS documentation

**C**entral **H**omestead **A**utomation and **O**peration **S**ystem — a
local-first control platform for an off-grid homestead.

CHAOS runs the property as a small industrial site rather than a collection of
consumer smart-home gadgets: an authoritative asset registry, a telemetry
pipeline, an energy-management state machine, an alarm engine with a
nuclear-control-room-style annunciator, and an audited supervisory command path
that local controllers are always free to refuse.

You run it from two places, and neither of them is a terminal:

- the **desktop shell** (`Chaos.Shell.exe`) — launcher, operator console, tray
  icon and annunciator panel on Windows;
- the **browser console** — the same operator console served to any browser on
  the LAN by the gateway.

You build, run and test it in **Visual Studio 2026**, from
`windows/CHAOS.sln`.

---

## Start here

Read these four in order. They take about half an hour and cover everything you
need to install the product, get it running, and find your way around it.

| # | Document | What it gives you |
|---:|---|---|
| 1 | [Getting started](./getting-started.md) | Launch the shell, let first-run setup build the database, open the console |
| 2 | [Building in Visual Studio 2026](./visual-studio.md) | Open the solution, pick the startup project, press F5, run the tests in Test Explorer |
| 3 | [The desktop shell](./desktop-shell.md) | The launcher screen, the console window, the tray icon, native settings |
| 4 | [The operator console](./operator-console.md) | The eight screens, operator identity, refresh and wall-display mode |

---

## How do I…

A task-oriented index. Every entry lands on the section that answers it.

### Get running

| I want to… | Go to |
|---|---|
| Install and start CHAOS for the first time | [Getting started § First launch](./getting-started.md#3-first-launch) |
| Understand what first-run setup did | [Getting started § What first-run setup does](./getting-started.md#4-what-first-run-setup-does) |
| Build the solution | [Visual Studio § Build the solution](./visual-studio.md#3-build-the-solution) |
| Run the platform under the debugger | [Visual Studio § Run it with F5](./visual-studio.md#4-run-it-with-f5) |
| Run the tests | [Visual Studio § Test Explorer](./visual-studio.md#5-tests-in-test-explorer) |
| Change the gateway address, port, database or theme | [The desktop shell § Settings](./desktop-shell.md#6-settings) |
| Open the console on another machine on the LAN | [The operator console § Reaching the console](./operator-console.md#1-reaching-the-console) |

### Operate

| I want to… | Go to |
|---|---|
| See the whole property at a glance | [The operator console § Home](./operator-console.md#41-home) |
| Acknowledge an alarm from a panel, not a list | [The annunciator panel](./annunciator.md) |
| Understand why an alarm never fired | [Alarms § Serviceability](./alarms.md#6-serviceability-a-dark-tile-is-a-claim) |
| Work out what a failure would take with it | [Topology and blast radius](./topology-view.md) |
| See what is in the rack | [Rack elevation](./rack-view.md) |
| Issue a command to equipment | [Control § Issuing a command](./control.md#3-issuing-a-command) |
| Find out why a command was refused | [Control § The eight interlocks](./control.md#4-the-eight-interlocks) |
| Put a subsystem into maintenance | [Control § Operating modes](./control.md#5-operating-modes) |
| Handle an alarm flood | [Operations § Alarm floods](./operations.md#3-alarm-floods) |
| Recover from a total power loss | [Operations § Black start](./operations.md#1-black-start) |
| Take and verify a backup | [Operations § Backup and restore](./operations.md#4-backup-and-restore) |
| Put a subsystem under automatic control | [Commissioning](./commissioning.md) |

### Fix

| I want to… | Go to |
|---|---|
| The shell shows a diagnostic instead of the console | [Troubleshooting § The shell shows a diagnostic](./troubleshooting.md#1-the-shell-shows-a-diagnostic-panel-instead-of-the-console) |
| The annunciator is dark | [Troubleshooting § The annunciator is dark](./troubleshooting.md#2-the-annunciator-is-dark) |
| An alarm never fires | [Troubleshooting § An alarm never fires](./troubleshooting.md#3-an-alarm-never-fires) |
| Setup says `needs_attention` | [Troubleshooting § Setup says needs_attention](./troubleshooting.md#4-setup-says-needs_attention) |
| The platform will not start | [Troubleshooting § The platform will not start](./troubleshooting.md#5-the-platform-will-not-start) |
| The console is empty or shows stale numbers | [Troubleshooting § The console has no data](./troubleshooting.md#6-the-console-loads-but-has-no-data) |

### Understand

| I want to… | Go to |
|---|---|
| Know how the pieces fit together | [Architecture](./architecture.md) |
| Understand what `data/` is and why it is authoritative | [The design package](./design-package.md) |
| Know what "not ratified" means on a document | [The design package § Ratification](./design-package.md#4-ratification-and-what-not-ratified-means) |
| Read the known gaps found by running the whole thing | [Integration findings](./integration-findings.md) |
| Look up an endpoint | [API reference](./api.md) |
| Know what survives losing the power container | [Secondary control node](./secondary-control-node.md) |
| See the network zones and trust boundaries | [Network and trust boundaries](./network-and-trust-boundaries.md) |
| Read the water control narrative | [Water-system control narrative](./water-control-narrative.md) |

---

## The full set

### Using CHAOS

| Document | Covers |
|---|---|
| [Getting started](./getting-started.md) | Install, first launch, first-run setup, what you should see |
| [Building in Visual Studio 2026](./visual-studio.md) | Solution layout, startup project, F5, Test Explorer, the Python half |
| [The desktop shell](./desktop-shell.md) | Launcher, console window, tray, settings, annunciator window, run modes |
| [The operator console](./operator-console.md) | Chrome, identity, the eight screens, wall-display mode |
| [The annunciator panel](./annunciator.md) | ISA-18.1 sequence, tiles, bays, horn, lamp test, out-of-service |
| [Topology and blast radius](./topology-view.md) | The dependency canvas and `impact` analysis |
| [Rack elevation](./rack-view.md) | The 42U elevation, findings, and why it is not as-built |

### How the platform works

| Document | Covers |
|---|---|
| [Architecture](./architecture.md) | The gateway, the backend, the shell, and the mapping onto the design document |
| [Alarms, incidents and notification](./alarms.md) | The alarm model, lifecycle, correlation, serviceability, notification |
| [Control, interlocks and operating modes](./control.md) | The command path, the eight interlocks, modes, the two safety gates |
| [The design package](./design-package.md) | `data/`, schemas, ratification status, design decisions, open conflicts |
| [Integration findings](./integration-findings.md) | Nine findings from running the platform end to end. Operational knowledge |

### Operating the property

| Document | Covers |
|---|---|
| [Operations runbooks](./operations.md) | Daily checks, black start, comms loss, alarm floods, backup, retention |
| [Troubleshooting](./troubleshooting.md) | Organised by symptom, GUI remedy first |
| [Commissioning](./commissioning.md) | The twelve-step sequence the platform enforces before any physical control |
| [Secondary control node](./secondary-control-node.md) | What survives loss of the power container, and why it must not take over |
| [Network and trust boundaries](./network-and-trust-boundaries.md) | Zones, trust boundaries, firewall flows, service identities |
| [Water-system control narrative](./water-control-narrative.md) | The water system's control design, in full |

### Reference

| Document | Covers |
|---|---|
| [API reference](./api.md) | Every endpoint, roles, error semantics, the gateway's own routes |
| [Design decision records](./design-decisions/README.md) | The open decisions, made decidable. All still *proposed* |

### Advanced — you do not need these

These describe the command line, containers and the packaging pipeline. They
exist for a **headless secondary node**, an **unattended install**, a **remote
session over a slow link**, and for **building the installer**. Nothing in the
sections above depends on them.

| Document | Covers |
|---|---|
| [Command line](./advanced-command-line.md) | The `chaos` CLI: every subcommand, exit codes, scripting |
| [Container deployment](./advanced-container-deployment.md) | Docker Compose for the primary and secondary nodes, the image, upgrades |
| [Windows packaging](./advanced-windows-packaging.md) | The embedded Python runtime, the build scripts, the MSI |

---

## Reading this offline

This documentation is also **part of the product**. Building the solution
regenerates it into an offline help site, which the gateway serves alongside the
operator console — and which also opens straight from disk, with no network of
any kind involved.

Two shapes come out of one pass: a directory of pages, and a **single
self-contained HTML file** with every page, the stylesheet and the search index
inlined. The single file is the copy to open when the platform is down and you
need [Troubleshooting](./troubleshooting.md).

See [Building in Visual Studio § Chaos.Runtime](./visual-studio.md#6-chaosruntime-the-parts-that-are-not-net).

---

## Conventions used throughout

**"The platform requests; local controllers decide."** Everything here sits at
Level 3 of the control hierarchy. The API can ask a pump to start; the PLC, the
float switch and the breaker keep the authority to refuse. This software is not
the protection system, and no document here should be read as if it were.

**Nothing is invented.** Where the design package records `TBD`, the
documentation says *open item* rather than a plausible guess. Where a subsystem
is planned but not built, it is named as not built.

**Two version numbers.** The Python platform is **0.4.0**; the .NET gateway and
desktop shell are **0.5.0**. The gateway is contracted against platform 0.4.0
and reports both on `/host/info`.

**Two `CHAOS_` namespaces.** The gateway binds `CHAOS_<PascalCase>`
(`CHAOS_AutoSetup`, `CHAOS_BackendUrl`). The Python platform binds
`CHAOS_<SCREAMING_SNAKE_CASE>` (`CHAOS_DATABASE_URL`, `CHAOS_DATA_DIR`). The
underscores are what keep them apart.

**The headline, stated once and honestly.** No part of this platform has been
connected to real plant. Every control path has been exercised against the
simulator and an in-memory message bus. That is step 1 of the twelve-step
commissioning sequence and nothing beyond it. See
[Commissioning](./commissioning.md) and
[Architecture § Status](./architecture.md#8-status-summary).
