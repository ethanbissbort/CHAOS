# Building in Visual Studio 2026

Everything in this document is done inside Visual Studio: Solution Explorer,
Test Explorer, the Build menu and F5. There is no step here that needs a
terminal.

Command-line build scripts do exist — they are how the MSI is produced and how
CI runs — and they are in [Windows packaging](./advanced-windows-packaging.md).
You do not need them to build, run or test the solution.

Related: [Getting started](./getting-started.md) ·
[The desktop shell](./desktop-shell.md) · [Architecture](./architecture.md)

---

## 1. What Visual Studio needs

| Requirement | Why |
|---|---|
| **.NET SDK 10.0.100** or a later 10.0.1xx feature band | `windows/global.json` pins it: `"version": "10.0.100"`, `"rollForward": "latestFeature"`, `"allowPrerelease": false`. A control system ships the configuration that was tested, not the newest one |
| **Windows App SDK / WinUI 3 tooling** | `Chaos.Shell` is WinUI 3 against Windows App SDK 2.3.1. Without the tooling the project loads and restores but will not build |
| **Microsoft Edge WebView2 Evergreen Runtime** | Needed to *run* the shell, not to build it |

The solution targets `net10.0` everywhere except the shell, which targets
`net10.0-windows10.0.26100.0` with a minimum of Windows 10 build 17763.

**.NET 10 is deliberate.** It is the current LTS. A system that controls
off-grid power, water and freeze protection tracks LTS rather than the newest
preview.

---

## 2. Open the solution

Open **`windows/CHAOS.sln`**.

Solution Explorer shows three solution folders:

```text
CHAOS
├── src
│   ├── Chaos.Api                 subsystems ported from Python to .NET
│   ├── Chaos.Host                the gateway — LAN listener, proxy, first-run setup
│   ├── Chaos.Host.Supervisor     supervises the Python backend as a child process
│   ├── Chaos.Runtime             builds the things that are not .NET — see section 6
│   ├── Chaos.Shell               the WinUI 3 desktop shell (Windows only)
│   └── Chaos.Shell.Core          the shell's testable half — no Windows dependency
├── build                         solution items: the build props, global.json,
│                                 the PowerShell scripts and the two .targets files
└── tests
    ├── Chaos.Api.ConformanceHost a minimal host that composes Chaos.Api and nothing else
    ├── Chaos.Api.Tests
    ├── Chaos.Host.Supervisor.Tests
    ├── Chaos.Host.Tests
    └── Chaos.Shell.Core.Tests
```

The Python platform backend is **not** in this solution. It lives in `src/chaos/`
at the repository root and is a separate build. Section 7 covers it.

### Which project does what

| Project | Role |
|---|---|
| `Chaos.Host` | The front door. The only LAN listener, a reverse proxy to the Python backend on loopback, the host of the operator console, and where ported subsystems land |
| `Chaos.Host.Supervisor` | Resolves which Python runs the platform and supervises it as a child process, with a restart policy |
| `Chaos.Api` | Where `/api/v1` routes ported to .NET live. Built and tested; **nothing in it is live yet** — see section 8 |
| `Chaos.Shell` | The WinUI 3 windows: launcher, console, settings, annunciator, tray icon |
| `Chaos.Shell.Core` | Every decision the shell makes — launcher state machine, settings validation, tray state, diagnostics — with no Windows dependency, so it can be tested anywhere |
| `Chaos.Runtime` | Compiles no code. It builds the two parts of the product that are not .NET: the embedded Python interpreter and this documentation site. Section 6 |

The split between `Chaos.Shell` and `Chaos.Shell.Core` is the reason the startup
experience is testable at all: every awkward combination (a service running while
the gateway is silent, a gateway answering while its backend is dead, a host too
old to report setup) is exercised as a unit test without Windows, a service
control manager or a network.

---

## 3. Build the solution

**Build → Build Solution** (`Ctrl+Shift+B`).

### Configuration and platform

The solution defines `Debug` and `Release` against `Any CPU`, `x64`, `x86` and
`ARM64`.

**WinUI 3 has no Any CPU.** `Chaos.Shell` declares `x86;x64;ARM64` explicitly,
and the solution maps its `Any CPU` rows onto `x86`. Everything else builds as
`Any CPU` regardless of which solution platform is selected.

### Warnings are errors

`windows/Directory.Build.props` sets `TreatWarningsAsErrors` for every project
(with `NU1900` excluded). `Chaos.Host` additionally sets
`GenerateDocumentationFile`, so **an undocumented public member is a build
error** — that assembly's public surface is a contract other work codes against.

