"""备份：pg_dump -Fc + pg_restore --list 校验 + 行数对账 + 双层滚动（蓝图 §10）。
WAL 归档为本地目录（archive_command 规避 udisks 跨用户权限，见 deploy/pg/stage1_install.sh），
本模块负责 WAL 归档→异盘 rsync 与滚动清理。
"""
import logging
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import config, db
from .db import PgPool

log = logging.getLogger("memory-engine.backup")

WAL_ARCHIVE_LOCAL = Path("/var/lib/postgresql/wal_archive_memengine")


def _prune(dumps_dir: Path, keep_days: int) -> int:
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for p in dumps_dir.glob("memengine-*.dump"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError as e:
            log.warning("prune skip %s: %s", p, e)
    return removed


def _prune_wal(d: Path, keep_days: int) -> int:
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for p in d.iterdir() if d.is_dir() else []:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError as e:
            log.warning("wal prune skip %s: %s", p, e)
    return removed


def _sync_wal_to_ext() -> dict:
    """本地 WAL 归档 → 异盘（rsync 无 -o/-g：落盘文件归 qq，便于后续滚动清理）。"""
    if not WAL_ARCHIVE_LOCAL.is_dir():
        return {"synced": 0, "note": "no local wal archive dir"}
    ext_wal = config.BACKUP_EXT_DIR.parent / "wal"
    ext_wal.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["rsync", "-a", str(WAL_ARCHIVE_LOCAL) + "/", str(ext_wal) + "/"],
        capture_output=True, timeout=600,
    )
    if r.returncode != 0:
        return {"synced": 0, "error": r.stderr.decode()[-300:]}
    n = len(list(ext_wal.glob("0*")))
    return {"synced": n, "ext_dir": str(ext_wal)}


def run_backup(pool: PgPool, trigger: str = "manual") -> dict:
    t0 = time.perf_counter()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    config.BACKUP_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    local = config.BACKUP_LOCAL_DIR / f"memengine-{ts}.dump"
    subprocess.run(
        ["pg_dump", "-h", "127.0.0.1", "-p", "5433", "-U", "memengine",
         "-d", "memengine", "-Fc", "-f", str(local)],
        check=True, timeout=1800, capture_output=True,
    )
    r = subprocess.run(["pg_restore", "--list", str(local)], capture_output=True, timeout=300)
    verified = (r.returncode == 0) and (b"memories" in r.stdout)

    counts = {}
    with pool.connection() as conn:
        counts["memories"] = (db.fetch_one(conn, "SELECT count(*) c FROM memories") or {"c": 0})["c"]
        counts["changelog"] = (db.fetch_one(conn, "SELECT count(*) c FROM changelog") or {"c": 0})["c"]

    ext_path, ext_err = None, None
    try:
        config.BACKUP_EXT_DIR.mkdir(parents=True, exist_ok=True)
        ext_path = config.BACKUP_EXT_DIR / local.name
        shutil.copy2(local, ext_path)
    except Exception as e:
        ext_err = str(e)

    wal = _sync_wal_to_ext()
    local_removed = _prune(config.BACKUP_LOCAL_DIR, config.BACKUP_KEEP_LOCAL_DAYS)
    ext_removed = _prune(config.BACKUP_EXT_DIR, config.BACKUP_KEEP_EXT_DAYS)
    _prune_wal(WAL_ARCHIVE_LOCAL, 7)

    out = {
        "trigger": trigger,
        "file": str(local),
        "size_bytes": local.stat().st_size if local.exists() else 0,
        "verified": verified,
        "counts": counts,
        "ext_path": str(ext_path) if ext_path else None,
        "ext_error": ext_err,
        "wal_sync": wal,
        "pruned": {"local": local_removed, "ext": ext_removed},
        "took_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    log.info("backup done: %s", out)
    return out
