# Container deployment

> **Advanced. You do not need this to run Project CHAOS.**
>
> The supported way to run CHAOS on a Windows node is the installed product and
> the desktop shell — see [Getting started](./getting-started.md).
>
> This document covers the **container deployment**: a Docker Compose stack for
> the primary node and a second one for a headless secondary control node. It is
> the right shape for a Linux host in another structure with no display, and it
> is what CI exercises. Everything here is a terminal task by nature.

Related: [Command line](./advanced-command-line.md) ·
[Secondary control node](./secondary-control-node.md) ·
[Network and trust boundaries](./network-and-trust-boundaries.md) ·
[Operations](./operations.md)

---

## 1. Topology

| Node | Where | File | Services |
|---|---|---|---|
| **Primary** | The 20-foot power/utilities/battery/server container | `deploy/docker-compose.yml` | PostgreSQL+PostGIS, Mosquitto, the platform API, Prometheus, Grafana |
| **Secondary** | A physically separate structure (placement undecided) | `deploy/docker-compose.secondary.yml` | PostgreSQL replica, Mosquitto with an inbound-only bridge, the platform API in secondary role, Grafana |

Both run the same image. The role is chosen at runtime by `CHAOS_NODE_ROLE`.

Building a separate image for the secondary node would produce a secondary node
nobody has tested — and the version drift shows up exactly when you need it.

**Everything in the primary stack shares one physical failure domain.** The
design requires you to assume it is gone. Read
[Secondary control node](./secondary-control-node.md) before treating the
primary as the whole system.

---

## 2. Local development without containers

The platform runs on SQLite and an in-memory bus: no PostgreSQL, no broker, no
containers, no hardware. That is also the "bench test" commissioning step.

```sh
python3 -m venv .venv && . .venv/bin/activate
make install                      # pip install -e ".[dev]"

make validate                     # design package vs. its JSON Schemas
make init-db
make load
make status
make serve                        # http://127.0.0.1:8000
```

Give it something to look at, in a second terminal:

```sh
make simulate ARGS="--list-scenarios"
make simulate ARGS="--scenario <name> --offline"
```

### Settings

Every setting in `src/chaos/config.py` is overridable as `CHAOS_<FIELD>`, and a
`.env` in the working directory is read automatically.

| Variable | Default | Notes |
|---|---|---|
| `CHAOS_NODE_ROLE` | `primary` | `primary` or `secondary` |
| `CHAOS_DATABASE_URL` | `sqlite:///var/homestead.db` | PostgreSQL needs the `postgres` extra |
| `CHAOS_MQTT_ENABLED` | `true` | `false` runs the API with no bus and no background services |
| `CHAOS_ALLOW_PHYSICAL_CONTROL` | `false` | The safety gate. See section 6 |
| `CHAOS_EMS_ENABLED` | `true` | Ignored on a secondary node |
| `CHAOS_HISTORIAN_RAW_RETENTION_DAYS` | `90` | |
| `CHAOS_NOTIFICATION_BACKENDS` | `log` | Comma separated: `log,email,push,voice` — only `log` is implemented |

**The API port here is 8000**, because this is the platform backend running on
its own. In the packaged Windows product the backend is on 8081 behind the
gateway on 8080. See [Architecture](./architecture.md).

---

## 3. Primary node

### Prerequisites

- Docker Engine with the Compose plugin.
- A host in the servers network zone.
- **A firewall in front of it.** The compose file binds every port to
  `${BIND_ADDRESS:-127.0.0.1}` precisely so that Docker's port publishing is not
  what decides who can reach a control API.

### Configure

```sh
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env
```

Replace every `CHANGE_ME`. **Generate values; do not invent them:**

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Per-device and per-service credentials are required. One reused secret turns a
single compromised greenhouse sensor into broker-wide command authority.
`deploy/.env` is gitignored — keep it that way.

### Start

```sh
make up
make ps          # watch until every service is healthy
make logs
```

| Service | Port, bound to `BIND_ADDRESS` |
|---|---|
| Platform API and console | 8000 |
| Grafana | 3000 |
| Prometheus | 9090 |
| Mosquitto | 1883, 8883 |
| PostgreSQL | 5432 |

### Initialise

```sh
COMPOSE="docker compose -f deploy/docker-compose.yml"

$COMPOSE exec twin chaos validate
$COMPOSE exec twin chaos init-db
$COMPOSE exec twin chaos load-all
$COMPOSE exec twin chaos status
```

Table creation also runs at API startup, so this is belt and braces — but doing
it explicitly gives you an error message instead of a log line if the database
rejects it.

