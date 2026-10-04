#!/bin/bash
# test_install.sh - offline tests for scripts/install.sh: fresh install,
# idempotence (config/env/state never clobbered, code dirs replace-copied),
# the engine dry run, --test-page (stub curl on PATH), and --apply-cron
# against a stub crontab. The stub crontab sits on PATH for EVERY case, so a
# bug can never touch the real crontab. No network, no sudo, everything under
# a mktemp root; python3 stdlib only; runs on macOS and Linux.

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRCROOT="$REPO/authoring/scripts"
INSTALL="$SRCROOT/install.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); echo "  PASS $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL $1"; }
# assert <name> <want_rc> <want_substr|-> -- cmd...
assert() {
    local name="$1" want_rc="$2" want="$3"; shift 3; shift  # drop --
    local out rc
    out=$(env "$@" 2>&1); rc=$?
    if [ "$rc" != "$want_rc" ]; then bad "$name (rc=$rc want=$want_rc; out=$out)"; return; fi
    if [ "$want" != "-" ] && ! printf '%s' "$out" | grep -q "$want"; then
        bad "$name (missing '$want' in: $out)"; return
    fi
    ok "$name"
}
fmode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null; }

DP="$T/deploy"
EF="$T/cranston.env"

# --- stub crontab: on PATH for ALL cases. Records every argv; -l prints the
# --- stored state (exit 1 when none, like a user with no crontab); '-' stores stdin.
STUBDIR="$T/stubs"; mkdir -p "$STUBDIR"
export CRONTAB_LOG="$T/crontab-calls.log"
export CRONTAB_STATE="$T/crontab-state"
cat > "$STUBDIR/crontab" <<'EOF'
#!/bin/bash
echo "crontab $*" >> "${CRONTAB_LOG:?}"
case "${1:-}" in
    -l) [ -f "${CRONTAB_STATE:?}" ] && cat "$CRONTAB_STATE" || exit 1 ;;
    -)  cat > "${CRONTAB_STATE:?}" ;;
    *)  echo "stub crontab: unexpected argv: $*" >&2; exit 64 ;;
esac
EOF
chmod +x "$STUBDIR/crontab"

# run_install [env overrides...] -- [flags...]: output in $OUT, rc in $RC
OUT=""; RC=0
run_install() {
    local envs=()
    while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
    OUT=$(env PATH="$STUBDIR:$PATH" "${envs[@]}" \
          bash "$INSTALL" --deploy "$DP" --env-file "$EF" "$@" 2>&1)
    RC=$?
}
has() { printf '%s\n' "$OUT" | grep -qF "$1"; }

echo "== 1. installer parses, is executable, refuses unknown flags =="
bash -n "$INSTALL" && ok "bash -n install.sh" || bad "bash -n install.sh"
[ -x "$INSTALL" ] && ok "install.sh is executable" || bad "install.sh is not executable"
assert "unknown flag -> 64 with usage" 64 "usage" -- PATH="$STUBDIR:$PATH" bash "$INSTALL" --bogus
assert "--deploy without a value -> 64" 64 "usage" -- PATH="$STUBDIR:$PATH" bash "$INSTALL" --deploy

echo "== 2. fresh run (no flags) =="
# TEST-NET gateway override: the network gate goes down fast, so the example
# config's remote probes are skipped and the dry run stays offline.
run_install SELFHEAL_GW_OVERRIDE=203.0.113.1 --
[ "$RC" = 0 ] && ok "fresh run exits 0" || bad "fresh run exits 0 (rc=$RC; $OUT)"
alldirs=1
for d in engine bin checks gates remediations onboard; do
    [ -d "$DP/$d" ] || { alldirs=0; bad "deploy dir $d exists"; }
done
[ "$alldirs" = 1 ] && ok "all six code dirs deployed"
[ -f "$DP/services.json" ] && ok "services.json seeded from the example" || bad "services.json seeded"
[ -f "$EF" ] && ok "env file created" || bad "env file created"
[ "$(fmode "$EF")" = "600" ] && ok "env file mode 600" || bad "env file mode 600 (got $(fmode "$EF"))"
grep -qF "export SELFHEAL_ALERT_FILE=$DP/state/alert-pending.json" "$EF" \
    && ok "env file pins the alert file under state/" || bad "alert file line in env file: $(cat "$EF")"
has ". $EF;" && ok "cron lines source the env file" || bad "cron lines source the env file ($OUT)"
has "flock -n /tmp/cranston.cronlock" && ok "engine cron line uses flock -n" || bad "flock line"
has "$DP/bin/notify-alerts.sh" && ok "notify-alerts.sh cron line printed" || bad "notify-alerts line"
has "SELFHEAL_DIGEST_FILE=$DP/state/digest.jsonl" && has "SELFHEAL_NOTIFY_CMD=$DP/bin/send-telegram.sh" \
    && has "$DP/bin/flush-digest.sh" && ok "digest cron line carries both env vars" || bad "digest line ($OUT)"
