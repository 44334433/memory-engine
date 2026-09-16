"""Client tests — httpx.MockTransport, no real engine touched."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from memoryengine import (
    Conflict,
    EngineConnectionError,
    EngineOverloaded,
    EngineServerError,
    EndpointNotAvailable,
    HealthStatus,
    InvalidInput,
    MemoryEngine,
    NotFound,
    RecallResponse,
)


# --------------------------------------------------------------------------- #
# mock transport factory
# --------------------------------------------------------------------------- #

def make_engine(handler, base_url="http://engine.test:8766", **kw) -> MemoryEngine:
    return MemoryEngine(base_url, transport=httpx.MockTransport(handler), **kw)


def ok(body, status=200) -> httpx.Response:
    return httpx.Response(status, json=body, headers={"content-type": "application/json"})


# --------------------------------------------------------------------------- #
# happy paths
# --------------------------------------------------------------------------- #

def test_retain_posts_bank_items_and_dedup():
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.read())
        return ok({"ids": ["id-1", "id-2"], "dedup_skipped": 0, "dedup_existing": [], "seq": 5})

    me = make_engine(h)
    r = me.retain([{"content": "c", "context": "x"}], bank="knowledge", dedup=False)
    assert seen["method"] == "POST" and seen["path"] == "/v1/retain"
    assert seen["body"]["bank"] == "knowledge"
    assert seen["body"]["dedup"] is False
    assert seen["body"]["items"][0]["content"] == "c"
    assert r["ids"] == ["id-1", "id-2"]


def test_retain_default_bank_and_caller():
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.read())
        return ok({"ids": ["i"], "dedup_skipped": 0, "dedup_existing": [], "seq": 1})

    me = make_engine(h, caller="agent-x")
    me.retain([{"content": "c", "context": "x"}])
    assert seen["body"]["bank"] == "hermes"
    assert seen["body"]["caller"] == "agent-x"


def test_recall_parses_hits_and_passes_filters():
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.read())
        return ok({"results": [
            {"id": "m1", "score": 0.9, "content": "hello", "source_ref": "s_1",
             "tags": ["a"], "score_parts": {"vec": 1}},
            {"id": "m2", "score": 0.5, "content": "world"},
        ], "degraded": False, "took_ms": 12.5})

    me = make_engine(h)
    resp = me.recall("query text", bank="hermes", top_k=3, filters={"tags": ["q:x"]})
    assert seen["body"]["query"] == "query text"
    assert seen["body"]["top_k"] == 3
    assert seen["body"]["filters"] == {"tags": ["q:x"]}
    assert isinstance(resp, RecallResponse)
    assert len(resp) == 2
    assert resp[0].id == "m1" and resp[0].score_parts == {"vec": 1}
    assert resp.results[1].content == "world"
    assert resp.took_ms == 12.5
    assert resp.degraded is False


def test_recall_degraded_passthrough_200():
    """Engine degraded mode = HTTP 200 + degraded=true + failed_routes — must surface."""

    def h(req: httpx.Request) -> httpx.Response:
        return ok({"results": [{"id": "m1", "score": 0.4, "content": "fts hit"}],
                   "degraded": True, "failed_routes": ["vector"], "took_ms": 9})

    resp = make_engine(h).recall("q")
    assert resp.degraded is True
    assert resp.failed_routes == ["vector"]
    assert len(resp.results) == 1  # degraded results are still served, not dropped


def test_get_memories_pagination_params():
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["params"] = dict(req.url.params)
        return ok({"items": [{"id": "m1"}], "total": 1, "limit": 5, "offset": 10})

    page = make_engine(h).get_memories(bank="hermes", state="active", domain="d",
                                       q="needle", limit=5, offset=10)
    assert seen["params"] == {"bank": "hermes", "state": "active", "domain": "d",
                              "q": "needle", "limit": "5", "offset": "10"}
    assert page.total == 1 and page.items[0]["id"] == "m1"


def test_get_memory():
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/memories/m1"
        return ok({"id": "m1", "content": "c"})

    assert make_engine(h).get_memory("m1")["id"] == "m1"


def test_patch_body_fields():
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = req.method, req.url.path
        seen["body"] = json.loads(req.read())
        return ok({"id": "m1", "re_embedded": True})

    r = make_engine(h).patch("m1", body="new text", tags=["t"], supersede=True)
    assert seen["method"] == "PATCH" and seen["path"] == "/v1/memories/m1"
    assert seen["body"] == {"body": "new text", "tags": ["t"], "supersede": True}
    assert r["re_embedded"] is True


def test_adopt_hits_per_memory_route():
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["path"], seen["params"] = req.url.path, dict(req.url.params)
        return ok({"id": "m1", "adopt_count": 3})

    r = make_engine(h, caller="main").adopt("m1", caller="host1")
    assert seen["path"] == "/v1/memories/m1/adopt"
    assert seen["params"]["caller"] == "host1"
    assert r["adopt_count"] == 3


def test_delete_soft_and_purge():
    seen = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.url.params))
        return ok({"id": "m1", "state": "retired"})

    me = make_engine(h)
    me.delete("m1")
    me.delete("m1", purge=True)
    assert seen[0].get("purge") == "false"
    assert seen[1].get("purge") == "true"


def test_export_streams_jsonl():
    lines = [json.dumps({"id": f"m{i}", "seq": i}) for i in range(3)]

    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.params["since_seq"] == "2"
        return httpx.Response(200, content="\n".join(lines).encode() + b"\n",
                              headers={"content-type": "application/x-ndjson"})

    rows = list(make_engine(h).export(since_seq=2))
    assert [r["id"] for r in rows] == ["m0", "m1", "m2"]


def test_health_four_truths():
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/health"
        return ok({"status": "ok", "db": True, "model_loaded": True, "warm": True,
                   "ready": True, "version": "0.4.0-p1c"})

    hs: HealthStatus = make_engine(h).health()
    assert hs.ok is True and hs.db and hs.model_loaded and hs.warm and hs.ready


# --------------------------------------------------------------------------- #
# error mapping
# --------------------------------------------------------------------------- #

def test_400_maps_to_invalid_input():
    def h(req):
        return ok({"detail": "context 必填"}, 400)
    with pytest.raises(InvalidInput) as ei:
        make_engine(h).retain([{"content": "c", "context": ""}])
    assert ei.value.status == 400


def test_422_maps_to_invalid_input():
    def h(req):
        return ok({"detail": [{"loc": ["body", "items"], "msg": "missing"}]}, 422)
    with pytest.raises(InvalidInput):
        make_engine(h).recall("q")  # engine answers 422 for a broken contract


def test_503_maps_to_engine_overloaded_with_retryable():
    def h(req):
        return ok({"detail": "all recall routes failed", "degraded": True,
                   "retryable": True}, 503)
    with pytest.raises(EngineOverloaded) as ei:
        make_engine(h).recall("q")
    assert ei.value.retryable is True
    assert ei.value.degraded is True
    assert ei.value.status == 503


def test_500_maps_to_engine_server_error():
    def h(req):
        return ok({"detail": "boom"}, 500)
    with pytest.raises(EngineServerError):
        make_engine(h).recall("q")


def test_404_maps_to_not_found():
    def h(req):
        return ok({"detail": "memory x 不存在"}, 404)
    with pytest.raises(NotFound):
        make_engine(h).get_memory("x")


def test_409_maps_to_conflict():
    def h(req):
        return ok({"detail": "superseded entry"}, 409)
    with pytest.raises(Conflict, match="superseded"):
        make_engine(h).patch("m1", body="x")


def test_connect_error_maps_to_engine_connection_error():
    def h(req):
        raise httpx.ConnectError("refused", request=req)
    with pytest.raises(EngineConnectionError):
        make_engine(h).health()


def test_timeout_maps_to_engine_connection_error():
    def h(req):
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(EngineConnectionError):
        make_engine(h).health()


# --------------------------------------------------------------------------- #
# reserved surface + lifecycle
# --------------------------------------------------------------------------- #

def test_digest_unmounted_route_raises_endpoint_not_available():
    """Engines that run freshness host-side have no /v1/freshness/digest route."""
    def h(req):
        return ok({"detail": "Not Found"}, 404)
    with pytest.raises(EndpointNotAvailable):
        make_engine(h).digest("sess-1", cursor=7)


def test_digest_forwards_body_when_mounted():
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"] = req.url.path, json.loads(req.read())
        return ok({"entries": [], "version": 8})

    r = make_engine(h).digest("sess-1", 7, domains=["docs"], budget_chars=1500)
    assert seen["path"] == "/v1/freshness/digest"
    assert seen["body"] == {"session_id": "sess-1", "cursor": 7,
                            "domains": ["docs"], "budget_chars": 1500}
    assert r["version"] == 8


def test_attach_file_base64_encodes_and_posts():
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"] = req.url.path, json.loads(req.read())
        return ok({"id": "att-1", "mime": "image/png", "size": 4})

    r = make_engine(h).attach_file("m1", b"\x89PNG", filename="shot.png", mime="image/png")
    assert seen["path"] == "/v1/memories/m1/attachments"
    assert base64.b64decode(seen["body"]["data_base64"]) == b"\x89PNG"
    assert seen["body"]["mime"] == "image/png" and seen["body"]["filename"] == "shot.png"
    assert r["id"] == "att-1"


def test_list_and_delete_attachments():
    seen = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, dict(req.url.params)))
        if req.method == "GET":
            return ok({"attachments": [{"id": "a1"}]})
        return ok({"id": "a1", "deleted": True})

    me = make_engine(h)
    atts = me.list_attachments("m1", include_deleted=True)
    me.delete_attachment("m1", "a1", purge=True)
    assert atts == [{"id": "a1"}]
    assert seen[0] == ("GET", "/v1/memories/m1/attachments", {"include_deleted": "true"})
    assert seen[1][:2] == ("DELETE", "/v1/memories/m1/attachments/a1")
    assert seen[1][2]["purge"] == "true"


def test_graph_snapshot_params():
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["path"], seen["params"] = req.url.path, dict(req.url.params)
        return ok({"nodes": [{"id": "n1"}], "edges": [], "counts": {"edges_total": 0}})

    r = make_engine(h).graph(bank="knowledge", limit=50)
    assert seen["path"] == "/v1/graph"
    assert seen["params"] == {"bank": "knowledge", "limit": "50"}
    assert r["counts"]["edges_total"] == 0


def test_context_manager_closes_client():
    with make_engine(lambda req: ok({"status": "ok", "db": True, "ready": True})) as me:
        assert me.health().ok
    assert me._client.is_closed
