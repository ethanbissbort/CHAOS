-- Project CHAOS -- PostgreSQL bootstrap.
--
-- Runs once, on first initialisation of an empty data directory
-- (/docker-entrypoint-initdb.d). It must be safe to re-run by hand, so every
-- statement is IF NOT EXISTS.
--
-- Ordering note: this executes BEFORE the application has created any tables.
-- `chaos init-db` (SQLAlchemy `create_all`) owns the schema. Anything
-- here that touches an application table is therefore written as a DO block
-- that checks for the table first, so a fresh database and an existing one both
-- succeed. See SDD sections 8.1 and 40.

-- ===========================================================================
-- 1. Extensions
-- ===========================================================================

-- PostGIS: required by SDD 8.1/8.6 for the property map (structures, utility
-- lines, tanks, valves, irrigation zones, orchard blocks, solar rows, cameras).
CREATE EXTENSION IF NOT EXISTS postgis;

-- Trigram indexes make asset-name and point-name search usable in the UI.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Query statistics. Left commented: pg_stat_statements only works when the
-- library is in shared_preload_libraries, and enabling it here without that
-- would create an extension whose views error on first use. Add both together
-- when the historian starts to hurt.
-- CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- TimescaleDB is the other candidate for the historian (SDD 8.5 and open
-- design decision 22.3: InfluxDB vs TimescaleDB). It is NOT enabled here
-- because that decision is unresolved and the postgis/postgis image does not
-- ship it. If TimescaleDB wins, switch the image and add:
--   CREATE EXTENSION IF NOT EXISTS timescaledb;
--   SELECT create_hypertable('telemetry_samples', 'ts', if_not_exists => TRUE);
-- Until then the relational historian in telemetry_samples is what runs.

-- ===========================================================================
-- 2. Timezone and time discipline
-- ===========================================================================
-- Every platform timestamp is UTC (models/base.py utcnow, SDD 16.3). Display
-- conversion happens in the UI, not in the database. The database name comes
-- from POSTGRES_DB, so it has to be interpolated rather than hard-coded.
DO $$
BEGIN
  EXECUTE format('ALTER DATABASE %I SET timezone TO %L', current_database(), 'UTC');
END
$$;

-- ===========================================================================
-- 3. Post-schema objects
--
-- These blocks no-op on a fresh database and apply after `init-db` has run.
-- Re-run them at any time with:
--   docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -f /docker-entrypoint-initdb.d/10-homestead.sql
-- ===========================================================================

