#!/bin/bash
# cron-emulator.sh <env-file> <deploy> [plan-file] - stand-in for system cron
# on containers that ship no usable cron daemon (the Claude Code cloud box).
#
# Foreground loop, one tick per minute. Each tick it re-reads the plan file
# (so an edit to the digest line mid-drill takes effect, exactly like a real
# crontab edit), takes the lines inside the `# cranston-skill begin/end`
# marker block (no markers: every non-comment line), and runs each line whose
# cadence is due this tick, in the same `. <env-file>; <command>` shape the
# installer's real cron lines use. Double-sourcing is harmless: the plan
# lines already open with `. <env-file>;` and the env file is idempotent
# exports.
#
# Cadence parsing is deliberately small: `*/N * * * *` runs every N ticks,
# `* * * * *` every tick; anything else (e.g. the installer's default
# `25 5 * * *` digest slot) is treated as daily (1440 ticks) and SAID SO in
# the log - the cloud drill rewrites that line to */15 before traffic starts,
# so hitting the daily fallback in a drill means phase 2 was skipped.
#
# Ticks count from process start, not wall clock: tick 0 runs everything,
# deterministic and boot-order-independent. Commands run backgrounded, like
# independent cron jobs; the engine line's own `flock -n` prevents overlap.
#
# Env:
#   CRON_EMULATOR_LOG   where the tick log goes
#                       (default <deploy>/state/cron-emulator.log)
#   CRON_EMULATOR_TICK  seconds per tick, default 60. TEST-ONLY knob for the
#                       one-tick smoke test; a drill must keep the real 60 or
#                       every engine timing assertion is off.
#   CRON_EMULATOR_MAX   stop after this many ticks (default: run until SIGTERM)
#
# SIGTERM/SIGINT finish the current tick's launches and exit 0 (the sleep is
# backgrounded + wait'ed so a signal interrupts it immediately).
# bash 3.2 compatible; stdlib awk only. Exit: 0 clean stop; 64 usage.
set -u

[ $# -ge 2 ] || { echo "usage: cron-emulator.sh <env-file> <deploy> [plan-file]"; exit 64; }
ENVF="$1"; DEPLOY="$2"; PLAN="${3:-$DEPLOY/state/cron-plan.txt}"
[ -f "$ENVF" ]  || { echo "cron-emulator: no env file at $ENVF"; exit 64; }
[ -d "$DEPLOY" ] || { echo "cron-emulator: no deploy dir at $DEPLOY"; exit 64; }
LOG="${CRON_EMULATOR_LOG:-$DEPLOY/state/cron-emulator.log}"
TICK_SECS="${CRON_EMULATOR_TICK:-60}"
MAX_TICKS="${CRON_EMULATOR_MAX:-0}"
mkdir -p "$(dirname "$LOG")"

STOP=0
trap 'STOP=1' TERM INT

logline() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) cron-emulator: $*" >> "$LOG"; }

# plan -> "N<TAB>command" lines: cadence in ticks, schedule fields stripped.
# Inside the marker block when one exists, else every non-comment line.
plan_jobs() {
    awk '
        /^# cranston-skill begin$/ { inblk = 1; sawblk = 1; next }
        /^# cranston-skill end$/   { inblk = 0; next }
        /^[[:space:]]*(#|$)/       { next }
        { line[++n] = $0; blk[n] = inblk }
        END {
            for (i = 1; i <= n; i++) {
                if (sawblk && !blk[i]) continue
                split(line[i], f, /[[:space:]]+/)
                sched = f[1]
                if (sched ~ /^\*\/[0-9]+$/)      { sub(/^\*\//, "", sched); cad = sched + 0 }
                else if (sched == "*")           { cad = 1 }
                else                             { cad = 1440; note[i] = 1 }
                cmd = line[i]
                for (j = 1; j <= 5; j++) sub(/^[^[:space:]]+[[:space:]]+/, "", cmd)
                printf "%d\t%d\t%s\n", cad, (note[i] ? 1 : 0), cmd
            }
        }' "$PLAN" 2>/dev/null
}

logline "started: plan=$PLAN env=$ENVF deploy=$DEPLOY tick=${TICK_SECS}s"
tick=0
while [ "$STOP" -eq 0 ]; do
    if [ ! -f "$PLAN" ]; then
        logline "tick $tick: plan file missing ($PLAN) - idling"
    else
        ran=0
        while IFS="$(printf '\t')" read -r cad fell cmd; do
            [ -n "$cad" ] || continue
            [ "$fell" = 1 ] && logline "tick $tick: unsupported schedule treated as daily: $cmd"
            if [ $((tick % cad)) -eq 0 ]; then
                ran=$((ran + 1))
                logline "tick $tick: run (*/$cad) ${cmd}"
                bash -c ". '$ENVF'; $cmd" &
            fi
        done <<EOF
$(plan_jobs)
EOF
        logline "tick $tick: launched $ran job(s)"
    fi
    tick=$((tick + 1))
    if [ "$MAX_TICKS" -gt 0 ] 2>/dev/null && [ "$tick" -ge "$MAX_TICKS" ]; then
        logline "reached CRON_EMULATOR_MAX=$MAX_TICKS - stopping"
        break
    fi
    sleep "$TICK_SECS" &
    wait $! 2>/dev/null
done
logline "stopping (signal or max ticks); waiting for launched jobs"
wait 2>/dev/null
logline "stopped clean at tick $tick"
exit 0