[ ! -s "$CRONTAB_LOG" ] && ok "crontab never invoked without --apply-cron" || bad "crontab touched: $(cat "$CRONTAB_LOG")"

echo "== 3. idempotence: config/env/state kept, code replace-copied =="
# A valid-JSON admin edit (the engine quarantines corrupt configs on its own,
# so the edit must parse to prove the INSTALLER never rewrites the file).
python3 - "$DP/services.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["_admin_marker"] = "edited-by-hand-case3"
json.dump(d, open(p, "w"), indent=2)
PY
cp "$DP/services.json" "$T/services.edited"
echo "export CRANSTON_TEST_MARKER=1" >> "$EF"
cp "$EF" "$T/env.edited"
echo stale > "$DP/engine/PLANTED-stale-file"
mkdir -p "$DP/state"; echo precious > "$DP/state/keepme"
run_install SELFHEAL_GW_OVERRIDE=203.0.113.1 --
[ "$RC" = 0 ] && ok "re-run exits 0 (engine rc is non-fatal)" || bad "re-run exits 0 (rc=$RC; $OUT)"
cmp -s "$DP/services.json" "$T/services.edited" && ok "edited services.json kept byte-identical" || bad "services.json overwritten"
has "services.schema.md" && ok "kept-note points at services.schema.md" || bad "kept-note ($OUT)"
cmp -s "$EF" "$T/env.edited" && ok "edited env file kept byte-identical" || bad "env file overwritten"
[ ! -e "$DP/engine/PLANTED-stale-file" ] && ok "replace-copy removed the stale file in engine/" || bad "stale file survived the re-copy"
[ "$(cat "$DP/state/keepme" 2>/dev/null)" = "precious" ] && ok "state/ untouched by the re-run" || bad "state/ was touched"
chmod 644 "$EF"
run_install SELFHEAL_GW_OVERRIDE=203.0.113.1 --
has "WARNING" && has "600" && ok "non-600 env file draws a warning" || bad "mode warning ($OUT)"
chmod 600 "$EF"

echo "== 4. the dry run really executes the engine =="
# Minimal valid services.json: a local, trivially-ok check (the sandbox's
# box-disk pattern) plus one that deterministically fails - a healthy check
# leaves no state key, so the failing one is the proof the checks really ran.
# Gateway pinned to 127.0.0.1 so the network gate is up.
rm -f "$DP/state/state.json"
cat > "$DP/services.json" <<EOF
{
  "version": 2,
  "paths": {
    "state_dir": "state",
    "audit_log": "state/audit.log",
    "digest_file": "state/digest.jsonl",
    "alert_sink": "bin/send-alert.sh",
    "approval_gate": null
  },
  "defaults": {
    "fail_threshold": 2,
    "check_timeout_seconds": 20,
    "cooldown_minutes": 0,
    "max_attempts": 2,
    "attempt_window_hours": 6,
    "verify_delay_seconds": 1,
    "realert_minutes": 60,
    "recovery_hold_minutes": 1,
    "pending_ttl_hours": 6,
    "post_outage_grace_minutes": 0,
    "gateway_ip": "127.0.0.1"
  },
  "services": [
    {
      "name": "box-disk",
      "enabled": true,
      "local": true,
      "check": "checks/templates/check-disk-space.sh",
      "params": {
        "CHECK_KEY": "box-disk",
        "MOUNT_PATH": "/",
        "MIN_FREE_PCT": "1"
      },
      "remediations": {}
    },
    {
      "name": "box-absent",
      "enabled": true,
      "local": true,
      "check": "checks/templates/check-disk-space.sh",
      "params": {
        "CHECK_KEY": "box-absent",
        "MOUNT_PATH": "$T/no-such-mount",
        "MIN_FREE_PCT": "1"
      },
      "remediations": {}
    }
  ]
}
EOF
run_install SELFHEAL_GW_OVERRIDE=127.0.0.1 --
[ "$RC" = 0 ] && ok "install run with a valid config exits 0" || bad "valid-config run (rc=$RC; $OUT)"
has "dry run OK (rc=0)" && ok "dry run reported rc=0" || bad "dry run report ($OUT)"
[ -f "$DP/state/state.json" ] && ok "dry run wrote state/state.json" || bad "state.json missing after dry run"
grep -q "box-absent" "$DP/state/state.json" 2>/dev/null \
    && ok "the failing check's finding reached state.json (engine really ran)" || bad "box-absent not in state.json"
grep -q "VOLUME_ABSENT" "$DP/state/state.json" 2>/dev/null \
    && ok "finding code recorded" || bad "VOLUME_ABSENT not recorded"

