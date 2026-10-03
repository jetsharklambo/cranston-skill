#!/bin/bash
# check-ha-entity.sh - Home Assistant entity threshold / state template, with
# blind-persistence. READ-ONLY. Generalizes the proven half of cranston's
# officedelta check.
#
# The two lessons baked in:
#  1. Cloud-borne entities go `unavailable`/`unknown` during integration
#     warm-up (e.g. ~10 min after an HA restart). Acting on a young blind
#     reading caused a real outage. Blindness only becomes a finding when it
#     has PERSISTED, measured by the entity's own last_changed - never by
#     when we first noticed.
#  2. Exact-match state conditions are a bug when the source steps coarsely
#     (3-4% per cloud push skipped a `to: "50"` trigger). Thresholds here are
#     always <= / >= comparisons.
#
# Params:
#   CHECK_KEY        required
#   HA_URL           required (IP literal preferred)
#   HA_TOKEN         or HA_TOKEN_FILE (sourced; must export HA_TOKEN)
#   ENTITY_ID        required
#   VALUE_LOW        optional numeric - at/below emits VALUE_LOW
#   VALUE_CRITICAL   optional numeric - at/below emits VALUE_CRITICAL
#   EXPECT_STATE     optional - any other state emits STATE_MISMATCH
#   BLIND_MINUTES    default 15 - unavailable/unknown persisting this long
#                    emits ENTITY_BLIND; younger blindness is silent warm-up
#
# Findings: ENTITY_BLIND, VALUE_CRITICAL, VALUE_LOW, STATE_MISMATCH,
#           HA_UNREACHABLE is NOT emitted - if the HA API is down we exit 0
#           quietly; the deployment's HA service owns that (layer isolation).

set -u
LIB="${SELFHEAL_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}/engine/lib"
. "$LIB/check.sh"

require_param CHECK_KEY "finding key"
require_param HA_URL "Home Assistant URL"
require_param ENTITY_ID "entity id"
[ -n "${HA_TOKEN_FILE:-}" ] && [ -f "$HA_TOKEN_FILE" ] && . "$HA_TOKEN_FILE"
require_param HA_TOKEN "HA long-lived token (or HA_TOKEN_FILE)"
KEY="$CHECK_KEY"
BLIND_MIN=$(param BLIND_MINUTES 15)

body=$(curl -s --max-time 8 -H "Authorization: Bearer $HA_TOKEN" \
            "$HA_URL/api/states/$ENTITY_ID" 2>/dev/null)

# HA itself unreachable or auth broken -> someone else's finding; stay silent.
case "$body" in *'"state"'*) : ;; *) exit 0 ;; esac

SELFHEAL_ENTITY_JSON="$body" \
python3 - "$KEY" "$ENTITY_ID" "$BLIND_MIN" "${VALUE_LOW:-}" "${VALUE_CRITICAL:-}" "${EXPECT_STATE:-}" <<'PY'
import json, os, sys
from datetime import datetime, timezone

key, entity, blind_min = sys.argv[1], sys.argv[2], int(sys.argv[3])
v_low, v_crit, expect = sys.argv[4], sys.argv[5], sys.argv[6]
body = json.loads(os.environ["SELFHEAL_ENTITY_JSON"])
state = body.get("state", "unknown")
last_changed = body.get("last_changed", "")

def emit(status, layer, detail, severity=None):
    f = {"key": key, "status": status, "layer": layer, "detail": detail}
    if severity:
        f["severity"] = severity
    print(json.dumps(f))

findings = 0
if state in ("unavailable", "unknown", ""):
    age_min = 0
    try:
        dt = datetime.fromisoformat(last_changed.replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        age_min = 0  # unparsable timestamp = treat as young (fail safe-quiet)
    if age_min >= blind_min:
        emit("ENTITY_BLIND", "sensor",
             f"{entity} has read '{state}' for {int(age_min)} min (threshold {blind_min}) - "
             f"the integration is persistently blind, not warming up; numeric automations "
             f"keyed on this entity cannot fire")
        findings += 1
    raise SystemExit(1 if findings else 0)

if expect and state != expect:
    emit("STATE_MISMATCH", "state",
         f"{entity} reads '{state}', expected '{expect}'")
    findings += 1

try:
    val = float(state)
except ValueError:
    val = None

if val is not None and v_crit and val <= float(v_crit):
    emit("VALUE_CRITICAL", "threshold",
         f"{entity} at {state} (critical <= {v_crit})")
    findings += 1
elif val is not None and v_low and val <= float(v_low):
    emit("VALUE_LOW", "threshold",
         f"{entity} at {state} (low <= {v_low})")
    findings += 1

raise SystemExit(1 if findings else 0)
PY
