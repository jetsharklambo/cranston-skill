# sim/ — home-simulation test suite (soak layer)

Months of realistic traffic against the REAL engine, checks, remediations,
queue, drainer, flusher and approve-heal — with only two clocks simulated:
the engine module's `now()` (monkeypatched, the `tests/test_engine.py`
pattern) and the drainer's sent-window clock (the test-only `SELFHEAL_NOW`
env the drainer documents in its header). Everything else is the production
bash/python running as real subprocesses.

Two layers share this directory:

* **soak** (this layer): compressed clock, `sim/soak.py` + `sim/casa/` +
  `sim/scenarios/` + `sim/report.py`. The deployment is built by copying
  `authoring/scripts/` (up-casa.sh), i.e. the SKILL.md install shape.
* **cloud drill** (`sim/cloud/`, `sim/CLOUD-TEST.md`, built separately):
  real-time, installs with the generated `scripts/install.sh` instead, and
  runs against the SAME casa world and stub phone described here — the
  **interface contract** section below is its build target.

```
scenario.jsonl ──> soak.py ──(casa-ctl)──> sim world ($CASA_RUN)
                     │                        ├─ lan/ping shim + hosts/   (reused sandbox/lan/ping)
                     │                        ├─ dns mode files ──> bin/dig PATH stub
                     │                        ├─ disk-table.json ──> bin/df PATH stub
                     │                        ├─ certs/*.pem      (openssl, real clock)
                     │                        └─ loopback fake services (sandbox fake-service.py)
                     ├─ sh.SelfHeal(cfg).run()   one real engine cycle per 2-min grid slot
                     ├─ bin/notify-alerts.sh     real drainer, SELFHEAL_NOW=<sim minute>
                     ├─ bin/flush-digest.sh      real flusher at 05:25 sim-daily
                     ├─ engine/approve-heal.py   on admin_heal events
                     └─ stub_botapi.py           the family's "phone" (chat 1001 admin Dana,
                                                 chat 2001 household announce: Sam + Riley)
outputs: phone.jsonl, queued-ledger.jsonl, digest-ledger.jsonl, metrics.json,
         run.log, transcript.log, tg-requests.jsonl, final-* copies
gates:   report.py (G1–G10, exit 1 on breach; the Claude judge is advisory
         and reads --emit-judge-input, never an exit code)
```

Everything runtime lives under `sim/.run/` (gitignored).

## Quick start

```bash
python3 sim/soak.py --scenario sim/scenarios/family-home-90d.jsonl --out sim/.run/out
python3 sim/report.py --run sim/.run/out
python3 sim/soak.py --gen-seed 42 --days 45 --out sim/.run/out-seeded
python3 sim/report.py --run sim/.run/out-seeded
python3 sim/soak.py --selftest            # equivalence + determinism (slow: see below)
```

Runs must NOT overlap: the casa world uses fixed loopback ports (18324/18325
plex, 18446 nas) and one hosts dir.

## Interface contract (what sim/cloud/ builds against)

### `sim/casa/up-casa.sh [--run DIR] [--deploy DIR]`
Builds the world (default `sim/.run/casa`, deploy `$RUN/deploy`), starts all
services plugged/healthy, writes `$RUN/env.sh` exporting:
`CASA_RUN`, `SANDBOX_RUN` (= `$CASA_RUN`; the reused, unmodified
`sandbox/lan/ping` shim reads `$SANDBOX_RUN/lan/hosts/<ip>`), `DEPLOY`,
`PATH` (prepends `$CASA_RUN/bin` — dig/df stubs + casa-ctl — and
`$CASA_RUN/lan`), `SELFHEAL_GW_OVERRIDE=192.168.1.1`,
`SELFHEAL_ALERT_FILE=$DEPLOY/state/alert-pending.json`,
`TG_BOT_TOKEN` (fake), `TG_CHAT_ID=1001`, `TG_ANNOUNCE_CHAT_ID=2001`.
`TG_API` is the caller's job once the stub's port is known.

