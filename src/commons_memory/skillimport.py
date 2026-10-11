"""Import skills already installed on this machine into the shared pool (with provenance + dedupe).

Scans: ~/.claude/skills/*/SKILL.md, ./.claude/skills/*/SKILL.md, Claude Code plugins (~/.claude/plugins/**/skills/*/SKILL.md),
.cursor/rules/*.mdc (project + home), .cursorrules, AGENTS.md (project, ~/.codex), ~/.codex/skills, ~/.agents/skills
and any custom paths. Optionally replaces local copies with the bootstrap pointer (originals backed up).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import socket
from pathlib import Path

from .ids import now
from .skilldiscovery import _as_skill, _slug
from .store import Store

MAX_FILE = 100_000


def locations(project_dir: str = ".", extra: list[str] | None = None) -> list[Path]:
    h, p = Path.home(), Path(project_dir).resolve()
    return [h / ".claude/skills", p / ".claude/skills", h / ".claude/plugins", h / ".codex/skills", h / ".agents/skills",
            p / ".cursor/rules", h / ".cursor/rules", p / "AGENTS.md", h / ".codex/AGENTS.md", p / ".cursorrules"] + \
        [Path(x).expanduser() for x in (extra or [])]


def scan(project_dir: str = ".", extra: list[str] | None = None) -> list[dict]:
    found, seen = [], set()
    for loc in locations(project_dir, extra):
        if not loc.exists():
            continue
        cands = [loc] if loc.is_file() else list(loc.rglob("SKILL.md")) + list(loc.rglob("*.mdc")) + \
            ([loc / "AGENTS.md"] if (loc / "AGENTS.md").exists() else [])
        for f in cands:
            f = f.resolve()
            if f in seen or not f.is_file() or f.stat().st_size > MAX_FILE:
                continue
            seen.add(f)
            body = f.read_text(encoding="utf-8", errors="replace")
            if "Commons (bootstrap)" in body:
                continue  # our own pointer
            if f.name == "SKILL.md":
                name, kind, files = f.parent.name, "skill", {}
                for g in sorted(f.parent.rglob("*")):
                    if g.is_file() and g != f and g.stat().st_size < MAX_FILE and len(files) < 20:
                        try:
                            files[str(g.relative_to(f.parent))] = g.read_text(encoding="utf-8")
                        except UnicodeDecodeError:
                            pass
            elif f.suffix == ".mdc" or f.name == ".cursorrules":
                name, kind, files = f"cursor-{f.stem.lstrip('.')}", "rules", {}
            else:
                name, kind, files = f"agents-md-{f.parent.name}", "agents_md", {}
            content = _as_skill(body, name, {"description": f"Imported {kind} from {f}", "path": str(f)})
            found.append({"path": str(f), "name": _slug(name), "kind": kind, "content": content, "files": files,
                          "hash": hashlib.sha256((content + json.dumps(files, sort_keys=True)).encode()).hexdigest()})
    return found


def run(store: Store, project_dir: str = ".", extra: list[str] | None = None, dry_run: bool = False,
        replace_local: bool = False, agent_id: str | None = None) -> dict:
    from .skillpool import SkillPool
    pool = SkillPool(store)
    existing = {}
    for r in store.q("SELECT s.name, v.content, v.files FROM skill_versions v JOIN skills s ON s.id=v.skill_id"):
        existing[hashlib.sha256((r["content"] + json.dumps(json.loads(r["files"] or "{}"), sort_keys=True)).encode()).hexdigest()] = r["name"]
    out = {"imported": [], "duplicates": [], "updated": [], "replaced": [], "dry_run": dry_run}
    host = socket.gethostname()
    for s in scan(project_dir, extra):
        if s["hash"] in existing:
            out["duplicates"].append({"path": s["path"], "same_as": existing[s["hash"]]})
            continue
        prov = f"imported from local {s['kind']} {s['path']} on {host}"
        if dry_run:
            out["imported"].append({"path": s["path"], "name": s["name"], "kind": s["kind"]})
            continue
        if store.q("SELECT 1 FROM skills WHERE name=?", (s["name"],)):
            r = pool.update(s["name"], s["content"], files=s["files"], changelog=prov, agent_id=agent_id)
            out["updated"].append({"path": s["path"], "name": s["name"], "version": r["version"], "status": r["status"]})
        else:
            r = pool.publish(s["name"], s["content"], files=s["files"], tags=["imported", "local", s["kind"]],
                             agent_id=agent_id, changelog=prov)
            out["imported"].append({"path": s["path"], "name": s["name"], "status": r["status"]})
        existing[s["hash"]] = s["name"]
        if replace_local and s["kind"] == "skill":
            src = Path(s["path"]).parent
            bak = Path.home() / ".commons" / "skill-backups" / f"{src.name}-{int(now())}"
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, bak)
            shutil.rmtree(src)
            src.mkdir(parents=True)
            from importlib import resources
            ptr = (resources.files("commons_memory") / "skills" / "BOOTSTRAP.md").read_text(encoding="utf-8")
            (src / "SKILL.md").write_text(ptr.replace("name: commons", f"name: {s['name']}", 1) +
                                          f"\nThis skill now lives in the Commons pool: `skill_get(\"{s['name']}\")`.\n")
            out["replaced"].append({"path": str(src), "backup": str(bak)})
    store.audit(agent_id, "skills.import.local", f"{len(out['imported'])} new", {k: len(v) for k, v in out.items() if isinstance(v, list)})
    return out
