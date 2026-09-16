"""通用小工具：UUIDv7 / content_hash / staleness。"""
import hashlib
import os
import time
import uuid
from datetime import datetime, timezone

from . import config


def uuid7() -> uuid.UUID:
    """UUIDv7：48bit unix_ms + ver7 + rand_a + rand_b（时间有序主键，蓝图 §3.1）。"""
    ts_ms = time.time_ns() // 1_000_000
    rnd = os.urandom(10)
    b = bytearray(16)
    b[0:6] = ts_ms.to_bytes(6, "big")
    b[6] = 0x70 | (rnd[0] & 0x0F)
    b[7] = rnd[1]
    b[8] = rnd[2]
    b[9] = (rnd[3] & 0x3F) | 0x80
    b[10:16] = rnd[4:10]
    return uuid.UUID(bytes=bytes(b))


def content_hash(bank: str, body: str) -> str:
    """精确判重键：sha256(bank + NUL + body)。"""
    return hashlib.sha256(f"{bank}\x00{body}".encode("utf-8")).hexdigest()


def dedup_hash(body: str, context: str) -> str:
    """判重 UNIQUE 兜底键（P1 拍板）：sha256(body + US + context)。

    - 写入路径必带非空 context（retain 质量闸），故与新写入永不与「空 context 存量回填键」冲突；
    - 分隔符 US(0x1f)：PG 侧迁移回填用 chr(31) 同构（NUL 在 PG text 非法）；
    - 仅作 DB 层并发竞态兜底（应用层判重仍以 content_hash+语义判重为主，语义不变）。
    """
    return hashlib.sha256(f"{body}\x1f{context}".encode("utf-8")).hexdigest()


def staleness_of(original_date: datetime | None, created_at: datetime | None = None) -> str:
    """fresh ≤30d / aging 30-90d / stale >90d（拍板①）；基准=original_date，缺省用 created_at。"""
    ref = original_date or created_at
    if ref is None:
        return "fresh"
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - ref).total_seconds() / 86400.0
    if days <= config.STALE_FRESH_DAYS:
        return "fresh"
    if days <= config.STALE_AGING_DAYS:
        return "aging"
    return "stale"


def vec_to_pg(vec: list[float]) -> str:
    """1024 维向量 → pgvector 文本字面量 '[f1,f2,...]'。"""
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


def derive_title(content: str) -> str:
    """未显式给 title 时：首行/首 48 字符（memories.title NOT NULL）。"""
    first = content.strip().splitlines()[0].strip() if content.strip() else "untitled"
    return first[:48]