### If you build on a non-Windows machine

The shell degrades cleanly instead of failing. On a non-Windows host,
`Chaos.Shell` turns itself into an empty library: the XAML compiler is off, no
sources compile, restore still runs so the package graph is verified, and the
build log carries:

```text
Chaos.Shell (WinUI 3) was NOT COMPILED: it requires Windows. Restore ran, so
package resolution is still verified. Build it in Visual Studio 2026.
```

That is deliberate. A repository-wide build stays green and says plainly that
the project was not built, rather than emitting an artefact that could be
mistaken for one.

---

## 4. Run it with F5

### The normal case: run the whole product

Set **`Chaos.Shell`** as the startup project (right-click → *Set as Startup
Project*) and press **F5**.

The launcher window opens. From there everything is buttons: it finds or starts
the gateway, first-run setup runs, and **Open console** loads the console. See
[Getting started § First launch](./getting-started.md#3-first-launch) for what
each screen should say.

Because the shell is unpackaged (`WindowsPackageType=None`), the Windows App SDK
injects its bootstrapper as a module initialiser rather than requiring a call
from `Main` — which is what makes the custom entry point in `ShellEntryPoint`
safe. That entry point exists so a second launch can be redirected to the
already-running instance before any XAML is created.

### Running only the gateway

Set **`Chaos.Host`** as the startup project and pick a **launch profile** from
the drop-down next to the run button.

| Profile | Gives you |
|---|---|
| **CHAOS Gateway (8080)** | The gateway and the operator console. The documentation site is off |
| **CHAOS Gateway + Documentation (8080 + 8090)** | The same, plus this help site on port 8090, and the browser opens on it |
| **CHAOS Gateway + Documentation (this PC only)** | Both, bound to loopback rather than every interface |
| **CHAOS Gateway, no first-run setup (8080)** | The gateway with automatic setup turned off, for working against a database you are managing by hand |

Each profile sets the addresses as environment variables; the defaults, if you
run with none, come from `windows/src/Chaos.Host/appsettings.json`:

| Setting | Default | Meaning |
|---|---|---|
| `Chaos:ListenUrl` | `http://0.0.0.0:8080` | Where the gateway listens |
| `Chaos:BackendUrl` | `http://127.0.0.1:8081` | Where it expects the Python backend |
| `Chaos:AutoSetup` | `true` | Run first-run setup at startup |
| `Chaos:Docs:Enabled` | `true` | Serve the offline documentation site |
| `Chaos:Docs:ListenUrl` | `http://0.0.0.0:8090` | Where the documentation site listens |
| `Chaos:WebRootPath` | empty | Let the resolver find the console assets |

Anything can be overridden per-machine with a `CHAOS_`-prefixed environment
variable in the project's debug properties — `CHAOS_BackendUrl`,
`CHAOS_AutoSetup`, `CHAOS_Docs__Enabled`. Note the casing: the gateway binds
`CHAOS_<PascalCase>`, and the Python platform binds
`CHAOS_<SCREAMING_SNAKE_CASE>`. They share a prefix and are kept apart by the
underscores, which is why no gateway setting may ever be named with an
underscore in it.

Browse to `http://localhost:8080/` and the operator console loads. The gateway
finds its assets by probing, in order: a configured `Chaos:WebRootPath`, then
`<content root>/web`, then `<executable directory>/web`, then `src/chaos/web`
found by walking up from the content root — which is the case that makes F5 from
a checkout work without configuration. `/host/info` reports which one it used
and every path it tried.

### What works under F5 without the Python backend

| Works | Does not |
|---|---|
| The launcher and every check it renders | Any live data — assets, alarms, energy, telemetry |
| The console shell, chrome and navigation | Any screen's content |
| `/health`, `/health/live`, `/host/info`, `/host/routes` | `/api/v1/**` (there is nothing to proxy to) |
| Route-ownership validation at startup | |
| First-run setup, if a Python is resolvable — see section 7 | |

`/health` answers 503 with `backend: "down"`, the launcher headline reads *"The
platform is only half up"*, and the console loads with nothing in it. That is
correct behaviour, not a fault: a gateway that returned 200 while the platform
behind it was dead would tell every status-code-only monitor that an off-grid
site with no alarm engine is fine.

### What happens when you press F5

The gateway starts the Python backend itself. `Chaos.Host` registers
`Chaos.Host.Supervisor`, which resolves an interpreter — the embedded runtime
if one is present, otherwise the Python on your `PATH` with the repository's
`src` on `PYTHONPATH` — launches the platform on loopback, waits for its
`/health` to answer, and restarts it with backoff if it dies. `/host/info`
reports what it found:

```json
"supervisor": { "registered": true, "state": "Running",
  "runtime": "development", "restarts": 0 }
```

If you would rather run the backend yourself, under its own debugger, set
`CHAOS_SuperviseBackend=false` in the launch profile. The gateway then keeps
proxying to `Chaos:BackendUrl` and reports the backend from probing alone; it
simply does not start or stop it.

To drive an installed platform on another machine instead, point the shell at
it: **Settings** → *Gateway* → address and port. Your F5 build is then not
involved at all.

---

## 5. Tests in Test Explorer

**Test → Test Explorer**, then **Run All** (`Ctrl+R, A`).

All five test projects use xUnit with `xunit.runner.visualstudio`, so discovery
works with no configuration. They target `net10.0` and none of them needs
Windows — including the shell's tests, because everything the shell decides
lives in `Chaos.Shell.Core`.

| Test project | Covers |
|---|---|
| `Chaos.Host.Tests` | Host endpoints, route-ownership table and validator, setup assessor, setup endpoints, startup validation |
| `Chaos.Host.Supervisor.Tests` | Python runtime resolution, restart policy |
| `Chaos.Api.Tests` | Annunciator panel builder, legends, serviceability, read store, timestamps, query parsing |
| `Chaos.Shell.Core.Tests` | Launcher state machine, platform lifecycle, probe client, settings, tray state, window placement, setup contract |
| `Chaos.Api.ConformanceHost` | Not a test project — a minimal host that composes `Chaos.Api` alone, so the .NET port can be exercised against a real HTTP pipeline |

### Debugging a test

Right-click a test in Test Explorer → **Debug**. Breakpoints in
`Chaos.Shell.Core` work exactly as they do anywhere else, which is the practical
payoff of keeping the shell's judgement out of XAML.

### Two behaviours worth knowing before you change routing

- The host **refuses to start** if the route-ownership manifest claims a prefix
  for .NET and no .NET endpoint is registered under it. A route marked as ported
  with nothing behind it answers 404, and a 404 from an alarm endpoint reads
  like *"no alarms"*. `Chaos.Host.Tests` pins that behaviour.
- A manifest row for a prefix with no endpoint is a startup failure; an endpoint
  with no manifest row is harmless.

---

## 6. Chaos.Runtime: the parts that are not .NET

Two things ship with this product that no C# compiler produces: the **embedded
Python interpreter** that goes inside the installer, and **this documentation
site**. Both used to be producible only by typing PowerShell. Both are now
produced by **building a project**.

Build the solution, or right-click **Chaos.Runtime** in Solution Explorer and
choose **Build**, and they appear. No terminal, and no separate procedure to
remember.

### Why it is a project and not a target bolted onto the gateway

- **It is visible.** It has a node in Solution Explorer with Build, Rebuild and
  Clean on its context menu, and it can be unchecked in **Build →
  Configuration Manager**. A target hidden inside a `.csproj` is discoverable
  only by reading the `.csproj`.
- **It is separable.** Building the gateway does not build a Python interpreter.
  If the runtime build fails — no internet, say — `Chaos.Host` still builds and
  still runs, because **nothing references this project**.

It compiles no code. The empty assembly in `bin\` is a side effect of using the
standard SDK, which is worth it because the project then loads, builds and
behaves in Visual Studio exactly like every other project here.

### What each command does

| Command | Does |
|---|---|
| **Build** | Builds the embedded Python runtime **if it is missing or out of date** (2–5 minutes and a ~55 MB download, once), verifies it if it has not been verified since it was built, and regenerates the documentation site if any Markdown file has changed (seconds). Otherwise it does nothing at all, and says so |
| **Rebuild** | Forces both from scratch: deletes the stamps, rebuilds the interpreter and re-runs the full verification. This is the "force a rebuild" button, and it is a normal Visual Studio command |
| **Clean** | **Leaves the runtime and the documentation alone.** Clean Solution is a reflex; it must not cost a 55 MB download |

### Turning either half off

Both switches are commented into `Chaos.Runtime.csproj` where you will find
them:

| Property | Effect |
|---|---|
| `ChaosBuildPythonRuntime` | `false` skips the interpreter — for working on the .NET side on a machine with no internet. Unchecking `Chaos.Runtime` in Configuration Manager does the same thing without editing anything |
| `ChaosBuildDocs` | `false` skips the documentation site |
| `ChaosDocsGenerationFailsBuild` | `true` makes a documentation problem a hard error. Leave it off for day-to-day work; turn it on for a release |

### Why a broken document does not break your build

The documentation generator is also a **quality gate on the documentation**: it
refuses to write a site with a broken cross-reference. But prose is edited by
people who are not editing the control plane, so a dead link in a manual would
otherwise stop the gateway building and stop an F5 dead — a build break with no
connection to the change being made.

So by default a generation failure is **reported loudly and lets the build
finish**. Its output is printed in full at high importance, so it is visible in
the Output window at the default verbosity, and the previously generated site
keeps being served in the meantime.

### Reading the manual from the product

When the documentation site is enabled, the gateway serves it on **port 8090**
alongside the operator console on 8080. That is the copy to read when the
platform is misbehaving — it is entirely offline, with no external reference of
any kind.

The generator produces two shapes from one parse: a directory of pages, and a
**single self-contained HTML file** with every page, the stylesheet and the
search index in it. The single file is the copy someone opens when the platform
is down and they need the troubleshooting page.

Its styling is taken from the operator console's own design tokens, so the help
site and the console are the same product to look at.

---

## 7. The Python half

The platform backend is a separate Python project at the repository root. It is
not part of `CHAOS.sln`.

### Its tests

**2128 tests**, all against SQLite and an in-memory message bus — no PostgreSQL,
no broker, no hardware. That is deliberate: it is step 1 of the twelve-step
commissioning sequence ("bench test"), and it means the whole platform is
exercisable end to end from a laptop.

`pyproject.toml` already carries what any pytest runner needs:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

**These tests are not in Test Explorer today.** There is no Python project file
in the repository, so nothing here configures Visual Studio to discover them.
Visual Studio's Python workload can run pytest against a folder once a Python
environment and test framework are selected for the workspace, but that is a
per-machine setup step this repository does not perform for you. If you want the
numbers without setting that up, CI runs the same suite on every push.

### Which Python the gateway uses

The gateway's runtime resolver decides, in this order, and reports what it chose
in `/host/setup` and `/host/info`:

1. **Embedded** — `<install root>\python\python.exe`. This is the packaged
   product.
2. **Broken embedded** — the `python\` directory exists but holds no
   interpreter. **Hard failure.** It will not fall back to a system Python,
   because that turns "the installer is corrupt" into "it works on my machine
   and mysteriously not on the customer's".
3. **Development** — no `python\` directory at all. It walks up to eight levels
   looking for a checkout containing `src/chaos/cli.py`, then uses a configured
   interpreter or `python3`/`python` from `PATH`, with `PYTHONPATH` set to the
   checkout's `src`. This is the F5-from-a-checkout case, and it is why
   first-run setup can work under the debugger.

---

## 8. What is not ported yet

Every `/api/v1` prefix is served by the Python backend today. `Chaos.Api` exists,
is built, and is covered by tests, but nothing in it is wired into the gateway:
`Chaos.Host` does not reference it, and no manifest row names it.

Porting a subsystem is intended to be one row in the route-ownership manifest —
compiled into `RouteOwnershipDefaults`, or merged over it from the node's own
configuration:

| Field | Meaning |
|---|---|
| `PathPrefix` | The prefix being claimed |
| `Owner` | `Python` or `Dotnet` |
| `PortedInVersion` | The gateway version that took it over |
| `Notes` | Why, in a sentence |

Matching is longest-prefix-wins at segment boundaries, so a row for
`/api/v1/alarms/definitions` takes that subtree while `/api/v1/alarms/active`
keeps going to Python. `GET /host/routes` on a running host is the authoritative
live view of who serves what — prefer it over reading any file.

The `/api/v1` row itself is a deliberate catch-all: an endpoint added to the
Python app that nobody remembered to list still proxies correctly instead of
404-ing. Ownership never moves to .NET implicitly — only an explicit row can do
that, and only if an endpoint backs it.

---

## 9. Related reading

| Document | Why |
|---|---|
| [Getting started](./getting-started.md) | What the launcher and setup screens should show |
| [The desktop shell](./desktop-shell.md) | Every window the shell opens and what it decides |
| [Architecture](./architecture.md) | Why the gateway, backend and shell are split this way |
| [API reference](./api.md) | The gateway's own routes and the platform's 83 endpoints |
| [Windows packaging](./advanced-windows-packaging.md) | The embedded runtime, the build scripts and the MSI |