DO $$
BEGIN
  -- --- Registry search -----------------------------------------------------
  IF to_regclass('public.assets') IS NOT NULL THEN
    CREATE INDEX IF NOT EXISTS ix_assets_name_trgm
      ON assets USING gin (name gin_trgm_ops);

    -- The register carries JSONB blobs (properties, network, power, location).
    -- A GIN index makes "which assets are on VLAN 30" answerable without a
    -- full scan. Host addressing in the register is deliberately TBD; the
    -- vlan_id key is not.
    CREATE INDEX IF NOT EXISTS ix_assets_network_gin
      ON assets USING gin (network jsonb_path_ops);
    CREATE INDEX IF NOT EXISTS ix_assets_properties_gin
      ON assets USING gin (properties jsonb_path_ops);

    -- Hot filters on the home screen (SDD 17.1).
    CREATE INDEX IF NOT EXISTS ix_assets_domain_status
      ON assets (domain, status);
    CREATE INDEX IF NOT EXISTS ix_assets_criticality
      ON assets (criticality);

    COMMENT ON TABLE assets IS
      'Canonical asset identity (SDD 25). asset_id is never reused and never '
      'derived from a vendor identifier. Vendor serials, MACs, IPs and entity '
      'IDs belong in external_identifiers.';
  END IF;

  IF to_regclass('public.points') IS NOT NULL THEN
    CREATE INDEX IF NOT EXISTS ix_points_name_trgm
      ON points USING gin (point_name gin_trgm_ops);
    COMMENT ON TABLE points IS
      'Point instances: one canonical point definition applied to one asset '
      '(SDD 26.2). point_id is always <asset_id>/<point_name>.';
  END IF;

  IF to_regclass('public.point_bindings') IS NOT NULL THEN
    -- Ingest resolves an incoming MQTT topic to a point through mqtt_topic on
    -- every message; it is the hottest lookup in the platform. The model
    -- already declares ix_point_bindings_mqtt_topic (column index=True), so no
    -- index is added here -- adding one under the same name would silently
    -- no-op and read as a guarantee that is not being made.
    CREATE INDEX IF NOT EXISTS ix_point_bindings_status
      ON point_bindings (binding_status);
    COMMENT ON TABLE point_bindings IS
      'How a device currently exposes a canonical point (SDD 47). Bindings '
      'change over the life of the property; point identity does not. '
      'binding_status stays ''tbd'' until commissioning verifies the address.';
  END IF;

  -- --- Historian -----------------------------------------------------------
  IF to_regclass('public.telemetry_samples') IS NOT NULL THEN
    -- Retention deletes by ts; the composite index already declared by the
    -- model covers point-scoped queries. This one covers the sweep.
    CREATE INDEX IF NOT EXISTS ix_telemetry_samples_ts_brin
      ON telemetry_samples USING brin (ts) WITH (pages_per_range = 32);

    COMMENT ON TABLE telemetry_samples IS
      'Relational historian (SDD 40.1). Raw samples are pruned per SDD 16.4 '
      '(default 90 days) by `chaos retention --apply`. If this table '
      'becomes the bottleneck, resolve open design decision 22.3 and move to '
      'TimescaleDB or InfluxDB rather than growing it.';
  END IF;

  IF to_regclass('public.current_state') IS NOT NULL THEN
    CREATE INDEX IF NOT EXISTS ix_current_state_quality_ts
      ON current_state (quality, ts DESC);
    COMMENT ON TABLE current_state IS
      'Current-state cache (SDD 40.2). Must be reconstructable from the '
      'registry plus the message stream after a restart -- never the only copy '
      'of anything.';
  END IF;

  -- --- Alarms and audit ----------------------------------------------------
  IF to_regclass('public.alarms') IS NOT NULL THEN
    -- The active-alarm query on every dashboard refresh (SDD 17.1, FR-001).
    CREATE INDEX IF NOT EXISTS ix_alarms_open
      ON alarms (severity, detected_at DESC)
      WHERE state IN ('detected', 'active', 'acknowledged', 'mitigated');
    COMMENT ON TABLE alarms IS
      'Alarm occurrences (SDD 14.2). Retained indefinitely -- SDD 16.4 exempts '
      'alarm and command audit from retention pruning.';
  END IF;

  IF to_regclass('public.audit_log') IS NOT NULL THEN
    COMMENT ON TABLE audit_log IS
      'Append-only record of every control action (SDD 5.7, 15.2, FR-004). '
      'Never pruned. Never updated.';
  END IF;

  -- --- Geospatial ----------------------------------------------------------
  IF to_regclass('public.locations') IS NOT NULL THEN
    -- models/registry.py Location stores GeoJSON in `geometry` (JSONB) so the
    -- platform runs on SQLite for bench testing and on the low-power secondary
    -- node. PostGIS is additive here, never the only copy.
    --
    -- The generated column below is the intended PostGIS path: it derives a
    -- real geometry from the GeoJSON the application already writes, so the
    -- application needs no PostGIS-specific code and SQLite keeps working.
    --
    -- Enable it once the map work starts and a coordinate reference system has
    -- been agreed. SRID 4326 (WGS 84) matches GeoJSON; a projected SRID is
    -- better for area and distance and needs the final property location,
    -- which open design decision 22.7 leaves unresolved.
    --
    --   ALTER TABLE locations
    --     ADD COLUMN IF NOT EXISTS geom geometry(Geometry, 4326)
    --     GENERATED ALWAYS AS (
    --       CASE WHEN geometry IS NOT NULL
    --            THEN ST_SetSRID(ST_GeomFromGeoJSON(geometry::text), 4326)
    --       END
    --     ) STORED;
    --   CREATE INDEX IF NOT EXISTS ix_locations_geom ON locations USING gist (geom);
    --
    -- A point-only fallback, if only lat/lon are ever populated:
    --   ALTER TABLE locations
    --     ADD COLUMN IF NOT EXISTS geom_point geometry(Point, 4326)
    --     GENERATED ALWAYS AS (
    --       CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL
    --            THEN ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)
    --       END
    --     ) STORED;
    --   CREATE INDEX IF NOT EXISTS ix_locations_geom_point
    --     ON locations USING gist (geom_point);

    CREATE INDEX IF NOT EXISTS ix_locations_structure
      ON locations (structure_id) WHERE structure_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS ix_locations_rack
      ON locations (rack_id) WHERE rack_id IS NOT NULL;

    COMMENT ON COLUMN locations.geometry IS
      'GeoJSON geometry. Portable across PostgreSQL and SQLite. Add a '
      'generated PostGIS column over this value rather than replacing it.';
  END IF;
END
$$;

-- ===========================================================================
-- 4. Roles
-- ===========================================================================
-- Grafana should read the database with a role that cannot write to it
-- (SDD 15.2, least privilege). Create it here with a password supplied out of
-- band, then set GRAFANA_PG_USER / GRAFANA_PG_PASSWORD in deploy/.env.
--
-- Left commented because this file is committed and must never contain a
-- credential:
--
--   CREATE ROLE grafana_ro LOGIN PASSWORD 'set-me-out-of-band';
--   GRANT CONNECT ON DATABASE homestead TO grafana_ro;
--   GRANT USAGE ON SCHEMA public TO grafana_ro;
--   GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public
--     GRANT SELECT ON TABLES TO grafana_ro;
