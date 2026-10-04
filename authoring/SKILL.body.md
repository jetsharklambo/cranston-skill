# Cranston — self-healing home monitor

Install, operate, and approve fixes for a cron-driven monitoring engine that
watches home infrastructure, auto-heals what is safe, asks permission for what
is not, and batches the rest into one daily digest.

## When to Use

- The user asks to set up monitoring/self-healing for home infrastructure
  (DNS box, media server, Home Assistant, battery backup, containers, disks).
- A Cranston alert arrived (a page saying `Reply 'heal <key>'`, a `🚨`/`⚠️`
  page, or a daily digest) and the user wants it explained or acted on.
- The user asks for the monitor's status, its pending approvals, or why a
  service keeps paging.
- Not for: general smart-home control ("turn off the lights"), one-off
  diagnostics of a machine Cranston doesn't watch, or editing this skill's
  generated files (see Safety).

## Quick Reference

| Thing | Where |
|---|---|
| Engine (one cycle per cron run) | `{{SKILL_DIR}}/scripts/engine/selfheal.py` |
| Approve a pending fix | `{{SKILL_DIR}}/scripts/engine/approve-heal.py <key>` |
| Helper libs checks/remediations source | `{{SKILL_DIR}}/scripts/engine/lib/` |
| Default queueing sink + digest flusher | `{{SKILL_DIR}}/scripts/bin/` |
| Config contract | `{{SKILL_DIR}}/references/services.schema.md` |
| Starter config | `{{SKILL_DIR}}/assets/services.example.json` |
| Worked 12-service example (+ its notes) | `{{SKILL_DIR}}/references/example-cranston/` |
| Check templates (9) | `{{SKILL_DIR}}/scripts/checks/templates/` |
| Remediation templates | `{{SKILL_DIR}}/scripts/remediations/templates/` |
| Approval-gate templates + interview | `{{SKILL_DIR}}/scripts/gates/`, `{{SKILL_DIR}}/references/approval-gates.md` |
| Hardware guidance | `{{SKILL_DIR}}/references/hardware.md` |
| Onboarding methodology (placeholder) | `{{SKILL_DIR}}/references/methodology.md` |
| Live state (per deployment) | `<deploy-root>/state/state.json`, `pending-approvals.json`, `audit.log` |

## Inputs

- **A deployment root**: a writable directory on an always-on LAN box
  (see `{{SKILL_DIR}}/references/hardware.md` for choosing the box). The skill
  directory itself may be replaced on upgrade — deploy a copy, never cron the
  skill directory directly.
- **`services.json`**: one block per watched service; every probe target in
  its `params`. Contract: `{{SKILL_DIR}}/references/services.schema.md`.
- **An alert sink**: any command that delivers lines of text to the admin
  (`paths.alert_sink`). The bundled `scripts/bin/send-alert.sh` only queues
  into a pending file; something must deliver it, or point `alert_sink`
  straight at a sender (Telegram example in the Procedure).
- **Optional approval gate** (`paths.approval_gate`): an argv prefix the
  engine prepends when executing remediations (the deployment's 2FA/audit
  path). Choose it with the gate interview (see Procedure); working templates
  ship in `{{SKILL_DIR}}/scripts/gates/`. Until one is configured, keep
  nothing in `remediations` you wouldn't run on a plain shell.

## Procedure

### Install

1. Create the deployment root and copy the engine out of the skill:

   ```bash
   DEPLOY=/opt/cranston          # any writable always-on location
   mkdir -p "$DEPLOY"
   cp -R {{SKILL_DIR}}/scripts/. "$DEPLOY"/
   cp {{SKILL_DIR}}/assets/services.example.json "$DEPLOY"/services.json
   ```

2. Edit `$DEPLOY/services.json`. Start small: three services the household
   actually feels when they break beat twelve nobody notices. Use the check
   templates via relative paths (`checks/templates/check-http.sh` etc.) with
   `params` blocks; field-by-field docs are in
   `{{SKILL_DIR}}/references/services.schema.md`.

3. Wire the alert path. Quickest standalone sink (Telegram):

   ```bash
   cat > "$DEPLOY/bin/my-sink.sh" <<'EOF'
   #!/bin/bash
   TEXT=$(printf '%s\n' "$@")
   curl -s --max-time 15 "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT_ID}" --data-urlencode "text=${TEXT}" >/dev/null
   EOF
   chmod +x "$DEPLOY/bin/my-sink.sh"
   ```

   Set `"alert_sink": "bin/my-sink.sh"` in the config's `paths`. Keep the
   token in a mode-600 file sourced by the cron line — never in the config.

4. Dry-run one cycle and inspect the state it writes:

   ```bash
   python3 "$DEPLOY/engine/selfheal.py" --once
   cat "$DEPLOY/state/state.json"
   ```

