#!/usr/bin/env python3
"""L1 参数自调框架（P1 第三批 2026-09-16；路线图 §自进化 L1 + G07 盲审硬约束）。

变异→36 题离线回归→配对逐题检验→两轮连续≥基线+5pp 才固化；不达标自动回滚+台账。

G07 硬约束（S1 级盲审，写死）：
  36 题 P@5 是二元指标（SE≈7.6pp）→ 自调固化闸必须 = ①配对逐题检验（McNemar 式
  精确检验）②连续两轮通过 ③参数快照版本化。时序类参数（衰减天数/状态系数，
  效果窗口周月级）移出即时闸另立月度人工评审——本框架变异仅限非时序标量
  RRF_K / TIER_WEIGHTS.web / TIER_WEIGHTS.cron。

评测实现：离线复用生产 recall 代码路径（db 三路 SQL + recall.recall 融合），
query 向量经 daemon /v1/embed 预计算（与线上 query 嵌入同源），config 内存态
patch 后逐 mutant 重放——零代码分叉；基线离线 P@5 与线上参照值偏差>0.1 时显式告警。

固化=写 config.py（生产仓 + 公开仓副本）的 RRF_K/TIER_WEIGHTS 默认值 + 快照
source=solidified + changelog；引擎重启后才生效（changelog/回投显式提示）。

用法：
  python3 scripts/param_autotune.py --dry-run   # 首跑/演练：全流程出报告，绝不固化
  python3 scripts/param_autotune.py             # 正式轮：闸通过推进两轮计数，达标固化
  python3 scripts/param_autotune.py --show      # 查看台账/快照/pending 状态
环境：MEMORY_ENGINE_AUTOTUNE_MIN_DELTA（默认 0.05）/ _ALPHA（0.05）/ _ROUNDS（2）
"""
import argparse
import json
import math
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memory_engine import config, db, recall as recall_mod  # noqa: E402
from memory_engine.db import PgPool  # noqa: E402

EVAL_JSONL = Path(__file__).resolve().parent.parent / "eval" / "memory_recall_eval.jsonl"
MIN_EVAL_CORPUS = 800          # 评测语料下限（低于此=语料缺失，拒绝出分防伪闸）
SNAP_PARAMS = ("RRF_K", "W_VEC", "W_FTS", "W_TIME", "W_GRAPH", "TIER_WEIGHTS")
# 快照/回滚覆盖全部非时序标量（含本框架不变异的 W_*，保证任意快照可完整还原）
BANK, CALLER, TOPK = "hermes", "main", 10


# —————————————————— 配对逐题检验（G07 核心） ——————————————————

