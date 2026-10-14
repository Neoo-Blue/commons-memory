"""Re-file notes that sit under the wrong project.

Imports carry the project label of whatever folder the old tool ran in (claude-mem put thousands of Poise notes under
soul-pool), and folder buckets like `home` and `workspace` hold notes about every product. A small model reads those
notes in batches against a catalog of the real projects (title, the names grouped under it, sample note titles) and
says which ones clearly belong elsewhere. Nothing moves until the owner accepts a suggestion; every move remembers where
the note was, so Undo restores it. Notes captured live with a project are never candidates.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

from .ids import iso, now
from .store import CommonsError, Store

SCHEMA = """CREATE TABLE IF NOT EXISTS refile_moves(item_id TEXT PRIMARY KEY, from_pid TEXT, to_pid TEXT, confidence TEXT,
  status TEXT, run TEXT, created_at REAL, decided_at REAL)"""
BATCH = 80          # notes per model request
PER_BATCH_USD = 0.012  # measured order of magnitude for Haiku through the batch API (half price)
SYS = """You sort one person's notes into their projects. Each note shows the project it is filed under now, which is
often wrong: many were imported from a tool that labelled notes by the folder it happened to run in, and some sit in
folders like "home" that hold everything. For each note that clearly belongs to one listed project other than its
current one, return {id, to, confidence} with `to` the project's code. Leave out notes that fit their current project,
are generic, or could belong to several. confidence "high" means the note names the product, its repo, its host, or a
feature only that project has; "medium" means it very likely belongs there. A project's "is:" line is the owner's own
word on what belongs there and outranks its samples: when a note clearly does not fit its current project's "is:" line
and no listed project fits it either, return to "NONE" (it moves to Unsorted)."""
OUT = {"type": "object", "additionalProperties": False, "required": ["moves"], "properties": {"moves": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["id", "to", "confidence"],
    "properties": {"id": {"type": "string"}, "to": {"type": "string"}, "confidence": {"type": "string", "enum": ["high", "medium"]}}}}}}


def ensure(s: Store) -> None:
    if not getattr(s, "_refile_ok", False):
        s.x(SCHEMA)
        s._refile_ok = True


def _imported(meta: str | None) -> bool:
    try:
        return json.loads(meta or "{}").get("imported") in (True, "True", "true", 1)
    except ValueError:
        return False


def catalog(s: Store) -> tuple[list[dict], dict[str, str]]:
    """The real projects (not tasks or folders) with up to 4 sample titles each, from notes captured live when there are
    enough (an import's own labels are what we are checking), and a raw-project -> root map."""
    from .projects import _groups, _rows, listing
    rows = _rows(s)
    root_of = {m: root for root, ids in _groups(rows).items() for m in ids}
    out = []
    for i, p in enumerate(listing(s)["projects"][:60]):
        ids = [m for m, r in root_of.items() if r == p["id"]]
        ph = ",".join("?" * len(ids))
        recent = s.q(f"SELECT title, summary, content, meta FROM items WHERE project_id IN ({ph}) AND superseded_by IS NULL AND "
                     "kind IN ('decision','memory','handoff') ORDER BY created_at DESC LIMIT 200", ids)
        live = [r for r in recent if not _imported(r["meta"])]
        pick = (live if len(live) >= 3 else recent)[:4]
        samples = [((r["title"] or r["summary"] or r["content"] or "")[:90]).replace("\n", " ") for r in pick]
        out.append({"code": f"P{i + 1}", "id": p["id"], "title": p["title"], "members": p["members"], "samples": samples,
                    "description": p.get("description") or ""})
    return out, root_of


def candidates(s: Store, root_of: dict[str, str], folder_ids: set[str]) -> list[dict]:
    """Imported notes and notes in folder buckets, not yet decided, newest first."""
    ensure(s)
    done = {r["item_id"] for r in s.q("SELECT item_id FROM refile_moves")}
    out = []
    for r in s.q("SELECT id, project_id, meta, created_at, substr(COALESCE(title,'')||' '||COALESCE(content, summary, ''),1,240) c "
                 "FROM items WHERE superseded_by IS NULL AND project_id IS NOT NULL AND kind IN "
                 "('memory','decision','fact','document','handoff','note') ORDER BY created_at DESC"):
        if r["id"] in done or len((r["c"] or "").strip()) < 30:
            continue
        if r["project_id"] in folder_ids or _imported(r["meta"]):
            out.append({"id": r["id"], "pid": r["project_id"], "c": " ".join((r["c"] or "").split())})
    return out


def _setup(s: Store):
    from .projects import listing
    cat, root_of = catalog(s)
    folders = {m for e in listing(s)["folders"] for m in [e["id"]]}
    folders |= {m for m, r in root_of.items() if r in folders}
    return cat, root_of, candidates(s, root_of, folders)


def estimate(s: Store) -> dict:
    cat, _, cands = _setup(s)
    n = (len(cands) + BATCH - 1) // BATCH
    return {"notes": len(cands), "batches": n, "projects": len(cat), "estimate_usd": round(n * PER_BATCH_USD, 2)}


def _prompt(batch: list[dict], names: dict[str, str]) -> str:
    return "Notes:\n" + "\n".join(f"[{n['id']}] (now: {names.get(n['pid'], '?')}) {n['c']}" for n in batch)


def _prefix(cat: list[dict]) -> str:
    lines = [f"{p['code']} {p['title']}" + (f" (also filed as {', '.join(m for m in p['members'] if m != p['title'])})"
             if len(p["members"]) > 1 or p["members"][0] != p["title"] else "")
             + (f". is: {p['description']}" if p.get("description") else "")
             + (". samples: " + " | ".join(p["samples"]) if p["samples"] else "") for p in cat]
    return "Projects (code, name, what it is, sample note titles):\n" + "\n".join(lines) + "\n\n"


def run(s: Store, model: str | None = None, log=lambda m: None, poll: float = 30) -> dict:
    """Classify every candidate and store the proposals. Anthropic models go through the batch API (half price)."""
    from .projects import _rows
    from .providers import ProviderLayer
    ensure(s)
    cat, root_of, cands = _setup(s)
    if not cands or not cat:
        return {"notes": 0, "proposed": 0, "cost": 0.0}
    pl = ProviderLayer(s)
    model = model or pl.role_model("librarian") or pl.role_model("kb_summarizer")
    if not model:
        raise CommonsError("no model to check filing: set the librarian role or add a provider key on the Setup page")
    names = {pid: r["name"] for pid, r in _rows(s).items()}
    code = {p["code"]: p["id"] for p in cat}
    if any(p.get("description") for p in cat):  # "doesn't belong here, and nowhere listed": a folder, not a project
        code["NONE"] = s.project_id("unsorted")
        s.x("UPDATE projects SET kind='folder' WHERE id=? AND kind IS NULL", (code["NONE"],))
    prefix = _prefix(cat)
    batches = [cands[i:i + BATCH] for i in range(0, len(cands), BATCH)]
    by_id = {n["id"]: n for n in cands}
    tag, cost, answers = f"r{int(now())}", 0.0, []
    provider = model.partition("/")[0]
    if pl._p(provider)["kind"] == "anthropic":
        reqs = [{"id": f"b{i}", "prompt": _prompt(b, names), "system": SYS, "cache_prefix": prefix, "max_tokens": 6000,
                 "schema": OUT, "effort": "minimal"} for i, b in enumerate(batches)]
        bid = pl.batch_submit(model, reqs)
        log(f"submitted {len(reqs)} requests as batch {bid}")
        while True:
            r = pl.batch_results(model, bid, role="librarian")
            if r["status"] == "ended":
                break
            log(f"batch {bid}: {json.dumps(r.get('counts') or {})}")
            time.sleep(poll)
        cost = r.get("cost", 0.0) or 0.0
        answers = [r["results"].get(f"b{i}") for i in range(len(batches))]
    else:
        for i, b in enumerate(batches):
            got = pl.chat(model, _prompt(b, names), system=SYS, cache_prefix=prefix, max_tokens=6000, role="librarian", schema=OUT)
            cost += got.get("cost", 0) or 0
            answers.append(got.get("text"))
            log(f"batch {i + 1}/{len(batches)}, ${cost:.2f}")
    proposed = 0
    for text in answers:
        try:
            moves = json.loads(text or "{}").get("moves", [])
        except ValueError:
            continue
        for m in moves:
            n, to = by_id.get(m.get("id")), code.get("NONE" if str(m.get("to")).upper() == "NONE" else m.get("to"))
            if not n or not to or root_of.get(n["pid"]) == to:
                continue
            s.x("INSERT OR IGNORE INTO refile_moves VALUES(?,?,?,?,?,?,?,?)",
                (n["id"], n["pid"], to, m.get("confidence", "medium"), "proposed", tag, now(), None))
            proposed += 1
    s.audit(None, "refile.run", tag, {"notes": len(cands), "proposed": proposed, "cost": round(cost, 4), "model": model})
    return {"notes": len(cands), "batches": len(batches), "proposed": proposed, "cost": round(cost, 4)}


def run_async(s: Store) -> dict:
    """Start a filing check in the background (once at a time); progress in meta refile:job."""
    import threading
    ensure(s)
    with s.tx():
        cur = s.q("SELECT value FROM meta WHERE key='refile:job'")
        cur = json.loads(cur[0]["value"]) if cur else {}
        if cur.get("status") == "running" and now() - cur.get("at", 0) < 12 * 3600:
            return cur
        s.x("INSERT OR REPLACE INTO meta VALUES('refile:job', ?)", (json.dumps({"status": "running", "at": now()}),))
    state = {"status": "running", "at": now(), "note": "counting notes"}
    def put(**kw):
        state.update(kw)
        s.x("INSERT OR REPLACE INTO meta VALUES('refile:job', ?)", (json.dumps(state),))
    def go():
        try:
            put(result=run(s, log=lambda m: put(note=m)), status="done")
        except Exception as e:  # noqa: BLE001 - shown on the page
            put(status="failed", error=str(e)[:300])
    threading.Thread(target=go, daemon=True, name="refile").start()
    return state


def suggestions(s: Store) -> list[dict]:
    """Proposed moves grouped by (from project, to project): counts, how many are sure, and sample notes."""
    from .projects import _groups, _rows
    ensure(s)
    rows = _rows(s)
    root_of = {m: root for root, ids in _groups(rows).items() for m in ids}
    flows: dict[tuple, dict] = {}
    for r in s.q("SELECT m.item_id, m.from_pid, m.to_pid, m.confidence, substr(COALESCE(i.title,'')||' '||COALESCE(i.content, i.summary,''),1,160) c "
                 "FROM refile_moves m JOIN items i ON i.id=m.item_id WHERE m.status='proposed' ORDER BY i.created_at DESC"):
        k = (root_of.get(r["from_pid"], r["from_pid"]), r["to_pid"])
        f = flows.setdefault(k, {"from": k[0], "to": k[1], "count": 0, "high": 0, "samples": []})
        f["count"] += 1
        f["high"] += r["confidence"] == "high"
        if len(f["samples"]) < 6:
            f["samples"].append({"id": r["item_id"], "text": " ".join((r["c"] or "").split()), "confidence": r["confidence"]})
    title = {pid: (r["title"] or r["name"]) for pid, r in rows.items()}
    out = [f | {"from_title": title.get(f["from"], "?"), "to_title": title.get(f["to"], "?"), "key": f"{f['from']}>{f['to']}"}
           for f in flows.values()]
    return sorted(out, key=lambda f: -f["count"])


def _flow_rows(s: Store, key: str, sure_only: bool) -> list:
    from .projects import _groups, _rows
    frm, _, to = key.partition(">")
    members = set(_groups(_rows(s)).get(frm, [frm]))
    return [r for r in s.q("SELECT item_id, from_pid, confidence FROM refile_moves WHERE status='proposed' AND to_pid=?", (to,))
            if r["from_pid"] in members and (r["confidence"] == "high" or not sure_only)], to


def apply(s: Store, key: str, sure_only: bool = True) -> dict:
    """Move a flow's notes (the sure ones, or all) to their new project; Undo restores them."""
    ensure(s)
    rows, to = _flow_rows(s, key, sure_only)
    t = now()
    with s.tx():
        for r in rows:
            s.x("UPDATE items SET project_id=? WHERE id=?", (to, r["item_id"]))
            s.x("UPDATE refile_moves SET status='moved', decided_at=? WHERE item_id=?", (t, r["item_id"]))
    s.audit(None, "refile.apply", key, {"moved": len(rows), "sure_only": sure_only})
    return {"moved": len(rows), "undo": {"key": key, "at": t}}


def undo(s: Store, key: str, at: float) -> dict:
    ensure(s)
    frm, _, to = key.partition(">")
    rows = s.q("SELECT item_id, from_pid FROM refile_moves WHERE status='moved' AND to_pid=? AND decided_at=?", (to, float(at)))
    with s.tx():
        for r in rows:
            s.x("UPDATE items SET project_id=? WHERE id=?", (r["from_pid"], r["item_id"]))
            s.x("UPDATE refile_moves SET status='proposed', decided_at=NULL WHERE item_id=?", (r["item_id"],))
    s.audit(None, "refile.undo", key, {"restored": len(rows)})
    return {"restored": len(rows)}


def dismiss(s: Store, key: str) -> dict:
    ensure(s)
    rows, _ = _flow_rows(s, key, False)
    with s.tx():
        for r in rows:
            s.x("UPDATE refile_moves SET status='dismissed', decided_at=? WHERE item_id=?", (now(), r["item_id"]))
    return {"dismissed": len(rows)}


def status(s: Store) -> dict:
    ensure(s)
    job = s.q("SELECT value FROM meta WHERE key='refile:job'")
    counts = {r["status"]: r["n"] for r in s.q("SELECT status, COUNT(*) n FROM refile_moves GROUP BY status")}
    return {"job": json.loads(job[0]["value"]) if job else None, "counts": counts,
            "last": iso(max((json.loads(job[0]["value"]).get("at", 0),), default=0)) if job else None}
