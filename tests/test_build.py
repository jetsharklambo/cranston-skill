#!/usr/bin/env python3
"""Tests for tools/build.py + check_drift.py + validate_links.py.

Runs against a disposable copy of the repo so nothing here touches the real
working tree. Offline, stdlib only.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PASS = 0
FAILED = []


def ok(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def fresh_copy():
    tmp = Path(tempfile.mkdtemp(prefix="cranston-build-test-"))
    dst = tmp / "repo"
    shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", "state"))
    return dst


def run(dst, tool, *args):
    return subprocess.run([sys.executable, str(dst / "tools" / tool), *args],
                          capture_output=True, text=True, cwd=dst)


print("== build: determinism and token rendering ==")
dst = fresh_copy()
r1 = run(dst, "build.py")
ok("build exits 0", r1.returncode == 0, r1.stderr)

root_skill = (dst / "SKILL.md").read_text()
hermes_skill = (dst / "skills" / "cranston" / "SKILL.md").read_text()
ok("root view renders {baseDir}", "{baseDir}/scripts/engine/selfheal.py" in root_skill)
ok("hermes view renders ${HERMES_SKILL_DIR}",
   "${HERMES_SKILL_DIR}/scripts/engine/selfheal.py" in hermes_skill)
ok("no unresolved tokens in either view",
   "{{" not in root_skill and "{{" not in hermes_skill)
ok("hermes frontmatter omits soft env (no required_environment_variables)",
   "required_environment_variables" not in hermes_skill
   and "TG_BOT_TOKEN" not in hermes_skill.split("---")[1])
ok("hermes frontmatter has no openclaw namespace", "openclaw" not in hermes_skill.split("---")[1])
ok("openclaw frontmatter gates only hard bins",
   re.search(r"bins: \[python3, bash, curl\]", root_skill) is not None)
ok("openclaw soft env is required: false",
   "required: false" in root_skill and "requires:\n      env" not in root_skill)
norm = lambda s: s.split("Install, operate,", 1)[1].replace(
    "${HERMES_SKILL_DIR}", "@DIR@").replace("{baseDir}", "@DIR@")
ok("bodies identical after frontmatter+preamble (modulo the dir token)",
   norm(root_skill) == norm(hermes_skill))

# determinism: second run changes nothing
before = sorted((p.relative_to(dst), p.read_bytes())
                for p in dst.rglob("*") if p.is_file() and ".git" not in p.parts)
r2 = run(dst, "build.py")
after = sorted((p.relative_to(dst), p.read_bytes())
               for p in dst.rglob("*") if p.is_file() and ".git" not in p.parts)
ok("second build is a no-op", r2.returncode == 0 and before == after)

print("== build: exec bits and stale cleanup ==")
ok("template kept exec bit",
   (dst / "scripts" / "checks" / "templates" / "check-http.sh").stat().st_mode & 0o111)
stale = dst / "scripts" / "stale-file.sh"
stale.write_text("#!/bin/bash\n")
run(dst, "build.py")
ok("stale file in a generated dir is deleted", not stale.exists())

print("== check_drift ==")
r = run(dst, "check_drift.py")
ok("clean tree passes drift check", r.returncode == 0, r.stdout + r.stderr)
mutated = dst / "skills" / "cranston" / "scripts" / "engine" / "selfheal.py"
orig = mutated.read_text()
mutated.write_text(orig + "\n# hand edit\n")
r = run(dst, "check_drift.py")
ok("hand-edited generated file fails drift check", r.returncode == 1 and "differs" in r.stdout)
mutated.write_text(orig)

print("== validate_links ==")
r = run(dst, "validate_links.py")
ok("fully referenced views pass", r.returncode == 0, r.stdout + r.stderr)
orphan = dst / "references" / "orphan.md"
orphan.write_text("never referenced\n")
r = run(dst, "validate_links.py")
ok("unreferenced bundled file fails", r.returncode == 1 and "orphan.md" in r.stdout)
orphan.unlink()

print("== manifest validation ==")
man = dst / "authoring" / "manifest.yaml"
good = man.read_text()

man.write_text(good + "\nmystery_key: 1\n")
r = run(dst, "build.py")
ok("unknown manifest key rejected", r.returncode != 0 and "unknown key" in r.stderr)

man.write_text(good.replace("name: cranston", "name: Cranston"))
r = run(dst, "build.py")
ok("invalid slug rejected", r.returncode != 0 and "naming rules" in r.stderr)

man.write_text(good.replace("mode: soft", "mode: sorta", 1))
r = run(dst, "build.py")
ok("invalid dependency mode rejected", r.returncode != 0 and "mode must be" in r.stderr)

# a HARD env dep DOES render into Hermes required_environment_variables
man.write_text(good.replace(
    "    - name: TG_BOT_TOKEN\n      mode: soft",
    "    - name: TG_BOT_TOKEN\n      mode: hard"))
r = run(dst, "build.py")
hm = (dst / "skills" / "cranston" / "SKILL.md").read_text()
ok("hard env dep renders in hermes required_environment_variables",
   r.returncode == 0 and "required_environment_variables:" in hm
   and "TG_BOT_TOKEN" in hm.split("---")[1] and "TG_CHAT_ID" not in hm.split("---")[1])

man.write_text(good.replace(
    "description: Self-healing home monitor that fixes, asks first, or digests",
    "description: " + "x" * 61))
r = run(dst, "build.py")
ok("over-long description rejected", r.returncode != 0 and "max 60" in r.stderr)

man.write_text(good)

body = dst / "authoring" / "SKILL.body.md"
body.write_text(body.read_text() + "\nsee {{MYSTERY_TOKEN}}\n")
r = run(dst, "build.py")
ok("unresolved body token rejected", r.returncode != 0 and "unresolved template token" in r.stderr)

print()
if FAILED:
    print(f"{PASS} passed, {len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print(f"ALL {PASS} ASSERTIONS PASSED")