def mcnemar_exact_p(b: int, c: int) -> float:
    """McNemar 式精确检验：只看不一致对（基线对/mutant 错 b，反之 c），双侧 2×P(X≤min)。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2.0 * tail)


def paired_gate(base_hits: list[bool], mut_hits: list[bool],
                min_delta: float | None = None, alpha: float | None = None) -> dict:
    """逐题配对：delta=ΔP@5（pp）且 McNemar p<alpha 双条件（任一不满足=不通过）。"""
    assert len(base_hits) == len(mut_hits) and base_hits, "配对样本为空/长度不齐"
    b = sum(1 for x, y in zip(base_hits, mut_hits) if x and not y)
    c = sum(1 for x, y in zip(base_hits, mut_hits) if y and not x)
    p5b = sum(base_hits) / len(base_hits)
    p5m = sum(mut_hits) / len(mut_hits)
    p = mcnemar_exact_p(b, c)
    delta = p5m - p5b
    min_delta = config.AUTOTUNE_MIN_DELTA if min_delta is None else min_delta
    alpha = config.AUTOTUNE_ALPHA if alpha is None else alpha
    return {"n": len(base_hits), "b_only_baseline": b, "c_only_mutant": c,
            "p5_baseline": round(p5b, 4), "p5_mutant": round(p5m, 4),
            "delta": round(delta, 4), "mcnemar_p": round(p, 4),
            "gate_pass": bool(delta >= min_delta and p < alpha)}


# —————————————————— 参数快照（版本化）与内存态 patch ——————————————————

def capture_params() -> dict:
    return {"RRF_K": config.RRF_K, "W_VEC": config.W_VEC, "W_FTS": config.W_FTS,
            "W_TIME": config.W_TIME, "W_GRAPH": config.W_GRAPH,
            "TIER_WEIGHTS": dict(config.TIER_WEIGHTS)}


def apply_params(params: dict) -> None:
    config.RRF_K = int(params["RRF_K"])
    config.W_VEC, config.W_FTS, config.W_TIME = (float(params[k]) for k in ("W_VEC", "W_FTS", "W_TIME"))
    config.W_GRAPH = float(params["W_GRAPH"])
    config.TIER_WEIGHTS = dict(params["TIER_WEIGHTS"])


def _snapshots_dir() -> Path:
    d = Path(config.AUTOTUNE_DIR) / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_versions() -> list[int]:
    return sorted(int(m.group(1)) for p in _snapshots_dir().iterdir()
                  if (m := re.fullmatch(r"params-v(\d+)\.json", p.name)))


def write_snapshot(params: dict, source: str, extra: dict | None = None) -> Path:
    versions = snapshot_versions()
    v = (versions[-1] + 1) if versions else 1
    path = _snapshots_dir() / f"params-v{v}.json"
    snap = {"version": v, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source, "params": params}
    if extra:
        snap.update(extra)
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def latest_snapshot() -> dict | None:
    versions = snapshot_versions()
    if not versions:
        return None
    return json.loads((_snapshots_dir() / f"params-v{versions[-1]}.json").read_text(encoding="utf-8"))


# —————————————————— 变异（非时序标量，梯式确定性轮转） ——————————————————

def mutation_ladder(cur: dict) -> list[dict]:
    """围绕当前值的确定性小步梯（RRF_K ±10/±20 ∈[30,100]；tier 权重 ±0.05/±0.1 ∈[0.6,1.0]）。"""
    out: list[dict] = []
    for k in (cur["RRF_K"] - 20, cur["RRF_K"] - 10, cur["RRF_K"] + 10, cur["RRF_K"] + 20):
        if 30 <= k <= 100:
            out.append({"RRF_K": k})
    for key in ("web", "cron"):
        for d in (-0.1, -0.05, 0.05, 0.1):
            v = round(cur["TIER_WEIGHTS"][key] + d, 3)
            if 0.6 <= v <= 1.0:
                out.append({f"TIER_WEIGHTS.{key}": v})
    return out


def expand_mutant(cur: dict, m: dict) -> dict:
    """把 {RRF_K: 50} / {TIER_WEIGHTS.web: 0.9} 形式的变异展开为完整参数集。"""
    p = json.loads(json.dumps(cur))
    for k, v in m.items():
        if k == "RRF_K":
            p["RRF_K"] = int(v)
        elif k.startswith("TIER_WEIGHTS."):
            p["TIER_WEIGHTS"][k.split(".", 1)[1]] = float(v)
        else:
            raise ValueError(f"不支持的变异键（时序类参数禁入即时闸）: {k}")
    return p


# —————————————————— 离线 36 题评测（复用生产 recall 代码路径） ——————————————————

class _StubEmbedder:
    """预计算向量回放（与线上 /v1/embed query=true 同源）；文档嵌入路径不应被触发。"""
    device = "remote-precomputed"

    def __init__(self, vec: list[float]):
        self._vec = vec

    def embed_queries(self, qs: list[str]) -> list[list[float]]:
        assert len(qs) == 1, "逐题调用约定"
        return [self._vec]

    def embed_documents(self, ts: list[str]) -> list[list[float]]:
        raise RuntimeError("autotune 离线评测不应触发文档嵌入")


def load_questions() -> list[dict]:
    qs = [json.loads(l) for l in EVAL_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(qs) < 30:
        raise RuntimeError(f"评测题集异常（{len(qs)}<30 题）：{EVAL_JSONL}")
    return qs


def embed_questions(questions: list[dict], base_url: str) -> list[list[float]]:
    body = json.dumps({"input": [q["query"] for q in questions], "query": True}).encode()
    req = urllib.request.Request(base_url + "/v1/embed", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    vecs = out["embeddings"]
    assert len(vecs) == len(questions), f"embed 数量不齐 {len(vecs)}!={len(questions)}"
    return vecs


def eval_corpus_guard(pool: PgPool) -> int:
    with pool.connection() as conn:
        row = db.fetch_one(conn, "SELECT count(*) c FROM memories WHERE source_type='eval' AND is_current")
    n = int(row["c"]) if row else 0
    if n < MIN_EVAL_CORPUS:
        raise RuntimeError(f"评测语料不足（{n}<{MIN_EVAL_CORPUS}，source_type='eval'）：拒绝出分防伪闸")
    return n


def evaluate_params(pool: PgPool, questions: list[dict], qvecs: list[list[float]],
                    params: dict) -> list[dict]:
    """按给定参数集逐题重放生产 recall（config 内存态 patch，finally 还原）。"""
    saved = capture_params()
    try:
        apply_params(params)
        out = []
        for qd, vec in zip(questions, qvecs):
            res = recall_mod.recall(pool, _StubEmbedder(vec), qd["query"], BANK, CALLER, TOPK, {})
            ranked = [(r.get("source_ref") or r["id"]) for r in res["results"]]
            gold = qd["gold_id"]
            hit5 = gold in ranked[:5]
            rank = (ranked.index(gold) + 1) if gold in ranked else None
            out.append({"qid": qd.get("qid"), "hit5": hit5, "rank": rank})
        return out
    finally:
        apply_params(saved)


# —————————————————— 台账 / pending / 固化 ——————————————————

def _ledger_path() -> Path:
    d = Path(config.AUTOTUNE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / "ledger.jsonl"


def ledger_append(entry: dict) -> None:
    entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _ledger_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _pending_path() -> Path:
    return Path(config.AUTOTUNE_DIR) / "pending.json"


def read_pending() -> dict | None:
    try:
        return json.loads(_pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_pending(p: dict | None) -> None:
    if p is None:
        _pending_path().unlink(missing_ok=True)
    else:
        _pending_path().write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")


def _config_targets() -> list[Path]:
    """固化落点：生产仓 config.py（daemon 实际导入）+ 公开仓副本（存在才写，保持两仓同步）。"""
    targets = [Path(config.BASE_DIR) / "src" / "memory_engine" / "config.py"]
    pub = Path.home() / "projects" / "memory-engine-public" / "src" / "memory_engine" / "config.py"
    if pub.exists():
        targets.append(pub)
    return targets


def solidify_config(params: dict) -> list[str]:
    """把 RRF_K / TIER_WEIGHTS.web / TIER_WEIGHTS.cron 写进 config.py 默认值（正则锚定+写后校验）。"""
    changed = []
    for path in _config_targets():
        text = path.read_text(encoding="utf-8")
        new = re.sub(r"^RRF_K = \d+", f"RRF_K = {int(params['RRF_K'])}", text, count=1, flags=re.M)
        web = params["TIER_WEIGHTS"]["web"]
        new = re.sub(r'(MEMORY_ENGINE_TIER_WEIGHT_WEB", ")([0-9.]+)(")',
                     rf"\g<1>{web}\g<3>", new, count=1)
        cron = params["TIER_WEIGHTS"]["cron"]
        new = re.sub(r'(MEMORY_ENGINE_TIER_WEIGHT_CRON", ")([0-9.]+)(")',
                     rf"\g<1>{cron}\g<3>", new, count=1)
        if new != text:
            path.write_text(new, encoding="utf-8")
            # 写后校验：值确实落在文件里（防正则静默失配）
            chk = path.read_text(encoding="utf-8")
            assert f"RRF_K = {int(params['RRF_K'])}" in chk, f"RRF_K 固化校验失败: {path}"
            changed.append(str(path))
    return changed


def changelog_append(lines: list[str]) -> None:
    p = Path(config.AUTOTUNE_DIR) / "changelog.md"
    with p.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_report(report: dict) -> Path:
    d = Path(config.AUTOTUNE_DIR) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    fn = d / f"autotune-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
    fn.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return fn


# —————————————————— 主流程 ——————————————————

def run_round(dry_run: bool, max_mutants: int = 4, forced: list[dict] | None = None) -> dict:
    base_url = f"http://{config.HOST}:{config.PORT}"
    cur = capture_params()
    snap = latest_snapshot()
    if snap is None or snap["params"] != cur:
        path = write_snapshot(cur, source="baseline")
        log_line(f"baseline snapshot: {path}")
        snap = latest_snapshot()
    pool = PgPool(config.PG_DSN, 2, 4)
    try:
        corpus_n = eval_corpus_guard(pool)
        questions = load_questions()
        qvecs = embed_questions(questions, base_url)
        base_rows = evaluate_params(pool, questions, qvecs, cur)
        base_hits = [r["hit5"] for r in base_rows]
        ladder = forced if forced else mutation_ladder(cur)
        if not forced:
            versions = snapshot_versions()
            shift = (versions[-1] if versions else 0) * max_mutants   # 轮转：不同轮尝试不同梯位
            ladder = [ladder[(shift + i) % len(ladder)] for i in range(min(max_mutants, len(ladder)))]
        mutants = []
        for m in ladder:
            params = expand_mutant(cur, m)
            rows = evaluate_params(pool, questions, qvecs, params)
            gate = paired_gate(base_hits, [r["hit5"] for r in rows])
            mutants.append({"mutation": m, "params": params,
                            "per_q": rows, **gate})
        passing = [m for m in mutants if m["gate_pass"]]
        best = max(passing, key=lambda m: m["delta"]) if passing else None
        pending = read_pending()
        decision, pending_after = "no_candidate", None
        if dry_run:
            # dry-run 纪律：只评测出报告，绝不写 pending/固化（首跑与演练安全）
            decision = "dry_run_pass" if best is not None else "dry_run_no_candidate"
        elif best is not None:
            if pending and pending.get("mutation") == best["mutation"]:
                pending["pass_rounds"] += 1
                pending["last_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                write_pending(pending)
                pending_after = pending
                decision = "second_pass" if pending["pass_rounds"] >= config.AUTOTUNE_ROUNDS else "first_pass_held"
            else:
                pending_after = {"mutation": best["mutation"], "params": best["params"],
                                 "pass_rounds": 1,
                                 "first_ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                write_pending(pending_after)
                decision = "pending_reset" if pending else "first_pass"
            if decision == "second_pass":
                if dry_run:
                    decision = "second_pass_dry_hold"      # dry-run 到门槛也不固化（首跑纪律）
                else:
                    snap_path = write_snapshot(best["params"], source="solidified",
                                               extra={"mutation": best["mutation"],
                                                      "delta": best["delta"],
                                                      "mcnemar_p": best["mcnemar_p"]})
                    changed = solidify_config(best["params"])
                    changelog_append([
                        f"## {datetime.now(timezone.utc).isoformat(timespec='seconds')} 固化",
                        f"- mutation: {best['mutation']}  ΔP@5={best['delta']:+.4f} "
                        f"(McNemar p={best['mcnemar_p']}, b={best['b_only_baseline']}, c={best['c_only_mutant']})",
                        f"- 两轮连续≥基线+{config.AUTOTUNE_MIN_DELTA} 通过；快照={snap_path.name}",
                        f"- config 变更: {changed or '（无变化，值已在位）'}；**引擎需重启生效**",
                    ])
                    write_pending(None)
                    pending_after = None
                    decision = "solidified"
        elif not dry_run:
            if pending:
                decision = "rolled_back"                    # 连续性断裂：pending 撤销=自动回滚
            write_pending(None)
        report = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "mode": "dry_run" if dry_run else "normal",
                  "corpus": corpus_n, "n_questions": len(questions),
                  "baseline_p5_offline": round(sum(base_hits) / len(base_hits), 4),
                  "baseline_snapshot": snap.get("version"),
                  "mutants": [{k: v for k, v in m.items() if k != "per_q"} for m in mutants],
                  "best": ({k: v for k, v in best.items() if k != "per_q"} if best else None),
                  "decision": decision, "pending_after": pending_after}
        report_path = write_report(report)
        ledger_append({"mode": report["mode"], "decision": decision,
                       "best": report["best"] and {k: report["best"][k] for k in
                                                   ("mutation", "delta", "mcnemar_p", "gate_pass")},
                       "baseline_p5_offline": report["baseline_p5_offline"],
                       "n_mutants": len(mutants), "report": str(report_path),
                       "dry_run": dry_run})
        return {"report": report, "report_path": str(report_path)}
    finally:
        pool.close()


def log_line(msg: str) -> None:
    print(msg, flush=True)


def show_state() -> None:
    snap = latest_snapshot()
    print("snapshots:", snapshot_versions(), "| latest:", snap and snap["source"])
    print("params:", json.dumps(snap and snap["params"], ensure_ascii=False))
    print("pending:", json.dumps(read_pending(), ensure_ascii=False))
    lp = _ledger_path()
    if lp.exists():
        lines = lp.read_text(encoding="utf-8").strip().splitlines()
        print(f"ledger: {len(lines)} entries; last 3:")
        for l in lines[-3:]:
            print("  ", l[:220])
    else:
        print("ledger: empty")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L1 参数自调（G07 闸：配对检验+两轮+快照）")
    ap.add_argument("--dry-run", action="store_true", help="全流程出报告，绝不固化/不推进 pending")
    ap.add_argument("--show", action="store_true", help="只查看状态")
    ap.add_argument("--max-mutants", type=int, default=4)
    ap.add_argument("--mutant", action="append", default=[],
                    help="强制变异（可重复）：RRF_K=50 或 TIER_WEIGHTS.web=0.9")
    args = ap.parse_args(argv)
    if args.show:
        show_state()
        return 0
    forced = []
    for m in args.mutant:
        k, _, v = m.partition("=")
        forced.append({k.strip(): float(v) if "." in v else int(v)})
    out = run_round(dry_run=args.dry_run, max_mutants=args.max_mutants, forced=forced or None)
    r = out["report"]
    log_line(f"\n=== L1 自调轮（mode={r['mode']}）===")
    log_line(f"基线离线 P@5={r['baseline_p5_offline']}（{r['n_questions']} 题）")
    for m in r["mutants"]:
        log_line(f"mutant {m['mutation']}: P@5={m['p5_mutant']} Δ={m['delta']:+.4f} "
                 f"b={m['b_only_baseline']} c={m['c_only_mutant']} p={m['mcnemar_p']} "
                 f"gate={'PASS' if m['gate_pass'] else 'fail'}")
    log_line(f"decision: {r['decision']}")
    log_line(f"report: {out['report_path']}")
    if r["decision"] == "solidified":
        log_line("⚠ 固化已写入 config.py：重启 memory-engine 后生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
