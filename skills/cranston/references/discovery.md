# Discovery — probing and interviewing a home before you defend it

This is the Discover module: an agent onboarding a home runs it once, and
again after any physical change. Its output is a **home inventory draft**
(skeleton at the end) that the priority-of-needs interview and `services.json`
generation build on. Nothing here configures or fixes anything.

**The rule: every probe is READ-ONLY.** `ip neigh`, curl a health endpoint,
`ethtool` a link, list entities. Never toggle, never restart, never pair. A
question only answerable by actuating something goes under **Ask** — the
admin answers it or schedules a supervised drill later.

Each category: **Probe** (run these), **Ask** (only the admin knows),
**Record** (inventory fields), **Red flags** (checkable conditions that go
into the draft as standing findings — each one cost the reference deployment
an outage or a week of false alarms).

## D1. Network topology & addressing

**Probe**
- `ip neigh show` on every host, both directions per host pair — `FAILED`
  both ways = layer-2 blocking, not a dead host or firewall.
- `nmcli dev wifi list` / `wpa_cli status` — BSSID/band/channel per wireless
  host; two hosts on one BSSID is the precondition for client isolation.
- `nmcli dev status`, `networkctl list`, `/etc/netplan/` presence — which
  network manager each host *actually* runs (one can be installed and manage
  no device).
- `ip addr` + lease files vs static config — DHCP or static per host.
- `resolvectl status` / `/etc/resolv.conf` — which DNS each host really uses.
- `ethtool <iface>` on wired hosts — negotiated link speed.
- `arp -a` / `ip neigh` MAC harvest — the IP/MAC fingerprint inventory that
  seeds `../scripts/checks/templates/check-lan-inventory.sh`.

**Ask**: SSIDs/bands; is client isolation or a guest mode on? Who administers
the router, and will they set DHCP reservations for critical hosts? Does any
mesh satellite have its own WAN, or does one router outage kill every path out?

**Record**: hosts table (ip, mac, role, net-manager, dhcp/static, link, BSSID
if wireless), DNS per host, router admin, reservation status per critical IP.

**Red flags**
- ARP FAILED in both directions between two hosts on the same BSSID =
  wireless client isolation, not a dead host. This paged "service DOWN"
  hourly for eight days while the service was fine — only the monitor's
  layer-2 view was broken.
- Zero DHCP reservations = address drift is a matter of time (two drift
  incidents on the reference deployment). Pin statics on the hosts AND ask
  for reservations — a driver reset or cloud-init can drop the host-side pin.
- A gigabit NIC linked at 100Mbps (`ethtool`) = marginal cable (two outages).
- A network manager installed but managing no device = the config you edit is
  not the config in force.

## D2. Power dependency chains

**Probe**: little is automatic — this map is drawn WITH the admin, outlet by
outlet. Readable: smart-plug `power_on_behavior` / `power_outage_memory` via
the platform API or retained MQTT state; `pmset -g` / BIOS equivalents for
auto-restart-on-power; battery/UPS entities for health and level (feeds
`../scripts/checks/templates/check-ha-entity.sh` and
`../scripts/checks/templates/check-host-power.sh`).

**Ask**
- Who feeds whom, down to the outlet: wall → battery → hosts, strip outlet N
  → device. "It's plugged in somewhere" is not an answer.
- Hidden couplings: does any rescue actuator sit on the thing it rescues? The
  reference deployment's rescue outlet was also the Zigbee mesh's only relay
  hop — cycling it blinked the rescue path itself.
- Which hosts auto-boot on power restore, in what order? Boot timing sets
  `post_outage_grace_minutes` in `services.json`.
- Battery truths: real health %, runtime budget, kept-deliberately vs debt.
- Known side effects: on the reference deployment, switching the battery's AC
  *input* on cut its AC *output* in 2 of 9 observed cases.

**Record**: power-chain tree, per-host auto-boot behavior and boot time,
per-actuator power-on behavior, battery health/runtime, side effects.

**Red flags**
- An actuator whose `power_on_behavior` is `previous` goes dark if mains
  blips during the OFF half of a cycle. The invariant for anything in a
  rescue path is `on`.
- A host that does NOT auto-boot on restore (desktop OS at a login window)
  takes everything it hosts dark for hours while answering ping. Its
  services need a pre-login-aware check, not a liveness check.
- Any actuator that powers the device that controls it — name the loop.

## D3. Transport redundancy

**Probe**
- Per remote resource, enumerate LAN and overlay (e.g. Tailscale) paths and
  test BOTH (`../scripts/checks/templates/check-http.sh` semantics, one URL
  per transport).
- `tailscale status --json` — node state AND **key expiry dates**, every node.
- Compare overlay-name resolution vs direct-IP access; if MagicDNS is
  unhealthy, checks must pin IPs (doc-range example: `100.64.0.10`).
- VPN interference: with a consumer VPN up on a host, is the LAN /24 still
  reachable from it? One VPN claimed the whole /24 into its tunnel even with
  "allow LAN" on; only `tailscale serve` endpoints (terminated inside the
  overlay daemon) survived.
