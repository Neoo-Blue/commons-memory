"""The Librarian: a built-in agent that keeps Commons organized.

Runs as (a) a configured model via the provider layer (role `librarian`), (b) any connected agent that claims
`librarian` subtasks (`librarian_run(delegate=True)`), or (c) heuristics only. Each run is incremental (items
since the last watermark), budget-capped, and every change is logged with provenance and is undoable.
Destructive changes (merges) go to a review queue unless auto-approve is on.

Jobs: dedupe/merge near-duplicates · auto-tag · assign unassigned items to projects · fix graph links ·
flag stale/contradicting items · summaries per project/topic/tree node · "where to find X" index + suggested
queries · KB refresh · skill-pool tidying.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter

from .embed import HashEmbedder, cosine
from .ids import estimate_tokens, iso, new_id, now
from .kb import STOP, _terms
from .store import CommonsError, Store

DUP_THRESHOLD = float(os.environ.get("COMMONS_LIBRARIAN_DUP", "0.9"))
STALE_DAYS = float(os.environ.get("COMMONS_LIBRARIAN_STALE_DAYS", "90"))


class Librarian:
    def __init__(self, store: Store):
        self.s = store
        self.agent_id = store.ensure_agent("librarian")
        self._emb = HashEmbedder()

    # ------------------------------------------------------------------ config
    def auto_approve(self) -> bool:
        r = self.s.q("SELECT value FROM meta WHERE key='librarian_auto_approve'")
        return bool(r and r[0]["value"] == "1") or os.environ.get("COMMONS_LIBRARIAN_AUTO_APPROVE") == "1"

    def configure(self, auto_approve: bool | None = None, budget: float | None = None) -> dict:
        if auto_approve is not None:
            self.s.x("INSERT OR REPLACE INTO meta VALUES('librarian_auto_approve', ?)", ("1" if auto_approve else "0",))
        if budget is not None:
            self.s.x("INSERT OR REPLACE INTO meta VALUES('librarian_budget', ?)", (str(budget),))
        return {"auto_approve": self.auto_approve(), "budget": self.budget()}

    def budget(self) -> float:
        r = self.s.q("SELECT value FROM meta WHERE key='librarian_budget'")
        return float(r[0]["value"]) if r else float(os.environ.get("COMMONS_LIBRARIAN_BUDGET", "0.10"))

    def _watermark(self) -> float:
        r = self.s.q("SELECT value FROM meta WHERE key='librarian_watermark'")
        return float(r[0]["value"]) if r else 0.0

    # ------------------------------------------------------------------ logging / undo
    def _log(self, run_id, action, target, before, after, reversible=True) -> str:
        lid = new_id("lib")
        self.s.x("INSERT INTO librarian_log VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (lid, run_id, now(), action, target, json.dumps(before, default=str), json.dumps(after, default=str),
                  self.agent_id, "applied", int(reversible)))
        return lid

    def undo(self, log_id: str) -> dict:
        r = self.s.q("SELECT * FROM librarian_log WHERE id=?", (log_id,))
        if not r:
            raise CommonsError("unknown librarian log entry")
        e = r[0]
        if e["status"] != "applied" or not e["reversible"]:
            raise CommonsError("entry not undoable")
        b = json.loads(e["before"] or "{}")
        if e["action"] == "tag":
            self.s.x("UPDATE items SET tags=? WHERE id=?", (json.dumps(b["tags"]), e["target"]))
            self.s.sync_fts(e["target"])
        elif e["action"] == "assign_project":
            self.s.x("UPDATE items SET project_id=? WHERE id=?", (b["project_id"], e["target"]))
            self.s.x("DELETE FROM relations WHERE src=? AND dst=? AND type='in_project'",
                     (e["target"], json.loads(e["after"])["project_id"]))
        elif e["action"] == "merge":
            self.s.x("UPDATE items SET superseded_by=NULL WHERE id=?", (e["target"],))
            self.s.x("DELETE FROM relations WHERE src=? AND type='merged_into'", (e["target"],))
        elif e["action"] == "summary":
            if b.get("summary") is None:
                self.s.x("DELETE FROM node_summaries WHERE key=?", (e["target"],))
            else:
                self.s.x("INSERT OR REPLACE INTO node_summaries VALUES(?,?,?,?,?)",
                         (e["target"], b.get("path"), b["summary"], b.get("source"), now()))
        elif e["action"] == "skill_description":
            self.s.x("UPDATE skills SET description=? WHERE id=?", (b["description"], e["target"]))
        self.s.x("UPDATE librarian_log SET status='undone' WHERE id=?", (log_id,))
        self.s.audit(None, "librarian.undo", log_id, e["action"])
        return {"undone": log_id, "action": e["action"], "target": e["target"]}

    # ------------------------------------------------------------------ proposals
    def _propose(self, kind, target, payload, reason) -> str | None:
        if self.s.q("SELECT 1 FROM librarian_proposals WHERE kind=? AND target=? AND status='proposed'", (kind, target)):
            return None
        pid = new_id("lbp")
        self.s.x("INSERT INTO librarian_proposals VALUES(?,?,?,?,?,?,?,?)",
                 (pid, kind, target, json.dumps(payload), reason, "proposed", now(), None))
        return pid

    def proposals(self, status: str = "proposed") -> list[dict]:
        return [dict(r) | {"payload": json.loads(r["payload"]), "created_at": iso(r["created_at"]),
                           "target_label": self.s.label(r["target"])[1]}
                for r in self.s.q("SELECT * FROM librarian_proposals WHERE status=? ORDER BY created_at DESC", (status,))]

    def decide(self, proposal_id: str, approve: bool) -> dict:
        r = self.s.q("SELECT * FROM librarian_proposals WHERE id=?", (proposal_id,))
        if not r:
            raise CommonsError("unknown proposal")
        p = r[0]
        out = {"proposal": proposal_id, "status": "rejected"}
        if approve:
            pay = json.loads(p["payload"])
            if p["kind"] == "merge":
                out["log"] = self._merge(new_id("run"), pay["keep"], pay["dup"])
            out["status"] = "applied"
        self.s.x("UPDATE librarian_proposals SET status=?, decided_at=? WHERE id=?", (out["status"], now(), proposal_id))
        return out

    def _merge(self, run_id, keep, dup) -> str:
        self.s.x("UPDATE items SET superseded_by=? WHERE id=?", (keep, dup))
        self.s.relate(dup, keep, "merged_into", agent_id=self.agent_id)
        # carry the duplicate's links over to the survivor
        for r in self.s.q("SELECT dst, type FROM relations WHERE src=? AND type IN ('mentions','related_to','about')", (dup,)):
            self.s.relate(keep, r["dst"], r["type"], agent_id=self.agent_id)
        return self._log(run_id, "merge", dup, {"superseded_by": None}, {"superseded_by": keep})

    # ------------------------------------------------------------------ run
    def run(self, full: bool = False, max_items: int = 500) -> dict:
        run_id = new_id("run")
        since = 0.0 if full else self._watermark()
        items = [dict(r) for r in self.s.q("SELECT * FROM items WHERE created_at>? AND superseded_by IS NULL "
                                           "ORDER BY created_at LIMIT ?", (since, max_items))]
        report: dict = {"run_id": run_id, "items_scanned": len(items), "mode": self.mode()}
        report["dedupe"] = self._dedupe(run_id, items)
        report["tagged"] = self._autotag(run_id, items)
        report["assigned"] = self._assign(run_id, items)
        from .graphlinks import rebuild
        report["links_added"] = rebuild(self.s)["added"]
        report["contradictions"] = self._contradictions(run_id, items)
        report["flags"] = self._flags(run_id)
        report["skills_tidied"] = self._skills(run_id)
        from .kb import KB
        report["kb_rebuilt"] = KB(self.s).refresh_dirty(force=True)
        report["summaries"] = self._summaries(run_id)
        report["indexed"] = self.build_index()
        if items:
            self.s.x("INSERT OR REPLACE INTO meta VALUES('librarian_watermark', ?)", (str(items[-1]["created_at"]),))
        self.s.x("INSERT OR REPLACE INTO meta VALUES('librarian_last_run', ?)", (json.dumps(report | {"ts": now()}),))
        self.s.audit(self.agent_id, "librarian.run", run_id, {k: v for k, v in report.items() if k != "kb_rebuilt"})
        return report

    def mode(self) -> str:
        from .providers import ProviderLayer
        m = ProviderLayer(self.s).role_model("librarian")
        return f"model:{m}" if m else "heuristic"

    def _dedupe(self, run_id, items) -> dict:
        merged, proposed = [], []
        pool = [dict(r) for r in self.s.q("SELECT id, kind, project_id, title, content, created_at FROM items WHERE "
                                          "superseded_by IS NULL AND kind IN ('memory','fact','source') AND COALESCE(tags,'') "
                                          "NOT LIKE '%\"subtask\"%' ORDER BY created_at")]  # task outputs are per-run records
        new_ids = {i["id"] for i in items}
        pos = {p["id"]: n for n, p in enumerate(pool)}
        terms, vecs = {}, {}

        def t(p):  # computed on demand: a run touches new items x pool, not every pair (4M pairs at 3k items)
            if p["id"] not in terms:
                terms[p["id"]] = set(_terms(p["content"]))
            return terms[p["id"]]

        def v(p):
            if p["id"] not in vecs:
                vecs[p["id"]] = self._emb.embed([f"{p['title']} {p['content']}"[:2000]])[0]
            return vecs[p["id"]]
        seen = set()
        pairs = sorted({(min(pos[n["id"]], pos[x["id"]]), max(pos[n["id"]], pos[x["id"]]))
                        for n in pool if n["id"] in new_ids for x in pool
                        if x["id"] != n["id"] and x["kind"] == n["kind"] and x["project_id"] == n["project_id"]})
        for i, j in pairs:  # a is the older item and is kept; b, the newer, is the duplicate
            a, b = pool[i], pool[j]
            if b["id"] not in seen:
                ta, tb = t(a), t(b)
                jac = len(ta & tb) / max(1, len(ta | tb))
                if jac >= 0.8 and cosine(v(a), v(b)) >= DUP_THRESHOLD:
                    seen.add(b["id"])
                    if self.auto_approve():
                        self._merge(run_id, a["id"], b["id"])
                        merged.append(b["id"])
                    else:
                        p = self._propose("merge", b["id"], {"keep": a["id"], "dup": b["id"]},
                                          f"near-duplicate of {a['id']} (cosine ≥ {DUP_THRESHOLD})")
                        if p:
                            proposed.append(p)
        return {"merged": merged, "proposed": proposed}

    def _autotag(self, run_id, items) -> int:
        df = Counter(t for i in items for t in set(_terms(i["content"])))
        n = 0
        for i in items:
            tags = json.loads(i["tags"] or "[]")
            if tags:
                continue
            terms = Counter(t for t in _terms(f"{i['title']} {i['content']}") if len(t) > 3 and t not in STOP)
            new = [t for t, _ in sorted(terms.items(), key=lambda kv: (-kv[1], df[kv[0]]))[:3]]
            if not new:
                continue
            self.s.x("UPDATE items SET tags=? WHERE id=?", (json.dumps(new), i["id"]))
            self.s.sync_fts(i["id"])
            self._log(run_id, "tag", i["id"], {"tags": tags}, {"tags": new})
            n += 1
        return n

    def _assign(self, run_id, items) -> int:
        projects = [dict(r) for r in self.s.q("SELECT id, name FROM projects")]
        n = 0
        for i in items:
            if i["project_id"] or i["kind"] in ("checkpoint",):
                continue
            text = f"{i['title']} {i['content']}".lower()
            hits = [p for p in projects if re.search(rf"\b{re.escape(p['name'].lower())}\b", text)]
            if not hits:  # via linked entities belonging to a project
                ent = self.s.q("SELECT DISTINCT e.project_id FROM relations r JOIN entities e ON e.id=r.dst WHERE r.src=? "
                               "AND e.project_id IS NOT NULL", (i["id"],))
                hits = [p for p in projects if p["id"] in {e["project_id"] for e in ent}]
            if len(hits) == 1:
                pid = hits[0]["id"]
                self.s.x("UPDATE items SET project_id=? WHERE id=?", (pid, i["id"]))
                self.s.relate(i["id"], pid, "in_project", agent_id=self.agent_id)
                self._log(run_id, "assign_project", i["id"], {"project_id": None}, {"project_id": pid})
                self.s.kb_dirty(pid)
                n += 1
        return n

    def _flags(self, run_id) -> list[str]:
        out = []
        old = now() - STALE_DAYS * 86400
        for r in self.s.q("SELECT id FROM items WHERE kind IN ('fact','memory') AND superseded_by IS NULL AND "
                          "updated_at<? LIMIT 50", (old,)):
            p = self._propose("flag_stale", r["id"], {}, f"not updated in {int(STALE_DAYS)} days; verify or archive")
            if p:
                out.append(p)
        for c in self.s.list_conflicts():
            p = self._propose("flag_conflict", c["b"], {"conflict": c["id"], "a": c["a"]},
                              f"contradicts {c['a']} ({c['subject']}.{c['predicate']}); resolve with conflicts()")
            if p:
                out.append(p)
        return out

    def _contradictions(self, run_id, items) -> list[str]:
        """Free-text contradictions (structured facts are caught on write by subject/predicate): each new note vs its 3
        nearest notes in the same project, one TypeSafe yes/no per pair. Hits become open conflicts (pickup and
        conflicts() show them; _flags proposes a review)."""
        from . import typesafe
        if not typesafe.enabled():
            return []
        from concurrent.futures import ThreadPoolExecutor
        thr = float(os.environ.get("COMMONS_CONTRADICTION_THRESHOLD", "0.85"))
        kinds = ("memory", "fact", "decision")
        new = [i for i in items if i["kind"] in kinds and '"subtask"' not in (i.get("tags") or "")]
        q = ("Do note A and note B contradict each other, so that both cannot be true at the same time about the "
             "same thing?")
        found = []
        for it in new[:int(os.environ.get("COMMONS_CONTRADICTION_MAX", "50"))]:
            ids, _, _ = self.s.rank(f"{it.get('title') or ''} {it['content']}"[:500], kinds=list(kinds), limit=6,
                                    project=self.s.project_name(it["project_id"]) if it["project_id"] else None)
            ids = [i for i in ids if i != it["id"]][:3]
            if not ids:
                continue
            rows = {r["id"]: r for r in self.s.q(f"SELECT id, content, created_at FROM items WHERE id IN ({','.join('?' * len(ids))}) "
                                                 "AND superseded_by IS NULL", ids)}
            near = [rows[i] for i in ids if i in rows and not self.s.q(
                "SELECT 1 FROM relations WHERE type='contradicts' AND ((src=? AND dst=?) OR (src=? AND dst=?))",
                (it["id"], i, i, it["id"]))]

            def ask(r, it=it):
                try:
                    return typesafe.noul(f"Note A: {r['content'][:2000]}\nNote B: {it['content'][:2000]}", {"c": q}).get("c", 0)
                except Exception:  # noqa: BLE001 - a TypeSafe outage only skips this pass
                    return 0.0
            with ThreadPoolExecutor(3) as ex:
                for r, p in zip(near, ex.map(ask, near)):
                    if p >= thr:  # as for facts: a = the older note, b = the newer one that contradicts it
                        old, new_ = (r, it) if r["created_at"] <= it["created_at"] else (it, r)
                        self.s.x("INSERT INTO conflicts VALUES(?,?,?,?,?,?,?)",
                                 (new_id("cfl"), old["id"], new_["id"], "text", "contradicts", "open", now()))
                        self.s.x("UPDATE items SET conflict=1 WHERE id IN (?,?)", (old["id"], new_["id"]))
                        self.s.relate(new_["id"], old["id"], "contradicts", agent_id=self.agent_id)
                        self._log(run_id, "contradiction", new_["id"], None, {"with": old["id"], "p": round(p, 3)}, False)
                        found.append(new_["id"])
        return found

    def _skills(self, run_id) -> int:
        n = 0
        for sk in self.s.q("SELECT * FROM skills WHERE description IS NULL OR description=''"):
            v = self.s.q("SELECT content FROM skill_versions WHERE skill_id=? ORDER BY version DESC LIMIT 1", (sk["id"],))
            if v:
                body = re.sub(r"^---.*?---", "", v[0]["content"], flags=re.S).strip()
                desc = " ".join(body.split())[:160]
                self.s.x("UPDATE skills SET description=? WHERE id=?", (desc, sk["id"]))
                self._log(run_id, "skill_description", sk["id"], {"description": sk["description"]}, {"description": desc})
                n += 1
        for v in self.s.q("SELECT sv.id, s.name, sv.version FROM skill_versions sv JOIN skills s ON s.id=sv.skill_id "
                          "WHERE sv.status IN ('pending','flagged') AND sv.created_at<?", (now() - 7 * 86400,)):
            self._propose("skill_review", v["id"], {"skill": v["name"], "version": v["version"]}, "pending review > 7 days")
        return n

    def _summaries(self, run_id) -> int:
        from .providers import ProviderLayer
        from .tree import Tree
        pl = ProviderLayer(self.s)
        spent0 = self._spent()
        t = Tree(self.s)
        n = 0
        for nd in t.outline("workspace", depth=2, max_tokens=10 ** 6, max_children=200)["nodes"]:
            kids = t.children(nd["key"])
            if not kids:
                continue
            kinds = Counter(k["kind"] for k in kids)
            heur = f"{nd['label']}: " + ", ".join(f"{v} {k}" for k, v in kinds.most_common(4)) + \
                   "; e.g. " + "; ".join(k["label"][:40] for k in kids[:3])
            text, source = heur, "heuristic"
            if self._spent() - spent0 < self.budget():
                llm = pl.call_role("librarian", "Write one sentence (<=30 words) describing this folder of a shared "
                                   f"agent workspace so agents know what is inside.\nNODE: {nd['label']}\nCHILDREN: " +
                                   "; ".join(k["label"][:60] for k in kids[:15]), max_tokens=80)
                if llm:
                    text, source = llm.strip()[:300], "llm"
            prev = self.s.q("SELECT * FROM node_summaries WHERE key=?", (nd["key"],))
            if prev and prev[0]["summary"] == text:
                continue
            self.s.x("INSERT OR REPLACE INTO node_summaries VALUES(?,?,?,?,?)", (nd["key"], nd["path"], text, source, now()))
            self._log(run_id, "summary", nd["key"], {"summary": prev[0]["summary"] if prev else None,
                                                     "path": nd["path"], "source": prev[0]["source"] if prev else None},
                      {"summary": text})
            n += 1
        return n

    def _spent(self) -> float:
        return self.s.q("SELECT COALESCE(SUM(cost),0) c FROM usage WHERE note='llm:librarian'")[0]["c"]

    # ------------------------------------------------------------------ index & find
    def build_index(self, depth: int = 4) -> int:
        from .tree import Tree
        t = Tree(self.s)
        rows = []
        for root in ("workspace", "agents", "skills"):
            for nd in t.outline(root, depth=depth, max_tokens=10 ** 7, max_children=500)["nodes"]:
                text = nd["label"]
                if nd.get("id"):
                    it = self.s.q("SELECT title, summary, tags FROM items WHERE id=?", (nd["id"],))
                    if it:
                        text += f" {it[0]['summary']} {' '.join(json.loads(it[0]['tags'] or '[]'))}"
                summ = self.s.q("SELECT summary FROM node_summaries WHERE key=?", (nd["key"],))
                if summ:
                    text += " " + summ[0]["summary"]
                w = 1.0 + (0.5 if nd["kind"] in ("project", "group", "workflow", "skill", "kb_section", "dir", "job") else 0)
                for term in set(_terms(text)):
                    rows.append((term, nd["path"], nd["key"], nd.get("id"), nd["label"][:120], nd["kind"], w))
        with self.s.lock:
            self.s.db.execute("BEGIN")
            self.s.db.execute("DELETE FROM librarian_index")
            self.s.db.executemany("INSERT INTO librarian_index VALUES(?,?,?,?,?,?,?)", rows)
            self.s.db.execute("COMMIT")
        return len(rows)

    def find(self, query: str, limit: int = 5, max_tokens: int = 300) -> dict:
        """Best few places to look for X: tree paths + ids + why, from the librarian's index (cheap, no LLM)."""
        if not self.s.q("SELECT 1 FROM librarian_index LIMIT 1"):
            self.build_index()
        terms = _terms(query) or query.lower().split()
        scores: dict[str, float] = {}
        meta: dict[str, dict] = {}
        for t in terms:
            for r in self.s.q("SELECT * FROM librarian_index WHERE term=? OR term LIKE ?", (t, t + "%")):
                k = r["path"]
                scores[k] = scores.get(k, 0) + r["weight"] * (1.0 if r["term"] == t else 0.6)
                meta[k] = dict(r)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0].count(".")))
        lines, out, used = [], [], 0
        for path, sc in ranked:
            m = meta[path]
            if any(path.startswith(o["path"] + ".") for o in out) or (m["rid"] and m["rid"] in {o["rid"] for o in out}):
                continue  # one entry per area / per record
            summ = self.s.q("SELECT summary FROM node_summaries WHERE key=?", (m["key"],))
            ln = f"{path} [{m['kind']}] {m['label']}" + (f" <{m['rid']}>" if m["rid"] else "") + \
                 (f" — {summ[0]['summary'][:100]}" if summ else "")
            if used + estimate_tokens(ln) > max_tokens and lines:
                break
            used += estimate_tokens(ln)
            lines.append(ln)
            out.append({"path": path, "score": round(sc, 2), **{k: m[k] for k in ("key", "rid", "label", "kind")}})
            if len(out) >= limit:
                break
        return {"query": query, "locations": out, "text": "\n".join(lines) or "(nothing indexed matches)",
                "hint": "tree(root='<path>') to browse there; expand(<id>) to open"}

    def suggested_queries(self, project: str | None = None, n: int = 8) -> list[str]:
        pid = self.s.project_id(project) if project else None
        rows = self.s.q("SELECT tags FROM items WHERE project_id IS ? AND superseded_by IS NULL", (pid,))
        c = Counter(t for r in rows for t in json.loads(r["tags"] or "[]") if t not in ("subtask", "research", "source"))
        return [t for t, _ in c.most_common(n)]

    def status(self) -> dict:
        last = self.s.q("SELECT value FROM meta WHERE key='librarian_last_run'")
        return {"mode": self.mode(), "auto_approve": self.auto_approve(), "budget": self.budget(),
                "last_run": json.loads(last[0]["value"]) if last else None,
                "pending_proposals": len(self.proposals()),
                "recent": [dict(r) | {"ts": iso(r["ts"])} for r in
                           self.s.q("SELECT * FROM librarian_log ORDER BY ts DESC LIMIT 30")],
                "new_since_last_run": self.s.q("SELECT COUNT(*) c FROM items WHERE created_at>?", (self._watermark(),))[0]["c"]}

    # ------------------------------------------------------------------ delegate to an agent
    def delegate(self) -> dict:
        """Mode (b): post a `librarian` subtask any connected agent can claim; its reply lines are applied:
        TAG <id> a,b | PROJECT <id> <project> | SUMMARY <tree-key> <text> | MERGE <keep> <dup> (→ review queue)."""
        from .orchestrator import Orchestrator
        untagged = self.s.q("SELECT id, title FROM items WHERE (tags IS NULL OR tags='[]') AND superseded_by IS NULL LIMIT 40")
        unassigned = self.s.q("SELECT id, title FROM items WHERE project_id IS NULL AND superseded_by IS NULL LIMIT 40")
        body = ("You are the Commons librarian. Organize these records. Reply with one action per line:\n"
                "TAG <id> tag1,tag2 | PROJECT <id> <project-name> | MERGE <keep-id> <dup-id> | SUMMARY <tree-key> <text>\n\n"
                "Projects: " + ", ".join(r["name"] for r in self.s.q("SELECT name FROM projects")) +
                "\nUntagged:\n" + "\n".join(f"{r['id']} {r['title'][:80]}" for r in untagged) +
                "\nUnassigned:\n" + "\n".join(f"{r['id']} {r['title'][:80]}" for r in unassigned))
        return Orchestrator(self.s).submit(body, dag=[{"key": "librarian", "type": "librarian", "title": "Librarian pass",
                                                       "body": body, "deps": []}], title="Librarian pass")

    def apply_actions(self, text: str, actor: str | None = None) -> dict:
        run_id = new_id("run")
        done = {"tag": 0, "project": 0, "summary": 0, "merge_proposed": 0, "merged": 0}
        for line in (text or "").splitlines():
            parts = line.strip().split(maxsplit=2)
            if len(parts) < 3:
                continue
            op, a, b = parts[0].upper(), parts[1], parts[2]
            it = self.s.q("SELECT * FROM items WHERE id=?", (a,))
            if op == "TAG" and it:
                new = [t.strip().lower() for t in b.split(",") if t.strip()][:6]
                self._log(run_id, "tag", a, {"tags": json.loads(it[0]["tags"] or "[]")}, {"tags": new})
                self.s.x("UPDATE items SET tags=? WHERE id=?", (json.dumps(new), a))
                self.s.sync_fts(a)
                done["tag"] += 1
            elif op == "PROJECT" and it and self.s.q("SELECT 1 FROM projects WHERE name=?", (b.strip(),)):
                pid = self.s.project_id(b.strip())
                self._log(run_id, "assign_project", a, {"project_id": it[0]["project_id"]}, {"project_id": pid})
                self.s.x("UPDATE items SET project_id=? WHERE id=?", (pid, a))
                self.s.relate(a, pid, "in_project", agent_id=actor)
                done["project"] += 1
            elif op == "MERGE" and it and self.s.q("SELECT 1 FROM items WHERE id=?", (b.split()[0],)):
                if self.auto_approve():
                    self._merge(run_id, a, b.split()[0])
                    done["merged"] += 1
                elif self._propose("merge", b.split()[0], {"keep": a, "dup": b.split()[0]}, f"proposed by {actor}"):
                    done["merge_proposed"] += 1
            elif op == "SUMMARY":
                prev = self.s.q("SELECT * FROM node_summaries WHERE key=?", (a,))
                self.s.x("INSERT OR REPLACE INTO node_summaries VALUES(?,?,?,?,?)", (a, None, b[:300], f"agent:{actor}", now()))
                self._log(run_id, "summary", a, {"summary": prev[0]["summary"] if prev else None}, {"summary": b[:300]})
                done["summary"] += 1
        return done
