# Dual-Compatible Hermes and OpenClaw Skill Repository Specification

## Executive decision

Build one authoring repository with a **single canonical skill body and resource set**, then generate two thin distribution views:

1. An **OpenClaw-compatible skill at the repository root** for direct Git installation.
2. A **Hermes tap-compatible copy under `skills/<skill-slug>/`**.

Do not independently hand-edit two `SKILL.md` files. Hermes and OpenClaw both implement the Agent Skills format—one directory, a required `SKILL.md`, YAML frontmatter, Markdown instructions, and optional scripts/references/assets—so most content can remain shared. Their runtime extensions differ enough, however, that a build step is safer than pretending every field and behavior is identical.[^1][^2][^3]

This repository is an authoring bridge, not a permanent requirement. When separate repositories are created later, each generated package can be promoted without redesigning the skill.

## Compatibility baseline

The shared layer must conform to the Agent Skills specification:

- `SKILL.md` begins with valid YAML frontmatter.
- `name` and `description` are present.
- `name` is 1–64 characters, uses lowercase letters, digits, and hyphens, has no leading, trailing, or consecutive hyphens, and matches its parent skill directory.
- `description` explains both the capability and when it should activate.
- Optional portable fields are limited to `license`, `compatibility`, `metadata`, and `allowed-tools`.
- The body contains the operational instructions; supporting code and content belong in `scripts/`, `references/`, and `assets/`.[^3]

Use progressive disclosure deliberately. Keep the activation description short, keep the principal workflow in `SKILL.md`, and move detailed material into directly referenced supporting files. The open specification recommends keeping the body below roughly 5,000 tokens and 500 lines.[^3]

For maximum shared discovery quality, use one common description of no more than 60 characters, written as a capability statement that also includes a practical trigger when possible. This is stricter than OpenClaw’s under-160-character guidance and aligns with Hermes’ own authoring convention.[^2][^4]

## Platform differences

| Concern | Shared rule | Hermes adapter | OpenClaw adapter |
|---|---|---|---|
| Skill location | A skill is a directory containing `SKILL.md` | Generate `skills/<skill-slug>/`; Hermes taps conventionally expose skills beneath `skills/`.[^1] | Generate root `SKILL.md`; OpenClaw Git and local installs expect `SKILL.md` at the source root.[^5] |
| Runtime metadata | Keep common identity and documentation in standard fields | Use `metadata.hermes` for tags, tool/toolset conditions, config, and blueprint data.[^1] | Use `metadata.openclaw` for binary, environment, config, OS, and installer gating.[^6] |
| OS values | Store neutral OS names in the authoring manifest | Render `macos`, `linux`, `windows` in top-level `platforms` when gating is required.[^1] | Render `darwin`, `linux`, `win32` in `metadata.openclaw.os`.[^2] |
| Skill-directory token | Author with a neutral token such as `{{SKILL_DIR}}` | Render `${HERMES_SKILL_DIR}`.[^1] | Render `{baseDir}`.[^2] |
| Required secrets | Declare names and user-facing setup text in the canonical manifest; never commit values | Render top-level `required_environment_variables`; Hermes can prompt securely when the skill loads and pass declared values into supported sandboxes.[^1] | Render `metadata.openclaw.requires.env` only for hard requirements; use `envVars` with `required: false` for optional values.[^6] |
| Secret behavior | Document behavioral differences | Missing values do not hide the skill; setup can be deferred.[^1] | `requires.env` affects readiness and can make a skill ineligible; host injection does not automatically place secrets in a sandbox.[^5] |
| Binary dependencies | Document the minimum version and a manual setup path | Prefer existing tools or standard-library helpers; document installation when an external dependency is unavoidable.[^1] | Render `requires.bins`, `requires.anyBins`, and optional `install` specifications.[^6] |
| Invocation | Natural-language discovery should work everywhere | Installed skills are automatically exposed as slash commands.[^7] | Optional `user-invocable`, `disable-model-invocation`, and direct-tool dispatch fields control invocation.[^2] |
| Platform-only automation | Keep the core workflow portable | `metadata.hermes.blueprint` can describe opt-in scheduled automation.[^1] | No equivalent should be invented; omit it from the OpenClaw artifact. |
| Licensing | Select one license before generation | Hermes examples and taps can carry a declared skill license.[^1] | ClawHub publication applies MIT-0 and warns against conflicting terms.[^6] |

### Important semantic mismatch