- Any certs in the path: wire
  `../scripts/checks/templates/check-cert-expiry.sh` now, not after the
  first silent expiry.

**Ask**: what is the out-of-band channel for whole-home-dark? (Reference
deployment: an off-site dead-man's switch that emails on a stale heartbeat,
plus the home platform pinging the monitor host and pushing to a phone.) Does
the alert sender have a DNS fallback (public resolver) for when local DNS *is*
the outage?

**Record**: per-resource transport table (LAN URL, overlay URL, primary),
overlay key expiry dates, out-of-band channel(s), alert-path DNS fallback.

**Red flags**
- An overlay node key with an expiry date is a time bomb in every fallback
  path — an expired key silently killed all of the reference deployment's
  fallbacks for a week. Disable expiry or alert ahead of it.
- A check using overlay hostnames on a box with unhealthy overlay DNS tests
  the DNS, not the service.
- No out-of-band channel = from outside, the agent's death and the home's
  death are indistinguishable.

## D4. Sensor data provenance & granularity

**Probe**: for every sensor a check or automation will depend on, read its
history and answer: **where does the number come from and how often does it
move?** Measure real step size and cadence; read `last_changed` to tell
"fresh zero" from "stale zero". This calibrates every
`../scripts/checks/templates/check-ha-entity.sh` threshold and every
`verify_delay_seconds` in `services.json`.

**Ask**: which sensors are cloud-borne vs local, and the vendor's polling
interval? Known liars — anything that reads 0 while working, or
`unavailable` while fine?

**Record**: sensors table (entity, source, cadence, step, quirks), per-sensor
staleness threshold.

**Red flags**
- Cloud data stepping 3–4% every ~10 min means: exact-match triggers
  (`to: "50"`) never fire; a 30-second post-action verify reads stale state
  and reports a false FAILED; and "blind" must be judged by the entity's own
  `last_changed` being old **persistently** (≥15 min) before anyone acts.
- An entity that reads 0 while the thing works (a solar power sensor on the
  reference deployment) — key the check on a sibling that moves (voltage).
- A device limit contradicting its settings (stops charging at 76% with
  max=100): thresholds above the real ceiling can never fire. Test
  thresholds against observed history.
- A Tier-1 safety sensor can go quietly `unavailable` (one was blind 72 h
  unnoticed). Critical sensors need a staleness check, not just a value check.
- Inverted semantics ("on" = closed). Record the truth table; never trust
  the name.
- Integration warm-up after platform restart: entities sit `unknown` for
  minutes — a check acting on a *young* blind state fights the warm-up.

## D5. Platform quirks

**Probe**: identify each host's OS and storage (`uname -a`, `sw_vers`,
`mount`), then copy the matching quirk list below into the draft. Host-class
specifics: `hardware.md` (sibling).

**Ask**: anything the admin has already been burned by on these boxes.

**Record**: per-host quirk list, visible to every later check/remediation
author.

