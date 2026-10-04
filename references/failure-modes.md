# Failure-mode library — twenty classes, probed before they bite

Eight months of operating the reference deployment produced twenty recurring
failure classes. Every one happened at least once in a real home, and most
happened silently — the system *looked* healthy while it wasn't. This library
ships them as discovery-time checks or scheduled audits, so a new deployment
probes for each class up front instead of waiting for its own copy of the
incident.

Use it two ways. During Discover/Decide, walk the list against the home model
— each "Probe or audit" entry is runnable or askable during onboarding.
During Defend, the classes with a shipped artifact are covered by a template
or engine feature; the ones marked "audit manually" are honest gaps — put
them on a calendar, not a wish list. A coverage table closes the file.

## F1. Silent model-chain collapse

**How it bites.** The agent's primary model started erroring on every call
and the chain degraded through fallbacks for three days with zero pages —
the errors that kill chains (HTTP 410 retirement, empty-content replies,
schema 400s, 413 token limits) often don't trigger failover; it happened
twice.

**Probe or audit.** Daily: count failover errors in the gateway's logs and
check the `model` field on recent assistant turns — a collapse shows up as
the wrong model answering, not as an error. Add a throttled 1-token probe
per provider, and require independent keys per chain entry (one shared key
fails as a unit).

**Addressed by.** Audit manually (nothing shipped yet) — deliberately in
part: the engine keeps the LLM entirely out of the alert path, so a collapsed
chain never silences monitoring; it silences the *agent*, and only a
transcript audit catches that.

## F2. Monitoring that lies

**How it bites.** A single-transport probe fired 59 false DOWN alerts over
eight days against a healthy service (wireless client isolation ate the one
path), and a typo'd endpoint reported its own 404 body as "unreachable"
hourly for six weeks.

**Probe or audit.** Every remote check gets a second transport (overlay IP,
never a name that depends on the DNS being probed) and a ping split; blame
the host only when both paths fail *and* our own overlay is up — otherwise
emit a degraded `*_BLIND`. Audit: grep your checks for single-path probes.

**Addressed by.** `../scripts/checks/templates/check-http.sh` (dual
transport + ping split + `*_BLIND`), plus the engine's degraded severity.

## F3. Swallowed signals

**How it bites.** A disabled check hid a dead radio stack for 24 days, and
anti-flap thresholds can absorb a chronic intermittent forever — both look
exactly like health.

**Probe or audit.** Make the engine report what it is *not* saying: flap
counts for keys bouncing below the page threshold, and how long each
`enabled: false` service has been off. Monthly: re-justify every disabled
service with the admin.

**Addressed by.** Engine flap collapse (pages carry "(flapped Nx since…)")
and the weekly page-count meta-line pattern. Disabled-service age: audit
manually (nothing shipped yet).

## F4. DHCP drift

**How it bites.** A Wi-Fi driver reset cost the monitor its own address (two
days of hourly false alerts from the new one), and a media host quietly
moved IPs, breaking every check pinned to the old address.

**Probe or audit.** Keep an IP/MAC fingerprint inventory of critical hosts;
a different MAC at an expected IP is its own finding, distinct from
host-down. Discovery items: router reservations (usually none exist) and
whether the agent may pin static addresses host-side as well.

**Addressed by.** `../scripts/checks/templates/check-lan-inventory.sh`.

## F5. Key/credential expiry

**How it bites.** An expired overlay node key silently killed every fallback
transport for a week — the primaries still worked, so nothing paged until
they *also* failed and the redundant layer turned out to be long dead.

**Probe or audit.** Collect expiry dates at discovery: overlay node keys,
TLS certs, API tokens, SSH host keys, provider balances. Anything with a
date gets a countdown check; anything without one gets a calendar entry.

**Addressed by.** `../scripts/checks/templates/check-cert-expiry.sh` for
certificates and keys; API balances and node keys: audit manually (nothing
shipped yet).

## F6. Stale bind mounts

**How it bites.** A container showed "running" for five days while its bind
mount pointed at a vanished volume — liveness green, function dead, and a
restart couldn't fix the stale mount.

**Probe or audit.** Check *function through the container*, never container
state: probe the endpoint the mount serves, or read an identity-marker file
through the containerized path. Inventory every bind mount whose source is
removable or network-attached.

**Addressed by.** `../scripts/checks/templates/check-http.sh` aimed at the
real function; `../scripts/checks/templates/check-disk-space.sh` (identity
markers) for the volume underneath.

## F7. Removable media missing at boot

**How it bites.** A host rebooted with its external drive unplugged and
everything stored there simply vanished — no error, just absence — until a
human remounted and restarted the service.

**Probe or audit.** For every external volume: presence, read-write state
(ExFAT remounts read-only when dirty), free space, and an identity-marker
file proving the *right* volume is mounted, not just any volume at the path.

**Addressed by.** `../scripts/checks/templates/check-disk-space.sh`.

## F8. Auto-start gaps