A dependency marked “required” does not mean the same thing on both platforms. Hermes keeps a skill discoverable and requests a missing secret when it is loaded, while OpenClaw may filter a skill from the ready catalog when `requires.env` or another gate is unsatisfied. The canonical manifest must therefore classify each dependency as either:[^5][^1]

- **Hard**: the skill cannot perform any meaningful operation without it; render an OpenClaw readiness gate.
- **Soft**: only an optional feature needs it; keep the OpenClaw skill visible and render it as an optional `envVars` declaration.
- **Setup-only**: needed by an initialization script but not every run; document setup and avoid permanent runtime gating.

## Repository architecture

Use the following layout, where generated installation surfaces are committed so GitHub users do not need the build tool:

```text
dual-skill-repo/
├── CLAUDE.md
├── AGENTS.md
├── README.md
├── LICENSE
├── SKILL.md                       # generated OpenClaw/root artifact
├── scripts/                       # generated OpenClaw support files
├── references/                    # generated OpenClaw support files
├── assets/                        # generated OpenClaw support files, if used
├── skills/
│   └── <skill-slug>/              # generated Hermes tap artifact
│       ├── SKILL.md
│       ├── scripts/
│       ├── references/
│       └── assets/
├── authoring/
│   ├── manifest.yaml              # canonical identity and requirements
│   ├── SKILL.body.md              # shared instructions
│   ├── adapters/
│   │   ├── hermes.preamble.md
│   │   └── openclaw.preamble.md
│   ├── scripts/
│   ├── references/
│   └── assets/
├── tools/
│   ├── build.py
│   ├── check_drift.py
│   └── validate_links.py
├── tests/
│   ├── test_build.py
│   ├── test_frontmatter.py
│   ├── test_resources.py
│   └── test_scripts.py
├── .github/workflows/ci.yml
├── .clawhubignore
└── pyproject.toml
```

The root artifact exists because OpenClaw’s Git installer expects a source-root `SKILL.md`; the nested artifact exists because a Hermes tap conventionally discovers entries under `skills/`. The two generated package views should contain real files, not symlinks: both ecosystems apply path-containment and security checks, and materialized copies are more predictable across Git, ZIP, sandbox, and registry installation paths.[^5][^1]

Use `.clawhubignore` to exclude `authoring/`, `tools/`, `tests/`, CI files, and other repository-only material from a ClawHub publication. ClawHub honors `.clawhubignore` and `.gitignore`, while accepting regular files from the selected skill directory.[^6]

## Canonical manifest

`authoring/manifest.yaml` is the only source for metadata. A recommended schema is:

```yaml
schema_version: 1
skill:
  name: example-skill
  description: Performs X when users need Y.
  license: MIT-0
  compatibility: Requires network access and Python 3.
  author: owner-or-organization
  version: 0.1.0
  homepage: https://github.com/owner/repo

platforms:
  - linux
  - macos
  - windows

dependencies:
  bins:
    - name: python3
      mode: hard
  env:
    - name: EXAMPLE_API_KEY
      mode: soft
      prompt: Example API key
      help: https://example.com/keys
      required_for: Authenticated API operations

hermes:
  tags: [example, automation]
  requires_toolsets: [terminal]
  requires_tools: []
  fallback_for_toolsets: []
  fallback_for_tools: []
  config: []

openclaw:
  user_invocable: true
  disable_model_invocation: false
  command_dispatch: null
  command_tool: null
  installers: []
```

`schema_version` belongs to the authoring manifest, not necessarily to generated `SKILL.md` frontmatter. The generator should reject unknown manifest keys, duplicate dependencies, invalid modes, incompatible invocation combinations, and names that do not satisfy the open standard.

If ClawHub publication is expected, MIT-0 is the least-friction repository license because ClawHub distributes published skills under MIT-0 and does not support a per-skill conflicting override. If ClawHub will never be used, select another SPDX license deliberately and remove ClawHub publication from the release plan.[^6]

## Generated frontmatter

### Hermes output

The Hermes package should include only fields that Hermes or the common specification uses. A representative output is:

```yaml
---
name: example-skill
description: Performs X when users need Y.
license: MIT-0
compatibility: Requires network access and Python 3.
version: 0.1.0
author: owner-or-organization
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [example, automation]
    requires_toolsets: [terminal]
required_environment_variables:
  - name: EXAMPLE_API_KEY
    prompt: Example API key
    help: https://example.com/keys
    required_for: Authenticated API operations
---
```

