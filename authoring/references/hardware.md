# Choosing the always-on box

Cranston's engine is tiny — python3 + bash on a 2-minute cron — so almost
anything can run it. The hard requirements are about *posture*, not power:

1. **Always on.** Not "usually on". The monitor that sleeps is the monitor
   that misses the outage.
2. **On your home LAN.** It must reach `192.168.x.x` addresses and must still
   be alive when the internet is down — that's the moment it matters most. A
   cloud VPS structurally cannot do this job: it can't tell "internet down"
   from "house down", and it vanishes from your house exactly when the WAN
   does. (An off-site box has exactly one role here: the dead-man's switch
   that emails you when the whole house goes dark.)
3. **Wired Ethernet, strongly.** The monitor must not share a failure mode
   with the Wi-Fi it watches. The reference deployment is Wi-Fi-only and its
   incident log has three separate wifi-driver episodes to show for it — it
   survives them only because it also watches *its own NIC* and resets it.
   If Wi-Fi is your only option, enable an own-link check; treat it as
   supported-but-degraded.
4. **Survives power blips.** An internal battery (laptop, phone), a UPS, or
   at minimum BIOS "power on after AC loss". And put the **router and modem
   on the UPS too** — a monitor that survives an outage with no network is a
   witness with no phone.

**Electricity rule of thumb:** one watt, 24/7, costs ≈ $1.30/yr at $0.15/kWh
(≈ $2.60 at $0.30). An always-on box costs $5–40/yr; a gaming desktop left on
costs $80–300/yr. Idle watts matter more than peak.

## Workload tiers

| Tier | Runs | Needs |
|---|---|---|
| **A** | the core engine (this repo) | anything POSIX: python3 3.9+, bash, curl, cron, ~100 MB RAM |
| **B** | A + an agent framework (OpenClaw gateway, chat channel) | ~2 GB RAM, Node.js |
| **C** | B + a small local LLM fallback (7–9B quantized) | 16 GB+ RAM, decent CPU or Apple Silicon |

## The 2026 price context

The RAM shortage changed the usual advice. Raspberry Pi raised prices three
times between Dec 2025 and Apr 2026 (a Pi 5 8GB listed at $125+ before the
April increase); new N100/N150 mini PCs rose 40–50% in a year. A fully built
Pi 5 and a fully equipped N-series mini now land in the same ~$250 zone, and
used one-litre business PCs rose only ~17%. The homelab consensus (Jeff
Geerling, r/homelab, ServeTheHome) became: **repurpose used hardware** —
which happens to be this project's whole ethos. Verify current prices before
buying; the numbers below are early-2026.

## Device matrix

| Device | Price (2026) | Idle W | Tiers | Notes |
|---|---|---|---|---|
| **Old laptop** | free | 5–15 | A, B, C with 16GB | The reference deployment's own pattern. Built-in UPS, built-in recovery console. See the laptop guide below. |
| **Pi Zero 2 W** | ~$15 | ~1 | A only | 512MB, Wi-Fi only (USB Ethernet adapter fixes that). Fine as a minimal watchdog; boot from USB, not SD, if you can. |
| **Pi 4 (4–8GB)** | $75–115 | ~3 | A, B | Only worth it if you already own one at 2026 prices. |
| **Pi 5 (+27W PSU +NVMe HAT)** | ~$150–250 built | 3–5 | A, B | Use the official 5V/5A PSU (under-voltage warnings and current-limited USB otherwise) and official storage HAT (third-party NVMe HATs have known flakiness). Never run a server from an SD card. |
| **Used 1-litre PC** (ThinkCentre M720q/M920q, OptiPlex 3070/7070 Micro, EliteDesk 800 G3–G6) | $90–200 | 8–13 | A, B, C slowly with 32GB | The community-consensus value pick: business-grade parts, cheap RAM upgrades, Intel Ethernet. |
| **New N100/N150 mini** (Beelink, GMKtec…) | $170–270 | ~6 on Linux | A, B | Warranty and often dual 2.5GbE; main complaints are no-name SSDs and BIOS quirks. |
| **Ryzen mini (7840HS-class, 32GB)** | $400–600 | ~9 | A, B, C | The x86 pick if you want a usable local model. |
| **Mac mini M4 16GB** | ~$500–600 | 3–6 | A, B, C (best local-LLM perf per watt) | Great hardware, awkward headless recovery — see the Mac notes. Linux-only templates (systemd, sysfs, `ip neigh`) don't apply. |
| **NAS (Synology/QNAP)** | owned | 15–30 w/ disks | A in a container | Run it in a container, not on DSM/QTS directly (updates undo host tweaks). And never make the NAS the monitor of itself. |
| **OpenWrt router + Entware** | owned | +0 | marginal A | Python is heavy for router flash/RAM, and a monitor living on the device most likely to fail can't report that failure. Not recommended. |

