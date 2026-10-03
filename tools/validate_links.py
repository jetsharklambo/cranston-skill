#!/usr/bin/env python3
"""Validate that each distribution view is self-contained and fully referenced.

Two gates, per the Agent Skills guidance that installers may copy only what
SKILL.md references:
  1. Every path-like reference in a view's SKILL.md resolves to a real file
     or directory inside that view.
  2. Every bundled file is covered: referenced directly, or under a directory
     that SKILL.md references.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

REPO = build.REPO
# path-ish tokens that live under the packaged top-level dirs, after the
# platform dir token has been stripped. A bare top-dir mention ("scripts/")
# is prose, not a reference — it must never count as covering everything.
REF_RE = re.compile(r"(?:\{baseDir\}/|\$\{HERMES_SKILL_DIR\}/)?"
                    r"((?:scripts|references|assets)/[A-Za-z0-9_./-]*)")
BARE = {"scripts", "references", "assets"}


def check_view(root, label):
    errs = []
    text = (root / "SKILL.md").read_text()
    refs = set()
    for mt in REF_RE.finditer(text):
        r = mt.group(1).rstrip("./")
        if r not in BARE:
            refs.add(r)
    missing = [r for r in sorted(refs) if not (root / r).exists()]
    for r in missing:
        errs.append(f"  {label}/SKILL.md references missing path: {r}")
    covered_dirs = [r for r in refs if (root / r).is_dir()]
    for top in ("scripts", "references", "assets"):
        base = root / top
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_dir():
                continue
            rel = str(p.relative_to(root))
            if rel in refs or any(rel.startswith(d + "/") for d in covered_dirs):
                continue
            errs.append(f"  unreferenced file in {label}: {rel}")
    return errs


def main():
    m = build.load_manifest()
    slug = m["skill"]["name"]
    errs = check_view(REPO, ".") + check_view(REPO / "skills" / slug, f"skills/{slug}")
    if errs:
        print("validate_links.py: FAILED")
        print("\n".join(errs))
        sys.exit(1)
    print("validate_links.py: all references resolve; all bundled files covered")


if __name__ == "__main__":
    main()
