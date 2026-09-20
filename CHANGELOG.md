# Changelog

All notable changes, newest first. Three rules keep this honest:

- Version numbers below are the `VERSION` bumps in `src/memory_engine/config.py` plus the release tag — not marketing tiers. v0.1 and the split lines before v0.5.0 are retroactive labels for internal milestones that arrived pre-packaged in the 2026-09-16 open-source snapshot; they never had standalone releases.
- Every functional line cites a commit or a path that reproduces it. Claims that cannot be checked do not get an entry.
- The README states what is true now; this file states what changed when. If a release only re-arranges the README, it still gets a line.

## [Unreleased] — since tag v0.5.0 (2026-09-19), 15 commits

- perf: hash-dedup before embedding (exact repeats skip GPU work), chunked GPU lock with `sleep(0)` hand-off (recall no longer queues behind a whole embedding batch), entity-extract starvation fix via empty-result attempt ledger (`00ebcd8`)
- docs: SECURITY.md — poisoning threat model, fail-closed deletion, private disclosure (`3b957d0`); ROADMAP.md — trigger-gated S0–S4 ladder (`69989c5`); CONTRIBUTING expansion + evidence-first issue templates (`3da5eed`, `2c0f8b5`)
- docs(readme): Architecture decisions, 5 ADRs each with cost + flip condition (`13501a0`); named comparison vs Mem0/Zep/LangMem with explicit non-comparability warning (`45ffff9`); Operational evidence live governance snapshot (`7c96e76`)
- fix(config): W3 rerank block synced into the public mirror — the missing `RERANK_*` constants crashed every public clone's daemon at startup with AttributeError; the regression lesson (mirror-sync must include a clone-and-boot smoke) is recorded in `73b4844`, which also stamps `VERSION = 0.5.0-w3`
- feat(multitenant): RLS prep layer — `008_rls.sql` policies on memories/entities/edges, `MULTI_TENANT` default-off = passthrough (zero behavior), proven against a shadow database (`4468949`)
- feat(integrations): LangChain adapter — `BaseMemory` + `BaseRetriever` over HTTP to the daemon, 11 hermetic mock-HTTP tests (`930a226`)
- feat(eval): `run_eval.sh` — one command from compose to 500-question R@5, `--dry-run` prints the plan (`f2f06df`)
- feat(recall): `score_parts` carries `schema_version: 1` — the auditable-scoring field set becomes structurally versioned, consumers parse defensively (public `d251a1d`, prod `34932d9`)
- docs: this CHANGELOG introduced; unrendered `<you>` clone-URL placeholders replaced with the real repo URL (`a4d5357`); README gains Evaluation methodology, a five-minute demo, and four honest answers (what "production-proven" means / where the 6k bottleneck actually is / deletion-gate behavior / CJK-vs-English impact)

## v0.5.0 — 2026-09-19 (tag; covers the 09-17→09-19 batch run: W1–W4 + outcome feedback + graph depth + as-of + RRF tie-break)

The week the store grew categories and closed the outcome loop:

- W1 core-block: `GET /v1/core-block` — pinned + auto dual-track selection under a hard 1500-char budget, zero read side effects (16 unit tests; live P95 < 50 ms) + `pinned` column (migration 007)
- W2 memory-type layering: semantic / procedural / episodic with per-type decay scaling (migration 005, backfilled)
- Outcome feedback: `POST /v1/feedback` — adopted / corrected / useless → per-item EMA polarity feeding lifecycle and the hard-query pool (`7890540`)
- Bi-temporal `as_of` snapshots (`3427b64`), supersede-chain traversal (`GET /v1/memories/{id}/chain`, recursive CTE with cycle guards), two-hop graph neighbors with as-of edge filtering
- W3 reranker stage: Qwen3 cross-encoder, shipped behind `RERANK_ENABLED=false` until A/B evidence says on
- Attachments + graph batch: read-only graph API, image attachments (content-addressed, optional VLM caption, purge reclaim), migration 003 (`70852d6`)
- Typed Python SDK `memory-engine-client` (httpx): full `/v1` surface, exception mapping, 25 mock-transport tests (`7694b3f`)
- CI: PG18 + pgvector + PGroonga combined image, full pytest against a daemon on CPU embedder (`ab9f168`)
- Eval infra: LME scale-decay curve harness, 500→6k synthetic-haystack buckets (`e30f3f6`, `0758ff9`); D5 extract timer + `EXTRA_BANKS` (migration 004, `639f304`)
- Host-side plugin v1.1: auto-outcome inference (adopted/corrected), default-off `MEMORY_ENGINE_AUTO_OUTCOME` (`93c7b0b`, the tagged commit)

