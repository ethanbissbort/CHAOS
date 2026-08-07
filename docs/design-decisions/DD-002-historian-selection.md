# DD-002: Time-series historian selection

**Status:** proposed — awaiting owner ratification
**Date raised:** 2026-08-07
**Decision owner:** homestead owner
**SDD reference:** §8.5, §16.4, §22 item 3, §40.1
**Blocks:** historian deployment; retention policy (§16.4); Grafana dashboard queries; backup and export design; FR-010 open-format export

---

## Context

SDD §8.5 states the requirement and leaves the choice open: "InfluxDB is a strong initial fit; TimescaleDB is an
alternative if a unified PostgreSQL stack is preferred." §22 item 3 lists it as an open decision. The register
carries the ambivalence forward: `it.application_service.rack_01.historian_01` has
`service_role: influxdb_or_timescaledb`.

### What the design already fixes

- **The registry is not the historian** (§40.1). PostgreSQL holds identity, configuration, topology, current
  authoritative state and lifecycle records. The historian holds high-volume samples. The registry keeps the
  historian series key and sampling policy but must not become a telemetry table. Whichever historian is chosen,
  this boundary does not move.
- **PostgreSQL with PostGIS is already in the stack**, as `it.application_service.rack_01.postgres_01`, and the
  property map in §8.6 depends on PostGIS. Postgres is not optional.
- **Retention is tiered** (§16.4): raw high-frequency 90 days, one-minute downsample two years, hourly and daily
  indefinite, alarm and command audit indefinite.
- **Grafana is the visualization layer** (§8.6), and it supports both candidates.
- **Local-first is non-negotiable** (§5.1). Anything requiring a cloud service or a licence check to start is
  disqualified before the comparison begins.

### What makes this decision unusually consequential here

Migrating a historian is not like migrating a service. Years of telemetry accumulate in a schema and a query
language, and dashboards, alert rules and reports encode that query language. The design document plans for
decades of operation and explicit repairability (§5.6, §43). Choosing a query language is therefore closer to
choosing a data format than to choosing a package.

---

## Options

### Option 1 — InfluxDB

**For**

- Purpose-built for time series: fast ingest, native downsampling and retention policies that map almost directly
  onto the §16.4 tiers.
- Tag-based model fits the telemetry envelope (§10.2) well: `asset_id`, `point_name`, `quality`, `source` become
  tags, and the point value becomes a field.
- Widely used in exactly this role, with abundant Home Assistant, Node-RED, Telegraf and Grafana integration.
- Standalone: a historian outage does not take the registry down with it.

**Against**

- A second database engine to operate, back up, upgrade and understand. On a homestead with one operator, every
  additional operational surface is a real cost.
- The InfluxQL / Flux / SQL-ish language history has been unstable across major versions, and version migrations
  have been disruptive. A twenty-year plan should weigh that heavily.
- Joining telemetry to registry data means querying two systems and joining in the application or in Grafana, which
  makes questions like "show power for every asset whose criticality is critical" awkward.
- Its own backup and restore mechanism, separate from the Postgres one.

### Option 2 — TimescaleDB (PostgreSQL extension)

**For**

- One database engine. One backup, one restore procedure, one authentication model, one upgrade path, one thing to
  learn. On a single-operator site this is the dominant practical consideration.
- Telemetry and registry live in the same database, so cross-domain queries are ordinary SQL joins: point history
  joined to asset class, criticality, location, alarm state or work orders, in one query.
- SQL is the most durable query language available. Dashboards, reports and exports written now will still be
  readable in twenty years, which is exactly what §5.6 and §43 ask for.
- Continuous aggregates and retention policies implement the §16.4 tiers natively.
- PostGIS is in the same database, so geographic queries against the property map join telemetry directly.
- FR-010 open-format export is trivial from SQL.

**Against**

- Raw ingest throughput is lower than InfluxDB's. At homestead scale — hundreds of points at seconds-to-minutes
  intervals — this is very unlikely to bind, but it is a genuine difference.
- Couples the historian to the registry operationally: a Postgres problem is now a problem for both. Mitigable with
  separate instances on the same engine, at the cost of some of the joint-query benefit.
- Adds an extension to the Postgres instance that the registry depends on, so Postgres upgrades must consider
  TimescaleDB compatibility.
- Licensing of some advanced features differs from core PostgreSQL and should be read before committing.

### Option 3 — Both, with a defined split

