#!/bin/bash
# install.sh - automate the SKILL.md Install procedure: create the deployment
# root, copy the engine out of the skill, seed services.json and the mode-600
# credentials file, optionally send a Telegram test page, dry-run one engine
# cycle, and print (or, with --apply-cron, install) the three system-cron
# lines. Safe to re-run at any time: code dirs are replace-copied, but
# services.json, the env file, and everything under state/ are never
# overwritten - re-running upgrades the code and leaves the config alone.
#
# usage: install.sh [--deploy DIR] [--env-file PATH] [--test-page] [--apply-cron]
#   --deploy DIR     deployment root            (default /opt/cranston,
#                                                env override CRANSTON_DEPLOY)
#   --env-file PATH  mode-600 credentials file  (default /etc/cranston.env,
#                                                env override CRANSTON_ENV_FILE)
#   --test-page      source the env file and send "cranston test page"
#                    through bin/send-telegram.sh (skipped if tokens empty)
#   --apply-cron     write the three cron lines into the user's crontab
#                    (marker block, previous crontab backed up under state/);
#                    without it the lines are only printed
#
# Exit: 0 success; 64 usage; 66 skill bundle incomplete; 1 a step failed.
set -u

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPTS_DIR/.." && pwd)"
EXAMPLE="$SKILL_ROOT/assets/services.example.json"

DEPLOY="${CRANSTON_DEPLOY:-/opt/cranston}"
ENV_FILE="${CRANSTON_ENV_FILE:-/etc/cranston.env}"
TEST_PAGE=0
APPLY_CRON=0

usage() {
    echo "usage: install.sh [--deploy DIR] [--env-file PATH] [--test-page] [--apply-cron]"
    echo "defaults: --deploy ${CRANSTON_DEPLOY:-/opt/cranston} --env-file ${CRANSTON_ENV_FILE:-/etc/cranston.env}"
    exit 64
}

while [ $# -gt 0 ]; do
    case "$1" in
        --deploy)     [ $# -ge 2 ] || usage; DEPLOY="$2"; shift 2 ;;
        --env-file)   [ $# -ge 2 ] || usage; ENV_FILE="$2"; shift 2 ;;
        --test-page)  TEST_PAGE=1; shift ;;
        --apply-cron) APPLY_CRON=1; shift ;;
        *)            echo "install: unknown argument: $1"; usage ;;
    esac
done
[ -n "$DEPLOY" ] && [ -n "$ENV_FILE" ] || usage

# File mode as octal, GNU stat first, BSD stat as the fallback.
fmode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null; }

# The three cron lines, exactly the shape SKILL.md's Install step 5 shows,
# with this deployment's paths substituted. Every line sources the env file
# first (cron starts from an empty environment). flock(1) does not exist on
# macOS: when it is missing the engine line drops the flock prefix - the
# engine's own non-blocking state lock already makes an overlapping cycle
# exit instead of stacking, so the belt is Linux-only.
FLOCK_PREFIX="flock -n /tmp/cranston.cronlock "
if ! command -v flock >/dev/null 2>&1; then
    FLOCK_PREFIX=""
    echo "install: note - no flock(1) on this platform (macOS?); the engine cron" \
         "line runs without it. The engine's own lock prevents overlapping cycles."
fi
cron_lines() {
    printf '%s\n' \
        "*/2 * * * * . $ENV_FILE; ${FLOCK_PREFIX}python3 $DEPLOY/engine/selfheal.py >> /var/log/cranston.log 2>&1" \
        "* * * * *   . $ENV_FILE; $DEPLOY/bin/notify-alerts.sh >> /var/log/cranston.log 2>&1" \
        "25 5 * * *  . $ENV_FILE; SELFHEAL_DIGEST_FILE=$DEPLOY/state/digest.jsonl SELFHEAL_NOTIFY_CMD=$DEPLOY/bin/send-telegram.sh $DEPLOY/bin/flush-digest.sh >> /var/log/cranston.log 2>&1"
}

# -- 1. deployment root + replace-copy of the code dirs ----------------------
# Only the code is refreshed; state/ and services.json are never touched here.
mkdir -p "$DEPLOY" || { echo "install: ERROR - cannot create $DEPLOY (permissions? try --deploy somewhere writable)" >&2; exit 1; }
for d in engine bin checks gates remediations onboard; do
    if [ ! -d "$SCRIPTS_DIR/$d" ]; then
        echo "install: ERROR - $SCRIPTS_DIR/$d missing from the skill bundle" >&2; exit 66
    fi
    rm -rf "$DEPLOY/$d" || { echo "install: ERROR - cannot clear $DEPLOY/$d" >&2; exit 1; }
    cp -R "$SCRIPTS_DIR/$d" "$DEPLOY/$d" || { echo "install: ERROR - copy of $d failed" >&2; exit 1; }
