#!/usr/bin/env python3
"""interview-check.py <services.json> - grade the interview phase's output.

Reads the services.json that Dana's interview session filled (phase 4 of
sim/CLOUD-TEST.md) and asserts the config landed where
sim/cloud/persona-admin.md says it should. One PASS/FAIL line per assertion;
exit 1 on any FAIL, 2 on an unreadable config. Read-only: it never writes.

What it asserts (the contract, not the phrasing):
  - truenas-smb: ask-class dict remediation(s) with consent "household" and
    a non-empty announce text
  - plex: SERVICE_DOWN is auto-class (plain string, or dict-auto {"auto": ...})
  - pihole-dns: DNS_DOWN is auto-class
  - truenas-disk: LOW_SPACE routed notify_by_code "digest" with
    realert_minutes_by_code 1440
  - consent_notes on truenas-smb quotes Dana's concern (photos/blip)
  - _interview provenance maps (interview.py's apply_changes writes one per
    service it touched) present and non-empty on the interviewed services

Stdlib only; python3.9+.
"""

import json
import sys

FAILS = 0


def report(name, cond, detail=""):
    global FAILS
    if cond:
        print(f"PASS {name}")
    else:
        FAILS += 1
        print(f"FAIL {name}" + (f" - {detail}" if detail else ""))


def is_auto(entry):
    """Auto class the engine's way: a plain string, or the dict-auto form
    {"auto": ...} without "ask" (selfheal.py step())."""
    if isinstance(entry, str) and entry:
        return True
    return (isinstance(entry, dict) and isinstance(entry.get("auto"), str)
            and "ask" not in entry)


def main():
    if len(sys.argv) != 2:
        print("usage: interview-check.py <services.json>", file=sys.stderr)
        return 64
    try:
        with open(sys.argv[1]) as f:
            config = json.load(f)
    except Exception as e:
        print(f"FAIL config unreadable - {sys.argv[1]}: {e}")
        return 2

    svcs = {s.get("name"): s for s in config.get("services", []) if isinstance(s, dict)}
    for name in ("truenas-smb", "plex", "pihole-dns", "truenas-disk"):
        report(f"service {name} present", name in svcs)
    if FAILS:
        print(f"\ninterview-check: {FAILS} FAIL(s) - config is missing casa services; "
              f"was phase 2's casa config ever installed?")
        return 1

    # -- truenas-smb: ask-first, household consent, announce ------------------
    nas = svcs["truenas-smb"]
    rem = nas.get("remediations") or {}
    asks = {c: v for c, v in rem.items() if isinstance(v, dict) and "ask" in v}
    report("truenas-smb has at least one ask-class remediation", bool(asks),
           f"remediations: {json.dumps(rem)}")
    report("truenas-smb has NO auto-class remediation (never act unasked)",
           not any(is_auto(v) for v in rem.values()),
           f"remediations: {json.dumps(rem)}")
    for code, v in sorted(asks.items()):
        report(f"truenas-smb {code} consent is 'household'",
               v.get("consent") == "household", f"consent={v.get('consent')!r}")
        report(f"truenas-smb {code} announce text is non-empty",
               isinstance(v.get("announce"), str) and v["announce"].strip() != "",
               f"announce={v.get('announce')!r} (hand-set in the casa config; "
               f"the gappy edit must not have stripped it)")

    # -- consent_notes quotes Dana's concern ----------------------------------
    notes = nas.get("consent_notes")
    report("truenas-smb consent_notes present (Dana's words, verbatim tail of her consent answer)",
           isinstance(notes, str) and notes.strip() != "", f"consent_notes={notes!r}")
    if isinstance(notes, str):
        low = notes.lower()
        report("consent_notes quotes the photos/blip concern",
               ("photo" in low) or ("blip" in low),
               f"consent_notes={notes!r} - persona-admin.md tells Dana to keep "
               f"'photos'/'blip' in her consent answer")

    # -- plex: SERVICE_DOWN auto ----------------------------------------------
    plex_rem = (svcs["plex"].get("remediations") or {})
    report("plex SERVICE_DOWN is auto-class", is_auto(plex_rem.get("SERVICE_DOWN")),
           f"SERVICE_DOWN={json.dumps(plex_rem.get('SERVICE_DOWN'))}")

    # -- pihole-dns: DNS_DOWN auto ---------------------------------------------
    pi_rem = (svcs["pihole-dns"].get("remediations") or {})
    report("pihole-dns DNS_DOWN is auto-class", is_auto(pi_rem.get("DNS_DOWN")),
           f"DNS_DOWN={json.dumps(pi_rem.get('DNS_DOWN'))}")

    # -- truenas-disk: LOW_SPACE to the digest, daily --------------------------
    disk = svcs["truenas-disk"]
    report("truenas-disk notify_by_code.LOW_SPACE = 'digest'",
           (disk.get("notify_by_code") or {}).get("LOW_SPACE") == "digest",
           f"notify_by_code={json.dumps(disk.get('notify_by_code'))}")
    report("truenas-disk realert_minutes_by_code.LOW_SPACE = 1440",
           (disk.get("realert_minutes_by_code") or {}).get("LOW_SPACE") == 1440,
           f"realert_minutes_by_code={json.dumps(disk.get('realert_minutes_by_code'))}")

    # -- provenance: the interview wrote these, and says so ---------------------
    # apply_changes() in onboard/interview.py records every fill in the
    # service's "_interview" map (dotted path -> {at, why, value}); the fields
    # the interview filled on these two services must carry it.
    for name in ("truenas-smb", "truenas-disk"):
        prov = svcs[name].get("_interview")
        report(f"{name} carries a non-empty _interview provenance map",
               isinstance(prov, dict) and bool(prov),
               "the interview's fill --apply writes it; a hand edit would not")
    carried = sorted(n for n, s in svcs.items()
                     if isinstance(s.get("_interview"), dict) and s["_interview"])
    print(f"info: services with _interview provenance: {', '.join(carried) or 'none'}")

    print()
    if FAILS:
        print(f"interview-check: {FAILS} FAIL(s)")
        return 1
    print("interview-check: all assertions pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
