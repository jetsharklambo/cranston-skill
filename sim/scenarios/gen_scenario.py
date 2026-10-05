#!/usr/bin/env python3
"""gen_scenario.py --seed N [--days D] - deterministic seeded incident traffic
for the casa sim (sim/soak.py --gen-seed N runs this in-process).

Priors come from the reference deployment's lifetime message audit:
  * service crash           ~1 / 12 days per restartable service (plex,
                            pihole-dns via dns_down)
  * host power event        ~1 / 35 days per pluggable host (truenas, plex-mini)
  * gateway blip            1-2 / week, 2-25 min (expect: silence)
  * dns wedge               ~1 / 21 days (restart ineffective -> escalation ->
                            admin_heal 1-6 h later)
  * wan outage              ~1 / 10 days, 30-120 min (degraded digest)
  * flap storm              ~1 / week (plex host or the router)
  * one disk arc            daily +1% fill crossing the 85%-used threshold
  * one cert ladder         warn -> crit -> renewed
  * one tg outage / 90 days with a dns crash inside it (delivery retry proof)
  * truenas-smb ask incident ~1 / 30 days, healed 1-9 h later (announce)

Expect tags are conservative against real engine semantics: "notify" means
page OR digest within the budget (auto-fix notices can be demoted to the
digest by a lingering flap counter - see sim/README.md), "silent" only for
gateway events, "page" only where the engine always pages (a fresh ask).
Collisions are avoided with per-service and global exclusion windows so
coverage gates stay decidable. Deterministic for a given (seed, days).
"""
import argparse
import json
import random