**How it bites.** A host auto-booted after a power cut but its critical
services were login items, not daemons — the box answered ping and looked
"up" while everything that mattered stayed dark for hours behind a login
window.

**Probe or audit.** Per host, audit how each service starts: system daemon
(pre-login), user login item (needs a session), or shell-set env (doesn't
survive reboot). Then prove it: one supervised reboot per host during
commissioning.

**Addressed by.** `../scripts/checks/templates/check-systemd-unit.sh` for
daemon-managed services; the desktop-OS pre-login window: audit manually
(nothing shipped yet).

## F9. USB device blips vs restart policies

**How it bites.** A radio dongle dropped off USB for 14 seconds; the
supervisor's single restart attempt landed inside the gap, gave up, and the
radio stack stayed dead for weeks behind F3's disabled check.

**Probe or audit.** For every service bound to a USB device: does the
restart policy retry *past* a re-enumeration window (always + backoff, not
one attempt)? Is the device opened by stable ID? Test by pulling the device
for 30 seconds.

**Addressed by.** Audit manually (nothing shipped yet).

## F10. Immutable-flag / config-lock drift

**How it bites.** Both directions happened: a config file's immutable
protection was found off months after an edit (never re-locked), and a lock
left *on* silently blocked a scheduler from applying changes.

**Probe or audit.** Weekly, assert every protected file's expected state
both ways — protection present where required, absent where it blocks
legitimate writers. "Is it locked?" alone misses half the class.

**Addressed by.** Audit manually (nothing shipped yet).

## F11. Automations that die on restart or exact-match

**How it bites.** A platform automation waited for a sensor to equal exactly
"50" — a cloud sensor stepping 3–4% skipped right over it — and its wait
died on platform restart, leaving a charging plug ON for 2.5 days.

**Probe or audit.** The platform acts; the agent catches the platform
failing to act. Watchdog every load-bearing automation: still enabled? fired
inside its should-have-fired window? actuator stuck in a state it should
have ended? Audit definitions for exact-match triggers on coarse sensors and
waits without a restart-surviving sweep.

**Addressed by.** `../scripts/checks/templates/check-ha-entity.sh` (the
platform-automation watchdog archetype, blind-persistence by
`last_changed`).

## F12. Fail-safe actions with hidden side effects

**How it bites.** The "safe" automatic action — switching a battery's AC
*input* ON — cut the same unit's AC *output* two times in nine,
power-cycling every host the battery fed.

**Probe or audit.** Verify the safe action's own blast radius empirically
before trusting it: run it under observation, watch everything downstream in
the power chain. Keep auto actions structurally one-directional, and make
verify delays respect the sensor's real granularity — a 30-second verify
against a 10-minute cloud sensor pages a false failure.

**Addressed by.** `../scripts/remediations/templates/` (the ON-only template
structurally cannot switch anything off; every template carries its own rate
cap) plus the engine's verify-after-heal delay.

## F13. Alert storms, then muting

**How it bites.** Over a whole season of the reference deployment, only
about one alert item in eight reported a real incident or a completed fix;
the two worst offenders were both hourly re-pages of an already-known
condition (one ran sixteen straight days), the channel averaged ~29
messages a day for six weeks, and the admin eventually muted it — which
would have muted a real security alert with the noise.

**Probe or audit.** Volume is a budget with a standing audit: pages/day by
status code, top offender's share, a weekly meta-line in the digest. Any
single code above ~30% of volume is a design bug in that detector.

**Addressed by.** Engine features: `realert_minutes_by_code` (chronic nags
daily, outages hourly), `ask_demote_after` (an unanswered ask stops paging
and routes to the digest while the pending survives), flap collapse, digest
routing, the weekly page-count meta-line pattern.

## F14. Agent memory rot / session pinning

**How it bites.** The agent confidently reported on a DNS server five months
after its retirement — the memory file was never refreshed and nothing
forced it.

**Probe or audit.** Push every infrastructure change into the agent's memory
in the same work session; quarterly, diff memory against `services.json` for
services one mentions and the other doesn't.

**Addressed by.** Audit manually (nothing shipped yet). The engine keeps the
LLM out of the monitoring path, so memory rot cannot corrupt findings — but
it corrupts the agent's *answers about* findings, and only a scheduled audit
catches that.

## F15. Agent overwriting its own control files

**How it bites.** The agent wiped its own event-handler control file during
a model-confusion episode, taking every scheduled behavior with it; recovery
came from a dated backup, not from the agent noticing.

**Probe or audit.** Control files are admin-owned: the agent proposes
snippets, never holds write access. Audit that permissions and framework
settings enforce this; keep dated backups; alert on checksum change outside
a deploy.

**Addressed by.** `approval-gates.md` (the gate layer in front of every
dangerous command, including edits to the machinery) plus the audit trail's
EXEC/RESULT/REFUSED lines. Checksum watching: audit manually (nothing
shipped yet).

## F16. Timeouts vs workload

