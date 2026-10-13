"""Graphiti adapter: mirror Commons memory into a Graphiti (Zep) temporal knowledge graph, and bring its facts back.

push: new memories, facts and decisions become Graphiti episodes (POST /messages). Each carries a stable uuid derived
from the Commons id, and a watermark keeps re-runs incremental. Records restricted to named agents (row-level access)
are never sent. Graphiti ingests asynchronously and extracts entities and fact edges with its own LLM.
pull: Graphiti's fact edges for the latest episodes (GET /episodes/{group}, GET /entity-edge/{uuid}) come back as
Commons facts with their validity window; a fact Graphiti has invalidated is tagged `invalid`.
API: github.com/getzep/graphiti server/ (no auth; default port 8000). Set graphiti_url on the Setup page to push
every few minutes, or run `commons graphiti push|pull`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

from .ids import iso
from .store import CommonsError, Store

NS = uuid.UUID("6c0f4a8e-9d1b-4c62-8a3e-2f7b5d9e1c40")  # stable episode uuids: same Commons id, same Graphiti uuid
KINDS = ("memory", "fact", "decision")


def _call(url: str, method: str, path: str, body: dict | None = None, timeout: float = 30):
    req = urllib.request.Request(url.rstrip("/") + path, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - a URL the user configured
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise CommonsError(f"Graphiti {method} {path}: HTTP {e.code} {e.read()[:200]!r}") from e
    except OSError as e:
        raise CommonsError(f"Graphiti unreachable at {url}: {e}") from e
    return json.loads(raw) if raw else None


def push(s: Store, url: str, group: str = "commons", limit: int = 500) -> dict:
    key = f"graphiti:wm:{group}"
    wm = float((s.q("SELECT value FROM meta WHERE key=?", (key,)) or [{"value": 0}])[0]["value"])
    restricted = {r["ref"] for r in s.q("SELECT DISTINCT ref FROM row_acl")}
    rows = [dict(r) for r in s.q(
        f"SELECT * FROM items WHERE created_at>? AND superseded_by IS NULL AND kind IN ({','.join('?' * len(KINDS))}) "
        "ORDER BY created_at, id LIMIT ?", (wm, *KINDS, limit))]
    msgs = []
    for r in rows:
        if r["id"] in restricted or not (r["content"] or "").strip():
            continue
        user = '"conversation"' in (r["tags"] or "") and '"user"' in (r["tags"] or "")
        msgs.append({"content": r["content"][:8000], "role_type": "user" if user else "assistant",
                     "role": "user" if user else (s.label(r["agent_id"])[1] if r["agent_id"] else "commons"),
                     "uuid": str(uuid.uuid5(NS, r["id"])), "name": (r["title"] or r["id"])[:200],
                     "timestamp": iso(r["created_at"]),
                     "source_description": f"commons {r['kind']} {r['id']}"
                                           + (f" in {s.project_name(r['project_id'])}" if r["project_id"] else "")})
    for i in range(0, len(msgs), 50):
        _call(url, "POST", "/messages", {"group_id": group, "messages": msgs[i:i + 50]})
    if rows:
        s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(rows[-1]["created_at"])))
    s.audit(None, "graphiti.push", group, {"sent": len(msgs)})
    return {"group": group, "sent": len(msgs), "skipped_restricted": sum(r["id"] in restricted for r in rows),
            "more": len(rows) == limit}


def pull(s: Store, url: str, group: str = "commons", last_n: int = 50, project: str | None = None) -> dict:
    eps = _call(url, "GET", f"/episodes/{group}?last_n={int(last_n)}") or []
    edges = sorted({e for ep in eps for e in (ep.get("entity_edges") or [])})
    added, invalid = 0, 0
    for eid in edges:
        if s.q("SELECT 1 FROM meta WHERE key=?", (f"graphiti:fact:{eid}",)):
            continue
        f = _call(url, "GET", f"/entity-edge/{eid}") or {}
        if not (f.get("fact") or "").strip():
            continue
        gone = bool(f.get("invalid_at") or f.get("expired_at"))
        r = s.add_item("fact", f["fact"], title=f"[graphiti] {f.get('name') or 'fact'}", project=project,
                       tags=["graphiti"] + (["invalid"] if gone else []),
                       meta={"graphiti_uuid": eid, "valid_at": f.get("valid_at"), "invalid_at": f.get("invalid_at"),
                             "episodes": f.get("episodes") or []}, check_conflicts=False)
        s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"graphiti:fact:{eid}", r["id"]))
        added += 1
        invalid += gone
    s.audit(None, "graphiti.pull", group, {"facts": added})
    return {"group": group, "episodes": len(eps), "facts_added": added, "invalid": invalid}
