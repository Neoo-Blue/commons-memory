"""Import memories from other memory systems (v0.9): claude-mem, mem0, Claude Code memory files, sesame.

Each source yields plain item dicts; `import_items` writes them idempotently (meta.src = "<source>:<id>"), keeping
the original timestamps and projects, redacting like every write, and embedding in batches. Claude Code `feedback`
memories also become shared preferences, so every connected AI follows them through pickup().
`commons import <source>` runs against the local database or pushes to a remote hub (--url) via the import_items
MCP tool. Secrets never travel: sesame contributes service metadata and env-var names only.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from pathlib import Path
from typing import Iterator

from .store import PROJECT_NAME, CommonsError, Store

SOURCES = ("claude-mem", "mem0", "claude-memory", "sesame")


def project_name(raw: str | None) -> str | None:
    """Map a foreign project label onto a valid Commons project name (None when nothing usable is left). The home
    directory's own name (claude-mem's label for sessions run in ~) becomes "home"."""
    if not raw:
        return None
    if raw == Path.home().name:
        return "home"
    name = re.sub(r"[^A-Za-z0-9._ -]+", "-", raw.strip()).strip(" .-_")[:64]
    return name if name and PROJECT_NAME.fullmatch(name) else None


def _jl(v) -> list:
    try:
        x = json.loads(v) if isinstance(v, str) else (v or [])
        return x if isinstance(x, list) else []
    except ValueError:
        return []


# ------------------------------------------------------------------ sources
def claude_mem(path: str | None = None, prompts: bool = False) -> Iterator[dict]:
    """claude-mem's SQLite store: observations (typed), session summaries, optionally raw user prompts."""
    p = Path(path or Path.home() / ".claude-mem" / "claude-mem.db")
    if not p.exists():
        raise CommonsError(f"no claude-mem database at {p}")
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    for r in db.execute("SELECT * FROM observations ORDER BY id"):
        facts = "\n".join(f"- {f}" for f in _jl(r["facts"]))
        files = _jl(r["files_modified"]) + _jl(r["files_read"])
        body = "\n\n".join(x for x in (r["subtitle"], r["narrative"], facts,
                                       ("Files: " + ", ".join(files[:20])) if files else "") if x)
        typ = (r["type"] or "observation").lower()
        yield {"kind": "decision" if typ == "decision" else "memory", "title": r["title"] or "",
               "content": body or r["text"] or r["title"] or "", "project": project_name(r["merged_into_project"] or r["project"]),
               "tags": ["claude-mem", typ] + [c for c in _jl(r["concepts"])[:4] if isinstance(c, str)],
               "created_at": (r["created_at_epoch"] or 0) / 1000 or None, "src": f"claude-mem:obs:{r['id']}"}
    for r in db.execute("SELECT * FROM session_summaries ORDER BY id"):
        parts = [(k.replace("_", " "), r[k]) for k in ("request", "investigated", "learned", "completed", "next_steps", "notes")]
        body = "\n".join(f"{k.capitalize()}: {v}" for k, v in parts if v)
        if body.strip():
            yield {"kind": "memory", "title": f"Session summary: {(r['request'] or '')[:70]}", "content": body,
                   "project": project_name(r["merged_into_project"] or r["project"]), "tags": ["claude-mem", "session-summary"],
                   "created_at": (r["created_at_epoch"] or 0) / 1000 or None, "src": f"claude-mem:sum:{r['id']}"}
    if prompts:
        cols = {c[1] for c in db.execute("PRAGMA table_info(user_prompts)")}
        text_col = "prompt_text" if "prompt_text" in cols else "prompt"
        for r in db.execute(f"SELECT * FROM user_prompts ORDER BY id"):
            if (r[text_col] or "").strip():
                yield {"kind": "memory", "title": "", "content": r[text_col], "project": project_name(r["project"]) if "project" in cols else None,
                       "tags": ["claude-mem", "user-prompt"], "created_at": (r["created_at_epoch"] or 0) / 1000 or None,
                       "src": f"claude-mem:prompt:{r['id']}"}