InfluxDB for high-frequency raw telemetry, Postgres/TimescaleDB for downsampled long-term series and everything
joined to the registry.

**For**

- Each engine does what it is best at, and the two-year and indefinite tiers land where the joins are.

**Against**

- Two systems to operate *plus* a synchronisation path between them, which is more operational surface than either
  option alone, and a new failure mode where the tiers disagree.
- Directly contradicts §4.2's non-goal of unnecessary complexity in the first release.

---

## Recommendation

**Option 2 — TimescaleDB.**

Reasoning:

1. **One operator, one engine.** The homestead has one person maintaining it. §16.2 explicitly rejects a complex
   active-active cluster for the first release; the same reasoning applies to running two database engines. The
   cost of InfluxDB is not its ingest performance, it is a second thing to back up correctly at 2 a.m. during an
   outage.

2. **The joins are the point.** The whole architecture (§43) rests on separating asset identity, point identity and
   vendor binding. That separation only pays off if you can query across it. "Show me every critical-tier asset in
   the power container whose temperature exceeded its limit while the EMS was in `CONSERVE`" is one SQL statement
   in Option 2 and an application-layer join in Option 1. The design will ask questions like that constantly.

3. **SQL outlives products.** §5.6 versions everything and §43 targets decades of equipment replacement and
   software migration. SQL is the most portable thing available. If TimescaleDB itself is abandoned, the data is
   still in PostgreSQL tables and the queries still largely work — a materially better failure mode than a
   proprietary time-series query language.

4. **The scale argument does not bind.** The v0.3 package defines 214 point types and 245 bindings. Even with the
   fastest bindings at two-second sampling, this is orders of magnitude below where PostgreSQL-based time series
   struggles. Choosing the higher-throughput engine here optimises the constraint that is not binding at the cost
   of the one that is: operator time.

5. **PostGIS is already required.** §8.6 needs PostGIS for the property map. Postgres is in the stack no matter
   what. Option 2 adds an extension; Option 1 adds a system.

The honest counter-argument: if the owner already knows InfluxDB well and does not know SQL, Option 1 wins on
operator familiarity, and operator familiarity beats architectural elegance on a site with one operator. That is a
fact about the owner, not about the software, and it is why this record does not decide.

---

## Consequences

### If TimescaleDB is accepted

- `it.application_service.rack_01.historian_01.properties.service_role` becomes `timescaledb`, and the asset may
  be reconsidered as part of the Postgres service rather than a separate service position.
- Retention (§16.4) is implemented as continuous aggregates plus retention policies on hypertables.
- Backup design covers one engine, but the backup is larger and the restore-time objective must account for
  telemetry volume, not just registry size.
- The registry's historian series key (§40.1) becomes a table and column reference rather than a measurement name.
- Postgres sizing, WAL volume and disk allocation on the ZFS pool must be revisited for telemetry ingest.

### If InfluxDB is accepted

- Two backup and restore procedures, two upgrade paths, two authentication models.
- The registry must store an InfluxDB measurement and tag mapping for each point, and the loader must maintain it.
- Cross-domain queries move into Grafana or the digital-twin API, which becomes responsible for the join.
- A major-version migration path must be planned in advance, because history shows those are disruptive.

### Either way

- Retention limits (§22 item 10) remain undecided and are downstream of this record.
- FR-010 export must be demonstrated during commissioning, not assumed.

---

## What would settle this

1. **The owner's existing familiarity.** Which query language is already known? On a one-operator site this
   outweighs most technical arguments and is free to establish.
2. **A projected sample-rate budget** from `data/point_bindings.yaml`: sum the sampling intervals across all 245
   bindings, extrapolate to a fully instrumented site, and compute points per second and annual bytes. If it lands
   within the ordinary range for PostgreSQL time series, the throughput argument is closed.
3. **A restore drill on a representative dataset.** Time a full restore for each candidate at the projected
   two-year volume. Restore time, not ingest rate, is what matters at 2 a.m.
4. **A licence review** of the TimescaleDB features actually needed (hypertables, continuous aggregates,
   compression) against the licence the deployment will use.
5. **A written list of the ten questions the owner most wants to ask of the data.** Count how many need a
   telemetry-to-registry join. If most do, Option 2 is settled on the merits.

---

## Related records

- SDD §22 item 10 — data-retention limits, downstream of this decision.
- SDD §22 item 2 — virtualization platform, which determines how the historian is deployed.
- SDD §40.1 — the registry/historian separation, which neither option may violate.
