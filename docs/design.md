# Cranston — design

The published design document for the Cranston skill: a self-healing home
monitor distilled from eight months of operating a real deployment. Nothing
in here is speculative capability — every mechanism below generalizes
something the reference home does, and every rule was earned by an incident.

Sections 3, 4 and 7 of the original design (discovery taxonomy, interview,
failure-mode library) are superseded by runnable methodology modules:
[discovery](../references/discovery.md),
[interview](../references/interview.md),
[failure-modes](../references/failure-modes.md),
[doctrine](../references/doctrine.md),
[approval-gates](../references/approval-gates.md).
This document keeps what doesn't run: the vision, the shape, the policies,
and the roadmap.

## 1. Vision

Every self-hosted smart home eventually acquires a **self-imposed home
admin**: the person who wired up the home platform, the DNS box, the media
server, the battery backup — and who is now the single point of failure for
everyone who lives there. When the music cuts out or the internet blips, the
household doesn't file a ticket; they find the admin. The admin, in turn,
lives with a phone full of alerts they eventually mute.

**Cranston's job is to make the home admin a better person to share a home
with.** Not by adding more automation, but by:

- fixing what can be safely fixed without waking anyone,
- asking permission for what can't — from the *right* person,
- saying less, more honestly, and
- knowing the home well enough that its fixes don't become someone else's
  outage.

Each design north star was paid for by an incident in the reference home:

| Principle | The lesson behind it |
|---|---|
| **One root cause, one alert.** | A wedged Wi-Fi card once produced false alerts for four healthy hosts and a bogus power-cycle proposal. A dead gateway makes remote hosts *unknowable*, not failing — skip them. |
| **A muted channel is a failed monitor.** | The admin muted the alert channel under sustained noise; a real security alert would have been muted with it. Volume is a budget. |
| **Fail-safe direction.** | Automatic actions may only move a system toward its safe state (charge when in doubt; turn ON, never OFF). Blind-charging logic was safe *because* it was ON-only. |
| **The platform acts; the agent catches the platform failing to act.** | A native automation silently died on platform restart and left a charger ON for 2.5 days. The fix was a restart-proof automation *plus* a watchdog — not agent takeover. |
| **Prove the restore path before cutting power.** | Never turn something off without verified means to turn it back on: bridge online, device reachable, retained state present, restore trap armed, power-on behavior pinned. |
| **Consent is not approval.** | The rescue outlet for the home platform also fed the living-room TV and speakers. "May the agent act?" and "whose evening does this ruin?" are different questions with different answerers. |
| **Monitoring lies; design for it.** | Single-transport probes, log-silence heuristics, and verify windows shorter than a cloud sensor's granularity each produced multi-day false narratives. |
| **Silence is not success.** | A disabled check hid a dead radio stack for 24 days; the agent's model chain collapsed twice with zero pages. Absence of alerts proves nothing. |

## 2. Product shape

Cranston is a **skill on top of an agent harness** — OpenClaw is the
reference and supported harness; a Hermes package is generated from the same
source but is untested — with a three-phase, re-runnable lifecycle:

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│ DISCOVER │ ──> │  DECIDE  │ ──> │  DEFEND  │
└──────────┘     └──────────┘     └──────────┘
 probes +         home model +      engine runs:
 interview        monitor config    monitor, heal,
 (read-only)      + doctrine doc    digest, drill
                  (admin reviews)        │
      ^                                  │
      └───────── re-discover on drift ───┘
