#!/usr/bin/env bash
# apply_migrations.sh
# Applies all SQL migrations in correct version order using sort -V.
# Usage: bash scripts/core/apply_migrations.sh
# Requires: DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME, DB_USER set in .env
#
# Why this exists: Docker's /docker-entrypoint-initdb.d runs files in plain
# alphabetical order, which sorts v8.1 before v8.00 and v8.10 before v8.2.
# Use this script instead of relying on Docker auto-init.

set -euo pipefail

# Resolve repo root from the script's own location
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
MIGRATIONS_DIR="$REPO_ROOT/sql/migrations"

# Load only the keys we need — full-file sourcing breaks on values with
# @, $, spaces etc. (bash tries to execute them as commands).
if [[ -f "$ENV_FILE" ]]; then
    for key in DB_PASSWORD DB_HOST DB_PORT DB_NAME DB_USER; do
        val=$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-)
        [[ -n "$val" ]] && export "$key=$val"
    done
fi

export PGPASSWORD="${DB_PASSWORD:?DB_PASSWORD not set in .env}"
HOST="${DB_HOST:-localhost}"
PORT="${DB_PORT:-5432}"
DB="${DB_NAME:?DB_NAME not set in .env}"
USER="${DB_USER:?DB_USER not set in .env}"

echo "Applying migrations in version order to ${DB}@${HOST}:${PORT}..."
for f in $(ls "$MIGRATIONS_DIR"/*.sql | sort -V); do
    echo "  → $(basename "$f")"
    psql -h "$HOST" -p "$PORT" -U "$USER" -d "$DB" -v ON_ERROR_STOP=1 -f "$f"
done
echo "Done."
