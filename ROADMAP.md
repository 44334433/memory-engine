# Roadmap

**Discipline: triggers, not dates.** Every item below has an entry condition that is an
observable signal — "the second host actually connects", "measured FP > 30%" — and none is
scheduled on a calendar. Items were decided (拍板) as event-driven backlog on 2026-09-16/17;
an item whose trigger never fires is correctly never built. Anything here can be dropped
without ceremony if its premise stops paying.

## 1. Scaling ladder (S0 → S4)

The README "Scaling path" table is the summary; this is the canonical version with the
decided work estimates (from the evolution-path design doc, 2026-09-16).

| Stage | Trigger signal | Work | Est. |
|---|---|---|---|
| **S0** (today) | — | single host, full features; P95 recall 21–31 ms at 6k entries | — |
| **S1** replica-ready | need ≥2 daemon copies (HA / read scale-out), or GPU contention makes warmup fail repeatedly | embedder served separately via the existing pluggable provider (`openai_compat`) — config migration, not code; decision-state (param snapshots, word lists) converges into PG with an advisory-lock single writer | 1.5–2.5 days |
| **S2** multi-host | a second host genuinely connects (decided: the owner plans possible additional agents; `tenant_id`/`agent_id` columns already exist and are query-visible) | tenant routing + quota isolation + privacy boundary + recall-side filtering | 3–5 days |
| **S3** asset-grade durability | the store becomes someone's production asset / an SLA is promised | PG streaming replication + remote standby (logical JSONL export already gives cross-version backup) | 2–3 days, engine code unchanged |
| **S4** hosted service | explicit product decision (requires owner sign-off — it is an external commitment) | billing, tenant console, observability, multi-region | different product; not in this repo |

Explicit non-goals (recorded so they stay cheap to refuse): migrating to a distributed
vector DB (pgvector + HNSW holds 10 ms-class into millions of rows — a cluster buys nothing
here), splitting the daemon into microservices (the single binary *is* the design), and any
S4 work done speculatively.

## 2. Retrieval quality — code shipped, evidence pending

- **Reranker enablement.** The Qwen3 cross-encoder second stage shipped 2026-09-19
  (`src/memory_engine/reranker.py`) behind `RERANK_ENABLED=false`. Trigger to turn on: A/B on
  the heterogeneous full-corpus config (`scripts/w3_ab_compare.py`) shows the top-k problem
  moves (the 0.122 no-filter case in the README). Retirement condition: rerank on, no
  measurable delta → stage removed, not kept as decoration.
- **Entity disambiguation.** Graph recall merges entities lexically today. Trigger: measured
  route-precision loss attributable to alias/collision cases, or first external graph user
  reporting wrong pull-backs.
- **Pluggable embedder upgrades.** A larger quantized embedder is a registered upgrade path
  (decision on record: stay fp16 until evidence says otherwise). Trigger: retrieval quality
  bottleneck attributable to embedding, established by the decay-curve harness — not by vibe.

## 3. Lifecycle & governance backlog (decided P2, event-driven)

Each line names its trigger — items stay parked, not deleted, until one fires:

| Item | Trigger | Notes |
|---|---|---|
| PII classification gate | first real PII audit finding, or owner product decision | `contains_pii` column reserved since the P0 batch; `pii-filter` linkage designed |
| Right-to-be-forgotten API | SaaS/external-user scenario, or compliance requirement | semantic delete: subject-keyed, follows edges and supersede chains |
| Capacity planning at ~100k memories | store crosses 100k live rows | today: full dump + WAL archiving (172 segments, 0 failed, 2026-09-19); increment at that scale |
| External blind review of the eval set | before the numbers are cited by anyone outside this repo | question-set author = developer is a known bias; adversarial review is the counter |
| Memory browse/fix CLI | data-sovereignty demand (host wants offline inspection without SQL) | read path largely exists via `/v1/memories`; the gap is ergonomics |
| uuid7 RFC 9562 variant bits | tracked issue gets a PR | time-prefix semantics verified; conformance open |

## 4. Automation widening (fail-closed until evidence)

- **Host-side outcome auto-reporting → default on.** Inference of adopted/corrected ships
  default-off (2026-09-19). Trigger: a production window with validated inference quality —
  specifically, proof the false-negative path ("useless is never inferred") cannot starve the
  EMA. Retirement: evidence of polarity contamination → stays off.
- **Consolidation beyond drafts.** Today clusters synthesize human-reviewed drafts only.
  Trigger for any auto-apply: a counted history of drafts approved unchanged at a high rate
  (threshold set when the first N are reviewed). Silent rewrite stays out of scope regardless.
- **Contradiction enforcement (G15 → enforce).** Pre-registered hard precondition: golden-edge
  precision ≥ 0.7 **and** 30 days of passing spot checks. No early escalation on volume alone.

## 5. Ecosystem adapters

- **LangChain / framework adapters: built when asked, not before.** Decided posture: the
  httpx SDK (`sdk/python/`) + plain REST is the adapter surface; a thin LangChain-style memory
  wrapper is small (order: 1 day) once a real request exists, and is speculative packaging
  before that — the project's history says "don't build adapters for zero users" (this rule
  killed exactly such placeholders before). Trigger: first framework-integration request from
  a running host, evidenced by a real issue with reproduction, not upvotes. Retirement: no
  request across two quarters → this section gets deleted.
- **Hermes provider plugin** is the reference integration and stays maintained in-repo
  (`deploy/provider-plugin/` snapshot ships with releases).

## Changing this file

Adding an item requires stating its trigger signal in the PR; an item without a falsifiable
trigger is a wish, not a roadmap entry. Removing an item requires its premise to be dead —
say which, in the commit message.