Hermes documents `version`, `author`, `platforms`, `metadata.hermes`, and required environment-variable declarations as supported skill fields. Omit empty fields rather than emitting empty arrays or null values.[^8][^1]

### OpenClaw output

The OpenClaw package should render its extension beneath `metadata.openclaw`:

```yaml
---
name: example-skill
description: Performs X when users need Y.
license: MIT-0
compatibility: Requires network access and Python 3.
homepage: https://github.com/owner/repo
user-invocable: true
metadata:
  author: owner-or-organization
  version: "0.1.0"
  openclaw:
    requires:
      bins: [python3]
    envVars:
      - name: EXAMPLE_API_KEY
        required: false
        description: Used for authenticated API operations.
---
```

OpenClaw recognizes runtime prerequisites beneath `metadata.openclaw` and checks declared metadata against actual environment-variable usage during registry analysis. Use the current `openclaw` namespace, not legacy `clawdbot` or `clawdis` aliases.[^5][^6]

### Portable overlap rule

The generator may emit the same file byte-for-byte for both targets when no platform extension is needed. When extensions are necessary, the body and bundled resources must remain identical unless a documented adapter section is required. Platform-specific differences should be measurable in CI rather than hidden in manual copies.

## Shared body design

Use this section order in `authoring/SKILL.body.md`:

```markdown
# Skill title

One-sentence operational purpose.

## When to Use

- Positive trigger conditions.
- Requests and keywords that should activate it.
- Explicit non-goals that should not activate it.

## Inputs

- Required inputs.
- Optional inputs and defaults.
- Validation and trust assumptions.

## Procedure

1. Ordered action with a checkable completion condition.
2. Ordered action with a checkable completion condition.
3. Final action that produces the requested result.

## Safety

- Confirmation boundaries for destructive or external actions.
- Secret-handling and untrusted-input rules.
- Network and filesystem boundaries.

## Pitfalls

- Known failure mode and its correction.

## Verification

- Observable success criteria.
- Commands or checks that prove the operation succeeded.
- Required failure reporting when verification cannot complete.
```

Hermes explicitly recommends `When to Use`, `Quick Reference`, `Procedure`, `Pitfalls`, and `Verification`; OpenClaw’s own skill-creator guidance likewise favors ordered procedures with checkable completion criteria and final verification. Add `Quick Reference` only when it reduces repeated reading, and move long command tables to `references/`.[^9][^1]

All bundled files must be linked or named from `SKILL.md` using shallow relative paths. Hermes’ GitHub and URL installers copy `SKILL.md` plus the referenced local files and can omit unreferenced repository files. The build must fail when a referenced file is absent or when a support file is unreachable from the skill entry point.[^8]

Never place secrets, local absolute paths, personal identifiers, session transcripts, or machine-specific state in the body or examples. Scripts must validate untrusted arguments, avoid shell interpolation where structured process APIs are available, use timeouts for network operations, and produce actionable nonzero failures.

## Build behavior

`tools/build.py` must be deterministic and perform these operations:

1. Parse and validate `authoring/manifest.yaml`.
2. Validate the skill slug against the Agent Skills naming rules.
3. Parse the canonical Markdown body and reject unresolved template tokens.
4. Render the Hermes and OpenClaw frontmatter independently.
5. Replace `{{SKILL_DIR}}` with `${HERMES_SKILL_DIR}` for Hermes and `{baseDir}` for OpenClaw.[^1][^2]
6. Add only the required adapter preamble or appendix.
7. Copy support files while preserving executable bits.
8. Delete stale generated files that no longer exist in `authoring/`.
9. Write outputs in stable key and file order.
10. Re-run the build in memory and prove that a second generation produces no diff.

`tools/check_drift.py` should build into a temporary directory and compare it with committed outputs. CI fails if generated files differ, ensuring pull requests cannot modify only one platform copy.

## Claude project contract

Commit a concise root `CLAUDE.md`. Claude Code loads a project `CLAUDE.md` at session start and uses it for build commands, architectural decisions, naming rules, and standard workflows. It should contain the following operational contract:[^10]

