#!/usr/bin/env python3
"""LLM 实体抽取批量脚本（P1 第二批 ④）：从记忆 content 抽实体 + 四类边。

- 通道：LLM_GATEWAY_BASE_URL/LLM_GATEWAY_API_KEY（OpenAI 兼容 /chat/completions，
  与嵌入可插拔 openai_compat 同一套网关配置）；未配置 key → 显式报错退出（禁伪造产出）。
- 成本控制：--batch-size 条/请求、--limit 单次运行上限、--bank 过滤、跳过已抽取记忆
  （edges.source='llm_extract' 已有边者），分批可中断续跑。
- 缺口B修复（2026-09-19 拍板，研究-提取失败游标 §1.3）：LLM 成功但零产出（无边写出）
  的记忆登记 engine_meta(key='extract_attempts') 并在此后选单中排除——防「永久落单」
  记忆占满 seq DESC 头部窗口导致旧记忆饥饿 + 每轮重复烧 LLM。真失败（调用/解析炸）
  不登记、保持可重试。
- G15（S1 级盲审硬约束，observe-only，写死）：contradicts 边**只记录**进 edges 表，
  绝不触发 memories.invalid_at 置位、绝不 DELETE memories——升 enforce 前置 =
  金标边集 precision>=0.7 且 30 天抽检通过（见 config 注释 / db.insert_edge 注释 /
  scripts/migrations/002 文件头，三处一致）。

用法（默认 dry-run，不调用 LLM 不写库）：
  python3 scripts/llm_entity_extract.py                     # 打印分批计划
  python3 scripts/llm_entity_extract.py --apply --limit 40  # 抽 40 条（2 批×20）
环境：
  LLM_GATEWAY_BASE_URL（默认 config.LLM_GATEWAY_BASE_URL）、LLM_GATEWAY_API_KEY（必填才可 --apply）、
  MEMORY_ENGINE_LLM_MODEL（默认 deepseek-v4-flash）
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
import psycopg  # noqa: E402

SOURCE = "llm_extract"
ATTEMPTS_KEY = "extract_attempts"   # engine_meta 键：{memory_id: {"ts":..., "empty": true}}
BODY_SNIPPET = 400   # 每条记忆正文截断（成本闸：tokens ≈ 0.6×字符，20 条/批 ≤ ~8k tokens）
ENTITY_TYPES = db.ENTITY_TYPES
EDGE_TYPES = db.EDGE_TYPES

PROMPT = """你是记忆库的知识图谱抽取器。给你 {n} 条记忆（id 为 uuid）。请抽取：
1) entities：记忆中出现的实体（人名/组织/项目/概念/工具/地点/事件），
   etype ∈ {etypes}（不在表内归 other）。
2) edges：记忆之间的关系边或记忆→实体边，etype ∈ {edge_types}：
   - related：相关；causal：因果（src 导致 dst）；parent_child：父子（src 包含/派生 dst）；
   - contradicts：矛盾（dst 与 src 事实冲突）。
严格只输出 JSON（无 markdown 围栏），schema：
{{"entities": [{{"name": "...", "etype": "..."}}],
  "edges": [{{"src_mid": "<uuid>", "dst_mid": "<uuid 或省略>", "entity": "<实体名或省略>", "etype": "..."}}]}}
约束：src_mid 必须是给出的记忆 id；记忆间关系才填 dst_mid，记忆提到实体则填 entity；不要发明 id。

