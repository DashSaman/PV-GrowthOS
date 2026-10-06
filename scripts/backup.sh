#!/usr/bin/env bash
# GrowthOS database backup + verification (run on the production host).
# Only touches pv_growth — never Mirza/pvnaive/hajsaman databases.
set -euo pipefail

BACKUP_DIR="${1:-/opt/pv-growth/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
KEEP=14

mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/pv_growth-$STAMP.sql.gz"

: "${PVG_DATABASE_URL:?set PVG_DATABASE_URL (postgresql+psycopg://user:pass@host/db)}"

DB_PART="${PVG_DATABASE_URL#*//}"          # user:pass@host/db
CRED="${DB_PART%%@*}"
HOST_DB="${DB_PART#*@}"
PGUSER="${CRED%%:*}"; PGPASSWORD="${CRED#*:}"
HOSTPORT="${HOST_DB%%/*}"; PGDATABASE="${HOST_DB#*/}"
PGHOST="${HOSTPORT%%:*}"
if [ "$HOSTPORT" = "$PGHOST" ]; then
  PGPORT=5432
else
  PGPORT="${HOSTPORT##*:}"
fi

export PGPASSWORD
pg_dump -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDATABASE" | gzip > "$OUT"
unset PGPASSWORD

# verification: the archive is a non-empty, gzip-valid SQL dump
SIZE=$(stat -c%s "$OUT")
[ "$SIZE" -gt 1000 ] || { echo "backup too small ($SIZE bytes)"; exit 1; }
gzip -t "$OUT"
echo "backup ok: $OUT ($SIZE bytes)"

# rotate: keep newest $KEEP
ls -1t "$BACKUP_DIR"/pv_growth-*.sql.gz | tail -n +$((KEEP + 1)) | xargs -r rm --
echo "rotation: kept newest $KEEP"
