#!/bin/bash
# tailscale-running.sh - ALT_GUARD_CMD for checks/templates/check-http.sh:
# exit 0 iff THIS box's tailscale backend is Running, else 1 (not installed,
# daemon down, Stopped/NeedsLogin, unparseable output). A failing guard tells
# the check that the tailnet transport measures OUR outage, not the service's.
# JSON is parsed, not grepped: `tailscale status --json` key order and spacing
# are not a contract.
command -v tailscale >/dev/null 2>&1 || exit 1
tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    sys.exit(0 if json.load(sys.stdin).get("BackendState") == "Running" else 1)
except Exception:
    sys.exit(1)'
