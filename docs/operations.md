# Operations runbooks

Procedures for running the platform. Each runbook states what it assumes, what to
do, and what it does **not** cover.

Read this first: **the platform is a supervisor, not a protection system.** If a
runbook here conflicts with what the equipment in front of you is telling you,
the equipment wins. Level 0 protection — breakers, relief valves, float switches,
high-limit thermostats, emergency stops — is not something software gets a vote
on (SDD sections 5.5, 6).

---

## 0. Daily and weekly

**Daily (2 minutes)**

```sh
chaos status
```

Look at four things: active alarms, `ingest_dead_letters`, the EMS state and its
evaluation age, and the count of stale points on the platform dashboard. A
dead-letter count that grows every day is a binding that was never commissioned
properly, not noise.

**Weekly**

- Confirm the nightly backup ran: `ls -lt /var/backups/homestead | head`, then
  read the newest `MANIFEST` for `failures : 0`.
- Confirm the secondary node's replica is current: run `chaos status`
  there and compare counts with the primary.
- Skim the platform dashboard's "Point bindings by status" panel. Anything still
  `tbd` is un-commissioned, whatever the wiring looks like.

**Monthly**

- Restore the latest backup into a scratch database and confirm the counts match
  the manifest. A backup you have never restored is a hypothesis (SDD MVP
  criterion 10).

---

## 1. Black start — recovery from a de-energized property

**SDD section 35. Read it before you need it.**

This runbook covers the platform's part only. The electrical sequence is executed
at the equipment by Level 0/1 controls, and SDD 35.2 requires that at least one
local controller can execute it **without the primary server rack**. If the
platform is required for a black start, the black start design is wrong.

### Prerequisites (SDD 35.2 — verify physically, not on a screen)

1. BMS and inverter controls have protected DC power or an approved manual source.
2. Battery temperature and voltage are within black-start limits.
3. Fire, smoke and emergency-stop conditions are clear.
4. Critical distribution can be isolated from non-critical branches.
5. A local controller can run the sequence unaided.

### Sequence (SDD 35.3) — platform steps only

| SDD step | Action | Platform involvement |
|---:|---|---|
| 1–4 | Verify isolation and safety; energise BMS/inverter control power; close the battery contactor via native precharge; start the master inverter group | **None.** Do not attempt any of this from the API |
| 5 | Energise the critical control/communications bus | None |
| 6 | Start the secondary control node, core switch/router, minimum MQTT and time services | Start the secondary stack: `make secondary-up`. NTP first — see section 6 |
| 7 | Validate battery, inverter, frequency and critical-bus measurements | Read them at the equipment. Then, once ingest is up, cross-check against `chaos status` and the energy dashboard |
| 8 | Energise minimum refrigeration, water protection, greenhouse survival and security loads, staggered | Manual. SDD 33.2 forbids simultaneous restoration; the EMS is not running yet |
| 9 | Start the generator if reserve or battery limits require it | At the generator controller |
| 10 | Start primary rack services once the critical bus and container environment are stable | `make up`, then `chaos status` |
| 11 | Reconcile actual asset states with the digital twin | Section 1.1 below |

### 1.1 State reconciliation (SDD 35.4)

**The platform must not assume retained desired state equals physical state.**

After the primary stack is up:

```sh
chaos status
```

1. **Expire stale commands.** Anything in `pending` or `dispatched` from before
   the outage is meaningless. Check `/api/v1/commands?state=pending`; cancel what
   has not expired on its own TTL.
2. **Distrust retained MQTT state.** Retained topics may describe the world as it
   was before the lights went out.
3. **Find what the platform cannot see.** The platform dashboard's stale-points
   panel, or `GET /api/v1/telemetry/stale`. Every point there is unknown, not
   normal.
4. **Walk the plant.** Confirm actual breaker, valve and equipment states against
   what the twin believes. Correct the twin, not the plant.
5. **Only then** re-enable the EMS. It must not begin dispatching against a state
   model that is a mixture of pre-outage memory and post-outage guesswork.

Do not restart workshop machinery, spa equipment or any attended load
unattended after a black start (SDD 35.3).

---

## 2. Communications loss

### 2.1 A gateway goes quiet

**Symptom:** points stale; a last-will `availability` message arrived; alarms
firing for one asset group.

1. Is it the gateway or the network? `chaos status` — if `ingest` counts
   are still rising for other assets, the broker and ingest are fine.
2. Check the broker: `docker compose -f deploy/docker-compose.yml logs mosquitto | tail -50`.
   A gateway that reconnects in a loop is usually a credential or ACL problem, not
   a radio problem.
