# memory-engine

English | [简体中文](README.zh-CN.md)

**A lifecycle-managed memory daemon for LLM agents — with source-of-truth anchoring, a freshness protocol, and mechanical write-back verification.**

Not another session-scoped memory store. Most agent memory solutions answer "what did we talk about?" — this one answers "what is still *true*, how fresh is it, and who changed it since I last looked?"

Production-proven in a single-host deployment serving a multi-agent system around the clock:

| Metric | Value | How measured |
|---|---|---|
| Recall P@5 | **0.583** vs 0.194 baseline (session-scoped FTS) | Internal eval, 36 production queries, three-way hybrid retrieval |
| Recall latency | **P95 = 21.7 ms** end-to-end (embedding + SQL + rerank) | Production daemon, ~2.3k live memories |
| Migration | 2,303 memories migrated, 3,681 session-stream entries retired | One-shot migration with freshness audit |

*The baseline is a production FTS retrieval of the same corpus; the eval set is private (contains real production content) — the harness in [`eval/`](eval/) is published, the dataset is not.*

---

## Why another memory engine?

Existing options fall into two camps, and we needed a third:

1. **Vector stores** (pgvector, Qdrant, …) — retrieval primitives, no lifecycle. Every memory you write is immortal and equally fresh, which is a bug, not a feature.
2. **Agent memory frameworks** — convenient, but retention semantics are opaque: summarization is lossy and irreversible, and nothing tells you *which* memory went stale when reality changed underneath it.

We ran a framework in production and hit exactly that wall: compressed context destroyed provenance, stale memories resurfaced as current facts, and there was no mechanical way to verify "said vs. done". memory-engine is the fix, extracted and hardened:

- **Three-way hybrid retrieval** — dense (pgvector HNSW) + Chinese-optimized full-text (PGroonga) + structured filters, fused with RRF. Every hit ships `score_parts` so you can audit *why* it ranked.
- **Lifecycle, not landfill** — every memory has a TTL state (`active` → `aged` → `archived` → `retired` → purge), with a fail-closed purge pipeline (daily backup verified + batch exported off-device + health check = all true, or nothing is deleted).
- **Freshness protocol** — the host records a *version cursor* per session. When it comes back, the engine diffs "changes since your last read" into a bounded, domain-scoped digest. Miss a week? You get exactly what changed, not a firehose.
- **Staleness gate at query time** — date-bucketed decay (fresh / aging / stale), known-outdated keyword scanning, and content-vs-reality consistency checks after config migrations. Dedup prevents repeats; the staleness gate prevents *resurrection*.
- **Source-of-truth anchoring** — memories point at canonical facts instead of duplicating them. One truth, many indexes.

## Architecture

```
                       ┌────────────────────────────────────────────┐
 agent / cron / hooks ─▶│  FastAPI daemon  (127.0.0.1:8766)          │
                       │  /v1/retain  /v1/recall  /v1/memories/{id} │
                       │  /v1/health  /v1/export  /v1/admin/backup  │
                       └───────┬───────────────────┬────────────────┘
                               │                   │
                    ┌──────────▼─────────┐  ┌──────▼───────────────┐
                    │ Embedder (CUDA)    │  │ PostgreSQL 18 :5433  │
                    │ Qwen3-Emb 0.6B     │  │ pgvector (HNSW)      │
                    │ fp16, 1.1 GB VRAM  │  │ PGroonga (CJK FTS)   │
                    └────────────────────┘  │ JSONB (score_parts)  │
                                            └──────────────────────┘
```

Single binary process, single database, no external services. The embedder is warm-resident; cold-start warmup is part of the health check (`/v1/health` asserts db + model + warm + ready, all four).

## Quick Start

```bash
git clone https://github.com/<you>/memory-engine.git && cd memory-engine

# 1. Database (PostgreSQL 18 + pgvector + PGroonga)
docker compose up -d

# 2. Embedding model (downloads Qwen3-Embedding-0.6B)
python -m memory_engine.download_model

# 3. Configure
cp .env.example .env   # set MEMORY_ENGINE_HOME, LLM_GATEWAY_API_KEY if needed

# 4. Run
pip install -e .
python -m memory_engine.cli serve          # or: systemd unit in deploy/
curl -s localhost:8766/v1/health | jq      # expect: status=ok, all four true

# 5. Tests
pytest tests/ -v
```

## The freshness protocol in one minute

```jsonc
// POST /v1/recall  — every answer carries provenance and freshness
{
  "hits": [{
    "id": "m_01J9…",
    "body": "…",
    "score_parts": {"dense": 0.41, "fts": 0.22, "struct": 0.10},  // auditable ranking
    "freshness": {"bucket": "aging", "age_days": 47, "last_verified": "2026-09-10"}
  }]
}
```

Writes are deduplicated (cosine ≥ 0.97 against the last 30 days, context required). Hosts keep a per-session read cursor; the next visit gets `POST /v1/freshness/digest` — bounded by budget, domain-scoped, same-key collapses to the net change. **No digest ≠ nothing happened; it means nothing you haven't already read.**

## Honest limitations

- **Single-host scale.** Designed for one agent system and one operator (tested to ~6k memories). No sharding story. If you need multi-tenant, this is the wrong tool *today*.
- **Internal eval only.** The 36-query eval set contains real production content and stays private. Numbers are reproducible in kind, not in dataset. The harness is published so you can build your own.
- **CJK-first FTS.** PGroonga is load-bearing for Chinese recall; English-only deployments may prefer to swap in a different FTS extension.
- **One embedder opinionated.** Qwen3-Embedding-0.6B fp16 on CUDA was chosen after measurement (see `docs/`); CPU-only hosts work but latency budgets change.
- **uuid7 variant bits** are not fully RFC 9562-conformant yet (time-prefix semantics verified; tracked in issues).

## Positioning

| | memory-engine | vector DBs | agent memory frameworks |
|---|---|---|---|
| Lifecycle / TTL | ✅ first-class | ❌ | ⚠️ varies |
| Freshness diffing for returning sessions | ✅ cursor-based | ❌ | ❌ |
| Retrieval explainability | ✅ score_parts | ⚠️ raw scores | ❌ |
| Fail-closed deletion pipeline | ✅ | n/a | ❌ |
| Drop-in for any framework | ❌ host-integrated | ✅ | ✅ |
| Ops weight | daemon + 1 DB | service | usually light |

Born as the memory layer of a self-built agent platform ([Hermes Agent](https://hermes-agent.nousresearch.com/docs) ecosystem); the reference-and-callback idea for oversized tool outputs takes after [NeverFull compression proxy](https://github.com/061115xhsm/NeverFull-NeverStop-LLM-Context-Compaction-Proxy) (design study — not yet wired in).

## License

MIT. See [CONTRIBUTING.md](CONTRIBUTING.md) for the test discipline (all-green pytest + zero-match grep audit are the merge bar).
