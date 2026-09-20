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
  # -E UTF8 必须显式指定：minimal ubuntu:noble 无生成 locale，initdb 默认模板给出
  # SQL_ASCII 集群 → psycopg 对任何非 ASCII 查询直接 UnicodeEncodeError（CI 中文语料必炸）。
  runuser -u "$PGUSER" -- "$PGBIN/initdb" -D "$PGDATA" -A trust -E UTF8
  runuser -u "$PGUSER" -- "$PGBIN/pg_ctl" -D "$PGDATA" -w start \
    -o "-c listen_addresses=127.0.0.1 -c unix_socket_directories=$FSOCK"
  "$PGBIN/psql" -h 127.0.0.1 -U "$PGUSER" -d template1 -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgroonga;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pg_prewarm;
SQL
  "$PGBIN/psql" -h 127.0.0.1 -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT 'CREATE DATABASE $TARGET_DB'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='$TARGET_DB')\gexec
SQL
  runuser -u "$PGUSER" -- "$PGBIN/pg_ctl" -D "$PGDATA" -m fast -w stop
  echo "pg-entrypoint: init done (extensions seeded into template1, db=$TARGET_DB)"
fi

# CI 容器经 docker -p 端口映射访问，来源是网桥网关 IP（172.17.0.1）；initdb -A trust
# 只写入了 127.0.0.1/32 与 ::1/128 两条 host 规则 → 宿主 psql/daemon 会 "no pg_hba entry"。
# 一次性镜像场景全放行（trust 同 initdb -A，无额外暴露面）。
grep -q '^host all all all trust' "$PGDATA/pg_hba.conf" 2>/dev/null || \
  echo 'host all all all trust' >> "$PGDATA/pg_hba.conf"

exec runuser -u "$PGUSER" -- "$PGBIN/postgres" -D "$PGDATA" \
  -c "listen_addresses=*" -p 5432