**Red flags** (seeded quirk inventory)
- HA OS: journald is volatile — capture forensics *before* rebooting; no
  RTC — early-boot timestamps lie; API proxy routes can drift to 401 while
  service calls on the same token still work (test the call you'll use).
- macOS: login items vs the pre-login gap — login-item services are dark
  between power restore and login while `sshd` (a daemon) answers; no
  `timeout`/`flock`; bash 3.2; `launchctl setenv` doesn't survive reboot;
  TCC grants block scripted access that works interactively.
- ExFAT: inode counts are garbage; a dirty volume remounts read-only; Docker
  bind mounts into it go stale and `docker restart` cannot fix them.
- Linux: a wifi driver reset can rename interfaces and drop a DHCP lease;
  cloud-init regenerates network config over your static pin unless disabled.

## D6. Service inventory

**Probe**: per host, `systemctl list-units --type=service`, `docker ps -a`,
platform add-on lists — where each service runs and as what. Probe each
health endpoint (`../scripts/checks/templates/check-http.sh`) and verify it
is a **function check, not a liveness check**: does it prove the service does
its job, or only that a process exists? List unversioned scripts referenced
by cron/automations.

**Ask**: who owns each service; what does "working" mean to the household;
which config exists only on the box?

**Record**: services table (name, host, run-as, health endpoint, function vs
liveness, owner, versioned-where).

**Red flags**
- A "running" container with a dead bind mount — liveness green, function
  dead; this silently killed the reference deployment's backups for 5 days.
  Every check wired into `services.json` must exercise the function.
- Scripts that exist only on the box rot invisibly: mirror them into a repo
  during onboarding, or list them as debt.

## D7. Actuator inventory & blast radius

**Probe**: enumerate actuators (plugs, strips, relays) from the platform and
coordinator; read `power_on_behavior`, retained state, and mesh role (is it a
router others relay through?). List existing platform automations touching
actuators and their trigger types.

**Ask**
- Per actuator: what ELSE goes dark when it cycles? (Cycling the platform
  host's outlet on the reference deployment also cut household DNS for 3–4
  min — blast radius is the household's evening, not one box.)
- Rate caps (reference values: 1/24h on the platform host, 1/6h elsewhere).
- Devices excluded outright: a 3D printer mid-print, an alarm clock.
- Whose consent per actuator (`consent` on ask-first entries: admin, named
  person, household)?

**Record**: actuators table (device, feeds, blast radius, cap, power-on
behavior, consent, excluded), automation-fragility notes.

**Red flags**
- Two Zigbee coordinators open for joins = the wrong one steals the device.
  Close every other network's joins before pairing; a device on the wrong
  mesh must be removed there (a factory reset) before rejoining.
- A platform automation built on `wait_for_trigger` dies on restart and never
  resumes — one left a charge plug ON for 2.5 days. The engine should
  watchdog restart-fragile automations, not trust them.
- An auto remediation whose blast radius includes the channel the admin would
  use to approve or undo it: on-demand only — see `approval-gates.md`.

## D8. Security boundaries

**Probe**
- Find the existing gate inventory: a 2FA wrapper the host framework routes
  dangerous commands through, immutable config flags (`lsattr`), pattern
  detectors, audit logs. This feeds the Discover table in
  `approval-gates.md` (sibling) directly.
- Read any remote-control wrapper's verb surface: closed allowlist exec'd as
  argv, or string composition/`eval`? (An eval-based wrapper the reference
  deployment once ran amounted to effective host root.)
- Grep scripts for embedded credentials; read token expiry where possible.
- List which entities are exposed to voice/chat assistants.

**Ask**: what must NEVER be voice-exposed? Baseline: DNS protection, the
agent's own power feed, camera privacy, factory reset — "turn off the DNS
filter" from any houseguest must not kill the LAN. Where does the second
factor live, and can the agent's box read it? (If yes, it isn't a factor —
`approval-gates.md`, "the anti-pattern".)

**Record**: gate inventory, wrapper verb surface, credential-debt list (what,
where, expiry), voice-exposure denylist and current violations.

**Red flags**
- A plaintext long-lived token embedded in scripts (the reference deployment
  found a JWT valid for ten more years in three of them). Surface the debt in
  the draft; don't silently rotate it.
- A dangerous-command wrapper that builds shell strings from its input.
- A kill-the-monitor entity exposed to voice — found live once; unexposed the
  day it was noticed.

## D9. Agent/LLM substrate

The agent watching the home needs watching. **Probe**: read the model chain
config — every provider, model id, and *which API key each entry uses*; grep
recent gateway logs for failover errors per day; make a one-token live call
to each chain model; compare the agent's memory files' last-modified dates
with reality.

**Ask**: which providers are free-tier (retirement cadence ~monthly)? Budget
for a paid fallback? Who gets told when the chain degrades?

**Record**: model chain table (position, provider, model, key identity,
free/paid), failover-error baseline, memory freshness.

**Red flags**
- One API key shared across chain positions = the chain collapses as a unit —
  it did, twice, on the reference deployment: the primary 429'd and every
  "fallback" rode the same throttled key. Independence = providers AND keys.
- Error classes that don't trigger failover: an HTTP 410 (model retired) came
  back for 10 days while the gateway never failed over. A chain watchdog must
  call the models, not read the config.
- The agent's own memory rots: the reference deployment's agent reported on a
  DNS server five months after its retirement. Stale memory is a finding.

## The inventory draft

Discovery ends by writing this skeleton, filled, as `home-model.md` (plus a
machine-readable `home-model.json` mirroring the tables). The interview and
`services.json` generation consume it; the admin reviews it before anything
runs.

```markdown
# Home model — <home name> — drafted <date>

## Hosts
| ip | mac | role | net-manager | dhcp/static | link | notes |
| 192.168.1.47 | aa:bb:.. | monitor | netplan | static (pinned) | wifi 2.4GHz | |

## Power chains
wall → <battery> → { monitor-host, <host2> }
strip outlet 1 → wifi satellite      # who-feeds-whom, to the outlet
# hidden couplings: … | auto-boot on restore per host: yes/no + boot time

## Transports
| resource | LAN | overlay | key expiry | primary |
| mediabox | http://192.168.1.58:4533 | http://100.64.0.10:4533 | 2027-01-01 | LAN |
# out-of-band channel(s): …

## Sensors
| entity | source | cadence | step | quirks (inversion, lies-at-0, blind risk) |

## Services
| name | host | run-as | health endpoint | function-check? | versioned-where |

## Actuators
| device | feeds | blast radius | cap | power-on | consent | excluded |

## Security boundaries
gates: … | wrapper verb surface: … | credential debt: … | voice denylist: …

## Agent substrate
| chain pos | provider | model | key | free/paid |
# failover baseline: … | memory freshness: …

## Standing findings (red flags found)
- …
```

Every red flag found above lands in **Standing findings** with evidence — a
command and its output, not a hunch. That list is the first thing the Decide
phase triages.
