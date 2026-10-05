# Judge protocol

How the Claude session grades the drill's delivered messages against
`sim/judge/rubric.md`. **ADVISORY ONLY.** This is never a gate: no exit code,
no threshold, CI never runs it. The gates live in `sim/report.py`; the judge
exists to catch the failures a regex cannot — tone, honesty, inflation.

## Input

`judge-input.jsonl`, emitted by
`sim/report.py --emit-judge-input <file>` during the traffic phase
(the cloud drill leaves it at `$DRILL_HOME/judge-input.jsonl`). One JSON
object per delivered message:

```json
{"text": "...", "chat": "1001", "header": "🚨 Cranston: 2 alerts",
 "severity_class": "page", "incident_id": "c03-nas-down"}
```

- `text` — the message body as delivered
- `chat` — destination chat id (1001 = admin pages, 2001 = household announce)
- `header` — the drainer header the text rode under, if any
- `severity_class` — the reporter's classification (page / announce / digest / recovery)
- `incident_id` — the scenario event id it was matched to, when matched

## Procedure

1. **Deduplicate.** Score each UNIQUE `text` once (realert renewals repeat
   the same text by design; re-scoring them measures nothing). Keep the
   count of occurrences — it goes in the table.
2. **Score all 8 criteria** from the rubric for every unique text, 1–5 each.
   Judge the text as its reader sees it: a 2001 message is judged as a
   household announcement (criterion 5 applies hard; criterion 3 does not —
   the household is not asked to act); a digest bundle is judged entry by
   entry under criterion 8.
3. **Mark N/A honestly.** A criterion that cannot apply (e.g. criterion 5 on
   an admin page) is N/A, not 5. Averages use scored criteria only.
4. **Quote, don't paraphrase**, in every finding: the judged words verbatim.

## Output: judge-report.md

Write `judge-report.md` (under `$DRILL_HOME/` or the session report) with:

1. **Per-message table** — one row per unique text:
   `| # | chat | class | occurrences | c1..c8 (or –) | mean | text (first 60 chars) |`
2. **The 5 worst messages** — lowest mean first; for each: the full text
   quoted, which criteria it failed and why (one sentence each), and a
   **one-line fix** (the rewritten message, ready to ship).
3. **Overall grade A–F** — judge's holistic call across the drill's traffic,
   stated with one sentence of reasoning. Guide: A = nothing below 3 and the
   asks/announces are 5s; C = serviceable but tonal misses; F = any dishonest
   success claim or alarm inflation on the announce channel.
4. **3 systemic observations** — patterns, not instances: the kind of finding
   that should flow back into the authoring templates (e.g. "every escalation
   page buries the action behind the flap suffix"), each tied to the quoted
   evidence above.

## Standing rules

- The judge never edits repository files, templates, or configs — findings
  are for the report.
- A gate breach found while judging (a message in the wrong chat, a silent
  drop) is NOTED and attributed to the gates ("report.py should have caught
  this"), not folded into the style scores.
- If `judge-input.jsonl` is missing, judge straight from the phone log
  (`phone.jsonl`: `{"path","chat_id","text"}` per send) and say the
  classification columns are the judge's own inference.
