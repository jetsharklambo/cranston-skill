# Cranston

**A self-healing smart home agent for OpenClaw — the reference and supported
harness. The core is framework-agnostic by design (anything that can send a
message and gate a command could carry it), and a Hermes package is generated
from the same source, but OpenClaw is the only harness it has actually run
behind; everything else is untested.**

You built the smart home. The DNS box, the media server, the battery backup,
the automations. Which makes you the house IT department — and your household
didn't apply for help-desk tickets. Things break while you're out, and the
people you live with can't fix them, and you come home to a dark HA dashboard
and a cold look.

Cranston sits with you and learns your home: scans for devices, asks what
that thing is, what it's connected to, how you configured it and what you
wanted it to do in the first place. It builds a map — network, power chains,
blast radii, who-feeds-whom — and then it stands watch: fixes what can be
fixed safely without waking anyone, asks permission (from the *right* person)
for what can't, and tells you about the rest once a day instead of running a
commentary channel. You talk to it through Telegram or WhatsApp; it executes
through a 2FA-gated, audited, allowlisted path.

The goal isn't more automation. It's making the self-imposed home admin a
better person to share a home with.

This repo is the **framework-agnostic core** (phase 2 of the design
blueprint — a separate document, not yet published): the monitoring
engine, the check/remediation template library, and a worked example
translated from the reference deployment — eight months of a real home's
incidents distilled into defaults. The reference deployment ("cranston" the
instance, on OpenClaw) stays frozen; this package is written *from* it, never
deployed *to* it.

## Layout

**One canonical source, two generated installation views.** Everything under
`authoring/` is hand-edited; `tools/build.py` materializes the root skill
artifact (OpenClaw installs from the repo root) and `skills/cranston/` (Hermes
taps discover skills there). CI fails if the generated views drift from the
source, so the two platforms can never silently diverge.

```
authoring/              THE hand-edited source
  manifest.yaml         identity + dependency manifest (renders both frontmatters)
  SKILL.body.md         shared skill instructions ({{SKILL_DIR}} token, per-target render)
  adapters/             one short preamble per platform
  scripts/
    engine/selfheal.py    the orchestrator: one cycle per run, from OS cron
    engine/approve-heal.py executes a human-approved pending fix, then verifies
    engine/lib/           emit/param + audit->cap->act->verify skeletons
    bin/                  default queueing alert sink + daily-digest flusher
    checks/templates/     9 parameterized check archetypes (see below)
    remediations/templates/ skeleton + 3 archetypes
  references/           schema v2 docs, hardware guide, worked example, methodology
  assets/               minimal starter config
SKILL.md + scripts/ + references/ + assets/   GENERATED (OpenClaw root artifact)
skills/cranston/                              GENERATED (Hermes tap artifact)
tools/                  build.py + check_drift.py + validate_links.py
tests/                  engine suite (56 assertions) + template smoke tests (36)
                        + build tooling tests (22)
docs/                   the dual-platform repository spec + design notes
```

## Design, in one paragraph each

**The engine runs even when the agent is dead.** One cycle per invocation
from system cron; the alert path never traverses an LLM. Per key:
anti-flap threshold → auto remediation (capped, cooled down, verified) or
ask-first proposal or watch-only alert → escalation when the cap is hit.
Recoveries hold their ✅ for ten minutes so a flapping service collapses to
one message with a flap count instead of a page per bounce.

**Monitoring lies, so the engine distrusts itself.** When the default gateway
is unreachable, remote services are *skipped* — unknowable, not failing.
When a service's root key has a finding, its subkeys are skipped too (a dead
Docker daemon must not read as five healthy containers). Dual-transport
probes blame a host only when both paths fail *and* our own overlay is up;
otherwise the finding is a degraded `*_BLIND` that points at our side.

**Notification volume is a budget.** Ask-first findings page immediately
(the page is the approval prompt, and each re-page renews it before the
approval TTL lapses). Degraded and routine findings go to a daily digest with
a cron fallback flusher. A failed digest write pages rather than dropping
the line. This isn't taste — it's what eight months of the reference
deployment's full message history grades out to: of ~3,200 delivered
messages, **only about one alert item in eight reported a real incident or a
completed fix**. Roughly two-thirds were re-alerts of an already-known
condition or a broken probe path blaming a healthy service; the two worst
offenders were both "hourly re-page of a thing the admin already knew," one
of which ran for sixteen straight days. Ask-first pages were the sharpest
lesson: hundreds of permission pages across a handful of conditions produced
approvals in the low single-digit percent — an unanswered ask that re-pages
on a timer is indistinguishable from spam. And noise wasn't a one-time bug:
each new detector shipped new noise until its realert and severity budget
was designed in from the start, which is why every template here has one.

**Safety is structural, not prose.** Remediations are fixed-content scripts
taking at most one sanitized argument, run as argv through a configurable
approval gate (no shell strings, no eval). Every remediation keeps its own
rate cap and audit trail even though the engine has one too. Auto-class
remediations act in the fail-safe direction only — the ON-only template
structurally cannot switch anything off. Ask-first entries carry a `consent`
scope: whose evening the fix ruins is a separate question from whether the
agent may act.

