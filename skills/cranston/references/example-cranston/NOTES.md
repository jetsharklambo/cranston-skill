# The cranston worked example

`services.json` here is the live cranston deployment (selfheal-project v1,
frozen 2026-10-02) translated to core schema v2. It is documentation: it has
never run, and the reference box stays on v1.

## What the translation shows

| v1 (cranston) | v2 (this file) |
|---|---|
| 31 absolute check/remediation paths | templates referenced relative to the install root; bespoke scripts stay absolute |
| probe targets hardcoded in each check (`check-ha.sh`: `192.168.1.35`, `check-navidrome.sh`: two URLs + a tailscale guard) | `params` blocks on the service |
| `check-ha.sh`, `check-navidrome.sh`, `check-adguard.sh`, `check-gateway.sh`, `check-host-power.sh`, `check-lan-inventory.sh` (6 of 12 checks) | replaced by 5 templates: `check-http` ×2, `check-dns`, `check-systemd-unit`, `check-host-power`, `check-lan-inventory` |
| Telegram + TOTP assumptions inside the engine | `paths.alert_sink` + `paths.approval_gate` |
| secure-bash invoked with a shell string | gate is an argv prefix |

Finding codes change where templates take over (e.g. `STREAMBOX_HOST_DOWN` →
`HOST_DOWN` under the `navidrome` key, `HA_HOST_DOWN` → `HOST_DOWN` under
`home-assistant`). Remediation maps and realert overrides are keyed on the
new codes.

## What stays bespoke, and why

- **wifi-link** — iwlwifi microcode-crash signature + reset ladder: hardware-specific.
- **docker-macmini / nike** — remote probing through the Mac's forced-command
  SSH wrapper: site plumbing. (v2's blind-root suppression now covers their
  worst failure mode generically: a dead daemon no longer steps container
  subkeys healthy.)
- **zigbee-stack** — local rescue-power control plane in dependency order.
- **officedelta** — this home's energy doctrine (persistent-blind fail-safe
  charging, AC-out guard, the four HA-automation watchdog codes). The
  archetypes it proved (entity-threshold, sensor-blindness, automation
  watchdog) live in `check-ha-entity.sh` and the blueprint.
- **llm-chain** — reads the agent framework's transcripts and model config;
  inherently adapter-coupled.

## Adapter gaps this example exposes (phase 3 work)

All four are shipped. The example's absolute `.../workspace/bin/`
paths for the gate and the guard are the deployment's copies of the files
named below.

1. **`secure-bash-argv.sh` — shipped as `scripts/gates/secure-bash-argv.sh`.**
   The v1 secure-bash takes one shell STRING and `eval`s it; the v2 gate
   contract is an argv prefix. The gate validates `$1` (resolved to a full
   path, anchored to `$SELFHEAL_ROOT/remediations/` on *both* the auto and the
   human path) + optional `$2` (one `[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}`
   token), auto-passes the engine's allowlisted path with `exec bash "$@"`,
   and on the human path hands the wrapper named in mode-600
   `~/.cranston-gate-secure-bash.env` (`SECURE_BASH=...`) the one string
   `bash <fullpath> [arg]` — composed only from those validated parts, never
   eval'd here, audited `GATE-FORWARDED`. `GATE_CODE` passes through. The
   dangerous-detector bypass rule anchors on the gate file.
2. **`tailscale-running.sh` — shipped as `scripts/bin/tailscale-running.sh`**,
   the `ALT_GUARD_CMD`: exit 0 iff `tailscale status --json` reports
   `"BackendState": "Running"` (parsed as JSON, not grepped), 1 otherwise —
   including when tailscale is not installed.
3. **Delivery half of alerting — shipped as `scripts/bin/notify-alerts.sh` +
   `scripts/bin/send-telegram.sh`.** `alert_sink` still only queues into the
   pending file; the every-minute drainer takes the queue's lock, heads ONE
   message with the worst line present (🚨 > ⚠️ > 🔧 > ✅ > other), delivers
   through `SELFHEAL_NOTIFY_CMD` (default: the Telegram sender — Bot API,
   `TG_API_IP` DNS fallback, 4096-character chunking at line boundaries, exit
   non-zero unless every chunk got 2xx), removes exactly the delivered lines on
   success and keeps the queue on failure. Two things the install must get
   right: `SELFHEAL_ALERT_FILE` must be the same value for the engine's cron
   line (the writer) and the drainer's (the install puts it in
   `/etc/cranston.env`, sourced by every cron line), and the drainer keeps a
   sent-text window (`<file>.sent.json`, `SELFHEAL_SENT_WINDOW_MINUTES`,
   default 30) because queue-side dedupe cannot see what was already
   delivered. This is the delivery reference for other adapters.
4. **Consent routing — shipped as the announce channel.** A remediation
   entry (ask-first, or the auto-dict form) whose `consent` is `household`
   or `named:<person>` gets its `announce` text (or a generated fallback)
   queued through the alert sink with `SELFHEAL_ALERT_CHANNEL=announce` just
   before the fix runs — the engine announces auto fixes, `approve-heal.py`
   announces at approval time. `send-alert.sh` stores announce lines as
   tagged objects beside the plain page strings; `notify-alerts.sh` delivers
   them to `TG_ANNOUNCE_CHAT_ID` (a shared household chat) when set and folds
   them into the admin page when not, so nothing is silently dropped. An
   announce failure never blocks the fix.
