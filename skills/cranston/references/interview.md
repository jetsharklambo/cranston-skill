# Priority-of-needs interview — the 17 questions

Discovery ([discovery.md](discovery.md)) tells you what the home *is*. This
interview tells you what the admin *values* — seventeen questions no probe can
answer, because each is a judgment call: whose evening a fix may ruin, how much
paging a human tolerates before muting you, which broken things are broken on
purpose. Run it AFTER discovery, so every question can name the real services
and actuators you found. Every answer maps to concrete config (vocabulary:
[services.schema.md](services.schema.md)) or to a line in the house doctrine
([doctrine.md](doctrine.md)).

Two delivery styles — the admin picks:

- **One onboarding conversation.** All 17 in a sitting, ~30 minutes. Best when
  the deployment is new and nothing is paging yet.
- **One question per daily digest.** Append the next unanswered question to
  the evening summary until the set is done. Best when the system is already
  live — answers arrive grounded in that day's actual alerts.

Record answers verbatim before translating. The doctrine quotes the admin;
the config implements them. Each section below ends with the reference
deployment's answer as the worked example.

## U1. Act, ask, or tell

**Why it matters:** both failure directions are real — an auto-fix that cuts
power at the wrong moment, and an ask-first page nobody answers for hours.
**Ask:** "For each service I found: when it breaks, do I fix it silently, ask
you first, or just tell you?"
**Record:** a three-column list over the discovered service inventory.
**Maps to:** each service's `remediations` map — auto = script path string,
ask-first = `{"ask": "...", "arg": "...", "consent": "..."}`, tell-only =
`null` (watch-only).
*Reference deployment:* auto for the gateway, 2FA/sync containers, DNS add-on,
Zigbee stack, battery charge-ON; ask for HA reboot/cycle, media-box cycle,
Docker Desktop restart; tell-only for inventory, drive health, host power.

## U2. Fixes that cut the approval channel

**Why it matters:** an ask-first router cycle can never be approved during the
outage it would fix — router down means no chat, so the ask dies unheard.
**Ask:** "Which fixes, if I ran or even proposed them, would cut off the
channel you'd use to answer me?"
**Record:** the channel-severing actuators (router, the agent's own uplink,
the box the chat bot rides on).
**Maps to:** the fourth remediation class — OMISSION. Leave these out of every
`remediations` map; the scripts run only on an explicit human request through
the adapter's gate. Also the doctrine never-touch list.
*Reference deployment:* router/wifi cycling is on-demand only, never
auto-proposed.

## U3. Drill policy

**Why it matters:** an undrilled remediation is a guess — but one drill
against the wrong outlet blinked a whole infrastructure strip, rescue path
included.
**Ask:** "What may I test-cycle to prove the fixes work, when, and who needs
warning first?"
**Record:** per actuator — drill freely / with an explicit OK / never.
**Maps to:** the doctrine drill-policy section (the engine has no drill knob;
this is policy the agent obeys), backstopped by each remediation script's own
rate cap (exit 75).
*Reference deployment:* drills on the media box only; HA or TV outlets only
with an explicit OK; never network gear.

## U4. The alert budget

**Why it matters:** past ~30 pages/day the admin silently muted the channel —
after which every alert was delivered to nobody, worse than any digest.
**Ask:** "How many pages a day before you mute me? What news can wait for the
evening digest?"
**Record:** the daily page budget and the immediate-vs-digest split.
**Maps to:** `realert_minutes`, `ask_demote_after` (unanswered asks route to
the digest after N pages), `notify_by_code` (`"digest"`),
`recovery_hold_minutes` (flap collapse) — and the rule that every ask-first
code's realert interval stays shorter than `pending_ttl_hours` so each re-page
renews the approval.
*Reference deployment:* muted at ~30/day; settled on ask-first re-pages every
5h, degraded findings to the 9pm digest, flaps collapsed, honest headers.

## U5. Known-broken reminder cadence

**Why it matters:** a chronic condition paged hourly as 🚨 DOWN — 59 identical
alerts in a week for a service that was fine — trains the admin to ignore the
DOWN headline itself.
**Ask:** "When something is broken and waiting on YOU — a router setting, a
cable — how often do I remind you, and in what tone?"
**Record:** nag interval and the severity wording the admin will tolerate.
**Maps to:** `realert_minutes_by_code` (chronic codes at 1440) and
`"severity": "degraded"` on the finding — ⚠️ DEGRADED headline, digest routing
by default.
*Reference deployment:* daily ⚠️ DEGRADED, never hourly 🚨 DOWN.

