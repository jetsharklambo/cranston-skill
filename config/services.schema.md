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
    "approval_gate": null           // argv PREFIX for running remediations, e.g.
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
      },                            // never inside a template.
      "remediations": {             // finding code -> remediation class:
        "SERVICE_DOWN": "remediations/templates/restart-systemd-unit.sh",  // AUTO (string)
        "HOST_DOWN": { "ask": "remediations/my-outlet.sh", "arg": "svcoutlet",
                       "consent": "household" },                            // ASK-FIRST
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
- **`consent` on an ask-first entry** is carried for the adapter's alert
  composer: `"admin"` (default), `"named:<person>"`, or `"household"` —
  whose OK the fix needs is separate from whether the agent may act.
  (The engine stores and forwards it; enforcement wording is the adapter's.)

## The check contract (for template authors)

Exit 0 and print nothing when healthy. Exit 1 with one JSON object per line
when not. Exit 64 for misconfiguration (missing param). Anything else — or
unparseable output, or a timeout — becomes a synthesized `CHECK_ERROR` on the
root key. Mark a finding `"severity": "degraded"` only when the service is
provably fine by another route and it is your VIEW that is broken. Exit 0
silently when the root cause belongs to another service: one root cause, one
alert.

## The remediation contract

Fixed content; at most one argument; all targets from params. Exit 0
fixed+verified, 1 failed/unverified, 64 refused, 75 refused-by-cap. Source
`engine/lib/remediation.sh` and call `rem_begin` before anything else.
Auto-class remediations act in the fail-safe direction only.
