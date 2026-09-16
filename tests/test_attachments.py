"""多模态附件层用例（2026-09-17 P0 附件批）：上传判重/无 VLM 降级/软删/purge 文件回收全链。

仿 test_p1c_selfevolve 的活体验证纪律（conftest.live_server 探活，daemon 不可达即 skip
不伪绿）；走 httpx 打真 daemon 真库全链。真库零残留纪律：宿主记忆经 DELETE ?purge=true
硬删（attachments 行 CASCADE 清），内容寻址文件经「引用清零回收」分支删除；
坏参用例（404/422）在触达写入前即失败，同样零残留。
"""
import base64
import hashlib
import os
import struct
import uuid
import zlib
from pathlib import Path

import httpx
import pytest


@pytest.fixture(scope="module")
def client(live_server):
    with httpx.Client(base_url=live_server, timeout=60.0) as c:
        yield c


def _png_chunk(typ: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def _png(pixel: bytes = b"\x00\xff\x00\x00") -> bytes:
    """程序化构造 1x1 PNG（RGB），自含校验和，不依赖外部样图。"""
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(pixel)) + _png_chunk(b"IEND", b""))


def _host_memory(client: httpx.Client) -> str:
    """retain 真调用造宿主记忆（agent 来源 trial 入场），返回 id。"""
    r = client.post("/v1/retain", json={
        "bank": "hermes", "caller": "pytest-attach",
        "items": [{"content": f"附件全链测试宿主 {uuid.uuid4()}",
                   "context": "tests/test_attachments.py 全链测试专用记忆，测后 purge",
                   "source_tier": "agent"}]})
    assert r.status_code == 200, r.text
    ids = r.json()["ids"]
    assert ids, r.text
    return ids[0]


def test_attachment_full_chain_upload_dedup_no_vlm_softdel_purge(client):
    """全链：上传→no_desc 降级→判重→GET 查回→软删→重传→purge 引用计数文件回收→零残留。"""
    if os.environ.get("VLM_BASE_URL") and os.environ.get("MEMORY_ENGINE_VLM_MODEL"):
        pytest.skip("VLM 已配置，no_desc 降级路径不适用（本用例锁降级契约）")
    mid = _host_memory(client)
    try:
        png = _png()
        chash = hashlib.sha256(png).hexdigest()
        b64 = base64.b64encode(png).decode()
        # ① 上传：201 + 无 VLM 配置降级（description 空 + state=no_desc，不阻塞）
        r = client.post(f"/v1/memories/{mid}/attachments", json={"data_base64": b64})
        assert r.status_code == 201, r.text
        att = r.json()
        assert att["description"] in (None, "") and att["state"] == "no_desc"
        assert att["mime"] == "image/png" and att["bytes"] == len(png)
        assert att["has_embedding"] is False
        aid = att["id"]
        fpath = Path(att["path"])
        assert fpath.name == f"{chash}.png" and fpath.exists()      # 内容寻址落盘
        # ② 判重：同内容重传 → deduped=True 复用既有行，不新增
        r = client.post(f"/v1/memories/{mid}/attachments", json={"data_base64": b64})
        assert r.status_code == 200 and r.json()["deduped"] is True and r.json()["id"] == aid
        # ③ GET 列表查回
        r = client.get(f"/v1/memories/{mid}/attachments")
        assert r.status_code == 200 and r.json()["total"] == 1
        assert r.json()["items"][0]["id"] == aid
        assert r.json()["items"][0]["content_hash"] == chash
        # ④ 软删：默认列表隐身、include_deleted 仍可见、文件保留
        r = client.delete(f"/v1/memories/{mid}/attachments/{aid}")
        assert r.status_code == 200 and r.json()["state"] == "deleted"
        assert r.json()["file_removed"] is False and fpath.exists()
        assert client.get(f"/v1/memories/{mid}/attachments").json()["total"] == 0
        assert client.get(f"/v1/memories/{mid}/attachments",
                          params={"include_deleted": "true"}).json()["total"] == 1
        # ⑤ 软删后同内容重传 → 新行（部分唯一索引：软删行不占键，不永久堵死重传）
        r = client.post(f"/v1/memories/{mid}/attachments", json={"data_base64": b64})
        assert r.status_code == 201 and r.json()["id"] != aid and r.json()["state"] == "no_desc"
        aid2 = r.json()["id"]
        # ⑥ purge 新行：软删行仍引用同哈希 → 引用计数>0，文件保留
        r = client.delete(f"/v1/memories/{mid}/attachments/{aid2}", params={"purge": "true"})
        assert r.json()["state"] == "purged" and r.json()["file_removed"] is False
        assert fpath.exists()
        # ⑦ purge 软删行：引用清零 → 盘上文件回收（零残留）
        r = client.delete(f"/v1/memories/{mid}/attachments/{aid}", params={"purge": "true"})
        assert r.json()["state"] == "purged" and r.json()["file_removed"] is True
        assert not fpath.exists()
        # ⑧ 独立内容直链 purge：上传→硬删 → 文件即回收
        png2 = _png(pixel=b"\x00\x00\xff\x00")
        r = client.post(f"/v1/memories/{mid}/attachments",
                        json={"data_base64": base64.b64encode(png2).decode()})
        assert r.status_code == 201, r.text
        att2, fpath2 = r.json(), Path(r.json()["path"])
        assert fpath2.exists()
        r = client.delete(f"/v1/memories/{mid}/attachments/{att2['id']}", params={"purge": "true"})
        assert r.json()["file_removed"] is True and not fpath2.exists()
    finally:
        # 宿主记忆硬删：attachments 行随 CASCADE 清（测试中途断言失败也兜底清理）
        client.delete(f"/v1/memories/{mid}", params={"purge": "true"})
    # ⑨ 零残留断言：memory 与其附件均 404
    assert client.get(f"/v1/memories/{mid}").status_code == 404
    assert client.get(f"/v1/memories/{mid}/attachments").status_code == 404


def test_attachment_validation_rejects_no_side_effect(client):
    """坏参三态：合法图+不存在宿主→404；非图字节→422；非法 base64→422（均零写入）。"""
    rnd = uuid.uuid4()
    r = client.post(f"/v1/memories/{rnd}/attachments",
                    json={"data_base64": base64.b64encode(_png()).decode()})
    assert r.status_code == 404
    r = client.post(f"/v1/memories/{rnd}/attachments",
                    json={"data_base64": base64.b64encode(b"plain text, not an image").decode()})
    assert r.status_code == 422
    r = client.post(f"/v1/memories/{rnd}/attachments", json={"data_base64": "!!!not-base64!!!"})
    assert r.status_code == 422
    assert client.get(f"/v1/memories/{rnd}/attachments").status_code == 404