3. Check for `Denied PUBLISH` — an ACL that was edited without re-testing.
4. **The subsystem keeps running.** Level 1 retains control (SDD 5.2). Loss of
   telemetry is loss of visibility, not loss of control. Do not start
   power-cycling equipment to restore a dashboard.
5. Record the outage against the asset. If it recurs, it is a commissioning step 6
   failure that was passed too generously.

### 2.2 Broker loss

Every gateway's telemetry stops at once, and commands cannot be dispatched.

1. `docker compose -f deploy/docker-compose.yml ps mosquitto`
2. Restart: `docker compose -f deploy/docker-compose.yml restart mosquitto`.
   Persistent sessions and queued messages survive (`persistence true`).
3. If the broker will not start, check the volume: a missing
   `/mosquitto/config/local/passwd` stops it dead, and that is by design.
4. Expect a burst of retained-state and queued-message traffic on recovery, and
   expect state reconciliation (section 1.1) to matter.

### 2.3 Internet loss

**The platform is unaffected.** Local-first is the point (SDD 5.1, FR-006).

What stops: outbound email and push notification, upstream NTP, remote VPN
access. What continues: ingest, EMS, alarms, dashboards, commands, local
notification.

SDD MVP criterion 4 requires critical alarms to work during internet loss. If
`CHAOS_NOTIFICATION_BACKENDS` is only `log` and `email`, that criterion is
**not met** — a log line is not an alert. Local voice (CUCM) or an independent
device path is needed. Currently unresolved; see `docs/architecture.md`.

### 2.4 Loss of the primary node

See `docs/secondary-control-node.md` section 3. Summary: the secondary node
observes and alerts; local controllers keep the property running; nothing
automatically takes over supervisory control, and that is deliberate.

---

## 3. Alarm floods

**Symptom:** dozens or hundreds of alarms in seconds. Usually one root cause —
a power event, a comms failure, a gateway reboot.

### Do not

- Do not bulk-acknowledge to clear the screen. Acknowledgement is a record that a
  human saw it (SDD 14.2); bulk-acknowledging destroys the only evidence of what
  the operator actually knew.
- Do not suppress the alarm definition. That hides the next occurrence too.

### Do

1. **Find the incident, not the alarms.** The platform correlates related alarms
   into incidents precisely so one container outage does not produce hundreds of
   independent notifications: `GET /api/v1/incidents`, or the platform dashboard.
2. **Sort by severity and time.** The earliest critical alarm is usually the
   cause; the rest are consequences.
3. **Check whether the platform is the problem.** A flood with no plant symptoms
   is often ingest: a gateway republishing history with old timestamps, or a
   binding pointed at the wrong point.
4. **If notification volume is the emergency**, reduce the notification backends
   rather than the alarms — the alarms are the record.

   ```sh
   docker compose -f deploy/docker-compose.yml exec twin \
     env CHAOS_NOTIFICATION_BACKENDS=log chaos status   # inspect only
   ```

   Changing it for real means editing `deploy/.env` and recreating the service.
   Write down that you did it, and when you undid it.
5. **Afterwards**, review. A flood is usually an alarm-design failure: missing
   dead-band, missing on-delay, or an alarm on a derived value that should have
   been an alarm on its input. Fix `data/alarm_definitions.yaml`, then
   `chaos load-all`.

### Suppression during planned work

Use maintenance mode, not suppression:

```
POST /api/v1/operating-modes/{domain}   {"mode": "maintenance", "reason": "...", ...}
```

Maintenance mode inhibits automatic starts and modifies alarms with the lockout
visible (SDD 11). Suppressing an alarm makes the lockout invisible, which is how
a subsystem gets left in maintenance mode for three weeks.

---

## 4. Backup and restore

### 4.1 Taking a backup

```sh
deploy/backup/backup.sh                    # full set
chaos backup                      # registry + config only, portable
```

The full set contains `postgres.dump`, `twin-registry.tar.gz`,
`mosquitto-config.tar.gz` (**sensitive**), `grafana.tar.gz`,
`deploy-config.tar.gz`, a `MANIFEST` and `SHA256SUMS`. `deploy/.env` is excluded
deliberately: a backup that quietly contains every credential on the property is
a liability.

Three copies (SDD 15.8, 16.1): local, on the secondary node in another structure,
and offline/off-property.

### 4.2 Verifying

```sh
cd /var/backups/chaos/<timestamp>
sha256sum -c SHA256SUMS
cat MANIFEST                      # failures : 0
tar tzf twin-registry.tar.gz | head
```

