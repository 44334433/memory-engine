# Security Policy

## Trust model — read this first

The daemon binds `127.0.0.1:8766` and ships **no authentication or TLS**. That is a
deliberate boundary, not an oversight: this is a single-host memory layer, and the security
assumption is *the machine is the perimeter* — anything with local access to the port already
has full store access, so an API key at that layer would be theater. If your threat model
includes untrusted local processes or a second remote host, the engine as it stands today is
the wrong deployment; multi-tenant isolation is trigger-gated in [ROADMAP.md](ROADMAP.md)
(S2), not enabled now.

Data-side posture: no telemetry, no phone-home, no cloud dependency. Outbound calls exist
only where the operator configures them (optional LLM entity extraction, optional OpenAI-style
embed/rerank providers). `GET /v1/export` dumps the *entire* store — treat export files and
backups under `<data dir>/backups` as sensitive artifacts.

## The threat this engine was built against: memory poisoning

An agent ingests content it did not author — scraped pages, tool output, cron reports — and
that content carries imperative text ("ignore previous instructions…"). If it enters the
store, every future recall injects it into new sessions: **persistent cross-session prompt
injection**, which is worse than a one-time injection because it survives restarts.

Controls, in the order a write meets them:

1. **Source tiering is mandatory and distrust-by-default.** Every retain carries
   `source_tier ∈ {user, agent, web, cron}`; missing metadata defaults to `agent`, never
   `user` (`src/memory_engine/poison_gate.py`).
2. **Injection scan before anything is stored.** A bilingual (zh/en) pattern library —
   instruction override, role hijack, prompt exfiltration, forced execution, jailbreak
   markers — runs before embedding; a hit rejects the whole batch with `422` listing the
   matched pattern labels. No silent drops: the writer learns it was gated.
   Scope is env-controlled (`MEMORY_ENGINE_INJECTION_SCAN`, default `external_only`; `all`
   was the test mode used below).
3. **Measured false-positive behavior, reproducible.** Probed against 1,200 real LongMemEval
   haystack documents: 1 flagged, FP rate **0.0008**, 17.6 s (`eval/lme/poison_gate_probe.json`,
   regenerate with `eval/lme/probe_poison_gate.py`). Pre-registered tightening condition:
   if measured FP > 30%, scan narrows to external sources. False *negatives* are the accepted
   cost — pattern scanning is one layer, not the boundary; the next layer is what limits blast
   radius:
4. **Poisoned entries cannot silently become trusted.** External-origin content enters at
   TTL state `trial`, gets downweighted at recall (tier weight: web 0.85, cron 0.9), and the
   promotion gate to `active` requires ≥3 recall hits *and* ≥60% host-adoption rate over a
   6-day candidate window (`GET /v1/lifecycle` exposes the live rules). As of the
   2026-09-19 snapshot **zero entries have been promoted** — the gate is not a rubber stamp.
5. **Contradiction cannot self-enforce.** The graph's `contradicts` edges are observe-only
   (constraint G15, hardcoded in three write paths): a malicious or wrong "this fact is
   outdated" edge can never flip `invalid_at` on another memory. Escalation to enforce
   requires a golden-edge precision ≥ 0.7 plus 30 days of spot checks.

## Integrity of destructive paths

- **Purge is fail-closed with three gates** (`scripts/purge_archived.py`): same-day verified
  backup exists + batch exported to a second device + `/v1/health` four-true. Any gate false →
  nothing is deleted. Dry-run is the default; deletion goes through the HTTP API only (so it
  lands in the audit ledger), never direct SQL.
- **Every mutation is on the record.** The `changelog` table logs retain/update/lifecycle/
  delete/consolidate/adopt/feedback/supersede/reembed — 74,283 rows across 55,992 subject
  memories in the 2026-09-19 production snapshot. Corrections *supersede* (bi-temporal);
  in-place rewrites of live content are 409-rejected.
- **Degradation is visible by design.** Embedder failure → `200 + degraded + failed_routes`
  with a 60 s self-heal retry loop, never a silent downgrade pretending to be full recall;
  `503` only when all routes fail. A degraded state older than 30 minutes is defined as an
  operational incident, not a steady state.

## PII

Honest status, labeled: **[planned]**. The `contains_pii` column exists in the schema, but
classification is **not implemented** — the PII judgment gate is an open decision (ROADMAP,
governance backlog). What is true today:

- PII only enters the store if the host writes it; the engine adds no capture path of its own.
- Visibility scoping (caller-scoped banks, `private` entries invisible to non-owners) limits
  cross-agent reads on a single host.
- A subject-level "right to be forgotten" delete API (semantic delete along edges) is a
  trigger-gated roadmap item, not current capability. If you need erasure guarantees today,
  this engine does not yet ship them — say so in your deployment docs.

## Reporting a vulnerability

Use **private disclosure**: the "Report a vulnerability" form on the repository's Security
tab (GitHub private vulnerability reporting). Do not open a public issue for an unfixed
vulnerability.

This is a solo-maintained project: expect a first response within about a week, with no
contractual SLA. Scope: issues in this codebase's behavior (gate bypass, purge/audit
integrity, injection escalation, data leakage between visibility scopes). Out of scope:
attacks requiring already having local access, LLM-provider compromise, prompt injection in
models themselves (this engine hardens the persistence path; it does not make the host agent
injection-proof), and DoS on a single-host deployment.
