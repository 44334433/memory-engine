#!/usr/bin/env python3
"""阶段1 自测套件（任务⑥）：retain 20 中文 → recall 三路验证 → PATCH/DELETE → P95(50) → 重启恢复。
消费者纪律：全走 HTTP（127.0.0.1），禁 import 旁路；仅末尾直接 psql 读 access_events/changelog 做对账（只读）。
用法：MEMORY_ENGINE_PORT=8766 /usr/bin/python3 tests/smoke_test.py
"""
import http.client
import json
import os
import statistics
import subprocess
import sys
import time
import uuid

HOST = "127.0.0.1"
PORT = int(os.environ.get("MEMORY_ENGINE_PORT", "8766"))
RESULTS = {"port": PORT, "checks": {}, "p95": None, "fail": []}
CREATED_IDS: list[str] = []  # 本次冒烟写入的全部条目 id，尾部统一 purge（真库零残留）


def req(method: str, path: str, body: dict | None = None, timeout: float = 30) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    payload = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, payload, {"Content-Type": "application/json"})
    r = conn.getresponse()
    data = r.read().decode()
    conn.close()
    try:
        parsed = json.loads(data) if data else {}
    except json.JSONDecodeError:
        parsed = {"raw": data[:300]}
    return r.status, parsed if isinstance(parsed, dict) else {"list": parsed}


def check(name: str, ok: bool, detail=""):
    RESULTS["checks"][name] = {"ok": ok, "detail": detail}
    if not ok:
        RESULTS["fail"].append(name)
    print(("✓" if ok else "✗"), name, detail if not ok else "")


ITEMS = [
    ("knowledge", "pgvector HNSW 按 bank 建部分索引可裁剪扫描面，四库各建一个 HNSW 索引对应四路检索", "存储选型·pgvector", ["pgvector", "HNSW"], "memory-engine", 4, None),
    ("knowledge", "PGroonga 内建 CJK 分词，中文全文检索无需外置 jieba 预分词，TokenBigram 为默认分词器", "存储选型·PGroonga", ["pgroonga", "中文检索"], "memory-engine", 4, None),
    ("knowledge", "RRF 倒数排名融合：多路召回按 1/(60+rank) 加权求和，对异构分数天然鲁棒", "检索算法·RRF", ["RRF", "融合排序"], "memory-engine", 3, None),
    ("knowledge", "UUIDv7 前段是毫秒时间戳，做主键天然时间有序，B树索引写入局部性好", "工程·UUIDv7", ["uuid"], "memory-engine", 3, None),
    ("knowledge", "pg_dump -Fc 自定义格式支持压缩与并行恢复，pg_restore --list 可先校验归档目录再恢复", "运维·备份", ["pg_dump", "备份"], "memory-engine", 3, None),
    ("knowledge", "HNSW 图索引查询复杂度近对数，构建时 m 参数控制每层邻居数，ef_search 权衡召回与延迟", "存储选型·HNSW", ["hnsw"], "memory-engine", 3, None),
    ("hermes", "示例记忆A：沟通偏好类条目（合成测试数据，结论先行）", "示例·偏好", ["demo"], "demo-profile", 5, None),
    ("hermes", "示例记忆B：工作纪律类条目（合成测试数据，一次做到位优先）", "示例·纪律", ["demo"], "demo-rule", 5, None),
    ("hermes", "示例记忆C：网络环境类条目（合成测试数据，出网先探路由）", "示例·网络", ["demo"], "network", 4, None),
    ("hermes", "示例记忆D：备份纪律条目（合成测试数据，真源必须异盘备份）", "示例·备份", ["备份"], "ops", 4, None),
    ("hermes-sessions", "会话示例：对齐记忆引擎架构——PG18+pgvector+PGroonga 三路召回，daemon 端口可配置", "会话·架构对齐", ["架构"], "sessions", 3, None),
    ("hermes-sessions", "会话示例：嵌入模型选型——0.6B 级 fp16 常驻显存约 2-3GB，向量维度 1024", "会话·选型", ["embedding"], "sessions", 3, None),
    ("reflection", "教训：断言前必须实证，管道里的 $? 是最后一个命令的退出码，会吞掉真实失败", "反思·工程纪律", ["教训"], "methodology", 4, None),
    ("reflection", "反思：Socks 代理环境变量会泄漏到 Python httpx 导致 ImportError，直连场景应显式 unset", "反思·环境", ["教训"], "methodology", 3, None),
    ("reflection", "复盘：多实例共享端口前先 ss -ltnp 探测，蓝图端口规划要与在跑服务对账", "反思·部署", ["教训"], "methodology", 3, None),
    ("knowledge", "systemd Type=notify 要求服务就绪后主动发 READY=1，配合 WatchdogSec 需周期发 WATCHDOG=1 心跳", "工程·systemd", ["systemd"], "memory-engine", 3, None),
    ("knowledge", "PostgreSQL generate column STORED 列可被 PGroonga 直接建全文索引，中文搜索开箱即用", "存储·PG特性", ["postgres"], "memory-engine", 2, None),
    ("hermes", "示例记忆E：时效分级——fresh 三十天以内、aging 三十天到九十天、stale 九十天以上分级降权", "示例·时效", ["时效"], "memory-engine", 4, None),
    ("hermes-sessions", "会话示例：验收线——recall P95 两百毫秒以内硬指标，预热失败等于启动失败", "会话·验收", ["验收"], "sessions", 4, None),
    ("reflection", "观察：时间序列回测要先冻结切片再跑策略，防止未来函数污染信号统计", "反思·方法", ["回测"], "methodology", 2, "2026-07-28T10:00:00+08:00"),  # stale >90d
]

