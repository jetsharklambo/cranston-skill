# Onboarding methodology — Discover, Interview, Audit, Generate

Cranston's claim is not "more automation", it is that an agent can **learn a
home** well enough to stand watch over it safely. This index ties the four
runnable modules together; each is written for an agent to execute, with the
admin in the loop at every decision.

| Step | Module | Produces |
|---|---|---|
| 1. Discover | [discovery.md](discovery.md) — D1–D9 read-only probes + per-category questions | the home inventory draft |
| 2. Interview | [interview.md](interview.md) — U1–U17, the values calls no probe can answer | answers mapped to named config fields |
| 3. Audit | [failure-modes.md](failure-modes.md) — F1–F20 recurring classes, probed proactively | service entries, scheduled audits, hands-on list items |
| 4. Generate | drafts of `services.json` (via the check/remediation templates and their `params`) and the house doctrine from [doctrine.md](doctrine.md) | documents the ADMIN reviews and owns |

Gate selection is its own Decide module — [approval-gates.md](approval-gates.md)
— run between Generate and the first cron line.

Two rules hold throughout: **probes only observe** (discovery changes
nothing), and **the agent drafts, the admin applies** (no generated config or
doctrine takes effect until the human has read it). The flow ends exactly
where the README's install steps begin: a reviewed `services.json`, a chosen
gate, a `--once` dry-run, and two cron lines.

Every module is distilled from eight months of one real deployment's
incident record — the same record behind the aggregate numbers in the
README's notification-budget section. Nothing in them is speculative.