```markdown
# Project purpose

This repository authors one Agent Skill and generates Hermes and OpenClaw packages.

# Sources of truth

- Edit `authoring/manifest.yaml`, `authoring/SKILL.body.md`, and files under `authoring/`.
- Do not hand-edit root `SKILL.md`, root support directories, or `skills/<skill-slug>/`.
- Do not duplicate platform-neutral instructions in adapter files.

# Required workflow

1. Inspect the canonical manifest and body before changing behavior.
2. Change the shared source first.
3. Add an adapter change only when a documented runtime difference requires it.
4. Run `python tools/build.py`.
5. Run `python tools/check_drift.py`.
6. Run the complete test and validation commands documented below.
7. Report changed behavior, generated artifacts, tests, and unresolved platform differences.

# Hard rules

- Never commit secrets, credentials, local absolute paths, or generated runtime state.
- Keep the skill name identical across manifests, directories, and generated frontmatter.
- Every support file must be referenced from the skill.
- Every procedure must end in verification or an explicit unverifiable result.
- Do not invent platform fields, tools, commands, or compatibility claims.
- Preserve backward compatibility unless a release note explicitly declares a breaking change.
```

Keep detailed platform facts in `AGENTS.md` or `references/authoring.md`, then point to that file from `CLAUDE.md`. Claude’s guidance recommends keeping always-loaded project instructions concise and putting procedural material in on-demand skills or focused documentation.[^11][^12]

## Validation pipeline

The CI workflow should run on pushes and pull requests with these gates:

1. **Canonical schema**: validate `manifest.yaml` types, enums, names, URLs, and dependency modes.
2. **Deterministic build**: regenerate both artifacts and fail on a Git diff.
3. **Open-spec validation**: run the Agent Skills reference validator where the generated frontmatter remains strictly portable; `skills-ref validate` checks frontmatter and naming rules.[^3]
4. **Hermes structural checks**: parse the Hermes frontmatter, verify required fields, validate platform values, and check all declared referenced files.
5. **OpenClaw structural checks**: parse `metadata.openclaw`, verify hard/soft dependency mapping, and reject legacy metadata namespaces.
6. **Resource checks**: reject broken links, path traversal, symlinks leaving the skill tree, unexpected binaries, and unreferenced support files.
7. **Script tests**: lint and execute focused tests for every helper script.
8. **Security checks**: scan for likely secrets, destructive command patterns, unsafe shell interpolation, and undocumented network destinations.
9. **Package smoke tests**: create clean temporary installs for both generated views.
10. **Documentation checks**: verify README install commands and examples against the generated slug.

Use native smoke tests when the platform CLIs are available:

```bash
# Hermes
hermes chat --toolsets skills -q "Use the example-skill skill to perform its test fixture"

# OpenClaw
openclaw skills list
openclaw skills check
openclaw agent --message "perform the example skill test fixture"
```

Hermes documents direct skill testing through `hermes chat --toolsets skills`; OpenClaw documents `openclaw skills list`, direct agent invocation, and `openclaw skills check` for readiness problems.[^2][^1][^5]

Test discovery as behavior, not only syntax. Maintain at least:

- Three positive prompts that should activate the skill.
- Three negative prompts that should not activate it.
- One explicit slash-command invocation per platform.
- One missing-hard-dependency case.
- One missing-soft-dependency case.
- One malicious or malformed input case.
- One helper failure case proving that the skill reports the failure instead of claiming success.

## README requirements

The README should lead with what the skill does, supported platforms, and current maturity. It must include separate installation sections:

### Hermes

```bash
hermes skills tap add owner/repo
hermes skills install owner/repo/<skill-slug>
```

Hermes supports GitHub-backed taps and direct GitHub skill installation.[^7][^1]

### OpenClaw

```bash
openclaw skills install git:owner/repo@<tag-or-commit>
```

OpenClaw supports Git repository installation and installs into the active workspace by default.[^5]

Also document:

- Required and optional dependencies.
- Secret names without example secret values.
- Platform-specific behavioral differences.
- Verification commands.
- Security boundaries and destructive actions.
- Versioning and upgrade instructions.
- A statement that root and `skills/<slug>/` outputs are generated from one source.

Pin documentation examples to a release tag or commit when reproducibility matters. Do not imply that Git-sourced OpenClaw installations receive ClawHub’s update tracking; OpenClaw documents automatic update tracking for ClawHub installs, while Git or local sources are refreshed by reinstalling.[^5]

## Release workflow

Use semantic versions in the canonical manifest and Git tags such as `v0.1.0`. A release must:

