#!/usr/bin/env bash
# Minimal PG18+pgvector+pgroonga entrypoint for CI.
# Extensions are created in template1 so every database (incl. the app DB) inherits them.
set -euo pipefail

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
PGBIN=/usr/lib/postgresql/18/bin
export PATH="$PGBIN:$PATH"
FSOCK=/var/run/postgresql
PGUSER=postgres
TARGET_DB="${POSTGRES_DB:-memengine}"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "pg-entrypoint: initdb"
  runuser -u "$PGUSER" -- "$PGBIN/initdb" -D "$PGDATA" -A trust
  runuser -u "$PGUSER" -- "$PGBIN/pg_ctl" -D "$PGDATA" -w start \
    -o "-c listen_addresses=127.0.0.1 -c unix_socket_directories=$FSOCK"
  "$PGBIN/psql" -h 127.0.0.1 -U "$PGUSER" -d template1 -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgroonga;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pg_prewarm;
SQL
  "$PGBIN/psql" -h 127.0.0.1 -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT 'CREATE DATABASE $TARGET_DB' \
        WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='$TARGET_DB')\gexec"
  runuser -u "$PGUSER" -- "$PGBIN/pg_ctl" -D "$PGDATA" -m fast -w stop
  echo "pg-entrypoint: init done (extensions seeded into template1, db=$TARGET_DB)"
fi

exec runuser -u "$PGUSER" -- "$PGBIN/postgres" -D "$PGDATA" \
  -c "listen_addresses=*" -p 5432
