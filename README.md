# Cranston

**A self-healing smart home agent you can put on top of OpenClaw, Hermes, or
any agent framework that can send a message and gate a command.**

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
for what can't, and tells you about the rest once a day instead of thirty
times. You talk to it through Telegram or WhatsApp; it executes through a
2FA-gated, audited, allowlisted path.

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

```
engine/
  selfheal.py        the orchestrator: one cycle per run, from OS cron
  approve-heal.py    executes a human-approved pending fix, then verifies
  lib/check.sh       emit/param helpers for check templates
  lib/remediation.sh audit -> cap -> act -> verify -> result skeleton
bin/
  send-alert.sh      default alert sink: lock + merge + dedupe into a pending
                     file the adapter's delivery cron sends
  flush-digest.sh    cron fallback for the daily digest (the primary renderer
                     usually rides the agent, which can die silently)
checks/templates/    9 parameterized check archetypes (see below)
remediations/templates/  skeleton + 3 archetypes
config/              schema v2 (documented) + a minimal starter config
examples/cranston/   the reference deployment translated to v2 - worked
                     example only, never deployed
tests/               engine suite (43 assertions) + template smoke tests (36)
methodology/         pointer to the design blueprint (phase 3 fills this in)
```

## Design, in one paragraph each

**The engine runs even when the agent is dead.** One cycle per invocation
from system cron; the alert path never traverses an LLM. Per key:
anti-flap threshold → auto remediation (capped, cooled down, verified) or
ask-first proposal or watch-only alert → escalation when the cap is hit.
Recoveries hold their ✅ for ten minutes so a flapping service collapses to
one message with a flap count instead of thirty pages.

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
the line. The reference deployment's admin muted their channel at ~30
pages/day — 75% of which was one unthrottled nag. Never again.

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

## First deployment

What a first install looks like today, honestly: the **core is standalone-able**
(engine + templates + your own alert sender), while the polished OpenClaw
adapter (SKILL.md, installer, 2FA gate shim) is phase 3. These steps assume a
small always-on Linux box (a Pi, a NUC, an old laptop) that can reach the
things it should watch.

### 0. Prerequisites

- `python3` (3.9+, stdlib only), `bash`, `curl`, `cron`
- `dig` for the DNS template, `openssl` for the cert template (optional)
- A box that stays on. If it runs on battery-backed power, say so in its own
  config — `check-host-power.sh` exists precisely for the machine doing the
  watching.

### 1. Get the code and write your config

```bash
git clone git@github.com:jetsharklambo/cranston-skill.git
cd cranston-core
cp config/services.example.json services.json
```

Edit `services.json`: one service block per thing you care about, every
target in `params`. Start small — three services you actually feel when they
break beat twelve you don't. Field-by-field docs: `config/services.schema.md`.
A full 12-service real-world shape: `examples/cranston/services.json`.

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
*/2 * * * * flock -n /tmp/cranston-core.cronlock python3 /opt/cranston-core/engine/selfheal.py >> /var/log/cranston-core.log 2>&1
25 5 * * *  SELFHEAL_DIGEST_FILE=/opt/cranston-core/state/digest.jsonl SELFHEAL_NOTIFY_CMD=/opt/cranston-core/bin/my-sink.sh /opt/cranston-core/bin/flush-digest.sh
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

## Running the tests

```
python3 tests/test_engine.py     # 43 assertions, isolated temp install
bash tests/test_templates.sh     # 36 assertions, offline (local stub servers)
```

## Status / roadmap

- Phase 1 — design blueprint: done (separate document; published as part of phase 4)
- **Phase 2 — this repo: engine v2, template library, worked example, tests**
- Phase 3 — the OpenClaw adapter: SKILL.md, installer (cron lines, heartbeat
  snippet, detector rules), the argv gate shim, delivery scripts. The gaps
  are listed concretely in `examples/cranston/NOTES.md`.
- Phase 4 — onboarding methodology (Discover/Decide) + registry publication.
