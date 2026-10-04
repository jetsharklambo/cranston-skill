# services.json — schema v2

One file defines a deployment. The engine (`engine/selfheal.py`) reads it every
cycle; `engine/approve-heal.py` reads the same file (override with
`SELFHEAL_CONFIG`). Default location: `<install-root>/services.json`.
All relative paths resolve against the install root (the directory containing
`engine/`).

```jsonc
{
  "version": 2,

  "paths": {                        // all optional; defaults shown
    "state_dir":   "state",         // state.json, pending-approvals.json, .lock
    "audit_log":   "state/audit.log",   // EXEC/RESULT/REFUSED lines (remediation lib + approve-heal)
    "digest_file": "state/digest.jsonl",// daily-digest sink; lock is <file>.lock (fcntl)
    "alert_sink":  "bin/send-alert.sh", // string (one file; .sh/.py get an interpreter)
                                        // or argv list. Called with alert lines as args.
    "approval_gate": null           // argv PREFIX for running remediations (contract,
                                    // interview + templates: approval-gates.md), e.g.
                                    // ["bash", "/path/adapter-gate.sh"]. The engine runs
                                    // gate + [script] (+ [arg]) — argv only, never a shell
                                    // string. null/[] = run directly with bash.
  },

  "defaults": {                     // per-service overridable (same key on the service)
    "fail_threshold": 2,            // consecutive bad cycles before acting (anti-flap)
    "check_timeout_seconds": 45,
    "cooldown_minutes": 30,         // between auto-remediation attempts
    "max_attempts": 2,              // auto attempts per attempt_window_hours, then escalate
    "attempt_window_hours": 6,
    "verify_delay_seconds": 45,     // wait before the post-heal re-check — must respect the
                                    // DATA SOURCE's refresh cadence (cloud entities: minutes)
    "realert_minutes": 60,          // quiet window between repeat alerts for a key
    "recovery_hold_minutes": 10,    // hold a ✅ this long; re-failure inside = flap, suppressed
    "pending_ttl_hours": 6,         // ask-first approval lifetime. RULE: any ask-first code's
                                    // realert interval must be SHORTER than this, so each
                                    // re-page renews the approval before it lapses
    "ask_demote_after": 3,          // immediate pages an unanswered ask gets before further
                                    // re-pages route to the daily digest (pending keeps
                                    // renewing; recovery or a new code resets the count).
                                    // The reference deployment measured 439 ask pages ->
                                    // 15 approvals before this existed. 0 disables
    "post_outage_grace_minutes": 6, // after the local network returns, skip remote services
                                    // while the other hosts finish booting
    "gateway_ip": null              // optional: the router IP for the network gate. Unset,
                                    // the engine reads `ip route show default`. SET THIS on
                                    // platforms where that read is restricted (Android/Termux:
                                    // `ip` exists but gets netlink permission-denied). A failed
                                    // route read fails OPEN; only a clean read with no default
                                    // route counts as network-down.
  },

  "services": [
    {
      "name": "my-service",         // the root finding key; subkeys are "my-service/<sub>"
      "enabled": true,
      "local": false,               // true = keeps running while the local network is down
                                    // (that's when local actuators and own-NIC checks matter)
      "check": "checks/templates/check-http.sh",   // exit 0 healthy; exit 1 + one JSON
                                    // finding per line {key,status,layer,detail,severity?}
      "params": {                   // exported as ENVIRONMENT to this service's check,
        "CHECK_KEY": "my-service",  // remediations and hooks. Every probe target, threshold
        "HTTP_URL": "http://192.168.1.10:8080/health"   // and device name lives here —
      },                            // never inside a template. SELFHEAL_*, GATE_* and PATH-like
                                    // names (PATH, LD_*, PYTHON*, HOME, ...) are RESERVED: the
                                    // engine refuses the whole service with CHECK_ERROR.
      "remediations": {             // finding code -> remediation class:
        "SERVICE_DOWN": "remediations/templates/restart-systemd-unit.sh",  // AUTO (string)
        "API_ERROR": { "auto": "remediations/templates/restart-systemd-unit.sh",
                       "consent": "household",                              // AUTO (dict):
                       "announce": "restarting the media box — music will stop ~2 min" },
                                    // same as the string form, plus a consent scope and an
                                    // optional pre-announcement sent just before the fix runs
        "HOST_DOWN": { "ask": "remediations/my-outlet.sh", "arg": "svcoutlet",
                       "consent": "household",
                       "announce": "power-cycling the streambox outlet" },   // ASK-FIRST
        "LAN_UNREACHABLE": null     // WATCH-ONLY (alert, no fix proposed)
        // a fourth class exists by OMISSION: on-demand remediations are not
        // wired to any code — the agent may run them only on an explicit
        // human request through the adapter's gate
      },
      "consent_notes": "optional free text: whose evening does acting on this ruin?",
      "realert_minutes_by_code": {  // chronic-but-known codes nag daily; real outages hourly
        "LAN_UNREACHABLE": 1440
      },
      "notify_by_code": {           // routing override: "digest" | "immediate".
        "SERVICE_DOWN": "digest"    // Without an entry: ask-first ⇒ immediate;
      },                            // degraded ⇒ digest; everything else ⇒ immediate.
      "on_fail_forensics": "hooks/capture.sh",    // optional, read-only, run once at the
      "on_recover_forensics": "hooks/recover.sh", // first confirmed failure / at recovery
      "check_timeout_seconds": 55,  // any defaults key may be overridden per service
      "recovery_hold_minutes": 10
    }
  ]
}
```

