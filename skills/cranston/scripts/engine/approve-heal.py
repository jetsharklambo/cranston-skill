#!/usr/bin/env python3
"""approve-heal.py <service-key> [code] - execute a human-approved remediation.

The host-framework adapter invokes this after the user approves a pending fix
(e.g. replies 'heal <key>' in the chat channel). The remediation runs through
the deployment's `paths.approval_gate` (argv prefix) when one is configured -
the gate sees SELFHEAL_CALLER=approve-heal and NO SELFHEAL_AUTOMATION marker,
which is its cue to demand the second factor (contract + templates:
references/approval-gates.md). Inherited SELFHEAL_* and GATE_* names are
stripped from the child env so neither the caller's environment nor a
service's params block can plant that marker (see svc_env). The optional
[code] argument (e.g. a TOTP code the user included in the chat reply) is
passed to the gate as the GATE_CODE env var; it is never written to the audit
log or state.

Looks up the pending-approval entry recorded by selfheal.py, runs the recorded
remediation, re-runs the service's health check to verify (any verify failure
collapses to an "unverified" verdict - it can never eat the result), prints a
human-readable result for the agent to relay verbatim, and clears the pending
entry + escalated state. The state lock is held only around the state-file
reads/writes (two short phases), never across the remediation run, so the
engine's monitor cycles keep running while a fix is in flight.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent      # engine/
ROOT = BASE.parent

# A pending entry's argument is a finding subkey or a pinned arg: no leading
# '-', no slash, no whitespace, max 64 chars. Keep identical to selfheal.py.
ARG_RE = r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,63}"
# A service key is <name> or <name>/<subkey> - the same shape selfheal.py's
# valid_finding accepts, so every key the engine can create is approvable
# here (subkeys may carry '@' and ':', e.g. a host:port) and nothing else is
# (one slash at most; a second slash never names an engine key).
KEY_RE = re.compile(r"[A-Za-z0-9_.-]+(?:/" + ARG_RE + r")?")
# Verify-step fallbacks when neither the service nor the config's "defaults"
# block sets them. Keep identical to selfheal.py's ENGINE_DEFAULTS (this
# script is standalone by design - no selfheal import).
VERIFY_DEFAULTS = {"verify_delay_seconds": 45, "check_timeout_seconds": 45}
# Names a service's params block may NOT set (engine-owned markers, the gate's
# second factor, PATH-like names). Keep identical to selfheal.py's copy.
RESERVED_PARAM_RE = r"^(SELFHEAL_|GATE_|LD_|BASH_|PYTHON|SHELLOPTS$|PATH$|ENV$|CDPATH$|IFS$|HOME$)"
# Inherited env names the human path never forwards (why: svc_env).
STRIP_RE = r"^(SELFHEAL_|GATE_)"


def reserved_param(svc):
    """The first reserved key in a service's params block, or None."""
    for k in (svc or {}).get("params", {}):
        if re.match(RESERVED_PARAM_RE, str(k)):
            return str(k)
    return None


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve(p):
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def load(path, default):
    """Mirror selfheal.py's load_json: a missing file is the default; a
    CORRUPT file is moved aside and the default returned (silently treating
    a corrupt pending/state file as empty erased approvals and history)."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        aside = path.with_suffix(
            path.suffix + f".corrupt-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
        try:
            os.replace(path, aside)
            print(f"(warning: {path.name} corrupt ({e}); moved aside to {aside.name})")
        except OSError:
            pass
        return default


def save(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def svc_env(svc, state_dir, audit_log):
    """Environment for the remediation (and the gate): the process env MINUS
    every SELFHEAL_*/GATE_* name, + the service's params, + the engine exports
    selfheal.py's auto path also sets - in that order, so params can never
    override an export.

    The strip is the second factor's integrity. Every gate template passes an
    allowlisted script through without interaction when SELFHEAL_AUTOMATION is
    "true", so an inherited `SELFHEAL_AUTOMATION=true approve-heal.py <key>`
    (or a params block setting it) used to skip the human factor outright, and
    an inherited GATE_* could repoint a gate's verifier. This path never sets
    SELFHEAL_AUTOMATION; the one GATE_* it carries is GATE_CODE, which main()
    adds after this returns. (A gate's own settings belong in its mode-600 env
    file, never in the caller's environment.)

    SELFHEAL_AUDIT_LOG matters most: the remediation library counts its own
    EXEC lines there for its rate cap and falls back to a /tmp file without
    it - which split the audit trail and made the cap count every deployment
    on the box (a bug the sandbox drills found)."""
    env = {k: v for k, v in os.environ.items() if not re.match(STRIP_RE, k)}
    for k, v in (svc or {}).get("params", {}).items():
        env[str(k)] = str(v)
    if svc and "containers_auto" in svc:
        env["SELFHEAL_CONTAINERS_AUTO"] = " ".join(svc["containers_auto"])
    if svc and "containers_watch" in svc:
        env["SELFHEAL_CONTAINERS_WATCH"] = " ".join(svc["containers_watch"])
    env["SELFHEAL_CALLER"] = "approve-heal"
    env["SELFHEAL_ROOT"] = str(ROOT)
    env["SELFHEAL_STATE_DIR"] = str(state_dir)
    env["SELFHEAL_AUDIT_LOG"] = str(audit_log)
    return env


def argvify(v):
    """Same semantics as selfheal.py's argvify: a string names one file
    (.sh/.py get an interpreter), a list is taken as-is with '/'-containing
    elements resolved against the install root."""
    if not v:
        return []
    if isinstance(v, str):
        path = str(resolve(v))
        if path.endswith(".sh"):
            return ["bash", path]
        if path.endswith(".py"):
            return ["python3", path]
        return [path]
    return [str(resolve(x)) if "/" in str(x) else str(x) for x in v]


def main():
    if len(sys.argv) not in (2, 3) or not KEY_RE.fullmatch(sys.argv[1]):
        print("Usage: approve-heal.py <service-key> [code]")
        return 64
    key = sys.argv[1]
    gate_code = sys.argv[2] if len(sys.argv) == 3 else None
    if gate_code is not None and not re.fullmatch(r"[A-Za-z0-9-]{4,64}", gate_code):
        print("Usage: approve-heal.py <service-key> [code]   (code: 4-64 alphanumerics)")
        return 64

    config_path = os.environ.get("SELFHEAL_CONFIG", str(ROOT / "services.json"))
    config = load(Path(config_path), {"services": [], "paths": {}, "defaults": {}})
    paths = config.get("paths", {})
    state_dir = resolve(paths.get("state_dir", "state"))
    state_file = state_dir / "state.json"
    pending_file = state_dir / "pending-approvals.json"
    lock_file = state_dir / ".lock"
    audit_log = resolve(paths.get("audit_log", "state/audit.log"))

    state_dir.mkdir(parents=True, exist_ok=True)

    # -- phase 1 (LOCKED): resolve + validate the pending entry, audit the
    # intent, then RELEASE the lock. The lock serializes state-file reads
    # and writes ONLY - never the remediation run itself: holding it through
    # gate + remediation + verify (minutes, worst case) made selfheal.py's
    # non-blocking flock skip whole monitor cycles.
    with open(lock_file, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)

        pending = load(pending_file, {})
        entry = pending.get(key)
        if not entry:
            # Tolerate mangled keys from chat replies ("open-claw-gateway",
            # "OpenClaw Gateway"): compare lowercase alphanumerics only.
            def norm(s):
                return re.sub(r"[^a-z0-9]", "", s.lower())
            matches = [k for k in pending if norm(k) == norm(key)]
            if len(matches) == 1:
                print(f"(interpreted '{key}' as pending service '{matches[0]}')")
                key = matches[0]
                entry = pending[key]
        if not entry:
            print(f"Nothing pending for '{key}'. Current pending approvals: "
                  f"{', '.join(sorted(pending)) or 'none'}.")
            return 1
        if datetime.now(timezone.utc) > datetime.fromisoformat(entry["expires"].replace("Z", "+00:00")):
            del pending[key]
            save(pending_file, pending)
            print(f"The approval for '{key}' expired ({entry['expires']}). "
                  f"Run the health check for fresh status before retrying.")
            return 1

        script, arg = entry["script"], entry.get("arg")
        svc = next((s for s in config["services"]
                    if key == s["name"] or key.startswith(s["name"] + "/")), None)
        # Refuse before anything runs or is audited; the pending entry stays,
        # so a corrected services.json can be approved against on retry.
        if arg is not None and not re.fullmatch(ARG_RE, str(arg)):
            print("⛔ refusing: pending entry has an invalid argument. "
                  "The pending approval is kept.")
            return 1
        bad = reserved_param(svc)
        if bad:
            print(f"⛔ refusing: params for {svc['name']} sets reserved key {bad} "
                  f"(SELFHEAL_*/GATE_*/PATH-like names are engine-owned). "
                  f"The pending approval is kept - fix services.json and retry.")
            return 1

        audit_log.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_log, "a") as f:
            f.write(f"{now_iso()} ts={int(time.time())} APPROVED-EXEC service={key} "
                    f"script={script} arg={arg or ''} caller=approve-heal\n")
    # lock released: the engine's cycles run freely while the fix does

    # -- UNLOCKED: announce, gate + remediation, verify.
    # Consent pre-announcement: a household/named-consent fix tells the
    # people it disturbs BEFORE it runs - one extra sink invocation on the
    # 'announce' channel. The sink only queues the line; an announce failure
    # never blocks or vetoes the fix.
    consent = str(entry.get("consent") or "")
    if consent == "household" or consent.startswith("named:"):
        text = entry.get("announce") or (
            f"Heads-up: fixing {key} now ({Path(script).name}) — "
            f"it may be briefly unavailable.")
        sink = argvify(paths.get("alert_sink", "bin/send-alert.sh"))
        a_env = svc_env(svc, state_dir, audit_log)
        a_env["SELFHEAL_ALERT_CHANNEL"] = "announce"
        try:
            a = subprocess.run(sink + [text], capture_output=True, text=True,
                               timeout=30, env=a_env)
            if a.returncode != 0:
                print(f"(note: announce sink failed rc={a.returncode}; continuing)")
        except Exception as e:
            print(f"(note: announce sink failed ({type(e).__name__}); continuing)")

    # Human-approved runs go through the deployment's approval gate when
    # one is configured. The gate distinguishes this caller from the
    # engine's auto path by SELFHEAL_CALLER=approve-heal and the ABSENCE
    # of SELFHEAL_AUTOMATION, and may demand a second factor; GATE_CODE
    # carries one supplied with the approval. Budget is 300s here (vs the
    # engine's 180s) so an interactive gate has room for the human.
    gate = argvify(paths.get("approval_gate"))
    cmd = (gate or ["bash"]) + [str(resolve(script))] + ([str(arg)] if arg else [])
    env = svc_env(svc, state_dir, audit_log)
    if gate_code:
        env["GATE_CODE"] = gate_code
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           env=env)
    except subprocess.TimeoutExpired:
        print(f"❌ {Path(script).name} timed out after 300s "
              f"(gate included). State unchanged.")
        return 1
    if r.returncode == 65:
        out = (r.stdout + r.stderr).strip()
        print(f"⛔ approval gate refused: {out[-300:] or 'no reason given'}. "
              f"The pending approval is kept - retry with a valid factor.")
        return 1
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 75:
        print(f"⛔ {Path(script).name} refused: internal rate cap. {out}")
        return 1
    if r.returncode != 0:
        print(f"❌ {Path(script).name} failed (exit {r.returncode}): {out[-300:]}")
        return 1

    # verify: re-run the service's check. From here on the remediation HAS
    # run, so NOTHING may traceback past the cleanup below: a missing check,
    # garbage output or any verify error (v1 debt: a slow check tracebacked
    # here, the user got nothing, and the pending entry was never cleared)
    # collapses to an "unverified" verdict instead.
    verdict = "remediation ran, but no health check found to verify"
    if svc and svc.get("check"):
        try:
            delay = svc.get("verify_delay_seconds",
                            config.get("defaults", {}).get(
                                "verify_delay_seconds", VERIFY_DEFAULTS["verify_delay_seconds"]))
            time.sleep(delay)
            chk = subprocess.run(["bash", str(resolve(svc["check"]))],
                                 capture_output=True, text=True,
                                 timeout=svc.get("check_timeout_seconds",
                                                 config.get("defaults", {}).get(
                                                     "check_timeout_seconds",
                                                     VERIFY_DEFAULTS["check_timeout_seconds"])) + 15,
                                 env=svc_env(svc, state_dir, audit_log))
            # parse per-line like selfheal.run_check: a malformed line is
            # skipped, never a traceback
            findings_seen, still = 0, False
            for l in chk.stdout.splitlines():
                l = l.strip()
                if not l.startswith("{"):
                    continue
                try:
                    f = json.loads(l)
                except Exception:
                    continue
                if isinstance(f, dict) and "key" in f:
                    findings_seen += 1
                    if f.get("key") == key:
                        still = True
            if chk.returncode == 0:
                verdict = "re-checked and it's HEALTHY again ✅"
            elif still:
                verdict = "ran, but the re-check says it's still UNHEALTHY ⚠️ — needs human eyes"
            elif findings_seen:
                verdict = "re-checked and it's HEALTHY again ✅"   # check failing on OTHER keys only
            else:
                verdict = ("the fix ran but the verify output was unreadable — "
                           "treat as unverified and watch the next monitor cycle")
        except subprocess.TimeoutExpired:
            verdict = ("the fix ran but the verify check timed out — "
                       "treat as unverified and watch the next monitor cycle")
        except Exception as e:
            verdict = (f"the fix ran but the verify step errored ({type(e).__name__}) — "
                       f"treat as unverified and watch the next monitor cycle")

    # The remediation ran: the outcome is ALWAYS audited and the pending
    # entry ALWAYS cleared, whatever the verify step managed.
    verdict_word = ("ok" if "HEALTHY ✅" in verdict
                    else "unhealthy" if "UNHEALTHY" in verdict else "unverified")
    with open(audit_log, "a") as f:
        f.write(f"{now_iso()} ts={int(time.time())} APPROVED-RESULT service={key} "
                f"script={Path(script).name} verdict={verdict_word}\n")

    # -- phase 2 (LOCKED): re-read both files fresh - the engine may have
    # renewed the pending entry or rewritten state while the fix ran - then
    # pop/update only this key and save.
    with open(lock_file, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pending = load(pending_file, {})
        pending.pop(key, None)
        save(pending_file, pending)
        state = load(state_file, {"keys": {}})
        rec = state.get("keys", {}).get(key)
        if isinstance(rec, dict):
            state.setdefault("keys", {})[key] = rec
            rec["status"] = "ok" if "HEALTHY ✅" in verdict else "failing"
            rec["consecutive_failures"] = 0
            rec["last_transition"] = now_iso()
            if rec["status"] == "ok":
                rec.pop("first_failed_at", None)  # the incident is over
            save(state_file, state)

    print(f"🔧 Done — ran {Path(script).name} for {key}: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
