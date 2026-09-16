"""多模态附件层（P0 2026-09-17，全新增量批：不改动任何既有文件）。

路由：
  POST   /v1/memories/{mid}/attachments          JSON base64 图片上传（判重 content_hash）
  GET    /v1/memories/{mid}/attachments          附件列表（默认不含软删）
  DELETE /v1/memories/{mid}/attachments/{aid}    软删 state='deleted'；?purge=true 硬删行+引用清零时删盘上文件

写入流程：校验 memory 存在 → base64 解码 + 魔数嗅探 mime（仅图片）→ sha256 判重
  → 存盘 ATTACHMENTS_DIR/{sha256}.{ext}（内容寻址，原子写）→ INSERT（state='active'）
  → VLM 中文描述（vlm.describe_image，纯 HTTP）→ 描述走现有 embedder 出向量
  → UPDATE description/description_embedding/embed_ver。GPU/HTTP 调用一律在 DB 事务外
（对齐 api_memories「持池连接不做嵌入」先例）。

降级契约：VLM 未配置或调用失败 → description 留空 + state='no_desc'，上传 2xx 不阻塞（log.warning）；
embedder 不可用/失败 → description 照存、description_embedding 留空（log.warning）。

接线说明（本批禁改既有文件，故 app.py 未动——生产 daemon 接入为部署一步）：
    from .api_attachments import router as attachments_router
    app.include_router(attachments_router)          # app.py create_app() 内一行

已知限制（P0 显式登记）：直接 purge 记忆时 attachments 行随 CASCADE 清，但盘上文件不回收
（孤儿文件无害，内容寻址可复用）；描述嵌入暂不参与召回（recal.py 融合=P1 范围，禁越界）。
"""
import base64
import binascii
import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import psycopg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import config, db, vlm

log = logging.getLogger("memory-engine.api.attachments")
router = APIRouter(prefix="/v1")

# config.py 本批禁改：附件目录常量落在本模块（默认 BASE_DIR/attachments，env 可覆盖）。
# 生产 daemon 的 BASE_DIR=MEMORY_ENGINE_DIR（systemd unit 指向 repo 根），与 models/state 同根。
ATTACHMENTS_DIR = Path(os.environ.get(
    "MEMORY_ENGINE_ATTACHMENTS_DIR", str(config.BASE_DIR / "attachments")))
MAX_BYTES = int(os.environ.get("MEMORY_ENGINE_ATTACH_MAX_MB", "20")) * 1024 * 1024
RAW_B64_CAP = 64_000_000  # base64 文本预检上限（防解码前内存膨胀；20MB 图 ≈27MB b64）

# 魔数嗅探表：mime → 规范扩展名（扩展名由 mime 映射，文件名永不参与落盘路径，杜绝路径注入）
_SNIFF = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)
_MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

ATT_COLS = ("id, memory_id, path, content_hash, mime, bytes, description, embed_ver, state, "
            "created_at, (description_embedding IS NOT NULL) AS has_embedding")


class AttachmentUpload(BaseModel):
    data_base64: str
    mime: Optional[str] = None        # 缺省时按文件魔数嗅探
    filename: Optional[str] = None    # 仅作 mime 推断参考，不参与落盘路径


def _jsonable(row: dict) -> dict:
    out = dict(row)
    for k, v in out.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            out[k] = str(v)
    return out


