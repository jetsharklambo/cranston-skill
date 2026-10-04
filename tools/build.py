#!/usr/bin/env python3
"""Deterministic generator for the two distribution views.

Reads authoring/ (the only hand-edited source) and materializes:
  - the OpenClaw root artifact:  SKILL.md + scripts/ + references/ + assets/
  - the Hermes tap artifact:     skills/<slug>/{SKILL.md,scripts,references,assets}

Stdlib only. The manifest is parsed by a deliberately strict YAML-subset
loader (maps, lists of scalars or flat maps, plain/quoted scalars, comments);
anything fancier is a build error — the manifest is ours, not user input.

Determinism contract: building twice produces byte-identical trees (asserted
on every run), file order is sorted, frontmatter key order is fixed, and the
generated areas are wholesale-replaced so stale files cannot survive.
"""
import os
import shutil
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AUTH = REPO / "authoring"

SLUG_OK = "abcdefghijklmnopqrstuvwxyz0123456789-"
MODES = {"hard", "soft", "setup"}
PLATFORMS = {"linux", "macos", "windows"}
SKIP_NAMES = {"__pycache__", ".DS_Store"}

TOP_KEYS = {"schema_version", "skill", "platforms", "dependencies", "hermes", "openclaw"}
SKILL_KEYS = {"name", "description", "license", "compatibility", "author", "version", "homepage"}
DEP_KEYS = {"bins", "env"}
BIN_KEYS = {"name", "mode", "required_for"}
ENV_KEYS = {"name", "mode", "prompt", "help", "required_for"}
HERMES_KEYS = {"tags", "requires_toolsets", "requires_tools",
               "fallback_for_toolsets", "fallback_for_tools", "config"}
OPENCLAW_KEYS = {"user_invocable", "disable_model_invocation",
                 "command_dispatch", "command_tool", "installers"}


def die(msg):
    sys.exit(f"build.py: ERROR: {msg}")


# ---- strict YAML-subset loader ----------------------------------------------
def parse_scalar(s):
    s = s.strip()
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return s[1:-1]
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        return s


def load_yaml_subset(path):
    lines = []
    for raw in path.read_text().splitlines():
        if raw.strip().startswith("#") or not raw.strip():
            continue
        if "\t" in raw:
            die(f"{path.name}: tabs are not allowed")
        lines.append(raw)

    def parse_block(i, indent):
        """Parse at `indent`; returns (obj, next_index)."""
        obj = None
        while i < len(lines):
            line = lines[i]
            cur = len(line) - len(line.lstrip())
            if cur < indent:
                break
            if cur > indent:
                die(f"{path.name}: unexpected indent at: {line.strip()!r}")
            text = line.strip()
            if text.startswith("- "):
                if obj is None:
                    obj = []
                if not isinstance(obj, list):
                    die(f"{path.name}: mixed list/map at: {text!r}")
                item = text[2:]
                if ":" in item and not item.strip().startswith('"'):
                    # flat map item: "- name: python3" + following deeper keys
                    k, _, v = item.partition(":")
                    d = {k.strip(): parse_scalar(v)}
                    i += 1
                    while i < len(lines):
                        nxt = lines[i]
                        ni = len(nxt) - len(nxt.lstrip())
                        if ni <= indent or nxt.strip().startswith("- "):
                            break
                        k2, sep, v2 = nxt.strip().partition(":")
                        if not sep:
                            die(f"{path.name}: expected key: value in: {nxt.strip()!r}")
                        d[k2.strip()] = parse_scalar(v2)
                        i += 1
                    obj.append(d)
                else:
                    obj.append(parse_scalar(item))
                    i += 1
                continue
            k, sep, v = text.partition(":")
            if not sep:
                die(f"{path.name}: expected key: value in: {text!r}")
            if obj is None:
                obj = {}
            if not isinstance(obj, dict):
                die(f"{path.name}: mixed map/list at: {text!r}")
            k = k.strip()
            if k in obj:
                die(f"{path.name}: duplicate key {k!r}")
            if v.strip():
                obj[k] = parse_scalar(v)
                i += 1
            else:
                child, i = parse_block(i + 1, indent_of(i + 1, lines, indent))
                obj[k] = child
        return obj if obj is not None else {}, i

    def indent_of(i, lines, parent_indent):
        if i >= len(lines):
            die(f"{path.name}: key with no value at end of file")
        ind = len(lines[i]) - len(lines[i].lstrip())
        if ind <= parent_indent:
            die(f"{path.name}: empty block under a key")
        return ind

    obj, i = parse_block(0, 0)
    if i != len(lines):
        die(f"{path.name}: trailing content at: {lines[i].strip()!r}")
    return obj