**Don't run it on:** a cloud VPS (see rule 2), a gaming desktop (idle watts),
a VM on your daily-use machine (dies on every host reboot/sleep), or the
router/NAS it's supposed to be watching (the watcher must not share fate
with the watched).

## The old-laptop guide (the free pick, and the house favorite)

A retired laptop is the only free option with a built-in UPS and a built-in
screen for when things go really wrong. Three things make it a good citizen:

1. **Lid and sleep:** `HandleLidSwitch=ignore` in
   `/etc/systemd/logind.conf`, disable suspend
   (`systemctl mask sleep.target suspend.target`).
2. **Battery care:** a pack held at 100% for months is a swelling risk, and
   heat accelerates it. Set a charge limit of 60–80% where the kernel exposes
   one (`/sys/class/power_supply/BAT0/charge_control_end_threshold`):
   ThinkPads have the best support (start+stop thresholds), ASUS exposes
   stop-only, Dell needs kernel 6.12+, Framework 6.12+; many consumer brands
   expose nothing — check the BIOS, or accept the risk and inspect
   periodically (a lifting trackpad or deck = stop immediately). The engine's
   `check-host-power.sh` template watches the same sysfs tree — the box can
   monitor its own supply.
3. **Wired Ethernet** (rule 3), and know that most laptops have no "power on
   after AC loss" BIOS switch — the battery mostly makes it moot.

## Mac notes

The engine's core runs on macOS, but the Linux-only templates don't, and
unattended recovery is a chain the reference deployment got burned by: power
returns → Mac powers on (`sudo pmset -a autorestart 1`) → **stops at the
login window**, where login items (and anything that depends on them) stay
dead for hours. Rules: run everything as **LaunchDaemons, not login items or
LaunchAgents**; FileVault blocks headless recovery (decide deliberately);
and test the full power-pull path once.

## UPS quick guide

- Entry: APC Back-UPS BE600M1-class (~$65) — 20–45 min at 10–30W, USB port
  works with NUT/apcupsd for clean shutdown signaling.
- Step up: CyberPower CP850/1000PFCLCD (~$120–140), pure sine wave.
- Router + modem + switch go on the UPS with the monitor. Always.
- Pi/mini-PC "UPS HATs" and pass-through power banks are hit-and-miss — many
  drop output for a moment exactly when wall power fails. Test yours by
  pulling the plug.

## What about an old Android phone?

Tempting — it's free and the battery is a built-in UPS. The honest answer:
**the engine can run under Termux, but stock Android actively fights
always-on background work, and when it loses you lose silently.**

### What works (unrooted Termux)

python3, bash, curl, openssl, dig, `fcntl` locking, crond (via
termux-services), and Android's unprivileged ping. The engine's config has
what Termux needs: set `defaults.gateway_ip` to your router's IP (Android
restricts the route-table read the network gate otherwise uses — the engine
fails open on that, but setting the gateway restores the real
network-down detection), and set `SELFHEAL_ALERT_FILE` somewhere under
`$PREFIX/tmp` (Android has no `/tmp`).

