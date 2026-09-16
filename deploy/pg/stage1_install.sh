#!/usr/bin/env bash
# memory-engine 阶段1 · PG 专用实例安装脚本（root 执行）
# 依据：设计文档回退链（apt+systemd 部署 / WAL 归档开启 / 专用实例端口 5433）
# 形态：PG18 专用 cluster=memengine @5433；不建 main 集群（避免与 5432 既有实例冲突）
set -uo pipefail
exec 2>&1

# WAL/备份归档根目录：异盘优先（挂载点路径经此注入），缺省本地兜底
WAL_ARCHIVE_ROOT="${MEMORY_ENGINE_WAL_ARCHIVE_ROOT:-/var/lib/postgresql/18/memengine/wal_archive_ext}"
PG_ARCHIVE_ROOT="${MEMORY_ENGINE_PG_ARCHIVE_ROOT:-/var/lib/postgresql/18/memengine/pg_archive_ext}"

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY 2>/dev/null || true
export DEBIAN_FRONTEND=noninteractive

echo "== [1/8] PGDG 仓库 =="
install -d /usr/share/postgresql-common/pgdg /etc/apt/keyrings
curl -fsS -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main' > /etc/apt/sources.list.d/pgdg.list
cat /etc/apt/sources.list.d/pgdg.list