**How it bites.** Raising a "top N items" knob from 5 to 10 made a parent's
300-second timeout structurally unreachable (the floor was N × 31.5 s); the
two values lived in different files and the first scheduled run after the
change died on time.

**Probe or audit.** Document every timeout's derivation next to the value it
depends on (`TIMEOUT=900  # top_n × 30s fetch × margin`); when a knob
changes, grep for timeouts citing it; audit any timeout guarding a loop with
a configurable iteration count.

**Addressed by.** Audit manually (nothing shipped yet) — an authoring
convention the shipped templates model by commenting every timing value.

## F17. Doc drift

**How it bites.** A script existed only on the box and rotted out of sync
with the repo claiming to be its source of truth; the eventual redeploy
overwrote a live fix nobody had committed.

**Probe or audit.** Before any deploy, md5-compare live files against repo
HEAD and stop on mismatch — the mismatch *is* the finding. Every deploy gets
a changelog entry; unversioned on-box scripts go on the discovery inventory
as debt.

**Addressed by.** Audit manually (nothing shipped yet) — the live==repo md5
check is a deploy-time convention, not an engine feature.

## F18. Physical degradation

**How it bites.** A gigabit host linked at 100 Mbps on a marginal cable
through two outages before anyone looked, and the monitor itself ran on a
battery pack quietly down to 19.7% health — both trending for months with
nobody trending them.

**Probe or audit.** Collect the physical numbers on a schedule and trend
them in the digest: wired link speeds (`ethtool`), battery health, and radio
link quality for every low-power mesh hop. A number only read during an
outage is a number nobody reads.

**Addressed by.** `../scripts/checks/templates/check-host-power.sh` (own
mains + battery via sysfs); link speeds and radio LQI: audit manually
(nothing shipped yet).

## F19. Long-poll sockets wedging

**How it bites.** After a network blip, a service's long-poll connection
wedged open — process alive, unit "active", nothing arriving for hours until
a restart; meanwhile a *single-sink* silence heuristic once restarted a
healthy service twice.

**Probe or audit.** Wedge detection requires *both* evidence sinks quiet —
the process's own log file (by mtime; a long-lived process keeps its
boot-day file open past midnight, never select by date) AND the system
journal — over a window longer than the longest legitimate silence.

**Addressed by.** `../scripts/checks/templates/check-systemd-unit.sh` (unit
+ port + dual-sink log-staleness wedge heuristic).

## F20. Remote-control trust gaps

**How it bites.** An early remote-command wrapper `eval`'d its input — one
metacharacter from effective root on the remote host — and an unconstrained
off-site heartbeat key would have been a full shell on the box that pages
when the house goes dark.

**Probe or audit.** Audit every remote-control path for forced-command SSH
keys locked to one action, constant argv arrays (no eval, ever), metachar
rejection, and a REJECT audit taxonomy — then *read* the REJECT lines on a
schedule; a refusal from an unexpected caller is an intrusion signal.

**Addressed by.** `../scripts/gates/` (exec-through argv, fail-closed,
GATE-AUTO-PASS / GATE-APPROVED / GATE-REFUSED lines) and the remediation
library's EXEC/RESULT/REFUSED audit convention. Scheduled REJECT review:
audit manually (nothing shipped yet).

## Coverage table

| # | Class | Shipped artifact |
|---|---|---|
| F1 | Silent model-chain collapse | manual |
| F2 | Monitoring that lies | `check-http.sh` (dual transport + `*_BLIND`) |
| F3 | Swallowed signals | engine flap counts + weekly meta-line; disabled-age manual |
| F4 | DHCP drift | `check-lan-inventory.sh` |
| F5 | Key/credential expiry | `check-cert-expiry.sh`; balances/node keys manual |
| F6 | Stale bind mounts | `check-http.sh` (function) + `check-disk-space.sh` (markers) |
| F7 | Removable media missing at boot | `check-disk-space.sh` |
| F8 | Auto-start gaps | `check-systemd-unit.sh`; pre-login window manual |
| F9 | USB blips vs restart policies | manual |
| F10 | Immutable-flag/config-lock drift | manual |
| F11 | Automations dying on restart/exact-match | `check-ha-entity.sh` (watchdog archetype) |
| F12 | Fail-safe actions with side effects | `remediations/templates/` (ON-only) + verify-after-heal |
| F13 | Alert storms then muting | `realert_minutes_by_code`, `ask_demote_after`, flap collapse, digest |
| F14 | Agent memory rot | manual |
| F15 | Agent overwriting control files | `approval-gates.md` + audit lines; checksum watch manual |
| F16 | Timeouts vs workload | manual (convention modeled in templates) |
| F17 | Doc drift | manual (live==repo md5 convention) |
| F18 | Physical degradation | `check-host-power.sh`; link/radio trending manual |
| F19 | Long-poll sockets wedging | `check-systemd-unit.sh` (dual-sink wedge) |
| F20 | Remote-control trust gaps | `gates/` templates + audit taxonomy; REJECT review manual |
