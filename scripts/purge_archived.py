#!/usr/bin/env python3
"""A2: archived TTL 清理（2026-09-16 v2）——archived 超 TTL 天 → purge 硬删流水。

前置硬闸（fail-closed，缺一拒绝）：
  G1 当日 pg 备份产物存在（外置备份目录 MEMORY_ENGINE_BACKUP_EXT，pg/ 最新 mtime=今日）
  G2 purge 批次 JSONL 已导出异盘（本脚本生成，空文件=拒绝）
  G3 引擎 /v1/health 四真（db/model_loaded/warm/ready）
依赖：仅 psql CLI + HTTP（零 PG 驱动依赖，与 daemon asyncpg 解耦）。
语义：archived=软删（隐藏不召回）已由生命周期完成；本脚本处理「archived 且 updated_at 超 TTL」→ 物理 purge。
纪律：dry-run 默认（--apply 才删）；DELETE 走 HTTP /v1/memories/{mid}?purge=true（禁直改 PG）；
     报告落引擎目录 purge_report/（BASE_DIR/purge_report）。
废弃条件：purge 误删健康条目→调大 TTL 或下线；连续 30 天 0 purge→降频。
"""
import argparse, glob, json, os, subprocess, sys, datetime, urllib.request, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from memory_engine import config  # noqa: E402

ENGINE = f"http://{config.HOST}:{config.PORT}"
ARCHIVE = os.environ.get("MEMORY_ENGINE_BACKUP_EXT", str(config.BACKUP_EXT_DIR))
PG_DIR = os.path.join(ARCHIVE, "pg")
PURGED_DIR = os.path.join(ARCHIVE, "purged")
PG_HOST = os.environ.get("PGHOST", "127.0.0.1")
PG_PORT = os.environ.get("PGPORT", "5433")
PG_USER = os.environ.get("PGUSER", "memengine")
PSQL = ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-t", "-A", "-v", "ON_ERROR_STOP=1"]
PSQL_ENV = dict(os.environ, PGPASSWORD="")

def psql(sql: str) -> list:
    r = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=60, env=PSQL_ENV)
    if r.returncode != 0:
        raise RuntimeError(f"psql 失败: {r.stderr[:150]}")
    return [l for l in r.stdout.splitlines() if l.strip()]

def gate_backup_today():
    today = datetime.date.today()
    latest = None
    for f in glob.glob(os.path.join(PG_DIR, "**", "*"), recursive=True):
        if os.path.isfile(f):
            m = datetime.date.fromtimestamp(os.path.getmtime(f))
            if m == today and (latest is None or os.path.getmtime(f) > latest[1]):
                latest = f
    return (latest is not None), (f"当日备份: {os.path.basename(latest)}" if latest else "当日无备份产物")

def gate_health():
    try:
        d = json.loads(urllib.request.urlopen(f"{ENGINE}/v1/health", timeout=5).read())
        four = all(d.get(k) is True for k in ("db", "model_loaded", "warm", "ready"))
        return four, f"status={d.get('status')} four_true={four}"
    except Exception as e:
        return False, f"health 异常: {str(e)[:60]}"

def fetch_targets(ttl_days):
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=ttl_days)).isoformat()
    rows = psql(
        "SELECT json_build_object('id', id, 'seq', seq, 'bank', bank, "
        "'title', left(coalesce(title,''),80), 'created_at', created_at)::text "
        f"FROM memories WHERE ttl_state='archived' AND updated_at < '{cutoff}' ORDER BY seq")
    return [json.loads(x) for x in rows]

def export_batch(targets, cutoff):
    os.makedirs(PURGED_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(PURGED_DIR, f"purged_batch_{ts}.jsonl")
    ids = ",".join("'" + t["id"] + "'" for t in targets)
    r = subprocess.run(PSQL + ["-c",
        f"COPY (SELECT json_build_object('id',id,'seq',seq,'bank',bank,'domain',domain,"
        f"'title',title,'body',body,'source_ref',source_ref,'created_at',created_at)::text "
        f"FROM memories WHERE id IN ({ids}) ORDER BY seq) TO STDOUT"],
        capture_output=True, text=True, timeout=120, env=PSQL_ENV)
    if r.returncode != 0:
        raise RuntimeError(f"COPY 导出失败: {r.stderr[:150]}")
    with open(out, "w", encoding="utf-8") as f:
        f.write(r.stdout)
    return out, os.path.getsize(out), len(r.stdout.strip().splitlines())

def purge_one(mid):
    req = urllib.request.Request(f"{ENGINE}/v1/memories/{mid}?purge=true", method="DELETE")
    urllib.request.urlopen(req, timeout=10)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--ttl-days", type=int, default=30)
    args = ap.parse_args()
    ok1, d1 = gate_backup_today()
    ok3, d3 = gate_health()
    print(f"[gate] G1 当日备份: {d1} -> {'PASS' if ok1 else 'FAIL'}")
    print(f"[gate] G3 健康四真: {d3} -> {'PASS' if ok3 else 'FAIL'}")
    if not (ok1 and ok3):
        print("FAIL: 前置硬闸未过（fail-closed），purge 拒绝")
        sys.exit(2)
    targets = fetch_targets(args.ttl_days)
    print(f"[scan] archived 超 {args.ttl_days} 天: {len(targets)} 条")
    if not targets:
        print("无目标，0 purge")
        return
    for t in targets[:10]:
        print(f"  {'[dry]' if not args.apply else '[hit]'} seq={t['seq']} {t['id'][:8]} {t['bank']}: {t['title']}")
    if not args.apply:
        print(f"DRY-RUN 完成：{len(targets)} 条将 purge（--apply 执行）")
        return
    out, sz, n_export = export_batch(targets, args.ttl_days)
    if n_export != len(targets) or sz == 0:
        print(f"FAIL: G2 导出不完整（目标{len(targets)} 导出{n_export}），purge 拒绝")
        sys.exit(2)
    print(f"[gate] G2 导出: {out} ({n_export} 条 {sz}B)")
    done, fails = 0, []
    for t in targets:
        try:
            purge_one(t["id"])
            done += 1
        except Exception as e:
            fails.append((t["seq"], str(e)[:60]))
        time.sleep(0.05)
    print(f"[purge] 完成 {done}/{len(targets)}，失败 {len(fails)}")
    for s, e in fails[:5]:
        print(f"  FAIL seq={s}: {e}")
    rpt_dir = str(config.BASE_DIR / "purge_report")
    os.makedirs(rpt_dir, exist_ok=True)
    rpt = os.path.join(rpt_dir, f"purge_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    json.dump({"purged": done, "failed": len(fails), "export_file": out,
               "ttl_days": args.ttl_days, "detail": fails}, open(rpt, "w"), ensure_ascii=False, indent=1)
    print(f"[report] {rpt}")
    sys.exit(0 if not fails else 1)

if __name__ == "__main__":
    main()