def mem0(url: str | None = None, key: str | None = None, user_id: str | None = None) -> Iterator[dict]:
    """A mem0 server (self-hosted OSS API: GET /memories with X-API-Key; or MEM0_SELFHOSTED_URL/KEY in env)."""
    url = (url or os.environ.get("MEM0_SELFHOSTED_URL") or os.environ.get("MEM0_URL") or "").rstrip("/")
    key = key or os.environ.get("MEM0_SELFHOSTED_KEY") or os.environ.get("MEM0_API_KEY")
    if not url:
        raise CommonsError("mem0: pass --url or set MEM0_SELFHOSTED_URL")
    q = "top_k=1000" + (f"&user_id={user_id}" if user_id else "")
    req = urllib.request.Request(f"{url}/memories?{q}", headers={"X-API-Key": key or "", "User-Agent": "commons-import/0.9"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - user-supplied server
        data = json.loads(r.read())
    for m in data.get("results", data) if isinstance(data, dict) else data:
        text = m.get("memory") or m.get("text") or ""
        if text.strip():
            yield {"kind": "memory", "title": "", "content": text, "project": None,
                   "tags": ["mem0"] + ([m["user_id"]] if m.get("user_id") else []),
                   "created_at": m.get("created_at"), "src": f"mem0:{m.get('id')}"}


def claude_memory(root: str | None = None) -> Iterator[dict]:
    """Claude Code auto-memory: ~/.claude/projects/<encoded cwd>/memory/*.md with name/description/type frontmatter."""
    base = Path(root or Path.home() / ".claude" / "projects")
    for f in sorted(base.glob("*/memory/*.md")):
        if f.name == "MEMORY.md":
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        fm, body = {}, text
        m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
        if m:
            body = m.group(2)
            for line in m.group(1).splitlines():
                k, _, v = line.partition(":")
                if v.strip():
                    fm[k.strip()] = v.strip()
        typ = fm.get("type") or ("feedback" if f.stem.startswith("feedback") else "memory")
        proj = f.parent.parent.name.strip("-").split("-")[-1] if f.parent.parent.name.count("-") > 2 else "home"
        yield {"kind": "memory", "title": fm.get("description") or fm.get("name") or f.stem,
               "content": body.strip(), "project": project_name(proj),
               "tags": ["claude-memory", typ], "created_at": f.stat().st_mtime, "src": f"claude-memory:{f.parent.parent.name}/{f.name}",
               "preference": fm.get("description") if typ == "feedback" else None}


def sesame(path: str | None = None) -> Iterator[dict]:
    """The sesame service registry: what each service is, its urls, env-var names and verified commands. Secret
    values live in the OS keychain and are never read here."""
    p = Path(path or Path.home() / ".claude" / "sesame" / "registry.json")
    reg = json.loads(p.read_text())
    for sid, sv in (reg.get("services") or reg).items():
        if not isinstance(sv, dict):
            continue
        lines = [f"Service {sv.get('label') or sid} (sesame id `{sid}`), category {sv.get('category', '?')}, "
                 f"status {sv.get('status', '?')}. Use credentials with `sesame run {sid} -- <command>`."]
        urls = dict(sv.get("urls") or {})
        insts = urls.pop("instances", None) or sv.get("instances") or []
        lines += [f"{k}: {v}" for k, v in urls.items() if isinstance(v, str)]
        lines += [f"instance {i.get('name')}: {i.get('url')}" for i in insts if isinstance(i, dict)]
        auth = sv.get("auth") or {}
        if auth.get("kind"):
            lines.append(f"auth: {auth['kind']}")
        for fld in auth.get("fields") or []:
            if not isinstance(fld, dict):
                continue
            envs = ", ".join(f"env://{e}" for e in fld.get("env") or [])
            if fld.get("secret"):  # the value stays in the keychain; only where to get it travels
                lines.append(f"secret {fld.get('name')}: {envs or 'sesame ref ' + str(fld.get('ref'))}")
            else:
                lines.append(f"{fld.get('name')}: {fld.get('value', '')} {('(' + envs + ')') if envs else ''}".rstrip())
        for rc in sv.get("recipes") or []:
            if isinstance(rc, dict):
                lines.append(f"recipe {rc.get('name')}: {rc.get('run') or rc.get('command') or ''}")
        lines += [f"gotcha: {g}" for g in sv.get("gotchas") or []]
        if sv.get("notes"):
            lines.append(f"notes: {sv['notes']}")
        yield {"kind": "memory", "title": f"Service: {sv.get('label') or sid}", "content": "\n".join(lines),
               "project": "services", "tags": ["sesame", str(sv.get("category") or "other")], "created_at": None,
               "src": f"sesame:{sid}"}


def collect(source: str, **kw) -> list[dict]:
    fn = {"claude-mem": claude_mem, "mem0": mem0, "claude-memory": claude_memory, "sesame": sesame}.get(source)
    if not fn:
        raise CommonsError(f"unknown source {source!r}; choose from {', '.join(SOURCES)}")
    return list(fn(**{k: v for k, v in kw.items() if v is not None}))


# ------------------------------------------------------------------ writer
def known(s: Store, srcs: list[str]) -> set[str]:
    """srcs already handled (imported, dropped, merged, duplicate). Queued-for-review items don't count: approving
    them must still import them."""
    out: set[str] = set()
    for i in range(0, len(srcs), 400):
        ch = [x for x in srcs[i:i + 400] if x]
        if ch:
            out |= {r["key"][7:] for r in s.q(f"SELECT key, value FROM meta WHERE key IN ({','.join('?' * len(ch))})",
                                              [f"import:{x}" for x in ch]) if r["value"] != "queued"}
    return out


def import_items(s: Store, items: list[dict], agent_id: str | None = None) -> dict:
    """Write items idempotently (skip any whose src is already handled). Per-item `status`: keep (default) imports;
    review queues it for a human (retro review queue); drop / merge / duplicate only records the decision so later
    runs skip it. Embeds once at the end, in batches."""
    from .prefs import Preferences
    have = known(s, [i.get("src") for i in items])
    srcs = [i.get("src") for i in items if i.get("src")]
    queued_already = {r["key"][7:] for k in range(0, len(srcs), 400) for r in s.q(
        f"SELECT key FROM meta WHERE value='queued' AND key IN ({','.join('?' * len(srcs[k:k + 400]))})",
        [f"import:{x}" for x in srcs[k:k + 400]])}
    added, skipped, prefs, queued, ledger, errors = 0, 0, 0, 0, 0, []
    s.defer_embed = True
    try:
        for it in items:
            src, status = it.get("src"), it.get("status") or "keep"
            if src and src in have:
                skipped += 1
                continue
            if status in ("drop", "merge", "duplicate"):
                if src:
                    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"import:{src}", status))
                ledger += 1
                continue
            if status == "review":
                if src in queued_already:  # a re-run must not queue the same item for review twice
                    skipped += 1
                    continue
                from .retro import RetroLearner, fingerprint
                RetroLearner(s)._enqueue("import-review", {
                    "kind": "memory", "text": it.get("title") or (it.get("content") or "")[:200],
                    "payload": {"item": {k: v for k, v in it.items() if k != "status"}},
                    "source": {"kind": (src or "import").split(":")[0], "id": src}, "project": it.get("project"),
                    "confidence": float(it.get("confidence") or 0.5), "fingerprint": fingerprint("memory", src or it.get("content", "")),
                    "method": it.get("method") or "curator"})
                if src:
                    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"import:{src}", "queued"))
                queued += 1
                continue
            try:
                r = s.add_item(it.get("kind") or "memory", it.get("content") or "", title=it.get("title") or "",
                               summary=it.get("summary") or "", project=it.get("project"), agent_id=agent_id,
                               tags=it.get("tags"), meta={"src": src, "imported": True},
                               created_at=_epoch(it.get("created_at")), check_conflicts=False)
            except CommonsError as e:
                errors.append(f"{src}: {e}")
                continue
            if src:
                s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"import:{src}", r["id"]))
                have.add(src)
            if it.get("preference"):
                Preferences(s).observe(it["preference"], project=None, source=f"import:{src}", confidence=0.9,
                                       evidence=r["id"])
                prefs += 1
            added += 1
    finally:
        s.defer_embed = False
    emb = s.reindex() if added else {"done": 0}
    return {"added": added, "skipped": skipped, "queued": queued, "decided": ledger, "preferences": prefs,
            "embedded": emb.get("done", 0), "errors": errors[:20], "error_count": len(errors)}


def _epoch(v) -> float | None:
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    from .store import _parse_time
    try:
        return _parse_time(str(v).replace("Z", "+00:00"))
    except (ValueError, CommonsError):
        return None
