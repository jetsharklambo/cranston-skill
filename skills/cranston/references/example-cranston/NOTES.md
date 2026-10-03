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

1. **`secure-bash-argv.sh` does not exist yet.** The v1 secure-bash takes one
   shell STRING and `eval`s it; the v2 gate contract is an argv prefix. The
   OpenClaw adapter needs a thin wrapper that validates `$1` (script path
   anchored to the remediations dir) + optional `$2` (one sanitized arg) and
   execs them — no eval. The dangerous-detector bypass rule then anchors on
   the wrapper.
2. **`tailscale-running.sh` does not exist yet** — a 3-line guard
   (`tailscale status --json | grep '"BackendState": "Running"'`) referenced
   as `ALT_GUARD_CMD`.
3. **Delivery half of alerting** — `alert_sink` queues into the pending file;
   OpenClaw's `notify-alerts.sh` (every-minute cron, honest headers, DNS
   fallback, delete-on-2xx) is the delivery reference for other adapters.
4. **Consent routing** — the engine now carries `consent` on ask-first
   proposals ("This affects the household — give them a heads-up."); the
   adapter's alert composer and any household-announce channel build on it.
