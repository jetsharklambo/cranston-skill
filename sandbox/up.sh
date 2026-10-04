#!/bin/bash
# up.sh [--openclaw] - spin up the Cranston sandbox under sandbox/.run/:
#   venv/      isolated python3 (the engine is stdlib-only; this pins the interpreter)
#   lan/       fake home LAN: services on loopback + a ping shim with pluggable hosts
#   deploy/    a deployment root built exactly as SKILL.md's Install procedure says
#   openclaw/  (--openclaw) OpenClaw CLI in an isolated HOME, skill installed from
#              this checkout; deploy/ is then copied from the INSTALLED skill dir
# Then: source sandbox/.run/env.sh   and   bash sandbox/drill.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
RUN="$HERE/.run"
WITH_OC=0; [ "${1:-}" = "--openclaw" ] && WITH_OC=1

[ -x "$RUN/lan/svcctl" ] && bash "$HERE/down.sh" >/dev/null 2>&1 || true
rm -rf "$RUN"; mkdir -p "$RUN/lan/hosts" "$RUN/lan/pids"

echo "== venv"
python3 -m venv "$RUN/venv"
"$RUN/venv/bin/python3" --version

echo "== fake LAN"
cp "$HERE/lan/ping" "$HERE/lan/svcctl" "$HERE/lan/fake-service.py" "$RUN/lan/"
cat > "$RUN/env.sh" <<ENV
export SANDBOX_RUN="$RUN"
export DEPLOY="$RUN/deploy"
export PATH="$RUN/lan:$RUN/venv/bin:\$PATH"
ENV
[ "$WITH_OC" = 1 ] && cat >> "$RUN/env.sh" <<ENV
oc() { HOME="$RUN/openclaw/home" PATH="$RUN/openclaw/node/bin:\$PATH" "$RUN/openclaw/cli/node_modules/.bin/openclaw" "\$@"; }
ENV
. "$RUN/env.sh"
svcctl plug 192.168.1.1     # router (the engine's network gate)
svcctl plug 192.168.1.58    # media-server's host
svcctl plug 127.0.0.1       # the nas "host" (TCP_HOST)
svcctl start media-server
svcctl start nas
svcctl ls

SKILL_DIR="$REPO"
if [ "$WITH_OC" = 1 ]; then
    echo "== OpenClaw"
    OC="$RUN/openclaw"; mkdir -p "$OC/home" "$OC/cli"
    major=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
    if [ "$major" -ge 24 ] && [ "$major" -ne 25 ]; then
        mkdir -p "$OC/node/bin"; ln -sf "$(command -v node)" "$OC/node/bin/node"
    else
        echo "node $major too old for openclaw; fetching node@24 locally"
        npm install --silent --prefix "$OC/nodepkg" node@24 >/dev/null
        mkdir -p "$OC/node/bin"; ln -sf "$OC/nodepkg/node_modules/node/bin/node" "$OC/node/bin/node"
    fi
    (cd "$OC/cli" && PATH="$OC/node/bin:$PATH" npm init -y >/dev/null \
        && PATH="$OC/node/bin:$PATH" npm install --silent "openclaw@${OPENCLAW_VERSION:-latest}" >/dev/null)
    oc --version
    # Stage the checkout outside OpenClaw's install target (a local install
    # copies the whole source dir, and .run/ lives inside the repo). Tracked +
    # untracked-not-ignored files: what a git: install of this branch would see.
    mkdir -p "$RUN/src"
    (cd "$REPO" && git ls-files -co --exclude-standard -z | xargs -0 -I{} cp --parents {} "$RUN/src/")
    oc skills install "$RUN/src"
    oc skills info cranston
    SKILL_DIR="$OC/home/.openclaw/workspace/skills/cranston"
fi

echo "== deploy (from $SKILL_DIR)"
# SKILL.md Install steps 1+3, verbatim shape: copy the engine OUT of the skill.
mkdir -p "$DEPLOY"
cp -R "$SKILL_DIR/scripts/." "$DEPLOY"/
cp "$HERE/overlay/bin/sandbox-sink.sh" "$DEPLOY/bin/"
cp "$HERE/overlay/remediations/sandbox-restart.sh" "$DEPLOY/remediations/"
sed "s#@SVCCTL@#$RUN/lan/svcctl#g" "$HERE/overlay/services.sandbox.json" > "$DEPLOY/services.json"

echo "== first cycle (SKILL.md Install step 4)"
python3 "$DEPLOY/engine/selfheal.py" --once
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print({k:v['status'] for k,v in d['keys'].items()})" "$DEPLOY/state/state.json"
echo
echo "Sandbox up. Next:  source $RUN/env.sh && bash $HERE/drill.sh"