```

- **Discover** (agent-driven, read-only): probe the network, power chains,
  services, actuators, sensors, security boundaries; interview the admin for
  what no probe can answer. Output: a home model.
- **Decide** (agent proposes, admin disposes): draft `services.json` (the
  engine config) and `doctrine.md` (the per-home values in prose: drill
  policy, consent map, alert budget, refusal list). **Both are reviewed and
  approved by the admin before anything runs.** This gate is load-bearing —
  the reference home's config encodes seventeen distinct values calls that
  no probe could have answered.
- **Defend** (engine-driven): the engine runs from OS cron, independent of
  the agent. The agent is the *interface* — status questions, approvals,
  onboarding conversations — and is **never the alert path**. An agent that
  is down still gets its home monitored.

## 3. Engine, in summary

The engine is framework-agnostic: stdlib Python plus POSIX shell, zero
imports from any agent framework, **one cycle per invocation from OS cron**
(single-instance flock, atomic state writes, corrupt-state quarantine). Per
key, a small state machine: `ok → failing → healing → escalated |
awaiting_approval`, with:

- **Anti-flap**: N consecutive failures before acting; attempt history
  survives recovery so crash-loops still hit caps.
- **Caps at two layers**: engine-side attempts/window/cooldown *and* an
  in-script backstop (never trust the caller; exit 75 = refused by cap).
- **Verified heals**: act, wait a delay that respects the sensor's real
  granularity, re-run the check, and only then claim success.
- **Remediation classes**: auto (fail-safe direction only), ask-first,
  watch-only, on-demand (explicit human request only, never proposed).
- **Ask-first with TTL + consent**: the page *is* the approval prompt;
  pendings carry a TTL and are renewed before any realert interval could
  outlive them, and each ask-first entry carries a `consent` scope — whose
  evening the fix ruins is separate from whether the agent may act.
- **Distrust of its own view**: gateway unreachable → remote services are
  skipped, not failed; a root key's finding suppresses its subkeys (a dead
  container daemon must not read as five healthy containers); dual-transport
  probes emit degraded `*_BLIND` findings instead of blaming healthy hosts;
  a post-outage grace window covers boot ordering on mains restore.
- **A digest budget**: ask-first pages immediately; degraded and routine
  findings route to a daily digest with a cron fallback flusher; a failed
  digest write pages rather than dropping the line; recoveries hold their ✅
  so flap pairs collapse to one message with a count.

The full configuration contract — every field, default, and coupling rule —
is [../references/services.schema.md](../references/services.schema.md).

## 4. Humane alerting policy

The policy is not taste; it is what the reference deployment's full message
history grades out to. Of roughly 3,200 delivered messages over eight
months, **only about one alert item in eight reported a real incident or a
completed fix**. Roughly two-thirds were re-alerts of an already-known
condition or a broken probe path blaming a healthy service; the two worst
offenders were both "hourly re-page of a thing the admin already knew," one
of which ran for sixteen straight days. Ask-first pages were the sharpest
lesson: hundreds of permission pages across a handful of conditions produced
approvals in the low single-digit percent — an unanswered ask that re-pages
on a timer is indistinguishable from spam.

Defaults the skill ships:

- **Page immediately:** true DOWN of anything the household feels; every
  ask-first (the page is the approval prompt); escalations; the monitor's
  own power (the last message that can get out before the dead-man fires).
- **Digest, daily, at the home's chosen hour:** degraded conditions, routine
  auto-fix successes, recoveries of digest-class keys, chronic known-broken
  items.
- **Collapse:** flap pairs inside the hold window; same-cycle bursts merge
  into one message.
- **Honest headers:** the header derives from the worst line present. A
  recovery must never arrive under a CRITICAL banner — that is how real
  criticals get muted.
- **Chronic ≠ urgent:** a condition waiting on a human (a router setting, a
  key re-auth) nags daily at most, as DEGRADED.
- **Coupling rule:** ask-first realert interval < approval TTL, so a
  standing offer never silently lapses.
- **Per-audience routing:** the admin gets pages; a `household` consent
  scope can emit a human-readable pre-announcement ("restarting the media
  box, music will stop for ~2 minutes") to a shared channel. This is the
  concrete mechanism behind the mission statement.

And because noise was never a one-time bug — each new detector shipped new
noise until its budget was designed in — **every check template carries its
realert and severity defaults from the start**.

## 5. Safety model

Nine layers, generalized from the reference deployment's defense-in-depth:

1. **A technical gate for dangerous ops** (adapter-provided): 2FA with the
   secret on a *different box* than the agent, short sessions, and a pattern
   detector in front of every shell. (Gate selection, including when *not*
   to build one, is [../references/approval-gates.md](../references/approval-gates.md).)
2. **An anchored automation bypass:** the only 2FA-free path is a full match
   on `<remediation-dir>/<name>.sh` plus at most one sanitized argument,
   with the automation marker set by the engine and every use audited.
   Touching the machinery itself is gated.
3. **Closed-allowlist remote wrappers:** forced-command SSH keys, constant
   argv arrays, no eval, metachar rejection, a REJECT audit taxonomy. An
   off-site key may be allowed to do exactly one thing.
4. **Caps at two layers** plus per-actuator budgets and commissioned gates —
   a remediation refuses to act until the human confirms the thing is
   physically wired.
5. **Refusal rules:** never cut your own uplink; actions unapprovable during
   the outage they would fix are on-demand only; blind + ambiguous = do
   nothing (unknown ≠ off).
6. **Drill policy as configuration:** what may be live-tested, what needs an
   OK, what never — plus a stub harness so most testing needs no live
   hardware at all.
7. **Household physical overrides:** actuator hardware buttons stay
   unlocked; anyone in the house can override the robot at the wall.
8. **A voice-exposure allowlist** enforced at the platform: DNS protection,
   the agent's own power, camera privacy, and factory resets are never
   voice-controllable; dangerous intents need confirmation regardless of
   speaker.
9. **The agent cannot modify its own config, control files, or safety
   machinery** — immutability, gates, and the rule that installers and
   humans own those files.

## 6. Roadmap and status

| Phase | Scope | Status today |
|---|---|---|
| 1 — Blueprint | design review and iteration | **done** (this document is its published form) |
| 2 — Engine generalization | `params`/`paths`/pluggable edges, remediation library, parameterized templates, debt fixes, stub-harness test suite | **shipped** — engine, 9 check + 4 remediation templates, gates, tests, and the dual-platform packaging/build all live in this repo |
| 3 — OpenClaw adapter | SKILL.md, installer, detector snippet, end-to-end install on a clean machine | **half done** — the skill body and gate shims ship; the installer, the default alert-sender wiring, and the harness shims are open |
| 4 — Publication & methodology | registry packaging, risk disclosure, methodology split into runnable modules | **in progress** — approval-gates and failure-modes are shipped as interview-grade modules; discovery, interview, and doctrine follow |

The Hermes artifact is generated from the same source and is
**experimental**: OpenClaw is the only harness this has actually run behind.
A dogfood deployment on a second, non-reference machine is pending — the
reference deployment stays frozen; this package is written *from* it, never
deployed *to* it.

## 7. Open questions

1. **Dependency graph vs convention** — should layer isolation become
   declarative (`defer_to: home-assistant`) or stay a check-authoring
   convention? Declarative would fix the blind-check-steps-dependents-healthy
   gap generically.
2. **Non-HA platforms** — the platform-coupled archetypes (entity
   thresholds, automation watchdog, service calls) assume Home Assistant.
   Adapter per platform, or HA-only at first? Current lean: HA-only v1 with
   platform-neutral interface names.
3. **Multi-admin homes** — approvals from more than one person; consent
   routing when the admin is away.
4. **Interview UX** — one long onboarding conversation, or incremental (one
   values question per day in the digest)?
5. **Paid reliability floor** — should the skill refuse to run its
   LLM-dependent paths on free-tier model chains at all, given that silent
   chain collapse happened twice? (The engine's answer so far: keep the LLM
   out of every path that matters. The open question is whether the agent
   half deserves the same rigor.)