done
echo "install: refreshed code in $DEPLOY (engine bin checks gates remediations onboard)"

# -- 2. services.json: seed once, never overwrite ----------------------------
if [ -f "$DEPLOY/services.json" ]; then
    echo "install: kept existing $DEPLOY/services.json (field docs: references/services.schema.md)"
else
    if [ ! -f "$EXAMPLE" ]; then
        echo "install: ERROR - $EXAMPLE not found; cannot seed services.json (incomplete skill bundle?)" >&2
        exit 66
    fi
    cp "$EXAMPLE" "$DEPLOY/services.json" || { echo "install: ERROR - cannot write $DEPLOY/services.json" >&2; exit 1; }
    echo "install: wrote starter $DEPLOY/services.json from services.example.json - edit it for this home"
fi

# -- 3. env file: skeleton once, mode 600, never overwrite -------------------
if [ -f "$ENV_FILE" ]; then
    mode=$(fmode "$ENV_FILE")
    if [ "$mode" = "600" ]; then
        echo "install: kept existing $ENV_FILE"
    else
        echo "install: kept existing $ENV_FILE - WARNING: mode is ${mode:-unreadable}, expected 600 (chmod 600 $ENV_FILE)"
    fi
else
    {
        echo "export TG_BOT_TOKEN="
        echo "export TG_CHAT_ID="
        echo "# export TG_ANNOUNCE_CHAT_ID=   (optional second chat for household pre-announcements)"
        echo "export SELFHEAL_ALERT_FILE=$DEPLOY/state/alert-pending.json"
    } > "$ENV_FILE" || { echo "install: ERROR - cannot write $ENV_FILE (permissions? try --env-file somewhere writable)" >&2; exit 1; }
    chmod 600 "$ENV_FILE"
    echo "install: wrote skeleton $ENV_FILE (mode 600) - fill in TG_BOT_TOKEN and TG_CHAT_ID"
fi

# -- 4. optional test page (--test-page) -------------------------------------
if [ "$TEST_PAGE" = 1 ]; then
    (
        set +u
        . "$ENV_FILE" 2>/dev/null
        if [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ]; then
            "$DEPLOY/bin/send-telegram.sh" "cranston test page"
            rc=$?
            if [ "$rc" = 0 ]; then
                echo "install: test page sent (rc=0) - check the Telegram chat"
            else
                echo "install: test page FAILED (rc=$rc) - verify TG_BOT_TOKEN/TG_CHAT_ID in $ENV_FILE and network"
            fi
        else
            echo "install: test page skipped - TG_BOT_TOKEN/TG_CHAT_ID are empty in $ENV_FILE (fill them in, re-run with --test-page)"
        fi
    )
fi

# -- 5. dry-run one engine cycle ----------------------------------------------
# A nonzero rc does NOT abort the install: a half-edited services.json is a
# normal state to run the installer from; finish the config and re-run.
mkdir -p "$DEPLOY/state"
python3 "$DEPLOY/engine/selfheal.py" --once
dry_rc=$?
if [ "$dry_rc" = 0 ]; then
    echo "install: dry run OK (rc=0) - inspect $DEPLOY/state/state.json"
else
    echo "install: dry run exited rc=$dry_rc (not fatal - the config may be unfinished; fix $DEPLOY/services.json and re-run) - see $DEPLOY/state/state.json"
fi

# -- 6. the three cron lines (always printed) ---------------------------------
echo "install: system-cron lines for this deployment (SKILL.md Install step 5):"
cron_lines

# -- 7. optional crontab install (--apply-cron) --------------------------------
if [ "$APPLY_CRON" = 1 ]; then
    bk="$DEPLOY/state/crontab.backup-$(date +%Y%m%d%H%M%S)"
    crontab -l > "$bk" 2>/dev/null || : > "$bk"
    {
        # Everything outside our marker block survives; a previous block is
        # replaced, which makes a re-run byte-identical.
        awk '/^# cranston-skill begin$/{inblk=1; next}
             /^# cranston-skill end$/{inblk=0; next}
             !inblk' "$bk"
        echo "# cranston-skill begin"
        cron_lines
        echo "# cranston-skill end"
    } | crontab - || { echo "install: ERROR - 'crontab -' failed; previous crontab saved at $bk" >&2; exit 1; }
    echo "install: crontab updated (previous crontab backed up at $bk)"
else
    echo "install: crontab NOT touched (re-run with --apply-cron, or paste the lines into 'crontab -e')"
fi

exit 0
