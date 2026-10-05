---
name: cranston
description: Self-healing home monitor that fixes, asks first, or digests
license: MIT-0
compatibility: Requires python3 3.9+, bash, curl, ping, and system cron on an always-on LAN box
version: 0.2.0
author: jetsharklambo
platforms: [linux, macos]
metadata:
  hermes:
    tags: [home-automation, monitoring, self-healing]
    requires_toolsets: [terminal]
---
# Cranston — self-healing home monitor

> **Hermes notes (experimental).** This package is generated from the same
> source as the OpenClaw artifact and follows the documented tap conventions,
> but it has not yet run on a real Hermes deployment — treat it as untested.
> After install the skill is exposed as a slash command; natural-language
> requests matching "When to Use" also activate it. `TG_BOT_TOKEN`/
> `TG_CHAT_ID` are only needed by the example Telegram sink in the Procedure;
> any `paths.alert_sink` command of your own works without them.

Install, operate, and approve fixes for a cron-driven monitoring engine that
watches home infrastructure, auto-heals what is safe, asks permission for what
is not, and batches the rest into one daily digest.

## When to Use

- The user asks to set up monitoring/self-healing for home infrastructure
  (DNS box, media server, Home Assistant, battery backup, containers, disks).
- The user asks Cranston to learn/map/onboard their home — run the onboarding
  flow (see "Onboard a home" in the Procedure).
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
| Engine (one cycle per cron run) | `${HERMES_SKILL_DIR}/scripts/engine/selfheal.py` |
| Scripted install (deploy copy, env skeleton, dry run, cron lines) | `${HERMES_SKILL_DIR}/scripts/install.sh` |
| Approve a pending fix | `${HERMES_SKILL_DIR}/scripts/engine/approve-heal.py <key>` |
| Helper libs checks/remediations source | `${HERMES_SKILL_DIR}/scripts/engine/lib/` |
| Alert queue, drainer, sender, digest flusher | `${HERMES_SKILL_DIR}/scripts/bin/` |
| Config contract | `${HERMES_SKILL_DIR}/references/services.schema.md` |
| Starter config | `${HERMES_SKILL_DIR}/assets/services.example.json` |
| Worked 12-service example (+ its notes) | `${HERMES_SKILL_DIR}/references/example-cranston/` |
| Check templates (9) | `${HERMES_SKILL_DIR}/scripts/checks/templates/` |
| Remediation templates | `${HERMES_SKILL_DIR}/scripts/remediations/templates/` |
| Approval-gate templates (incl. the `secure-bash-argv.sh` wrapper) + interview | `${HERMES_SKILL_DIR}/scripts/gates/`, `${HERMES_SKILL_DIR}/references/approval-gates.md` |
| Per-device interview (fills gaps in a hand-written config) | `${HERMES_SKILL_DIR}/scripts/onboard/` |
| Onboarding methodology (index) | `${HERMES_SKILL_DIR}/references/methodology.md` |
| Discovery probes (D1–D9) | `${HERMES_SKILL_DIR}/references/discovery.md` |
| Priority-of-needs interview (U1–U17) | `${HERMES_SKILL_DIR}/references/interview.md` |
| Failure-mode audit library (F1–F20) | `${HERMES_SKILL_DIR}/references/failure-modes.md` |
| House doctrine template | `${HERMES_SKILL_DIR}/references/doctrine.md` |
| Hardware guidance | `${HERMES_SKILL_DIR}/references/hardware.md` |
| Live state (per deployment) | `<deploy-root>/state/state.json`, `pending-approvals.json`, `audit.log` |

## Inputs

- **A deployment root**: a writable directory on an always-on LAN box
  (see `${HERMES_SKILL_DIR}/references/hardware.md` for choosing the box). The skill
  directory itself may be replaced on upgrade — deploy a copy, never cron the
  skill directory directly.
- **`services.json`**: one block per watched service; every probe target in
  its `params`. Contract: `${HERMES_SKILL_DIR}/references/services.schema.md`.