## Semantics the schema can't express (engine behavior)

- **Blind-root suppression.** When a cycle's findings include the service's
  ROOT key, subkeys without findings are skipped as unknowable — not stepped
  healthy, no phantom recoveries. Subkeys with findings still step.
- **Network gate.** When the default gateway is unreachable, non-`local`
  services are skipped entirely (unknowable ≠ failing), plus the post-outage
  grace window on restore.
- **Flap collapse.** Recoveries hold their ✅ for `recovery_hold_minutes`; a
  re-failure inside the window suppresses the pair and bumps a counter later
  pages carry as "(flapped Nx since HH:MMZ)". Mid-flap, fresh DOWN
  transitions and cap-reached alerts are realert-gated and 🔧 successes go to
  the digest.
- **Digest discipline.** A failed digest write pages instead of dropping the
  line. The digest file is rendered by the adapter's daily summary and by
  `bin/flush-digest.sh` as the cron fallback — both under `<digest>.lock`.
- **`consent` routes pre-announcements.** `"admin"` (default),
  `"named:<person>"`, or `"household"` — whose evening the fix ruins is
  separate from whether the agent may act. On a dict entry (ask-first or
  auto-dict) whose consent is not `admin`, the engine queues a plain-language
  pre-announcement (the entry's `announce` text, or a generated fallback)
  through the alert sink with `SELFHEAL_ALERT_CHANNEL=announce` just before
  the fix runs — at approval time for ask-first (`approve-heal.py`), at
  remediation time for auto. `bin/notify-alerts.sh` delivers announce lines
  to `TG_ANNOUNCE_CHAT_ID` when set, otherwise folds them into the admin
  page; an announce failure never blocks or fails the fix.
- **Cycles stretch by `verify_delay_seconds` per remediation attempted**
  (the engine sleeps inline before re-checking each heal). This is by
  design — bound it with `max_attempts`/`cooldown_minutes`; the cron
  `flock -n` makes an overrun skip the next cycle rather than overlap.
- **`first_failed_at`** (a `state.json` record field) is stamped when a key's
  current incident began; it survives re-pages (which move `last_transition`),
  is cleared on recovery, and is what the interview's retire question reads to
  ask about a device that has been failing for a week.
- **Per-service error boundary.** A service the engine itself cannot process
  (a remediation that cannot be launched, a config or state shape the code
  did not expect) no longer ends the cycle: the error is logged on one line,
  recorded under `_errors` in `state.json` (cleared on the next clean cycle),
  and stepped onto the service's root key as a `CHECK_ERROR` so it pages like
  any other finding; the other services, the state save and the alert flush
  all still run. Any `defaults` key left out of the config falls back to the
  documented default above (service value → `defaults` block → engine default).
- **Escalated re-nags renew the approval.** Every "STILL DOWN … reply 'heal
  <key>'" re-page re-creates the pending approval for the code's auto fix, so
  `heal <key>` keeps working for as long as the admin keeps being paged, not
  just for `pending_ttl_hours` after the cap was hit. A watch-only re-page
  never creates a pending entry.
- **Alert-sink retry.** A sink that exits non-zero, times out (30 s) or cannot
  be launched does not lose the page: the paged keys get their previous
  `last_alert` back, so the realert window is open again next cycle and the
  state machine re-sends; `_sink` in `state.json` records
  `last_failure`/`consecutive_failures`/`detail` (or `last_success`). The
  lines are not also written to the digest (the retry would duplicate them).
  The sink runs with `SELFHEAL_ROOT`, `SELFHEAL_STATE_DIR` and
  `SELFHEAL_AUDIT_LOG` exported, like checks and remediations.

## The check contract (for template authors)

Exit 0 and print nothing when healthy. Exit 1 with one JSON object per line
when not. Exit 64 for misconfiguration (missing param). Anything else — or
unparseable output, or a timeout — becomes a synthesized `CHECK_ERROR` on the
root key. Mark a finding `"severity": "degraded"` only when the service is
provably fine by another route and it is your VIEW that is broken. Exit 0
silently when the root cause belongs to another service: one root cause, one
alert.

Finding keys must be the service `name` or `<name>/<subkey>`, where the subkey
is letters, digits and `. _ @ : -` only — no leading `-`, no slash, no
whitespace, max 64 characters — because it becomes the remediation's argument.
Any other finding (another service's key, a traversal-shaped subkey, a missing
or non-UPPER_SNAKE `status`) is rejected and surfaces as a `CHECK_ERROR` on the
root key that counts the rejects and names the first.

## The remediation contract

Fixed content; at most one argument; all targets from params. Exit 0
fixed+verified, 1 failed/unverified, 64 refused, 75 refused-by-cap. Source
`engine/lib/remediation.sh` and call `rem_begin` before anything else.
Auto-class remediations act in the fail-safe direction only. The single
argument must match the subkey pattern above (letters, digits, `. _ @ : -`, no
leading `-`, no slash, max 64): the engine and `approve-heal.py` refuse to run
otherwise (exit 64 / "⛔ refusing"), and a malformed pinned `arg` in an
ask-first entry is ignored with a WARN — the proposal says so.
