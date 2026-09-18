# memory-engine

English | [简体中文](README.zh-CN.md)

**A lifecycle-managed memory daemon for LLM agents — with source-of-truth anchoring, a freshness protocol, and mechanical write-back verification.**

Not another session-scoped memory store. Most agent memory solutions answer "what did we talk about?" — this one answers "what is still *true*, how fresh is it, and who changed it since I last looked?"

Production-proven in a single-host deployment serving a multi-agent system around the clock:

| Metric | Value | How measured |
|---|---|---|
| LongMemEval-S R@5 | **0.652** vs 0.528 official BM25 baseline | 500-question full set, official harness; per-question results committed (`eval/lme/`) |
| Recall latency | **P95 ≈ 65–235 ms** end-to-end at 39k memories (typed filters narrow it fastest) | Production daemon; scale decay curve below |
| Migration | 2,303 memories migrated, 3,681 session-stream entries retired | One-shot migration with freshness audit |

*The baseline is a production FTS retrieval of the same corpus; the benchmark set is fully public (`eval/lme/`), the 36-query production baseline stays private (real content) — parameter changes must clear both.*

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

## What's inside

| Capability | How it works | Hard numbers |
|---|---|---|
| **Four-route recall** | dense vector + PGroonga CJK full-text + temporal routing + one-hop graph pull-back, fused by weighted RRF | hybrid beats BM25 by +12.4pp (see Benchmark) |
| **Auditable scoring** | every hit ships `score_parts` — per-route rank, freshness factor, staleness factor, tier weight | no black-box ranking |
| **Bi-temporal memory** | `valid_at` / `invalid_at` on every entry; corrections *supersede* — old rows are never deleted | history is replayable |
| **Write protection** | prompt-injection gate (external content scanned, `external_only` default) + exact-hash and semantic dedup (cos ≥ 0.97 vs last 30 days) + source-tier downweighting (web 0.85, cron 0.9) | poisoned input never becomes trusted memory |
| **Visibility model** | caller-scoped: `main` / per-agent / subagent see disjoint views; `private` entries invisible to non-owners | zero cross-host leakage |
| **Graceful degradation** | embedder failure → explicit fts-only mode (200 + `degraded` + `failed_routes`), self-heal thread retries every 60 s and pulls vector recall back | degraded ≠ dead, and it says so |
| **Typed memory** | every entry classified `semantic` / `procedural` / `episodic` at write time (heuristic classifier, backfill script included); decay half-lives differ per type (semantic ×2 slowest, episodic ×1 unchanged); `filters.memory_type` narrows all three routes | 37k entries backfilled; filtered recall runs faster than unfiltered |
| **Outcome feedback** | `POST /v1/feedback {memory_id, outcome: adopted/corrected/useless}` — per-item EMA polarity (α=0.1) feeds the decay shield; corrected/useless land in the hard-query pool | closes the learn-from-the-host loop, not just introspection |
| **Core-memory block** | `GET /v1/core-block` — pinned entries plus auto-selected (high-polarity, repeatedly-adopted semantic/procedural) rendered under a hard char budget, read with zero side effects | host pulls it per turn; default off, no system-prompt writes |
| **Lifecycle TTL** | six-state machine (trial → active → … → retired → deleted): adoption extends life, 90 days of zero access decays | unused memory stops costing quality |
| **Disaster recovery** | PG snapshots + timer, plus full logical export (`GET /v1/export`) | RTO measured at 2.5 s |
| **Multi-host ready** | `tenant_id` / `agent_id` columns already in schema; isolation enforcement lands when a second host actually connects | schema now, enforcement on trigger |
| **Embedder swap** | pluggable: local Qwen3 or any OpenAI-compatible endpoint, one config line | re-embedding versioned via `embed_ver` |
| **Graph visualization** | zero-build single-file UI (`deploy/graph.html`, sigma.js WebGL) + read-only `GET /v1/graph` | 11k edges / 2.4k entities on the production corpus |
| **Image attachments** | `POST /v1/memories/{id}/attachments` — content-addressed storage, optional VLM caption (degrades gracefully if unconfigured), caption embedded with the same text embedder | no images-in-vector yet by design (caption-mediated, Mem0-style) |

## API surface

```
POST /v1/retain                 write (dedup + injection scan + tiering)     → ids, dedup_skipped
POST /v1/recall                 four-route hybrid search                     → results + score_parts + routes
                                 filters: memory_type / as_of / date_range /
                                 staleness / tags / tenant_id / agent_id
POST /v1/freshness/digest       what changed since my last cursor            → budgeted, domain-scoped
GET  /v1/core-block             always-on entries under a hard char budget   → text + ids (default off)
POST /v1/feedback               host outcome signal: adopted / corrected /
                                useless (EMA polarity, feeds decay + pool)   → polarity
GET  /v1/memories               list/filter (bank, domain, state, time, as_of, type) → paginated
GET  /v1/memories/{id}          single entry + full provenance
PATCH /v1/memories/{id}         update fields (re-embeds when body changes);
                                body+supersede=true starts a new version, pinned=true pins it
POST /v1/memories/{id}/adopt    report host adoption (feeds use-it-or-lose-it)
POST /v1/memories/{id}/attachments    image attachments (content-addressed, optional VLM caption)
DELETE /v1/memories/{id}        retire (soft), `?purge=true` for hard delete
GET  /v1/graph                  read-only entity/edge graph
GET  /v1/export                 full JSONL export (logical backup)
GET  /v1/health                 four-truth check: db + model + warm + ready
```

