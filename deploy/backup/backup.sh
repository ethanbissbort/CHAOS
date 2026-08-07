#!/bin/sh
# Homestead Digital Twin -- backup.
#
# SDD section 16.1 mitigation 3: critical configuration and asset data must be
# replicated OUTSIDE the combined battery/server container. SDD section 15.8 and
# MVP acceptance criterion 5 add that Home Assistant, Node-RED, Grafana and the
# digital twin API are backed up automatically, and criterion 10 that restore
# has actually been tested.
#
# What this script produces, per run, under $BACKUP_DIR/<UTC timestamp>/:
#
#   postgres.dump           pg_dump custom format (registry + historian + audit)
#   twin-registry.tar.gz    `homestead-twin backup`: registry as JSON, redacted
#                           settings, and the machine-readable design package.
#                           Readable without PostgreSQL, which matters when the
#                           thing you are restoring onto is a laptop.
#   mosquitto-config.tar.gz broker config, password file and ACL
#   node-red.tar.gz         Node-RED flows and settings, if NODE_RED_DIR is set
#   home-assistant.tar.gz   Home Assistant config, if HOME_ASSISTANT_DIR is set
#   grafana.tar.gz          Grafana database and provisioning
#   MANIFEST                what ran, what succeeded, sizes and checksums
#   SHA256SUMS              verify with: sha256sum -c SHA256SUMS
#
# Usage:
#   deploy/backup/backup.sh                 # full run
#   BACKUP_DIR=/mnt/usb deploy/backup/backup.sh
#
# Cron (daily 02:15 local):
#   15 2 * * * /srv/homestead-twin/deploy/backup/backup.sh >> /var/log/homestead-backup.log 2>&1
#
# ---------------------------------------------------------------------------
# OFFSITE AND OFFLINE COPIES -- READ THIS
# ---------------------------------------------------------------------------
# A backup written to a disk inside the power container is not a backup. It is
# a second copy inside the same failure domain, and SDD 16.1 exists precisely
# because that domain can be lost whole: fire, smoke, water ingress, electrical
# fault, or container HVAC failure.
#
# Keep three copies:
#   1. Local, on the primary node -- fast restore from operator error.
#   2. Off the container, on the secondary control node in another structure --
#      survives loss of the power container.
#   3. Offline and off-property -- an encrypted disk that is disconnected
#      between runs, or an offsite target. Survives fire, theft, and
#      ransomware, none of which respect an always-mounted network share.
#
# Copies 2 and 3 are NOT automated here on purpose. Doing so needs credentials
# and a target that the design package leaves unresolved (SDD open decision
# 22.9: remote-access and offsite-backup architecture). Set BACKUP_OFFSITE_TARGET
# to enable the rsync step once that decision is made and a key has been issued.
#
# And: a backup you have never restored is a hypothesis. SDD MVP criterion 10
# requires restore to have been tested. See docs/operations.md.

set -eu

# ---------------------------------------------------------------------------
# Configuration (override through the environment or deploy/.env)
# ---------------------------------------------------------------------------
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEPLOY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
REPO_DIR=$(CDPATH= cd -- "$DEPLOY_DIR/.." && pwd)

# shellcheck disable=SC1091  # optional, path is deployment-specific
[ -f "$DEPLOY_DIR/.env" ] && . "$DEPLOY_DIR/.env"

BACKUP_DIR=${BACKUP_DIR:-/var/backups/homestead}
BACKUP_KEEP_DAYS=${BACKUP_KEEP_DAYS:-30}
COMPOSE_FILE=${COMPOSE_FILE:-$DEPLOY_DIR/docker-compose.yml}
COMPOSE=${COMPOSE:-docker compose -f $COMPOSE_FILE}
POSTGRES_USER=${POSTGRES_USER:-homestead}
POSTGRES_DB=${POSTGRES_DB:-homestead}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TARGET="$BACKUP_DIR/$STAMP"
MANIFEST="$TARGET/MANIFEST"

FAILURES=0