## Check template library

| Template | Archetype |
|---|---|
| `check-http.sh` | HTTP endpoint: optional auth, optional second transport, ping split, degraded blind findings |
| `check-dns.sh` | resolver with upstream isolation (a WAN outage is never the resolver's fault) |
| `check-systemd-unit.sh` | unit + port + dual-sink log-staleness wedge heuristic |
| `check-tcp-port.sh` | plain TCP reachability with ping split |
| `check-disk-space.sh` | volume presence / identity markers / read-only / free % |
| `check-cert-expiry.sh` | certificate countdown (file or live endpoint) |
| `check-host-power.sh` | own mains + internal battery via sysfs |
| `check-ha-entity.sh` | Home Assistant entity thresholds with blind-persistence by `last_changed` |
| `check-lan-inventory.sh` | IP/MAC fingerprint watch (DHCP drift vs host down) |

All targets and thresholds come from the service's `params` block — see
`config/services.schema.md` for the contract and `examples/cranston/` for a
12-service real-world configuration.

## Prerequisites: the always-on box

Cranston needs a device that is **always on and lives on your home LAN** —
it must still be running when the internet is down, because that's the
moment it exists for. A cloud VPS cannot do this job (it can't tell
"internet down" from "house down", and it disappears from your house
exactly when the WAN does). Software needs are tiny: `python3` 3.9+,
`bash`, `curl`, `cron` (plus `dig`/`openssl` for two optional templates;
the systemd/sysfs templates are Linux-only).

Two posture rules, both paid for in the reference home's incident log:
**wire it with Ethernet** (the monitor must not share a failure mode with
the Wi-Fi it watches), and **make it survive power blips** (internal
battery, UPS, or BIOS power-on-after-AC-loss — with the router and modem
on the UPS too, or the survivor is blind).

| Tier | Runs | Needs |
|---|---|---|
| A | the core engine (this repo) | ~anything POSIX, 512MB RAM |
| B | A + an agent framework (OpenClaw etc.) | ~2GB RAM, Node.js |
| C | B + a small local LLM fallback | 16GB+ RAM |

Early-2026 picks (the RAM shortage made a built Pi 5 cost the same as an
N100 mini, so the smart money is on reused hardware — fitting, for this
project):

| Budget | Pick |
|---|---|
| **Free** | An old laptop: wired Ethernet, charge limit 60–80%, lid-ignore. Built-in UPS and recovery screen. Tiers A–B, C with 16GB. The reference deployment is exactly this. |
| **~$15** | Pi Zero 2 W + USB Ethernet: tier A watchdog only. |
| **~$150** | Used 1-litre business PC (ThinkCentre Tiny / OptiPlex Micro / EliteDesk Mini), 8–13W idle + a ~$65 UPS. The homelab-consensus value pick. |
| **~$500** | Mac mini M4 16GB (LaunchDaemons, not login items) or a Ryzen mini with 32GB — the only tier-C-at-usable-speed options. |

**Would an old Android phone work?** Under Termux: yes for the engine, with
real caveats — Android's phantom-process killer and battery managers fight
always-on work and lose *silently*, so a stock-Android phone should never be
the only monitor (set `defaults.gateway_ip`; Android blocks the route-table
read the network gate otherwise uses). A postmarketOS-flashed phone is the
trustworthy version. Full story, recipes, and the battery-swelling warnings:
**[docs/hardware.md](docs/hardware.md)**.

**Don't run it on:** a cloud VPS, a gaming desktop (idle watts), a VM on
your daily-use machine, or the router/NAS it's supposed to watch.

## First deployment

What a first install looks like today, honestly: the **core is standalone-able**
(engine + templates + your own alert sender), while the polished OpenClaw
adapter (SKILL.md, installer, 2FA gate shim) is phase 3. These steps assume a
small always-on Linux box (a Pi, a NUC, an old laptop) that can reach the
things it should watch.

### 0. Prerequisites

See **Prerequisites: the always-on box** above (and `docs/hardware.md` for
the full device guide). If the box runs on battery-backed power, give it a
`check-host-power.sh` service — that template exists precisely for the
machine doing the watching.

### 1. Get the code and write your config

```bash
git clone https://github.com/jetsharklambo/cranston-skill.git
mkdir -p /opt/cranston
cp -R cranston-skill/scripts/. /opt/cranston/
cp cranston-skill/assets/services.example.json /opt/cranston/services.json
cd /opt/cranston
```

(The deployment is a *copy* of the generated `scripts/` tree — the install
root is the directory that contains `engine/`. Any writable always-on
location works; `/opt/cranston` is just the example.)

Edit `services.json`: one service block per thing you care about, every
target in `params`. Start small — three services you actually feel when they
break beat twelve you don't. Field-by-field docs:
`references/services.schema.md`. A full 12-service real-world shape:
`references/example-cranston/services.json`.

### 2. Wire the alert path (the part that makes it yours)

The engine calls `paths.alert_sink` with alert lines as arguments. The
default sink only queues into a pending file — **something must deliver it**.
Quickest standalone wiring: point `alert_sink` straight at a sender script.
Telegram example (create a bot with @BotFather, get your chat id):

```bash
cat > bin/my-sink.sh <<'EOF'
#!/bin/bash
# direct Telegram delivery; called with alert lines as args
TEXT=$(printf '%s\n' "$@")
curl -s --max-time 15 "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
     --data-urlencode "chat_id=${TG_CHAT_ID}" --data-urlencode "text=${TEXT}" >/dev/null
EOF
chmod +x bin/my-sink.sh
```

Set `"alert_sink": "bin/my-sink.sh"` and put the two env vars in the cron
line (step 4). Keep the token in a mode-600 file the cron line sources, not
in the config.

### 3. Dry-run one cycle

```bash
python3 engine/selfheal.py --once
cat state/state.json
```

First run should log quietly and write `state/`. Force a finding to see the
whole path (anti-flap means TWO consecutive bad cycles before anything
pages): break a target on purpose, run two cycles, watch the alert arrive,
fix it, run cycles until the held ✅ releases (~10 min of health).

### 4. Put it on cron

```cron
*/2 * * * * flock -n /tmp/cranston.cronlock python3 /opt/cranston/engine/selfheal.py >> /var/log/cranston.log 2>&1
25 5 * * *  SELFHEAL_DIGEST_FILE=/opt/cranston/state/digest.jsonl SELFHEAL_NOTIFY_CMD=/opt/cranston/bin/my-sink.sh /opt/cranston/bin/flush-digest.sh
```

System cron, never an agent's scheduler — the whole point is that monitoring
survives the agent dying. (On a box without `flock`, drop it; the engine
holds its own fcntl lock.)

### 5. Approvals

When a page says `Reply 'heal <key>'`, the pending fix sits in
`state/pending-approvals.json` for `pending_ttl_hours`. Execute it with:

```bash
python3 engine/approve-heal.py <key>
```

Standalone, that's you SSHing in; with an agent adapter, the agent runs it
on your chat reply, behind the deployment's 2FA gate (`paths.approval_gate`).
Until a gate is configured, leave nothing in `remediations` you wouldn't
want run on a plain shell.

### 6. Know what you just promised your household

Before wiring any remediation that cuts power or restarts something shared:
walk the blast radius (what else is on that outlet?), verify the restore
path (power-on behavior, retained state), set the `consent` field, and cap
it. The reference home's rules of thumb are in the blueprint's safety
section — every one of them was paid for.

## Installing as an agent skill

Both packages are generated from one source (`authoring/`) and committed, so
installs need no build step. The skill teaches an agent to install, operate,
and approve fixes for the engine — the engine itself still runs from system
cron, never from the agent.

### OpenClaw

```bash
openclaw skills install git:jetsharklambo/cranston-skill@main
```

OpenClaw installs the repository root (`SKILL.md` + `scripts/` +
`references/` + `assets/`). Pin a tag instead of `@main` when
reproducibility matters; Git-sourced installs are refreshed by reinstalling.

### Hermes (experimental)

```bash
hermes skills tap add jetsharklambo/cranston-skill
hermes skills install jetsharklambo/cranston-skill/cranston
```

Hermes discovers the tap artifact at `skills/cranston/`. It is generated from
the same source as the OpenClaw package and follows the documented tap
conventions, but it has never been installed on a real Hermes deployment —
expect rough edges, and reports are welcome. No Hermes-specific features will
be added until one exists.

### Dependencies and secrets

Hard: `python3` (3.9+), `bash`, `curl`. Soft: `dig` (DNS template),
`openssl` (cert template), and `TG_BOT_TOKEN`/`TG_CHAT_ID` — needed only by
the example Telegram sink; any `alert_sink` command of your own works
without them. Never put secret values in `services.json`; keep them in a
mode-600 file the cron line sources.

## Running the tests

```
python3 tests/test_engine.py     # 56 assertions, isolated temp install
bash tests/test_templates.sh     # 36 assertions, offline (local stub servers)
python3 tests/test_build.py      # 22 assertions, build/drift/link tooling
```

## Status / roadmap

- Phase 1 — design blueprint: done (separate document; published as part of phase 4)
- **Phase 2 — this repo: engine v2, template library, worked example, tests**
- **Phase 3 (in progress) — dual-harness packaging: done (one authoring
  source generates the OpenClaw root artifact and the Hermes tap artifact,
  drift-gated in CI). Still open: the OpenClaw runtime shims — the argv gate
  wrapper, the Tailscale guard, delivery scripts, consent routing — listed
  concretely in `references/example-cranston/NOTES.md`.**
- Phase 4 — onboarding methodology (Discover/Decide) + registry publication.
