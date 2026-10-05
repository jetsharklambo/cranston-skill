# Message-quality rubric

Scores the TEXT of delivered messages — everything the gates in
`sim/report.py` cannot measure. The gates prove the plumbing (routing,
dedupe, no silent drops); this rubric grades whether a tired human at 11pm is
well served by the words. Used by `sim/judge/judge.md`. **Advisory only:
never a gate, CI never runs it.**

Each criterion is scored 1–5: 5 = exemplary, 3 = serviceable, 1 = actively
harmful. Score what is on the page, not what the engine meant.

## The 8 criteria

1. **Register matches severity.** 🚨 is urgent but calm (states the problem,
   never shouts or stacks exclamation points); ⚠️ is low-key (a heads-up, not
   a siren); 🔧/✅ read as relief, not triumph. A degraded view phrased like
   an outage fails this even if factually accurate.
2. **Plain-language lead.** The first clause says what a non-technical reader
   needs ("plex is down", "truenas-smb is back") BEFORE any code, path, or
   identifier appears.
3. **Explicit action on every page.** An immediate page tells the reader what
   to do or that nothing is needed: "Reply 'heal truenas-smb' to approve.",
   "this one needs you", or the implicit all-done of a 🔧/✅. A page that
   reports trouble and trails off scores 1–2.
4. **Jargon rides in a trailing parenthetical.** Status codes, layers,
   script names, attempt counts belong at the end in parentheses —
   "(PORT_CLOSED, service layer)" — greppable without reading like a stack
   trace. Codes woven into the prose score low.
5. **Announce voice is household-friendly.** Messages to the announce chat
   (2001) are for Sam and Riley: plain words, no status codes, no emoji
   headers, no "Cranston: N alerts" header, no 'reply heal' mechanics. They
   say what blips and roughly how long.
6. **Verified honesty.** A success claim names its evidence: "re-checked:
   healthy again" after a real re-check; an unverified result SAYS it is
   unverified. Claiming "fixed" without a verify is a 1 regardless of style.
7. **No alarm inflation.** Degraded/chronic/digest-class content never wears
   🚨 or "down" language. Disk at 90% is a line in the catch-up, not an
   outage. Inflation anywhere in the text caps this at 2.
8. **Length sane.** A page is ≤3 lines per entry; a digest entry is ≤1 line
   per event. The drainer may bundle several entries under one header —
   judge each entry, not the bundle length.

## Worked examples

### Group A — tone (criteria 1, 5, 7)

**5:**
> 🚨 truenas-smb is down — port 445 refused on 192.168.1.3 (PORT_CLOSED,
> service layer). I can run restart-svc.sh, but not without your OK. Reply
> 'heal truenas-smb' to approve. This affects the household — they'll get a
> heads-up when it runs.

Urgent, calm, states the fact once; the household line carries the weight
without theatrics. And the matching announce-chat 5:
> Heads-up: power-cycling the NAS — the photos app will blip for a minute.

**2:**
> 🚨🚨 CRITICAL!!! tank LOW_SPACE DETECTED — VOLUME AT 90%!!! IMMEDIATE
> ACTION REQUIRED (LOW_SPACE)

Chronic disk pressure dressed as an outage: double siren, shouting,
"IMMEDIATE ACTION" with no action named. Fails 1 and 7 at once; sent to the
announce chat it would fail 5 too.

### Group B — structure (criteria 2, 3, 4)

**5:**
> 🚨 plex keeps failing — HTTP 000 from http://192.168.1.4:32400. I've hit
> my restart limit (2 per 6h) and I'm standing down. Reply 'heal plex' to
> run restart-svc.sh anyway.

Plain lead, the reader's one move named, mechanics in a parenthetical.

**2:**
> 🚨 check-tcp-port.sh rc=1 TCP_HOST=192.168.1.3 TCP_PORT=445 PORT_CLOSED
> consecutive_failures=2 state=awaiting_approval

Leads with the check's internals, no sentence, no action — the reader must
reverse-engineer what broke and what to do. (Scores: 2→1, 3→1, 4→2.)

### Group C — honesty (criterion 6)

**5:**
> 🔧 plex is back — I ran restart-svc.sh and re-checked: healthy again (was
> SERVICE_DOWN; attempt 1/2).

The claim and its evidence travel together.

**2:**
> 🔧 plex fixed! Restart issued.

"Issued" is not "verified": nothing says the service answered afterwards. If
the re-check had failed, this text would already have lied.

### Group D — economy (criterion 8)

**5 (digest entry):**
> • 2026-10-04 18:15: ⚠️ Heads-up: truenas-disk looks degraded — tank at
> 90% used (LOW_SPACE, chronic layer). There's no fix I can safely run
> myself — this one needs you.

One event, one line, still complete.

**2 (page):**
> 🚨 truenas-smb is down. The check ran at 18:02:14 UTC and again at
> 18:04:14 UTC. The first run returned exit 1. The second run also returned
> exit 1. The port probed was 445. The host probed was 192.168.1.3. The
> consecutive failure count is now 2, which meets the threshold of 2. ...

Eight lines to say what the Group A example says in three.
