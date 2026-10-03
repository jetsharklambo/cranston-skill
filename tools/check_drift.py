#!/usr/bin/env python3
"""Fail if the committed distribution views differ from a fresh build.

CI gate: a pull request cannot modify only one platform copy, and generated
files cannot be hand-edited without the build catching it.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

REPO = build.REPO


def committed_tree(dest, managed_dirs):
    tree = {}
    skill_md = dest / "SKILL.md"
    if skill_md.exists():
        tree["SKILL.md"] = (skill_md.read_bytes(), False)
    for d in managed_dirs:
        base = dest / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_dir() or any(part in build.SKIP_NAMES for part in p.parts) \
               or p.suffix == ".pyc":
                continue
            tree[str(p.relative_to(dest))] = (p.read_bytes(), os.access(p, os.X_OK))
    return tree


def diff(want, have, label):
    bad = []
    for rel in sorted(set(want) | set(have)):
        if rel not in have:
            bad.append(f"  missing:  {label}/{rel}")
        elif rel not in want:
            bad.append(f"  stale:    {label}/{rel}")
        elif want[rel][0] != have[rel][0]:
            bad.append(f"  differs:  {label}/{rel}")
        elif want[rel][1] != have[rel][1]:
            bad.append(f"  exec-bit: {label}/{rel}")
    return bad


def main():
    m = build.load_manifest()
    slug = m["skill"]["name"]
    managed = ("scripts", "references", "assets")
    bad = diff(build.collect_tree("openclaw", m), committed_tree(REPO, managed), ".")
    bad += diff(build.collect_tree("hermes", m),
                committed_tree(REPO / "skills" / slug, managed), f"skills/{slug}")
    if bad:
        print("check_drift.py: committed views are out of sync with authoring/ "
              "- run: python3 tools/build.py")
        print("\n".join(bad))
        sys.exit(1)
    print("check_drift.py: generated views match authoring/ exactly")


if __name__ == "__main__":
    main()
