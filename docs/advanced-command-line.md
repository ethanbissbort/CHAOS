# Command line

> **Advanced. You do not need this.**
>
> Everything in this document has a GUI path in
> [The desktop shell](./desktop-shell.md) and
> [The operator console](./operator-console.md), except where it says
> otherwise. The command line exists for four situations:
>
> - a **headless secondary node** with no display;
> - an **unattended install** or a scheduled task;
> - a **remote session** over a slow or text-only link;
> - a **partially deployed node** where the interface will not come up at all.
>
> If you are sitting in front of a working install, close this and use the
> shell.

Related: [Container deployment](./advanced-container-deployment.md) ·
[Windows packaging](./advanced-windows-packaging.md) ·
[Operations](./operations.md)

---

## 1. What it is

`chaos` is the platform's own operator command line. Its design rules explain
why it exists at all:

- **Subsystem imports are lazy.** `--help`, `init-db`, `status`, `backup` and
  `export` keep working even when the registry loader, energy manager, alarm
  engine, ingest or simulator packages are absent or broken. A missing subsystem
  is reported with an actionable message and a dedicated exit code, never a
  traceback.
- **Every failure exits non-zero.**
- **Nothing invents state.** Counts, energy state and alarms are read from the
  database; where a table does not exist yet the CLI says so rather than
  printing a reassuring zero.

That is what makes it usable on a node where nothing else is.

### Where it lives

| Install | Path |
|---|---|
| The packaged Windows product | `<install root>\python\Scripts\chaos.exe` |
| A development checkout | `chaos` on the path after an editable install, or `python -m chaos.cli` |

### Global options

| Option | Effect |
|---|---|
| `--database-url` | Override the configured database for this invocation |
| `--data-dir` | Override the design-package directory (default `data/`) |
| `--log-level` | `CRITICAL`, `ERROR`, `WARNING`, `INFO` (default), `DEBUG` |
| `--version` | Print the platform version |

Every setting is also overridable through `CHAOS_*` environment variables. Note
the casing: the **platform** binds `CHAOS_<SCREAMING_SNAKE_CASE>`
(`CHAOS_DATABASE_URL`, `CHAOS_DATA_DIR`); the **gateway** binds
`CHAOS_<PascalCase>`. They share a prefix and are kept apart by the underscores.

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success |
| 1 | Runtime failure |
| 2 | Usage error |
| 3 | Subsystem unavailable on this node |

Exit code 3 is the one that matters for scripting: it distinguishes "this node
does not have that subsystem installed" from "that subsystem failed".

---

## 2. The subcommands

| Command | Purpose | GUI equivalent |
|---|---|---|
| `init-db` | Create every platform table | First-run setup |
| `load-registry` | Load the design package into the registry | First-run setup |
| `load-all` | Registry + load schedule + alarm definitions | First-run setup |
| `validate` | Validate the design package against its schemas | Runs in CI |
| `serve` | Run the platform API | The gateway starts it (see section 8) |
| `simulate` | Delegate to the simulator | None |
| `retention` | Apply the data-retention policy | None — scheduled task |
| `backup` | Write a portable registry and configuration archive | None — scheduled task |
| `status` | Node role, database, counts, energy state, active alarms | The console's Home and Assets screens |
| `export` | Open-format export of registry and history | None |

---

## 3. Setting up a node by hand

This is what the gateway's first-run setup does for you. Run it by hand only on
a node with no gateway, or when you want the error message on your terminal
instead of in a log.

```sh
chaos validate            # the design package is intact
chaos init-db             # idempotent; creates tables, never alters them
chaos load-all            # registry + load schedule + alarm definitions
chaos status              # confirm what landed
```

`load-all --skip-missing` skips stages whose subsystem is not installed rather
than failing — which is what the gateway uses, because a node without the alarm
engine should still get a registry.

Expected after a successful load:

```text
Homestead Digital Twin 0.4.0
  node role        : primary
  physical control : disabled

Registry and history
  assets                 90
  points                 701
  point_bindings         245
  power_load_profiles    12
  alarm_definitions      40
```

`physical control : disabled` is correct and should stay that way until a
subsystem has passed [commissioning](./commissioning.md).

**The banner still reads "Homestead Digital Twin".** That is the project's
former name; the package, the environment prefix and the message-bus topics have
all moved to `chaos`, and this banner has not. It is cosmetic, and it is the one
place the old name still appears at runtime.

---

## 4. Status

```sh
chaos status
chaos status --json
```

`status` prints the node role, site, database URL, whether physical control is
enabled, the broker configuration, the historian backend, which services are
enabled, the registry and history counts, the energy state and the active
alarms.