# ---- manifest validation -----------------------------------------------------
def check_keys(d, allowed, where):
    unknown = set(d) - allowed
    if unknown:
        die(f"unknown key(s) {sorted(unknown)} in {where}")


def load_manifest():
    m = load_yaml_subset(AUTH / "manifest.yaml")
    check_keys(m, TOP_KEYS, "manifest root")
    if m.get("schema_version") != 1:
        die("schema_version must be 1")
    sk = m.get("skill") or {}
    check_keys(sk, SKILL_KEYS, "skill")
    for req in ("name", "description", "license", "version", "author"):
        if not sk.get(req):
            die(f"skill.{req} is required")
    name = sk["name"]
    if not (1 <= len(name) <= 64) or any(c not in SLUG_OK for c in name) \
       or name.startswith("-") or name.endswith("-") or "--" in name:
        die(f"skill.name {name!r} violates Agent Skills naming rules")
    if len(sk["description"]) > 60:
        die(f"skill.description is {len(sk['description'])} chars (max 60)")
    for p in m.get("platforms") or []:
        if p not in PLATFORMS:
            die(f"unknown platform {p!r}")
    deps = m.get("dependencies") or {}
    check_keys(deps, DEP_KEYS, "dependencies")
    seen = set()
    for kind, keys in (("bins", BIN_KEYS), ("env", ENV_KEYS)):
        for d in deps.get(kind) or []:
            check_keys(d, keys, f"dependencies.{kind}")
            if not d.get("name"):
                die(f"dependencies.{kind} entry without a name")
            if d.get("mode") not in MODES:
                die(f"dependency {d['name']}: mode must be one of {sorted(MODES)}")
            if (kind, d["name"]) in seen:
                die(f"duplicate dependency {d['name']}")
            seen.add((kind, d["name"]))
    check_keys(m.get("hermes") or {}, HERMES_KEYS, "hermes")
    check_keys(m.get("openclaw") or {}, OPENCLAW_KEYS, "openclaw")
    oc = m.get("openclaw") or {}
    if oc.get("disable_model_invocation") and not oc.get("user_invocable"):
        die("openclaw: disabling model invocation on a non-user-invocable skill "
            "makes it uninvokable")
    return m