### `casa-ctl` (on PATH; needs `CASA_RUN`)
```
start|stop|status <svc>      pihole-dns | truenas-smb | plex | plex-primary | plex-alt
plug|unplug <ip>             hosts by casa.json ip (.1 router, .2 pihole-pi,
                             .3 truenas, .4 plex-mini); also stops/starts the
                             services hosted there
dns-mode <tgt> <mode>        resolver|upstream1|upstream2|all  x  up|down|wedged
disk-fill <vol> <pct>        tank | root  ->  scripted df Capacity%
cert-set <name> <days>       regenerates $CASA_RUN/certs/<name>.pem (real clock)
tg-mode ok|500               flips the stub Bot API answer
ls                           world at a glance
```
`wedged` is the restart-is-ineffective mode: `casa-ctl start pihole-dns`
exits 0 and `status` says running, but the dig stub keeps failing — the
engine's own verify catches it and escalates. `stop` for the dns kind writes
`down` (restartable).

### `sim/phone/stub_botapi.py --port P --log FILE --mode-file FILE`
One JSON line per POST: `{"path", "chat_id", "text", "status", "ts"}`
(`ts` = real receipt time, ISO UTC — the cloud drill keys deliveries on it; `status`
200/500 per the mode file — a harness must count only status-200 lines as
delivered; the sender retries failures and every retry is a request). Each
send-telegram 4096-unit chunk is its own request/line. GET / answers 200
(readiness probe).

