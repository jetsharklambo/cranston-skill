> **OpenClaw notes.** Run dangerous operations through the deployment's
> gated path (secure-bash or equivalent) exactly as the workspace's own
> rules demand — this skill never exempts you from the 2FA gate. The
> OpenClaw-native runtime shims (an argv gate wrapper, a Tailscale guard,
> and a delivery sender for the pending-file sink) ship in a future release;
> until then wire `paths.alert_sink` to your own sender as shown below, and
> leave `paths.approval_gate` pointing at your deployment's gate wrapper.
