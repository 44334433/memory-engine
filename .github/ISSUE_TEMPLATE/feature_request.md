---
name: Feature request
description: Propose a capability — name the signal that makes it necessary
labels: [enhancement]
---

### The blocked workflow

What are you actually trying to do, and what stops today's API from doing it? "Nice to have"
is a valid answer — it just won't rank above items with a blocked workflow behind them.

### Which axis does this move?

- retrieval quality (would it move R@5 on `eval/lme/`? by how much, plausibly?)
- write safety (does it close a poisoning/PII/audit hole?)
- operational surface (backups, degradation, upgrade ergonomics?)
- host integration (SDK / framework / multi-agent wiring?)

### Trigger vs. timing

The project runs on trigger conditions, not dates ([ROADMAP.md](../ROADMAP.md)). Does this
request fire an existing trigger (second host connects, PII audit finding, framework
adapter demand…), or is it a new premise? If it looks like a non-goal
(distributed vector DB, microservices, hosted SaaS), reference the ROADMAP section arguing
why it stays out before building.

### Proposed shape

Sketch the API surface / schema change / config line — prose is fine, a `curl` contract is
better. Note whether it changes existing endpoint behavior (breaking vs. additive decides
the review bar).

### Alternatives you considered

Including "run without it": knowing what you'd do instead tells us the real gap size.

### Evidence

Can it be measured with a public set (`eval/lme/`, the decay-curve harness)? If only a
private corpus can show it, say so — we will ask for methodology, not data.
