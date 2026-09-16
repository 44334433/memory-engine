#!/usr/bin/env python3
"""embed_ver 批量重嵌骨架（P1 第一批 2026-09-16；换模型时启用——当前为可运行骨架）。

后台任务式设计（不阻塞服务）：
- 分批扫描 embed_ver < --to-ver 的存量行（只读直连 PG），逐条走 daemon HTTP
  POST /v1/memories/{id}/reembed（常驻 provider 重嵌，写路径不绕过 daemon）；
- 每批间 sleep 限速，进度落盘 state/reembed_backfill.json，可断点续跑（Ctrl-C 安全）；
- 默认 dry-run（fail-closed），--apply 才真正执行。

用法：
  python3 scripts/reembed_backfill.py                       # dry-run：列出待重嵌数量
  python3 scripts/reembed_backfill.py --to-ver 2 --apply --batch 50 --sleep 0.2

TODO(P2 换模实拍板时补齐，骨架不预实现)：
- 维度 fail-closed：新模型 dim != 1024 时先 ALTER vector 列 + 重建 HNSW 索引 + 双写过渡，禁止直接混写；
- 失败重试/退避 + 失败清单告警（回投 Hermes）；
- 限速自适应（daemon P95 监控联动）；
- openai_compat provider 下的 query instruction 口径确认（网关模型卡）。
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from memory_engine import config  # noqa: E402
import psycopg  # noqa: E402

STATE_FILE = Path(config.BASE_DIR) / "state" / "reembed_backfill.json"


def fetch_stale_ids(dsn: str, to_ver: int, limit: int) -> list[str]:
    """只读直连 PG 扫 embed_ver 落后行（写路径仍走 daemon，禁 import 旁路不破）。"""
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM memories WHERE embed_ver < %s AND embedding IS NOT NULL "
                "AND ttl_state <> 'retired' ORDER BY seq LIMIT %s",
                (to_ver, limit),
            )
            return [str(r[0]) for r in cur.fetchall()]


def reembed_one(base: str, mid: str) -> dict:
    req = urllib.request.Request(f"{base}/v1/memories/{mid}/reembed", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser(description="embed_ver 批量重嵌（后台任务式骨架）")
    ap.add_argument("--to-ver", type=int, default=config.EMBED_VER)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--sleep", type=float, default=0.2, help="批间限速秒")
    ap.add_argument("--apply", action="store_true", help="实际执行（默认 dry-run）")
    args = ap.parse_args()

    base = f"http://127.0.0.1:{config.PORT}"
    done: list[str] = []
    if STATE_FILE.exists():
        done = json.loads(STATE_FILE.read_text()).get("done", [])
    done_set = set(done)

    ids = fetch_stale_ids(config.PG_DSN, args.to_ver, 100000)
    todo = [i for i in ids if i not in done_set]
    print(f"embed_ver < {args.to_ver} 待重嵌 {len(todo)} 条（已完成跳过 {len(done)}）"
          f"{' [dry-run]' if not args.apply else ' [APPLY]'}")
    if not args.apply:
        for i in todo[:10]:
            print(" would reembed:", i)
        return 0

    t0 = time.time()
    for n, mid in enumerate(todo, 1):
        try:
            reembed_one(base, mid)
            done.append(mid)
        except Exception as e:  # noqa: BLE001 —— 骨架：单条失败记日志续跑，重试/告警 TODO
            print(f" FAIL {mid}: {e}", file=sys.stderr)
        if n % args.batch == 0:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({"to_ver": args.to_ver, "done": done}))
            print(f" progress {n}/{len(todo)} elapsed={time.time()-t0:.0f}s")
            time.sleep(args.sleep)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"to_ver": args.to_ver, "done": done}))
    print(f"done: {len(done)} 条已重嵌，elapsed={time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
