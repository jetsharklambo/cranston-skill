#!/bin/bash
# casa-sink.sh - the casa deployment's paths.alert_sink: pin the queue path,
# then exec the REAL bin/send-alert.sh beside this wrapper (so every alert
# still flows through the production queue + drainer).
#
# Why the pin: approve-heal.py's env scrub used to strip SELFHEAL_ALERT_FILE
# with the rest of SELFHEAL_* (second-factor integrity), sending the consent
# announcement to the global /tmp queue no drainer watches. The sim FOUND that
# bug and it is now fixed upstream (approve-heal passes the queue path through
# the scrub; test_engine group 35 pins it). The wrapper stays as a belt: it
# derives the per-deploy queue from SELFHEAL_STATE_DIR whenever
# SELFHEAL_ALERT_FILE is absent for any reason, so the sim keeps working even
# against an engine that predates the fix.
HERE="$(cd "$(dirname "$0")" && pwd)"
export SELFHEAL_ALERT_FILE="${SELFHEAL_ALERT_FILE:-${SELFHEAL_STATE_DIR:-$HERE/../state}/alert-pending.json}"
exec bash "$HERE/send-alert.sh" "$@"
