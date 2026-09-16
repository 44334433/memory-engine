#!/usr/bin/env bash
# groonga 源 GPG key 修复（FATAL 根因=key 未导入而非包不存在）
# 步骤：导入 624CF77434839225 → trusted.gpg.d；groonga-org 源 http→https；
#       apt update → apt-cache madison 全量查 PG18/17/16 pgroonga 可用性（留痕定分支）
set -uo pipefail

echo "== [1/4] 导入 groonga 公钥 624CF77434839225 =="
curl -fsS "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x624CF77434839225" -o /tmp/groonga-key.asc || { echo "FATAL: key fetch fail"; exit 1; }
head -1 /tmp/groonga-key.asc
gpg --list-packets /tmp/groonga-key.asc 2>/dev/null | grep -m1 "keyid" || true
gpg --dearmor -o /etc/apt/trusted.gpg.d/groonga.gpg /tmp/groonga-key.asc || { echo "FATAL: dearmor fail"; exit 1; }
chmod 644 /etc/apt/trusted.gpg.d/groonga.gpg

echo "== [2/4] groonga-org 源 http→https（trusted.gpg.d 全局信任，去 signed-by）=="
echo 'deb [arch=amd64] https://packages.groonga.org/ubuntu noble main' > /etc/apt/sources.list.d/groonga-org.list
cat /etc/apt/sources.list.d/groonga-org.list

echo "== [3/4] apt update（只看关键行）=="
apt-get update -o Acquire::Retries=3 2>&1 | grep -E "Err:|W:|groonga|pgdg" | head -15
echo "update exit marker: $?"

echo "== [4/4] madison 全量查 pgroonga 可用性 =="
for v in 18 17 16; do
  echo "--- PG$v ---"
  apt-cache madison "postgresql-$v-pgroonga" "postgresql-$v-pgdg-pgroonga" 2>/dev/null | head -4
done
echo "FIX_DONE"