## U6. Energy and charging policy

**Why it matters:** battery policy is a values call — the reference deployment
chose fail-safe charging after a blind sensor nearly ran the agent's own power
source flat.
**Ask:** "What's the battery/charging policy? Who charges from grid, who from
solar, and what do I do when the sensors go blind?"
**Record:** target levels, source preferences, the blind-sensor rule.
**Maps to:** the charging service's `params` block (thresholds, entity names,
windows — never inside templates) and its auto remediation, which must act in
the fail-safe direction only (charge-ON, never charge-OFF).
*Reference deployment:* grid charges to 75%, solar owns 75→100; charge when in
doubt.

## U7. Who acts, who catches

**Why it matters:** two actors on one problem is how an outage got
double-remediated — the platform's automation fired, then the agent's
redundant fix paged a false FAILED.
**Ask:** "Is the home platform the primary actor with me as watchdog — or am I
the one who acts?"
**Record:** one sentence of actor doctrine, per domain if it varies.
**Maps to:** where remediations live. Platform-acts ⇒ the agent's service is a
watchdog: check-only, findings like AUTOMATION_DEGRADED, remediation `null`.
Agent-acts ⇒ auto/ask classes per U1. Also the doctrine's opening line.
*Reference deployment:* "HA acts; the agent catches HA failing to act."

## U8. Known hardware compromises

**Why it matters:** a probe sees a battery at 19.7% health and wants to page
forever; only the admin knows it stays deliberately as a last-mile UPS.
**Ask:** "What hardware compromises are you knowingly running — things that
look alarming but stay by decision?"
**Record:** each compromise, the reason, and what change WOULD warrant a page.
**Maps to:** the doctrine known-compromises register; config-wise either no
check, or one thresholded at the escalation trigger, not at "looks bad".
*Reference deployment:* a 19.7%-health laptop pack kept as last-mile UPS,
pending a swelling check.

## U9. Intentional noise

**Why it matters:** the morning digest carried 19 overdue tasks every day; an
eager agent "fixing" that ritual would have destroyed a feature.
**Ask:** "Which noisy-looking or broken-looking things are intentional and
must be left alone?"
**Record:** the list, verbatim — landmines for future sessions.
**Maps to:** the doctrine known-compromises register (same section as U8);
never wire a check or cleanup against anything on it.
*Reference deployment:* the 19-overdue-task morning digest is a ritual. Leave
it.

## U10. Household consent

**Why it matters:** the admin's OK does not cover the living room — a TV
outlet cycle mid-movie is a household incident even when technically correct.
**Ask:** "Who else lives here, and whose consent covers which devices? Which
fixes ruin someone else's evening?"
**Record:** household members and a consent scope per shared actuator.
**Maps to:** the `consent` field on ask-first entries (`"admin"` default,
`"named:<person>"`, `"household"`) and `consent_notes` on the service; the
adapter's alert composer words the ask accordingly.
*Reference deployment:* TV/Sonos blips and the HA/DNS reboot need an OK — the
admin's alone doesn't cover the living room.

## U11. Voice and guest exposure