- **An alert sink**: any command that delivers lines of text to the admin
  (`paths.alert_sink`). The bundled default is a queue plus a drainer
  (`scripts/bin/`): `send-alert.sh` only queues lines into the pending-alert
  file. `notify-alerts.sh` drains that file from cron every minute and
  delivers through `send-telegram.sh` (or the command in
  `SELFHEAL_NOTIFY_CMD`), deleting a line only once it was sent. A
  standalone sender of your own in `alert_sink` is the alternative (short
  form in the Install procedure).
- **Optional approval gate** (`paths.approval_gate`): an argv prefix the
  engine prepends when executing remediations (the deployment's 2FA/audit
  path). Choose it with the gate interview (see Procedure); working templates
  ship in `${HERMES_SKILL_DIR}/scripts/gates/`. Until one is configured, keep
  nothing in `remediations` you wouldn't run on a plain shell.

## Procedure

### Install

Scripted: `bash ${HERMES_SKILL_DIR}/scripts/install.sh --deploy /opt/cranston`
performs steps 1 and 3–4 below (deploy copy, env-file skeleton, dry run) and
prints the step-5 cron lines; it never overwrites an existing `services.json`
or env file, and only `--apply-cron` touches the crontab (with a backup).
Re-run it after a skill upgrade to refresh the code directories. The manual
steps:

1. Create the deployment root and copy the engine out of the skill:

   ```bash
   DEPLOY=/opt/cranston          # any writable always-on location
   mkdir -p "$DEPLOY"
   cp -R ${HERMES_SKILL_DIR}/scripts/. "$DEPLOY"/
   cp ${HERMES_SKILL_DIR}/assets/services.example.json "$DEPLOY"/services.json
   ```

2. Edit `$DEPLOY/services.json`. Start small: three services the household
   actually feels when they break beat twelve nobody notices. Use the check
   templates via relative paths (`checks/templates/check-http.sh` etc.) with
   `params` blocks; a tailnet second transport on `check-http.sh` takes
   `bin/tailscale-running.sh` as its `ALT_GUARD_CMD`. Field-by-field docs
   are in `${HERMES_SKILL_DIR}/references/services.schema.md`.