## Self-evolution

The engine tunes itself from its own traffic — five loops, all running today:

> **Scope note, because reviewers read this differently than intended:** everything below optimizes *retrieval and memory behavior inside the engine*. Deliberately out of scope: learning that rewrites the host agent's prompts, policies, or actions — that loop belongs to the agent layer (e.g. lesson capture → constitution update in an agent framework). A memory backend that "improves agent behavior" by itself is an overreach signal, not a feature.

- **Parameter self-tuning** (`scripts/param_autotune.py` + weekly systemd timer): mutates retrieval weights (RRF k / route weights), replays the eval question set per-question, and commits a new parameter snapshot only after two consecutive rounds beat the frozen baseline by ≥5%. Otherwise it rolls back and logs why. First scheduled run: dry-run.
- **Use-it-or-lose-it** (lifecycle): the `access_events` table feeds the TTL state machine — memories that get recalled and *adopted* by the host get their lifespan extended; 90 days of zero access demotes them toward decay. Memory that is never used stops costing retrieval quality.
- **Failure backflow** (`src/memory_engine/hard_queries.py`): every recall that returns nothing (or a below-threshold top score) lands in a hard-query pool. Periodic analysis turns the pool into concrete tuning proposals instead of letting failures evaporate.
- **Outcome backflow** (`POST /v1/feedback`): the host reports what happened to a recalled memory — adopted, corrected, or useless. Each signal moves a per-item EMA polarity that shields against decay; corrections and useless votes also land in the hard-query pool. This is the loop that learns *from the host* rather than from the engine's own logs.
- **Consolidation** (`scripts/consolidate_synthesize.py`): clusters of same-domain active memories synthesize into observation drafts via the batch LLM channel — drafts only, a human reviews before anything enters the store. No silent rewrites of your memory.

Two honesty notes: the self-tuning gate uses paired per-question testing (not aggregate averages, which hide regressions), and none of these loops can delete or rewrite memories — consolidation stops at draft, deletion stays manual.

## Knowledge graph

Beyond vectors and full-text, the schema carries an entity-relation layer (`entities` / `edges`, lightweight, no separate graph DB): memories are linked by extracted entities, and recall runs a fourth route over the graph — a one-hop pull-back catches the related memory that vector similarity missed. The graph is no longer a toy: LLM extraction ran over the full production corpus (**2,463 entities, ~8k semantic edges** on top of 3.5k co-occurrence edges, 2026-09-18), with typed relations (related / parent_child / causal / contradicts). The graph ships with a **zero-build visualization UI** (`deploy/graph.html` — single file, graphology + sigma.js v3 via CDN, served next to the daemon: filter by bank/domain, click a node for full memory provenance) and a read-only `GET /v1/graph` endpoint. Entity disambiguation, multi-hop traversal and supersede-chain queries are on the roadmap; if you want a full property graph engine with a browser, this is still not that tool.

## Quick Start

Single-host daemon (see `scripts/`), or from a client with the Python SDK:

```python
from memoryengine import MemoryEngine

mem = MemoryEngine("http://127.0.0.1:8766")          # one line: point at the daemon
mem.retain("用户偏好橙白主题", context="UI 设计基线", source_tier="user")
hits = mem.recall("界面配色用什么", top_k=5)           # typed results with score_parts
```

The SDK (`sdk/python/`, MIT, httpx-based) covers the full `/v1` surface — retain / recall / freshness digest / memories CRUD / adopt / attachments / graph / export — with exception mapping (`InvalidInput` on 400/422, `NotFound` on 404, `Conflict` on 409, `EngineOverloaded` on 503 with the `retryable` flag) and degraded responses passed through as data rather than raised as errors. 25 unit tests run against mocked transport; nothing about the daemon's lifecycle is required to use it.

```bash
pip install -e sdk/python    # from a checkout; py.typed included for editors
```

Single-host daemon notes:

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

## Benchmark