### 4.3 Restoring the database

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
$COMPOSE exec twin chaos status             # counts vs. MANIFEST
```

### 4.4 Rebuilding from the design package instead

Faster, and often better: the design package in Git is the source of truth for
registry content. History is not recoverable this way.

```sh
chaos init-db
chaos load-all
chaos status
```

### 4.5 Restoring onto a machine with nothing

`twin-registry.tar.gz` is JSON plus YAML. Untar it and read it. This is the
SDD 16.1 mitigation-3 case: the container is gone and you have a laptop.

```sh
tar xzf twin-registry.tar.gz
cat manifest.json                # counts, provenance, node role
cat RESTORE.txt                  # procedure
ls tables/                       # one JSON array per table
ls design-package/data/          # the YAML the registry was built from
```

### 4.6 Credentials are re-issued, not restored

MQTT identities, Grafana admin, database passwords. If the container was
destroyed or compromised, the secrets inside it should be assumed readable.
Re-issue them (`deploy/mosquitto/README.md`), and record the new identities as
`ExternalIdentifier` rows.

---

## 5. Data retention

SDD 16.4:

| Class | Policy |
|---|---|
| Raw high-frequency telemetry | 90 days (`CHAOS_HISTORIAN_RAW_RETENTION_DAYS`) |
| Downsampled 1-minute | 2 years |
| Downsampled hourly/daily | Indefinite |
| Alarm and command audit | **Indefinite — never pruned** |
| Maintenance and asset history | Indefinite |
| Camera footage | Separate policy, outside this platform |

```sh
chaos retention --dry-run       # default; computes and rolls back
chaos retention --apply
```

Schedule daily:

```
30 3 * * * docker compose -f /srv/chaos/deploy/docker-compose.yml \
             exec -T twin chaos retention --apply
```

Two things worth knowing. The dry run rolls the transaction back, so an
implementation that commits internally would still persist — check the printed
counts, not just the exit code. And retention is a **deletion**: run it on a
schedule, after backups, never for the first time on a full production historian
without a dump in hand.

Open decision SDD 22.10 (retention limits based on storage and power budget) is
unresolved; 90 days is the SDD's initial policy, not a measured one.

---

## 6. Time synchronisation

SDD 16.3: all servers, gateways, PLCs, cameras and field nodes use the homestead
NTP service, with an external source when available and a local holdover source
when isolated.

Every platform timestamp is UTC (`models/base.py`); display conversion happens in
the UI. `deploy/postgres/init.sql` sets the database to UTC.

Clock skew is a subtle failure: it makes alarm correlation wrong, command TTLs
wrong, and `$__timeFilter` in Grafana silently return nothing. If a dashboard is
empty and ingest counts are rising, check clocks before anything else.

The NTP service itself is **not deployed by this repository**. Nothing here
provides holdover.

---

## 7. Enabling physical control

Do not skip to this section.

1. The subsystem has passed all twelve SDD section 19 steps —
   `docs/commissioning.md`, and `GET /api/v1/commissioning/{asset_id}/status`.
2. `POST /api/v1/commissioning/bindings/{point_id}` sets
   `automatic_control_allowed` for that binding. The platform refuses if the
   prerequisites have not passed.
3. Only then set `CHAOS_ALLOW_PHYSICAL_CONTROL=true` in `deploy/.env` and
   recreate the `twin` service.
4. Verify: `chaos status` shows `physical control : ENABLED`.
5. Issue one command, watch it acknowledge, and read the audit record before
   issuing a second.

To revoke, in an emergency:

```sh
# Fastest: revoke at the broker. Application state is irrelevant if the
# command cannot leave the bus.
docker compose -f deploy/docker-compose.yml exec mosquitto \
  sh -c 'sed -i "s|^topic write chaos/+/+/+/cmd/+|# REVOKED &|" /mosquitto/config/local/acl'
docker compose -f deploy/docker-compose.yml kill -s HUP mosquitto

# Then, properly:
# set CHAOS_ALLOW_PHYSICAL_CONTROL=false in deploy/.env and recreate `twin`.
```

Then find out why, and write it down.

---

## 8. What these runbooks do not cover

- **Electrical work.** Black start, generator start, transfer switching, battery
  isolation. Equipment procedures, not platform procedures.
- **Water, greenhouse, spa, nitrogen storage.** No coordinator exists for these
  yet (`docs/architecture.md`).
- **Camera and NVR operations.** Not integrated.
- **Network device recovery.** Switch, router and firewall procedures.
- **Anything requiring hardware that has never been connected.** Every runbook
  above has been exercised against the simulator and the in-memory bus. None has
  been exercised against real plant, because no part of this platform has.

That last point is the honest state of this document, and it changes one
subsystem at a time as commissioning proceeds.