3. Wire the alert path. The default `alert_sink`, `bin/send-alert.sh`, only
   queues; `bin/notify-alerts.sh` (cron, step 5) delivers the queue through
   `bin/send-telegram.sh`. The secrets and the shared alert-file path live in
   ONE mode-600 file that every cron line sources — the alert file must be
   the same for the engine's sink and the drainer, and under `state/` it is
   off `/tmp`, which a reboot wipes:

   ```bash
   cat > /etc/cranston.env <<'EOF'
   export TG_BOT_TOKEN=<token from @BotFather>
   export TG_CHAT_ID=<chat id the bot may write to>
   # export TG_ANNOUNCE_CHAT_ID=<optional second chat for household pre-announcements>
   export SELFHEAL_ALERT_FILE=/opt/cranston/state/alert-pending.json
   EOF
   chmod 600 /etc/cranston.env
   . /etc/cranston.env; "$DEPLOY/bin/send-telegram.sh" "cranston test page"
   ```

   Leave `"alert_sink": "bin/send-alert.sh"` as the starter config has it;
   never put the token in the config. Alternative: any command that
   delivers lines of text works as `alert_sink`, so a standalone sender
   (`"alert_sink": "bin/my-sink.sh"`) is one `curl`:

   ```bash
   cat > "$DEPLOY/bin/my-sink.sh" <<'EOF'
   #!/bin/bash
   TEXT=$(printf '%s\n' "$@")
   curl -fsS --max-time 15 "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT_ID}" --data-urlencode "text=${TEXT}" >/dev/null
   EOF
   chmod +x "$DEPLOY/bin/my-sink.sh"
   ```

   **Announcements**: with `TG_ANNOUNCE_CHAT_ID` set (a shared household
   chat), remediation entries whose `consent` is `household` or
   `named:<person>` get their plain-language `announce` text delivered there
   just before the fix runs ("restarting the media box — music will stop
   ~2 min"); unset, announcements fold into the admin page. See the consent
   bullet in `references/services.schema.md`.

4. Dry-run one cycle and inspect the state it writes:

   ```bash
   python3 "$DEPLOY/engine/selfheal.py" --once
   cat "$DEPLOY/state/state.json"
   ```

5. Put it on **system cron** (never an agent's scheduler — monitoring must
   survive the agent dying). Every line sources `/etc/cranston.env` first
   because cron starts from an empty environment: without it the sender has
   no token, and the engine and the drainer would use different alert files.

   ```cron
   */2 * * * * . /etc/cranston.env; flock -n /tmp/cranston.cronlock python3 /opt/cranston/engine/selfheal.py >> /var/log/cranston.log 2>&1
   * * * * *   . /etc/cranston.env; /opt/cranston/bin/notify-alerts.sh >> /var/log/cranston.log 2>&1
   25 5 * * *  . /etc/cranston.env; SELFHEAL_DIGEST_FILE=/opt/cranston/state/digest.jsonl SELFHEAL_NOTIFY_CMD=/opt/cranston/bin/send-telegram.sh /opt/cranston/bin/flush-digest.sh >> /var/log/cranston.log 2>&1
   ```

   macOS has no `flock(1)`: drop the `flock -n /tmp/cranston.cronlock` prefix
   there (the installer does this automatically) — the engine's own
   non-blocking state lock already makes an overlapping cycle exit instead
   of stacking.

### Onboard a home (the methodology)

A hand-written `services.json` is the default: the Install procedure above
is complete without any interview, and the config the admin writes is the
source of truth. The flow below is for when the admin asks Cranston to learn
the home, or would rather be *asked* than fill in fields by hand (index:
`${HERMES_SKILL_DIR}/references/methodology.md`). Never start it unasked.

1. **Discover** (`${HERMES_SKILL_DIR}/references/discovery.md`): run the D1–D9
   read-only probes, ask the per-category questions, and fill the inventory
   draft. Probes only observe; nothing is changed.
2. **Interview — per device, only the gaps** (`${HERMES_SKILL_DIR}/scripts/onboard/`;
   the question catalogue and what each answer maps to:
   `${HERMES_SKILL_DIR}/references/interview.md`). The tool reads every device in
   the admin's `services.json`, ranks which ones need a decision first
   (failing right now › failing for a week › a shipped fix fits › no
   fix/ask/tell chosen › an ask-first fix with no consent scope › chronic
   codes with no nag cadence), and hands you ONE device-shaped question at a
   time. Relay it verbatim; record the admin's words verbatim (the keyword at
   the front is what maps). Re-interview one device, never the whole house.

   ```bash
   python3 "$DEPLOY/onboard/interview.py" add <device> <kind>    # a device not in the config yet (http, tcp, dns, systemd, disk, cert, ha-entity, host-power, lan-inventory)
   python3 "$DEPLOY/onboard/interview.py" plan                   # ranked devices + why
   python3 "$DEPLOY/onboard/interview.py" next                   # one question (--device <name> to pick)
   python3 "$DEPLOY/onboard/interview.py" answer <device> <kind> "<the admin's words>"
   python3 "$DEPLOY/onboard/interview.py" fill                   # the diff, written to services.draft.json
   python3 "$DEPLOY/onboard/interview.py" fill --apply           # write services.json (backup kept)
   python3 "$DEPLOY/onboard/interview.py" reask <device>         # reopen ONE device's questions
   python3 "$DEPLOY/onboard/interview.py" doctrine               # draft the doctrine sections the answers cover
   ```

   `fill` never overwrites a field the admin set by hand; it may revise only
   its own earlier fills, and only on a device under `reask` (or named with
   `--device <name> --overwrite`). After `--apply`, verify with a `--once`
   cycle. The house-level questions in `interview.md` (alert budget,
   dark-house channel, paying for reliability) are conversation-only.