记忆列表：
{items}"""


def _chat(base_url: str, api_key: str, model: str, prompt: str, timeout: float) -> str:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                         "temperature": 0.1}).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if not content.strip():
        raise RuntimeError(f"LLM 返回空 content（模型 {model} 可能是 reasoning 型被截断），"
                           f"原始键: {sorted(payload.get('choices', [{}])[0].get('message', {}).keys())}")
    return content


def _parse_json(text: str) -> dict:
    """容错解析：剥 markdown 围栏 / 截取首尾大括号。"""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def load_attempts(conn) -> dict:
    """attempt 登记表（缺口B修复，2026-09-19 拍板）：engine_meta(key='extract_attempts')。

    值 = {memory_id: {"ts": iso, "empty": true}}，只登记「LLM 成功但判定无边」的记忆；
    真失败（LLM 调用/解析炸）不登记、保持可重试。饥饿解除：此类记忆不再占据
    seq DESC 头部窗口，旧记忆得以进入；且不再每轮重复烧 LLM 成本。
    """
    row = db.fetch_one(conn, "SELECT value FROM engine_meta WHERE key=%s", (ATTEMPTS_KEY,))
    val = row["value"] if row else None
    return dict(val) if isinstance(val, dict) else {}


def save_attempts(conn, attempts: dict) -> None:
    db.execute(conn,
               "INSERT INTO engine_meta(key, value) VALUES (%s, %s::jsonb) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (ATTEMPTS_KEY, json.dumps(attempts, ensure_ascii=False)))


def mark_empty_attempts(attempts: dict, batch_ids: list[str], covered: set[str], ts: str) -> int:
    """批后登记：本轮 LLM 成功但没为某记忆写出任何新 llm_extract 边的 → empty 标记。
    （产出全被校验丢弃、0 提案、或提案全部撞已有边成 dup——均属「无边可抽」。）返回新增数。"""
    n = 0
    for mid in batch_ids:
        if mid not in covered and mid not in attempts:
            attempts[mid] = {"ts": ts, "empty": True}
            n += 1
    return n


def pick_memories(conn, limit: int, bank: str | None,
                  exclude_empty: list[str] | None = None) -> list[dict]:
    """待抽取记忆：现行 + 未 retired/archived，且尚无 llm_extract 边（续跑语义），
    并排除 attempt 登记表中 empty=true 者（缺口B：空输出记忆不再占据头部窗口）。"""
    sql = """
        SELECT id, title, body, valid_at FROM memories m
        WHERE m.is_current AND m.ttl_state NOT IN ('archived', 'retired')
          AND (%s::text IS NULL OR m.bank = %s)
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.src_mid = m.id AND e.source = %s)"""
    params: list = [bank, bank, SOURCE]
    if exclude_empty:
        sql += "\n          AND m.id::text <> ALL(%s::text[])"
        params.append(exclude_empty)
    sql += "\n        ORDER BY m.seq DESC LIMIT %s"
    params.append(limit)
    return db.fetch_all(conn, sql, tuple(params))


def persist(conn, mems: list[dict], llm_out: dict, valid_at_map: dict) -> dict:
    """实体 upsert + 边写入。返回计数。校验失败的条目丢弃并计数（禁脏数据入库）。"""
    id_set = {str(m["id"]) for m in mems}
    ent_type = {str(e.get("name", "")).strip().lower(): str(e.get("etype", "other"))
                for e in llm_out.get("entities") or []}   # 同批实体类型映射（缺省 other）
    stats = {"entities": 0, "edges": 0, "edges_dup": 0, "dropped": 0, "by_etype": {}}
    covered: set[str] = set()   # 写出过新边的 src 记忆（缺口B：有产出者不做 empty 登记）
    with conn.transaction():
        for ent in llm_out.get("entities") or []:
            try:
                name = str(ent.get("name", "")).strip()[:120]
                if not name:
                    raise ValueError("empty name")
                db.upsert_entity(conn, name, str(ent.get("etype", "other")))
                stats["entities"] += 1
            except Exception:
                stats["dropped"] += 1
        for e in llm_out.get("edges") or []:
            try:
                src = str(e.get("src_mid", ""))
                etype = str(e.get("etype", ""))
                if src not in id_set or etype not in EDGE_TYPES:
                    raise ValueError(f"bad src/etype: {src[:8]}/{etype}")
                dst = e.get("dst_mid")
                ent_name = str(e.get("entity") or "").strip()
                vat = valid_at_map.get(src)
                if dst:
                    if str(dst) not in id_set or str(dst) == src:
                        raise ValueError("bad dst")
                    eid = db.insert_edge(conn, src, dst_mid=str(dst), etype=etype,
                                         valid_at=vat, source=SOURCE)
                elif ent_name:
                    eid0 = db.upsert_entity(conn, ent_name[:120],
                                            ent_type.get(ent_name.lower(), "other"))
                    eid = db.insert_edge(conn, src, entity_id=eid0, etype=etype,
                                         valid_at=vat, source=SOURCE)
                else:
                    raise ValueError("edge 无指向（dst_mid/entity 均缺）")
                if eid:
                    stats["edges"] += 1
                    stats["by_etype"][etype] = stats["by_etype"].get(etype, 0) + 1
                    covered.add(src)
                else:
                    stats["edges_dup"] += 1
            except Exception:
                stats["dropped"] += 1
    stats["covered_ids"] = sorted(covered)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 实体抽取批量脚本（dry-run 默认）")
    ap.add_argument("--apply", action="store_true", help="实际调用 LLM 并写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=40, help="单次运行记忆条数上限（成本闸）")
    ap.add_argument("--batch-size", type=int, default=20, help="每请求记忆条数")
    ap.add_argument("--bank", default=None, help="限定 bank（缺省跨库）")
    args = ap.parse_args()

    base_url = config.LLM_GATEWAY_BASE_URL
    api_key = config.LLM_GATEWAY_API_KEY
    model = config.LLM_EXTRACT_MODEL
    if args.apply and not api_key:
        print("LLM_GATEWAY_API_KEY 未配置：--apply 需要网关密钥（禁伪造产出）；先 export 后重试")
        return 2

    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        attempts = load_attempts(conn)   # 缺口B：attempt 登记表（empty=不再重选）
        empty_ids = sorted(k for k, v in attempts.items()
                           if isinstance(v, dict) and v.get("empty"))
        mems = pick_memories(conn, args.limit, args.bank, empty_ids)
        batches = [mems[i:i + args.batch_size] for i in range(0, len(mems), args.batch_size)]
        print(f"待抽取 {len(mems)} 条 → {len(batches)} 批（batch_size={args.batch_size}, "
              f"model={model}, apply={args.apply}）；attempt 登记 {len(empty_ids)} 条空输出已排除")
        if not args.apply:
            for bi, b in enumerate(batches):
                print(f"--- batch {bi}: ids={[str(m['id'])[:8] for m in b]}")
            return 0

        total = {"entities": 0, "edges": 0, "edges_dup": 0, "dropped": 0, "by_etype": {}}
        ts_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for bi, batch in enumerate(batches):
            vat_map = {str(m["id"]): m["valid_at"] for m in batch}
            items = "\n".join(
                f"- id={m['id']} | title={m['title']!r} | body={(m['body'] or '')[:BODY_SNIPPET]!r}"
                for m in batch)
            prompt = PROMPT.format(n=len(batch), etypes=list(ENTITY_TYPES),
                                   edge_types=list(EDGE_TYPES), items=items)
            try:
                out = _parse_json(_chat(base_url, api_key, model, prompt, config.LLM_EXTRACT_TIMEOUT))
            except (urllib.error.URLError, RuntimeError, ValueError, json.JSONDecodeError) as e:
                print(f"batch {bi}: LLM 调用/解析失败，跳过该批（不写库，attempt 不登记=可重试）: {e}")
                continue
            stats = persist(conn, batch, out, vat_map)
            covered = set(stats.pop("covered_ids", []))
            batch_ids = [str(m["id"]) for m in batch]
            marked = mark_empty_attempts(attempts, batch_ids, covered, ts_now)
            if marked:
                save_attempts(conn, attempts)   # 每批落盘：中断续跑时登记不丢
            for k in total:
                if k == "by_etype":
                    for et, c in stats["by_etype"].items():
                        total["by_etype"][et] = total["by_etype"].get(et, 0) + c
                else:
                    total[k] += stats[k]
            print(f"batch {bi}: {json.dumps(stats, ensure_ascii=False)} 空输出登记 +{marked}")
        print(json.dumps({"total": total}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
