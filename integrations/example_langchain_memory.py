#!/usr/bin/env python3
"""LangChain 适配器最小示例（对运行中的 memory-engine daemon 直连演示）。

前提：daemon 已起（默认 http://127.0.0.1:8766，`GET /v1/health` 可通）。
运行：python3 integrations/example_langchain_memory.py [BASE_URL]
      MEMORY_ENGINE_PORT=8767 python3 integrations/example_langchain_memory.py
零 langchain 环境也能跑（shim 基类，方法面不变）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from langchain_memory import MemoryEngineMemory, MemoryEngineRetriever  # noqa: E402


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1
            else os.environ.get("MEMORY_ENGINE_LANGCHAIN_BASE",
                                "http://127.0.0.1:"
                                + os.environ.get("MEMORY_ENGINE_PORT", "8766")))
    sid = os.environ.get("USER", "anon")  # 演示：按用户隔离会话

    mem = MemoryEngineMemory(base_url=base, session_id=sid, bank="hermes", top_k=5)
    mem.save_context({"input": "我习惯用 /usr/bin/python3.12 跑测试"},
                     {"output": "已记录：该宿主 pytest 必须用 python3.12（3.11 收集即炸）"})
    history = mem.load_memory_variables({"input": "跑测试用什么解释器？"})
    print("== memory.history ==")
    print(history["history"] or "(召回为空——daemon 未起或 bank 无匹配)")

    retr = MemoryEngineRetriever(base_url=base, bank="hermes", top_k=3)
    docs = retr._get_relevant_documents("python3.12")
    print(f"\n== retriever: {len(docs)} docs ==")
    for d in docs:
        print(f"- score={d.metadata.get('score')} | {d.page_content[:60]}")

    mem.clear()  # 演示结束清场（软删=retired，历史可审计）
    print("\nclear() 完成（该会话记忆已 retired）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