SEMANTIC_QUERIES = [
    "怎么给记忆条目做向量近似搜索",
    "中文分词全文检索用什么方案",
    "多路召回结果怎么合并排序",
    "备份归档怎么校验可用",
    "模型常驻显卡要占多少内存",
]
FTS_QUERY = "TokenBigram"
TIME_QUERY = "zzz-无语义无关词-9x7qz"


def main() -> int:
    # 1) health
    st, h = req("GET", "/v1/health")
    check("health.ready", st == 200 and h.get("db") and h.get("model_loaded") and h.get("warm"), str(h)[:200])
    RESULTS["health"] = h

    # 2) retain 20 条中文（按 ITEMS 的 bank 分组，真实验证四库部分 HNSW 索引；断言幂等=ids+dedup）
    st, r = req("POST", "/v1/retain", {"bank": "knowledge", "caller": "main", "items": [
        {"content": ITEMS[0][1], "context": "批量写入", "tags": []}]})
    check("retain.single", st == 200 and len(r.get("ids", [])) + r.get("dedup_skipped", 0) == 1, f"{st} {r}")
    CREATED_IDS.extend(r.get("ids", []))
    by_bank: dict[str, list] = {}
    for (_b, c, ctx, tags, dom, pri, od) in ITEMS:
        item = {"content": c, "context": ctx, "tags": tags, "domain": dom,
                "priority": pri, "source_type": "manual"}
        if od:
            item["original_date"] = od
        by_bank.setdefault(_b, []).append(item)
    total_ids, total_skip = 0, 0
    for b, its in by_bank.items():
        st, r = req("POST", "/v1/retain", {"bank": b, "caller": "main", "items": its})
        total_ids += len(r.get("ids", []))
        total_skip += r.get("dedup_skipped", 0)
        CREATED_IDS.extend(r.get("ids", []))
    check("retain.batch20", st == 200 and total_ids + total_skip == 20,
          f"st={st} ids={total_ids} skipped={total_skip}")
    st, r2 = req("POST", "/v1/retain", {"bank": "knowledge", "caller": "main", "items": [
        {"content": ITEMS[1][1], "context": "重复写入测试"}]})
    check("retain.dedup_hash", st == 200 and r2.get("dedup_skipped") == 1, f"{r2}")
    near = ITEMS[2][1].replace("，", ", ")
    st, r3 = req("POST", "/v1/retain", {"bank": "knowledge", "caller": "main", "items": [
        {"content": near, "context": "语义近似判重测试"}]})
    check("retain.dedup_semantic(info)", st == 200, f"dedup_skipped={r3.get('dedup_skipped')}（近重复信息项）")
    st, r4 = req("POST", "/v1/retain", {"bank": "knowledge", "caller": "main", "items": [
        {"content": "没有上下文的记忆", "context": "   "}]})
    check("retain.context_422", st == 422, f"{st} {r4}")
    st, r5 = req("POST", "/v1/retain", {"bank": "bad-bank", "caller": "main", "items": [
        {"content": "非法bank测试", "context": "bank校验"}]})
    check("retain.bank_422", st == 422, str(st))
    # 可见性测试条目（private + agent）
    st, r6 = req("POST", "/v1/retain", {"bank": "hermes", "caller": "main", "items": [
        {"content": "机密条目：私有可见性测试专用内容 qzx9", "context": "可见性测试", "visibility": "private"},
        {"content": "时效降权测试：三个月前的旧结论应被降权 vqx7", "context": "时效测试", "original_date": "2026-05-10T00:00:00+08:00"}]})
    check("retain.vis_and_stale_items", st == 200 and len(r6.get("ids", [])) + r6.get("dedup_skipped", 0) == 2, f"{r6}")
    CREATED_IDS.extend(r6.get("ids", []))

    # 3) recall 三路验证
    for i, q in enumerate(SEMANTIC_QUERIES):
        st, r = req("POST", "/v1/recall", {"query": q, "caller": "main", "top_k": 5})
        top = (r.get("results") or [{}])[0]
        parts = top.get("score_parts", {})
        check(f"recall.semantic[{i}]", st == 200 and bool(top) and "rrf" in parts,
              f"{st} took={r.get('took_ms')} routes={r.get('routes')}")
        if i == 0:
            RESULTS["semantic_top"] = {"query": q, "title": top.get("title"), "routes": r.get("routes"), "score_parts": parts}

    st, r = req("POST", "/v1/recall", {"query": FTS_QUERY, "caller": "main", "top_k": 5})
    fts_hit = any("fts" in x["score_parts"]["routes"] for x in r.get("results", []))
    check("recall.fts_route", st == 200 and fts_hit, f"routes={r.get('routes')}")
    st, r = req("POST", "/v1/recall", {"query": TIME_QUERY, "caller": "main", "top_k": 30})
    time_hit = any("time" in x["score_parts"]["routes"] for x in r.get("results", [])) \
               or (r.get("routes") or {}).get("time", 0) > 0
    check("recall.time_route", st == 200 and time_hit, f"routes={r.get('routes')}（库为活态：聚合+明细双判定，2026-09-16 修复）")

    # 可见性：subagent 看不到 private/他人 agent 条目
    st, r = req("POST", "/v1/recall", {"query": "私有可见性测试专用内容 qzx9", "caller": "subagent:test-x", "top_k": 10})
    check("recall.vis_subagent_denied", st == 200 and not r.get("results"), f"{len(r.get('results', []))} 条泄漏")
    st, r = req("POST", "/v1/recall", {"query": "私有可见性测试专用内容 qzx9", "caller": "main", "top_k": 10})
    check("recall.vis_main_ok", st == 200 and bool(r.get("results")), "")

    # 时效降权：stale 条目 score_parts.stale == 0.7
    st, r = req("POST", "/v1/recall", {"query": "三个月前的旧结论应被降权 vqx7", "caller": "main", "top_k": 5})
    stale_item = next((x for x in r.get("results", []) if "vqx7" in x["title"] + x["body"]), None)
    check("recall.stale_factor", bool(stale_item) and stale_item["score_parts"]["stale"] == 0.7,
          f"stale={stale_item and stale_item['score_parts'].get('stale')}")

    # 4) PATCH（只动本次写入的条目 mid=CREATED_IDS[0]，禁碰库内真实记忆——2026-09-16 事故修复）
    mid = CREATED_IDS[0]
    st, orig = req("GET", f"/v1/memories/{mid}")
    old_pri = orig.get("priority")
    st, r = req("PATCH", f"/v1/memories/{mid}", {"priority": 5, "tags": ["patched"]})
    check("patch.fields", st == 200 and r.get("priority") == 5, f"{st} {r}")
    st, r = req("PATCH", f"/v1/memories/{mid}", {"body": orig.get("body") + "（补一句触发重嵌）"})
    check("patch.reembed", st == 200 and r.get("has_embedding"), f"{st}")
    st, r = req("POST", "/v1/recall", {"query": f"{orig['title']} 补一句触发重嵌",
                                        "caller": "main", "top_k": 3})
    check("patch.recall_after_patch", st == 200 and any(x["id"] == mid for x in r.get("results", [])), "")

    # 4b) W1 核心记忆块 core-block（2026-09-18）：形状/默认值/pin→入块/unpin→出块/预算闸/P95
    st, cb0 = req("GET", "/v1/core-block")
    check("core_block.shape", st == 200 and all(
        k in cb0 for k in ("text", "ids", "budget_chars", "used_chars", "truncated", "took_ms"))
        and len(cb0.get("text", "")) <= 1500, f"{st}")
    st, rc = req("POST", "/v1/retain", {"bank": "hermes", "caller": "main", "items": [
        {"content": f"核心记忆块冒烟条目 cbx7：钉住后应出现在常驻注入区 {int(time.time())}",
         "context": "W1 core-block 冒烟（用后即删）", "source_tier": "user",
         "memory_type": "semantic", "domain": "smoke"}]})
    cmid = (rc.get("ids") or [""])[0]
    CREATED_IDS.append(cmid)
    check("core_block.retain", st == 200 and bool(cmid), f"{rc}")
    st, mem = req("GET", f"/v1/memories/{cmid}")
    check("core_block.default_unpinned", st == 200 and mem.get("pinned") is False, f"{mem.get('pinned')}")
    st, _ = req("GET", "/v1/core-block")
    check("core_block.fresh_not_in", cmid not in _.get("ids", []), "")
    st, p = req("PATCH", f"/v1/memories/{cmid}", {"pinned": True})
    check("core_block.patch_pin", st == 200 and p.get("pinned") is True, f"{st}")
    st, cb1 = req("GET", "/v1/core-block")
    check("core_block.pinned_in", st == 200 and cmid in cb1["ids"] and "cbx7" in cb1["text"],
          f"in_ids={cmid in cb1.get('ids', [])}")
    st, cb2 = req("GET", "/v1/core-block?budget_chars=120")
    check("core_block.budget_gate", st == 200 and len(cb2.get("text", "")) <= 120,
          f"len={len(cb2.get('text', ''))}")
    st, p = req("PATCH", f"/v1/memories/{cmid}", {"pinned": False})
    st2, cb3 = req("GET", "/v1/core-block")
    check("core_block.unpinned_out", st == 200 and p.get("pinned") is False
          and st2 == 200 and cmid not in cb3["ids"], "")
    lat_cb = []
    for _ in range(30):
        t0 = time.perf_counter()
        st, _r = req("GET", "/v1/core-block")
        lat_cb.append((time.perf_counter() - t0) * 1000)
        assert st == 200
    lat_cb.sort()
    p95_cb = lat_cb[int(len(lat_cb) * 0.95) - 1]
    RESULTS["core_block_p95"] = round(p95_cb, 1)
    check("core_block.p95_lt50ms", p95_cb < 50, f"p95={p95_cb:.1f}ms（验收线 50ms，环回含 HTTP 开销）")

    # 4c) 图谱深度批（2026-09-19）：取代链多跳回放 + 2 跳邻居遍历（链就地构造，尾部 purge 零残留）
    st, gc0 = req("POST", "/v1/retain", {"bank": "hermes", "caller": "main", "items": [
        {"content": f"链深冒烟 v1 gdch7：端口配置 8080 {int(time.time())}",
         "context": "图谱深度批冒烟（用后即删）", "domain": "smoke"}]})
    g1 = (gc0.get("ids") or [""])[0]
    CREATED_IDS.append(g1)
    check("chain.retain_seed", st == 200 and bool(g1), f"{st} {gc0}")
    gmid = g1
    for i in (2, 3):
        st, gp = req("PATCH", f"/v1/memories/{gmid}", {
            "body": f"链深冒烟 v{i} gdch7：端口配置 {8080 + i * 1010}",
            "supersede": True})
        assert st == 200, gp
        gmid = gp.get("id") or gmid
        CREATED_IDS.append(gmid)
    st, ch = req("GET", f"/v1/memories/{g1}/chain?max_hops=5")
    vers = ch.get("versions", [])
    check("chain.length3", st == 200 and ch.get("length") == 3
          and vers and vers[0]["id"] == g1 and vers[-1]["is_current"] is True,
          f"{st} {str(ch)[:200]}")
    check("chain.windows_contiguous",
          all(a["invalid_at"] == b["valid_at"] for a, b in zip(vers, vers[1:]))
          and vers[-1]["invalid_at"] is None, str(vers[-2:] if vers else "")[:200])
    st, ch_mid = req("GET", f"/v1/memories/{gmid}/chain")   # 链上任意 seed 双向回溯到同一全链
    check("chain.seed_middle", st == 200 and ch_mid.get("length") == 3
          and ch_mid.get("head") == g1 and ch_mid.get("tail") == gmid
          and [v["hop_from_seed"] for v in ch_mid.get("versions", [])] == [-2, -1, 0],
          f"{st} {str(ch_mid)[:200]}")
    st, ch_cap = req("GET", f"/v1/memories/{g1}/chain?max_hops=1")
    check("chain.max_hops_truncated", st == 200 and ch_cap.get("length") == 2
          and ch_cap.get("truncated_forward") is True and ch_cap.get("truncated_back") is False,
          f"{st} {str(ch_cap)[:150]}")
    st, _c404 = req("GET", f"/v1/memories/{uuid.uuid4()}/chain")
    check("chain.404", st == 404, str(st))
    # 邻居：从全图快照挑真实边端点做种子（只读，取首个出结果的）；as_of 远古=空窗（边过滤语义）
    st, gsnap = req("GET", "/v1/graph?limit=20")
    seed_id, nb = None, {}
    for cand in [e["source"] for e in gsnap.get("edges", [])][:8]:
        st, nb = req("GET", f"/v1/graph/neighbors?id={cand}&hops=2")
        if st == 200 and nb.get("nodes"):
            seed_id = cand
            break
    nbs = nb.get("nodes", [])
    check("neighbors.two_hop", bool(seed_id) and all(1 <= n["hop"] <= 2 for n in nbs)
          and len({n["id"] for n in nbs}) == len(nbs),   # 防环：无重复节点
          f"counts={nb.get('counts')}")
    st, nb0 = req("GET", f"/v1/graph/neighbors?id={seed_id}&hops=2&as_of=2020-01-01T00:00:00Z")
    check("neighbors.asof_empty_window", st == 200 and nb0.get("edges") == []
          and bool(nb0.get("as_of")), f"{st} {str(nb0)[:150]}")
    st, nb_404 = req("GET", f"/v1/graph/neighbors?id={uuid.uuid4()}")
    check("neighbors.404", st == 404, str(st))

    # 4d) W4 bank 级自适应阈值（2026-09-19）：默认只登记 skip（旧 daemon 无 W4 代码=不伪绿不假红）；
    #     MEMORY_ENGINE_W4_LIVE=1 时对 hermes(0.95)/knowledge(0.98) 跑同一内容对的判重分化 HTTP 实证
    #     （X/Y 对与 cos=0.9722 实证登记 tests/test_w4_bank_thresholds.py，此处为端到端冒烟复核）。
    if os.environ.get("MEMORY_ENGINE_W4_LIVE") == "1":
        w4x = "W4演示甲（合成数据）：光伏板巡检SLAM项目的三轮验收在九月完成，整体通过；打光模组遗留两处轻微缺陷，计划下月闭环，需复拍一组数据。"
        w4y = "W4演示乙（合成数据）：光伏板巡检SLAM项目九月通过三轮整体验收，打光模组遗留两处轻微缺陷待下月闭环，验证需再拍一批数据。"
        tag = int(time.time() * 1000) % 10**9
        w4_ids, w4_ok = [], True
        for bank, expect_skip in (("hermes", True), ("knowledge", False)):
            st, rx = req("POST", "/v1/retain", {"bank": bank, "caller": "main", "items": [
                {"content": f"{w4x} 标记{tag}", "context": "W4 冒烟判重分化（用后即删）",
                 "source_tier": "user", "domain": "smoke"}]})
            w4_ok &= st == 200 and bool(rx.get("ids"))
            w4_ids += rx.get("ids", [])
            st, ry = req("POST", "/v1/retain", {"bank": bank, "caller": "main", "items": [
                {"content": f"{w4y} 标记{tag}", "context": "W4 冒烟判重分化（用后即删）",
                 "source_tier": "user", "domain": "smoke"}]})
            w4_ok &= st == 200 and (bool(ry.get("dedup_skipped")) == expect_skip)
            w4_ids += ry.get("ids", [])
        for cid in w4_ids:
            req("DELETE", f"/v1/memories/{cid}?purge=true")
        check("w4.dedup_bank_differentiation", w4_ok, "hermes@0.95 skip vs knowledge@0.98 retain")
    else:
        RESULTS["w4"] = "skipped（daemon 未运行 W4 代码或未开 MEMORY_ENGINE_W4_LIVE；实证批见 test_w4_bank_thresholds.py）"
        print("- w4 段 skip（登记 last_smoke.json，非失败）")

    # 5) DELETE（retired 语义）+ purge
    st, r = req("DELETE", f"/v1/memories/{mid}")
    check("delete.retired", st == 200 and r.get("state") == "retired", f"{r}")
    st, r = req("POST", "/v1/recall", {"query": f"{orig['title']} 补一句触发重嵌",
                                        "caller": "main", "top_k": 5})
    check("delete.recall_gone", st == 200 and not any(x["id"] == mid for x in r.get("results", [])), "")
    st, r = req("GET", f"/v1/memories/{mid}")
    check("delete.get_still_exists", st == 200 and r.get("ttl_state") == "retired" and r.get("has_embedding") is False, f"{r.get('ttl_state')},{r.get('has_embedding')}")
    st, r = req("DELETE", f"/v1/memories/{mid}?purge=true")
    check("delete.purge", st == 200 and r.get("state") == "deleted", f"{r}")

    # 6) P95 50 次热查询
    lat = []
    for i in range(50):
        q = SEMANTIC_QUERIES[i % len(SEMANTIC_QUERIES)]
        t0 = time.perf_counter()
        st, r = req("POST", "/v1/recall", {"query": q, "caller": "main", "top_k": 10})
        lat.append((time.perf_counter() - t0) * 1000)
        assert st == 200, f"recall failed {st}"
    lat.sort()
    p50 = statistics.median(lat)
    p95 = lat[int(len(lat) * 0.95) - 1]
    RESULTS["p95"] = round(p95, 1)
    RESULTS["p50"] = round(p50, 1)
    RESULTS["lat_max"] = round(lat[-1], 1)
    check("p95.lt200", p95 < 200, f"p50={p50:.1f} p95={p95:.1f} max={lat[-1]:.1f}ms")

    # 7) export 冒烟
    st, raw = _export()
    check("export.jsonl", st == 200 and raw.count("\n") >= 21, f"lines={raw.count(chr(10))}")

    # 8) 对账（只读直查：access_events / changelog）
    import psycopg
    conn = psycopg.connect(os.environ.get("MEMORY_ENGINE_PG_DSN", "postgresql://memengine@127.0.0.1:5433/memengine"), autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM memories), (SELECT count(*) FROM changelog), (SELECT count(*) FROM access_events)")
        m, c, a = cur.fetchone()
    conn.close()
    RESULTS["reconcile"] = {"memories": m, "changelog": c, "access_events": a}
    check("reconcile.events", a > 0 and c > 20, f"mem={m} changelog={c} access={a}")

    # 9) 自清理：purge 本次写入的全部测试条目（真库零残留；404=此前已 purge 亦算干净）
    purged, missed = 0, []
    for cid in dict.fromkeys(CREATED_IDS):
        st, _r = req("DELETE", f"/v1/memories/{cid}?purge=true")
        if st in (200, 404):
            purged += 1
        else:
            missed.append(cid)
    RESULTS["cleanup"] = {"attempted": len(set(CREATED_IDS)), "purged": purged, "missed": missed}
    check("cleanup.purged", not missed, f"{purged}/{len(set(CREATED_IDS))} missed={len(missed)}")

    print(json.dumps(RESULTS, ensure_ascii=False, indent=2, default=str)[:3000])
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_smoke.json"), "w") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2, default=str)
    return 1 if RESULTS["fail"] else 0


def _export() -> tuple[int, str]:
    conn = http.client.HTTPConnection(HOST, PORT, timeout=30)
    conn.request("GET", "/v1/export?since_seq=0")
    r = conn.getresponse()
    data = r.read().decode()
    conn.close()
    return r.status, data


if __name__ == "__main__":
    sys.exit(main())
