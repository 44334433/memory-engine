#!/usr/bin/env bash
# 生产仓 → 公开仓镜像同步闸（唯一同步入口）。
#
# 三道闸，任一失败即中止且不推：
#   闸0  公开仓工作树必须干净（不覆盖对方在飞工作）；无参全量模式下生产仓也必须干净
#   闸1  rsync 覆盖共享子树（永不 --delete：sdk/.github/根文档是公开仓独有，镜像是叠加非替换）
#   闸2  冒烟 import memory_engine.app —— 2026-09-20 教训：公开仓缺 RERANK 常量曾让 daemon
#        启动即 AttributeError crash（73b4844），此后「同步过去先能 import」是硬闸。
#        失败则回滚本次同步路径（checkout+clean）并 exit 1。
#
# 推送永远是人的动作：本脚本不 commit 不 push；通过后人工审阅
#   git -C <pub> diff → commit → push（CI 自动触发）。
#
# 用法：
#   scripts/sync_public.sh                 # 全量共享子树同步（要求两仓工作树均干净）
#   scripts/sync_public.sh src tests/xxx.py # 手术模式：只同步显式列出的路径（容忍他人在飞）
set -euo pipefail

PROD="${MEMORY_ENGINE_REPO:-$HOME/.hermes/memory-engine}"
PUB="${MEMORY_ENGINE_PUBLIC_REPO:-$HOME/projects/memory-engine-public}"
SMOKE_PY="${SMOKE_PYTHON:-/usr/bin/python3.12}"   # 需已装 fastapi/pydantic（CI 同款依赖）
SHARED=(src tests scripts deploy eval integrations schema.sql)   # README 不在内：公开仓 README 是手写对外文档，非镜像内容
EXCL=(--exclude "__pycache__/" --exclude "*.pyc" --exclude "*.bak-*"
      --exclude ".venv/" --exclude "provider.json" --exclude "*.jsonl"
      --exclude ".env" --exclude "data/")

[ -d "$PROD/.git" ] && [ -d "$PUB/.git" ] || { echo "FATAL: 仓库路径缺失: $PROD | $PUB"; exit 1; }

# —— 闸 0 ——
# 全量模式：两仓必须干净。手术模式：公开仓在飞允许，但被同步路径本身必须干净（防覆盖）。
if [ $# -eq 0 ]; then
  git -C "$PUB" diff --quiet || { echo "FATAL: 公开仓有未提交改动（全量模式要求干净，或用手术模式）"; exit 1; }
  git -C "$PROD" diff --quiet || {
    echo "FATAL: 生产仓工作树不干净（可能有他人在飞 WIP）。";
    echo "  → 提交后重试全量模式，或用手术模式显式列路径：scripts/sync_public.sh <path> ...";
    exit 1; }
  PATHS=("${SHARED[@]}")
else
  PATHS=("$@")
  git -C "$PUB" diff --quiet -- "${PATHS[@]}" || {
    echo "FATAL: 公开仓的以下目标路径有未提交改动，拒绝覆盖："; git -C "$PUB" diff --name-only -- "${PATHS[@]}"; exit 1; }
fi

rollback() {
  echo "== 冒烟失败：回滚本次同步路径 =="
  for p in "${PATHS[@]}"; do
    git -C "$PUB" checkout -- "$p" 2>/dev/null || true
    git -C "$PUB" clean -fdq "$p" 2>/dev/null || true
  done
  exit 1
}

# —— 闸 1：rsync ——
for p in "${PATHS[@]}"; do
  if [ -e "$PROD/$p" ]; then
    rsync -a "${EXCL[@]}" "$PROD/$p" "$PUB/$(dirname "$p" | sed 's|^\.$||;s|/$||')"
  fi
done

# —— 闸 2：import 冒烟（公开树自身 PYTHONPATH=src）——
if ! (cd "$PUB" && PYTHONPATH=src "$SMOKE_PY" -c "import memory_engine.app" 2>/tmp/sync_public_smoke.err); then
  echo "FATAL: 冒烟 import memory_engine.app 失败——"
  tail -5 /tmp/sync_public_smoke.err
  rollback
fi

echo "== 同步完成 + 冒烟绿（import memory_engine.app OK, py=$SMOKE_PY）=="
git -C "$PUB" status --short
echo "下一步（人工）：git -C $PUB diff 审阅 → commit → push"
