# Contributing

Public API, storage layout, and the self-evolving loops are all in scope for contributions —
but the project optimizes for *provable behavior* over feature count. This document states the
bar precisely so nobody wastes a round-trip.

## Development environment

1. Python ≥ 3.12. Run tests with a 3.12 interpreter — `tests/smoke_test.py` uses nested
   f-string syntax that breaks collection on 3.11.
2. Database: PostgreSQL 18 + pgvector + PGroonga (+ pg_trgm, pg_prewarm):
   - Quick: `docker compose up -d db` — creates extensions and applies `schema.sql`.
   - Bare metal (Ubuntu noble): `sudo bash deploy/pg/stage1_install.sh` (PGDG + PGroonga repos).
   - Migrations: `python3 scripts/migrate.py --apply` (dry-run is the default; migrations are
     idempotent SQL under `scripts/migrations/`).
3. Embedding model: Qwen/Qwen3-Embedding-0.6B weights are **not** distributed with the repo.
   Download to `MEMORY_ENGINE_MODEL_DIR` (default `<data dir>/models/qwen3-embedding-0.6b`),
   or `python -m memory_engine.download_model`.
4. Configuration is environment-only; copy `.env.example` to `.env` and edit there.
   No config-file patching.

## Running the engine

```bash
deploy/memory-engine.sh serve        # or: PYTHONPATH=src python3 -m memory_engine.cli serve
curl -s http://127.0.0.1:8766/v1/health   # expect status=ok and db+model+warm+ready all true
```

## Tests

```bash
pytest tests/ -v                 # engine suite
pytest sdk/python/tests -v       # SDK suite (mocked transport, fully offline)
flake8 src tests sdk/python --max-line-length=120 --extend-ignore=E203
```

- Unit tests (`tests/test_unit_pure.py`, SDK tests) run offline.
- Integration suites (smoke / stage2) need a live daemon + database; they **auto-skip** when
  the daemon is unreachable rather than faking green. Run directly with
  `python3 tests/smoke_test.py` / `python3 tests/stage2_test.py`.
- Generated evidence files (`tests/last_smoke.json`, `tests/last_stage2.json`) stay out of
  version control — paste the summary into the PR description instead.
- CI (`.github/workflows/ci.yml`) is the merge gate: it boots a real PG18+pgvector+PGroonga
  container, applies schema + migrations, starts the daemon with the CPU embedder, then runs
  flake8 and the full suites. A PR is only reviewable with CI green.

## Pull-request flow

1. Branch from `main`, one topic per branch ("feat/x", "fix/y"). Mixed-purpose PRs get split,
   not reviewed.
2. Commit subjects follow the existing style: `type(scope): imperative summary`
   (types seen in history: `feat`, `fix`, `perf`, `chore`, `docs`, `test`).
3. Open the PR against `main` early (draft is fine) with: what changed, **why**, and the
   evidence line — which tests ran, skip counts (integration skips must be the expected
   number, not "don't care"), and for retrieval-path changes, the `eval/lme/` result delta
   (the project's dual-gate policy: parameter changes must clear the public 500-question set
   *and* a private production baseline; the private leg is run by the maintainer — flag any
   change that could move it).
4. Behavior changes require the docs updated in the same PR: README (API surface / capability
   table), this file, and `.env.example` if a new variable appears. Code without its doc line
   is treated as unfinished, not "documented later".
5. Data files (`*.jsonl`, `*.dump`, model weights, backups) are never committed — `.gitignore`
   enforces this; don't weaken it.
6. Review: one maintainer approval, CI green, then squash-merge. Security problems go through
   the channel in [SECURITY.md](SECURITY.md), never a public PR.

## Ground rules that come from production incidents

These are not style preferences; each one maps to a failure this system actually had:

- **All consumers go through the HTTP API.** No import-based bypass of retain/recall, in
  product code or tests (one read-only reconciliation test is exempt). The write protections —
  dedup, injection gate, source tiering — only mean something at the single door.
- **Degraded must be explicit.** A route failure returns `200 + degraded + failed_routes`;
  `503` is reserved for total failure. Silent permanent degradation is the failure mode this
  project treats as an incident ("who pulls it back, who knows" must have an answer).
- **Deletion is fail-closed.** Anything touching the purge path must keep the triple gate
  (same-day backup + off-device batch export + four-true health) and HTTP-only deletes.
- **Fix the pattern, not the instance.** Before patching, grep for sibling call sites; keep
  the diff surgical. "First make it work, optimize later" is not how this codebase grew.

See [ROADMAP.md](ROADMAP.md) for what is deliberately *not* planned — sending a PR for a
non-goal is likely to be declined, so check there first.
