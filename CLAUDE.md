# Project purpose

This repository authors ONE Agent Skill (Cranston, a self-healing home
monitor) and generates two installation views from a single canonical source:
the OpenClaw root artifact (`SKILL.md` + `scripts/` + `references/` +
`assets/`) and the Hermes tap artifact (`skills/cranston/`). Design rationale
and platform facts: `docs/dual-skill-spec.md`; the system blueprint:
`docs/design.md`.

# Sources of truth

- Edit `authoring/manifest.yaml`, `authoring/SKILL.body.md`, and files under
  `authoring/` (scripts, references, assets, adapter preambles).
- Do NOT hand-edit root `SKILL.md`, root `scripts/`/`references/`/`assets/`,
  or anything under `skills/cranston/` — all generated, wholesale-replaced by
  the build.
- Do not duplicate platform-neutral instructions in the adapter preambles.
- Author package paths with the neutral token `{{SKILL_DIR}}`; the build
  renders `${HERMES_SKILL_DIR}` / `{baseDir}` per target.

# Architecture map

| Area | What lives there |
|---|---|
| `authoring/scripts/engine/` | `selfheal.py` — one monitoring cycle per cron run: network gate, checks, per-key state machine (ok → failing → healing/escalated), anti-flap, ask-demotion, consent pre-announcements, digest routing, per-service error boundary, atomic state under `state/.lock`. `approve-heal.py` — runs a human-approved fix through the gate (standalone; shared constants are duplicated with "keep identical" comments, never imported). `lib/check.sh` + `lib/remediation.sh` — the contracts every template sources. |
| `authoring/scripts/checks/templates/` | 9 read-only check archetypes (http, tcp, dns, systemd, disk, cert, ha-entity, host-power, lan-inventory). Contract: exit 0 healthy, else one JSON finding per line. |
| `authoring/scripts/remediations/templates/` | Fixed-content fix scripts: audit → cap → act → **verify** → result (exit 0 only when verified; 1 unverified; 75 rate-capped). `ha-outlet-cycle.sh` is ask-first only. |
| `authoring/scripts/gates/` | Approval-gate argv prefixes (exit 65 = refusal): `gate-telegram-confirm.sh`, `gate-totp-remote.sh` + `gate-totp-server.py` (6-digit codes), `secure-bash-argv.sh` (OpenClaw one-shell-string wrapper adapter), `TEMPLATE.sh`. |
| `authoring/scripts/bin/` | Delivery chain: `send-alert.sh` (queue; announce lines are tagged objects) → `notify-alerts.sh` (every-minute drainer; worst-line header; routes announce lines to `TG_ANNOUNCE_CHAT_ID`, folds them into the admin page when unset) → `send-telegram.sh` (chunking, DNS-fallback). `flush-digest.sh` (daily digest fallback), `tailscale-running.sh` (ALT_GUARD_CMD). |
| `authoring/scripts/install.sh` | Scripted install: deploy copy, env skeleton, dry run, prints the 3 cron lines; `--apply-cron` opt-in with backup. Never overwrites `services.json` or the env file. |
| `authoring/scripts/onboard/` | Per-device interview (`interview.py` + `catalog.json`): fills gaps in a hand-written `services.json`, provenance-tracked, manual config always wins. |
| `authoring/references/` | The onboarding methodology (discovery/interview/failure-modes/doctrine), schema contract, approval-gates guide, worked example, hardware guide. |
| `sandbox/` | Local fake-LAN drills (`up.sh`/`drill.sh`/`interview-drill.sh`); excluded from the package. |
| `sim/` | Home-simulation suite (excluded from the package): `casa/` fake family home (pihole/truenas/plex, dig+df PATH stubs, two-chat stub Bot API in `phone/`), `scenarios/` (authored 90-day timeline + seeded generator), `soak.py` (compressed-clock driver: patched engine clock + real delivery chain under `SELFHEAL_NOW`), `report.py` (hard gates G1–G10 on frequency/routing/dedupe), `judge/` (advisory Claude tone rubric), `cloud/` + `CLOUD-TEST.md` (real-time drill runbook for a Claude cloud session: install, interview-as-persona, 75-min traffic). |
| `tools/` | `build.py` (deterministic dual-view build), `check_drift.py`, `validate_links.py`. |
| `tests/` | See workflow below. Harness patterns: `test_engine.py` imports the engine with a fake clock (`sh.now`) and `verify_delay_seconds: 0`, runs approve-heal as a subprocess; `test_delivery.sh`/`test_install.sh` use a recording `$STUB` sender / PATH-stubbed `crontab` and an `assert <name> <rc> <substr> -- env... cmd` helper; `test_templates.sh` stubs binaries on PATH. All suites run against `authoring/` directly — no build needed to test. |

