#!/usr/bin/env python3
"""Generate docs/configuration.md (COMMONS_* env var reference) from the source code.

Every `os.environ.get("COMMONS_X", default)` / `os.environ["COMMONS_X"]` / `getenv` call is collected with its
default and the module(s) that read it. Descriptions come from DESCRIPTIONS below; a test fails when a variable in
the code has no description, so the table cannot silently go stale.
Usage: python scripts/gen_env_reference.py [--check]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "commons_memory"
OUT = ROOT / "docs" / "configuration.md"
PAT = re.compile(r"""(?:environ\.get|getenv)\(\s*["'](COMMONS_[A-Z0-9_]+)["']\s*(?:,\s*([^)]{0,60}?))?\)"""
                 r"""|environ\[\s*["'](COMMONS_[A-Z0-9_]+)["']\s*\]""")

GROUPS = {
    "Core": ["DB", "DB_KEY", "AGENT", "API_KEY", "EMBEDDER", "MODEL", "PUBLIC_URL", "PROMPT_FILE"],
    "Auth & security": ["REQUIRE_KEYS", "ALLOW_ANON_READS", "ADMIN_AGENTS", "RATE_PER_MIN", "DASHBOARD_TOKEN",
                        "OAUTH_", "TLS_", "ENV_SECRETS", "ENTROPY_", "EXPORT_SECRET_REFS", "WEBHOOK_ALLOWLIST",
                        "KB_MIN_TRUST", "TRUST_WEIGHTING"],
}


def scan() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for f in sorted(SRC.rglob("*.py")):
        for m in PAT.finditer(f.read_text(encoding="utf-8")):
            name = m.group(1) or m.group(3)
            if name.endswith("_"):  # prefix families (COMMONS_EVERY_<JOB>, COMMONS_RETAIN_<TABLE>)
                name += "*"
            d = (m.group(2) or "").strip().strip("\"'") if m.group(1) else ""
            if any(c in d for c in "({\"'") or " " in d:  # computed default
                d = "computed"
            e = found.setdefault(name, {"default": d, "files": set()})
            if d and not e["default"]:
                e["default"] = d
            e["files"].add(f.relative_to(SRC.parent).as_posix().removeprefix("commons_memory/"))
    for fam in ("COMMONS_EVERY_*", "COMMONS_RETAIN_*"):  # f-string families
        found.setdefault(fam, {"default": "", "files": set()})
    for name in ("COMMONS_EVERY_", "COMMONS_RETAIN_", "COMMONS_MCP_"):  # prefixes, not variables
        found.pop(name + "*", None) if name == "COMMONS_MCP_" else None
    return found


def load_desc() -> dict[str, str]:
    p = ROOT / "scripts" / "env_descriptions.tsv"
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            k, _, v = line.partition("\t")
            out[k.strip()] = v.strip()
    return out


def render(found: dict[str, dict], desc: dict[str, str]) -> str:
    rows = ["# Configuration reference", "",
            "Generated from the source by `scripts/gen_env_reference.py` — do not edit by hand.",
            "All settings are environment variables; CLI flags override them where both exist.", "",
            "| Variable | Default | What it does | Read in |", "|---|---|---|---|"]
    for name in sorted(found):
        e = found[name]
        dflt = f"`{e['default']}`" if e["default"] and e["default"] != "None" else "—"
        rows.append(f"| `{name}` | {dflt} | {desc.get(name, 'TODO')} | {', '.join(sorted(e['files']))} |")
    return "\n".join(rows) + "\n"


def main() -> int:
    found, desc = scan(), load_desc()
    missing = sorted(n for n in found if n not in desc)
    text = render(found, desc)
    if "--check" in sys.argv:
        stale = not OUT.exists() or OUT.read_text(encoding="utf-8") != text
        if stale:
            print("note: docs/configuration.md is stale; run scripts/gen_env_reference.py")
        if missing:
            print("missing descriptions:", missing)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({len(found)} variables, {len(missing)} without description)")
    if missing:
        print("  missing:", ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
