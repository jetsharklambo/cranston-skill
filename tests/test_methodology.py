#!/usr/bin/env python3
"""Guard tests for the onboarding methodology modules (phase 4).

Cheap structural checks so the modules, the skill body, and the templates
they point at cannot drift apart silently. Offline, stdlib only.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REF = REPO / "authoring" / "references"
PASS = 0
FAILED = []


def ok(cond, name, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


discovery = (REF / "discovery.md").read_text()
interview = (REF / "interview.md").read_text()
failure = (REF / "failure-modes.md").read_text()
doctrine = (REF / "doctrine.md").read_text()
gates = (REF / "approval-gates.md").read_text()
body = (REPO / "authoring" / "SKILL.body.md").read_text()
index = (REF / "methodology.md").read_text()

print("== module ids are all present ==")
ok(all(re.search(rf"\bD{i}\b", discovery) for i in range(1, 10)),
   "discovery.md carries D1-D9")
ok(all(re.search(rf"\bU{i}\b", interview) for i in range(1, 18)),
   "interview.md carries U1-U17")
ok(all(re.search(rf"\bF{i}\b", failure) for i in range(1, 21)),
   "failure-modes.md carries F1-F20")

print("== every failure mode is honestly mapped ==")
sections = re.split(r"^## F\d+\.", failure, flags=re.M)[1:]
ok(len(sections) == 20, "exactly 20 F-sections", f"got {len(sections)}")
unmapped = [i + 1 for i, s in enumerate(sections)
            if "../scripts/" not in s and "approval-gates.md" not in s
            and "audit manually" not in s.lower()
            and "schema" not in s.lower() and "engine" not in s.lower()]
ok(not unmapped, "each F-entry names a shipped artifact or says 'audit manually'",
   f"unmapped: {unmapped}")

print("== cross-links resolve ==")
def links_resolve(text, base, label):
    bad = []
    for m in re.finditer(r"\]\((\.\./[A-Za-z0-9_./-]+|[A-Za-z0-9_-]+\.md)(#[A-Za-z0-9_-]+)?\)", text):
        target = (base / m.group(1)).resolve()
        if not target.exists():
            bad.append(m.group(1))
    ok(not bad, f"{label}: markdown links resolve", f"broken: {bad}")

links_resolve(discovery, REF, "discovery.md")
links_resolve(interview, REF, "interview.md")
links_resolve(failure, REF, "failure-modes.md")
links_resolve(doctrine, REF, "doctrine.md")

print("== doctrine sections cite interview questions ==")
ok(len(re.findall(r"\bU\d{1,2}\b", doctrine)) >= 10,
   "doctrine template cites U-questions throughout")

print("== the skill body wires the flow ==")
for name in ("discovery.md", "interview.md", "failure-modes.md", "doctrine.md",
             "approval-gates.md", "methodology.md"):
    ok(name in body, f"SKILL body references {name}")
ok("Onboard a home" in body, "SKILL body has the onboarding procedure")
ok("DRAFT" in body or "draft" in body, "onboarding emits drafts, never auto-applies")

print("== index and hygiene ==")
for name in ("discovery.md", "interview.md", "failure-modes.md", "doctrine.md",
             "approval-gates.md"):
    ok(name in index, f"methodology.md indexes {name}")
for label, text in (("discovery", discovery), ("interview", interview),
                    ("failure-modes", failure), ("doctrine", doctrine)):
    ok("{{" not in text.replace("{{SKILL_DIR}}", ""),
       f"{label}: no unresolved template tokens")
    ok("Homestead" not in text, f"{label}: no working-title leakage")

print()
if FAILED:
    print(f"{PASS} passed, {len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print(f"ALL {PASS} ASSERTIONS PASSED")