### What fights you

- **The phantom process killer** (Android 12+) kills background child
  processes. Mitigations exist — Android 14+ has a Developer Options toggle
  ("Disable child process restrictions"); 12–13 need an `adb shell settings
  put global settings_enable_monitor_phantom_procs false` — but reports of
  processes still dying persist on some devices/ROMs in 2026.
- **Doze + vendor battery managers.** You need `termux-wake-lock`, battery
  set to Unrestricted, and on Samsung/Xiaomi/Oppo the extra never-sleep
  settings (see dontkillmyapp.com). **OS updates can silently revert these.**
- **No `/sys` guarantees:** the host-power check's sysfs paths are blocked by
  SELinux on many devices (use the Termux:API `termux-battery-status`
  instead — not yet ported). `ip neigh` (the LAN-inventory MAC check) is
  also restricted. systemd/docker templates don't apply.
- **Battery:** a phone charged to 100% for months is the same swelling story
  as the laptop, with fewer escape hatches. Pixels (Android 15+) and Samsung
  (One UI 6.1+ "Battery protection: Maximum") can hold 80%; most older or
  budget phones can't limit at all. An unattended, always-plugged, no-limit
  phone in a closet is not a risk worth taking.

### The recipe (if you do it anyway)

1. Install **Termux, Termux:API and Termux:Boot from F-Droid** (not the Play
   Store build), then `pkg install python bash curl openssl-tool dnsutils
   util-linux cronie termux-services termux-api`.
2. Battery → Unrestricted; enable the phone's charge limit; apply the
   phantom-process mitigation for your Android version; on Samsung, the
   never-sleeping-apps dance.
3. Clone the repo under `$HOME`; set `defaults.gateway_ip` in
   `services.json`; export `SELFHEAL_ALERT_FILE="$PREFIX/tmp/alert.json"` in
   the cron line; skip the systemd/host-power/lan-inventory templates.
4. `sv-enable crond`, crontab:
   `*/2 * * * * flock -n $PREFIX/tmp/cranston.lock python3 $HOME/cranston-skill/engine/selfheal.py >> $HOME/cranston.log 2>&1`
5. A `~/.termux/boot/` script that runs `termux-wake-lock` and starts
   services, so reboots recover.
6. **Non-negotiable:** have something *else* watch the phone (another box
   pinging it, or an off-site dead-man's check). Android's failure mode is
   silent death — the one failure a monitor must never have.

### The trustworthy phone path

If you have a **postmarketOS-supported** old phone, that's the version worth
trusting: real Linux, no phantom killer, no Doze, the engine runs unchanged.
People run genuine 24/7 servers this way; one well-known build removes the
battery entirely and feeds regulated 5V to the charge controller — solving
the swelling problem for good. Device support is the limiting factor.

### Verdict

Old phone as a **secondary** watchdog or a postmarketOS box: yes. Old phone
as the **only** monitor of a home you care about, on stock Android: no —
spend the $90 on a used one-litre PC and give the phone a different job.

**Don't run the full agent stack on a phone** regardless: a long-running
Node gateway is exactly what Android kills, and the monitor dying with the
agent it monitors defeats the whole design.

---

*Sources consulted (early 2026): Raspberry Pi pricing coverage (PCWorld,
Notebookcheck, Gigazine, Jeff Geerling), ServeTheHome TinyMiniMicro series,
minipclab/igor'sLAB N-series reviews, TLP vendor battery docs
(linrunner.de), Termux GitHub issues (#5150, #2366, #377) and
agnostic-apollo's Android docs, dontkillmyapp.com, Samsung/Google battery
documentation, postmarketOS server write-ups (Hackernoon, Hackaday),
r/homelab and r/selfhosted consensus threads. Prices move; check before
buying.*
