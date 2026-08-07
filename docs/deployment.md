# Deployment

How to run the Homestead Digital Twin: locally for development, and as a
container stack on the primary and secondary nodes.

Related: `docs/operations.md` (runbooks), `docs/commissioning.md` (before any
physical control), `docs/secondary-control-node.md`,
`docs/network-and-trust-boundaries.md`.

---

## 1. Deployment topology

| Node | Where | File | Services |
|---|---|---|---|
| **Primary** | The 20-foot power/utilities/battery/server container, on the R740xd | `deploy/docker-compose.yml` | PostgreSQL+PostGIS, Mosquitto, twin API, Prometheus, Grafana |
| **Secondary** | A physically separate structure (SDD 22.13, undecided) | `deploy/docker-compose.secondary.yml` | PostgreSQL replica, Mosquitto (bridge), twin API in secondary role, Grafana |

Both run the same image. Role is chosen at runtime by `HOMESTEAD_NODE_ROLE`.

Everything in the primary stack shares one physical failure domain. SDD section
16.1 requires the design to assume it is gone; read
`docs/secondary-control-node.md` before treating the primary as the whole system.

---

## 2. Local development

No PostgreSQL, no broker, no containers. The platform runs on SQLite and the
in-memory bus — which is also SDD 19 step 1, bench test.

```sh
git clone <repo> && cd homestead-twin
python3 -m venv .venv && . .venv/bin/activate
make install                      # pip install -e ".[dev]"

make validate                     # design package vs. its JSON Schemas
make init-db                      # create tables (var/homestead.db)
make load                         # registry + load schedule + alarm definitions
make status                       # confirm what landed
make run                          # http://127.0.0.1:8000  (UI at /ui, docs at /docs)
```

In a second terminal, give it something to look at:

```sh
make simulate ARGS="--list-scenarios"
make simulate ARGS="--scenario <name> --offline"
```

`--offline` uses the in-process bus and prints a summary. Point the simulator at
a real broker with `--broker` / `--port` once one is running.

### Environment

Every setting in `src/homestead_twin/config.py` is overridable as
`HOMESTEAD_<FIELD>`, and a `.env` in the working directory is read automatically.

| Variable | Default | Notes |
|---|---|---|
| `HOMESTEAD_NODE_ROLE` | `primary` | `primary` or `secondary` |
| `HOMESTEAD_DATABASE_URL` | `sqlite:///var/homestead.db` | PostgreSQL needs the `postgres` extra |
| `HOMESTEAD_MQTT_ENABLED` | `true` | `false` runs the API with no bus and no background services |
| `HOMESTEAD_ALLOW_PHYSICAL_CONTROL` | `false` | The safety gate. See section 6 |
| `HOMESTEAD_EMS_ENABLED` | `true` | Ignored on a secondary node |
| `HOMESTEAD_HISTORIAN_RAW_RETENTION_DAYS` | `90` | SDD 16.4 |
| `HOMESTEAD_NOTIFICATION_BACKENDS` | `log` | Comma separated: `log,email,push,voice` |

---

## 3. Primary node

### Prerequisites

- Docker Engine with the Compose plugin.
- A host in the `SERVERS` zone (VLAN 20).
- A firewall in front of it. The compose file binds every port to
  `${BIND_ADDRESS:-127.0.0.1}` precisely so that Docker's port publishing is not
  what decides who can reach a control API.

### Configure

```sh
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env
```

Replace every `CHANGE_ME`. Generate values; do not invent them:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

SDD 15.2 requires per-device and per-service credentials. One reused secret turns
a single compromised greenhouse sensor into broker-wide command authority.
`deploy/.env` is gitignored — keep it that way.

### Start

```sh
make up                                  # or: docker compose -f deploy/docker-compose.yml up -d
make ps                                  # watch until every service is healthy
make logs
```

| Service | Port (bound to `BIND_ADDRESS`) |
|---|---|
| Twin API and UI | 8000 |
| Grafana | 3000 |
| Prometheus | 9090 |
| Mosquitto | 1883, 8883 |
| PostgreSQL | 5432 |

### Initialise

```sh
COMPOSE="docker compose -f deploy/docker-compose.yml"

$COMPOSE exec twin homestead-twin validate     # design package intact
$COMPOSE exec twin homestead-twin init-db      # idempotent
$COMPOSE exec twin homestead-twin load-all     # registry + loads + alarms
$COMPOSE exec twin homestead-twin status
```

`init-db` also runs at API startup (`create_app(init_db=True)`), so this is
belt and braces — but running it explicitly gives you an error message instead of
a log line if the database rejects it.

### Broker credentials

The broker will not accept a connection until identities exist:
`allow_anonymous false`, and both `passwd` and `acl` live in a volume, not in the
repository. Follow `deploy/mosquitto/README.md` before expecting telemetry.

Sanity check:

```sh
$COMPOSE logs mosquitto | grep -i "denied\|error"
$COMPOSE exec twin homestead-twin status        # mqtt line should show auth=yes
```

---

