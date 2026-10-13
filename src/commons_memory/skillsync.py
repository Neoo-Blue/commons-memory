"""Skills appear natively in every AI. The skill pool stays central, but clients only trigger skills they can see on
disk, so Commons writes a small pointer (SKILL.md with the skill's name and description) for every active pool skill
into each client's skills folder; the AI loads the real content with skill_get. The session-start hook refreshes them,
so a skill published once shows up everywhere. Only folders Commons created (marked .commons-skill) are ever touched:
the user's own skills are left alone."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

DIRS = ["~/.claude/skills", "~/.agents/skills", "~/.gemini/config/skills"]  # Claude Code, cross-tool, Antigravity
MARK = ".commons-skill"


def index(s) -> list[dict]:
    """Active pool skills, for the session-start hook response."""
    out = []
    for r in s.q("SELECT name, description, active_version FROM skills WHERE active_version IS NOT NULL ORDER BY name"):
        out.append({"name": r["name"], "description": (r["description"] or "")[:300], "version": r["active_version"]})
    return out[:200]


def _pointer(sk: dict) -> str:
    desc = " ".join((sk.get("description") or f"Commons skill {sk['name']}").split()).replace('"', "'")
    return (f"---\nname: {sk['name']}\ndescription: {desc}\n---\n\n"
            f"This skill lives in the Commons skill pool (version {sk['version']}). Load the full skill with "
            f"skill_get(name=\"{sk['name']}\") (through call('skill_get', {{\"name\": \"{sk['name']}\"}}) if it isn't in "
            "your tool list), follow it, then skill_rate(name, success).\n")


def write(skills: list[dict], dirs: list[str] | None = None) -> dict:
    wrote, removed, kept = [], [], []
    want = {sk["name"]: sk for sk in skills if re.fullmatch(r"[\w.-]{1,80}", sk.get("name") or "")}
    for d in dirs or DIRS:
        root = Path(os.path.expanduser(d))
        if not root.is_dir():
            continue
        for name, sk in want.items():
            folder = root / name
            if folder.exists() and not (folder / MARK).exists():
                kept.append(str(folder))  # the user's own skill with that name: never overwritten
                continue
            if (folder / MARK).exists() and (folder / MARK).read_text().strip() == str(sk["version"]):
                continue
            folder.mkdir(exist_ok=True)
            (folder / "SKILL.md").write_text(_pointer(sk))
            (folder / MARK).write_text(str(sk["version"]))
            wrote.append(str(folder))
        for folder in root.iterdir():
            if (folder / MARK).exists() and folder.name not in want:
                shutil.rmtree(folder)  # retired from the pool
                removed.append(str(folder))
    return {"wrote": wrote, "removed": removed, "left_alone": kept}
