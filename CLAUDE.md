# Project purpose

This repository authors ONE Agent Skill (Cranston, a self-healing home
monitor) and generates two installation views from a single canonical source:
the OpenClaw root artifact (`SKILL.md` + `scripts/` + `references/` +
`assets/`) and the Hermes tap artifact (`skills/cranston/`). Design rationale
and platform facts: `docs/dual-skill-spec.md`.

# Sources of truth

- Edit `authoring/manifest.yaml`, `authoring/SKILL.body.md`, and files under
  `authoring/` (scripts, references, assets, adapter preambles).
- Do NOT hand-edit root `SKILL.md`, root `scripts/`/`references/`/`assets/`,
  or anything under `skills/cranston/` — all generated, wholesale-replaced by
  the build.
- Do not duplicate platform-neutral instructions in the adapter preambles.
- Author package paths with the neutral token `{{SKILL_DIR}}`; the build
  renders `${HERMES_SKILL_DIR}` / `{baseDir}` per target.

# Required workflow

1. Inspect `authoring/manifest.yaml` and `authoring/SKILL.body.md` before
   changing behavior.
2. Change the shared source first; touch an adapter preamble only when a
   documented runtime difference requires it.
3. `python3 tools/build.py`
4. `python3 tools/check_drift.py && python3 tools/validate_links.py`
5. `python3 tests/test_engine.py && bash tests/test_templates.sh && python3 tests/test_build.py && python3 tests/test_interview.py`
6. Report changed behavior, generated artifacts, test results, and any
   unresolved platform differences.

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