**Why it matters:** "turn off the DNS protection" spoken by any houseguest
must not kill the LAN — exactly that switch was found voice-exposed.
**Ask:** "What may anyone in the house control by voice or open UI? What must
never be exposed that way?"
**Record:** the curated exposure set and the explicit never-expose list (DNS,
the agent's own power, camera privacy, factory reset).
**Maps to:** platform exposure settings (adapter territory, not engine config)
and the doctrine consent map, next to the never-touch list.
*Reference deployment:* ~77 curated entities; climate yes; alarm clock, TV
modes, printer outlets declined; safety sensors read-only.

## U12. Rescue actuators and the platform

**Why it matters:** the rescue outlet exists to power-cycle the platform when
the platform is dead — if the platform can also drive it, one bad automation
saws off the branch both sit on.
**Ask:** "May the home platform touch the rescue actuators, or are those
agent-only?"
**Record:** yes/no per rescue actuator.
**Maps to:** integration boundaries (keep rescue devices out of the platform's
registry) and the doctrine never-touch list.
*Reference deployment:* no — rescue outlets are agent-only, deliberately not
integrated into HA.

## U13. Retirement list

**Why it matters:** the agent reported on a retired DNS server for five months
because nobody told the monitoring it was gone.
**Ask:** "What's legacy I should stop watching — retired services, dead hosts,
disabled automations?"
**Record:** everything retired, with dates if known.
**Maps to:** `enabled: false` or removal of the service entry, plus the
doctrine retirement list so future sessions don't resurrect them.
*Reference deployment:* old DNS stack retired; old models removed; job
automations disabled.

## U14. Network-config authority

**Why it matters:** a DHCP lease drift moved a monitored host and produced two
days of hourly false alarms; the durable fix needed both a host-side static
pin and a router reservation — only the admin can say which the agent may do.
**Ask:** "May I change device network config myself (static IPs on hosts), or
only recommend router changes for you to make?"
**Record:** the authority split — agent-may-do vs recommend-only.
**Maps to:** whether an IP-pin remediation is auto/ask/omitted; the
router-side half goes on the standing hands-on list (U15).
*Reference deployment:* both — static pin on the host AND a router
reservation (the latter done by the admin).

## U15. The standing hands-on list

**Why it matters:** some router tasks sat pending for months. That is normal.
Nag hourly and you get muted (U4); forget them and the fix is lost.
**Ask:** "Which hands-on tasks will you actually do, and roughly when? Which
should I keep on a list and re-surface gently?"
**Record:** each task, its age, the admin's honest intent.
**Maps to:** the doctrine standing hands-on list (with ages), plus
`realert_minutes_by_code` daily nags for the findings those tasks would
clear. The skill must tolerate months-old entries gracefully.
*Reference deployment:* a standing list (router isolation, DHCP reservations,
a cable swap, a key-expiry setting) — some pending for months.

## U16. The dark-house channel

**Why it matters:** when the whole house is dark, every in-house alert path is
dark with it — the page that matters most cannot travel the usual way.
**Ask:** "Where do you hear about it when the whole house is dark — power out,
internet out, or me dead?"
**Record:** the off-site channel (external box + email, platform phone push —
anything outside the blast radius).
**Maps to:** a dead-man's heartbeat to an off-site host (adapter/script
territory), `local: true` on services that must keep running with the LAN
down, `post_outage_grace_minutes` on the recovery side, `paths.alert_sink`
for the normal path it complements.
*Reference deployment:* off-site email via an external server's dead-man
check, plus phone push from the home platform watching the agent back.

## U17. Paying for reliability

**Why it matters:** the model chain silently collapsed twice on free tiers —
free-tier retirements and shared keys fail as a unit. Whether money is an
acceptable fix is the admin's call, not the agent's.
**Ask:** "Is paying for reliability on the table — paid API tiers, a UPS, a
second ISP — and at what rough threshold?"
**Record:** the stance and the threshold ("after the second incident", "under
$X/month", "never").
**Maps to:** the doctrine paid-reliability stance; the agent proposes paid
fixes only inside the stated envelope.
*Reference deployment:* eventually yes — unblocked a paid-tier console after
the second silent collapse.

## The household dimension

U10, U11 and U16 are first-class, not footnotes. A self-healing home is not a
server rack: the blast radius of a fix includes people, and the people are
not all the admin.

- **Consent scopes on actuators.** Every ask-first remediation carries
  `consent`: `"admin"` (default — only the admin's evening at stake),
  `"named:<person>"` (one specific person must OK it — their device, their
  room), or `"household"` (announce before acting — everyone feels it).
  Deliberately separate from the approval class: WHETHER the agent may act is
  the gate's question ([approval-gates.md](approval-gates.md)); WHOSE OK the
  act needs is a consent question. The engine stores and forwards the field;
  the adapter's alert composer words the ask.
- **Per-audience routing.** The admin gets pages; the household gets
  announcements. "The internet will blip for 2 minutes" to a shared channel
  is a different message, on a different channel, from the technical page
  that triggered it. Wire it in the adapter next to `paths.alert_sink`.
- **The dark-house channel is a household promise.** U16's off-site path
  makes every other promise honest: without it, "I'll tell you" quietly means
  "I'll tell you unless it's the big one."

## Siblings

- [discovery.md](discovery.md) — run FIRST; the probes that give this
  interview its nouns.
- [approval-gates.md](approval-gates.md) — the Decide module for U1's
  ask-first class: which second factor, if any.
- [doctrine.md](doctrine.md) — the template the answers fill; the admin
  reviews and owns it.
- [services.schema.md](services.schema.md) — the exact config vocabulary the
  "Maps to" lines use.
