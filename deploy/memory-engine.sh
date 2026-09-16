#!/usr/bin/env bash
# memory-engine CLI 薄壳：唯一入口（蓝图 §8.1 ExecStart 指向此文件）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${SCRIPT_DIR}/../src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m memory_engine.cli "$@"
