"""FastAPI 应用工厂 + sd_notify（Type=notify / WatchdogSec=60）+ 启动预热（蓝图 §8.2）。"""
import faulthandler
import logging
import os
import signal
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import config, db, recall as recall_mod
from . import consolidate as consol, lifecycle as lc
from .api_core import router as core_router
from .api_lifecycle import router as lifecycle_router
from .api_memories import router as mem_router
from .db import PgPool
from .embedder import Embedder

log = logging.getLogger("memory-engine")

PREWARM_OBJECTS = (
    "memories", "changelog", "access_events",
    "idx_vec_hermes", "idx_vec_sessions", "idx_vec_knowledge", "idx_vec_reflection",
    "idx_mem_fts", "idx_mem_bank_state", "idx_mem_owner_vis", "idx_mem_updated",
    "idx_mem_hash", "idx_mem_tags",
)


class Engine:
    def __init__(self) -> None:
        self.db: PgPool | None = None
        self.embedder: Embedder | None = None
        self.ready = False
        self.warm = False
        self.model_loaded = False
        self.started_at = time.time()
        self.lifecycle_last: dict | None = None
        self.lifecycle_thread: threading.Thread | None = None


def sd_notify(state: str) -> bool:
    """systemd Type=notify 通知（无需 libsystemd，直接 NOTIFY_SOCKET DGRAM）。"""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect(addr)
        s.sendall(state.encode())
        s.close()
        return True
    except OSError:
        return False


def _watchdog_loop(stop: threading.Event) -> None:
    while not stop.wait(20.0):          # WatchdogSec=60 → 20s 心跳（蓝图 §8.1）
        if not sd_notify("WATCHDOG=1"):
            return                       # 非 systemd 环境无 NOTIFY_SOCKET，退出心跳


def _prewarm(pool: PgPool) -> int:
    done = 0
    with pool.connection() as conn:
        for obj in PREWARM_OBJECTS:
            try:
                db.fetch_one(conn, "SELECT pg_prewarm(%s)", (obj,))
                done += 1
            except Exception as e:
                log.warning("pg_prewarm skip %s: %s", obj, e)
    log.info("pg_prewarm %d/%d objects", done, len(PREWARM_OBJECTS))
    return done


@asynccontextmanager
async def lifespan(app: FastAPI):
    eng = Engine()
    app.state.engine = eng
    stop = threading.Event()
    threading.Thread(target=_watchdog_loop, args=(stop,), daemon=True, name="sd-watchdog").start()
    t0 = time.perf_counter()
    eng.db = PgPool(config.PG_DSN, config.POOL_MIN, config.POOL_MAX)
    eng.embedder = Embedder()
    eng.embedder.load()                              # fp16 → CUDA 常驻（~2-5s）
    eng.model_loaded = True
    eng.embedder.warmup()                            # dummy ×2 消 kernel 编译
    _prewarm(eng.db)
    smoke = recall_mod.recall(eng.db, eng.embedder, "memory engine warmup 预热查询", None, "main", 3, {})
    if smoke["took_ms"] >= 200:
        raise RuntimeError(f"warmup recall {smoke['took_ms']}ms >= 200ms（蓝图 §8.2-3 启动失败）")
    eng.warm = True
    eng.ready = True
    eng.lifecycle_last = None
    eng.lifecycle_thread = lc.start_thread(eng, stop)      # 延迟 60s 自启动（蓝图 §8.2-4）
    consol._ensure_worker(eng.db)                          # 空闲即挂起，入队才消费
    sd_notify("READY=1")
    log.info("memory-engine READY: port=%s warm_recall=%.1fms startup=%.1fs",
             config.PORT, smoke["took_ms"], (time.perf_counter() - t0) * 1000)
    yield
    eng.ready = False
    stop.set()
    if eng.db:
        eng.db.close()
    sd_notify("STOPPING=1")


def create_app() -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # 运维钩子：kill -USR1 <pid> 向 journald 转储全部线程栈（挂死取证，阶段2 加装）
    try:
        if not faulthandler.is_enabled():
            faulthandler.enable()
        faulthandler.register(signal.SIGUSR1, file=sys.__stderr__, all_threads=True)
    except Exception as e:
        logging.warning("faulthandler register failed: %s", e)
    app = FastAPI(title="memory-engine", version=config.VERSION, lifespan=lifespan)
    app.include_router(core_router)
    app.include_router(mem_router)
    app.include_router(lifecycle_router)
    return app
