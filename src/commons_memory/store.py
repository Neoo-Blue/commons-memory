"""SQLite system of record: memories, entities, relations, sessions, agents, inbox, decisions.

Every record has a stable prefixed id (mem_, ent_, rel_, ses_, agt_, prj_, tsk_, ...).
Relations can link any two ids, so memories, code entities, sessions and agents share one graph.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as pysecrets
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .embed import Embedder, cosine, get_embedder, pack, unpack
from .ids import estimate_tokens, iso, new_id, now
from .redact import redact

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, vendor TEXT, model TEXT,
  description TEXT, capabilities TEXT, key_hash TEXT, a2a_url TEXT, created_at REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, root TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, project_id TEXT, agent_id TEXT, session_id TEXT,
  title TEXT, summary TEXT, content TEXT, tags TEXT, meta TEXT,
  subject TEXT, predicate TEXT, value TEXT,
  conflict INTEGER DEFAULT 0, superseded_by TEXT, authenticated INTEGER DEFAULT 0,
  created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS items_kind ON items(kind, created_at);
CREATE INDEX IF NOT EXISTS items_proj ON items(project_id, created_at);
CREATE INDEX IF NOT EXISTS items_fact ON items(subject, predicate);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(item_id UNINDEXED, title, summary, content, tags,
  tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS embeddings(item_id TEXT PRIMARY KEY, model TEXT, vec BLOB);
CREATE TABLE IF NOT EXISTS entities(
  id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, project_id TEXT, path TEXT,
  line INTEGER, meta TEXT, created_at REAL,
  UNIQUE(type, name, project_id, path));
CREATE INDEX IF NOT EXISTS entities_name ON entities(name);
CREATE TABLE IF NOT EXISTS relations(
  id TEXT PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL, type TEXT NOT NULL, meta TEXT,
  agent_id TEXT, created_at REAL, UNIQUE(src, dst, type));
CREATE INDEX IF NOT EXISTS rel_src ON relations(src);
CREATE INDEX IF NOT EXISTS rel_dst ON relations(dst);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, agent_id TEXT, project_id TEXT, goal TEXT, status TEXT, state TEXT,
  created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS inbox(
  id TEXT PRIMARY KEY, kind TEXT, from_agent TEXT, to_agent TEXT, project_id TEXT, body TEXT,
  context_ids TEXT, status TEXT, result TEXT, claimed_by TEXT, created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS inbox_to ON inbox(to_agent, status);
CREATE TABLE IF NOT EXISTS conflicts(
  id TEXT PRIMARY KEY, a TEXT, b TEXT, subject TEXT, predicate TEXT, status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS secret_refs(
  ref TEXT PRIMARY KEY, description TEXT, project_id TEXT, allowed_agents TEXT, max_ttl INTEGER,
  created_by TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS leases(
  id TEXT PRIMARY KEY, ref TEXT, agent_id TEXT, scope TEXT, purpose TEXT, expires_at REAL,
  revoked INTEGER DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent_id TEXT, action TEXT, target TEXT, detail TEXT);
"""


PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")


class CommonsError(Exception):
    pass


