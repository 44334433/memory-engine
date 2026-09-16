"""手写迁移 runner（P1 第一批起；P2#9 迁移机制化前的最小实现）。

用法：
  python3 scripts/migrate.py            # dry-run：列出待应用迁移 + 打印 SQL（默认，fail-closed）
  python3 scripts/migrate.py --apply    # 实际执行（按文件名序逐个应用，engine_meta.migrations 登记防重）

约定：
- 迁移文件 = scripts/migrations/NNN_*.sql，文件名序即应用序；
- SQL 本身必须幂等（IF NOT EXISTS / 幂等 UPDATE），登记仅为运维可见性，非唯一防线；
- 应用记录写 engine_meta(key='migrations', value=jsonb 数组)。
"""
import argparse
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from memory_engine import config  # noqa: E402
import psycopg  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def applied_versions(conn) -> list[str]:
    row = psycopg_client_fetch(conn)
    return row if row else []


def psycopg_client_fetch(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM engine_meta WHERE key='migrations'")
        row = cur.fetchone()
    if not row:
        return []
    v = row[0]
    return v if isinstance(v, list) else []


def main() -> int:
    ap = argparse.ArgumentParser(description="memory-engine 手写迁移 runner")
    ap.add_argument("--apply", action="store_true", help="实际执行（默认 dry-run）")
    args = ap.parse_args()

    files = sorted(MIGRATIONS_DIR.glob("[0-9]*.sql"))
    if not files:
        print("无迁移文件")
        return 0
    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        applied = applied_versions(conn)
        pending = [f for f in files if f.name not in applied]
        print(f"已应用: {applied or '∅'}")
        print(f"待应用: {[f.name for f in pending] or '∅'}")
        for f in pending:
            sql = f.read_text(encoding="utf-8")
            if not args.apply:
                print(f"----- dry-run: {f.name} -----\n{sql}")
                continue
            print(f" applying {f.name} ...", flush=True)
            with conn.transaction():
                conn.execute(sql)   # 多语句事务整体应用，失败即回滚
                cur_applied = applied_versions(conn)
                cur_applied.append(f.name)
                conn.execute(
                    "INSERT INTO engine_meta(key, value) VALUES ('migrations', %s::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (json.dumps(cur_applied),),
                )
            print(f" OK: {f.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
