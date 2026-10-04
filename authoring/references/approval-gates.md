# Approval gates — choosing one, running the interview, writing your own

This is both the reference for `paths.approval_gate` and an **interview module**:
an agent onboarding a home runs Discover → Decide → Recommend below and wires the
result. It is the first shipped piece of the Decide methodology.

## The contract

`paths.approval_gate` is an **argv prefix**. When a remediation runs, the engine
executes `gate_argv + [script_path] (+ [arg])` — the gate receives the remediation
command as *its own argv* and decides:

- **Approve** → `exec bash "$@"` — exec through, never `eval`, never compose a
  shell string from anything a finding or a chat message produced.
- **Refuse** → `exit 65` with a one-line reason on stdout (the engine and
  `approve-heal.py` surface it; 75 stays reserved for a remediation's own rate cap).
- **Bound the argv yourself** → `exit 65` for anything but `<script>` plus at most
  one argument, and for any argument that is not one token of letters, digits,
  `.` `_` `@` `:` `-` — no leading `-`, no slash, no whitespace, 64 characters at
  most (`[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}`, the engine's own rule). The engine
  checks it too; the gate re-checks because it never trusts its caller, and it
  checks *before* the auto-pass branch so the engine's path is bounded as well.
- **Show the full script path** — in the prompt and in the audit line, never the
  basename: an admin approving `restart.sh` cannot tell
  `/opt/cranston/remediations/restart.sh` from `/tmp/restart.sh`.

Callers, distinguished by environment:

| Caller | Env | What the gate should do |
|---|---|---|
| engine auto path | `SELFHEAL_AUTOMATION=true`, `SELFHEAL_CALLER=selfheal.py` | pass through non-interactively IF the script lives under `$SELFHEAL_ROOT/remediations/` (auto-class fixes are fail-safe by design; blocking them on a human breaks self-healing) |
| human approval | `SELFHEAL_CALLER=approve-heal`, plus `GATE_CODE` when the admin's reply carried a code (`approve-heal.py <key> <code>`) | demand the second factor |

Budgets: the engine's auto path allows 180 s total; `approve-heal.py` allows 300 s
total, so keep human interaction within ~120 s (`GATE_TIMEOUT` in the templates).
A gate must be safe to re-run (refusal changes nothing; the pending approval is
kept on exit 65). `SELFHEAL_AUDIT_LOG` is set — write a line for every decision.

## Interview: Discover

Probe (commands) and ask (questions); record the answers:

| # | Probe / question | How |
|---|---|---|
| D1 | Does the deployment already have a gated command path? | look for an existing 2FA wrapper the host framework routes dangerous commands through (e.g. an OpenClaw secure-bash) |
| D2 | What risk class is actually configured? | read `services.json` `remediations`: only watch-only/fail-safe templates (`ha-service-call` is structurally ON-only), or anything that cuts power / restarts shared services? |
| D3 | Is a chat bot already wired? | a bot-credentials file exists (mode 600) and the admin's chat id is known |
| D4 | Is there a second always-on device on the LAN? | HA box, NAS, spare Pi, an old phone (see `hardware.md` for the phone recipe) that answers ping and can run python3 |
| D5 | Who approves? | solo admin, or multiple approvers / `consent` scopes in use |

## Interview: Decide (first match wins)

| Condition | Recommendation |
|---|---|
| D1 yes | **Reuse the existing gate.** Don't build a second one. |
| D2 risky **and** D4 yes | **Tier 2**: `gates/gate-totp-remote.sh` here + `gates/gate-totp-server.py` on the second device. |
| D2 risky **and** D3 yes (no second device) | **Tier 1**: `gates/gate-telegram-confirm.sh`. |
| D2 risky, no factor possible (no bot, no second device) | **Don't gate — shrink**: demote the risky remediations to watch-only until a factor exists. A weaker gate is not the answer. |
| D2 fail-safe-only (solo admin) | **Tier 0**: no `approval_gate`. SSH-key possession is the gate. Revisit when a risky remediation is added. |

## Interview: Recommend (say this to the admin)

- **Tier 0**: "Your SSH key is already the approval gate: `approve-heal.py` only runs
  for someone with shell on this box, and nothing configured here can act in an
  unsafe direction on its own. Adding TOTP on this same box would be theater — the
  secret would sit where the agent could read it. If we ever add a remediation that
  can cut power to something shared, we revisit this."
- **Tier 1**: "You already run a Telegram bot, so approvals can prove possession of
  your phone: when you approve a fix, the gate itself messages you a one-time code
  and waits for your reply — it talks to Telegram directly, so the agent can't fake
  your answer. Nothing new to install."
- **Tier 2**: "Your <second device> is always on, so the 2FA secret can live there
  instead of here: a 100-line verifier answers 'is this code valid', your
  authenticator app holds the enrolment, and this box — the one the agent lives
  on — can never mint its own approvals. No Docker, no dependencies: one python
  file and a mode-600 secret."
- **Shrink**: "There's no second factor available in this home right now, so instead
  of a gate that only pretends to protect you, I'd demote <risky remediations> to
  watch-only: you'll be paged with the proposed fix and run it yourself over SSH.
  When a second device or a chat bot shows up, we upgrade."
- **Reuse**: "Your deployment already routes dangerous commands through <gate>; the
  engine will use the same path, so there's one audit trail and one factor."

## The anti-pattern (name it when you see it)

**A TOTP secret on the agent's own box is theater.** `oathtool --totp "$(cat
~/.totp_secret)"` feels like 2FA, but the agent can read the same file and mint its
own codes. The factor must live where the agent isn't: another device (Tier 2) or
the admin's phone via a channel the gate reads directly (Tier 1).

## Writing your own gate

Start from `scripts/gates/TEMPLATE.sh`. Checklist:

1. Exec through (`exec bash "$@"`); never eval; argv only.
2. Bound the argv first (step 0 in the template): `$#` is 1 or 2, and `$2`, when
   present, matches `[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}` — otherwise exit 65. Keep
   the check above the auto-path pass-through.
3. Keep the auto-path pass-through (step 1 in the template) or routine self-heals
   stall on a human.
4. Pin identities — chat id, verifier URL — in a mode-600 env file, never in
   `services.json`.
5. Fail closed: timeout, unreachable verifier, malformed anything → exit 65.
6. Audit every decision (`GATE-AUTO-PASS` / `GATE-APPROVED` / `GATE-REFUSED`) and
   show the approver the full resolved script path, never the basename.
7. Test the refusal path before the approval path.
