"""memory-engine client — typed HTTP wrapper over the engine's /v1 surface.

The engine is a FastAPI daemon (default ``http://127.0.0.1:8766``). This client
maps its documented API surface 1:1 and translates error semantics into typed
exceptions (see :mod:`memoryengine.exceptions`). Degraded responses (engine
returns 200 with ``degraded: true``) are passed through transparently — check
``RecallResponse.degraded`` instead of catching.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

import httpx

from .exceptions import (
    Conflict,
    EndpointNotAvailable,
    EngineConnectionError,
    EngineOverloaded,
    EngineServerError,
    InvalidInput,
    MemoryEngineError,
)

__all__ = ["MemoryEngine", "RecallHit", "RecallResponse", "MemoriesPage", "HealthStatus"]


# --------------------------------------------------------------------------- #
# Response types (thin typed views; ``raw`` keeps the full engine payload)
# --------------------------------------------------------------------------- #

@dataclass
class RecallHit:
    id: str
    score: float
    content: str
    context: Optional[str] = None
    bank: Optional[str] = None
    source_ref: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    score_parts: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, d: Dict[str, Any]) -> "RecallHit":
        return cls(
            id=str(d.get("id", "")),
            score=float(d.get("score", 0.0)),
            content=d.get("content", ""),
            context=d.get("context"),
            bank=d.get("bank"),
            source_ref=d.get("source_ref"),
            tags=list(d.get("tags", []) or []),
            score_parts=dict(d.get("score_parts", {}) or {}),
            raw=d,
        )


@dataclass
class RecallResponse:
    results: List[RecallHit]
    degraded: bool = False          # engine 200-passthrough: recall served at reduced capacity
    failed_routes: List[str] = field(default_factory=list)
    took_ms: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def __iter__(self) -> Iterator[RecallHit]:
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def __getitem__(self, i: int) -> RecallHit:
        return self.results[i]

    @classmethod
    def from_raw(cls, d: Dict[str, Any]) -> "RecallResponse":
        return cls(
            results=[RecallHit.from_raw(r) for r in d.get("results", [])],
            degraded=bool(d.get("degraded", False)),
            failed_routes=list(d.get("failed_routes", []) or []),
            took_ms=d.get("took_ms"),
            raw=d,
        )


@dataclass
class MemoriesPage:
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_raw(cls, d: Dict[str, Any]) -> "MemoriesPage":
        return cls(items=list(d.get("items", [])), total=int(d.get("total", 0)),
                   limit=int(d.get("limit", 0)), offset=int(d.get("offset", 0)))


@dataclass
class HealthStatus:
    ok: bool
    status: str
    db: bool
    model_loaded: bool
    warm: bool
    ready: bool
    version: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, d: Dict[str, Any]) -> "HealthStatus":
        return cls(ok=d.get("status") == "ok" and bool(d.get("db")) and bool(d.get("ready")),
                   status=d.get("status", ""), db=bool(d.get("db")),
                   model_loaded=bool(d.get("model_loaded")), warm=bool(d.get("warm")),
                   ready=bool(d.get("ready")), version=d.get("version"), raw=d)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class MemoryEngine:
    """Typed client for a memory-engine daemon.

    >>> me = MemoryEngine("http://127.0.0.1:8766")
    >>> me.retain([{"content": "...", "context": "..."}])
    >>> hits = me.recall("where did we leave off?").results
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8766",
        *,
        timeout: float = 30.0,
        caller: str = "main",
        headers: Optional[Dict[str, str]] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._caller = caller
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Content-Type": "application/json", **(headers or {})},
            transport=transport,
        )

    # -- convenience -------------------------------------------------------- #

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MemoryEngine":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transport / error mapping ------------------------------------------ #

    def _request(self, method: str, path: str, *, json_body: Any = None,
                 params: Optional[Dict[str, Any]] = None,
                 stream: bool = False) -> httpx.Response:
        try:
            resp = self._client.request(method, path, json=json_body, params=params)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise EngineConnectionError(f"cannot reach memory engine: {e}") from e
        if resp.status_code < 400:
            return resp
        self._raise_for_status(resp, path=path)
        raise MemoryEngineError("unreachable")  # pragma: no cover

    @staticmethod
    def _raise_for_status(resp: httpx.Response, *, path: str) -> None:
        status = resp.status_code
        try:
            body: Any = resp.json()
        except (json.JSONDecodeError, ValueError):
            body = resp.text
        detail = body.get("detail") if isinstance(body, dict) else body
        msg = f"{resp.request.method} {path} -> {status}: {detail}"

        if status in (400, 422):
            raise InvalidInput(msg, status=status, detail=detail, body=body)
        if status == 404:
            # Documented-but-unmounted routes (digest on host-side-freshness
            # builds, attachments/graph before their routers ship).
            raise EndpointNotAvailable(msg, status=status, detail=detail, body=body)
        if status == 409:
            raise Conflict(msg, status=status, detail=detail, body=body)
        if status == 503:
            retryable = bool(isinstance(body, dict) and body.get("retryable"))
            degraded = bool(isinstance(body, dict) and body.get("degraded", True))
            raise EngineOverloaded(msg, status=status, detail=detail, body=body,
                                   retryable=retryable, degraded=degraded)
        if status >= 500:
            raise EngineServerError(msg, status=status, detail=detail, body=body)
        raise MemoryEngineError(msg, status=status, detail=detail, body=body)

    # -- core surface -------------------------------------------------------- #

    def retain(self, items: List[Dict[str, Any]], *, bank: str = "hermes",
               dedup: bool = True) -> Dict[str, Any]:
        """Write memories. Each item needs ``content`` + ``context`` (write gate).

        Returns ``{"ids": [...], "dedup_skipped": N, "dedup_existing": [...], "seq": N}``.
        """
        return self._request("POST", "/v1/retain",
                             json_body={"bank": bank, "caller": self._caller,
                                        "items": items, "dedup": dedup}).json()

    def recall(self, query: str, *, bank: Optional[str] = None, top_k: int = 10,
               filters: Optional[Dict[str, Any]] = None) -> RecallResponse:
        """Hybrid retrieval (dense + CJK FTS + temporal + graph, RRF-fused).

        Degraded responses pass through: ``response.degraded`` is True when the
        engine served with a route down (e.g. fts-only after embedder failure).
        """
        body: Dict[str, Any] = {"query": query, "caller": self._caller, "top_k": top_k}
        if bank is not None:
            body["bank"] = bank
        if filters:
            body["filters"] = filters
        return RecallResponse.from_raw(self._request("POST", "/v1/recall", json_body=body).json())

    def digest(self, session_id: str, cursor: int, *, domains: Optional[List[str]] = None,
               budget_chars: Optional[int] = None, **extra: Any) -> Dict[str, Any]:
        """Freshness digest: what changed since ``cursor`` (documented route
        ``POST /v1/freshness/digest``).

        Raises :class:`EndpointNotAvailable` on engine builds that run the
        freshness protocol host-side (no mounted route).
        """
        body: Dict[str, Any] = {"session_id": session_id, "cursor": cursor, **extra}
        if domains is not None:
            body["domains"] = domains
        if budget_chars is not None:
            body["budget_chars"] = budget_chars
        return self._request("POST", "/v1/freshness/digest", json_body=body).json()

    def get_memories(self, *, bank: Optional[str] = None, state: Optional[str] = None,
                     owner: Optional[str] = None, domain: Optional[str] = None,
                     q: Optional[str] = None, limit: int = 20,
                     offset: int = 0) -> MemoriesPage:
        """List/filter memories (paginated)."""
        params = {"limit": limit, "offset": offset}
        for k in ("bank", "state", "owner", "domain", "q"):
            v = locals()[k]
            if v is not None:
                params[k] = v
        return MemoriesPage.from_raw(self._request("GET", "/v1/memories", params=params).json())

    def get_memory(self, memory_id: str) -> Dict[str, Any]:
        """Single entry + full provenance."""
        return self._request("GET", f"/v1/memories/{memory_id}").json()

    def patch(self, memory_id: str, *, body: Optional[str] = None, title: Optional[str] = None,
              tags: Optional[List[str]] = None, domain: Optional[str] = None,
              supersede: bool = False, **fields: Any) -> Dict[str, Any]:
        """Update fields (re-embeds when the body changes).

        ``supersede=True`` = bi-temporal correction: old entry invalidated, a new
        row is written (its id is in the response under ``superseded_from`` links).
        """
        payload: Dict[str, Any] = {"supersede": supersede, **fields}
        if body is not None:
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        if tags is not None:
            payload["tags"] = tags
        if domain is not None:
            payload["domain"] = domain
        return self._request("PATCH", f"/v1/memories/{memory_id}", json_body=payload).json()

    def adopt(self, memory_id: str, *, caller: Optional[str] = None) -> Dict[str, Any]:
        """Report host adoption (use-it-or-lose-it lifecycle signal)."""
        params = {"caller": caller or self._caller}
        return self._request("POST", f"/v1/memories/{memory_id}/adopt", params=params).json()

    def delete(self, memory_id: str, *, purge: bool = False) -> Dict[str, Any]:
        """Retire (soft) by default; ``purge=True`` hard-deletes the row."""
        return self._request("DELETE", f"/v1/memories/{memory_id}",
                             params={"purge": "true" if purge else "false"}).json()

    def export(self, *, since_seq: int = 0, limit: Optional[int] = None) -> Iterator[Dict[str, Any]]:
        """Stream the full logical export as JSONL (generator of parsed rows)."""
        params: Dict[str, Any] = {"since_seq": since_seq}
        if limit is not None:
            params["limit"] = limit
        with self._client.stream("GET", "/v1/export", params=params) as resp:
            if resp.status_code >= 400:
                resp.read()
                self._raise_for_status(resp, path="/v1/export")
            for line in resp.iter_lines():
                line = line.strip()
                if line:
                    yield json.loads(line)

    def health(self) -> HealthStatus:
        """Four-truth health check: db + model + warm + ready."""
        return HealthStatus.from_raw(self._request("GET", "/v1/health").json())

    def attach_file(self, memory_id: str, data: bytes, *, filename: Optional[str] = None,
                    mime: Optional[str] = None) -> Dict[str, Any]:
        """Attach binary content (image/PDF/…) to a memory (base64 over JSON).

        ``mime`` is sniffed from the file magic when omitted; ``filename`` only
        assists mime inference, storage paths are engine-owned.
        """
        body: Dict[str, Any] = {"data_base64": base64.b64encode(data).decode("ascii")}
        if filename is not None:
            body["filename"] = filename
        if mime is not None:
            body["mime"] = mime
        return self._request("POST", f"/v1/memories/{memory_id}/attachments",
                             json_body=body).json()

    def list_attachments(self, memory_id: str, *, include_deleted: bool = False) -> List[Dict[str, Any]]:
        """List a memory's attachments."""
        d = self._request("GET", f"/v1/memories/{memory_id}/attachments",
                          params={"include_deleted": "true" if include_deleted else "false"}).json()
        return d.get("attachments", d.get("items", d if isinstance(d, list) else []))

    def delete_attachment(self, memory_id: str, attachment_id: str, *,
                          purge: bool = False) -> Dict[str, Any]:
        """Delete an attachment (soft by default, ``purge=True`` removes the blob)."""
        return self._request("DELETE",
                             f"/v1/memories/{memory_id}/attachments/{attachment_id}",
                             params={"purge": "true" if purge else "false"}).json()

    def graph(self, *, bank: Optional[str] = None, domain: Optional[str] = None,
              limit: Optional[int] = None) -> Dict[str, Any]:
        """Read-only weak-graph snapshot: ``{nodes, edges, counts}``."""
        params: Dict[str, Any] = {}
        if bank is not None:
            params["bank"] = bank
        if domain is not None:
            params["domain"] = domain
        if limit is not None:
            params["limit"] = limit
        return self._request("GET", "/v1/graph", params=params).json()
