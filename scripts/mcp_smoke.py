#!/usr/bin/env python3
"""MCP server stdio 协议冒烟（mcp-server-dev 技能验证环）：
spawn → initialize → tools/list（六工具在列）→ tools/call memory_recall 真查询。

用法：PYTHONPATH=src python3 scripts/mcp_smoke.py [查询词]
需要：本机 venv 装有 mcp SDK，daemon 可达（MEMORY_ENGINE_BASE，缺省 127.0.0.1:8766）。
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {"memory_retain", "memory_recall", "memory_feedback",
            "memory_get", "memory_search_list", "engine_metrics"}


async def run(query: str) -> int:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "memory_engine.mcp_server"],
        env={k: v for k, v in os.environ.items() if k != "PYTHONSTARTUP"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            info = await s.initialize()
            print("== initialize ==")
            print(json.dumps({"server": info.server_info.name,
                              "version": info.server_info.version,
                              "protocol": info.protocol_version}, ensure_ascii=False))
            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            print("== tools/list ==")
            print(json.dumps(sorted(names), ensure_ascii=False))
            missing = EXPECTED - names
            print("tools check:", "OK 六工具在列" if not missing else f"FAIL 缺 {missing}")
            res = await s.call_tool("memory_recall", {"query": query, "k": 3})
            # SDK 1.x/2.x 字段名兼容（isError/is_error；TextContent.text）
            err = getattr(res, "is_error", None)
            if err is None:
                err = getattr(res, "isError", False)
            first = res.content[0] if res.content else None
            text = str(getattr(first, "text", first))
            print("== tools/call memory_recall ==", query)
            print(text[:1500])
            ok = not missing and not err and '"results"' in text
            print("SMOKE:", "PASS" if ok else "FAIL")
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else
                             "memory-engine 投毒闸 source_tier 低信任入场")))