def generate(seed, days=90):
    rng = random.Random(seed)
    events = []        # (day, minute_of_day, dict)
    busy = []          # (day0_abs_min, day1_abs_min) global exclusion windows

    def abs_min(day, mod):
        return (day - 1) * 1440 + mod

    def clash(day, mod, span=0, pad=240):
        a0, a1 = abs_min(day, mod) - pad, abs_min(day, mod) + span + pad
        return any(not (a1 < b0 or b1 < a0) for b0, b1 in busy)

    def reserve(day, mod, span=0, pad=240):
        busy.append((abs_min(day, mod) - pad, abs_min(day, mod) + span + pad))

    def tstr(day, mod):
        return f"D{day:02d} {mod // 60:02d}:{mod % 60:02d}"

    def add(day, mod, ev, span=0, pad=240, **kw):
        if day < 1 or day > days or clash(day, mod, span, pad):
            return False
        reserve(day, mod, span, pad)
        e = {"t": tstr(day, mod), "ev": ev}
        e.update(kw)
        events.append((abs_min(day, mod), e))
        return True

    def slot(d0, d1):
        # mod capped at 20:00 so a same-day dur_min <=3h never crosses midnight
        return rng.randint(max(1, d0), min(days, d1)), rng.randint(6 * 60, 20 * 60)

    # one disk arc (reserve its daily 03:00 slots implicitly - they never clash:
    # pad 0, they are instant table writes)
    if days >= 20:
        start = rng.randint(5, max(6, days // 3))
        fill = 70
        d = start
        while d <= days and fill <= 97:
            add(d, 180, "disk_fill", pad=0, vol="tank", pct=fill,
                **({"expect": "notify", "within_min": 1740,
                    "id": f"disk-crossing-s{seed}"} if fill == 86 else {}))
            fill += 1
            d += 1

    # one cert ladder
    if days >= 25:
        d = rng.randint(8, days - 12)
        add(d, 600, "cert_set", name="truenas", days=13, expect="notify",
            within_min=1740, id=f"cert-warn-s{seed}")
        add(d + 5, 610, "cert_set", name="truenas", days=6, expect="notify",
            within_min=1560, id=f"cert-crit-s{seed}")
        add(d + 7, 620, "cert_set", name="truenas", days=364, expect="notify",
            within_min=60, id=f"cert-renewed-s{seed}")

    # one tg outage with a dns crash inside it
    if days >= 15:
        for _ in range(40):
            d, mod = slot(5, days - 2)
            if 5 * 60 <= mod <= 6 * 60:     # never span the 05:25 flush slot
                continue
            dur = rng.randint(25, 55)
            if add(d, mod, "tg_mode", span=dur + 120, mode="500", dur_min=dur,
                   id=f"tg-outage-s{seed}"):
                events.append((abs_min(d, mod + 4),
                               {"t": tstr(d, mod + 4), "ev": "dns_down",
                                "expect": "notify", "within_min": dur + 20,
                                "id": f"dns-in-tg-window-s{seed}"}))
                break

    # weekly-ish recurring traffic
    n_weeks = max(1, days // 7)
    for w in range(n_weeks):
        # 1-2 gateway blips
        for _ in range(rng.randint(1, 2)):
            for _ in range(30):
                d, mod = slot(w * 7 + 1, w * 7 + 7)
                dur = rng.randint(2, 25)
                if add(d, mod, "gw_down", span=dur, pad=300, dur_min=dur,
                       expect="silent", id=f"gw-blip-w{w}-s{seed}"):
                    break
        # one flap storm (router storms are silent; plex-host storms notify)
        for _ in range(30):
            d, mod = slot(w * 7 + 1, w * 7 + 7)
            host = rng.choice(["192.168.1.1", "192.168.1.4"])
            n = rng.choice([4, 6, 8])
            if add(d, mod, "flap", span=n * 5 + 20, pad=300, host=host, n=n,
                   period_min=5,
                   **({"expect": "silent", "id": f"router-flap-w{w}-s{seed}"}
                      if host == "192.168.1.1"
                      else {"expect": "notify", "id": f"plex-flap-w{w}-s{seed}"})):
                break

    # poisson-ish singles over the whole span
    def sprinkle(n_mean, make):
        n = max(0, round(rng.gauss(n_mean, max(0.5, n_mean / 4))))
        for _ in range(n):
            for _ in range(40):
                d, mod = slot(2, days - 1)
                if make(d, mod):
                    break

    sprinkle(days / 12, lambda d, m: add(
        d, m, "svc_down", svc="plex", expect="notify", within_min=20,
        id=f"plex-crash-D{d}-s{seed}"))
    sprinkle(days / 12, lambda d, m: add(
        d, m, "dns_down", expect="notify", within_min=20,
        id=f"dns-crash-D{d}-s{seed}"))
    sprinkle(days / 35, lambda d, m: add(
        d, m, "host_down", span=60, host="192.168.1.3",
        dur_min=rng.randint(20, 60), expect="notify", within_min=20,
        id=f"nas-power-D{d}-s{seed}"))
    sprinkle(days / 35, lambda d, m: add(
        d, m, "host_down", span=90, host="192.168.1.4",
        dur_min=rng.randint(20, 90), expect="notify", within_min=20,
        id=f"plexmini-power-D{d}-s{seed}"))
    sprinkle(days / 10, lambda d, m: add(
        d, m, "wan_down", span=120, dur_min=rng.randint(30, 120),
        expect="notify", within_min=1740, id=f"wan-D{d}-s{seed}"))

    # dns wedges: wedge -> unwedge -> admin heal (escalation exercised)
    def wedge(d, m):
        lag = rng.randint(120, 360)
        if abs_min(d, m) + lag > abs_min(days, 20 * 60):
            return False  # the heal must land before the end marker
        if not add(d, m, "dns_wedge", span=lag + 60, pad=360, expect="notify",
                   within_min=20, id=f"dns-wedge-D{d}-s{seed}"):
            return False
        events.append((abs_min(d, m) + lag - 10,
                       {"t": tstr(d + (m + lag - 10) // 1440, (m + lag - 10) % 1440),
                        "ev": "dns_ok", "id": f"dns-unwedge-D{d}-s{seed}"}))
        events.append((abs_min(d, m) + lag,
                       {"t": tstr(d + (m + lag) // 1440, (m + lag) % 1440),
                        "ev": "admin_heal", "key": "pihole-dns",
                        "expect": "heal-ok", "id": f"dns-heal-D{d}-s{seed}"}))
        return True
    sprinkle(days / 21, wedge)

    # ask-first incidents: nas service dies, admin heals hours later
    def ask(d, m):
        lag = rng.randint(60, 540)
        if abs_min(d, m) + lag > abs_min(days, 20 * 60):
            return False  # the heal must land before the end marker
        if not add(d, m, "svc_down", span=lag + 60, pad=360, svc="truenas-smb",
                   expect="page", within_min=20, id=f"nas-down-D{d}-s{seed}"):
            return False
        events.append((abs_min(d, m) + lag,
                       {"t": tstr(d + (m + lag) // 1440, (m + lag) % 1440),
                        "ev": "admin_heal", "key": "truenas-smb",
                        "expect": "heal-ok", "id": f"nas-heal-D{d}-s{seed}"}))
        return True
    sprinkle(days / 30, ask)

    events.sort(key=lambda p: p[0])
    lines = [json.dumps({"meta": True, "name": f"gen-seed{seed}-{days}d",
                         "gates": "invariants", "seed": seed, "days": days})]
    lines += [json.dumps(e, ensure_ascii=False) for _, e in events]
    lines.append(json.dumps({"t": f"D{days:02d} 23:00", "ev": "end",
                             "expect": "all-clear",
                             "except": ["truenas-disk"] if days >= 20 else []}))
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default="-")
    args = ap.parse_args()
    lines = generate(args.seed, args.days)
    if args.out == "-":
        print("\n".join(lines))
    else:
        with open(args.out, "w") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
