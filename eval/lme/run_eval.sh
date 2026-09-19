#!/usr/bin/env bash
# LME 评测一键复现（外部反馈短板#3：评估可复现）
#
# 链路：docker compose 起 PG18+pgvector+PGroonga → schema+migrations → 起引擎 daemon →
#       （缺料时）建语料 → 灌 turns → 500 题全量检索评测 → 出 R@5；--with-qa 追加端到端 QA 子样本。
# 结果与口径文件同目录（lme_retrieval_results.json 等），逐题结果已入库可离线核对。
#
# 用法：
#   bash eval/lme/run_eval.sh                # 全链路
#   bash eval/lme/run_eval.sh --dry-run      # 只打印步骤计划（CI 冒烟/评审用，零副作用）
#   bash eval/lme/run_eval.sh --with-qa      # 检索评测后追加 QA（需 LME_LLM 指向 OpenAI 兼容端点）
#
# 环境变量：
#   ENGINE_PORT=8767              引擎评测实例端口（与生产 8766 隔离；eval 脚本 LME_ENGINE 同源）
#   PG_PORT=5433                  docker compose 映射端口
#   LME_LLM=http://127.0.0.1:8769/v1/chat/completions     QA 生成+判定端点（本地 llama-server 同构）
#   LME_SCORER_SRC=…              官方 LongMemEval 仓 src/retrieval 路径（官方计分函数零重实现）
#   PYTHON=python3                需含 psycopg（migrate runner）+ numpy + rank_bm25（BM25 基线）
#   MEMORY_ENGINE_EMBED_DEVICE    嵌入设备（生产默认 cuda；无 GPU 机器设 cpu——注意本机 CPU 抢占教训：
#                                 建议 OMP_NUM_THREADS=4 + taskset 限核，见 README）
#
# 前提：docker + compose 插件；HF 可下载 LongMemEval 数据集（仅缺 turns.jsonl 时需要）。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LME_DIR="$ROOT/eval/lme"
ENGINE_PORT="${ENGINE_PORT:-8767}"
PG_PORT="${PG_PORT:-5433}"
PYTHON="${PYTHON:-python3}"
PG_DSN="postgresql://memengine@127.0.0.1:${PG_PORT}/memengine?sslmode=disable"
ENGINE_URL="http://127.0.0.1:${ENGINE_PORT}"
DRY=0; WITH_QA=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --with-qa) WITH_QA=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $arg（--dry-run / --with-qa）" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;36m[eval] %s\033[0m\n' "$*"; }
fail() { echo "[eval][FAIL] $*" >&2; exit 1; }
# 统一步骤执行器：dry-run 只打印，不执行
run() {
  if [ "$DRY" = 1 ]; then printf '  (dry-run) %s\n' "$*"; return 0; fi
  "$@" || fail "步骤失败: $*"
}
# 带 env 前缀执行（run 不支持 env 内联，单独包装）
runenv() { # runenv KEY=VAL… -- CMD…
  local envs=(); while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  if [ "$DRY" = 1 ]; then printf '  (dry-run) env %s %s\n' "${envs[*]}" "$*"; return 0; fi
  env "${envs[@]}" "$@" || fail "步骤失败: $*"
}

cleanup() {
  if [ -n "${SERVE_PID:-}" ] && kill -0 "$SERVE_PID" 2>/dev/null; then
    kill "$SERVE_PID" 2>/dev/null || true
    echo "[eval] 引擎 daemon(pid=$SERVE_PID) 已停；数据卷保留（销毁：docker compose -f $ROOT/docker-compose.yml down -v）"
  fi
}
trap cleanup EXIT

say "0/6 预检"
command -v docker >/dev/null || fail "需要 docker（compose 起 PG）"
docker compose version >/dev/null 2>&1 || docker-compose --version >/dev/null 2>&1 || fail "需要 compose 插件"
command -v "$PYTHON" >/dev/null || fail "找不到 $PYTHON（或设 PYTHON=…）"
if [ "$DRY" = 0 ]; then
  "$PYTHON" -c "import psycopg" 2>/dev/null || fail "$PYTHON 缺 psycopg（pip install 'psycopg[binary]'）"
  "$PYTHON" -c "import numpy, rank_bm25" 2>/dev/null || fail "$PYTHON 缺 numpy/rank_bm25（BM25 基线依赖）"
fi
[ -n "${LME_SCORER_SRC:-}" ] || LME_SCORER_SRC="$HOME/LongMemEval/src/retrieval"
if [ "$DRY" = 0 ] && [ ! -f "$LME_SCORER_SRC/eval_utils.py" ]; then
  fail "官方计分库缺失：clone https://github.com/xiaowu0162/LongMemEval 后设 LME_SCORER_SRC=<path>/src/retrieval"
fi