5. Put it on **system cron** (never an agent's scheduler — monitoring must
   survive the agent dying):

   ```cron
   */2 * * * * flock -n /tmp/cranston.cronlock python3 /opt/cranston/engine/selfheal.py >> /var/log/cranston.log 2>&1
   25 5 * * *  SELFHEAL_DIGEST_FILE=/opt/cranston/state/digest.jsonl SELFHEAL_NOTIFY_CMD=/opt/cranston/bin/my-sink.sh /opt/cranston/bin/flush-digest.sh
   ```

### Choose the approval gate (interview)

Run the Discover → Decide → Recommend flow in
`{{SKILL_DIR}}/references/approval-gates.md`: probe for an existing gated
command path, a configured chat bot, and a second always-on device; read
which risk class `remediations` actually contains; then recommend ONE of —
reuse the deployment's existing gate, `gates/gate-totp-remote.sh` + the
single-file verifier on the second device, `gates/gate-telegram-confirm.sh`,
plain SSH-as-the-gate for fail-safe-only setups, or demoting risky
remediations to watch-only when no second factor exists. Say the honest
caveat lines verbatim (they are written in the reference), wire the choice
into `paths.approval_gate`, and **drill the refusal path once** before
trusting the approval path. Never place a TOTP secret on this box — that is
the anti-pattern the reference names.

### Operate

- **Status**: read `$DEPLOY/state/state.json` (per-key status, failure
  streaks, caps) and `$DEPLOY/state/audit.log` (every EXEC/RESULT/REFUSED).
- **A page arrived**: the key and finding code name the service and symptom.
  Degraded `*_BLIND` codes mean "our probe can't see", not "the host is
  down" — say so when relaying.
- **Daily digest**: routine/degraded findings batch into
  `state/digest.jsonl`; the cron flusher delivers once a day. A failed digest
  write pages instead of dropping — never "fix" that by silencing it.

### Approve a fix

When a page says `Reply 'heal <key>'`, a proposed remediation is waiting in
`$DEPLOY/state/pending-approvals.json` (TTL `pending_ttl_hours`). On the
admin's explicit go-ahead — and only then:

```bash
python3 "$DEPLOY/engine/approve-heal.py" <key>
```

The engine runs the remediation through `paths.approval_gate` if configured,
then re-runs the service's check and reports the verified outcome. Relay that
output to the admin verbatim. If the pending entry carries a `consent` field,
it names whose evening the fix may ruin — give that household member a
heads-up before approving, not after.

## Safety

- **Never bypass the approval gate.** Ask-first remediations run only via
  `approve-heal.py` after the admin's explicit reply — not by invoking the
  remediation script directly.
- Remediations are fixed-content scripts taking at most one sanitized
  argument, executed as argv (no shell strings, no eval). Do not compose
  ad-hoc shell to "help".
- Auto-class remediations act in the fail-safe direction only (the ON-only
  templates structurally cannot switch anything off). Keep it that way when
  writing new ones — start from
  `{{SKILL_DIR}}/scripts/remediations/templates/TEMPLATE.sh`.
- Before wiring any remediation that cuts power or restarts something shared:
  walk the blast radius, verify the restore path, set `consent`, and cap it.
- Never commit or echo secrets; sink credentials live in mode-600 files.
- This skill's files are generated from an authoring source — never hand-edit
  `SKILL.md` or the packaged `scripts/`/`references/`/`assets/` in place;
  changes go to the repository's `authoring/` tree.

## Pitfalls

- **Nothing pages on the first bad cycle** — anti-flap needs two consecutive
  failures (~4 min at the */2 cadence). That's by design; don't "fix" it.
- **A recovery ✅ is held ~10 minutes** so a flapping service collapses into
  one message with a flap count instead of a page per bounce.
- **Blind is not down.** When the default gateway is unreachable, remote
  services are skipped as unknowable; when a service's root key has a
  finding, its subkeys are skipped too (a dead Docker daemon must not read as
  five healthy containers).
- The engine holds its own lock; a second concurrent cycle exits quietly.
  On boxes without `flock(1)`, drop it from the cron line.

## Verification

- `python3 "$DEPLOY/engine/selfheal.py" --once` exits 0 and writes `state/`.
- Forced-failure drill: break one target on purpose, run two cycles, watch
  the page arrive; fix it and run cycles until the held ✅ releases.
- The repository's offline suites pass: `python3 tests/test_engine.py` and
  `bash tests/test_templates.sh` (run from a checkout; they are not shipped
  in the skill package).
- If any of this cannot be verified (no cron access, sink undeliverable),
  report exactly what was and wasn't confirmed — never claim a watch that
  isn't running.
