#!/usr/bin/env python3
"""LongMemEval-S user turns → memory-engine LME 实例（8767）灌库。

口径：每个 user turn = 1 条记忆；bank=hermes-sessions；dedup=false（评测语料移植）；
tags=['lme', 'q:<qid>', 's:<session_id>']（flat 评测按 q: 过滤，全局评测不过滤）；
source_ref=官方 turn doc_id；original_date=session_date（转 ISO8601，供时效衰减）。
断点续跑：progress.json 记已提交偏移，启动时与 DB count 对账。
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ENGINE = "http://127.0.0.1:8767"
TURNS = os.path.expanduser("~/.hermes/memory-engine/eval/lme/turns.jsonl")
PROGRESS = os.path.expanduser("~/.hermes/memory-engine/eval/lme/ingest_progress.json")
BATCH = 32

DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2}).*?(\d{2}):(\d{2})")


def to_iso(s: str | None) -> str | None:
    if not s:
        return None
    m = DATE_RE.search(s)
    if not m:
        return None
    y, mo, d, h, mi = map(int, m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).isoformat()


def db_count() -> int:
    with urllib.request.urlopen(ENGINE + "/v1/health", timeout=30) as r:
        return int(json.loads(r.read())["pg"]["memories"])


def post_retain(items: list[dict]) -> dict:
    body = json.dumps({"bank": "hermes-sessions", "caller": "main",
                       "items": items, "dedup": False}, ensure_ascii=False).encode()
    req = urllib.request.Request(ENGINE + "/v1/retain", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if len(items) > 1:
            # 批内个别异常 → 降级单条，孤立坏样本不阻塞整批（无部分提交风险：闸先扫后插）
            ids = []
            for it in items:
                try:
                    r1 = post_retain([it])
                    ids += r1.get("ids", [])
                except urllib.error.HTTPError as e1:
                    with open(PROGRESS + ".rejects", "a") as rf:
                        rf.write(json.dumps({"status": e1.code, "detail": e1.read().decode()[:500],
                                             "source_ref": it.get("source_ref")}, ensure_ascii=False) + "\n")
            return {"ids": ids, "fallback": True}
        raise


def main() -> int:
    lines = open(TURNS, encoding="utf-8").readlines()
    prog = json.load(open(PROGRESS)) if os.path.exists(PROGRESS) else {"offset": 0, "skipped": 0}
    offset, skipped_seed = prog.get("offset", 0), prog.get("skipped", 0)
    cur = db_count()
    print(f"turns={len(lines)} resume_offset={offset} skipped_seed={skipped_seed} db_count={cur}", flush=True)
    if cur < offset - skipped_seed:
        print(f"FATAL: db({cur}) < offset-skipped({offset - skipped_seed}) — 进度文件与库不一致，人工核查", flush=True)
        return 1
    if cur > offset - skipped_seed:
        print(f"NOTE: db({cur}) > offset-skipped({offset - skipped_seed}) — 以 progress 为准继续", flush=True)

    t0, n_done = time.time(), 0
    skipped_total = skipped_seed
    with open(PROGRESS + ".log", "a") as logf:
        while offset < len(lines):
            batch_lines = lines[offset:offset + BATCH]
            items = []
            for ln in batch_lines:
                t = json.loads(ln)
                items.append({
                    "content": t["content"],
                    "context": f"LongMemEval-S haystack turn (qid={t['qid']}, session={t['session_id']})",
                    "title": (t["content"][:60] or "lme turn"),
                    "tags": ["lme", f"q:{t['qid']}", f"s:{t['session_id']}"],
                    "domain": "general",
                    "priority": 3,
                    "source_type": "eval",
                    "source_tier": "web",
                    "source_ref": t["doc_id"],
                    "owner": "main",
                    "visibility": "agent",
                    "original_date": to_iso(t.get("session_date")),
                })
            resp = post_retain(items)
            committed = len(resp.get("ids", []))
            skipped_batch = len(items) - committed
            skipped_total += skipped_batch
            offset += len(items)
            n_done += len(items)
            json.dump({"offset": offset, "skipped": skipped_total, "ts": time.time()},
                      open(PROGRESS, "w"))
            if n_done % (BATCH * 20) == 0:
                rate = n_done / (time.time() - t0)
                eta_min = (len(lines) - offset) / max(rate, 1) / 60
                db_now = db_count()
                invariant = (db_now == offset - skipped_total)
                msg = (f"offset={offset}/{len(lines)} skipped={skipped_total} db={db_now} "
                       f"invariant={'OK' if invariant else 'VIOLATED'} rate={rate:.0f}/s eta={eta_min:.0f}min")
                print(msg, flush=True)
                logf.write(msg + "\n")
                logf.flush()
    print(f"INGEST_DONE offset={offset} elapsed={(time.time()-t0)/60:.1f}min", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
