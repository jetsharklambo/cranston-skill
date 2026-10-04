#!/bin/bash
# down.sh - stop the sandbox's fake services and delete sandbox/.run/.
HERE="$(cd "$(dirname "$0")" && pwd)"
export SANDBOX_RUN="$HERE/.run"
if [ -x "$SANDBOX_RUN/lan/svcctl" ]; then
    "$SANDBOX_RUN/lan/svcctl" stop media-server
    "$SANDBOX_RUN/lan/svcctl" stop nas
fi
rm -rf "$SANDBOX_RUN"
echo "sandbox removed"
