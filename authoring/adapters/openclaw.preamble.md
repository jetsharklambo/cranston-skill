> **OpenClaw notes.** Run dangerous operations through the deployment's
> gated path (secure-bash or equivalent) exactly as the workspace's own
> rules demand — this skill never exempts you from the 2FA gate. The
> OpenClaw-native runtime shims ship in the package: `scripts/bin/` holds
> the delivery pair for the pending-file sink (`notify-alerts.sh`,
> `send-telegram.sh`) and the `tailscale-running.sh` guard, and
> `scripts/gates/secure-bash-argv.sh` fronts a one-shell-string secure-bash
> as the argv prefix `paths.approval_gate` takes — use it where the
> workspace's gate is secure-bash; otherwise keep your deployment's wrapper.
