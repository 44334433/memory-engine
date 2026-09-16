# memory-engine-client

Typed Python client for [memory-engine](../../) — a lifecycle-managed memory daemon for LLM agents.

Three lines from zero to retrieval:

```python
from memoryengine import MemoryEngine

me = MemoryEngine("http://127.0.0.1:8766")            # your engine daemon
me.retain([{"content": "User runs deploys on Tuesdays", "context": "ops briefing"}])
hit = me.recall("when do we deploy?").results[0]
```

```bash
pip install memory-engine-client    # or: pip install -e sdk/python
```

## Client ↔ API mapping

| Client method | Engine route | Notes |
|---|---|---|
| `retain(items, bank=…, dedup=…)` | `POST /v1/retain` | items need `content` + `context` (write gate); returns `ids`, `dedup_skipped` |
| `recall(query, bank=…, top_k=…, filters=…)` | `POST /v1/recall` | hybrid retrieval; **degraded responses pass through** (`resp.degraded`) |
| `get_memories(bank=…, state=…, q=…, limit/offset)` | `GET /v1/memories` | paginated list/filter |
| `get_memory(id)` | `GET /v1/memories/{id}` | entry + provenance |
| `patch(id, body=…, supersede=…)` | `PATCH /v1/memories/{id}` | body changes re-embed; `supersede=True` = bi-temporal correction |
| `adopt(id, caller=…)` | `POST /v1/memories/{id}/adopt` | lifecycle adoption signal |
| `delete(id, purge=False)` | `DELETE /v1/memories/{id}` | soft retire by default, `purge=True` hard delete |
| `export(since_seq=…)` | `GET /v1/export` | generator over the JSONL logical backup |
| `health()` | `GET /v1/health` | four-truth check → `HealthStatus.ok` |
| `digest(session_id, cursor)` | `POST /v1/freshness/digest` | see version note below |
| `attach_file(id, data, filename=…, mime=…)` | `POST /v1/memories/{id}/attachments` | binary content, base64 over JSON; mime sniffed when omitted |
| `list_attachments(id)` / `delete_attachment(id, aid, purge=…)` | `GET/DELETE /v1/memories/{id}/attachments` | |
| `graph(bank=…, domain=…, limit=…)` | `GET /v1/graph` | read-only weak-graph snapshot `{nodes, edges, counts}` |

## Error semantics

Every failure raises a typed exception (all subclass `MemoryEngineError`, carrying
`.status`, `.detail`, `.body`):

| Engine | Exception |
|---|---|
| `400` / `422` (bad params, contract violation) | `InvalidInput` |
| `404` (unknown id) | `NotFound` |
| `404` on a documented-but-unmounted route | `EndpointNotAvailable` |
| `409` (e.g. rewriting a superseded entry in place) | `Conflict` |
| `503` (all recall routes down; body has `retryable` / `degraded`) | `EngineOverloaded` |
| other `5xx` | `EngineServerError` |
| connection refused / timed out | `EngineConnectionError` |

**Degraded ≠ error.** When the embedder is down the engine serves fts-only recall
as HTTP 200 with `degraded: true` and `failed_routes` — the client surfaces both
on `RecallResponse` instead of raising, so a degraded answer is never mistaken
for a dead engine (or for a healthy one).

### Version note on `digest`

The freshness protocol currently runs host-side (cursors + digest live in the
host's state dir, backed by the engine changelog), so engines of the `0.4.x`
series answer `POST /v1/freshness/digest` with 404 → the client raises
`EndpointNotAvailable`. The method is wired to the documented contract and
starts working unchanged once the engine mounts the route.

## Design notes

- HTTP via [httpx](https://www.python-httpx.org/) — sync, typed, timeout-aware;
  the only dependency.
- Fully type-annotated, `py.typed` shipped — inline completion and mypy work out of the box.
- Tests (`tests/test_client.py`) run against `httpx.MockTransport` — no engine needed:
  `cd sdk/python && pip install -e . && pytest tests/`
