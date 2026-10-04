# Sandbox — try the skill on a fake home LAN

A throwaway test environment for the OpenClaw skill. Nothing here is part of
the packaged skill (it is in `.clawhubignore`), and nothing touches real
hosts: the "LAN" is loopback services plus a `ping` shim.

```bash
bash sandbox/up.sh --openclaw     # omit --openclaw to skip the OpenClaw CLI
source sandbox/.run/env.sh
bash sandbox/drill.sh --full      # ~70s; without --full ~5s
bash sandbox/down.sh              # stop services, delete sandbox/.run/
```

`up.sh` builds, under `sandbox/.run/` (gitignored):

| Piece | What it is |
|---|---|
| `venv/` | isolated python3 (the engine is stdlib-only) |
| `lan/` | `svcctl` + two fake services (`media-server` HTTP :18096, `nas` TCP :18445) and a `ping` shim whose hosts you `plug`/`unplug` |
| `openclaw/` | OpenClaw CLI in an isolated `HOME` (fetches a local Node 24 if the system Node is too old); the skill is installed with `openclaw skills install` from a staged copy of this checkout |
| `deploy/` | a deployment root made exactly as SKILL.md's Install procedure says — copied from the *installed* skill dir when `--openclaw` is used |

The sandbox config (`overlay/services.sandbox.json`) shortens timings
(verify delay 1s, recovery hold 1 min, no post-outage grace) so drills run
fast, and pages go to `deploy/state/outbox.log` instead of Telegram.

## Drills (`drill.sh`)

1. All healthy → no pages.
2. **Auto** class: `media-server` dies → absorbed once, restarted on cycle 2, 🔧 page, audit EXEC/RESULT.
3. **Ask-first** class: `nas` closes → 🚨 page with `Reply 'heal nas'` + household heads-up; engine waits; `approve-heal.py nas` fixes and verifies.
4. **Network gate**: router unplugged → remote services skipped as unknowable, no pages, no failure counted.
5. **Watch-only**: media host unplugged → `HOST_DOWN` page, no fix; recovery ✅ held, then released (`--full`).

## Poke it by hand

```bash
svcctl stop nas; python3 "$DEPLOY/engine/selfheal.py" --once   # twice to page
cat "$DEPLOY/state/outbox.log" "$DEPLOY/state/pending-approvals.json"
python3 "$DEPLOY/engine/approve-heal.py" nas
svcctl unplug 192.168.1.1        # router; `svcctl ls` shows everything
oc skills check                  # OpenClaw's readiness view (with --openclaw)
```

Not covered: a live OpenClaw agent conversation (`oc agent --message ...`)
needs a model provider configured in the sandbox's OpenClaw home.