echo "== 5. --test-page: skipped on empty tokens, sends via curl when set =="
# Env file still holds the skeleton's empty tokens (plus the case-3 marker).
run_install SELFHEAL_GW_OVERRIDE=127.0.0.1 -- --test-page
has "test page skipped" && has "TG_BOT_TOKEN" && ok "empty tokens -> skipped, reason printed" || bad "skip reason ($OUT)"
# Fill in fake tokens and stub curl: send-telegram.sh reads the http_code from
# curl's stdout, so the stub prints 200 and records its argv.
export CURL_LOG="$T/curl-calls.log"
cat > "$STUBDIR/curl" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "${CURL_LOG:?}"
printf '200'
exit 0
EOF
chmod +x "$STUBDIR/curl"
cat > "$EF" <<EOF
export TG_BOT_TOKEN=000000:FAKE-TOKEN
export TG_CHAT_ID=123456789
export SELFHEAL_ALERT_FILE=$DP/state/alert-pending.json
EOF
chmod 600 "$EF"
run_install SELFHEAL_GW_OVERRIDE=127.0.0.1 -- --test-page
[ "$RC" = 0 ] && ok "--test-page run exits 0" || bad "--test-page run (rc=$RC; $OUT)"
has "test page sent (rc=0)" && ok "installer reports the page delivered" || bad "delivery report ($OUT)"
grep -q "bot000000:FAKE-TOKEN/sendMessage" "$CURL_LOG" 2>/dev/null \
    && ok "send-telegram.sh posted to the Bot API sendMessage path" || bad "curl stub never fired ($(cat "$CURL_LOG" 2>/dev/null))"
grep -q "chat_id=123456789" "$CURL_LOG" && ok "page addressed to the configured chat" || bad "chat_id in curl argv"
rm -f "$STUBDIR/curl"   # nothing after this case may 'send'
[ ! -s "$CRONTAB_LOG" ] && ok "crontab still untouched through case 5" || bad "crontab touched early: $(cat "$CRONTAB_LOG")"

echo "== 6. --apply-cron: marker block, backup, idempotent =="
printf '0 0 * * * echo keepme\n' > "$CRONTAB_STATE"      # a pre-existing user crontab
run_install SELFHEAL_GW_OVERRIDE=127.0.0.1 -- --apply-cron
[ "$RC" = 0 ] && ok "--apply-cron exits 0" || bad "--apply-cron (rc=$RC; $OUT)"
grep -q "^crontab -$" "$CRONTAB_LOG" && ok "stub saw a 'crontab -' write" || bad "no crontab - write ($(cat "$CRONTAB_LOG"))"
grep -q "^0 0 \* \* \* echo keepme$" "$CRONTAB_STATE" && ok "pre-existing cron line survives" || bad "user line lost: $(cat "$CRONTAB_STATE")"
[ "$(grep -c '^# cranston-skill begin$' "$CRONTAB_STATE")" = 1 ] \
    && [ "$(grep -c '^# cranston-skill end$' "$CRONTAB_STATE")" = 1 ] \
    && ok "exactly one marker block" || bad "marker block count: $(cat "$CRONTAB_STATE")"
grep -qF "flock -n /tmp/cranston.cronlock python3 $DP/engine/selfheal.py" "$CRONTAB_STATE" \
    && ok "engine line installed" || bad "engine line missing"
grep -qF "$DP/bin/notify-alerts.sh" "$CRONTAB_STATE" && ok "notify line installed" || bad "notify line missing"
grep -qF "SELFHEAL_DIGEST_FILE=$DP/state/digest.jsonl SELFHEAL_NOTIFY_CMD=$DP/bin/send-telegram.sh $DP/bin/flush-digest.sh" "$CRONTAB_STATE" \
    && ok "digest line installed with both env vars" || bad "digest line missing"
ls "$DP/state"/crontab.backup-* >/dev/null 2>&1 && ok "backup saved under state/" || bad "no crontab backup in state/"
cp "$CRONTAB_STATE" "$T/crontab.first"
sleep 1   # distinct backup filename on the second run
run_install SELFHEAL_GW_OVERRIDE=127.0.0.1 -- --apply-cron
cmp -s "$CRONTAB_STATE" "$T/crontab.first" && ok "second --apply-cron is byte-identical" || bad "crontab drifted on re-apply: $(diff "$T/crontab.first" "$CRONTAB_STATE")"
[ "$(grep -c '^# cranston-skill begin$' "$CRONTAB_STATE")" = 1 ] && ok "still exactly one block" || bad "block duplicated"
[ "$(ls "$DP/state"/crontab.backup-* | wc -l | tr -d ' ')" -ge 2 ] && ok "each apply leaves its own backup" || bad "backup count"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ] || exit 1
