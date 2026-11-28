#!/usr/bin/env bash
# Backup/restore drill: dump SOURCE_URL, restore into a brand-new scratch database, compare every table's row count and the Alembic revision.
# Read-only against the source. Creates and drops ONLY a database whose name starts with "atla_restore_drill_".
#   SOURCE_URL=postgresql://user:pass@host:5432/dbname ADMIN_URL=postgresql://user:pass@host:5432/postgres ./scripts/backup_restore_drill.sh
# Prints timings (your real restore time) and PASS/FAIL. Never prints credentials. Exit code 0 only on PASS.
set -euo pipefail
: "${SOURCE_URL:?set SOURCE_URL (the database to back up)}"
: "${ADMIN_URL:?set ADMIN_URL (a connection that may CREATE/DROP DATABASE, to the postgres database)}"
DRILL_DB="atla_restore_drill_$(date +%s)"
DUMP="$(mktemp -t atla_drill_XXXXXX.dump)"
ADMIN_BASE="${ADMIN_URL%/*}"
DRILL_URL="${ADMIN_BASE}/${DRILL_DB}"
cleanup() { rm -f "$DUMP"; psql "$ADMIN_URL" -qAt -c "DROP DATABASE IF EXISTS ${DRILL_DB}" >/dev/null 2>&1 || true; }
trap cleanup EXIT
[[ "$DRILL_DB" == atla_restore_drill_* ]] || { echo "refusing: bad scratch name"; exit 2; }
counts() { psql "$1" -qAt -c "SELECT string_agg(format('%s=%s', relname, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I', relname), false, true, '')))[1]::text), ',' ORDER BY relname) FROM pg_class WHERE relkind='r' AND relnamespace='public'::regnamespace"; }
rev() { psql "$1" -qAt -c "SELECT version_num FROM alembic_version" 2>/dev/null || echo "none"; }
t0=$(date +%s); pg_dump -Fc --no-owner "$SOURCE_URL" -f "$DUMP"; t1=$(date +%s)
psql "$ADMIN_URL" -qAt -c "CREATE DATABASE ${DRILL_DB}" >/dev/null
pg_restore --no-owner --exit-on-error -d "$DRILL_URL" "$DUMP"; t2=$(date +%s)
A="$(counts "$SOURCE_URL")"; B="$(counts "$DRILL_URL")"; RA="$(rev "$SOURCE_URL")"; RB="$(rev "$DRILL_URL")"
echo "dump: $((t1-t0))s  restore: $((t2-t1))s  size: $(du -h "$DUMP" | cut -f1)  alembic: source=$RA restored=$RB"
if [[ "$A" == "$B" && "$RA" == "$RB" && -n "$A" ]]; then echo "PASS: every table's row count and the Alembic revision match"; else echo "FAIL: restored copy differs from the source"; echo "source:   $A"; echo "restored: $B"; exit 1; fi