# Required workflow

1. Inspect `authoring/manifest.yaml` and `authoring/SKILL.body.md` before
   changing behavior.
2. Change the shared source first; touch an adapter preamble only when a
   documented runtime difference requires it.
3. `python3 tools/check_drift.py` — run BEFORE building: it should fail
   listing exactly your authoring changes and nothing else.
4. `python3 tools/build.py`, then `python3 tools/check_drift.py` (now clean)
   and `python3 tools/validate_links.py`.
5. `git status --porcelain` — only intended files (CI fails on stray
   generated files).
6. Full test run:
   `python3 tests/test_engine.py && python3 tests/test_build.py &&
   python3 tests/test_methodology.py && python3 tests/test_interview.py &&
   bash tests/test_templates.sh && bash tests/test_delivery.sh &&
   bash tests/test_install.sh`
   For engine/delivery/message changes also run the simulation soak:
   `python3 sim/soak.py --scenario sim/scenarios/family-home-90d.jsonl --out sim/.run/out
    && python3 sim/report.py --run sim/.run/out --scenario sim/scenarios/family-home-90d.jsonl`
7. CI additionally greps the generated views for secrets, absolute home
   paths, and real tailnet IPs — don't introduce any.
8. Report changed behavior, generated artifacts, test results, and any
   unresolved platform differences.

# Status (see docs/design.md §6 for the authoritative table)

- Phases 1–2 (blueprint, engine v2 + templates + packaging): shipped.
- Phase 3 (OpenClaw adapter): done — runtime shims, delivery pair,
  `install.sh`, and consent routing (the announce channel) all ship.
- Phase 4 (publication & methodology): methodology shipped as runnable
  modules; **registry publication still open** (no git tags yet).
- The Hermes view is **experimental** — generated and drift-gated, never
  installed on a real Hermes deployment; no Hermes-side runtime test exists.
- The live reference deployment ("cranston" the box) is frozen v1: this
  package is written FROM it, never deployed TO it.

# Known gaps / worklist

- Registry publication + a first git tag ("pin a tag" in the README can't be
  followed until one exists).
- Dogfood deployment on a second, non-reference machine (design.md calls it
  pending); a live OpenClaw agent conversation is untested everywhere.
- Sandbox drills and the `--openclaw` install path are not in CI; no macOS
  CI job despite `platforms: [linux, macos]`.
- No behavioral tests for `check-dns.sh` beyond UPSTREAM_DOWN, or for
  `restart-docker-container.sh`; `interview.py status` is never exercised.
- design.md §7 open questions: dependency graph vs convention, non-HA
  platforms, multi-admin approvals, interview UX, paid reliability floor.
- Documented-as-designed quirks: cycles stretch by `verify_delay_seconds`
  per attempted remediation (bound via `max_attempts`/`cooldown_minutes`);
  `flush-digest.sh` clears identical message texts together.
- Doc drift still to sweep: README blueprint-publication wording,
  `methodology.md` "two cron lines" (it's three), the Android recipe in
  `hardware.md` (wrong engine path, no drainer line), stale assertion counts
  anywhere they appear.

# Hard rules

- Never commit secrets, credentials, local absolute paths, real LAN/tailnet
  identifiers, or generated runtime state (`state/`).
- Keep the skill name (`cranston`) identical across the manifest, the
  `skills/cranston/` directory, and both generated frontmatters.
- Every bundled support file must be referenced from `SKILL.md`
  (`tools/validate_links.py` enforces this).
- Every procedure in the skill body must end in verification or an explicit
  unverifiable-result report.
- Do not invent platform fields, tools, commands, or compatibility claims —
  platform facts come from `docs/dual-skill-spec.md` and its sources.
- The live reference deployment ("cranston" the box) is frozen v1: this
  package is written FROM it, never deployed TO it.
- Preserve backward compatibility unless a release note declares a break.