**On a Windows node the gateway does all of this for you**; see
[Getting started § What first-run setup does](./getting-started.md#4-what-first-run-setup-does).

### Broker credentials

The broker will not accept a connection until identities exist: anonymous access
is off, and both the password and permission files live in a volume rather than
in the repository. Follow `deploy/mosquitto/README.md` before expecting
telemetry.

Sanity check: look for denials in the broker log, and confirm `chaos status`
reports broker authentication as configured.

---

## 4. Secondary node

Full treatment in [Secondary control node](./secondary-control-node.md). Short
version:

```sh
cp deploy/.env.example deploy/.env
# Set every SECONDARY_* value to something DIFFERENT from the primary's.
make secondary-up
docker compose -f deploy/docker-compose.secondary.yml exec twin chaos status
```

Confirm the output reports `node role: secondary`, `physical control :
disabled`, `ems : disabled`.

**If physical control reads `ENABLED`, stop and fix it.** That is the failure
mode the whole secondary-node design exists to prevent.

---

## 5. The image

```sh
make build       # docker build -f deploy/Dockerfile -t chaos:local .
```

Multi-stage: stage 1 builds a virtual environment with the `postgres` extra;
stage 2 is a slim Python base plus `curl` and `tini`.

- **Runs as a non-root uid, never root.** The platform can command physical
  plant; it has no business running as uid 0.
- The health check polls `/health`, which reports liveness, version and node
  role and nothing else — no telemetry, no credentials, no control state.
- `data/`, `schemas/` and `tools/` are copied in, so validation and loading work
  offline with no repository checkout.
- `/app/var` is a volume: the SQLite fallback database and local backups.

CI builds the image, runs the CLI inside it, and asserts the container is
non-root and reaches healthy.

---

## 6. The safety gate

`CHAOS_ALLOW_PHYSICAL_CONTROL` defaults to `false` and should stay there until
the subsystem you intend to control has passed all twelve commissioning steps
and has the records to show for it.

Two independent gates exist, and turning on the global one **does not** enable
control of anything that has not been commissioned. See
[Control § The two safety gates](./control.md#6-the-two-safety-gates) and
[Commissioning](./commissioning.md).

---

## 7. Backups

```sh
# Portable registry + configuration archive:
docker compose -f deploy/docker-compose.yml exec twin chaos backup

# Full backup set: database dump + registry + broker + dashboards + configs:
deploy/backup/backup.sh
```

Schedule the second one:

```cron
15 2 * * * /srv/chaos/deploy/backup/backup.sh >> /var/log/homestead-backup.log 2>&1
```

The default destination is `/var/backups/homestead`, overridable with
`BACKUP_DIR`. A set contains the database dump, the registry archive, the broker
configuration (**sensitive**), the dashboards, the deployment configuration, a
manifest and a checksum file. **`deploy/.env` is excluded deliberately** — a
backup that quietly contains every credential on the property is a liability.

**A backup written to a disk inside the power container is not a backup.** It is
a second copy in the same failure domain. Keep three: local, on the secondary
node in another structure, and offline/off-property.

### Verifying

```sh
cd /var/backups/homestead/<timestamp>
sha256sum -c SHA256SUMS
cat MANIFEST                      # failures : 0
tar tzf twin-registry.tar.gz | head
```

### Restoring the database

```sh
COMPOSE="docker compose -f deploy/docker-compose.yml"

$COMPOSE stop twin                                   # stop writers first
$COMPOSE exec -T postgres dropdb   -U "$POSTGRES_USER" homestead
$COMPOSE exec -T postgres createdb -U "$POSTGRES_USER" homestead
$COMPOSE exec -T postgres psql -U "$POSTGRES_USER" -d homestead \
  -f /docker-entrypoint-initdb.d/10-homestead.sql
$COMPOSE exec -T postgres pg_restore -U "$POSTGRES_USER" -d homestead \
  --no-owner --no-privileges < postgres.dump
$COMPOSE start twin
$COMPOSE exec twin chaos status                      # counts vs. MANIFEST
```

### Rebuilding from the design package instead

Often faster, and often better: `data/` in version control is the source of
truth for registry content. **History is not recoverable this way.**

```sh
chaos init-db
chaos load-all
chaos status
```

### Restoring onto a machine with nothing

The portable archive is JSON plus YAML. Untar it and read it — a manifest with
counts and provenance, a restore procedure, one JSON file per table, and the
design package the registry was built from. That is the case where the container
is gone and you have a laptop.

### Credentials are re-issued, not restored

If the container was destroyed or compromised, the secrets inside it should be
assumed readable. Re-issue broker identities, dashboard admin and database
passwords, and record the new identities in the registry.

---

## 8. Retention

```sh
chaos retention --dry-run       # default; rolls back
chaos retention --apply
```

Schedule daily, off-peak, after the backup:

```cron
30 3 * * * docker compose -f /srv/chaos/deploy/docker-compose.yml \
             exec -T twin chaos retention --apply
```

Policy and warnings: [Operations § Data retention](./operations.md#5-data-retention).

---

## 9. Upgrading

```sh
git pull
make build
make up                                          # recreates changed services
docker compose -f deploy/docker-compose.yml exec twin chaos init-db
docker compose -f deploy/docker-compose.yml exec twin chaos status
```

**There are no schema migrations yet.** Table creation adds new tables but never
alters existing ones. A change to an existing column is currently a
dump-and-restore. Alembic is declared in the `postgres` extra for when this
stops being acceptable — which is the moment real telemetry is in the historian.

**Take a backup before every upgrade.**

---

## 10. Troubleshooting the container stack

| Symptom | Check |
|---|---|
| The platform container restarts repeatedly | `make logs`. Usually the database URL or a missing broker username |
| The health check never goes healthy | Inspect the container's health state. `/health` binds to `CHAOS_API_PORT` |
| No telemetry arriving | `chaos status` → dead-letter count, then the broker log for denied publishes |
| `status` shows zero assets | `load-all` has not run, or ran against a different database |
| Grafana panels empty | Correct — the platform writes nothing until ingest runs. Check the datasource first: Connections → Data sources → homestead-pg → Save & test |
| Prometheus shows one target | Correct. Every other job is commented out because its exporter is not deployed |
| `psycopg` import error | Install the `postgres` extra, or use the image, which includes it |
| Compose refuses to start | `docker compose -f deploy/docker-compose.yml config -q` names the missing variable |

For symptoms on a Windows node, use [Troubleshooting](./troubleshooting.md)
instead — it is written for the shell and the console.

---

## 11. Related reading

| Document | Why |
|---|---|
| [Command line](./advanced-command-line.md) | Every subcommand used above |
| [Secondary control node](./secondary-control-node.md) | The node the second stack is for |
| [Network and trust boundaries](./network-and-trust-boundaries.md) | Zones, flows and service identities |
| [Operations](./operations.md) | What to do with a running stack |
| [Architecture](./architecture.md) | How this shape relates to the Windows product |