`--json` is the machine-readable form, and it is what the gateway's first-run
setup reads to decide whether a database needs setting up.

---

## 5. Backup

```sh
chaos backup                       # portable registry + configuration archive
chaos backup --output /mnt/offsite/
chaos backup --include-history     # also telemetry samples and dead letters (large)
```

Default output is `var/backups/homestead-<role>-<UTC>.tar.gz`.

**Credentials are never included.** The archive holds the registry,
configuration and the design package — enough to rebuild the platform on new
hardware — as JSON and YAML that can be read with nothing but a text editor.

The archive contains a manifest with counts, provenance and node role; a restore
procedure; one JSON file per table; and the design package the registry was
built from.

### The fuller backup set

The container deployment ships a script that additionally dumps the database,
the broker configuration, the dashboards and the deployment configuration, with
a manifest and checksums. Its default destination is `/var/backups/homestead`,
overridable with `BACKUP_DIR`. See
[Container deployment § Backups](./advanced-container-deployment.md#7-backups).

**Keep three copies**: local, on the secondary node in another structure, and
offline/off-property. See [Operations § Backup and restore](./operations.md#4-backup-and-restore).

---

## 6. Retention

```sh
chaos retention --dry-run          # the default: computes and rolls back
chaos retention --apply
```

Run it daily, off-peak, **after** the backup.

Two warnings:

- The dry run rolls its transaction back, so an implementation that committed
  internally would still persist. **Check the printed counts, not just the exit
  code.**
- Retention is a **deletion**. Never run it for the first time against a full
  production historian without a dump in hand.

The policy itself is in [Operations § Data retention](./operations.md#5-data-retention).

---

## 7. Export

```sh
chaos export --format yaml --include registry -o registry.yaml
chaos export --include state,history --since 2026-08-01T00:00:00Z -o baseline.json
chaos export --include all --format json -o everything.json
```

Groups: `registry`, `state`, `history`, `alarms`, `commands`, `energy`,
`maintenance`, `all`. Default is `registry` to stdout as JSON.

`--since` / `--until` take ISO-8601 timestamps; `--limit` caps rows per table.

This is the open-format export requirement, and it is also how commissioning
step 12 captures a subsystem's baseline.

---

## 8. Running the backend for development

**This is the one development task with no GUI path today.**

The gateway is built to supervise the Python backend as a child process, and
ships the supervisor — but its shipped entry point registers the no-op
supervisor, so nothing launches the backend. See
[Visual Studio § What happens when you press F5](./visual-studio.md#what-happens-when-you-press-f5).

Until that is wired up, start the backend yourself on the port the gateway
expects:

```sh
chaos serve --host 127.0.0.1 --port 8081
```

Then press F5 on `Chaos.Host` (or `Chaos.Shell`) and everything works: the
gateway proxies to it, `/health` returns 200 with `backend: "up"`, and the
launcher reaches *Project CHAOS is ready*.

`serve --reload` restarts on source changes, which is useful while working on
the Python half and pointless otherwise.

**Bind it to loopback.** The backend is an unauthenticated control API. It is
reached through the gateway or not at all — the gateway refuses a non-loopback
backend address unless explicitly configured otherwise.

---

## 9. The simulator

```sh
chaos simulate -- --list-scenarios
chaos simulate -- --scenario <name> --offline
```

A simulated site — solar, battery, inverter, generator, loads, rack, weather —
publishing real envelopes on real topics, either to a real broker or to the
in-process bus. `--offline` uses the in-process bus and prints a summary.

This is how every control path in the platform has been exercised, and it is
what the "bench test" commissioning step means. It is also the fastest way to
give a fresh install something to look at.

---

## 10. Development targets

A `Makefile` at the repository root wraps the common Python-side tasks —
installing the package, running pytest, running ruff, validating the design
package, and the container stacks. `make` with no target prints its own help.

It exists for CI and for headless work. **Building and testing the solution is
done in Visual Studio** — see [Building in Visual Studio 2026](./visual-studio.md).

---

## 11. Related reading

| Document | Why |
|---|---|
| [Container deployment](./advanced-container-deployment.md) | The Docker stacks, the image, upgrades |
| [Windows packaging](./advanced-windows-packaging.md) | The embedded runtime and the PowerShell build scripts |
| [Secondary control node](./secondary-control-node.md) | The node this document mostly exists for |
| [Operations](./operations.md) | What to do with the output |
| [Building in Visual Studio 2026](./visual-studio.md) | The normal way to build, run and test |
