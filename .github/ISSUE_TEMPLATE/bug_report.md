---
name: Bug report
description: Report behavior that differs from what the docs promise — with a reproducible path
labels: [bug]
---

> **Privacy first:** reproduce with synthetic content. Do not paste real memory bodies,
> export files, or anything from a production store. This repo is public.

### What happened

One paragraph. What you did, what came back, and which doc/promise it contradicts.

### Health snapshot

Paste `curl -s localhost:8766/v1/health | jq` — the version, `status`
(ok vs degraded), and the four booleans tell us half the story before you type a word.

### Reproduction

Minimal path from a fresh stack (`docker compose up -d db` → migrate → serve), as `curl`
calls or ~10 lines of the SDK:

```bash
# 1. ...
# 2. ...
```

If it only happens on existing data: what does `GET /v1/lifecycle` report (states, not
content), and does the behavior reproduce on the `eval/lme/` corpus?

### Response shape (if retrieval-related)

Paste the JSON with `body` fields redacted — we need `score_parts`, `degraded`,
`failed_routes`, tier weights, and freshness buckets, not your content.

### Environment

- OS / GPU or `MEMORY_ENGINE_EMBED_DEVICE=cpu` / PG version
- Commit or tag (`git rev-parse HEAD`), and anything from `.env` that is not a secret

### What you expected vs. what the docs say

If expected == documented but actual != expected, say so — that split decides whether this is
a code bug or a doc bug, and both are bugs.