## v0.4.0-p1c — 2026-09-16 (VERSION bump at `21f6ca8`)

- Keep-alive batch: embedder self-heal thread — a degraded boot (model load failed) retries `load()+warmup()` every 60 s and pulls itself back to the vector route; "degraded" answers *who pulls it back, who knows* (`21f6ca8`)
- P1c self-evolution loops: `param_autotune.py` (weekly timer; mutates retrieval weights, replays the eval set per-question, commits only after two consecutive rounds beat the frozen baseline by ≥5%) and consolidation-to-draft (`scripts/consolidate_synthesize.py`, human review before anything enters the store) (`a2655e2`)
- Documentation of what was already running: LongMemEval benchmark section (`ffb617b`), self-evolution + knowledge-graph sections (`268571c`), capability matrix with hard numbers (`d479178`), per-question eval data committed under `eval/lme/` — synthetic corpus, zero production memories (`f1d5de9`)

## v0.3.0-p1b — 2026-09-16 (VERSION bump at `e5a0fb9`)

- Bi-temporal supersede: a revision truncates the old row's `invalid_at` and re-inserts a new version — history is kept, not overwritten; in-place edits of historical rows return 409 (`e5a0fb9`)
- Knowledge-graph foundation: `entities`/`edges` tables, co-occurrence + weak-graph edges, LLM typed-relation extraction (related / parent_child / causal / contradicts — contradicts is observe-only, promotion gated on labeled precision ≥0.7 + 30-day sampling), fourth recall route (graph, observe weight 0.5, `score_parts.graph` exposed)
- Multi-host columns (`tenant_id`, `agent_id`) carried from here on — enforcement did not arrive until the RLS prep layer (Unreleased)
- Migration 002, idempotent

## v0.2.0-phase2 — 2026-09-16 (open-source snapshot `9daa9c5`)

- The snapshot itself: three-way hybrid recall (pgvector HNSW dense + PGroonga full-text + structured/temporal) fused with weighted RRF, TTL six-state lifecycle, changelog audit trail, HTTP-only `/v1` surface on FastAPI
- P0 (`691fc2c`): unified error semantics (200+`degraded` for partial-route failure, 503+`retryable` only for all routes down, 400 for bad params) and the write-time poisoning gate (four-level `source_tier`, injection-pattern scan, external sources default to `trial` entry)
- P1 (`5b2c03f`, `a6a5d54`): fts-only degradation path, atomic PATCH/adopt, UNIQUE dedup fallback, pluggable embedder provider (`qwen3` | `openai_compat`), tier down-weighting (web 0.85 / cron 0.9); wait-ready relaxed to db+ready so a degraded boot stops getting killed into a restart loop by systemd start-post
- Tooling: archived-TTL purge with fail-closed triple gates (`c160dad`); bilingual README (`eff0d6e`)

## v0.1 — internal, pre-2026-09-16 (retroactive label, never published)

- The phase-1 core the snapshot was cut from: retain/recall loop, RRF fusion, TTL state machine, changelog ledger. No v0.1 artifact exists — the version number is inferred from the snapshot stamping itself `0.2.0-phase2`; public history starts at v0.2.0.

## Maintenance — how this file stays true

1. Cutting a release: bump `VERSION` in `src/memory_engine/config.py` → `git tag vX.Y.Z` → add the dated section here citing the commits → link it in the release notes.
2. Functional lines need a commit sha, a test name, or a command that reproduces them. Docs-only releases get one line, not three paragraphs.
3. Breaking the shape of `score_parts` or any response body means bumping its `schema_version` **and** saying so in this file — structural changes never ride along silently.