say "1/6 docker compose 起 PG（pgvector+PGroonga，映射 127.0.0.1:$PG_PORT）"
run docker compose -f "$ROOT/docker-compose.yml" up -d db
if [ "$DRY" = 0 ]; then
  for i in $(seq 1 60); do
    if [ "$(docker inspect --format '{{.State.Health.Status }}' memory-engine-db 2>/dev/null)" = healthy ]; then break; fi
    sleep 2
    [ "$i" = 60 ] && fail "PG 未就绪（首跑需构建镜像，可先手动 docker compose build db）"
  done
  echo "  pg healthy"
fi

say "2/6 schema + migrations（幂等，可重跑）"
if [ "$DRY" = 1 ]; then
  echo "  (dry-run) docker compose exec -T db psql -U memengine -d memengine -f - < schema.sql"
  echo "  (dry-run) env MEMORY_ENGINE_PG_DSN=$PG_DSN $PYTHON scripts/migrate.py --apply"
else
  docker compose -f "$ROOT/docker-compose.yml" exec -T db \
    psql -U memengine -d memengine -v ON_ERROR_STOP=1 -q < "$ROOT/schema.sql" \
    || fail "schema.sql 应用失败"
  MEMORY_ENGINE_PG_DSN="$PG_DSN" "$PYTHON" "$ROOT/scripts/migrate.py" --apply \
    || fail "migrations 应用失败"
fi

say "3/6 起引擎评测实例（端口 $ENGINE_PORT，与生产隔离；日志 /tmp/lme-eval-engine.log）"
if [ "$DRY" = 1 ]; then
  printf '  (dry-run) env MEMORY_ENGINE_PORT=%s MEMORY_ENGINE_PG_DSN=%s PYTHONPATH=src %s -m memory_engine.cli serve &\n' \
         "$ENGINE_PORT" "$PG_DSN" "$PYTHON"
else
  ( cd "$ROOT" && env MEMORY_ENGINE_PORT="$ENGINE_PORT" MEMORY_ENGINE_PG_DSN="$PG_DSN" \
      PYTHONPATH=src "$PYTHON" -m memory_engine.cli serve > /tmp/lme-eval-engine.log 2>&1 ) &
  SERVE_PID=$!
  for i in $(seq 1 120); do
    h=$(curl -sf "$ENGINE_URL/v1/health" 2>/dev/null || true)
    if printf '%s' "$h" | grep -q '"ready":true\|"ready": true'; then echo "  engine ready"; break; fi
    kill -0 "$SERVE_PID" 2>/dev/null || fail "daemon 退出，见 /tmp/lme-eval-engine.log"
    sleep 2; [ "$i" = 120 ] && fail "60×2s 内未就绪，见 /tmp/lme-eval-engine.log"
  done
fi

say "4/6 语料（缺 turns.jsonl 才建：HF 下载 LongMemEval-S + 官方口径切片）"
if [ -f "$LME_DIR/turns.jsonl" ]; then
  echo "  turns.jsonl 已存在，跳过构建"
else
  runenv LME_DIR="$LME_DIR" -- "$PYTHON" "$LME_DIR/build_lme_corpus.py"
fi

say "5/6 灌库（断点续跑；bank=hermes-sessions，dedup=false 评测语料移植口径）"
runenv LME_DIR="$LME_DIR" LME_ENGINE="$ENGINE_URL" -- "$PYTHON" "$LME_DIR/ingest_turns.py"

say "6/6 评测出分（500 题全量，官方计分；flat=按题 tag 过滤，与官方 BM25 基线同口径）"
runenv LME_DIR="$LME_DIR" LME_ENGINE="$ENGINE_URL" LME_SCORER_SRC="$LME_SCORER_SRC" \
     -- "$PYTHON" "$LME_DIR/eval_retrieval.py"
if [ "$DRY" = 0 ]; then
  "$PYTHON" - "$LME_DIR/lme_retrieval_results.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
s = d.get("summary", d)
print(f"\n== headline: R@5 = {s.get('recall_any@5', s.get('flat_engine', {}).get('recall_any@5', '?'))} ==")
PY
fi
if [ "$WITH_QA" = 1 ]; then
  say "+QA 端到端子样本（n=100 固定 seed=42，本地模型自判定——口径见 eval/lme/README 诚实注记）"
  [ "${LME_LLM:-}" ] || fail "--with-qa 需设 LME_LLM=<OpenAI 兼容 chat 端点>"
  runenv LME_DIR="$LME_DIR" LME_ENGINE="$ENGINE_URL" LME_LLM="${LME_LLM:-http://127.0.0.1:8769/v1/chat/completions}" \
       -- "$PYTHON" "$LME_DIR/eval_qa.py"
fi
say "完成。逐题结果：$LME_DIR/lme_retrieval_results.json（引擎侧）——与仓内已提交基线对比即可复算"
