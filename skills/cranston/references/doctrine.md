# House doctrine — template

The doctrine is the one-page contract between the agent and the home: every
values call from the priority-of-needs interview ([interview.md](interview.md)),
written down where a future session — or a future admin — can read it without
re-interviewing anyone. The config implements it; the doctrine explains it.

**The rule:** the agent DRAFTS this document from the interview answers; the
admin reviews it, corrects it, and owns it. After that, the agent never edits
it without being asked — a doctrine the agent can quietly rewrite is not a
contract. When reality drifts from the doctrine, the agent reports the drift
and proposes the edit; the admin makes the call.

Copy the template below, fill every placeholder, delete none of the sections —
an empty section with "none" in it is information; a missing section is a
question nobody asked.

---

```markdown
# House doctrine — <home name>
Drafted by <agent> on <date> from the priority-of-needs interview.
Reviewed and owned by <admin>. The agent does not edit this file unasked.

## Actor doctrine (U7)
<One sentence: who acts, who catches. Per domain if it varies.>

## Remediation class per service (U1, U2)
| Service | Class | Notes |
|---|---|---|
| <service> | auto / ask-first / watch-only / on-demand | <why> |
<On-demand = deliberately NOT wired to any finding code; runs only on an
explicit human request through the gate.>

## Drill policy (U3)
<Per actuator: drill freely / drill with explicit OK / never drill.
Who gets warned, and how long before.>

## Never-touch list (U2, U12)
<Actuators and settings the agent must never auto-propose against:
channel-severing devices, rescue actuators, the platform's hold on them.>

## Consent map (U10, U11)
<Who lives here. Per shared actuator: consent scope (admin-only /
named:<person> / household-announce). The voice/guest exposure set and the
never-expose list.>

## Alert budget (U4, U5)
<Pages/day ceiling. Immediate-vs-digest split. Known-broken nag cadence and
tone (degraded daily, never DOWN hourly).>

## Energy and charging policy (U6)
<Targets, sources, and the blind-sensor rule. Which direction is fail-safe.>

## Known-compromises register (U8, U9)
<Each deliberate compromise and each intentional "noise": what it is, why it
stays, and the trigger that WOULD warrant a page. Landmines for future
sessions — nothing here gets "fixed".>

## Retirement list (U13)
<Everything the agent must stop watching and never resurrect, with dates.>

## Standing hands-on list (U15, U14)
| Task | Owner | Open since | Nag cadence |
|---|---|---|---|
| <task> | admin | <date> | daily digest |
<Entries here may be months old. That is tolerated, not fixed.>

## Dark-house channel (U16)
<The off-site path that works when every in-house channel is dark: what it
is, where it terminates, how it is tested.>

## Paid-reliability stance (U17)
<Whether money is on the table, and the threshold. The agent proposes paid
fixes only inside this envelope.>
```

---

## Worked excerpt — the reference deployment

A filled section, so the expected altitude is unambiguous (quote the admin;
explain the why; name the escalation trigger):

```markdown
## Actor doctrine (U7)
"HA acts; the agent catches HA failing to act." Charging, failsafes and
guards live in HA automations; the agent's services are watchdogs that page
when an automation is off, misfired, or structurally unable to fire.

## Never-touch list (U2, U12)
Router and wifi: on-demand only, never auto-proposed — an ask to cycle the
router can never be approved during the outage it would fix. Rescue outlets:
agent-only, deliberately not integrated into HA.
```

## How the draft gets made

1. Finish discovery ([discovery.md](discovery.md)) and the interview
   ([interview.md](interview.md)); record answers verbatim.
2. Fill the template — every section, quoting the admin where the wording is
   itself the policy (actor doctrine, intentional noise).
3. Cross-check against the generated `services.json`
   ([services.schema.md](services.schema.md)): every ask-first entry's
   `consent` matches the consent map; every never-touch item is absent from
   all `remediations` maps; every retirement-list service is disabled or
   gone; chronic codes carry `realert_minutes_by_code` per the alert budget.
   A doctrine/config mismatch is a bug in one of them — say which.
4. Hand it to the admin with the gate recommendation from
   [approval-gates.md](approval-gates.md) attached. The admin's edits win.
5. File the reviewed copy with the deployment. From here on: report drift,
   propose edits, never apply them unasked.
