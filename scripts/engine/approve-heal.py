#!/usr/bin/env python3
"""approve-heal.py <service-key> - execute a human-approved remediation.

The host-framework adapter invokes this after the user approves a pending fix
(e.g. replies 'heal <key>' in the chat channel). The adapter's dangerous-
command gate must classify this script as gated, so the path stays behind the
deployment's 2FA.

Looks up the pending-approval entry recorded by selfheal.py, runs the recorded
remediation, re-runs the service's health check to verify (catching the verify
timeout - a v1 debt), prints a human-readable result for the agent to relay
verbatim, and clears the pending entry + escalated state.
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


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve(p):
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def svc_env(svc):
    env = dict(os.environ, SELFHEAL_CALLER="approve-heal")
    for k, v in (svc or {}).get("params", {}).items():
        env[str(k)] = str(v)
    if svc and "containers_auto" in svc:
        env["SELFHEAL_CONTAINERS_AUTO"] = " ".join(svc["containers_auto"])
    if svc and "containers_watch" in svc:
        env["SELFHEAL_CONTAINERS_WATCH"] = " ".join(svc["containers_watch"])
    env["SELFHEAL_ROOT"] = str(ROOT)
    return env


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9/_.-]+", sys.argv[1]):
        print("Usage: approve-heal.py <service-key>")
        return 64
    key = sys.argv[1]

    config_path = os.environ.get("SELFHEAL_CONFIG", str(ROOT / "services.json"))
    config = load(Path(config_path), {"services": [], "paths": {}, "defaults": {}})
    paths = config.get("paths", {})
    state_dir = resolve(paths.get("state_dir", "state"))
    state_file = state_dir / "state.json"
    pending_file = state_dir / "pending-approvals.json"
    lock_file = state_dir / ".lock"
    audit_log = resolve(paths.get("audit_log", "state/audit.log"))

    state_dir.mkdir(parents=True, exist_ok=True)
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

        audit_log.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_log, "a") as f:
            f.write(f"{now_iso()} ts={int(time.time())} APPROVED-EXEC service={key} "
                    f"script={script} arg={arg or ''} caller=approve-heal\n")

        cmd = ["bash", str(resolve(script))] + ([arg] if arg else [])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                               env=svc_env(svc))
        except subprocess.TimeoutExpired:
            print(f"❌ {Path(script).name} timed out after 180s. State unchanged.")
            return 1
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 75:
            print(f"⛔ {Path(script).name} refused: internal rate cap. {out}")
            return 1
        if r.returncode != 0:
            print(f"❌ {Path(script).name} failed (exit {r.returncode}): {out[-300:]}")
            return 1

        # verify: re-run the service's check. The verify timeout is CAUGHT
        # (v1 debt: a slow check tracebacked here and the user got nothing).
        verdict = "remediation ran, but no health check found to verify"
        if svc:
            delay = svc.get("verify_delay_seconds",
                            config.get("defaults", {}).get("verify_delay_seconds", 45))
            time.sleep(delay)
            try:
                chk = subprocess.run(["bash", str(resolve(svc["check"]))],
                                     capture_output=True, text=True,
                                     timeout=svc.get("check_timeout_seconds",
                                                     config.get("defaults", {}).get("check_timeout_seconds", 45)) + 15,
                                     env=svc_env(svc))
                still = any(json.loads(l).get("key") == key
                            for l in chk.stdout.splitlines() if l.strip().startswith("{"))
                verdict = ("verified HEALTHY ✅" if chk.returncode == 0 or not still
                           else "still UNHEALTHY after remediation ⚠️ — may need manual attention")
            except subprocess.TimeoutExpired:
                verdict = ("remediation ran but the verify check timed out — "
                           "treat as unverified and watch the next monitor cycle")

        del pending[key]
        save(pending_file, pending)
        state = load(state_file, {"keys": {}})
        rec = state["keys"].get(key)
        if rec:
            rec["status"] = "ok" if "HEALTHY ✅" in verdict else "failing"
            rec["consecutive_failures"] = 0
            rec["last_transition"] = now_iso()
            save(state_file, state)

        print(f"🔧 Approved heal for {key}: {Path(script).name} completed, {verdict}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
