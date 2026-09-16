"""CLI 入口（~/.user/bin/memory-engine 薄壳调用，蓝图 §8.1 + 阶段2 §7.1）。

运维组：serve / backup / wait-ready（systemd 用）。
记忆管理组（阶段2，全部 HTTP 调 daemon，禁 import 旁路/禁直连 DB）：
  list / search / get / delete / adopt / retrain / stats / lifecycle / consolidate
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


# —————————————————— HTTP 基座 ——————————————————

def _base_candidates() -> list[str]:
    import os
    env = os.environ.get("MEMORY_ENGINE_PORT")
    ports = [int(env)] if env else [config.PORT]
    for p in (8766, 8765):                     # 裁决=8766；8765 为阶段1暂绑历史，兜底探测
        if p not in ports:
            ports.append(p)
    return [f"http://{config.HOST}:{p}" for p in ports]


def _resolve_base() -> str:
    for base in _base_candidates():
        try:
            urllib.request.urlopen(f"{base}/v1/health", timeout=2).read()
            return base
        except Exception:
            continue
    raise SystemExit(f"memory-engine daemon 不可达（ tried: {_base_candidates()}）")


def http(method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    base = _resolve_base()
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", e.reason)
        except Exception:
            detail = e.reason
        raise SystemExit(f"HTTP {e.code}: {detail}")


def _print_rows(rows: list[dict], cols: tuple) -> None:
    if not rows:
        print("(空)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, ""))[:widths[c]].ljust(widths[c]) for c in cols))


# —————————————————— 运维组（阶段1） ——————————————————

def cmd_serve(args) -> int:
    import uvicorn
    from .app import create_app
    host = args.host or config.HOST
    port = args.port or config.PORT
    uvicorn.run(create_app(), host=host, port=port, log_level="info", access_log=False)
    return 0


def cmd_backup(args) -> int:
    """优先走 daemon /v1/admin/backup；daemon 不可达时直连 pg_dump 兜底。"""
    try:
        out = http("POST", "/v1/admin/backup", timeout=1800)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except SystemExit as e:
        print(f"daemon 不可达({e})，直连 pg_dump 兜底", file=sys.stderr)
        from .backup import run_backup
        from .db import PgPool
        pool = PgPool(config.PG_DSN, 1, 2)
        try:
            print(json.dumps(run_backup(pool, trigger="fallback-cli"), ensure_ascii=False, indent=2))
            return 0
        finally:
            pool.close()


def cmd_wait_ready(args) -> int:
    """ExecStartPost：轮询 /v1/health 至 db+model+warm 全真（蓝图 §8.2-5）。"""
    url = f"http://{config.HOST}:{config.PORT}/v1/health"
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            d = json.loads(urllib.request.urlopen(url, timeout=3).read())
            if d.get("db") and d.get("model_loaded") and d.get("warm"):
                print("READY", json.dumps({"uptime_s": d.get("uptime_s"), "status": d.get("status")}))
                return 0
        except Exception:
            pass
        time.sleep(1)
    print(f"TIMEOUT: {args.timeout}s 内未就绪", file=sys.stderr)
    return 1


# —————————————————— 记忆管理组（阶段2，薄壳→HTTP） ——————————————————

def cmd_list(args) -> int:
    qs = f"?limit={args.limit}"
    if args.bank: qs += f"&bank={args.bank}"
    if args.state: qs += f"&state={args.state}"
    if args.owner: qs += f"&owner={args.owner}"
    if args.domain: qs += f"&domain={args.domain}"
    if args.q: qs += f"&q={urllib.parse.quote(args.q)}"
    out = http("GET", f"/v1/memories{qs}")
    print(f"total={out['total']} shown={len(out['items'])}")
    _print_rows(out["items"], ("id", "bank", "ttl_state", "priority", "access_count", "adopt_count", "title"))
    return 0


def cmd_search(args) -> int:
    body = {"query": args.query, "top_k": args.top_k, "caller": "cli"}
    if args.bank:
        body["bank"] = args.bank
    out = http("POST", "/v1/recall", body)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"took={out.get('took_ms')}ms degraded={out.get('degraded')} hits={len(out.get('results', []))}")
    for r in out.get("results", []):
        print(f"[{r.get('score', 0):.4f}] {r['id']} ({r.get('bank')}/{r.get('ttl_state')}) {r.get('title')}")
        print(f"        {(r.get('body') or '')[:120].replace(chr(10), ' ')}")
    return 0


def cmd_get(args) -> int:
    out = http("GET", f"/v1/memories/{args.id}")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_delete(args) -> int:
    qs = "?purge=true" if args.force else ""
    out = http("DELETE", f"/v1/memories/{args.id}{qs}")
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_adopt(args) -> int:
    out = http("POST", f"/v1/memories/{args.id}/adopt?caller={args.caller}")
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_retrain(args) -> int:
    """单条重嵌：POST /v1/memories/{id}/reembed（不改正文，仅重算向量）。"""
    out = http("POST", f"/v1/memories/{args.id}/reembed", timeout=120)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_stats(args) -> int:
    h = http("GET", "/v1/health")
    lc = http("GET", "/v1/lifecycle")
    print(json.dumps({"status": h.get("status"), "version": h.get("version"),
                      "uptime_s": h.get("uptime_s"), "pg": h.get("pg"),
                      "embed": h.get("embed"), "states": lc.get("states"),
                      "lifecycle_thread": lc.get("thread_alive"),
                      "wal": lc.get("wal")}, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_lifecycle(args) -> int:
    if args.action == "run":
        out = http("POST", "/v1/lifecycle/run", {"dry_run": args.dry_run})
    elif args.action == "candidates":
        out = http("GET", "/v1/lifecycle/candidates")
    elif args.action == "history":
        out = http("GET", f"/v1/lifecycle/history?limit={args.limit}")
    else:
        out = http("GET", "/v1/lifecycle")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_transition(args) -> int:
    out = http("POST", "/v1/lifecycle/transition",
               {"memory_id": args.id, "action": args.to, "reason": args.reason})
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_consolidate(args) -> int:
    if args.action == "run":
        body = {}
        if args.days: body["days"] = args.days
        if args.sim: body["sim"] = args.sim
        body["dry_run"] = args.dry_run
        out = http("POST", "/v1/consolidate", body)
        print(json.dumps(out, ensure_ascii=False))
        if args.wait:
            time.sleep(1)
            out = http("GET", f"/v1/consolidate/{out['operation_id']}")
            for _ in range(120):
                if out.get("status") in ("done", "failed"):
                    break
                time.sleep(1)
                out = http("GET", f"/v1/consolidate/{out['operation_id']}")
            print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        out = http("GET", "/v1/consolidate")
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="memory-engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("serve", help="启动 daemon（uvicorn 单 worker，嵌入模型常驻 CUDA）")
    ps.add_argument("--host", default=None)
    ps.add_argument("--port", type=int, default=None)
    ps.set_defaults(fn=cmd_serve)

    pb = sub.add_parser("backup", help="触发一次备份（daemon 优先，pg_dump 直连兜底）")
    pb.set_defaults(fn=cmd_backup)

    pw = sub.add_parser("wait-ready", help="等待 daemon 预热完成（ExecStartPost 用）")
    pw.add_argument("--timeout", type=int, default=90)
    pw.set_defaults(fn=cmd_wait_ready)

    pl = sub.add_parser("list", help="列出记忆（GET /v1/memories）")
    pl.add_argument("--bank"); pl.add_argument("--state"); pl.add_argument("--owner")
    pl.add_argument("--domain"); pl.add_argument("--q")
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(fn=cmd_list)

    pse = sub.add_parser("search", help="语义检索（POST /v1/recall）")
    pse.add_argument("query"); pse.add_argument("--bank")
    pse.add_argument("--top-k", type=int, default=10); pse.add_argument("--json", action="store_true")
    pse.set_defaults(fn=cmd_search)

    pg = sub.add_parser("get", help="单条详情（GET /v1/memories/{id}）")
    pg.add_argument("id"); pg.set_defaults(fn=cmd_get)

    pd = sub.add_parser("delete", help="删除（默认软删=retired；--force 硬删）")
    pd.add_argument("id"); pd.add_argument("--force", action="store_true")
    pd.set_defaults(fn=cmd_delete)

    pa = sub.add_parser("adopt", help="采纳回执（准入闸信号）")
    pa.add_argument("id"); pa.add_argument("--caller", default="cli")
    pa.set_defaults(fn=cmd_adopt)

    pr = sub.add_parser("retrain", help="单条重嵌（POST /v1/memories/{id}/reembed）")
    pr.add_argument("id"); pr.set_defaults(fn=cmd_retrain)

    pst = sub.add_parser("stats", help="引擎健康+状态分布（/v1/health + /v1/lifecycle）")
    pst.set_defaults(fn=cmd_stats)

    plc = sub.add_parser("lifecycle", help="生命周期查询/触发（status|run|candidates|history）")
    plc.add_argument("action", choices=["status", "run", "candidates", "history"], nargs="?", default="status")
    plc.add_argument("--dry-run", action="store_true"); plc.add_argument("--limit", type=int, default=50)
    plc.set_defaults(fn=cmd_lifecycle)

    ptr = sub.add_parser("transition", help="人工生命周期转换（promote/demote/archive/revive/trialize）")
    ptr.add_argument("id"); ptr.add_argument("to"); ptr.add_argument("--reason", default="")
    ptr.set_defaults(fn=cmd_transition)

    pco = sub.add_parser("consolidate", help="整合（run|list）")
    pco.add_argument("action", choices=["run", "list"], nargs="?", default="list")
    pco.add_argument("--days", type=int); pco.add_argument("--sim", type=float)
    pco.add_argument("--dry-run", action="store_true"); pco.add_argument("--wait", action="store_true")
    pco.set_defaults(fn=cmd_consolidate)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
