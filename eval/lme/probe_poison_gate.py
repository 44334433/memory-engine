#!/usr/bin/env python3
"""投毒闸对 LongMemEval 真实语料的实测误报率探针（scope=all，单条 retain）。

产出：~/.hermes/memory-engine/eval/lme/poison_gate_probe.json
  {scope, n_attempted, n_flagged, n_passed, rate, flagged:[{doc_id,patterns,excerpt}]}
推进 ingest_progress.json offset（通过者已入库，tier=web）。
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
LME_DIR = os.path.expanduser("~/.hermes/memory-engine/eval/lme")
PROGRESS = f"{LME_DIR}/ingest_progress.json"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2}).*?(\d{2}):(\d{2})")


def to_iso(s):
    m = DATE_RE.search(s or "")
    if not m:
        return None
    y, mo, d, h, mi = map(int, m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).isoformat()


def main() -> int:
    lines = open(f"{LME_DIR}/turns.jsonl", encoding="utf-8").readlines()
    offset = json.load(open(PROGRESS))["offset"] if os.path.exists(PROGRESS) else 0
    flagged, passed, attempted = [], 0, 0
    t0 = time.time()
    while offset < len(lines) and attempted < N:
        t = json.loads(lines[offset])
        item = {"content": t["content"],
                "context": f"LongMemEval-S haystack turn (qid={t['qid']}, session={t['session_id']})",
                "title": (t["content"][:60] or "lme turn"),
                "tags": ["lme", f"q:{t['qid']}", f"s:{t['session_id']}"],
                "domain": "general", "priority": 3, "source_type": "eval",
                "source_tier": "web", "source_ref": t["doc_id"],
                "owner": "main", "visibility": "agent",
                "original_date": to_iso(t.get("session_date"))}
        body = json.dumps({"bank": "hermes-sessions", "caller": "main",
                           "items": [item], "dedup": False}, ensure_ascii=False).encode()
        req = urllib.request.Request(ENGINE + "/v1/retain", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        attempted += 1
        try:
            urllib.request.urlopen(req, timeout=120).read()
            passed += 1
            offset += 1
        except urllib.error.HTTPError as e:
            if e.code == 422:
                d = json.loads(e.read())
                det = d.get("detail", {})
                for it in det.get("items", []):
                    flagged.append({"doc_id": t["doc_id"], "qid": t["qid"],
                                    "patterns": it.get("patterns"),
                                    "excerpt": t["content"][:200]})
                # 422 = 未入库，跳过该条推进 offset（避免死循环），计入误报
                offset += 1
            else:
                raise
        if attempted % 200 == 0:
            json.dump({"offset": offset, "ts": time.time()}, open(PROGRESS, "w"))
            print(f"attempted={attempted} flagged={len(flagged)} rate={len(flagged)/attempted:.1%} "
                  f"({time.time()-t0:.0f}s)", flush=True)
    json.dump({"offset": offset, "ts": time.time()}, open(PROGRESS, "w"))
    result = {"scope": "all", "tier": "web", "n_attempted": attempted, "n_flagged": len(flagged),
              "n_passed": passed, "false_positive_rate": round(len(flagged) / max(attempted, 1), 4),
              "elapsed_s": round(time.time() - t0, 1), "flagged": flagged}
    json.dump(result, open(f"{LME_DIR}/poison_gate_probe.json", "w"), indent=2, ensure_ascii=False)
    print(json.dumps({k: v for k, v in result.items() if k != "flagged"}, ensure_ascii=False))
    print(f"PROBE_DONE offset={offset}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