log()  { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
note() { printf '%s\n' "$*" >> "$MANIFEST"; }
fail() { FAILURES=$((FAILURES + 1)); log "FAILED: $*"; note "FAILED: $*"; }

mkdir -p "$TARGET"
: > "$MANIFEST"

note "Homestead Digital Twin backup"
note "started_at   : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
note "host         : $(hostname)"
note "repo         : $REPO_DIR"
note "compose_file : $COMPOSE_FILE"
note "node_role    : ${HOMESTEAD_NODE_ROLE:-primary}"
note ""

# ---------------------------------------------------------------------------
# 1. PostgreSQL -- registry, historian, alarms, command audit
# ---------------------------------------------------------------------------
# Custom format (-Fc) so pg_restore can select individual tables. A plain SQL
# dump is easier to read but cannot be restored selectively, and selective
# restore is what you want at 3am when one table is wrong.
log "Dumping PostgreSQL..."
if $COMPOSE exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
       --format=custom --compress=6 --no-owner --no-privileges \
       > "$TARGET/postgres.dump" 2> "$TARGET/postgres.err"; then
  note "postgres.dump: $(wc -c < "$TARGET/postgres.dump") bytes"
  rm -f "$TARGET/postgres.err"
else
  fail "pg_dump (see postgres.err)"
  rm -f "$TARGET/postgres.dump"
fi

# ---------------------------------------------------------------------------
# 2. Registry + configuration, in an open format
# ---------------------------------------------------------------------------
# This is the SDD 16.1 mitigation-3 artefact. It restores without PostgreSQL,
# without Docker and without this repository: JSON tables plus the design
# package. It contains no credentials.
log "Exporting registry and configuration..."
if $COMPOSE exec -T twin homestead-twin backup --output /app/var/backups \
     > "$TARGET/twin-backup.log" 2>&1; then
  LATEST=$($COMPOSE exec -T twin sh -c 'ls -1t /app/var/backups/*.tar.gz 2>/dev/null | head -1' | tr -d '\r')
  if [ -n "$LATEST" ]; then
    $COMPOSE exec -T twin cat "$LATEST" > "$TARGET/twin-registry.tar.gz"
    note "twin-registry.tar.gz: $(wc -c < "$TARGET/twin-registry.tar.gz") bytes (from $LATEST)"
  else
    fail "homestead-twin backup produced no archive"
  fi
else
  fail "homestead-twin backup (see twin-backup.log)"
fi

# ---------------------------------------------------------------------------
# 3. Mosquitto -- config, per-device credentials, ACL
# ---------------------------------------------------------------------------
# The password file holds hashes and the ACL names every device identity.
# Treat this archive as a secret: chmod 600 below, and encrypt it before it
# leaves the property (SDD 15.2).
log "Archiving Mosquitto configuration..."
if $COMPOSE exec -T mosquitto tar -czf - -C /mosquitto config \
     > "$TARGET/mosquitto-config.tar.gz" 2>/dev/null; then
  chmod 600 "$TARGET/mosquitto-config.tar.gz"
  note "mosquitto-config.tar.gz: $(wc -c < "$TARGET/mosquitto-config.tar.gz") bytes (SENSITIVE)"
else
  fail "mosquitto config archive"
fi

# ---------------------------------------------------------------------------
# 4. Grafana -- dashboards, users, datasource state
# ---------------------------------------------------------------------------
log "Archiving Grafana..."
if $COMPOSE exec -T grafana tar -czf - -C /var/lib grafana \
     > "$TARGET/grafana.tar.gz" 2>/dev/null; then
  note "grafana.tar.gz: $(wc -c < "$TARGET/grafana.tar.gz") bytes"
else
  fail "grafana archive"
fi

# ---------------------------------------------------------------------------
# 5. Node-RED and Home Assistant
# ---------------------------------------------------------------------------
# Named in SDD 8.3/8.4 and MVP criterion 5, but NOT part of this compose stack
# (see docs/architecture.md -- both are "not built yet" here). Set NODE_RED_DIR
# and HOME_ASSISTANT_DIR in deploy/.env once they are deployed on this host.
#
# Node-RED flows must also be exported to Git, reviewed and tagged (SDD 8.4);
# a tarball is disaster recovery, not change control.
archive_dir() {
  label=$1
  path=$2
  if [ -z "${path:-}" ]; then
    note "$label: not configured (skipped)"
    return 0
  fi
  if [ ! -d "$path" ]; then
    fail "$label: configured path does not exist: $path"
    return 0
  fi
  if tar -czf "$TARGET/$label.tar.gz" -C "$(dirname "$path")" "$(basename "$path")" 2>/dev/null; then
    chmod 600 "$TARGET/$label.tar.gz"
    note "$label.tar.gz: $(wc -c < "$TARGET/$label.tar.gz") bytes (from $path)"
  else
    fail "$label archive from $path"
  fi
}

log "Archiving Node-RED and Home Assistant if configured..."
archive_dir node-red "${NODE_RED_DIR:-}"
archive_dir home-assistant "${HOME_ASSISTANT_DIR:-}"

# ---------------------------------------------------------------------------
# 6. Deployment configuration from the repository
# ---------------------------------------------------------------------------
# deploy/.env is EXCLUDED. It holds live secrets, and a backup set that quietly
# contains every credential on the property is a liability, not an asset.
# Restore procedure re-issues credentials rather than restoring them.
log "Archiving deployment configuration..."
if tar -czf "$TARGET/deploy-config.tar.gz" \
     -C "$REPO_DIR" \
     --exclude='deploy/.env' \
     deploy data schemas tools 2>/dev/null; then
  note "deploy-config.tar.gz: $(wc -c < "$TARGET/deploy-config.tar.gz") bytes (deploy/.env excluded)"
else
  fail "deploy config archive"
fi

# ---------------------------------------------------------------------------
# 7. Checksums and manifest
# ---------------------------------------------------------------------------
( cd "$TARGET" && sha256sum ./*.tar.gz ./*.dump > SHA256SUMS 2>/dev/null ) || true

note ""
note "finished_at  : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
note "failures     : $FAILURES"
note ""
note "Verify:  sha256sum -c SHA256SUMS"
note "Restore: see docs/operations.md"

chmod 700 "$TARGET"

# ---------------------------------------------------------------------------
# 8. Retention of backup sets
# ---------------------------------------------------------------------------
# Local only. Never prune the offsite copy from the machine being backed up:
# a compromised or confused primary node must not be able to delete the only
# surviving copy.
if [ "$BACKUP_KEEP_DAYS" -gt 0 ] 2>/dev/null; then
  log "Pruning local sets older than $BACKUP_KEEP_DAYS days..."
  find "$BACKUP_DIR" -maxdepth 1 -mindepth 1 -type d -mtime "+$BACKUP_KEEP_DAYS" \
    -exec rm -rf {} + 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 9. Offsite copy (opt-in)
# ---------------------------------------------------------------------------
if [ -n "${BACKUP_OFFSITE_TARGET:-}" ]; then
  log "Copying to offsite target..."
  if rsync -a --partial "$TARGET" "$BACKUP_OFFSITE_TARGET/"; then
    note "offsite: $BACKUP_OFFSITE_TARGET"
  else
    fail "offsite rsync to $BACKUP_OFFSITE_TARGET"
  fi
else
  log "No BACKUP_OFFSITE_TARGET set -- this run exists only on this host."
  note "offsite: NOT CONFIGURED"
fi

# Optional: emit a Prometheus textfile metric for the backup job
# (deploy/prometheus/prometheus.yml, 'backup' job).
if [ -n "${BACKUP_TEXTFILE_DIR:-}" ] && [ -d "$BACKUP_TEXTFILE_DIR" ]; then
  {
    echo "# HELP homestead_backup_last_success_timestamp_seconds Unix time of the last successful backup."
    echo "# TYPE homestead_backup_last_success_timestamp_seconds gauge"
    if [ "$FAILURES" -eq 0 ]; then echo "homestead_backup_last_success_timestamp_seconds $(date -u +%s)"; fi
    echo "# HELP homestead_backup_failures Number of failed steps in the last backup run."
    echo "# TYPE homestead_backup_failures gauge"
    echo "homestead_backup_failures $FAILURES"
  } > "$BACKUP_TEXTFILE_DIR/homestead_backup.prom.$$"
  mv "$BACKUP_TEXTFILE_DIR/homestead_backup.prom.$$" "$BACKUP_TEXTFILE_DIR/homestead_backup.prom"
fi

log "Backup set: $TARGET"
if [ "$FAILURES" -ne 0 ]; then
  log "$FAILURES step(s) failed -- see $MANIFEST"
  exit 1
fi
log "All steps succeeded."
exit 0