# ---- frontmatter emission ----------------------------------------------------
def q(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    if s == "" or s != s.strip() or any(c in s for c in ':#{}[]&*?|>%@`"\''):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def ylist(vals):
    return "[" + ", ".join(q(v) for v in vals) + "]"


HERMES_OS = {"linux": "linux", "macos": "macos", "windows": "windows"}


def hermes_frontmatter(m):
    sk, deps = m["skill"], m.get("dependencies") or {}
    h = m.get("hermes") or {}
    out = ["---",
           f"name: {q(sk['name'])}",
           f"description: {q(sk['description'])}",
           f"license: {q(sk['license'])}"]
    if sk.get("compatibility"):
        out.append(f"compatibility: {q(sk['compatibility'])}")
    out.append(f"version: {q(sk['version'])}")
    out.append(f"author: {q(sk['author'])}")
    plats = m.get("platforms") or []
    if plats:
        out.append(f"platforms: {ylist(HERMES_OS[p] for p in plats)}")
    meta = []
    if h.get("tags"):
        meta.append(f"    tags: {ylist(h['tags'])}")
    if h.get("requires_toolsets"):
        meta.append(f"    requires_toolsets: {ylist(h['requires_toolsets'])}")
    if h.get("requires_tools"):
        meta.append(f"    requires_tools: {ylist(h['requires_tools'])}")
    if meta:
        out.append("metadata:")
        out.append("  hermes:")
        out.extend(meta)
    # Hermes may prompt for declared variables at load time, so only HARD env
    # dependencies belong in required_environment_variables. Soft env (e.g.
    # the example sink's Telegram vars) is documented in the body instead -
    # declaring it here made an optional convenience read as a requirement.
    envs = [e for e in (deps.get("env") or []) if e.get("mode") == "hard"]
    if envs:
        out.append("required_environment_variables:")
        for e in envs:
            out.append(f"  - name: {q(e['name'])}")
            if e.get("prompt"):
                out.append(f"    prompt: {q(e['prompt'])}")
            if e.get("help"):
                out.append(f"    help: {q(e['help'])}")
            if e.get("required_for"):
                out.append(f"    required_for: {q(e['required_for'])}")
    out.append("---")
    return "\n".join(out) + "\n"


def openclaw_frontmatter(m):
    sk, deps = m["skill"], m.get("dependencies") or {}
    oc = m.get("openclaw") or {}
    out = ["---",
           f"name: {q(sk['name'])}",
           f"description: {q(sk['description'])}",
           f"license: {q(sk['license'])}"]
    if sk.get("compatibility"):
        out.append(f"compatibility: {q(sk['compatibility'])}")
    if sk.get("homepage"):
        out.append(f"homepage: {q(sk['homepage'])}")
    if oc.get("user_invocable") is not None:
        out.append(f"user-invocable: {q(bool(oc['user_invocable']))}")
    if oc.get("disable_model_invocation"):
        out.append("disable-model-invocation: true")
    out.append("metadata:")
    out.append(f"  author: {q(sk['author'])}")
    out.append(f'  version: "{sk["version"]}"')
    hard_bins = [b["name"] for b in (deps.get("bins") or []) if b["mode"] == "hard"]
    soft_env = [e for e in (deps.get("env") or []) if e["mode"] == "soft"]
    hard_env = [e for e in (deps.get("env") or []) if e["mode"] == "hard"]
    if hard_bins or soft_env or hard_env:
        out.append("  openclaw:")
        if hard_bins or hard_env:
            out.append("    requires:")
            if hard_bins:
                out.append(f"      bins: {ylist(hard_bins)}")
            if hard_env:
                out.append(f"      env: {ylist(e['name'] for e in hard_env)}")
        if soft_env:
            out.append("    envVars:")
            for e in soft_env:
                out.append(f"      - name: {q(e['name'])}")
                out.append("        required: false")
                if e.get("required_for"):
                    out.append(f"        description: {q('Used for ' + e['required_for'] + '.')}")
    out.append("---")
    return "\n".join(out) + "\n"


# ---- body assembly -----------------------------------------------------------
TOKENS = {"hermes": "${HERMES_SKILL_DIR}", "openclaw": "{baseDir}"}


def render_body(target):
    body = (AUTH / "SKILL.body.md").read_text()
    pre = (AUTH / "adapters" / f"{target}.preamble.md").read_text().rstrip()
    body = body.replace("{{SKILL_DIR}}", TOKENS[target])
    if "{{" in body:
        tok = body[body.index("{{"):body.index("{{") + 40]
        die(f"unresolved template token near: {tok!r}")
    # preamble goes right under the H1
    lines = body.splitlines()
    if not lines or not lines[0].startswith("# "):
        die("SKILL.body.md must start with an H1 title")
    return "\n".join([lines[0], "", pre] + lines[1:]) + ("\n" if not body.endswith("\n") else "")


# ---- tree building -------------------------------------------------------------
def collect_tree(target, m):
    """Return {relpath: (bytes, is_exec)} for one distribution view."""
    front = hermes_frontmatter(m) if target == "hermes" else openclaw_frontmatter(m)
    tree = {"SKILL.md": ((front + render_body(target)).encode(), False)}
    for top in ("scripts", "references", "assets"):
        src = AUTH / top
        if not src.is_dir():
            continue
        for p in sorted(src.rglob("*")):
            if p.is_dir() or any(part in SKIP_NAMES for part in p.parts) \
               or p.suffix == ".pyc":
                continue
            rel = str(Path(top) / p.relative_to(src))
            tree[rel] = (p.read_bytes(), os.access(p, os.X_OK))
    return tree


def materialize(tree, dest, managed_dirs):
    for d in managed_dirs:
        if (dest / d).exists():
            shutil.rmtree(dest / d)
    skill_md = dest / "SKILL.md"
    if skill_md.exists():
        skill_md.unlink()
    for rel in sorted(tree):
        data, is_exec = tree[rel]
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        mode = 0o755 if is_exec else 0o644
        os.chmod(out, mode)


def main():
    m = load_manifest()
    slug = m["skill"]["name"]
    oc_tree = collect_tree("openclaw", m)
    hm_tree = collect_tree("hermes", m)
    # determinism self-check: a second in-memory generation must be identical
    if collect_tree("openclaw", m) != oc_tree or collect_tree("hermes", m) != hm_tree:
        die("non-deterministic generation (second pass differed)")
    materialize(oc_tree, REPO, ("scripts", "references", "assets"))
    hermes_root = REPO / "skills" / slug
    hermes_root.mkdir(parents=True, exist_ok=True)
    materialize(hm_tree, hermes_root, ("scripts", "references", "assets"))
    print(f"built: root SKILL.md + {len(oc_tree)-1} files (openclaw), "
          f"skills/{slug}/ + {len(hm_tree)-1} files (hermes)")


if __name__ == "__main__":
    main()
