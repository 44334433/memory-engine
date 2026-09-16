#!/usr/bin/env python3
"""L4 巩固合成骨架（P1 第三批 2026-09-16；路线图 §自进化 L4）。

扫描 active 且高访问记忆簇（同 domain≥5 条，按簇内总访问量排序）→ LLM 批量通道
合成 observations 草稿 → 落 state/consolidation_drafts/observations-{date}.jsonl
（status=draft_pending_review）→ **人工审后入库**。

边界（本批拍板）：只到草稿产出——绝不自动写库（memories/edges 均不触碰）；
合成物入库（经 /v1/retain，becomes 可衰减可引用的记忆）是下一批拍板项。
LLM 通道与 llm_entity_extract.py 同源（LLM_GATEWAY_BASE_URL/KEY，成本闸 --limit）。

用法（默认 dry-run，不调用 LLM 不写文件）：
  python3 scripts/consolidate_synthesize.py                     # 打印簇计划
  python3 scripts/consolidate_synthesize.py --apply --limit 3   # 合成 3 个簇的草稿
环境：LLM_GATEWAY_API_KEY（--apply 必填，缺失显式报错禁伪造产出）
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memory_engine import config, db  # noqa: E402
from memory_engine.db import PgPool  # noqa: E402

BODY_SNIPPET = 400      # 每条正文截断（成本闸：5 条簇 ≈ 2.5k tokens/请求）
MIN_CLUSTER = 5         # 拍板：同 domain ≥5 条成簇

PROMPT = """你是记忆巩固合成器。下面是同一 domain（{domain}）的 {n} 条记忆（id 为 uuid）。
请从这组记忆中合成 1-3 条 observations（高阶洞察：跨条目共性/因果/规律，
不是任何单条内容的复述）。每条 observation 必须标注支撑它的记忆 id（evidence_ids，≥2 条）。
严格只输出 JSON（无 markdown 围栏），schema：
{{"observations": [{{"text": "...", "evidence_ids": ["<uuid>", "..."]}}]}}

记忆列表：
{items}"""


def _chat(base_url: str, api_key: str, model: str, prompt: str, timeout: float) -> str:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                         "temperature": 0.2}).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def scan_clusters(pool: PgPool, min_cluster: int, limit: int) -> list[dict]:
    """active+is_current 记忆按 domain 聚簇（≥min_cluster 条），按簇内总访问量降序。"""
    with pool.connection() as conn:
        rows = db.fetch_all(conn, """
            SELECT domain, count(*) n, sum(access_count) access_sum
            FROM memories WHERE ttl_state='active' AND is_current
            GROUP BY domain HAVING count(*) >= %s
            ORDER BY sum(access_count) DESC NULLS LAST, count(*) DESC LIMIT %s""",
            (min_cluster, limit))
    out = []
    for r in rows:
        out.append({"domain": r["domain"], "n": int(r["n"]),
                    "access_sum": int(r["access_sum"] or 0),
                    "members": fetch_members(pool, r["domain"])})
    return out


def fetch_members(pool: PgPool, domain: str) -> list[dict]:
    with pool.connection() as conn:
        return db.fetch_all(conn, """
            SELECT id, title, left(body, %s) AS body_snip, access_count
            FROM memories WHERE ttl_state='active' AND is_current AND domain=%s
            ORDER BY access_count DESC NULLS LAST LIMIT 20""",
            (BODY_SNIPPET, domain))


def synthesize_cluster(cluster: dict, api_key: str) -> list[dict]:
    """一簇 → LLM observations（草稿；解析失败显式报错，禁伪造）。"""
    items = "\n".join(
        f"- {m['id']} | {m['title']} | 访问{m['access_count'] or 0}次 | {m['body_snip']}"
        for m in cluster["members"])
    prompt = PROMPT.format(domain=cluster["domain"], n=len(cluster["members"]), items=items)
    raw = _chat(config.LLM_GATEWAY_BASE_URL, api_key, config.LLM_EXTRACT_MODEL, prompt,
                config.LLM_EXTRACT_TIMEOUT)
    raw = raw.strip()
    if raw.startswith("```"):                       # 容错：剥 markdown 围栏
        raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw, flags=re.M).strip()
    parsed = json.loads(raw)
    obs = parsed.get("observations")
    if not isinstance(obs, list) or not obs:
        raise ValueError(f"LLM 产出无 observations: {raw[:200]}")
    valid_ids = {str(m["id"]) for m in cluster["members"]}
    out = []
    for o in obs:
        ev = [str(e) for e in (o.get("evidence_ids") or []) if str(e) in valid_ids]
        if not o.get("text") or len(ev) < 2:        # 证据不足的洞察不入草稿（可核查性）
            continue
        out.append({"text": str(o["text"])[:600], "evidence_ids": ev})
    if not out:
        raise ValueError("所有 observation 证据不足（evidence_ids<2 或不在簇内）")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="L4 巩固合成骨架（active 高访问簇→observations 草稿）")
    ap.add_argument("--apply", action="store_true", help="真实调用 LLM 产出草稿（默认 dry-run 只打印计划）")
    ap.add_argument("--min-cluster", type=int, default=MIN_CLUSTER)
    ap.add_argument("--clusters", type=int, default=5, help="本轮最多合成的簇数（成本闸）")
    ap.add_argument("--limit", type=int, default=None, help="= --clusters 别名")
    args = ap.parse_args(argv)
    limit = args.limit if args.limit is not None else args.clusters

    pool = PgPool(config.PG_DSN, 1, 2)
    try:
        clusters = scan_clusters(pool, args.min_cluster, limit)
        print(f"簇计划（domain≥{args.min_cluster} 条，按访问量取前 {limit}）：")
        for c in clusters:
            print(f"  {c['domain']}: n={c['n']} access_sum={c['access_sum']}")
        if not args.apply:
            print("dry-run（未调 LLM 未写文件）；加 --apply 产出草稿")
            return 0
        api_key = config.LLM_GATEWAY_API_KEY
        if not api_key:
            print("错误：LLM_GATEWAY_API_KEY 未配置——--apply 需真实 LLM 通道，禁伪造产出", file=sys.stderr)
            return 2
        drafts_dir = Path(config.STATE_DIR) / "consolidation_drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        out_path = drafts_dir / f"observations-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        total = 0
        with out_path.open("a", encoding="utf-8") as f:
            for c in clusters:
                try:
                    obs = synthesize_cluster(c, api_key)
                except Exception as e:              # 单簇失败不拖垮整轮（显式登记）
                    print(f"  [{c['domain']}] 合成失败（显式登记不静默）: {e}", file=sys.stderr)
                    continue
                rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "kind": "observation_draft", "cluster_domain": c["domain"],
                       "member_ids": [str(m["id"]) for m in c["members"]],
                       "observations": obs, "status": "draft_pending_review",
                       "note": "L4 骨架批：人工审后经 /v1/retain 入库，本批不自动入库"}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                total += len(obs)
                print(f"  [{c['domain']}] 合成 {len(obs)} 条 observation 草稿")
        print(f"草稿落盘: {out_path}（observations 共 {total} 条；status=draft_pending_review）")
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
