#!/usr/bin/env bash
# Reset the development database to a clean schema at head.
#
#   scripts/dev_db.sh              # drop + recreate public schema, migrate to head
#   scripts/dev_db.sh --migrate    # migrate only, keep existing data
#
# DESTRUCTIVE by default: every table, row, and enum type in schema `public`
# goes away. Development only — never point DATABASE_URL at anything you care
# about and run this.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

PYTHON="$ROOT/.venv/bin/python"
ALEMBIC="$ROOT/.venv/bin/alembic"
PSQL="${PSQL:-/usr/local/opt/postgresql@17/bin/psql}"

[ -x "$PYTHON" ]  || { echo "error: no virtualenv at $ROOT/.venv" >&2; exit 1; }
[ -x "$ALEMBIC" ] || { echo "error: alembic not installed in $ROOT/.venv" >&2; exit 1; }

# Ask the app for the URL so this script and the app can never disagree.
DB_URL="$("$PYTHON" -c 'from mazz.config import get_settings; print(get_settings().database_url)')"
# psql does not understand the SQLAlchemy "+psycopg" driver suffix.
PSQL_URL="${DB_URL/+psycopg/}"

echo "database: $PSQL_URL"

if [ "${1:-}" != "--migrate" ]; then
  [ -x "$PSQL" ] || { echo "error: psql not found at $PSQL (override with PSQL=...)" >&2; exit 1; }
  echo "--> dropping and recreating schema public"
  "$PSQL" "$PSQL_URL" -v ON_ERROR_STOP=1 -q -c 'DROP SCHEMA IF EXISTS public CASCADE;'
  "$PSQL" "$PSQL_URL" -v ON_ERROR_STOP=1 -q -c 'CREATE SCHEMA public;'
fi

echo "--> alembic upgrade head"
"$ALEMBIC" upgrade head

echo "--> tables now present"
if [ -x "$PSQL" ]; then
  "$PSQL" "$PSQL_URL" -c '\dt'
fi

echo "done."