### `sim/report.py`
`--run DIR` (a soak output dir; the drill writes the same file set),
`--scenario F` (default: the run's `scenario.jsonl` copy), `--mode soak|drill`
(drill runs G3/G5/G6/G8 — G4's ledger equation is a soak instrument; `sim/cloud/collect-run.py` builds the drill's run dir and scenario times may be 'DNN HH:MM', '+MM:SS' or ISO UTC), `--sent-window N` (G8's window; **the drill
must pass 4** — see the dedupe trap), `--emit-judge-input F` (deduped
delivered texts + `{chat, via, header, severity_class, incident_id}` for the
advisory judge).

### Scenario JSONL
First line may be meta: `{"meta":true, "name":..., "gates":"full|invariants"}`.
Events: `{"t":"DNN HH:MM", "ev":..., ..., "dur_min":N, "expect":...,
"within_min":N, "id":...}` — `dur_min` auto-emits the paired restore. `ev`
vocabulary: `svc_down/svc_up` (svc), `host_down/host_up` (host=ip),
`gw_down/gw_up`, `flap` (host, n, period_min; starts down, ends plugged),
`dns_wedge`, `dns_ok` (clears the wedge, resolver STAYS down until healed),
`dns_down`/`dns_up`, `wan_down/wan_up` (all three dns targets),
`disk_fill` (vol, pct), `cert_set` (name, days), `tg_mode` (mode),
`admin_heal` (key -> real approve-heal.py), `end` (may carry
`expect:"all-clear"` + `"except":[keys]`). `expect` ∈ `page` (admin page
naming the subject within `within_min`, default 15), `digest` (flushed
catch-up naming it ≤29 h), `notify` (either), `silent`, `announce`,
`heal-ok`, `all-clear`. `#` lines are comments.

## Clock semantics

* **Engine time** is the patched module clock; every timestamp the engine
  writes (state, digest entries, pending expiries, queue) is sim time. The
  sim epoch is 2027-01-04 (the FUTURE) because `approve-heal.py` checks
  pending-approval expiry against the REAL clock — a past epoch would expire
  every approval instantly.
* **Drainer time**: `notify-alerts.sh` gets `SELFHEAL_NOW=<sim minute>` per
  invocation, so its sent-text window prunes/dedupes on sim gaps, not on the
  seconds of wall time between drains. Unset (`--no-selfheal-now`) is the
  negative test: every legitimate hourly re-nag looks "within 30 min" to the
  real clock and gets dropped (G8 catches it; G4 still reconciles, because
  the drops are logged, not silent).
* **Cert time is real**: check-cert-expiry computes days-left against
  `datetime.now()` inside a child process the patch cannot reach, so the
  `cert_set` event regenerates the PEM with `openssl -days N` instead of
  moving time. Consequence: the human-readable notAfter date inside
  CERT_EXPIRING texts is the one wall-clock-dependent substring in a soak's
  output; the determinism selftest masks exactly that substring (regex
  `Mon DD HH:MM:SS YYYY GMT`) before comparing bytes.
* **Remediation rate caps are real-epoch**: `engine/lib/remediation.sh`
  counts its internal cap in `date +%s` seconds, so a 90-day soak compressed
  into a minute of wall time would trip a production 3-per-6h cap on
  compression alone. The casa remediation sets `CAP_MAX=99`; the ENGINE's own
  attempt caps run on sim time and stay fully exercised.

### Hot/cold scheduler (why skipping cycles is sound)

A cron-faithful 90-day run is 64,800 engine cycles of real subprocesses.
`soak.py` instead walks minute-by-minute while anything is **hot** and jumps
to the next **wake** otherwise.

Hot = the alert queue exists, a post-outage grace window (+4 min) is open, a
key is mid-absorption/healing (`consecutive_failures > 0` or status
`healing`), or any not-ok key's next timer is ≤4 min away. Every scenario
action and every cold jump also forces ~3 minutes of hot so the next 2-min
grid slot runs a cycle against the changed world.

Wake set = next scenario action, each not-ok key's `last_alert` + its
effective realert (per-code override respected) and, for `failing` keys,
`attempts[-1]` + cooldown, each flap hold's `pending_since` + hold, pending
TTL expiries, and the next 05:25 flush when the digest is non-empty.

Equivalence argument: a skipped cycle is one where every check would pass or
keep failing with no timer due. Such a cycle mutates only
`consecutive_failures` (compared solely against `fail_threshold`, already
met) and attempt-window pruning (timestamp math) — nothing admin-visible.
All state changes originate from scenario actions (which force hot) or
timers (all in the wake set), so the first grid cycle after a wake observes
exactly what the faithful run's cycle at that time would. Deliveries can
shift by one grid slot; `--selftest` asserts texts identical in order and
timestamps within 2 minutes, plus equal final state statuses. The drainer
runs at every visited minute where the queue file exists — the queue only
changes during cycles/heals, so minutes without it are provably no-ops
(`notify-alerts.sh` exits 0 on a missing queue).

Scheduler cost on the canonical scenario: 253 cycles instead of 64,800.

## The drill dedupe trap

The drill compresses realert to ~5 min, which is INSIDE the production
30-min sent-text window — the drainer would eat every legitimate renewal as
a "within-window repeat". The cloud drill must export
`SELFHEAL_SENT_WINDOW_MINUTES=4` (and pass `--sent-window 4` to report.py).
The soak doesn't need this: it keeps production cadences and drives the
window with SELFHEAL_NOW instead.

## Gates (report.py; calibrated numbers from real canonical runs)

| Gate | Rule (full set) | Canonical observed |
|------|-----------------|--------------------|
| G1 frequency | admin msgs/day mean ≤3.0, max ≤8 (invariants set: mean ≤4) | mean 0.58, max 4 |
| G2 baseline week | D01–07: ≤2 non-digest msgs, ≤1 digest/day, 0 in gw windows | 0 / 0 / 0 |
| G3 coverage | every `expect` satisfied (page ≤ within_min, digest ≤29 h, silent windows clean, heal-ok verified, end all-clear) | 19 expectations |
| G4 zero silent drops | queued = delivered + logged window-drops + still-queued; digest ledger = flushed bullets + still-pending | 16=16+0+0; 48=48+0 |
| G5 header severity | every page header's emoji == worst body line (🚨>⚠️>🔧>✅) | 15 pages clean |
| G6 announce | exactly one 2001 text per consent heal; none to 1001; no codes/alert-headers in announce text | 1/1 |
| G7 gate windows | zero admin msgs inside gw-down + grace windows (box-disk exempt) | 2 windows clean |
| G8 dedupe | no identical (chat,text) pair delivered < window apart (strict <, matching the drainer), AND ≥1 legitimate repeat ≥ window delivered | 0 close, 4 spaced |
| G9 ask discipline | per ask incident: ≤4 immediate pages then digest re-nags; pending alive at heal; heal verified | 3 pages, 6 nags |
| G10 sender outage | every text queued during a tg-500 window delivered exactly once ≤3 h post-recovery | 1 text, 1 delivery |

Canonical headline (90 d): 52 admin messages total — 15 pages, 36 digest
flushes, 1 household announce; mean 0.58/day, busiest day 4 (the dns-wedge
escalation day). Seeded (seed 42, 45 d): 62 admin msgs, mean 1.38/day,
max 6, 2 announces — all invariant gates pass.

G8 is strict-< by design: the drainer drops only `age < window`, so a
repeat at exactly the window boundary is legal and delivered (the canonical
D22 pair lands 32 min apart with the 30-min window).

## Gate negative-test checklist (each run once; recorded results)

| # | Perturbation | Expected | Recorded result |
|---|--------------|----------|-----------------|
| 1a | `--override-defaults '{"cooldown_minutes":10}' --sent-window 0` on a D22 slice | G8 fails (close repeats delivered) | **G8 FAIL** — identical "still broken" pages 12 min apart |
| 1b | same slice, default 30-min window (control) | G8 passes, G4 reconciles the drop | **PASS** — "6 queued = 5 delivered + 1 window-dropped" |
| 2 | `--no-selfheal-now` (drainer on the real clock) | G8 reverse direction fails | **G8 FAIL** — 0 spaced repeats delivered, 5 window-dropped (G4 still reconciles: drops are loud) |
| 3 | `TG_ANNOUNCE_CHAT_ID=` (unset) | G6 fails | **G6 FAIL** (0 announces, expected 1) + G3 misses the announce expectation |
| 4 | remove the D71 `admin_heal` line | G3 fails | **G3 FAIL** (end-state: truenas-smb awaiting_approval) + **G9 FAIL** ("never healed (pending left to rot)") |

Bonus finding while testing #4 with the D22 dns heal instead: removing THAT
heal is masked — D47's `wan_up` sets the resolver mode up again, so the
escalated key recovers "by coincidence" and the end state is clean; in
between, the escalated hourly re-nags land in the digest (ask-demotion), so
even G1 stays within budget. The messaging system absorbing an unanswered
escalation into one digest/day is the designed behavior; the NAS variant is
the one that proves G3/G9 can fail.

## Where engine/template reality forced the scenario or config to bend

Everything below is REAL engine behavior the sim asserts as-is (none of it
is simulated around); each item names the artifact that documents it.

1. **UPSTREAM_DOWN is not degraded.** The check-dns template emits it with
   no `severity`, so by default a WAN outage would PAGE as "pihole-dns is
   down". The casa config routes it with `notify_by_code: {"UPSTREAM_DOWN":
   "digest"}` (services.casa.json carries the note). The digest line still
   reads 🚨 — tone follows the template.
2. **A lingering flap counter demotes later auto-fix notices to the
   digest.** After D12's storm, plex's `flap.count` stays >0 forever (the ✅
   that would clear it is only released on an immediate-class recovery
   hold, and mid-storm recoveries never re-arm one), so D18's 🔧 success is
   "routine" and lands in the digest — the canonical scenario expects
   `digest` for plex-crash-2, deliberately.
3. **dns_ok ≠ resolver up.** The wedge heal story only works if the wedge
   clears while the resolver stays DOWN (otherwise the engine recovers on
   its own and pops the pending approval before the admin ever replies).
   The plan sketch's `dur 180m` with a 09:40 heal was unsatisfiable;
   canonical uses wedge 07:00 → dns_ok 09:30 (unwedge, still down) →
   admin_heal 09:40, and approve-heal's casa-restart is what brings it up.
4. **approve-heal's result is not phone-visible.** Its stdout goes to the
   agent to relay; only the consent announce rides the sink. The D22 heal
   therefore closes with no admin message at all (asserted via
   metrics.heals, expect `heal-ok`), and after a heal the engine emits no
   ✅ (approve-heal resets the key to ok itself).
5. **approve-heal used to strip `SELFHEAL_ALERT_FILE` from the announce
   sink's env** (its SELFHEAL_*/GATE_* scrub is the second factor's
   integrity), so with a non-default queue path the household announce
   landed in the global `/tmp/selfheal-alert-pending.json` that no drainer
   watches. This sim found the bug; it is **fixed upstream** (the queue path
   now passes the scrub — it is a data file, never an exec vector, and
   params still can't plant it; `tests/test_engine.py` group 35 pins the
   pass-through). The deploy's `paths.alert_sink` stays `bin/casa-sink.sh`,
   a wrapper that re-derives the queue from `SELFHEAL_STATE_DIR` as a belt
   against engines predating the fix.
6. **The crit cert page waits for the daily realert slot.** truenas-cert
   uses `realert_minutes_by_code: {CERT_EXPIRING: 1440}` so the warn phase
   nags daily; the same code gates the crit page, which therefore lands up
   to ~24 h after the crit crossing — in canonical ~74 min (`within_min:
   90` on cert-crit). A deployment wanting a faster warn→crit escalation
   would need distinct codes.
7. **truenas-smb's port is 18446, not the sandbox's 18445** — the
   orchestrator's `sandbox/.run` world may be alive on the same machine and
   its nas listener made a stopped casa NAS look healthy. Same reason
   truenas's ping identity is 127.0.0.1: check-tcp-port pings the SAME
   address it connects to, and connecting to a literal 192.168.1.x could
   reach a real device on the developer's LAN.
8. **The disk arc starts at 64% (not the sketch's 78%)** so +1%/day crosses
   the 85%-used threshold exactly at D62.
9. **Healthy services have no state records** — the engine creates a key's
   record lazily on its first finding, so "final state all ok" means
   "every key that ever had an incident ended ok".
10. **Chunked messages are separate Bot API requests.** A >4096-unit digest
    flush (seen when an unanswered escalation nags hourly for weeks)
    arrives as two stub log lines; soak.py classifies header-less
    continuations by the preceding request.

## Costs (measured on a macOS arm64 dev box; Linux spawns are ~3-5x cheaper)

One engine cycle costs ~0.17-0.25 s wall — almost entirely subprocess spawn
(6 checks; the two PATH stubs are pure bash to keep it down). Measured on
this box: canonical 90-day soak 253 cycles / 73 s; seeded 45-day 350 cycles
/ 96 s; both + reports ≈ 2 min 49 s sequential. The <2-min CI budget
therefore depends on Linux's cheaper spawns; the two runs cannot
parallelize on one machine (fixed loopback ports + one hosts dir).

`--selftest` runs, in order: (a1) equivalence on the dense 34 h scenario
(faithful 1,021 cycles / ~3 min vs scheduler 19 cycles / 8 s; 4 identical
deliveries - a 🔧 page, a HOST_DOWN tell-page, the held ✅, and a digest
flush), (a2) equivalence on the canonical 7-day quiet prefix (faithful
5,036 cycles / ~14 min vs scheduler 6 cycles; both silent, equal final
state) and (b) determinism (seed 7, 14 d twice -> byte-identical phone.jsonl
after cert-date masking; 14 deliveries). Full recorded run: all PASS,
~18 min wall here. `--eq-days 3` shortens (a2) for the developer loop.