Evaluated on [LongMemEval](https://github.com/xiaowu0162/LongMemEval) — 500-question full set, official harness (2026-09-16):

| Metric | memory-engine | Official BM25 baseline |
|---|---|---|
| Recall@5 | **0.652** | 0.528 |

The +12.4pp gain comes from hybrid retrieval: dense vectors (Qwen3-Embedding-0.6B) + PostgreSQL full-text (PGroonga) + temporal routing, fused with weighted RRF. **Full data & reproduction pipeline: [`eval/lme/`](eval/lme/README.md)** — per-question results, corpus, and scripts are committed.

**Full-corpus honesty note**: with no filtering at all, retrieval over the full 122K-memory store (production memories merged with the entire benchmark corpus — maximal heterogeneity, the hardest configuration) scores R@5 0.122, against BM25's 0.179 under the identical condition. The scale decay curve below isolates the cause: heterogeneity, not size. Production deployments mitigate with time-windowed filtering, the freshness protocol, typed retrieval (`memory_type` filters), core-memory blocks for always-on entries, and — coming next — pluggable reranking.

**Scale decay curve (synthetic haystack, 2026-09-18)**: does the 0.122 come from corpus size? No — same 100-question set, same official scorer, only the synthetic haystack grows (nested distractors, no production memories):

| Haystack turns | R@5 | R@1 |
|---|---|---|
| 500 | 0.97 | 0.97 |
| 1,000 | 0.96 | 0.96 |
| 2,000 | 0.94 | 0.94 |
| 4,000 | 0.91 | 0.91 |
| 6,000 | 0.88 | 0.88 |

Every doubling costs 2–3pp — a smooth asymptote, no cliff at 6k. The gap between 0.88 here (designed distractors, even at 6k turns) and 0.122 on the full heterogeneous store is therefore mostly heterogeneity, not scale: real memories compete with each other, designed distractors do not. Reproduce with `eval/lme/decay_curve.py` (results merge across restarts; one config per bucket via `DECAY_BUCKETS`).

## Honest limitations

- **Single-host scale.** Designed for one agent system and one operator (running at ~40k memories; tested to 122k in the no-filter worst case below). No sharding story. If you need multi-tenant, this is the wrong tool *today*.
- **Eval split, on purpose.** Two tracks: the public LongMemEval set (500 questions, `eval/lme/`, fully reproducible) and a 36-query private baseline containing real production content — reproducible in kind, not in dataset. Dual-gate: parameter changes must clear both before they stick.
- **CJK-first FTS.** PGroonga is load-bearing for Chinese recall; English-only deployments may prefer to swap in a different FTS extension.
- **One embedder opinionated.** Qwen3-Embedding-0.6B fp16 on CUDA was chosen after measurement (see `docs/`); CPU-only hosts work but latency budgets change.
- **uuid7 variant bits** are not fully RFC 9562-conformant yet (time-prefix semantics verified; tracked in issues).

## Scaling path (what changes when single-host stops being enough)

"No sharding story" above is a scope statement, not a dead end. The measured headroom and the ordered escalation are public so nobody has to guess:

| Stage | Trigger signal (not a date) | Work |
|---|---|---|
| S0 (today) | — | single host; P95 recall 21-31 ms at 6k entries (line: 200 ms) |
| S1 replica-ready | need ≥2 daemon copies, or GPU contention on embedder startup | embedder served separately via the pluggable provider (already `openai_compat`-ready); decision-state (param snapshots, word lists) converges into PG with advisory-lock single-writer — 1.5-2.5 days |
| S2 multi-host | a second host genuinely connects | `tenant_id`/`agent_id` columns exist already; add quota isolation + privacy boundary + recall filtering — 3-5 days |
| S3 asset-grade durability | the memory store becomes someone's production asset | PG streaming replication + remote standby (export JSONL already provides logical cross-version backup) — engine code unchanged |
| S4 hosted service | explicit product decision | billing, tenant console, observability — a different product, not on this roadmap |

What is deliberately *not* planned: migrating to a distributed vector DB (pgvector + HNSW stays 10 ms-class to millions of rows — a cluster buys nothing here) or splitting the daemon into microservices (the single binary is the point).

## Positioning

| | memory-engine | vector DBs | agent memory frameworks |
|---|---|---|---|
| Lifecycle / TTL | ✅ first-class | ❌ | ⚠️ varies |
| Freshness diffing for returning sessions | ✅ cursor-based | ❌ | ❌ |
| Retrieval explainability | ✅ score_parts | ⚠️ raw scores | ❌ |
| Fail-closed deletion pipeline | ✅ | n/a | ❌ |
| Drop-in for any framework | ❌ host-integrated | ✅ | ✅ |
| Ops weight | daemon + 1 DB | service | usually light |

Built as a memory layer for [Hermes Agent](https://hermes-agent.nousresearch.com/docs), an open-source agent framework by Nous Research; the reference-and-callback idea for oversized tool outputs takes after [NeverFull compression proxy](https://github.com/061115xhsm/NeverFull-NeverStop-LLM-Context-Compaction-Proxy) (design study — not yet wired in).

## Maintenance tooling

- [`scripts/purge_archived.py`](scripts/purge_archived.py) — archived-TTL purge with fail-closed triple gates (same-day backup exists, batch exported to external disk, engine health four-true). Dry-run by default; deletion goes through the HTTP API only, never direct SQL.
- Regression evidence files (`tests/last_smoke.json`, `tests/last_stage2.json`) are generated per run and kept out of version control; last full run: 28/28 checks, P95 = 15.8 ms under eval-corpus load.

## License

MIT. See [CONTRIBUTING.md](CONTRIBUTING.md) for the test discipline (all-green pytest + zero-match grep audit are the merge bar).