1. Build both artifacts.
2. Pass CI and native smoke tests.
3. Confirm generated outputs contain no local paths or credentials.
4. Confirm the README install commands use the correct slug and tag.
5. Tag the exact commit used for both packages.
6. Publish the Hermes path or tap entry.
7. Publish the OpenClaw package from the clean generated skill directory when using ClawHub.
8. Record any platform-specific behavior in release notes.

ClawHub versions are semver-based and tags point to versions; its security analysis compares declared runtime needs with actual behavior. Hermes can publish a skill to GitHub and consume custom repositories as taps.[^1][^6]

## Split-repository migration

When separate repositories are desired, do not fork the authored content manually. Add export targets:

```text
export/hermes-repo/
├── README.md
├── LICENSE
└── skills/<skill-slug>/...

export/openclaw-repo/
├── README.md
├── LICENSE
├── SKILL.md
├── scripts/
├── references/
└── assets/
```

Initially generate both exports from the same canonical source and push them with a release workflow. Split the source of truth only if the actual procedures—not merely metadata, paths, dependency gating, or installation commands—become materially different. Until then, a common source plus adapters provides clearer maintenance and prevents one platform from silently receiving stale fixes.

## Acceptance criteria

Claude may consider the initial repository complete only when all of the following are true:

- One canonical manifest and one canonical body generate both packages.
- Root `SKILL.md` is installable as the OpenClaw Git source.
- `skills/<skill-slug>/SKILL.md` is installable through a Hermes tap.
- Both generated names match their package directories.
- No unresolved template token remains.
- Platform directory tokens are rendered correctly.
- Hard and soft dependencies preserve the intended platform semantics.
- Every bundled resource is referenced and present in both packages.
- A second build produces no changes.
- Generic, platform-specific, script, security, and smoke tests pass.
- The repository contains no secrets, personal paths, or mutable runtime state.
- README commands have been tested from clean environments.
- The generated artifacts differ only where an adapter requirement is documented.

The core design principle is: **share behavior, isolate runtime metadata, generate installation layouts, and test both as products**. This gives the current GitHub repository genuine dual-platform utility while preserving a clean path to separate Hermes and OpenClaw repositories later.

---

## References

1. [Creating Skills | Hermes Agent - Nous Research](https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills) - How to create skills for Hermes Agent — SKILL.md format, guidelines, and publishing

2. [Creating skills](https://docs.openclaw.ai/tools/creating-skills) - Build, test, and publish custom SKILL.md workspace skills or personal skills on a shared Gateway.

3. [Specification](https://agentskills.io/specification) - The SKILL.md file must contain YAML frontmatter followed by Markdown content. Max 64 characters. Low...

4. [Hermes Agent Skill Authoring — Author in-repo SKILL.md files](https://hermes-agent.nousresearch.com/docs/user-guide/skills/bundled/software-development/software-development-hermes-agent-skill-authoring) - Author in-repo SKILL.md files: frontmatter and structure

5. [Skills](https://docs.openclaw.ai/tools/skills) - Skills teach your agent how to use tools. Learn how they load, how precedence works, and how to conf...

6. [Skill format - OpenClaw Docs](https://docs.openclaw.ai/clawhub/skill-format) - Skill folder format, required files, supporting artifacts, limits.

7. [hermes-agent/website/docs/user-guide/features/skills.md ...](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md) - Skills are on-demand knowledge documents the agent can load when needed. They follow a progressive d...

8. [Skills System | Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/) - On-demand knowledge documents — progressive disclosure, agent-managed skills, and the Skills Hub

9. [openclaw/skills/skill-creator/SKILL.md at main - GitHub](https://github.com/openclaw/openclaw/blob/main/skills/skill-creator/SKILL.md) - The AI that really does things. Any OS. Any Platform. The lobster way. 🦞 - openclaw/openclaw

10. [How Claude remembers your project - Claude Code Docs](https://code.claude.com/docs/en/memory) - CLAUDE.md files are markdown files that give Claude persistent instructions for a project, your pers...

11. [Using CLAUDE.MD files: Customizing Claude Code for your codebase | Claude by Anthropic](https://claude.com/blog/using-claude-md-files) - Learn how to use CLAUDE.md files to give Claude Code persistent context about your project structure...

12. [Steering Claude Code: when to use CLAUDE.md, skills, hooks ...](https://claude.com/blog/steering-claude-code-skills-hooks-rules-subagents-and-more) - Seven ways to steer Claude Code—CLAUDE.md files, rules, skills, subagents, hooks, and more—and when ...