def _sniff_mime(b: bytes) -> str | None:
    for magic, mime, _ext in _SNIFF:
        if b.startswith(magic):
            return mime
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.post("/memories/{mid}/attachments")
def upload_attachment(mid: uuid.UUID, req: AttachmentUpload, request: Request):
    eng = request.app.state.engine
    if len(req.data_base64) > RAW_B64_CAP:
        raise HTTPException(413, f"base64 文本过大（>{RAW_B64_CAP} 字符）")
    try:
        raw = base64.b64decode(req.data_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(422, f"data_base64 非法: {e}") from None
    if not raw:
        raise HTTPException(422, "data_base64 解码为空")
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, f"附件超过上限 {MAX_BYTES // (1024 * 1024)}MB")
    mime = _sniff_mime(raw) or (req.mime.strip() if req.mime and req.mime.strip() in _MIME_EXT else None)
    if not mime:
        raise HTTPException(422, "仅支持图片附件（png/jpeg/gif/webp；无法从魔数/声明识别）")
    ext = _MIME_EXT[mime]
    chash = hashlib.sha256(raw).hexdigest()

    with eng.db.connection() as conn:
        if not db.fetch_one(conn, "SELECT id FROM memories WHERE id=%s", (mid,)):
            raise HTTPException(404, f"memory {mid} 不存在")
        # 判重：同内容（全库内容寻址）已有未软删附件 → 直接复用，不落盘不重复嵌
        dup = db.fetch_one(
            conn, f"SELECT {ATT_COLS} FROM attachments WHERE content_hash=%s AND state<>'deleted'",
            (chash,))
        if dup:
            return {**_jsonable(dup), "deduped": True}

        ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        path = ATTACHMENTS_DIR / f"{chash}.{ext}"
        if not path.exists():  # 内容寻址：已存在则复用（软删行残留文件场景）
            tmp = path.with_suffix(  # 唯一临时名：并发上传同号新内容时 .part 撞车会炸 os.replace
                f"{path.suffix}.{os.getpid()}.{uuid.uuid4().hex[:8]}.part")
            tmp.write_bytes(raw)
            os.replace(tmp, path)  # 原子落盘，杜绝半文件
        try:
            row = db.fetch_one(
                conn,
                f"INSERT INTO attachments (memory_id, path, content_hash, mime, bytes) "
                f"VALUES (%s,%s,%s,%s,%s) RETURNING {ATT_COLS}",
                (mid, str(path), chash, mime, len(raw)))
        except psycopg.errors.UniqueViolation:
            # 并发竞态：唯一索引兜底（应用层判重与 INSERT 间隙撞车）→ 返回既有条目
            dup = db.fetch_one(
                conn, f"SELECT {ATT_COLS} FROM attachments WHERE content_hash=%s AND state<>'deleted'",
                (chash,))
            if not dup:
                raise HTTPException(500, "附件判重竞态且未找到既有条目") from None
            return {**_jsonable(dup), "deduped": True}

    # —— 描述链路：VLM（HTTP）与 embedder（GPU/远程）均在 DB 连接外 ——

    desc = vlm.describe_image(req.data_base64, mime) if vlm.configured() else None
    sets, params = [], []
    if desc:
        emb = getattr(eng, "embedder", None)
        usable = emb is not None and getattr(emb, "loaded", False)
        vec = None
        if usable:
            try:
                vec = emb.embed_documents([desc])[0]
            except Exception as e:  # noqa: BLE001 —— 描述照存，向量缺位不阻塞
                log.warning("描述嵌入失败（description 已存，向量留空）: %s", str(e)[:200])
        else:
            log.warning("embedder 不可用（降级态）→ 描述照存、向量留空")
        sets = ["description=%s", "state='active'", "embed_ver=%s"]
        params = [desc, str(config.EMBED_VER)]
        if vec is not None:
            from .util import vec_to_pg
            sets.append("description_embedding=%s::vector")
            params.append(vec_to_pg(vec))
    else:
        # vlm.configured()=False → 静默降级；describe 内部失败已 warning，这里补一行状态账
        log.warning("附件 %s 无描述（VLM 未配置或失败）→ state=no_desc", row["id"])
        sets = ["state='no_desc'"]
        params = []
    with eng.db.connection() as conn:
        row = db.fetch_one(
            conn, f"UPDATE attachments SET {', '.join(sets)} WHERE id=%s RETURNING {ATT_COLS}",
            (*params, row["id"]))
    return _attachment_out(row, 201)


def _attachment_out(row: dict, status: int):
    return JSONResponse(status_code=status, content=_jsonable(row))


@router.get("/memories/{mid}/attachments")
def list_attachments(mid: uuid.UUID, request: Request, include_deleted: bool = False):
    eng = request.app.state.engine
    with eng.db.connection() as conn:
        if not db.fetch_one(conn, "SELECT id FROM memories WHERE id=%s", (mid,)):
            raise HTTPException(404, f"memory {mid} 不存在")
        where = "" if include_deleted else " AND state<>'deleted'"
        rows = db.fetch_all(
            conn, f"SELECT {ATT_COLS} FROM attachments WHERE memory_id=%s{where} ORDER BY created_at, id",
            (mid,))
    return {"items": [_jsonable(r) for r in rows], "total": len(rows)}


@router.delete("/memories/{mid}/attachments/{aid}")
def delete_attachment(mid: uuid.UUID, aid: uuid.UUID, request: Request, purge: bool = False):
    """软删（state='deleted'，行与盘上文件保留，可重传同内容）；purge=true 硬删行，
    且当同 content_hash 引用清零时回收盘上文件（内容寻址共享，按引用计数）。"""
    eng = request.app.state.engine
    with eng.db.connection() as conn, conn.transaction():
        if purge:
            row = db.fetch_one(
                conn, "DELETE FROM attachments WHERE id=%s AND memory_id=%s RETURNING path, content_hash",
                (aid, mid))
            state = "purged"
        else:
            row = db.fetch_one(
                conn, "UPDATE attachments SET state='deleted' WHERE id=%s AND memory_id=%s "
                      "AND state<>'deleted' RETURNING path, content_hash",
                (aid, mid))
            state = "deleted"
        if not row:
            raise HTTPException(404, f"attachment {aid} 不存在或已软删")
        remaining = db.fetch_one(
            conn, "SELECT count(*) c FROM attachments WHERE content_hash=%s", (row["content_hash"],))["c"]
    file_removed = False
    if purge and remaining == 0:
        p = Path(row["path"])
        if p.parent == ATTACHMENTS_DIR:  # 越界路径防护：只回收附件目录内的内容寻址文件
            file_removed = not p.exists() or _unlink(p)
        else:
            log.warning("附件路径越界，跳过文件回收: %s", row["path"])
    return {"id": str(aid), "memory_id": str(mid), "state": state, "file_removed": file_removed}


def _unlink(p: Path) -> bool:
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError as e:
        log.warning("附件文件回收失败 %s: %s", p, e)
        return False