3. **Audit** (`${HERMES_SKILL_DIR}/references/failure-modes.md`): walk F1–F20
   against the inventory; each hit becomes a service entry, a scheduled
   audit, or a line in the doctrine's standing hands-on list.
4. **Generate DRAFTS, never apply**: emit a draft `services.json` (map each
   need onto a check/remediation template via its `params` block; start
   small — three services the household feels beat twelve nobody notices)
   and a draft house doctrine from
   `${HERMES_SKILL_DIR}/references/doctrine.md`. The admin reviews and owns both;
   nothing is installed or scheduled until they approve.
5. Then: the gate interview (next section), a `--once` dry-run, and cron —
   exactly as in the Install procedure above.

### Choose the approval gate (interview)

Run the Discover → Decide → Recommend flow in
`${HERMES_SKILL_DIR}/references/approval-gates.md`: probe for an existing gated
command path, a configured chat bot, and a second always-on device; read
which risk class `remediations` actually contains; then recommend ONE of —
reuse the deployment's existing gate (shipped as `gates/secure-bash-argv.sh`
when that gate is a one-shell-string 2FA wrapper such as OpenClaw's
secure-bash), `gates/gate-totp-remote.sh` + the single-file verifier on the
second device, `gates/gate-telegram-confirm.sh`, plain SSH-as-the-gate for
fail-safe-only setups, or demoting risky remediations to watch-only when no
second factor exists. Say the honest caveat lines verbatim (they are written
in the reference), wire the choice into `paths.approval_gate`, and **drill
the refusal path once** before trusting the approval path. Never place a
TOTP secret on this box — that is the anti-pattern the reference names.

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
it names whose evening the fix may ruin — `approve-heal.py` queues the entry's
pre-announcement to the household automatically at approval time (see
Install, "Announcements"), but a human heads-up before approving is still
the polite default.

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
  `${HERMES_SKILL_DIR}/scripts/remediations/templates/TEMPLATE.sh`.
- Before wiring any remediation that cuts power or restarts something shared:
  walk the blast radius, verify the restore path, set `consent`, and cap it.
- `remediations/templates/ha-outlet-cycle.sh` is ask-first only — it cuts
  power — and refuses the auto path; keep that refusal when adapting it.
- Never commit or echo secrets; sink credentials live in mode-600 files.
- This skill's files are generated from an authoring source — never hand-edit
  `SKILL.md` or the packaged `scripts/`/`references/`/`assets/` in place;
  changes go to the repository's `authoring/` tree.

## Pitfalls

- **Nothing pages on the first bad cycle** — anti-flap needs two consecutive
  failures (~4 min at the */2 cadence). That's by design; don't "fix" it.
- **Manual config wins.** A finding code absent from `remediations` is
  watch-only to the engine and *undecided* to the interview; an explicit
  `null` is a decision. The interview reports a hand-set field it disagrees
  with as kept — it never changes it.
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
- A test page arrives: `"$DEPLOY/bin/send-telegram.sh" "cranston test page"`
  (with `/etc/cranston.env` sourced).
- Forced-failure drill: break one target on purpose, run two cycles, watch
  the page arrive; fix it and run cycles until the held ✅ releases.
- The drainer delivers a queued line within a minute: after a forced page,
  watch `$DEPLOY/state/alert-pending.json` empty as the page arrives.
- After an interview: `python3 "$DEPLOY/onboard/interview.py" plan` reports
  nothing to ask and `fill` reports nothing to fill, and a `--once` cycle on
  the filled config exits 0.
- The repository's offline suites pass: `python3 tests/test_engine.py`,
  `bash tests/test_templates.sh`, `bash tests/test_delivery.sh` and
  `python3 tests/test_interview.py` (run from a checkout; they are not
  shipped in the skill package).
- If any of this cannot be verified (no cron access, sink undeliverable),
  report exactly what was and wasn't confirmed — never claim a watch that
  isn't running.