## 4. Secondary node

Full treatment in `docs/secondary-control-node.md`. Short version:

```sh
cp deploy/.env.example deploy/.env
# Set every SECONDARY_* value to something DIFFERENT from the primary's.
make secondary-up
docker compose -f deploy/docker-compose.secondary.yml exec twin homestead-twin status
```

Confirm the output says `node role: secondary`, `physical control : disabled`,
`ems : disabled`. If physical control reads `ENABLED`, stop and fix it.

---

## 5. The container image

```sh
make build            # docker build -f deploy/Dockerfile -t homestead-twin:local .
```

Multi-stage: stage 1 builds a virtualenv with the `postgres` extra; stage 2 is
`python:3.11-slim` plus `curl` and `tini`.

- Runs as uid 10001 (`homestead`), never root. The platform can command physical
  plant; it has no business running as uid 0.
- `HEALTHCHECK` polls `/health`, which reports liveness, version and node role
  and nothing else — no telemetry, no credentials, no control state.
- `data/`, `schemas/` and `tools/` are copied in, so `homestead-twin validate`
  and `load-all` work offline with no repository checkout.
- `/app/var` is a volume: SQLite fallback database and local backups.

CI builds the image, runs the CLI inside it, and asserts the container is
non-root and reaches `healthy`.

---

## 6. The safety gate

`HOMESTEAD_ALLOW_PHYSICAL_CONTROL` defaults to `false` and should stay there
until the subsystem you intend to control has passed all twelve steps of the SDD
section 19 sequence and has the commissioning records to show for it.

Two independent gates exist:

1. **Global** — `HOMESTEAD_ALLOW_PHYSICAL_CONTROL`.
2. **Per binding** — `point_bindings.automatic_control_allowed`, which
   `maintenance/commissioning.py` will only set once steps 1–8 and step 9 have
   passed for that asset.

Enabling the global flag does not enable control of anything that has not been
commissioned. That is the design: one switch should not be able to arm the whole
property.

See `docs/commissioning.md`.

---

## 7. Backups

```sh
# Portable registry + configuration archive (SDD 16.1 mitigation 3):
docker compose -f deploy/docker-compose.yml exec twin homestead-twin backup

# Full backup set: pg_dump + registry + Mosquitto + Grafana + configs:
deploy/backup/backup.sh
```

Schedule the second one:

```
15 2 * * * /srv/homestead-twin/deploy/backup/backup.sh >> /var/log/homestead-backup.log 2>&1
```

A backup written to a disk inside the power container is not a backup — it is a
second copy in the same failure domain. Keep three: local, on the secondary node
in another structure, and offline/off-property. See the header of
`deploy/backup/backup.sh` and `docs/operations.md`.

---

## 8. Retention

SDD 16.4: raw high-frequency telemetry 90 days; downsampled 1-minute data 2
years; hourly/daily indefinitely; alarm and command audit indefinitely.

```sh
homestead-twin retention --dry-run       # default; rolls back
homestead-twin retention --apply
```

Schedule daily, off-peak:

```
30 3 * * * docker compose -f /srv/homestead-twin/deploy/docker-compose.yml \
             exec -T twin homestead-twin retention --apply
```

---

## 9. Upgrading

```sh
git pull
make build
make up                                          # recreates changed services
docker compose -f deploy/docker-compose.yml exec twin homestead-twin init-db
docker compose -f deploy/docker-compose.yml exec twin homestead-twin status
```

**There are no schema migrations yet.** The platform uses SQLAlchemy
`create_all`, which adds new tables but never alters existing ones. A change to
an existing column is currently a dump-and-restore. `alembic` is declared in the
`postgres` extra for when this stops being acceptable — which is the moment real
telemetry is in the historian.

Take a backup before every upgrade. `make backup` is one command.

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| `twin` restarts repeatedly | `make logs`. Usually the database URL or a missing `HOMESTEAD_MQTT_USERNAME` |
| Healthcheck never goes healthy | `docker inspect --format '{{json .State.Health}}' <container>`. `/health` binds to `HOMESTEAD_API_PORT` |
| No telemetry arriving | `homestead-twin status` → `ingest_dead_letters` count. Then the platform dashboard's dead-letter panel, then the broker log for `Denied PUBLISH` |
| `status` shows zero assets | `homestead-twin load-all` has not run, or ran against a different database |
| Grafana panels empty | Correct — the platform writes nothing until ingest runs. Check the datasource first: Connections → Data sources → homestead-pg → Save & test |
| Prometheus shows one target | Correct. See `deploy/prometheus/prometheus.yml`; every other job is commented out |
| `psycopg` import error | `pip install -e ".[postgres]"`, or use the container image, which includes it |
| Compose refuses to start | `docker compose -f deploy/docker-compose.yml config -q` names the missing variable |

The CLI is designed to be usable on a node with nothing else working:

```sh
homestead-twin status            # role, database, counts, EMS state, alarms
homestead-twin status --json     # same, machine readable
homestead-twin --log-level DEBUG status
```