echo "== [2/8] groonga PPA（keyring 手工导入，避免依赖 software-properties-common）=="
FP=$(curl -fsS https://api.launchpad.net/1.0/~groonga/+archive/ubuntu/ppa | python3 -c "import json,sys;print(json.load(sys.stdin)['signing_key_fingerprint'])")
echo "PPA fingerprint: $FP"
curl -fsS "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${FP}" | gpg --dearmor -o /etc/apt/keyrings/groonga-ppa.gpg
echo 'deb [signed-by=/etc/apt/keyrings/groonga-ppa.gpg] https://ppa.launchpadcontent.net/groonga/ppa/ubuntu noble main' > /etc/apt/sources.list.d/groonga-ppa.list

echo "== [3/8] 阻止 postgresql-common 自动建 main 集群（专用实例独立端口）=="
mkdir -p /etc/postgresql-common
echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.conf

echo "== [4/8] apt update =="
apt-get update -o Acquire::Retries=3 || echo "WARN: apt update 部分源失败（第三方源可能超时，继续）"

echo "== [5/8] 安装 postgresql-18 + pgvector =="
apt-get install -y --no-install-recommends postgresql-18 postgresql-client-18 postgresql-18-pgvector || { echo "FATAL: postgresql-18 安装失败"; exit 1; }

echo "== [6/8] 安装 PGroonga（PPA → packages.groonga.org 回退）=="
PGV=$(apt-cache search --names-only '^postgresql-18.*pgroonga$' 2>/dev/null | awk '{print $1}' | sort | head -1)
echo "pgroonga candidate: ${PGV:-NONE}"
if [ -z "${PGV}" ]; then
  echo "-- 回退源 packages.groonga.org（组件=universe，PGDG 构建 postgresql-<ver>-pgdg-pgroonga）--"
  curl -fsS -o /usr/share/keyrings/groonga-archive-keyring.gpg https://packages.groonga.org/ubuntu/groonga-archive-keyring.gpg || true
  echo 'deb [arch=amd64] https://packages.groonga.org/ubuntu noble universe' > /etc/apt/sources.list.d/groonga-org.list
  apt-get update -o Acquire::Retries=3 || true
  PGV=$(apt-cache search --names-only '^postgresql-18.*pgroonga$' 2>/dev/null | awk '{print $1}' | sort | head -1)
  echo "pgroonga candidate (fallback): ${PGV:-NONE}"
fi
if [ -n "${PGV}" ]; then
  apt-get install -y --no-install-recommends "${PGV}" || { echo "FATAL: pgroonga 安装失败"; exit 1; }
else
  echo "FATAL: 两个源均无 postgresql-18 pgroonga 包"; exit 1
fi

echo "== [7/8] 建专用集群 memengine @5433 =="
pg_lsclusters -h || true
pg_dropcluster 18 memengine --stop 2>/dev/null || true   # 清理上次 initdb 失败残留（幂等）
if ! pg_lsclusters -h 2>/dev/null | grep -q 'memengine'; then
  # 注意：--port 是 pg_createcluster 自身选项，须在 -- 之前（initdb 不认 --port）
  pg_createcluster 18 memengine --port=5433 || { echo "FATAL: pg_createcluster 失败"; exit 1; }
fi
CLCONF=/etc/postgresql/18/memengine/conf.d
mkdir -p "$CLCONF"
cat > "$CLCONF/memory-engine.conf" <<'EOF'
# memory-engine 专用实例（蓝图 §8.1；WAL 归档 2026-09-16 拍板开启，覆盖蓝图 archive_mode=off）
port = 5433
listen_addresses = '127.0.0.1'
shared_buffers = 512MB
effective_cache_size = 2GB
max_connections = 20
archive_mode = on
archive_timeout = 300
archive_command = 'test -d ${WAL_ARCHIVE_ROOT} && test ! -f ${WAL_ARCHIVE_ROOT}/%f && cp %p ${WAL_ARCHIVE_ROOT}/%f || cp %p /var/lib/postgresql/18/memengine/wal_archive_local/%f'
EOF
HBA=/etc/postgresql/18/memengine/pg_hba.conf
if ! grep -q 'memory-engine rules' "$HBA"; then
  cp -n "$HBA" "$HBA.bak-orig" || true
  { printf '# memory-engine rules 2026-09-16（蓝图：unix socket peer + 回环 trust，无密码面；仅 127.0.0.1 监听）\nlocal   memengine  memengine  peer\nhost    memengine  memengine  127.0.0.1/32  trust\nhost    memengine  memengine  ::1/128       trust\n\n'; cat "$HBA"; } > "${HBA}.new" && mv "${HBA}.new" "$HBA"
fi
# 归档目录：异盘优先 + 本地兜底（异盘缺失时 archiver 不卡死）
install -d -o postgres -g postgres "$WAL_ARCHIVE_ROOT"
install -d -o postgres -g postgres "$PG_ARCHIVE_ROOT"
install -d -o postgres -g postgres /var/lib/postgresql/18/memengine/wal_archive_local
pg_ctlcluster 18 memengine restart || { echo "FATAL: 集群启动失败"; journalctl -u postgresql@18-memengine --no-pager | tail -20; exit 1; }

echo "== [8/8] 角色/库/扩展/验证 =="
su - postgres -c "psql -p 5433 -tAc \"SELECT 1 FROM pg_roles WHERE rolname='memengine'\"" | grep -q 1 || \
  su - postgres -c "psql -p 5433 -c \"CREATE ROLE memengine LOGIN;\""
su - postgres -c "psql -p 5433 -tAc \"SELECT 1 FROM pg_database WHERE datname='memengine'\"" | grep -q 1 || \
  su - postgres -c "psql -p 5433 -c \"CREATE DATABASE memengine OWNER memengine;\""
su - postgres -c "psql -p 5433 -d memengine -v ON_ERROR_STOP=1" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgroonga;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pg_prewarm;
SQL
echo "-- 扩展清单 --"
psql -h 127.0.0.1 -p 5433 -U memengine -d memengine -c "SELECT extname, extversion FROM pg_extension ORDER BY 1;"
echo "-- WAL 归档自检（pg_switch_wal 后 5 秒查异盘）--"
psql -h 127.0.0.1 -p 5433 -U memengine -d memengine -c "SELECT pg_switch_wal();" >/dev/null
sleep 5
ls -la "$WAL_ARCHIVE_ROOT" | tail -3
echo "-- 集群状态 --"
pg_lsclusters
pg_isready -h 127.0.0.1 -p 5433
echo "STAGE1_INSTALL_DONE"
