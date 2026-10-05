# Cloud end-to-end test — runbook

A real-time, real-cron drill of the cranston skill on a throwaway cloud box:
install with the shipped installer, run the casa fake home for ~75 minutes of
scripted trouble, interview as the admin persona, judge every delivered
message. Nothing here touches the repository; everything a run produces lives
under `$DRILL_HOME` (default `~/cranston-drill`).

## Prerequisites

- A **Claude Code cloud session** on `github.com/jetsharklambo/cranston-skill`
  (ubuntu container). The session is the operator AND, in phase 4, the admin.
- `python3`, `bash`, `openssl` on the box (stock ubuntu image has them).
- **~2 hours** of wall time: the traffic phase alone is 75 real minutes plus
  up to 16 more waiting for the */15 digest flush.
- The sibling layers must exist in the checkout: `sim/casa/`, `sim/phone/`,
  `sim/report.py` (the drill exits 65 naming whichever is missing).

## The session prompt (copy-paste, verbatim)

```
You are testing the cranston skill end-to-end on this machine. Follow
sim/CLOUD-TEST.md §Phases exactly, running sim/cloud/cloud-drill.sh phase by
phase. In phase 4 YOU are 'Dana' (sim/cloud/persona-admin.md) — answer
interview.py's questions one device at a time in Dana's own words; never edit
services.json by hand. In phase 5 judge every unique delivered message
against sim/judge/rubric.md per sim/judge/judge.md. Do not modify repository
files; everything you produce goes under sim/.run/ or your report. End your
session with the report in §Report format.
```

(When the session wants artifacts under the repo, set
`DRILL_HOME=$PWD/sim/.run` before phase 1 — `sim/.run/` is gitignored run
state, never committed.)

## §Phases

Run each as `bash sim/cloud/cloud-drill.sh <phase>`; every phase prints a
PHASE banner and PASS/FAIL assertion lines, and appends them to
`$DRILL_HOME/assertions.log`.

### 1. `install` (~3 min)

Starts `sim/phone/stub_botapi.py` (the fake Telegram Bot API, backgrounded,
pid recorded), pre-writes the mode-600 env file with the fake token, chat ids
1001/2001, `TG_API=http://127.0.0.1:<port>`, `SELFHEAL_SENT_WINDOW_MINUTES=4`,
the alert file under the deploy's `state/`, and a guarded source of casa's
`env.sh` (cron starts from an empty environment — this file is the only
carrier). Then runs the real installer:
`scripts/install.sh --deploy ... --env-file ... --test-page [--apply-cron]`.

Asserts: six code dirs, services.json seeded, a hand-edited services.json
survives a re-run, env file kept at mode 600, the stub logged
"cranston test page", and the `# cranston-skill begin/end` marker block holds
exactly 3 cron lines.

**What can go wrong:**
- **No sudo** → `/var/log/cranston.log` is unwritable; the drill rewrites the
  cron lines' log path to `$DRILL_HOME/cranston.log` and says so. Report which
  path ran.
- **No usable cron** (probe: `command -v crontab && service cron status`,
  with a `pgrep cron|crond` fallback) → the drill skips `--apply-cron`,
  writes the printed lines into `$DRILL_HOME/cron-plan.txt` as the marker
  block, and later phases run them through `sim/cloud/cron-emulator.sh`.
  SAY SO in the report.

### 2. `casa` (~2 min)

Runs `sim/casa/up-casa.sh --deploy <same deploy> --run <casa run dir>`,
installs the casa services.json into the deploy, then compresses the engine
defaults in place for a 75-minute clock: `realert_minutes 5`,
`recovery_hold_minutes 2`, `post_outage_grace_minutes 2`, `pending_ttl_hours
1`, `verify_delay_seconds 2`, `ask_demote_after 3`. Edits the digest cron
line inside the marker block to `*/15` (engine stays `*/2`), and clears
`state/` so traffic starts from silence.

### 3. `traffic` (~75–91 min)

Starts the cron emulator if phase 1 chose it, then drives
`sim/scenarios/cloud-75min.jsonl` with real sleeps: 10 min baseline silence →
plex auto-fix → truenas-smb unattended ask (renewals, then ask-demotion) →
the admin's `heal` (announce to 2001, verified fix) → a 6-min gateway outage
(silence + grace) → a tank disk-fill into the digest. After the end event it
waits up to 16 minutes for the `⚠️ Daily catch-up` flush, asserts the exact
shipped strings, then runs
`sim/report.py --phone ... --scenario ... --mode drill` (the invariant gates)
and `--emit-judge-input $DRILL_HOME/judge-input.jsonl`.

**What can go wrong:** container clock skew/suspend stretches sleeps — the
driver schedules each event against the drill's own start time, so events
stay ordered, but cron cadences ride the wall clock: if the box pauses, page
timings drift and the timing-sensitive expects (renewal spacing, demotion
point) may need a human eye on the driver log before calling a FAIL real.

### 4. `interview` + the Dana session (~15 min)

`cloud-drill.sh interview` snapshots the config, strips exactly the persona's
open decisions (truenas-smb consent + consent_notes, truenas-disk nag fields,
all `_interview` provenance), and prints the hand-off instructions. **You**
then drive `onboard/interview.py` (`plan` → `next` → `answer` per device →
`fill --apply` → `doctrine --out ...`) as Dana, in her words, per
`sim/cloud/persona-admin.md`. Never edit the JSON. Then run
`cloud-drill.sh interview-verify`: it runs `sim/cloud/interview-check.py`
against the filled config and forces one failure (`casa-ctl stop
truenas-smb`, ~3 engine cycles) to prove the interviewed config actually
pages the ask with the household consent line.

### 5. judge (~10 min)

No script — you. Score every unique delivered message in
`$DRILL_HOME/judge-input.jsonl` (fallback: `phone.jsonl`) against
`sim/judge/rubric.md`, following `sim/judge/judge.md`, and write
`judge-report.md`. Advisory only; it gates nothing.

### 6. `wrap` (~1 min)

Kills the stub and emulator pids, prints the collected assertion ledger and
artifact paths as the report skeleton. If the real crontab was used, it
prints the one-liner that removes the marker block.

## §Report format

End the session with exactly these sections:

1. **Install table** — the phase-1 PASS/FAIL lines, plus: sudo available?
   which cron path ran (crontab or emulator)? which log path?
2. **Interview table + doctrine excerpt** — interview-check.py's PASS/FAIL
   lines, the per-service answered/open summary from `interview.py status`,
   and the consent-map + drill-policy sections of the doctrine draft, quoted.
3. **Phone metrics** — total messages per chat (1001 vs 2001), messages per
   hour over the traffic window, and the extrapolation to messages/day at
   REAL cadences (the drill compressed realert 60→5 and the digest to */15:
   divide the renewal traffic accordingly and say you did).
4. **Gate table** — sim/report.py's drill-mode verdict per gate
   (header-vs-worst, announce routing, dedupe, silent drops, drill-scaled
   frequency), breach quotes verbatim.
5. **Judge summary** — the per-message table, the 5 worst with one-line
   fixes, the A–F grade, the 3 systemic observations.
6. **Artifact paths** — everything under `$DRILL_HOME` worth keeping
   (phone.jsonl, driver.out, report.out, judge-input.jsonl, judge-report.md,
   doctrine-draft.md, assertions.log, the deploy's state/).
7. **Which cron path ran**, and every honest-failure note the drill printed
   (no sudo, emulator, clock skew) — verbatim.