def default_db_path() -> str:
    return os.environ.get("COMMONS_DB") or str(Path.home() / ".commons" / "commons.db")


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Store:
    def __init__(self, path: str | None = None, embedder: Embedder | None | str = "auto"):
        from .pgcompat import PgDB, is_pg
        self.path = path or default_db_path()
        self.dialect = "postgres" if is_pg(self.path) else "sqlite"
        if self.dialect == "postgres":
            self.db = PgDB(self.path)
        else:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.db = _sqlite_connect(self.path)
        self.lock = threading.RLock()
        self.db.executescript(SCHEMA)
        from .schema_v2 import migrate
        migrate(self.db)
        self.embedder = get_embedder() if embedder == "auto" else (
            get_embedder(embedder) if isinstance(embedder, str) else embedder)
        self._pgvec = False
        if self.dialect == "postgres" and self.embedder:
            dim = self.embedder.dim
            have = self.q("SELECT value FROM meta WHERE key='pgvector_dim'")
            if not have:
                self.db.execute(f"ALTER TABLE embeddings ADD COLUMN IF NOT EXISTS v vector({int(dim)})")
                self.db.execute("CREATE INDEX IF NOT EXISTS emb_hnsw ON embeddings USING hnsw (v vector_cosine_ops)")
                self.x("INSERT OR REPLACE INTO meta VALUES('pgvector_dim', ?)", (str(dim),))
                have = [{"value": str(dim)}]
            self._pgvec = have[0]["value"] == str(dim)  # different-dim embedder -> fall back to python cosine

    def sync_vectors(self) -> int:
        """Postgres: fill the pgvector column from stored embeddings (after a migration)."""
        if not self._pgvec:
            return 0
        n = 0
        for r in self.q("SELECT item_id, vec FROM embeddings WHERE v IS NULL AND model=?", (self.embedder.name,)):
            self.x("UPDATE embeddings SET v=?::vector WHERE item_id=?", (_vecstr(unpack(r["vec"])), r["item_id"]))
            n += 1
        return n

    # ------------------------------------------------------------------ helpers
    def q(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, tuple(args)).fetchall()

    def x(self, sql: str, args: Iterable = ()) -> None:
        with self.lock:
            self.db.execute(sql, tuple(args))

    def audit(self, agent_id: str | None, action: str, target: str = "", detail: Any = None) -> None:
        self.x("INSERT INTO audit(ts,agent_id,action,target,detail) VALUES(?,?,?,?,?)",
               (now(), agent_id, action, target, _j(detail) if detail is not None else None))

    # ------------------------------------------------------------------ agents
    def register_agent(self, name: str, vendor: str = "", model: str = "", description: str = "",
                       capabilities: list[str] | None = None, a2a_url: str = "",
                       issue_key: bool = True) -> dict:
        existing = self.get_agent(name)
        key = f"cmk_{pysecrets.token_urlsafe(24)}" if issue_key else None
        if existing:
            self.x("UPDATE agents SET vendor=COALESCE(NULLIF(?,''),vendor), model=COALESCE(NULLIF(?,''),model),"
                   " description=COALESCE(NULLIF(?,''),description), capabilities=?, a2a_url=COALESCE(NULLIF(?,''),a2a_url),"
                   " key_hash=COALESCE(?,key_hash) WHERE id=?",
                   (vendor, model, description, _j(capabilities or json.loads(existing.get("capabilities") or "[]")),
                    a2a_url, _hash_key(key) if key else None, existing["id"]))
            aid = existing["id"]
        else:
            aid = new_id("agt")
            self.x("INSERT INTO agents(id,name,vendor,model,description,capabilities,key_hash,a2a_url,created_at,last_seen)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (aid, name, vendor, model, description, _j(capabilities or []),
                    _hash_key(key) if key else None, a2a_url, now(), now()))
        self.audit(aid, "agent.register", aid)
        out = {"id": aid, "name": name}
        if key:
            out["api_key"] = key
            out["note"] = "Store this key; it is shown once and only its hash is kept."
        return out

    def get_agent(self, name_or_id: str) -> dict | None:
        r = self.q("SELECT * FROM agents WHERE id=? OR name=?", (name_or_id, name_or_id))
        return dict(r[0]) if r else None

    def ensure_agent(self, name: str) -> str:
        a = self.get_agent(name)
        if a:
            self.x("UPDATE agents SET last_seen=? WHERE id=?", (now(), a["id"]))
            return a["id"]
        try:
            return self.register_agent(name, issue_key=False)["id"]
        except Exception as e:  # noqa: BLE001 - another process/thread registered it first (fresh DB race)
            a = self.get_agent(name)
            if a and "unique" in str(e).lower():
                return a["id"]
            raise

    def authenticate(self, api_key: str) -> dict | None:
        r = self.q("SELECT * FROM agents WHERE key_hash=?", (_hash_key(api_key),))
        return dict(r[0]) if r else None

    def list_agents(self) -> list[dict]:
        return [{k: r[k] for k in ("id", "name", "vendor", "model", "description", "a2a_url")}
                | {"capabilities": json.loads(r["capabilities"] or "[]"), "last_seen": iso(r["last_seen"])}
                for r in self.q("SELECT * FROM agents ORDER BY last_seen DESC")]

    # ------------------------------------------------------------------ projects
    def project_id(self, name: str | None, root: str | None = None) -> str | None:
        if not name:
            return None
        name = name.strip()
        r = self.q("SELECT id FROM projects WHERE name=? OR id=?", (name, name))
        if r:
            return r[0]["id"]
        if not PROJECT_NAME.fullmatch(name):
            raise CommonsError(f"invalid project name {name!r}: use 1-64 letters, digits, '.', '_', '-' or spaces, "
                               "starting with a letter or digit (e.g. 'commons-live')")
        pid = new_id("prj")
        self.x("INSERT INTO projects VALUES(?,?,?,?)", (pid, name, root, now()))
        self.x("INSERT OR IGNORE INTO entities(id,type,name,project_id,path,line,meta,created_at) VALUES(?,?,?,?,?,?,?,?)",
               (pid, "project", name, pid, root, None, "{}", now()))
        return pid

    def project_name(self, pid: str | None) -> str | None:
        if not pid:
            return None
        r = self.q("SELECT name FROM projects WHERE id=?", (pid,))
        return r[0]["name"] if r else None

    # ------------------------------------------------------------------ items (memories, facts, decisions, ...)
    def add_item(self, kind: str, content: str, *, title: str = "", summary: str = "", project: str | None = None,
                 agent_id: str | None = None, session_id: str | None = None, tags: list[str] | None = None,
                 meta: dict | None = None, entities: list[str] | None = None, related_ids: list[str] | None = None,
                 subject: str | None = None, predicate: str | None = None, value: str | None = None,
                 authenticated: bool = False, created_at: float | None = None,
                 check_conflicts: bool = True) -> dict:
        content, f1 = redact(content or "")
        title, f2 = redact(title or "")
        summary, f3 = redact(summary or "")
        value, f4 = redact(value) if value else (value, [])
        meta_s, f5 = redact(_j(meta or {}))
        found = sorted(set(f1 + f2 + f3 + f4 + f5))
        if not summary:
            first = (title + ": " if title else "") + " ".join(content.split())
            summary = first[:160] + ("…" if len(first) > 160 else "")
        if not title:
            title = " ".join(content.split())[:80]
        pid = self.project_id(project)
        self.check_project(pid, "write")
        iid = new_id({"decision": "dec", "handoff": "hof", "checkpoint": "chk", "fact": "fct"}.get(kind, "mem"))
        ts = float(created_at) if created_at is not None else now()  # explicit timestamp for imports/backfills
        tags = [t.strip().lower() for t in (tags or []) if t.strip()]
        self.x("INSERT INTO items(id,kind,project_id,agent_id,session_id,title,summary,content,tags,meta,subject,"
               "predicate,value,authenticated,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (iid, kind, pid, agent_id, session_id, title, summary, content, _j(tags), meta_s,
                subject, predicate, value, int(authenticated), ts, ts))
        self.x("INSERT INTO items_fts(item_id,title,summary,content,tags) VALUES(?,?,?,?,?)",
               (iid, title, summary, content, " ".join(tags)))
        if self.embedder and not getattr(self, "defer_embed", False):  # defer_embed: bulk import, reindex() batches
            vec = self.embedder.embed([f"{title}\n{summary}\n{content}"[:4000]])[0]
            self.x("INSERT OR REPLACE INTO embeddings(item_id,model,vec) VALUES(?,?,?)", (iid, self.embedder.name, pack(vec)))
            if self._pgvec:
                self.x("UPDATE embeddings SET v=?::vector WHERE item_id=?", (_vecstr(vec), iid))
        # graph links
        if pid:
            self.relate(iid, pid, "in_project", agent_id=agent_id)
        if agent_id:
            self.relate(iid, agent_id, "authored_by", agent_id=agent_id)
        if session_id:
            self.relate(iid, session_id, "in_session", agent_id=agent_id)
        for e in entities or []:
            typ, _, nm = e.partition(":") if e.split(":", 1)[0] in ("person", "org", "place", "tool", "concept") else ("", "", e)
            eid = e if self._exists(e) else self.upsert_entity(typ or "concept", nm.strip() or e,
                                                                project_id=None if typ == "person" else pid)
            self.relate(iid, eid, "mentions", agent_id=agent_id)
        for rid in related_ids or []:
            if self._exists(rid):
                self.relate(iid, rid, "related_to", agent_id=agent_id)
        if kind == "decision":
            from .graphlinks import link_item
            link_item(self, iid)
        self.kb_dirty(pid)
        out = {"id": iid, "kind": kind, "summary": summary}
        if found:
            out["redacted"] = found
            self.audit(agent_id, "redact", iid, found)
        if subject and predicate and check_conflicts:
            conflicts = self._check_conflict(iid, subject, predicate, value)
            if conflicts:
                out["conflicts_with"] = conflicts
        return out

    def sync_fts(self, iid: str, reembed: bool = False) -> None:
        """Rewrite an item's keyword-index row (and optionally its vector) from the items table. Every UPDATE of
        title/summary/content/tags must call this, or keyword search keeps matching the old text."""
        r = self.q("SELECT title, summary, content, tags FROM items WHERE id=?", (iid,))
        self.x("DELETE FROM items_fts WHERE item_id=?", (iid,))
        if not r:
            self.x("DELETE FROM embeddings WHERE item_id=?", (iid,))
            return
        r = r[0]
        self.x("INSERT INTO items_fts(item_id,title,summary,content,tags) VALUES(?,?,?,?,?)",
               (iid, r["title"], r["summary"], r["content"], " ".join(json.loads(r["tags"] or "[]"))))
        if reembed and self.embedder:
            vec = self.embedder.embed([f"{r['title']}\n{r['summary']}\n{r['content']}"[:4000]])[0]
            self.x("INSERT OR REPLACE INTO embeddings(item_id,model,vec) VALUES(?,?,?)", (iid, self.embedder.name, pack(vec)))
            if self._pgvec:
                self.x("UPDATE embeddings SET v=?::vector WHERE item_id=?", (_vecstr(vec), iid))

    def update_item(self, iid: str, *, content: str | None = None, title: str | None = None,
                    tags: list[str] | None = None, project: str | None = None, agent_id: str | None = None) -> dict:
        """Edit a memory in place (dashboard inline edit, librarian). Redacted, ACL-checked on the old and the new
        project, keyword index and vector refreshed."""
        r = self.q("SELECT * FROM items WHERE id=?", (iid,))
        if not r:
            raise CommonsError(f"unknown id {iid}")
        r = r[0]
        self.check_project(r["project_id"], "write")
        sets, args, found = [], [], []
        if content is not None and title is None and (r["title"] or "") == " ".join((r["content"] or "").split())[:80]:
            title = " ".join(content.split())[:80]  # an auto-derived title follows the edited content
        if content is not None:
            content, f = redact(content)
            found += f
            summary = " ".join(content.split())  # the summary follows the new content
            sets += ["content=?", "summary=?"]
            args += [content, summary[:160] + ("…" if len(summary) > 160 else "")]
        if title is not None:
            title, f = redact(title)
            found += f
            sets.append("title=?"); args.append(title)
        if tags is not None:
            sets.append("tags=?"); args.append(_j([t.strip().lower() for t in tags if t.strip()]))
        if project is not None:
            pid = self.project_id(project) if project else None
            self.check_project(pid, "write")
            sets.append("project_id=?"); args.append(pid)
            self.x("DELETE FROM relations WHERE src=? AND type='in_project'", (iid,))
            if pid:
                self.relate(iid, pid, "in_project", agent_id=agent_id)
        if not sets:
            return {"id": iid, "changed": False}
        self.x(f"UPDATE items SET {', '.join(sets)}, updated_at=? WHERE id=?", args + [now(), iid])
        self.sync_fts(iid, reembed=content is not None or title is not None)
        self.kb_dirty(r["project_id"])
        self.audit(agent_id, "item.update", iid, {"fields": [x.split("=")[0] for x in sets]})
        return {"id": iid, "changed": True} | ({"redacted": sorted(set(found))} if found else {})

    def delete_item(self, iid: str, agent_id: str | None = None) -> dict:
        """Hard-delete a memory with its keyword row, vector and graph edges (ACL: write on its project)."""
        r = self.q("SELECT project_id, kind FROM items WHERE id=?", (iid,))
        if not r:
            raise CommonsError(f"unknown id {iid}")
        self.check_project(r[0]["project_id"], "write")
        self.x("DELETE FROM items WHERE id=?", (iid,))
        self.sync_fts(iid)
        self.x("DELETE FROM relations WHERE src=? OR dst=?", (iid, iid))
        self.kb_dirty(r[0]["project_id"])
        self.audit(agent_id, "item.delete", iid, {"kind": r[0]["kind"]})
        return {"id": iid, "deleted": True}

    def kb_dirty(self, project_id: str | None) -> None:
        """Mark a knowledge-base bucket stale (rebuilt lazily / by scheduler, debounced)."""
        key = self.project_name(project_id) if project_id else "misc"
        self.x("INSERT INTO kb_state(bucket, dirty, dirtied_at, built_at) VALUES(?,1,?,0) "
               "ON CONFLICT(bucket) DO UPDATE SET dirty=1, dirtied_at=excluded.dirtied_at", (key, now()))

    def _check_conflict(self, iid: str, subject: str, predicate: str, value: str | None) -> list[str]:
        rows = self.q("SELECT id, value FROM items WHERE kind='fact' AND subject=? AND predicate=? AND id<>? "
                      "AND superseded_by IS NULL", (subject, predicate, iid))
        hits = []
        for r in rows:
            if (r["value"] or "").strip().lower() != (value or "").strip().lower():
                hits.append(r["id"])
                self.x("INSERT INTO conflicts VALUES(?,?,?,?,?,?,?)",
                       (new_id("cfl"), r["id"], iid, subject, predicate, "open", now()))
                self.x("UPDATE items SET conflict=1 WHERE id IN (?,?)", (r["id"], iid))
                self.relate(iid, r["id"], "contradicts")
        return hits

    def list_conflicts(self, status: str = "open") -> list[dict]:
        return [dict(r) | {"created_at": iso(r["created_at"])}
                for r in self.q("SELECT * FROM conflicts WHERE status=? ORDER BY created_at DESC", (status,))]

    def resolve_conflict(self, conflict_id: str, keep_id: str, agent_id: str | None = None) -> dict:
        r = self.q("SELECT * FROM conflicts WHERE id=?", (conflict_id,))
        if not r:
            raise CommonsError(f"unknown conflict {conflict_id}")
        c = r[0]
        if keep_id not in (c["a"], c["b"]):
            raise CommonsError("keep_id must be one of the conflicting items")
        loser = c["b"] if keep_id == c["a"] else c["a"]
        self.x("UPDATE items SET superseded_by=? WHERE id=?", (keep_id, loser))
        self.x("UPDATE conflicts SET status='resolved' WHERE id=?", (conflict_id,))
        if not self.q("SELECT 1 FROM conflicts WHERE status='open' AND (a=? OR b=?)", (keep_id, keep_id)):
            self.x("UPDATE items SET conflict=0 WHERE id=?", (keep_id,))
        self.audit(agent_id, "conflict.resolve", conflict_id, {"keep": keep_id, "superseded": loser})
        return {"resolved": conflict_id, "kept": keep_id, "superseded": loser}

    def _exists(self, any_id: str) -> bool:
        for t in ("items", "entities", "sessions", "agents", "inbox", "jobs", "subtasks", "leftovers", "recurring",
                  "preferences", "leases"):
            if self.q(f"SELECT 1 FROM {t} WHERE id=?", (any_id,)):
                return True
        return False

    def get_item(self, iid: str) -> dict | None:
        r = self.q("SELECT * FROM items WHERE id=?", (iid,))
        if not r:
            return None
        d = dict(r[0])
        d["tags"] = json.loads(d["tags"] or "[]")
        d["meta"] = json.loads(d["meta"] or "{}")
        d["project"] = self.project_name(d.pop("project_id"))
        a = self.get_agent(d["agent_id"]) if d["agent_id"] else None
        d["agent"] = a["name"] if a else None
        d["created_at"] = iso(d["created_at"])
        d["updated_at"] = iso(d["updated_at"])
        return d

    # ------------------------------------------------------------------ search
    def search(self, query: str = "", *, mode: str = "hybrid", project: str | None = None, agent: str | None = None,
               tags: list[str] | None = None, kinds: list[str] | None = None, since: str | None = None,
               until: str | None = None, entity: str | None = None, limit: int = 20,
               max_tokens: int = 800, include_superseded: bool = False, as_of: str | float | None = None,
               explain: bool = False, rerank: bool | str | None = None, conflict: bool | None = None) -> dict:
        ranked, total, why = self.rank(query, mode=mode, project=project, agent=agent, tags=tags, kinds=kinds,
                                       since=since, until=until, entity=entity,
                                       limit=max(limit, int(os.environ.get("COMMONS_RERANK_TOP", "30"))) if rerank else limit,
                                       include_superseded=include_superseded, as_of=as_of, conflict=conflict)
        if ranked is None:
            return why
        from .rerank import mode as rr_mode
        from .rerank import rerank_ids
        how = rr_mode() if rerank in (None, False, "") else ("cross-encoder" if rerank is True else str(rerank))
        if query and rerank is not False and how not in ("off", "none"):
            ranked = rerank_ids(self, query, ranked, top=int(os.environ.get("COMMONS_RERANK_TOP", "30")), how=how)
        ranked = ranked[:limit]
        out = self._budgeted(ranked, max_tokens, total=total)
        if explain:
            out["explain"] = {k: why[k] for k in ranked if k in why}
        return out

    def rank(self, query: str = "", *, mode: str = "hybrid", project: str | None = None, agent: str | None = None,
             tags: list[str] | None = None, kinds: list[str] | None = None, since: str | None = None,
             until: str | None = None, entity: str | None = None, limit: int = 20,
             include_superseded: bool = False, as_of: str | float | None = None, conflict: bool | None = None):
        """Ranked item ids (no token budget) -> (ids, total_candidates, per-id signal breakdown)."""
        from . import ranking as R
        where, args = ["1=1"], []
        if conflict:
            where.append("COALESCE(i.conflict,0)=1")
        if project:
            pid = self.q("SELECT id FROM projects WHERE name=? OR id=?", (project, project))
            if not pid:
                return None, 0, {"results": [], "text": "(no such project)", "total": 0}
            where.append("i.project_id=?"); args.append(pid[0]["id"])
        if agent:
            a = self.get_agent(agent)
            where.append("i.agent_id=?"); args.append(a["id"] if a else "__none__")
        if kinds:
            where.append(f"i.kind IN ({','.join('?' * len(kinds))})"); args += kinds
        for t in tags or []:
            where.append("i.tags LIKE ?"); args.append(f'%"{t.lower()}"%')
        if since:
            where.append("i.created_at>=?"); args.append(_parse_time(since))
        if until:
            where.append("i.created_at<=?"); args.append(_parse_time(until))
        if entity:
            ents = [entity] if self._exists(entity) else [r["id"] for r in self.q(
                "SELECT id FROM entities WHERE name=? OR name LIKE ?", (entity, f"%.{entity}"))]
            if not ents:
                return None, 0, {"results": [], "text": "(no such entity)", "total": 0}
            where.append(f"i.id IN (SELECT src FROM relations WHERE dst IN ({','.join('?' * len(ents))}) "
                         f"UNION SELECT dst FROM relations WHERE src IN ({','.join('?' * len(ents))}))")
            args += ents + ents
        if not include_superseded:
            where.append("i.superseded_by IS NULL")
        if as_of not in (None, ""):  # time travel: nothing written after the reference time exists yet
            where.append("i.created_at<=?"); args.append(_parse_time(str(as_of)))
        denied = self.denied_projects("read")
        if denied:
            where.append(f"(i.project_id IS NULL OR i.project_id NOT IN ({','.join('?' * len(denied))}))")
            args += denied
        wsql = " AND ".join(where)
        scores: dict[str, float] = {}
        why: dict[str, dict] = {}
        query = (query or "").strip()
        wk, wv = R.w("keyword", 1.0), R.w("semantic", 1.0 if (self.embedder and self.embedder.name != "hash") else 0.7)
        lex: dict[str, float] = {}  # id -> lexical score (higher is better)
        sem: dict[str, float] = {}  # id -> cosine similarity
        floor = 0.0
        if query and mode in ("hybrid", "keyword"):
            fts = R.fts_query(query)
            if fts and self.dialect == "postgres":
                tsq = " | ".join(t.strip('"') for t in fts.split(" OR "))
                rows = self.q(f"SELECT f.item_id AS id, ts_rank(f.tsv, to_tsquery('english', ?)) AS b FROM items_fts f "
                              f"JOIN items i ON i.id=f.item_id WHERE f.tsv @@ to_tsquery('english', ?) AND {wsql} "
                              f"ORDER BY b DESC LIMIT 200", [tsq, tsq] + args)
            elif fts:
                rows = self.q(f"SELECT f.item_id AS id, -bm25(items_fts) AS b FROM items_fts f JOIN items i ON i.id=f.item_id "
                              f"WHERE items_fts MATCH ? AND {wsql} ORDER BY bm25(items_fts) LIMIT 200", [fts] + args)
            else:
                rows = []
            for rank, r in enumerate(rows):
                lex[r["id"]] = float(r["b"] or 0)
                why.setdefault(r["id"], {})["bm25_rank"] = rank + 1
        if query and mode in ("hybrid", "semantic") and self.embedder:
            qv = self.embedder.embed_query(query)
            if self._pgvec:  # ANN in the database (HNSW)
                rows = self.q(f"SELECT e.item_id AS id, 1-(e.v <=> ?::vector) AS sim FROM embeddings e JOIN items i "
                              f"ON i.id=e.item_id WHERE e.model=? AND e.v IS NOT NULL AND {wsql} "
                              f"ORDER BY e.v <=> ?::vector LIMIT 200", [_vecstr(qv), self.embedder.name] + args + [_vecstr(qv)])
                sims = [(float(r["sim"]), r["id"]) for r in rows]
            else:
                rows = self.q(f"SELECT e.item_id AS id, e.vec FROM embeddings e JOIN items i ON i.id=e.item_id "
                              f"WHERE e.model=? AND {wsql}", [self.embedder.name] + args)
                sims = sorted(((cosine(qv, unpack(r["vec"])), r["id"]) for r in rows), reverse=True)[:200]
            floor = 0.12 if self.embedder.name == "hash" else float(os.environ.get("COMMONS_RANK_SIM_FLOOR", "0.3"))
            for rank, (sim, iid) in enumerate(sims):
                if sim < floor:
                    break
                sem[iid] = sim
                why.setdefault(iid, {}).update(vec_rank=rank + 1, sim=round(sim, 4))
        scores = R.fuse(lex, sem, wk, wv, sem_floor=floor if sem else 0.0)
        if not query:
            rows = self.q(f"SELECT i.id FROM items i WHERE {wsql} ORDER BY i.created_at DESC LIMIT ?", args + [limit])
            ranked = [r["id"] for r in rows]
            total = self.q(f"SELECT COUNT(*) n FROM items i WHERE {wsql}", args)[0]["n"] if len(ranked) >= limit else len(ranked)
            return ranked, total, why
        if scores:
            ids = list(scores)
            meta = {}
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                for r in self.q(f"SELECT id, agent_id, created_at FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                    meta[r["id"]] = r
            ref = _parse_time(str(as_of)) if as_of not in (None, "") else now()
            win = R.time_window(query, ref)
            ents = self._query_entity_items(query)
            w_ent, w_time, w_rec = R.w("entity", 0.3), R.w("time", 0.6), R.w("recency", 0.1)
            half = R.w("half_life_days", 30)
            trust = None
            if os.environ.get("COMMONS_TRUST_WEIGHTING", "1") == "1":  # provenance-weighted ranking
                from .trust import Trust
                trust = Trust(self)
            for k in ids:
                m = meta.get(k)
                if not m:
                    continue
                f, d = 1.0, why.setdefault(k, {})
                d["rrf"] = round(scores[k], 6)
                if k in ents and w_ent:
                    f *= 1 + w_ent; d["entity"] = 1
                if win and w_time:
                    t = R.time_score(m["created_at"], win)
                    f *= 1 + w_time * t; d["time"] = round(t, 3)
                if w_rec:
                    r_ = R.recency(m["created_at"], ref, half)
                    f *= 1 + w_rec * r_; d["recency"] = round(r_, 3)
                if trust is not None:
                    t = trust.score(m["agent_id"])
                    f *= 0.5 + t; d["trust"] = round(t, 3)
                scores[k] *= f
                d["score"] = round(scores[k], 6)
        ranked = [k for k, _ in sorted(scores.items(), key=lambda kv: -kv[1])][:limit]
        return ranked, len(scores), why

    def _query_entity_items(self, query: str) -> set[str]:
        """Items linked to entities whose name appears in the query (graph-aware boost)."""
        from . import ranking as R
        toks = [t for t in R.terms(query) if len(t) >= 3]
        cands = {t.lower() for t in toks} | {f"{a} {b}".lower() for a, b in zip(toks, toks[1:])}
        if not cands:
            return set()
        ph = ",".join("?" * len(cands))
        ents = [r["id"] for r in self.q(f"SELECT id FROM entities WHERE lower(name) IN ({ph}) LIMIT 50", list(cands))]
        if not ents:
            return set()
        ph = ",".join("?" * len(ents))
        rows = self.q(f"SELECT src AS id FROM relations WHERE dst IN ({ph}) UNION SELECT dst FROM relations WHERE src IN ({ph})",
                      ents + ents)
        return {r["id"] for r in rows}



    def reindex(self, limit: int | None = None, batch: int = 64, force: bool = False) -> dict:
        """(Re)compute vectors for items that have none for the active embedder (after switching models)."""
        if not self.embedder:
            return {"embedder": "none", "done": 0, "remaining": 0}
        name = self.embedder.name
        cond = "" if force else "WHERE e.item_id IS NULL OR e.model<>?"
        args = () if force else (name,)
        total = self.q(f"SELECT COUNT(*) n FROM items i LEFT JOIN embeddings e ON e.item_id=i.id {cond}", args)[0]["n"]
        done = 0
        while limit is None or done < limit:
            n = batch if limit is None else min(batch, limit - done)
            rows = self.q(f"SELECT i.id, i.title, i.summary, i.content FROM items i LEFT JOIN embeddings e ON e.item_id=i.id "
                          f"{cond} ORDER BY i.created_at DESC LIMIT ? OFFSET ?", (*args, n, done if force else 0))
            if not rows:
                break
            vecs = self.embedder.embed([f"{r['title'] or ''}\n{r['summary'] or ''}\n{r['content'] or ''}"[:4000] for r in rows])
            for r, v in zip(rows, vecs):
                self.x("INSERT OR REPLACE INTO embeddings(item_id,model,vec) VALUES(?,?,?)", (r["id"], name, pack(v)))
                if self._pgvec:
                    self.x("UPDATE embeddings SET v=?::vector WHERE item_id=?", (_vecstr(v), r["id"]))
            done += len(rows)
        return {"embedder": name, "done": done, "remaining": max(0, total - done)}

    def _budgeted(self, ids: list[str], max_tokens: int, total: int) -> dict:
        lines, results, used = [], [], 0
        hidden = self.hidden_ids(ids)
        for iid in ids:
            if iid in hidden:
                continue
            r = self.q("SELECT i.*, a.name AS agent_name, p.name AS project_name FROM items i "
                       "LEFT JOIN agents a ON a.id=i.agent_id LEFT JOIN projects p ON p.id=i.project_id WHERE i.id=?",
                       (iid,))
            if not r:
                continue
            r = r[0]
            flag = " ⚠conflict" if r["conflict"] else ""
            if r["agent_id"]:
                from .trust import Trust
                if Trust(self).score(r["agent_id"]) < 0.3:
                    flag += " ⚠low-trust"
            line = (f"{r['id']} [{r['kind']}] {iso(r['created_at'])[:10]} "
                    f"{r['project_name'] or '-'}/{r['agent_name'] or '-'}{flag}: {r['summary']}")
            t = estimate_tokens(line)
            if used + t > max_tokens and results:
                break
            if t > max_tokens:  # a single hit bigger than the whole budget: trim it rather than blow the budget
                line = line[: max(40, max_tokens * 4)] + "…"
                t = estimate_tokens(line)
            used += t
            lines.append(line)
            results.append({"id": r["id"], "kind": r["kind"], "summary": r["summary"], "project": r["project_name"],
                            "agent": r["agent_name"], "created_at": iso(r["created_at"]), "conflict": bool(r["conflict"]),
                            "authenticated": bool(r["authenticated"])})
        more = max(0, total - len(results))
        text = "\n".join(lines) or "(no results)"
        if more:
            text += f"\n… {more} more (raise max_tokens/limit or refine filters)"
        return {"results": results, "text": text, "total": total, "tokens": used,
                "hint": "use expand(id) for full content"}

    def denied_projects(self, need: str = "read") -> list[str]:
        from .auth import ACL, PRINCIPAL
        return ACL(self).denied_projects(PRINCIPAL.get(), need)

    def check_project(self, project_id: str | None, need: str = "read") -> None:
        from .auth import ACL, PRINCIPAL
        ACL(self).check(PRINCIPAL.get(), project_id, need)

    def find_project(self, name: str | None) -> str | None:
        """Project id by name or id, or None. Reads use this: project_id() creates missing projects."""
        if not name:
            return None
        r = self.q("SELECT id FROM projects WHERE name=? OR id=?", (name.strip(), name.strip()))
        return r[0]["id"] if r else None

    _PROJ_TABLES = ("items", "entities", "sessions", "inbox", "jobs", "leftovers", "recurring", "preferences",
                    "context_packs")

    def hidden_ids(self, ids, need: str = "read") -> set[str]:
        """The given ids that belong to a project the caller may not read: the one ACL filter for graph reads,
        expand and budgeted listings (batched, so it is cheap on big graph exports)."""
        denied = self.denied_projects(need)
        ids = [i for i in ids if isinstance(i, str)]
        if not denied or not ids:
            return set()
        out = {i for i in ids if i in set(denied)}
        dph = ",".join("?" * len(denied))
        for i in range(0, len(ids), 400):
            ch = ids[i:i + 400]
            ph = ",".join("?" * len(ch))
            for t in self._PROJ_TABLES:
                out |= {r["id"] for r in self.q(f"SELECT id FROM {t} WHERE id IN ({ph}) AND project_id IN ({dph})",
                                                ch + denied)}
            out |= {r["id"] for r in self.q(f"SELECT s.id FROM subtasks s JOIN jobs j ON j.id=s.job_id WHERE s.id IN "
                                            f"({ph}) AND j.project_id IN ({dph})", ch + denied)}
        return out

    def check_ids(self, *ids: str) -> None:
        if self.hidden_ids(list(ids)):
            raise CommonsError("access denied: that record belongs to a project you can't read")

    def expand(self, any_id: str, depth: int = 0, max_tokens: int = 2000) -> dict:
        self.check_ids(any_id)
        d = self.get_item(any_id)
        if d is None:
            r = self.q("SELECT * FROM entities WHERE id=?", (any_id,))
            if r:
                d = dict(r[0]) | {"meta": json.loads(r[0]["meta"] or "{}"), "created_at": iso(r[0]["created_at"])}
            else:
                for t in ("sessions", "inbox", "jobs", "subtasks", "leftovers", "preferences", "recurring", "leases"):
                    r = self.q(f"SELECT * FROM {t} WHERE id=?", (any_id,))
                    if r:
                        d = dict(r[0])
                        for k in ("state", "context_ids", "result", "deps", "gates", "budget", "used", "evidence"):
                            if k in d and d[k] and d[k][:1] in "[{":
                                d[k] = json.loads(d[k])
                        break
        if d is None:
            a = self.get_agent(any_id)
            if a:
                a.pop("key_hash", None)
                d = a
        if d is None:
            raise CommonsError(f"unknown id {any_id}")
        content = d.get("content")
        if isinstance(content, str) and estimate_tokens(content) > max_tokens:
            d["content"] = content[: max_tokens * 4] + "\n…[truncated; raise max_tokens]"
        if depth > 0:
            d["neighbors"] = self.neighbors(any_id, depth=depth, limit=50)["edges"]
        return d

    def timeline(self, project: str | None = None, entity: str | None = None, agent: str | None = None,
                 since: str | None = None, until: str | None = None, kinds: list[str] | None = None,
                 limit: int = 50, max_tokens: int = 800) -> dict:
        res = self.search("", project=project, entity=entity, agent=agent, since=since, until=until, kinds=kinds,
                          limit=limit, max_tokens=max_tokens, include_superseded=True)
        # chronological order (oldest first) reads better for timelines
        if res["results"]:
            lines = res["text"].split("\n")
            footer = [ln for ln in lines if ln.startswith("… ")]
            body = [ln for ln in lines if not ln.startswith("… ")]
            res["results"].reverse()
            res["text"] = "\n".join(list(reversed(body)) + footer)
        return res

    # ------------------------------------------------------------------ graph
    def upsert_entity(self, type_: str, name: str, project_id: str | None = None, path: str | None = None,
                      line: int | None = None, meta: dict | None = None) -> str:
        r = self.q("SELECT id FROM entities WHERE type=? AND name=? AND project_id IS ? AND path IS ?",
                   (type_, name, project_id, path))
        if r:
            if line is not None or meta:
                self.x("UPDATE entities SET line=COALESCE(?,line), meta=COALESCE(?,meta) WHERE id=?",
                       (line, _j(meta) if meta else None, r[0]["id"]))
            return r[0]["id"]
        eid = new_id("ent")
        self.x("INSERT INTO entities VALUES(?,?,?,?,?,?,?,?)",
               (eid, type_, name, project_id, path, line, _j(meta or {}), now()))
        return eid

    def relate(self, src: str, dst: str, type_: str, meta: dict | None = None, agent_id: str | None = None) -> str:
        rid = new_id("rel")
        self.x("INSERT OR IGNORE INTO relations(id,src,dst,type,meta,agent_id,created_at) VALUES(?,?,?,?,?,?,?)", (rid, src, dst, type_, _j(meta or {}), agent_id, now()))
        return rid

    def label(self, any_id: str) -> tuple[str, str]:
        """(type, display name) for any id."""
        if not any_id:
            return "", ""
        r = self.q("SELECT type, name FROM entities WHERE id=?", (any_id,))
        if r:
            return r[0]["type"], r[0]["name"]
        r = self.q("SELECT kind, title FROM items WHERE id=?", (any_id,))
        if r:
            return r[0]["kind"], (r[0]["title"] or "")[:60]
        r = self.q("SELECT name FROM agents WHERE id=?", (any_id,))
        if r:
            return "agent", r[0]["name"]
        r = self.q("SELECT goal FROM sessions WHERE id=?", (any_id,))
        if r:
            return "session", (r[0]["goal"] or "")[:60]
        r = self.q("SELECT body FROM inbox WHERE id=?", (any_id,))
        if r:
            return "task", (r[0]["body"] or "")[:60]
        if "://" in any_id:
            return "secret_ref", any_id
        if any_id.startswith("usg_"):
            r = self.q("SELECT cost, tokens_in, tokens_out, model FROM usage WHERE id=?", (int(any_id[4:]),))
            if r:
                return "usage", f"${r[0]['cost']:.4f} {r[0]['model'] or ''} ({(r[0]['tokens_in'] or 0) + (r[0]['tokens_out'] or 0)} tok)"
        for t, col, typ in (("jobs", "title", "job"), ("subtasks", "title", "subtask"), ("leftovers", "text", "leftover"),
                            ("recurring", "name", "schedule"), ("preferences", "statement", "preference"),
                            ("leases", "ref", "lease")):
            r = self.q(f"SELECT {col} AS v FROM {t} WHERE id=?", (any_id,))
            if r:
                return typ, (r[0]["v"] or "")[:60]
        return "unknown", any_id

    def neighbors(self, any_id: str, depth: int = 1, rel_types: list[str] | None = None,
                  direction: str = "both", limit: int = 100) -> dict:
        seen, frontier, edges = {any_id}, [any_id], []
        for _ in range(max(1, depth)):
            nxt = []
            for node in frontier:
                qs = []
                if direction in ("both", "out"):
                    qs.append(("SELECT src, dst, type FROM relations WHERE src=?", "dst"))
                if direction in ("both", "in"):
                    qs.append(("SELECT src, dst, type FROM relations WHERE dst=?", "src"))
                for sql, other in qs:
                    for r in self.q(sql, (node,)):
                        if rel_types and r["type"] not in rel_types:
                            continue
                        edges.append({"src": r["src"], "type": r["type"], "dst": r["dst"]})
                        o = r[other]
                        if o not in seen:
                            seen.add(o)
                            nxt.append(o)
                        if len(edges) >= limit:
                            break
            frontier = nxt
            if len(edges) >= limit:
                break
        hidden = self.hidden_ids(list(seen))
        if any_id in hidden:
            raise CommonsError("access denied: that record belongs to a project you can't read")
        edges = [e for e in edges if e["src"] not in hidden and e["dst"] not in hidden]
        nodes = {n: dict(zip(("type", "name"), self.label(n))) for n in seen - hidden}
        text = "\n".join(f"{nodes[e['src']]['name']} -{e['type']}-> {nodes[e['dst']]['name']}  ({e['src']}→{e['dst']})"
                         for e in edges[:limit])
        return {"root": any_id, "nodes": nodes, "edges": edges[:limit], "text": text or "(no neighbors)"}

    def graph_query(self, name: str | None = None, type_: str | None = None, project: str | None = None,
                    rel_type: str | None = None, depth: int = 1, limit: int = 100) -> dict:
        """Pattern query: find entities matching (name LIKE, type, project), then expand along rel_type."""
        where, args = ["1=1"], []
        if name:
            where.append("name LIKE ?"); args.append(f"%{name}%")
        if type_:
            where.append("type=?"); args.append(type_)
        if project:
            pid = self.find_project(project)
            self.check_project(pid)
            where.append("project_id=?"); args.append(pid or "__none__")
        roots = self.q(f"SELECT id,type,name,path,line FROM entities WHERE {' AND '.join(where)} LIMIT ?",
                       args + [min(limit, 50)])
        hidden = self.hidden_ids([r["id"] for r in roots])
        roots = [r for r in roots if r["id"] not in hidden]
        nodes, edges = {}, []
        for r in roots:
            nodes[r["id"]] = {"type": r["type"], "name": r["name"]}
            if depth > 0:
                nb = self.neighbors(r["id"], depth=depth, rel_types=[rel_type] if rel_type else None, limit=limit)
                nodes.update(nb["nodes"])
                edges += [e for e in nb["edges"] if e not in edges]
        return {"matches": [dict(r) for r in roots], "nodes": nodes, "edges": edges[:limit]}

    def export_graph(self, project: str | None = None, include_items: bool = True, limit: int = 5000) -> dict:
        if project:
            pid = self.find_project(project)
            if not pid:
                return {"nodes": {}, "edges": []}
            self.check_project(pid)
            ids = {r["id"] for r in self.q("SELECT id FROM entities WHERE project_id=? LIMIT ?", (pid, limit))}
            if include_items:
                ids |= {r["id"] for r in self.q("SELECT id FROM items WHERE project_id=? LIMIT ?", (pid, limit))}
            rels, idl = [], list(ids)
            for i in range(0, len(idl), 400):
                ch = idl[i:i + 400]
                rels += [dict(r) for r in self.q(f"SELECT src,dst,type FROM relations WHERE src IN ({','.join('?' * len(ch))})", ch)
                         if r["dst"] in ids]
        else:
            rels = [dict(r) for r in self.q("SELECT src,dst,type FROM relations LIMIT ?", (limit,))]
            ids = {r["src"] for r in rels} | {r["dst"] for r in rels}
            if not include_items:
                ids -= {r["id"] for r in self.q("SELECT id FROM items")}
        ids -= self.hidden_ids(list(ids))
        rels = [r for r in rels if r["src"] in ids and r["dst"] in ids]
        nodes = {i: dict(zip(("type", "name"), self.label(i))) for i in ids}
        return {"nodes": nodes, "edges": rels}

    # ------------------------------------------------------------------ sessions & handoff
    def session_start(self, goal: str, agent_id: str | None, project: str | None = None, state: dict | None = None) -> dict:
        goal, _ = redact(goal)
        sid = new_id("ses")
        pid = self.project_id(project)
        st, _ = redact(_j(state or {}))
        self.x("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (sid, agent_id, pid, goal, "active", st, now(), now()))
        if pid:
            self.relate(sid, pid, "in_project", agent_id=agent_id)
        if agent_id:
            self.relate(sid, agent_id, "run_by", agent_id=agent_id)
        return {"session_id": sid, "goal": goal, "project": project}

    def session_for(self, agent_id: str | None, project: str | None, goal: str) -> str:
        """The caller's latest active session (in `project` when given), or a new one: handoff without session_start."""
        where, args = ["status='active'"], []
        if agent_id:
            where.append("agent_id=?"); args.append(agent_id)
        if project:
            where.append("project_id=?"); args.append(self.project_id(project))
        r = self.q(f"SELECT id FROM sessions WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT 1", args) \
            if agent_id else []
        return r[0]["id"] if r else self.session_start(" ".join(goal.split())[:120], agent_id, project)["session_id"]

    def _session(self, sid: str) -> sqlite3.Row:
        r = self.q("SELECT * FROM sessions WHERE id=?", (sid,))
        if not r:
            raise CommonsError(f"unknown session {sid}")
        return r[0]

    def checkpoint(self, sid: str, note: str, state: dict | None = None, agent_id: str | None = None,
                   related_ids: list[str] | None = None) -> dict:
        s = self._session(sid)
        merged = json.loads(s["state"] or "{}") | (state or {})
        st, _ = redact(_j(merged))
        self.x("UPDATE sessions SET state=?, updated_at=? WHERE id=?", (st, now(), sid))
        return self.add_item("checkpoint", note + ("\n\nstate: " + _j(state) if state else ""), title=f"checkpoint: {note[:60]}",
                             project=s["project_id"], agent_id=agent_id or s["agent_id"], session_id=sid,
                             related_ids=related_ids, tags=["checkpoint"])

    def handoff(self, sid: str, *, summary: str, state: str = "", decisions: list[str] | None = None,
                next_steps: list[str] | None = None, open_questions: list[str] | None = None,
                relevant_ids: list[str] | None = None, to_agent: str | None = None, agent_id: str | None = None,
                goal: str | None = None) -> dict:
        s = self._session(sid)
        packet = {"session_id": sid, "goal": goal or s["goal"], "summary": summary, "state": state,
                  "decisions": decisions or [], "next_steps": next_steps or [], "open_questions": open_questions or [],
                  "relevant_ids": relevant_ids or [], "from_agent": self.label(agent_id or s["agent_id"])[1] if (agent_id or s["agent_id"]) else None,
                  "to_agent": to_agent, "project": self.project_name(s["project_id"]),
                  "secrets_note": "Handoffs carry secret references (vault://, env://, op://) only, never values."}
        body = _render_packet(packet)
        res = self.add_item("handoff", body, title=f"handoff: {packet['goal'][:60]}", summary=f"HANDOFF {summary[:140]}",
                            project=s["project_id"], agent_id=agent_id or s["agent_id"], session_id=sid,
                            related_ids=relevant_ids, tags=["handoff"], meta={"packet": packet})
        self.x("UPDATE sessions SET status='handed_off', updated_at=? WHERE id=?", (now(), sid))
        # unfinished work becomes leftovers so any agent can pick it up later
        for text, typ, prio in [(x, "general", 2) for x in next_steps or []] + \
                [(x, "research", 1) for x in open_questions or []]:
            lid = new_id("lft")
            self.x("INSERT INTO leftovers VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (lid, redact(text)[0], typ, prio, s["project_id"], sid, None, "open", None,
                    agent_id or s["agent_id"], now(), now()))
            self.relate(lid, sid, "left_over_from")
            self.relate(lid, s["project_id"], "in_project") if s["project_id"] else None
        self.kb_dirty(s["project_id"])
        if to_agent:
            t = self.post_task(agent_id or s["agent_id"], to_agent, f"Resume session {sid}: {summary}", kind="handoff",
                               context_ids=[res["id"], sid] + (relevant_ids or []), project=s["project_id"])
            res["task_id"] = t["task_id"]
        res["packet"] = self.get_item(res["id"])["meta"]["packet"]
        return res

    def resume(self, sid: str | None = None, project: str | None = None, agent_id: str | None = None,
               max_tokens: int = 1200) -> dict:
        if not sid:
            where, args = "1=1", []
            if project:  # look up only: a read must not create a project from a typo
                p = self.q("SELECT id FROM projects WHERE name=? OR id=?", (project.strip(), project.strip()))
                where, args = "project_id=?", [p[0]["id"] if p else "__none__"]
            r = self.q(f"SELECT id FROM sessions WHERE {where} ORDER BY updated_at DESC LIMIT 1", args)
            if not r:
                return {"session_id": None, "handoff": None,
                        "note": "no sessions yet" + (f" in project {project!r}" if project else "") +
                                ": nothing to resume, start fresh (handoff() before you stop)."}
            sid = r[0]["id"]
        s = self._session(sid)
        h = self.q("SELECT id FROM items WHERE session_id=? AND kind='handoff' ORDER BY created_at DESC LIMIT 1", (sid,))
        packet = self.get_item(h[0]["id"])["meta"].get("packet") if h else None
        recent = self.search("", kinds=["checkpoint", "decision", "memory", "fact"], limit=15,
                             max_tokens=max_tokens // 2, **({"project": s["project_id"]} if s["project_id"] else {}))
        in_session = [dict(r) for r in self.q("SELECT id, kind, summary FROM items WHERE session_id=? ORDER BY created_at",
                                             (sid,))]
        if agent_id and s["status"] == "handed_off":
            self.x("UPDATE sessions SET status='active', agent_id=?, updated_at=? WHERE id=?", (agent_id, now(), sid))
            self.relate(sid, agent_id, "resumed_by", agent_id=agent_id)
        return {"session_id": sid, "goal": s["goal"], "status": s["status"], "state": json.loads(s["state"] or "{}"),
                "handoff": packet, "session_items": in_session[-20:], "recent_project_context": recent["text"]}

    # ------------------------------------------------------------------ reasoning log
    def record_decision(self, what: str, why: str, alternatives: list[str] | None = None, agent_id: str | None = None,
                        project: str | None = None, session_id: str | None = None, related_ids: list[str] | None = None,
                        evidence: list[str] | None = None, entities: list[str] | None = None,
                        tags: list[str] | None = None) -> dict:
        content = f"DECISION: {what}\nWHY: {why}"
        if alternatives:
            content += "\nALTERNATIVES CONSIDERED:\n" + "\n".join(f"- {a}" for a in alternatives)
        if evidence:
            content += "\nEVIDENCE:\n" + "\n".join(f"- {e}" for e in evidence)
        return self.add_item("decision", content, title=what, summary=f"{what} — because {why}"[:200], project=project,
                             agent_id=agent_id, session_id=session_id, related_ids=related_ids, entities=entities,
                             tags=(tags or []) + ["decision"],
                             meta={"what": what, "why": why, "alternatives": alternatives or [], "evidence": evidence or []})

    def why(self, query: str, project: str | None = None, max_tokens: int = 800) -> dict:
        """Return decisions linked to an id, or matching a free-text query."""
        if self._exists(query):
            ids = {r["src"] for r in self.q("SELECT src FROM relations WHERE dst=?", (query,))} | \
                  {r["dst"] for r in self.q("SELECT dst FROM relations WHERE src=?", (query,))}
            rows = [i for i in ids if self.q("SELECT 1 FROM items WHERE id=? AND kind='decision'", (i,))]
            if self.q("SELECT 1 FROM items WHERE id=? AND kind='decision'", (query,)):
                rows.insert(0, query)
            res = self._budgeted(rows, max_tokens, len(rows))
        else:
            res = self.search(query, kinds=["decision"], project=project, max_tokens=max_tokens)
        res["decisions"] = [self.get_item(r["id"])["meta"] | {"id": r["id"], "agent": r["agent"],
                                                              "at": r["created_at"]} for r in res["results"][:5]]
        return res

    # ------------------------------------------------------------------ inbox / delegation
    def post_task(self, from_agent: str | None, to_agent: str, body: str, kind: str = "task",
                  context_ids: list[str] | None = None, project: str | None = None) -> dict:
        body, found = redact(body)
        to = self.get_agent(to_agent)
        to_id = to["id"] if to else ("*" if to_agent in ("*", "any") else self.ensure_agent(to_agent))
        tid = new_id("tsk")
        self.x("INSERT INTO inbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (tid, kind, from_agent, to_id, self.project_id(project) if project and not project.startswith("prj_") else project,
                body, _j(context_ids or []), "open", None, None, now(), now()))
        if from_agent:
            self.relate(tid, from_agent, "requested_by")
        if to_id != "*":
            self.relate(tid, to_id, "assigned_to")
            from .messaging import emit
            emit(self, to_id, {"notification": "escalation", "handoff": "handoff"}.get(kind, "inbox"), ref=tid, preview=body,
                 sender=self.label(from_agent)[1] if from_agent else None)
        for c in context_ids or []:
            if self._exists(c):
                self.relate(tid, c, "context")
        return {"task_id": tid, "to": to_id, "status": "open"} | ({"redacted": found} if found else {})

    def inbox(self, agent_id: str, status: str = "open", claim: bool = False, limit: int = 20) -> list[dict]:
        rows = self.q("SELECT * FROM inbox WHERE (to_agent=? OR to_agent='*') AND status=? ORDER BY created_at LIMIT ?",
                      (agent_id, status, limit))
        out = []
        for r in rows:
            d = dict(r) | {"context_ids": json.loads(r["context_ids"] or "[]"), "created_at": iso(r["created_at"]),
                           "updated_at": iso(r["updated_at"]), "from": self.label(r["from_agent"])[1] if r["from_agent"] else None}
            if claim and r["status"] == "open":
                with self.lock:  # only report a claim this caller actually won (two agents may race for a '*' task)
                    won = self.db.execute("UPDATE inbox SET status='claimed', claimed_by=?, updated_at=? WHERE id=? "
                                          "AND status='open'", (agent_id, now(), r["id"])).rowcount == 1
                if not won:
                    continue
                d["status"], d["claimed_by"] = "claimed", agent_id
            out.append(d)
        return out

    def respond_task(self, task_id: str, result: str, status: str = "done", agent_id: str | None = None) -> dict:
        result, found = redact(result)
        if not self.q("SELECT 1 FROM inbox WHERE id=?", (task_id,)):
            raise CommonsError(f"unknown task {task_id}")
        self.x("UPDATE inbox SET status=?, result=?, claimed_by=COALESCE(claimed_by,?), updated_at=? WHERE id=?",
               (status, result, agent_id, now(), task_id))
        return {"task_id": task_id, "status": status} | ({"redacted": found} if found else {})

    def get_task(self, task_id: str, agent_id: str | None = None) -> dict:
        r = self.q("SELECT * FROM inbox WHERE id=?", (task_id,))
        if not r:
            raise CommonsError(f"unknown task {task_id}")
        from .auth import PRINCIPAL
        if PRINCIPAL.get() is not None and agent_id not in (r[0]["from_agent"], r[0]["to_agent"], r[0]["claimed_by"]) \
                and r[0]["to_agent"] != "*":
            raise CommonsError(f"task {task_id} is between other agents")
        self.check_ids(task_id)
        return dict(r[0]) | {"context_ids": json.loads(r[0]["context_ids"] or "[]"),
                             "created_at": iso(r[0]["created_at"]), "updated_at": iso(r[0]["updated_at"])}

    def stats(self) -> dict:
        return {t: self.q(f"SELECT COUNT(*) c FROM {t}")[0]["c"] for t in
                ("items", "entities", "relations", "sessions", "agents", "projects", "inbox", "conflicts", "leases")} | {
            "embedder": self.embedder.name if self.embedder else "none", "db": self.path}


def _render_packet(p: dict) -> str:
    def lst(xs):
        return "\n".join(f"- {x}" for x in xs) or "- (none)"
    return (f"GOAL: {p['goal']}\nSUMMARY: {p['summary']}\nSTATE: {p['state'] or '-'}\n"
            f"DECISIONS:\n{lst(p['decisions'])}\nNEXT STEPS:\n{lst(p['next_steps'])}\n"
            f"OPEN QUESTIONS:\n{lst(p['open_questions'])}\nRELEVANT IDS: {', '.join(p['relevant_ids']) or '-'}")


def _vecstr(v) -> str:
    return "[" + ",".join(f"{x:.6g}" for x in v) + "]"


def _sqlite_connect(path: str):
    """SQLite connection; uses SQLCipher (encrypted at rest) when COMMONS_DB_KEY is set and sqlcipher3 is installed."""
    key = os.environ.get("COMMONS_DB_KEY")
    if key:
        try:
            import sqlcipher3.dbapi2 as sq  # type: ignore
        except ImportError as e:
            raise CommonsError("COMMONS_DB_KEY is set but sqlcipher3 is not installed: pip install 'commons-memory[encrypt]'") from e
        db = sq.connect(path, check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA key = " + "'" + key.replace("'", "''") + "'")
        try:
            db.execute("SELECT count(*) FROM sqlite_master").fetchall()
        except Exception as e:  # noqa: BLE001
            raise CommonsError("cannot open encrypted database: wrong COMMONS_DB_KEY?") from e
        db.row_factory = sq.Row
        return db
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def _fts_query(q: str) -> str:
    import re
    toks = re.findall(r"[A-Za-z0-9_]+", q)
    return " OR ".join(f'"{t}"' for t in toks[:32])


def _parse_time(s: str) -> float:
    import datetime as dt
    import re
    s = s.strip()
    m = re.fullmatch(r"(\d+)\s*([smhdw])", s)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return now() - int(m.group(1)) * mult
    try:
        return float(s)
    except ValueError:
        pass
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()
