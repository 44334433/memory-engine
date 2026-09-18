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
from .api_attachments import router as attachments_router   # P0 附件批（挂法见 api_attachments.py L18-19）
from .api_graph import router as graph_router               # P0 附件批：图谱只读可视化
from .api_feedback import router as feedback_router         # 自进化#1（2026-09-18）：POST /v1/feedback
from .api_core_block import router as core_block_router     # W1（2026-09-18）：GET /v1/core-block
from .db import PgPool
from .embedder import EmbeddingProvider, build_embedder

log = logging.getLogger("memory-engine")

PREWARM_OBJECTS = (
    "memories", "changelog", "access_events",
    "idx_vec_hermes", "idx_vec_sessions", "idx_vec_knowledge", "idx_vec_reflection",
    "idx_mem_fts", "idx_mem_bank_state", "idx_mem_owner_vis", "idx_mem_updated",
    "idx_mem_hash", "idx_mem_tags", "idx_mem_type",   # W2：类型过滤索引预热
)


class Engine:
    def __init__(self) -> None:
        self.db: PgPool | None = None
        self.embedder: EmbeddingProvider | None = None
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
    eng.embedder = build_embedder()                  # P1 可插拔：qwen3 | openai_compat
    try:
        eng.embedder.load()                          # fp16 → CUDA 常驻（~2-5s）
        eng.model_loaded = True
    except Exception as e:  # noqa: BLE001
        # P1 降级批：嵌入加载失败不再炸启动——daemon 以降级态服务（recall 自动 fts-only），
        # health 四真以 model_loaded=false 暴露（宁降级服务，不整 daemon 不可用）。
        eng.model_loaded = False
        log.error("embedder load FAILED → degraded start (recall=fts-only): %s", e)
    if eng.model_loaded:
        try:
            eng.embedder.warmup()                    # dummy ×2 消 kernel 编译
        except Exception as e:  # noqa: BLE001 —— 预热失败不炸启动，调用期按路降级
            log.warning("embedder warmup failed (per-call degrade): %s", e)
    else:
        _spawn_embedder_selfheal(eng)                # 保活：降级态周期重试拉回全功能（2026-09-16）
    _prewarm(eng.db)
    smoke = recall_mod.recall(eng.db, eng.embedder, "memory engine warmup 预热查询", None, "main", 3, {})
    warmup_budget_ms = int(os.environ.get("MEMORY_ENGINE_WARMUP_BUDGET_MS", "200"))
    if smoke["took_ms"] >= warmup_budget_ms:
        raise RuntimeError(
            f"warmup recall {smoke['took_ms']}ms >= {warmup_budget_ms}ms（蓝图 §8.2-3 启动失败；"
            "CPU 部署/CI 用 MEMORY_ENGINE_WARMUP_BUDGET_MS 放宽）")
    if smoke.get("degraded"):
        log.warning("startup smoke recall degraded=%s failed_routes=%s（fts-only 降级态启动）",
                    smoke["degraded"], smoke.get("failed_routes"))
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


def _spawn_embedder_selfheal(eng) -> None:
    """保活线程：降级态每 60s 重试 embedder.load()+warmup()，成功即拉回全功能并 log。
    背景：P1 降级批「加载失败不炸启动」缺自愈——路径错/GPU 瞬时被占等可恢复故障会永远困在 fts-only（2026-09-16 事故）。"""
    import threading

    def _heal_loop() -> None:
        attempt = 0
        while eng.model_loaded is False:
            attempt += 1
            time.sleep(60)
            try:
                eng.embedder.load()
                eng.embedder.warmup()
                eng.model_loaded = True
                log.warning("embedder self-heal OK after %d attempts → vector 路由恢复", attempt)
            except Exception as e:  # noqa: BLE001 —— 继续重试，不退出
                log.warning("embedder self-heal attempt %d failed: %s", attempt, str(e)[:200])

    threading.Thread(target=_heal_loop, daemon=True, name="embedder-selfheal").start()


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
    app.include_router(graph_router)
    app.include_router(attachments_router)
    app.include_router(feedback_router)   # 自进化#1：POST /v1/feedback（新路由零改动既有端点）
    app.include_router(core_block_router)  # W1：GET /v1/core-block（新路由零改动既有端点）
    return app
