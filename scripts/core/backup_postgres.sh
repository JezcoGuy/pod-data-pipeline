#!/usr/bin/env bash
# backup_postgres.sh — nightly your_db_name dump
# ----------------------------------------------
# Plain-SQL pg_dump piped through gzip → /opt/your_brand_id/backups/postgres/.
# Postgres runs inside the your_brand_id_postgres container; pg_dump is invoked
# via `docker exec` (the host has no postgres-client package).
#
# Format note: this is plain SQL (`-Fp` implicit), not custom format, so
# restore is `gunzip -c file.sql.gz | psql ...`. `pg_restore --list` does
# NOT work on this format — verify integrity with `gunzip -t` instead.
#
# Retention: keeps the newest 30 daily files by filename date (not mtime,
# which can drift). Older files are deleted unconditionally.
#
# Failure path: any non-zero exit from the dump or the gzip pipe triggers
# an SMTP alert via send_alert.py and a non-zero script exit. The cron
# `>> logs/backup.log 2>&1` redirect captures everything either way.

set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
ENV_FILE="/opt/your_brand_id/.env"
BACKUP_DIR="/opt/your_brand_id/backups/postgres"
LOG_FILE="/opt/your_brand_id/logs/backup.log"
ALERT_SCRIPT="/opt/your_brand_id/scripts/send_alert.py"
CONTAINER="your_brand_id_postgres"
DB_NAME="your_db_name"
DB_USER="your_db_user"
RETENTION_DAYS=30

STAMP=$(date +%F)                                           # e.g. 2026-06-13
FINAL="${BACKUP_DIR}/your_brand_id_${STAMP}.sql.gz"
PARTIAL="${FINAL}.partial"

# ─── Logging helper ──────────────────────────────────────────────────────────
log() {
  # Stamp every line. Tee so cron's >> redirect AND a direct run both work.
  echo "$(date '+%Y-%m-%d %H:%M:%S') [backup_postgres] $*" | tee -a "$LOG_FILE"
}

alert() {
  # Best-effort SMTP alert. If the alert itself fails, just log it — don't
  # loop, don't fail the script a second time.
  local subject="$1" body="$2"
  if ! python3 "$ALERT_SCRIPT" "$subject" "$body" >>"$LOG_FILE" 2>&1; then
    log "WARNING: send_alert.py failed — original failure stands"
  fi
}

# ─── Trap any unexpected error ───────────────────────────────────────────────
on_error() {
  local rc=$? line=$1
  log "FAIL: line ${line} exited ${rc} — sending alert"
  alert "Postgres backup FAILED" \
        "backup_postgres.sh exited ${rc} at line ${line} on $(hostname).
Tail of ${LOG_FILE}:
$(tail -30 ${LOG_FILE} 2>/dev/null || echo '(log unreadable)')"
  rm -f "$PARTIAL"
  exit "$rc"
}
trap 'on_error $LINENO' ERR

# ─── Extract DB_PASSWORD from .env ───────────────────────────────────────────
# Targeted grep instead of full-file sourcing (`set -a; . file`): unrelated
# values in .env can contain `@`, `$`, spaces etc. that bash would mis-parse
# as commands. Reads only the one key we need.
DB_PASSWORD=$(grep -E '^DB_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2-)
: "${DB_PASSWORD:?DB_PASSWORD not found in ${ENV_FILE}}"

log "Starting dump of ${DB_NAME} → ${FINAL}"

# ─── Dump ────────────────────────────────────────────────────────────────────
# pipefail (above) ensures a pg_dump failure surfaces even though gzip
# would otherwise mask it with exit 0.
docker exec -e PGPASSWORD="$DB_PASSWORD" "$CONTAINER" \
    pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --no-privileges \
  | gzip -9 > "$PARTIAL"

# Sanity-check the gzip integrity before promoting .partial → final.
gunzip -t "$PARTIAL"
mv "$PARTIAL" "$FINAL"

SIZE=$(du -h "$FINAL" | cut -f1)
log "OK: wrote ${FINAL} (${SIZE})"

# ─── Retention sweep ─────────────────────────────────────────────────────────
# Sort by filename (YYYY-MM-DD sorts chronologically), keep newest N.
mapfile -t ALL < <(ls -1 "${BACKUP_DIR}"/your_brand_id_*.sql.gz 2>/dev/null | sort)
KEEP=$(( ${#ALL[@]} > RETENTION_DAYS ? RETENTION_DAYS : ${#ALL[@]} ))
DELETE_COUNT=$(( ${#ALL[@]} - KEEP ))

if (( DELETE_COUNT > 0 )); then
  for f in "${ALL[@]:0:$DELETE_COUNT}"; do
    rm -f "$f"
    log "Pruned ${f}"
  done
fi

log "Retention: kept $(ls -1 "${BACKUP_DIR}"/your_brand_id_*.sql.gz | wc -l) file(s), max=${RETENTION_DAYS}"
log "Done"
