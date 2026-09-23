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

- **Three-way hybrid retrieval** — dense (pgvector HNSW) + Chinese-optimized full-text (PGroonga) + structured filters, fused with RRF. Every hit ships `score_parts` so you can audit *why* it ranked — the field set itself is versioned (`schema_version`) so consumers can parse defensively.
- **Lifecycle, not landfill** — every memory has a TTL state (`active` → `aged` → `archived` → `retired` → purge), with a fail-closed purge pipeline (daily backup verified + batch exported off-device + health check = all true, or nothing is deleted).
- **Freshness protocol** — the host records a *version cursor* per session. When it comes back, the freshness layer diffs "changes since your last read" into a bounded, domain-scoped digest (in this reference deployment the diffing runs host-side against the engine's changelog seq). Miss a week? You get exactly what changed, not a firehose.
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

## Architecture decisions

Short ADR-style notes for the trade-offs reviewers ask about. Each: the call, why, what it costs, when it flips.

- **AD-1: one PostgreSQL, five roles.** Vector (pgvector HNSW) + CJK full-text (PGroonga) + structured JSONB + bitemporal history + a light entity graph in one store — because one ACID snapshot *is* the backup, one query path *is* the audit, and cross-system sync bugs are a class we refuse to own. Cost: no horizontal write scaling. Flip condition: the S1–S3 ladder in ROADMAP (trigger-gated, not dated).
- **AD-2: embedder resident in-process.** Warm CUDA residency (1.14 GB, 1024-dim) means retain/recall never pay a cold start or a network hop, and failure degrades inside one process lifecycle to explicit fts-only mode. Cost: the daemon wants GPU-class hardware (CPU works; latency budgets change). Flip condition: S1 — the pluggable provider (`openai_compat`) already makes extraction a config migration, not a rewrite.
- **AD-3: HTTP is the only door.** Dedup, injection gate, and source-tier defaults live in the API, so consumers must not import around them — the single door is the enforcement point, which is why "no library bypass" is a test-enforced rule, not etiquette. Cost: one local hop (ms-level) plus a running daemon. Flip condition: none — that would delete the trust boundary.
- **AD-4: weighted RRF over learned fusion.** Ranking must be explainable per hit (`score_parts` shows each route's contribution), so a bad ranking is a diagnosable fact, not an oracle question. Cost: fusion weights are hand-tuned — which is exactly what the autotune loop exists to challenge under a paired-gate. Flip condition: none planned; the reranker stage is additive and keeps the explainable scores visible.
- **AD-5: supersede, never overwrite.** A correction inserts a new version and time-closes the old (`valid_at`/`invalid_at`); in-place edits of live content are 409-rejected. Cost: write amplification — 74,283 changelog rows across 55,992 subject memories against ~39k live rows (2026-09-19). Flip condition: none; physical removal only through the triple-gated purge.

## What's inside

| Capability | How it works | Hard numbers |
|---|---|---|
| **Four-route recall** | dense vector + PGroonga CJK full-text + temporal routing + one-hop graph pull-back, fused by weighted RRF | hybrid beats BM25 by +12.4pp (see Benchmark) |
| **Auditable scoring** | every hit ships `score_parts` — per-route rank, freshness factor, staleness factor, tier weight | no black-box ranking |
| **Bi-temporal memory** | `valid_at` / `invalid_at` on every entry; corrections *supersede* — old rows are never deleted | history is replayable |
| **Write protection** | prompt-injection gate (external content scanned, `external_only` default) + exact-hash and per-bank semantic dedup (cos threshold per bank: knowledge 0.98 / hermes 0.95, unregistered banks fall back to the global default, env-appendable) + source-tier downweighting (web 0.85, cron 0.9) | poisoned input never becomes trusted memory; thresholds have one change point in config |
| **Visibility model** | caller-scoped: `main` / per-agent / subagent see disjoint views; `private` entries invisible to non-owners | zero cross-host leakage |
| **Graceful degradation** | embedder failure → explicit fts-only mode (200 + `degraded` + `failed_routes`), self-heal thread retries every 60 s and pulls vector recall back | degraded ≠ dead, and it says so |
| **Typed memory** | every entry classified `semantic` / `procedural` / `episodic` at write time (heuristic classifier, backfill script included); decay half-lives differ per type (semantic ×2 slowest, episodic ×1 unchanged); `filters.memory_type` narrows all three routes | 37k entries backfilled; filtered recall runs faster than unfiltered |
| **Outcome feedback** | `POST /v1/feedback {memory_id, outcome: adopted/corrected/useless}` — per-item EMA polarity (α=0.1) feeds the decay shield; corrected/useless land in the hard-query pool; optional `group` + `retrieved_ids` target what a recall actually consumed | closes the learn-from-the-host loop, not just introspection |
| **Observability window** | `GET /v1/metrics` — 7-day read-only view: feedback liveness (writes vs distinct consumers, join-drift), injected two-state flags, skip counters, pinned share, core-block staleness | if a loop stops consuming, the number moves — no silent shadows |
| **Core-memory block** | `GET /v1/core-block` — pinned entries plus auto-selected (high-polarity, repeatedly-adopted semantic/procedural) rendered under a hard char budget, read with zero side effects | host pulls it per turn; default off, no system-prompt writes |
| **Lifecycle TTL** | six-state machine (trial → active → … → retired → deleted): adoption extends life, 90 days of zero access decays | unused memory stops costing quality |
| **Disaster recovery** | PG snapshots + timer, plus full logical export (`GET /v1/export`) | RTO measured at 2.5 s |
| **Multi-host ready** | `tenant_id` / `agent_id` columns + PG row-level-security migration (`scripts/migrations/008_rls.sql`, session pass-through when unset) + `MULTI_TENANT` recall filter — both default **off** | zero behavior change until a host opts in (enable in 3 steps below) |
| **LangChain ready** | `integrations/langchain_memory.py` — `BaseRetriever` + memory class over plain HTTP (`requests` only); importable with shims when langchain isn't installed | hermetic mock-HTTP tests (`tests/test_langchain_adapter.py`) |
| **Embedder swap** | pluggable: local Qwen3 or any OpenAI-compatible endpoint, one config line | re-embedding versioned via `embed_ver` |
| **Graph visualization** | zero-build single-file UI (`deploy/graph.html`, sigma.js WebGL) + read-only `GET /v1/graph` | 11k edges / 2.4k entities on the production corpus |
| **Image attachments** | `POST /v1/memories/{id}/attachments` — content-addressed storage, optional VLM caption (degrades gracefully if unconfigured), caption embedded with the same text embedder | no images-in-vector yet by design (caption-mediated, Mem0-style) |

## API surface

```
POST /v1/retain                 write (dedup + injection scan + tiering)     → ids, dedup_skipped
POST /v1/recall                 four-route hybrid search                     → results + score_parts (schema_version: 1) + routes
                                 filters: memory_type / as_of / date_range /
                                 staleness / tags / tenant_id / agent_id
POST /v1/freshness/digest       what changed since my last cursor            → budgeted, domain-scoped
                                 (host-side pattern in this build: raw material is changelog seq +
                                  /v1/memories/{id}/chain; route not mounted — POST returns 404)
GET  /v1/core-block             always-on entries under a hard char budget   → text + ids (default off)
POST /v1/feedback               host outcome signal: adopted / corrected /
                                useless (EMA polarity, feeds decay + pool)   → polarity
                                optional: group (E1 per-group rollup) +
                                retrieved_ids (M1 per-recall consumption)
GET  /v1/metrics                read-only observability window (7d): feedback
                                liveness probe (writes vs distinct consumers,
                                join-drift), injected two-state flags, skip
                                counters, pinned share, core-block staleness
GET  /v1/memories               list/filter (bank, domain, state, time, as_of, type) → paginated
GET  /v1/memories/{id}          single entry + full provenance
GET  /v1/memories/{id}/chain    supersede-chain traversal (recursive CTE, max_hops, cycle guards)
GET  /v1/graph                  read-only entity/edge graph
GET  /v1/graph/neighbors        BFS neighbors (hops=1-2, as_of edge filter, entity bridges)
PATCH /v1/memories/{id}         update fields (re-embeds when body changes);
                                body+supersede=true starts a new version, pinned=true pins it
POST /v1/memories/{id}/adopt    report host adoption (feeds use-it-or-lose-it)
POST /v1/memories/{id}/attachments    image attachments (content-addressed, optional VLM caption)
DELETE /v1/memories/{id}        retire (soft), `?purge=true` for hard delete
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

Beyond vectors and full-text, the schema carries an entity-relation layer (`entities` / `edges`, lightweight, no separate graph DB): memories are linked by extracted entities, and recall runs a fourth route over the graph — a one-hop pull-back catches the related memory that vector similarity missed. The graph is no longer a toy: LLM extraction ran over the full production corpus (**2,463 entities, ~8k semantic edges** on top of 3.5k co-occurrence edges, 2026-09-18), with typed relations (related / parent_child / causal / contradicts). The graph ships with a **zero-build visualization UI** (`deploy/graph.html` — single file, graphology + sigma.js v3 via CDN, served next to the daemon: filter by bank/domain, click a node for full memory provenance), a read-only `GET /v1/graph`, **supersede-chain traversal** (`GET /v1/memories/{id}/chain?max_hops=5` — recursive CTE over the version lineage with cycle guards and per-version time windows) and **two-hop neighbors** (`GET /v1/graph/neighbors?id=&hops=2&as_of=` — BFS with entity bridges and as-of edge filtering, both shipped 2026-09-19). Entity disambiguation is on the roadmap; if you want a full property graph engine with a browser, this is still not that tool.

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
git clone https://github.com/44334433/memory-engine.git && cd memory-engine

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

## Accessing from mainland China

GitHub direct connectivity from mainland China is intermittent — if `git clone` stalls, use a community accelerator (both verified HTTP 200 on 2026-09-23; third-party accelerators rotate over time, treat as best-effort):

```bash
git clone https://ghfast.top/https://github.com/44334433/memory-engine.git
# or
git clone https://gh-proxy.com/https://github.com/44334433/memory-engine.git
```

A mainland-reachable synced mirror is planned (gitcode import). Until it lands, the accelerators above are the shortest working path.

## Five-minute demo

Four steps from empty to auditable memory — every command below and every output shown was run against the same `/v1` surface this README documents (captured 2026-09-20; `…` marks trimmed ids and text). The database layer is the compose image from Quick Start (`docker/`), nothing else.

**1. Start** (once, ~2 min: image build + model download):

```bash
docker compose up -d                      # PG18 + pgvector + PGroonga, mapped to 127.0.0.1:5433
pip install -e . && python -m memory_engine.download_model
python -m memory_engine.cli serve &
curl -s localhost:8766/v1/health | jq '.status'    # "ok" — then run the next three steps
```

**2. Write** — `POST /v1/retain` (context required; every write books a changelog seq):

```bash
curl -s -X POST localhost:8766/v1/retain -H 'content-type: application/json' -d '{
  "bank": "knowledge", "caller": "me",
  "items": [{"content": "Warehouse A stock: 42 cases", "context": "demo", "title": "A stock", "source_tier": "user"}]}'
# → {"ids":["01a0bc1e-8641-…"],"dedup_skipped":0,"dedup_existing":[],"seq":56076,"took_ms":20.8}
```

**3. Read** — `POST /v1/recall`; the ranking arrives with its parts, versioned:

```bash
curl -s -X POST localhost:8766/v1/recall -H 'content-type: application/json' \
  -d '{"query":"how many cases in warehouse A","top_k":3,"bank":"knowledge"}'
# → {"results":[{"id":"01a0bc1e-8641-…","score":0.014631,
#      "score_parts":{"schema_version":1,"rrf":0.016393,"pri":1.05,"life":0.85,
#                     "stale":1.0,"tier_weight":1.0,"graph":0.0,
#                     "outcome":null,"polarity":null,"routes":{"vector":1}},
#      "ttl_state":"candidate","staleness":"fresh","memory_type":"episodic", …}], …}
```

**4. Revise — and watch the trail** — a correction supersedes instead of overwriting, and the ledger is queryable:

```bash
curl -s -X PATCH localhost:8766/v1/memories/01a0bc1e-8641-… \
  -H 'content-type: application/json' -d '{"body":"Warehouse A stock: 37 cases","supersede":true}'
# → {"id":"474c9f6d-81c3-…","superseded_from":"01a0bc1e-8641-…","is_current":true, …}

curl -s "localhost:8766/v1/memories/474c9f6d-81c3-…/chain?max_hops=5"
# → versions: [ {position 0, "42 cases", is_current: false, superseded_by: 474c9f6d-…},
#               {position 1, "37 cases", is_current: true } ]
```

One honesty note, because step 4 is where the docs and the code once diverged: the cursor-diff **digest** of the freshness protocol runs **host-side** in this reference deployment — this build mounts no `/v1/freshness/digest` route (a POST there returns 404; the SDK maps that to `EndpointNotAvailable`). The engine side supplies the raw material the host digests against: the changelog seq on every write and the version chain above.

## The freshness protocol in one minute

```jsonc
// POST /v1/recall  — every answer carries provenance and freshness
// (trimmed from a live capture, 39k-entry daemon, 2026-09-20)
{
  "results": [{
    "id": "01a0bc1e-8641-…",
    "title": "…", "body": "…",
    "score": 0.014631,
    "score_parts": {                        // auditable ranking; schema_version guards the field set
      "schema_version": 1, "rrf": 0.016393, "pri": 1.05, "life": 0.85,
      "stale": 1.0, "tier_weight": 1.0, "graph": 0.0,
      "outcome": null, "polarity": null, "routes": {"vector": 1}
    },
    "ttl_state": "candidate", "staleness": "fresh", "memory_type": "episodic"
  }]
}
```

Writes are deduplicated (semantic dedup with per-bank cosine thresholds — knowledge 0.98 / hermes 0.95 / default per config — against the last 30 days, context required). Hosts keep a per-session read cursor; the next visit gets a `freshness/digest` computed from the engine's changelog seq (see the demo note: host-side pattern in this build) — bounded by budget, domain-scoped, same-key collapses to the net change. **No digest ≠ nothing happened; it means nothing you haven't already read.**

## Benchmark

Evaluated on [LongMemEval](https://github.com/xiaowu0162/LongMemEval) — 500-question full set, official harness (2026-09-16):

| Metric | memory-engine | Official BM25 baseline |
|---|---|---|
| Recall@5 | **0.652** | 0.528 |

The +12.4pp gain comes from hybrid retrieval: dense vectors (Qwen3-Embedding-0.6B) + PostgreSQL full-text (PGroonga) + temporal routing, fused with weighted RRF. **Full data & reproduction pipeline: [`eval/lme/`](eval/lme/README.md)** — per-question results, corpus, and scripts are committed. **One command:** `bash eval/lme/run_eval.sh` (docker compose → PG → schema + migrations → engine → ingest → 500-question scoring → R@5; `--dry-run` prints the plan, `--with-qa` adds the self-judged QA subset).

### Methodology — how to read these numbers

- **Public set.** 500 questions sampled from LongMemEval-S (dataset snapshot sha `2ec2a55`, official cleaned-2025-09-19 release) with seed 42. Scored at session granularity, `recall_all@5`, under the official protocol: 81 questions without retrieval targets (30 `absence` + 51 no-target) drop out, 419 are scored — the BM25 column is scored on the same 419. The scorer path is pinned to the official repo (commit `9e0b455`), and the full chain reruns via `eval/lme/run_eval.sh`.
- **Hardware / configuration.** One machine: RTX 5070 Ti Laptop 12 GB, Ryzen 9 9955HX (16 cores), 30 GB RAM; PostgreSQL 18.6 + pgvector + PGroonga; Qwen3-Embedding-0.6B fp16 resident on GPU (~1.2 GB); engine `v0.5.0-w3` with default recall parameters. The reranker stage was off for these numbers — they predate W3.
- **Caliber limits.** Retrieval-only scoring, no LLM in the loop — that is what makes 0.652 vs 0.528 a same-yardstick comparison, and also why neither number may be compared against end-to-end QA accuracy, ours (self-judged n=100 subset) or anyone's. The decay-curve corpus is a synthetic ShareGPT-derived haystack with designed distractors and zero production memories — per-question traceable in `qa_log.jsonl`.
- **The private set, and why it stays private.** The second track is a 36-query baseline run against real production memories. Publishing it would publish the user's own memory content — a privacy wall, not a convenience choice. It is reproducible *in kind* (the same protocol on your own corpus), not in dataset; the honest cost is that outsiders cannot audit private-track numbers. What binds the tracks: the dual gate — a parameter change must clear both, so the auditable track holds a veto over the unauditable one.

**Full-corpus honesty note**: with no filtering at all, retrieval over the full 122K-memory store (production memories merged with the entire benchmark corpus — maximal heterogeneity, the hardest configuration) scores R@5 0.122, against BM25's 0.179 under the identical condition. The scale decay curve below isolates the cause: heterogeneity, not size. Production deployments mitigate with time-windowed filtering, the freshness protocol, typed retrieval (`memory_type` filters), core-memory blocks for always-on entries, and pluggable reranking (Qwen3 cross-encoder stage, shipped 2026-09-19 behind `RERANK_ENABLED=false` — off until the A/B evidence says on).

**Scale decay curve (synthetic haystack, 2026-09-18)**: does the 0.122 come from corpus size? No — same 100-question set, same official scorer, only the synthetic haystack grows (nested distractors, no production memories):

| Haystack turns | R@5 | R@1 |
|---|---|---|
| 500 | 0.97 | 0.97 |
| 1,000 | 0.96 | 0.96 |
| 2,000 | 0.94 | 0.94 |
| 4,000 | 0.91 | 0.91 |
| 6,000 | 0.88 | 0.88 |

Every doubling costs 2–3pp — a smooth asymptote, no cliff at 6k. The gap between 0.88 here (designed distractors, even at 6k turns) and 0.122 on the full heterogeneous store is therefore mostly heterogeneity, not scale: real memories compete with each other, designed distractors do not. Reproduce with `eval/lme/decay_curve.py` (results merge across restarts; one config per bucket via `DECAY_BUCKETS`).

**Where the bottleneck at 6k actually is.** The curve invites the question — 0.97 at 500, 0.88 at 6,000: what breaks at 6k? Honest answer in three parts. **Not latency:** engine-side recall P95 measures 21–31 ms at 6k entries (S0 row below; the 200 ms budget line was never crossed by corpus size), and end-to-end P95 at the 39k production store runs 65–235 ms with query embedding and HTTP inside the measurement. **Not a cliff:** 2–3 pp per doubling is a smooth slope under the conditions the harness can synthesize. **What actually bites past 6k is quality competition under heterogeneity** — and the only evidence for that is the full-store note above (0.122 at 122K, no filters), which is exactly why the mitigation stack is filters + types + reranker rather than sharding. Boundary stated: the decay curve is synthetic-caliber (designed distractors, fixed question set); it proves there is no size cliff inside corpora the harness can build, but it promises no slope for arbitrary real corpora — that extrapolation is unmeasured.

## Honest limitations

Three labels, so readers can tell design choices from debts: **[by-design]** = a deliberate scope decision with a trigger condition for revisiting; **[planned]** = accepted gap with work queued; **[accepted-cost]** = a trade-off we keep because the alternative is worse.

- **[by-design] Single-host scale.** Designed for one agent system and one operator (running at ~40k memories; tested to 122k in the no-filter worst case below). No sharding story. If you need multi-tenant, this is the wrong tool *today* — the schema carries `tenant_id`/`agent_id`, and the Scaling path below defines the triggers.
- **[accepted-cost] Eval split.** Two tracks: the public LongMemEval set (500 questions, `eval/lme/`, fully reproducible) and a 36-query private baseline containing real production content — reproducible in kind, not in dataset. Dual-gate: parameter changes must clear both before they stick.
- **[accepted-cost] CJK-first FTS, and the cross-language question nobody can answer yet.** PGroonga is load-bearing for Chinese recall; English-only deployments may prefer another FTS. English side: the public 0.652 was measured with PGroonga as the configured FTS over an English corpus, but the committed per-question results store scores, not `routes` — so FTS's individual contribution on English is not re-auditable from public data. "PGroonga does not hurt English" is therefore a configuration observation, not a measured ablation; an A/B against a different English FTS has never been run. Chinese side: no public benchmark exists at all — the only CJK evaluation is the private production baseline, which by definition cannot ship here. Two registered candidates, both unfunded: English FTS-swap A/B on LongMemEval, and a public CJK retrieval set.
- **[by-design] One embedder opinionated.** Qwen3-Embedding-0.6B fp16 on CUDA was chosen after measurement (see `docs/`); swapping is supported (pluggable provider + `embed_ver` re-embedding), CPU-only hosts work but latency budgets change.
- **[planned] uuid7 variant bits** are not fully RFC 9562-conformant yet (time-prefix semantics verified; tracked in issues).
- **[planned] Manual safety rails.** Consolidation stops at drafts, deletion stays manual, core-memory block ships off by default — deliberate fail-closed choices; automation widens only after production evidence accumulates.
- **[by-design] The deletion gate, exactly as it behaves.** `DELETE /v1/memories/{id}` is *soft*: the row stays (`ttl_state='retired'`), its embedding is cleared, the changelog books the op, recall stops seeing it — and `GET` on the same id still returns it. Audit first, amnesia never by accident (live-verified 2026-09-20: retired → GET 200, recall invisible). Hard removal is `?purge=true` only, and the automated path (`scripts/purge_archived.py`) sits behind the three fail-closed gates (same-day backup + off-device export + health all-true). What we do **not** claim: a false-block rate — how often the gates would refuse a legitimate purge is not instrumented. The design stance says blocking is cheap (fix the precondition, rerun) while over-deleting is not; no measurement exists to trade against that stance, and none is claimed.
- **[planned] Host-driven feedback.** The outcome loop learns only what the host reports; automatic inference from host behavior is the next step.

## Scaling path (what changes when single-host stops being enough)

"No sharding story" above is a scope statement, not a dead end. The measured headroom and the ordered escalation are public so nobody has to guess:

| Stage | Trigger signal (not a date) | Work |
|---|---|---|
| S0 (today) | — | single host; P95 recall 21-31 ms at 6k entries (line: 200 ms) |
| S1 replica-ready | need ≥2 daemon copies, or GPU contention on embedder startup | embedder served separately via the pluggable provider (already `openai_compat`-ready); decision-state (param snapshots, word lists) converges into PG with advisory-lock single-writer — 1.5-2.5 days |
| S2 multi-host | a second host genuinely connects | prep layer shipped: `tenant_id`/`agent_id` columns, RLS migration, `MULTI_TENANT` recall filter (all default-off) — enable in 3 steps below; remaining: quota isolation + privacy boundary + per-session `app.tenant_id` wiring |
| S3 asset-grade durability | the memory store becomes someone's production asset | PG streaming replication + remote standby (export JSONL already provides logical cross-version backup) — engine code unchanged |
| S4 hosted service | explicit product decision | billing, tenant console, observability — a different product, not on this roadmap |

What is deliberately *not* planned: migrating to a distributed vector DB (pgvector + HNSW stays 10 ms-class to millions of rows — a cluster buys nothing here) or splitting the daemon into microservices (the single binary is the point).

## Multi-tenancy (prepared layer) — enable in three steps

Isolation is **off by default and inert when off**: `MULTI_TENANT=0` passes recall filters through untouched, and the RLS policies pass through any session that hasn't set `app.tenant_id` (so the daemon, CLI, migrations, backups keep seeing everything). Verified with zero-behavior-change assertions in `tests/test_multi_tenant_rls.py` (app layer) and a live shadow-database run of `scripts/migrations/008_rls.sql` (DB layer: unset session sees all, `SET app.tenant_id='t1'` sees only t1, cross-tenant writes rejected by `WITH CHECK`).

When a real second host connects:

1. **Apply the migration** (idempotent, safe to run before switching anything):
   ```bash
   python3 scripts/migrate.py --apply        # dry-run by default; --apply executes
   ```
2. **Backfill ownership** — rows with `tenant_id IS NULL` become invisible to filtered recall once a tenant session is active (same semantics as the app-side filter since P1b):
   ```sql
   UPDATE memories SET tenant_id='default' WHERE tenant_id IS NULL;   -- entities/edges likewise, as needed
   ```
   From here on, every `POST /v1/retain` must pass `tenant_id` explicitly (the adapter passes it through).
3. **Flip the switches per deployment**: env `MEMORY_ENGINE_MULTI_TENANT=1` + `MEMORY_ENGINE_TENANT_ID=<this host's tenant>` gives forced tenant filtering on every recall (explicit `filters.tenant_id` still wins). For DB-level defense-in-depth, have each app session `SET app.tenant_id='<tenant>'` (pool note: converge `SET` to transaction scope — `SET LOCAL` — when wiring the engine pool; deliberately not done in the prep layer).

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

**Three boundaries, stated where the pitch ends** (added 2026-09-21 after the round-5 external review, so nobody has to infer them):

1. **Graph = light graph-augmented recall, not a temporal KG service.** The recall route is a one-hop pull-back over the entity layer — 2,463 entities / 7,975 typed semantic edges at the 2026-09-18 full-corpus snapshot (3,989 / 13,788 on the live store, measured 2026-09-21) — plus a browsable two-hop neighbors endpoint. This is not Zep/Graphiti territory: multi-hop graph reasoning, entity disambiguation (entities merge lexically today, trigger on ROADMAP §2), and temporal edge invalidation are roadmap work, not this release. The honest tell, counted from the live DB: `edges` carries `valid_at`/`invalid_at` columns and **0 of 13,788 edges has ever had `invalid_at` set** — the schema is bitemporal, the invalidation pipeline isn't wired yet.
2. **Memory typing = present, heuristic, episodic-major.** Every entry is `semantic` / `procedural` / `episodic`; the host may pass `memory_type` explicitly at retain (`api_core` validates it) or let the write-time classifier assign it, and TTL windows scale per type. Live distribution, measured 2026-09-21: episodic 23,549 · procedural 14,987 · semantic 921. The thin parts, said plainly: the classifier has no labeled-precision measurement, and a procedural memory is stored and decay-scheduled, not *executed* — execution semantics stay host-side. Type-depth work (classification quality, procedural execution contracts) is roadmap, not shipped.
3. **Production governance = single-user self-hosted first.** Multi-host fields are pre-staged (`tenant_id`/`agent_id`, RLS migration — all default-off), but letting a second host in has two hard prerequisites that do not exist today: quota isolation and privacy boundaries (the remaining-S2 line in the Scaling path above). Overload behavior is 503+`retryable` only — there is no per-client 429 rate limiting and no circuit breaker. That is a named gap on the multi-tenant path, not a hidden one.

## What's actually different

The table above compares categories; this one names names — and the honest headline first: **no single mechanism here is unprecedented.** Hybrid retrieval exists in Mem0 and Zep, temporal validity exists in Zep, memory SDKs exist in LangMem; none of them ship LLM consolidation as refined a pipeline as Mem0/Zep do (ours deliberately stops at human-reviewed drafts). The defensible claim is the **combination** — lifecycle + freshness cursor + per-write audit + fail-closed deletion as one loop, in one daemon — and the failure behavior engineered as first-class.

| System | Their strength, in their public numbers | Where this diverges |
|---|---|---|
| **Mem0** | LLM-extracted conversational memory, hosted + OSS; reports +26% relative accuracy vs OpenAI memory on LOCOMO, 91% lower p95 latency and >90% token savings vs full-context ([arXiv 2504.19413](https://arxiv.org/abs/2504.19413)) | Their product is the extraction pipeline; lifecycle and auditability are accessories. Inverted here: writes stay verbatim, state machines do the trusting, every mutation lands in the changelog |
| **Zep** | Temporal knowledge graph (Graphiti) as a managed service; reports DMR 94.8% vs MemGPT 93.4%, and +15.2–18.5% LongMemEval accuracy over full-context baselines at ~90% lower latency ([arXiv 2501.13956](https://arxiv.org/abs/2501.13956)) | The closest neighbor on bi-temporal thinking — we agree time-validity is the core. They run a graph service; we keep a light graph inside the same single Postgres and add freshness cursors + fail-closed ops instead of a managed-service surface |
| **LangMem** | In-process LangChain SDK: memory managers and prompt-optimizer loops; its docs publish no benchmark numbers as of 2026-09 | A library has no independent enforcement point — anything that can import can bypass. The daemon-at-127.0.0.1 is a trust boundary choice, not packaging laziness |

Two anti-marketing warnings. First, **those numbers are not comparable to our R@5 0.652**: Mem0/Zep report end-to-end answer accuracy judged by an LLM; ours is retrieval-only scoring with no LLM in the loop — different yardsticks, deliberately not ranked against each other. Second, single-point moats here are thin on purpose: the bet is that for agent memory, the boring properties (what's still true, who changed what, deletion with receipts) compose into something none of the three ships together as its *core* loop — and if a competitor ever does, this comparison table should get uncomfortable to maintain.

## Ecosystem & integration paths

What exists today, what is queued, and one correction on the record (added 2026-09-21 — the round-5 review scored this repo's ecosystem from a stale snapshot):

- **Today:** typed Python SDK — `sdk/python/memoryengine`, MIT, httpx, full `/v1` surface with exception mapping, 25 mock-transport tests (`7694b3f`, 2026-09-17); REST `/v1` (API surface above); CLI — `src/memory_engine/cli.py`; LangChain adapter — `BaseMemory` + `BaseRetriever` over HTTP (`integrations/langchain_memory.py`, `930a226`); zero-build graph UI (`deploy/graph.html`); full-corpus JSONL backup via `GET /v1/export`.
- **Today (landed 2026-09-21):** **MCP server** — `src/memory_engine/mcp_server.py`, stdio transport on the official MCP Python SDK (`pip install mcp`; auto-detects v2 `MCPServer`, falls back to 1.x `FastMCP`). Six tools map 1:1 onto the REST surface — `memory_retain` / `memory_recall` / `memory_feedback` / `memory_get` / `memory_search_list` / `engine_metrics` — over HTTP, never direct to the DB, so the engine's validation/audit/poison-gate chain applies unchanged. Fails open: a tool error returns `ERROR: ...` text to the host instead of crashing the bridge. Env: `MEMORY_ENGINE_BASE` (default `http://127.0.0.1:8766`), `MEMORY_ENGINE_CALLER` (default `main`; any other value makes the engine treat this bridge as a distinct host — visibility narrows to `owner==caller`/`public` per `recall._vis_sql`). Trust boundary: stdio is spawned by the host inside the local trust envelope, all six tools open, no auth; exposing over remote HTTP would require an added auth layer (not in scope). Claude Desktop (`claude_desktop_config.json` `mcpServers` entry; same shape works for Cursor `mcp.json`):

  ```json
  {
    "mcpServers": {
      "memory-engine": {
        "command": "python3",
        "args": ["-m", "memory_engine.mcp_server"],
        "env": {
          "PYTHONPATH": "/absolute/path/to/memory-engine/src",
          "MEMORY_ENGINE_BASE": "http://127.0.0.1:8766"
        }
      }
    }
  }
  ```

  `python3` must be an interpreter with `mcp` installed and the repo checked out; protocol smoke lives in `scripts/mcp_smoke.py` (spawn → initialize → tools/list → real recall).
- **Queued:** **TS SDK** — on demand; no demand signal yet, deliberately unscheduled. **Import API** — planned: `GET /v1/export` exists but has no re-import counterpart endpoint; backup restore today means host-side tooling.
- **Anti-misreading note:** "no Python SDK / no license / no contributing guide" — all three are false of this repo's `main`: `sdk/python/` shipped 2026-09-17, and `LICENSE` (MIT) and `CONTRIBUTING.md` have been in the tree since the first public snapshot (`9daa9c5`, 2026-09-16). A review that predates those commits describes staleness of the review, not a gap in the repo — cite the commit you looked at.

## Maintenance tooling

- [`scripts/purge_archived.py`](scripts/purge_archived.py) — archived-TTL purge with fail-closed triple gates (same-day backup exists, batch exported to external disk, engine health four-true). Dry-run by default; deletion goes through the HTTP API only, never direct SQL.
- Regression evidence files (`tests/last_smoke.json`, `tests/last_stage2.json`) are generated per run and kept out of version control; last full run: 38 checks, 0 failures, at 39k memories.

## Operational evidence

**What "production" means here, precisely.** This daemon is the live memory layer of the author's own Hermes Agent deployment: in continuous service since 2026-09-16 (restarts for upgrades included), 39,221 current memories, ≈160k access events from 9 distinct callers (primary agent `main` at 159.8k, plus CLI, doc-indexer, auto-outcome reporter), and the outcome loop live — 28 entries carry host-feedback polarity as of 2026-09-20, with `auto_outcome` inference switched on host-side. What it does **not** mean: multi-tenant SaaS traffic, external users, or third-party longitudinal validation. All 39,221 rows have `tenant_id` NULL — single-host wiring is the measured fact (enforcement is pre-staged in the RLS layer, waiting for a second host that does not exist yet). The claim buys exactly one thing: the failure modes documented in code comments — degraded boot killed into a systemd restart loop, GPU-lock starvation, silent permanent degradation — came from real traffic, not constructed demos; they were fixed in `a6a5d54`, `00ebcd8`, `21f6ca8` respectively.

Governance claims above are snapshot-checkable against the live daemon — `curl -s localhost:8766/v1/lifecycle`, `/v1/consolidate`, `/v1/hard-queries` (no auth bypass, no curated screenshot). Snapshot taken **2026-09-19** on the ~39.2k-memory production store (daemon v0.5.0-w3); live values drift, the endpoints don't lie.

| Loop | Current numbers (2026-09-19) | What they show |
|---|---|---|
| TTL six-state machine | candidate 8,084 · trial 26,949 · **active 0** · decaying 0 · archived 3,920 · retired 238; scanner thread alive, 600 s interval, last scan: 0 transitions | The promotion gate (≥3 recall hits + ≥60% adopt rate, 6-day window) has passed **zero** entries — trust is earned, not defaulted. Honest caveat: the store is 3 days past its bulk migration, so the 90-day decay paths have not yet had a chance to fire; that is reported, not hidden |
| Audit chain | `changelog`: 74,283 rows over 55,992 subject memories — retain 55,989 · delete 17,096 · lifecycle 402 · update 312 · consolidate 224 · adopt 77 · feedback 75 · supersede 68 · reembed 40; 5 supersede version chains; 156,634 access events; WAL: 172 segments archived, 0 failed | Every delete and transition replays by `op`+`ts`; corrections form traversable chains (`GET /v1/memories/{id}/chain`), not erased edits |
| Self-evolution, first rounds | Consolidation: 22 candidate clusters scanned → dry-run found 2 groups → apply merged 2, archived 3–4 stale members per run (ledger live at `GET /v1/consolidate`). Hard-query pool: 7 zero-hit recalls, each with reason. Parameter autotune: **0 committed mutations** — no mutant has passed the paired two-round ≥5% gate yet | The loops run, and the gates still refuse: proposals flow, parameters do not move without statistical evidence — which is the whole design point |

## License

MIT. See [CONTRIBUTING.md](CONTRIBUTING.md) for the test discipline (all-green pytest + zero-match grep audit are the merge bar). Release history: [CHANGELOG.md](CHANGELOG.md) — version-bump-anchored, commit-cited, maintained per release.
