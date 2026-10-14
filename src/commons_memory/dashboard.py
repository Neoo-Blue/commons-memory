"""Local web dashboard (stdlib only): `commons dashboard` → http://127.0.0.1:8766

Localhost-only by default. Binding to a non-loopback host requires a token (COMMONS_DASHBOARD_TOKEN / --token).
Mutations require POST + the `X-Commons: 1` header (blocks cross-site form CSRF) and the token when set.
Secrets: only references, leases and audit events are ever shown — never values.
"""
from __future__ import annotations

import gzip
import ipaddress
import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .graphexport import to_html
from .ids import iso, now
from .orchestrator import Orchestrator
from .secrets import Broker
from .store import CommonsError, Store
from .usage import Usage


class API:
    def __init__(self, store: Store):
        self.s, self.o, self.u, self.b = store, Orchestrator(store), Usage(store), Broker(store)

    def overview(self, q):
        from . import board as B
        st = self.s.stats()
        return st | {"paused": self.o.paused(), "spent_24h": round(self.u.spent_today(), 4),
                     "open_leftovers": len(self.o.leftovers()), "needs_user": B.needs_user(self.s),
                     "running_jobs": self.s.q("SELECT COUNT(*) c FROM jobs WHERE status='running'")[0]["c"]}

    def agents(self, q):
        out = []
        for a in self.s.q("SELECT * FROM agents ORDER BY last_seen DESC"):
            claims = [dict(r) | {"lease_expires": iso(r["lease_expires"]),
                                 "lease_left_s": round(r["lease_expires"] - now()) if r["lease_expires"] else None}
                      for r in self.s.q("SELECT id, job_id, type, title, lease_expires, started_at FROM subtasks "
                                        "WHERE claimed_by=? AND status='claimed'", (a["id"],))]
            last = self.s.q("SELECT kind, summary, created_at FROM items WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
                            (a["id"],))
            prof = json.loads(a["profile"] or "{}")
            out.append({"id": a["id"], "name": a["name"], "platform": a["platform"], "vendor": a["vendor"],
                        "model": a["model"], "last_seen": iso(a["last_seen"]) if a["last_seen"] else None,
                        "seen": bool(a["last_seen"]), "availability": prof.get("availability"),
                        "expertise": prof.get("expertise", {}), "claims": claims,
                        "last_activity": (dict(last[0]) | {"created_at": iso(last[0]["created_at"])}) if last else None,
                        "learned": self.o.profiles.get(a["id"])["learned"]})
        return out

    def sessions(self, q):
        rows = self.s.q("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 100")
        return [{"id": r["id"], "goal": r["goal"], "status": r["status"], "agent": self.s.label(r["agent_id"])[1]
                 if r["agent_id"] else None, "project": self.s.project_name(r["project_id"]),
                 "updated_at": iso(r["updated_at"]), "unfinished": r["status"] != "done",
                 "leftovers": [dict(x) for x in self.s.q("SELECT id,text,status FROM leftovers WHERE source_id=?", (r["id"],))]}
                for r in rows]

    def leftovers(self, q):
        return self.o.leftovers(q.get("project"), q.get("status", "open"))

    def jobs(self, q):
        return self.o.jobs(q.get("status") or None, 50)

    def job(self, q):
        st = self.o.status(q["id"])
        st["subtasks"] = [dict(r) | {"agent": self.s.label(r["claimed_by"] or r["assigned_agent"])[1]
                                     if (r["claimed_by"] or r["assigned_agent"]) else None,
                                     "deps": json.loads(r["deps"]), "gates": json.loads(r["gates"])}
                          for r in self.s.q("SELECT id,key,type,title,status,verdict,attempts,result,deps,gates,gate_of,"
                                            "claimed_by,assigned_agent FROM subtasks WHERE job_id=? ORDER BY created_at",
                                            (q["id"],))]
        return st

    def job_detail(self, q):
        """Everything a job drawer shows in one call: the job, its steps with their latest routing, the workspace
        folder, and for a six-stage workflow the Workflow page's view."""
        from .workspace import folder
        r = self.s.q("SELECT * FROM jobs WHERE id=?", (q.get("id") or "",))
        if not r:
            raise CommonsError(f"unknown job {q.get('id')}")
        job = dict(r[0])
        route = {x["subtask_id"]: x for x in self.s.q("SELECT subtask_id, model, level, rationale FROM routing_decisions "
                                                      "WHERE job_id=? ORDER BY created_at", (job["id"],))}  # last wins
        subs = []
        for s in self.s.q("SELECT * FROM subtasks WHERE job_id=? ORDER BY created_at", (job["id"],)):
            who, rd, res = s["claimed_by"] or s["assigned_agent"], route.get(s["id"]), s["result"] or ""
            subs.append({k: s[k] for k in ("id", "key", "type", "title", "status", "attempts", "max_attempts", "verdict",
                                           "gate_of")}
                        | {"agent": self.s.label(who)[1] if who else None, "deps": json.loads(s["deps"] or "[]"),
                           "result": res[:300] + ("…" if len(res) > 300 else ""), "created_at": iso(s["created_at"]),
                           "updated_at": iso(s["updated_at"]),
                           "route": {"model": rd["model"], "effort": rd["level"], "rationale": rd["rationale"]} if rd else None})
        pid = job.pop("project_id")
        out = {"job": job | {"project": self.s.project_name(pid) if pid else None,
                             "submitted_by": self.s.label(job["submitted_by"])[1] if job["submitted_by"] else None,
                             "budget": json.loads(job["budget"] or "{}"), "used": json.loads(job["used"] or "{}"),
                             "created_at": iso(job["created_at"]), "updated_at": iso(job["updated_at"])},
               "subtasks": subs, "folder": folder(job["id"])}
        if job["pattern"] == "sdlc":
            from .sdlc import view
            out["workflow"] = view(self.s, job["id"])
        return out

    # ---- v0.29: projects (one page per product: grouped raw names, timeline, work, changes, people)
    def project_list(self, q):
        from .projects import listing
        return listing(self.s)

    def project_detail(self, q):
        from .projects import detail
        return detail(self.s, q.get("id") or "")

    def project_timeline(self, q):
        from .projects import timeline
        return timeline(self.s, q.get("id") or "", float(q["before"]) if q.get("before") else None,
                        max(1, min(int(q.get("limit") or 60), 200)), q.get("types") or "highlights", q.get("agent") or None)

    def project_change(self, q):
        """One recorded change (commit, files, diff) by its folder path, for the change drawer."""
        from .workspace import recent
        got = recent(self.s, limit=1, diff_chars=60_000, where=("path=?", [(q.get("path") or "").rstrip("/") + "/changes.json"]))
        if not got:
            raise CommonsError("unknown change")
        return got[0]

    def agent_detail(self, q):
        """What an agent drawer shows: presence, profile, models, usage windows, its last 10 steps, CLI version and
        unread mail."""
        from . import quota as Q
        from .agentcli import listing
        from .messaging import Messaging, presence
        a = self.s.get_agent(q.get("name") or "")
        if not a:
            raise CommonsError(f"unknown agent {q.get('name')}")
        prof = json.loads(a["profile"] or "{}")
        ok, until, win = Q.can_take(self.s, a["id"])
        recent = self.s.q("SELECT t.id, t.job_id, t.title, t.status, t.updated_at, j.title job FROM subtasks t JOIN jobs j "
                          "ON j.id=t.job_id WHERE t.claimed_by=? OR t.assigned_agent=? ORDER BY t.updated_at DESC LIMIT 10",
                          (a["id"], a["id"]))
        return {"name": a["name"], "id": a["id"], "platform": a["platform"], "vendor": a["vendor"], "model": a["model"],
                "presence": presence(self.s, a["id"]),
                "profile": {k: prof.get(k) for k in ("expertise", "cost", "speed", "max_concurrent")},
                "models": [r["model_id"] for r in self.s.q("SELECT model_id FROM agent_models WHERE agent_id=? "
                                                           "ORDER BY model_id", (a["id"],))],
                "usage": {"can_take_work": ok, "waits_until": iso(until) if until else None, "blocking_window": win,
                          "windows": next((x["windows"] for x in Q.status(self.s) if x["agent"] == a["name"]), [])},
                "recent": [{"subtask_id": r["id"], "job_id": r["job_id"], "job": r["job"], "title": r["title"],
                            "status": r["status"], "updated_at": iso(r["updated_at"])} for r in recent],
                "cli": next((c for c in listing(self.s) if c["agent"] == a["name"]), None),
                "unread": Messaging(self.s).unread(a["id"])}

    def usage(self, q):
        return {"by": q.get("by", "agent"), "rows": self.u.summary(q.get("by", "agent")),
                "daily": self.u.summary("day"), "spent_24h": self.u.spent_today()}

    def search(self, q):
        kinds = [k for k in (q.get("kind") or "").split(",") if k] or None
        conflict = q.get("conflict") in ("1", "true", "yes")
        return self.s.search(q.get("q", ""), project=q.get("project") or None, kinds=kinds,
                             conflict=True if conflict else None, max_tokens=int(q.get("max_tokens", 4000)),
                             limit=int(q.get("limit", 50)))

    def timeline(self, q):
        return self.s.timeline(project=q.get("project") or None, limit=100, max_tokens=4000)

    def item(self, q):
        return self.s.expand(q["id"], depth=1, max_tokens=int(q.get("max_tokens", 8000)))

    def conflicts(self, q):
        out = []
        for c in self.s.list_conflicts():
            out.append(c | {"a_item": self.s.get_item(c["a"]), "b_item": self.s.get_item(c["b"])})
        return out

    def prefs(self, q):
        return self.o.prefs.get(include_all=True, max_tokens=100000)["preferences"]

    def capacity(self, q):
        from . import quota as Q
        from . import schedules as SC
        return {"agents": Q.status(self.s), "schedules": SC.listing(self.s)}

    def board(self, q):
        """Every board item (newest projects first) with project names; the page filters and nests client-side."""
        from . import board as B
        rows = [B.get(self.s, r["id"]) for r in self.s.q("SELECT id FROM board ORDER BY created_at LIMIT 2000")]
        for r in rows:
            if r["kind"] == "question" and r["status"] in B.OPEN:
                r["choices"] = B.choices(r["text"])
        return {"items": rows, "projects": sorted({r["project"] for r in rows if r["project"]})}

    def audit(self, q):
        return {"refs": self.b.list_refs(),
                "leases": [dict(r) | {"expires_at": iso(r["expires_at"]), "created_at": iso(r["created_at"]),
                                      "agent": self.s.label(r["agent_id"])[1] if r["agent_id"] else None,
                                      "active": (not r["revoked"]) and r["expires_at"] > now()}
                           for r in self.s.q("SELECT * FROM leases ORDER BY created_at DESC LIMIT 200")],
                "log": [dict(r) | {"ts": iso(r["ts"])} for r in
                        self.s.q("SELECT * FROM audit ORDER BY id DESC LIMIT 300")]}

    def recurring(self, q):
        return self.o.list_recurring()

    def kb_list(self, q):
        from .kb import KB
        return KB(self.s).list()

    def kb_page(self, q):
        from .kb import KB, SECTIONS, TITLES
        kb = KB(self.s)
        b = q.get("bucket") or "misc"
        stale = kb._ensure(b)
        pages = {r["section"]: {"title": r["title"], "content": r["content"], "citations": json.loads(r["citations"] or "[]"),
                                "updated_at": iso(r["updated_at"])}
                 for r in self.s.q("SELECT * FROM kb_pages WHERE bucket=?", (b,))}
        return {"bucket": b, "stale": stale, "sections": [s for s in SECTIONS if s in pages], "titles": TITLES,
                "pages": pages}

    def backlinks(self, q):
        rid = q["id"]
        kb = [{"bucket": r["bucket"], "section": r["section"]} for r in
              self.s.q("SELECT bucket, section FROM kb_pages WHERE citations LIKE ?", (f'%"{rid}"%',))]
        edges = self.s.neighbors(rid, depth=1, limit=40)
        return {"id": rid, "label": self.s.label(rid), "kb": kb, "edges": edges["edges"], "nodes": edges["nodes"]}

    def graph_scopes(self, q):
        return {"projects": [r["name"] for r in self.s.q("SELECT name FROM projects ORDER BY name")],
                "jobs": [{"id": r["id"], "title": r["title"]} for r in
                         self.s.q("SELECT id, title FROM jobs ORDER BY created_at DESC LIMIT 50")],
                "workflows": [r["name"] for r in self.s.q("SELECT DISTINCT name FROM entities WHERE type='workflow'")],
                "schedules": [{"id": r["id"], "title": r["name"]} for r in self.s.q("SELECT id, name FROM recurring")]}

    def projects(self, q):
        return [dict(r) for r in self.s.q("SELECT id,name FROM projects ORDER BY name")]

    # ---- v0.4: skills, models, org chart, librarian
    def skills(self, q):
        from .server_v4 import seed_skills
        from .skillpool import SkillPool
        seed_skills(self.s)
        sp = SkillPool(self.s)
        out = []
        for r in self.s.q("SELECT * FROM skills ORDER BY name"):
            out.append({"name": r["name"], "description": r["description"], "tags": json.loads(r["tags"] or "[]"),
                        "active_version": r["active_version"], "forked_from": r["forked_from"],
                        "author": self.s.label(r["author_agent"])[1] if r["author_agent"] else None,
                        "stats": sp.stats(r["id"]),
                        "pending": [dict(v) for v in self.s.q("SELECT version, status FROM skill_versions WHERE "
                                                              "skill_id=? AND status IN ('pending','flagged')", (r["id"],))]})
        return out

    def skill_history(self, q):
        from .skillpool import SkillPool
        return SkillPool(self.s).history(q["name"])

    def models_page(self, q):
        from .providers import ProviderLayer
        pl = ProviderLayer(self.s)
        from .agentcli import listing
        from .seats import status as seat_status
        return {"providers": pl.providers(), "models": pl.models(), "roles": pl.roles(), "routing": pl.routing(),
                "proposals": pl.proposals(None)[:50], "clis": listing(self.s), "seats": seat_status(self.s)}

    def tree_children(self, q):
        from .tree import ROOTS, Tree
        t = Tree(self.s)
        ref = q.get("ref", "workspace")
        key, path = t.resolve(ref)
        root = ref.split("/", 1)[0] if "/" in ref else (ref if ref in ROOTS else "workspace")
        pre = "" if root == "workspace" else root + "/"
        out = []
        for i, c in enumerate(t.children(key), 1):
            p = f"{path}.{i}" if path else str(i)
            summ = self.s.q("SELECT summary FROM node_summaries WHERE key=?", (c["key"],))
            out.append(c | {"path": pre + p, "count": c.get("count") if c.get("count") is not None
                            else len(t.children(c["key"])), "summary": summ[0]["summary"] if summ else None})
        return {"ref": ref, "children": out[:300], "more": max(0, len(out) - 300)}

    def tree_find(self, q):
        from .tree import Tree
        root = q.get("root", "workspace")
        hits = Tree(self.s).find(q.get("q", ""), root)
        return [h | {"path": (root + "/" if root != "workspace" else "") + h["path"]} for h in hits]

    def tree_outline(self, q):
        from .tree import Tree
        return Tree(self.s).outline(q.get("ref", "workspace"), int(q.get("depth", 3)), 20000, 200)

    def librarian(self, q):
        from .librarian import Librarian
        lib = Librarian(self.s)
        return lib.status() | {"proposals": lib.proposals(), "suggested": lib.suggested_queries()}

    def find(self, q):
        from .librarian import Librarian
        return Librarian(self.s).find(q.get("q", ""), 8, 800)

    def activity(self, q):
        ev = []
        for r in self.s.q("SELECT kind, summary, agent_id, created_at FROM items ORDER BY created_at DESC LIMIT 25"):
            ev.append({"ts": r["created_at"], "type": r["kind"], "text": r["summary"][:140],
                       "agent": self.s.label(r["agent_id"])[1] if r["agent_id"] else None})
        for r in self.s.q("SELECT ts, action, target FROM audit WHERE action NOT LIKE '%.internal' ORDER BY ts DESC LIMIT 40"):
            if ev and ev[-1]["type"] == r["action"] and ev[-1]["text"] == str(r["target"] or "")[:140]:
                continue  # the same plumbing event repeated (e.g. one per LLM call) is one line
            ev.append({"ts": r["ts"], "type": r["action"], "text": str(r["target"] or "")[:140], "agent": None})
        for r in self.s.q("SELECT ts, server, tool, status, agent_id FROM mcp_calls ORDER BY ts DESC LIMIT 15"):
            ev.append({"ts": r["ts"], "type": "mcp." + r["status"], "text": f"{r['server']}__{r['tool']}",
                       "agent": self.s.label(r["agent_id"])[1] if r["agent_id"] else None})
        ev.sort(key=lambda e: -(e["ts"] or 0))
        return [e | {"ago": round(now() - (e["ts"] or now()))} for e in ev[:40]]

    def usage_series(self, q):
        days = int(q.get("days", 14))
        # UTC calendar days ending today (Postgres rounds CAST(float AS INT), so a rolling window dropped today's usage)
        first = int(now()) // 86400 - days + 1
        rows = [dict(r) | {"d": r["d"] - first} for r in self.s.q(
            "SELECT CAST(ts AS INTEGER)/86400 d, agent_id, SUM(cost) c, SUM(tokens_in+tokens_out) t "
            "FROM usage WHERE ts>=? GROUP BY d, agent_id", (first * 86400,))]
        agents = sorted({r["agent_id"] for r in rows if r["agent_id"]}, key=str)
        series = {self.s.label(a)[1]: [0.0] * days for a in agents}
        token_series = {self.s.label(a)[1]: [0] * days for a in agents}
        tokens = [0] * days
        for r in rows:
            if 0 <= r["d"] < days:
                if r["agent_id"]:
                    series[self.s.label(r["agent_id"])[1]][r["d"]] += r["c"] or 0
                    token_series[self.s.label(r["agent_id"])[1]][r["d"]] += r["t"] or 0
                tokens[r["d"]] += r["t"] or 0
        return {"days": days, "series": series, "tokens": tokens, "token_series": token_series}

    def mcp(self, q):
        from .mcphub import Hub, load_catalog
        h = Hub(self.s)
        cat = load_catalog()
        rows = self.s.q("SELECT server, name, risk, description FROM mcp_tools ORDER BY server, name")
        return {"servers": h.servers(), "tools": [dict(r) | {"description": (r["description"] or "")[:120]} for r in rows],
                "policies": h.policies(), "approvals": h.approvals(), "calls": h.calls(60),
                "catalog": [{"id": k, **{x: v.get(x) for x in ("title", "transport", "auth", "verified", "docs", "notes")},
                             "params": list((v.get("params") or {}).keys()),
                             "secrets": [z["name"] for z in v.get("secrets", []) if not z.get("optional")]}
                            for k, v in sorted(cat["presets"].items())], "catalog_version": cat["catalog_version"]}

    def team(self, q):
        from .status import team
        return team(self.s)

    def setup(self, q):
        from .onboarding import status
        return status(self.s)

    def discover(self, q):
        from .skilldiscovery import Discovery
        d = Discovery(self.s)
        return {"status": d.status(), "rows": d.list(q.get("mode", "trending"), q.get("q", ""), q.get("kind", ""), 300)}

    def import_scan(self, q):
        from .skillimport import run
        return run(self.s, q.get("dir", "."), [p for p in q.get("paths", "").split(",") if p], dry_run=True)

    def context(self, q):
        from .contextpacks import ContextStore
        cs = ContextStore(self.s)
        out = {"packs": cs.packs(), "savings": cs.savings()}
        if q.get("id"):
            out["diff"] = cs.diff(q["id"])
            out["l1"] = cs.load(q["id"], "L1", 3000)["text"]
        return out

    def palette(self, q):
        from .librarian import Librarian
        term = q.get("q", "")
        out = []
        if term:
            for loc in Librarian(self.s).find(term, 6, 2000)["locations"]:
                out.append({"kind": "place", "label": loc["label"], "sub": f"{loc['path']} · {loc['kind']}", "path": loc["path"],
                            "id": loc.get("rid")})
            for r in self.s.search(term, max_tokens=1200, limit=6)["results"]:
                out.append({"kind": "memory", "label": r["summary"][:90], "sub": r["kind"] + " · " + r["id"], "id": r["id"]})
        return out

    # ---- mutations
    def system(self, q):
        from .auth import ACL
        from .background import status
        from .escalation import notifications
        from .trust import Trust
        esc = [dict(r) for r in self.s.q("SELECT * FROM escalations ORDER BY created_at DESC LIMIT 30")]
        rv = [dict(r) for r in self.s.q("SELECT * FROM routing_validation ORDER BY applied_at DESC LIMIT 20")]
        return {"background": status(self.s), "notifications": notifications(self.s, limit=30),
                "trust": Trust(self.s).table(), "acl": ACL(self.s).list(), "escalations": esc, "routing_validation": rv,
                "dialect": self.s.dialect, "encrypted": bool(os.environ.get("COMMONS_DB_KEY")),
                "network": __import__("commons_memory.expose", fromlist=["state"]).state(self.s)}

    def retro(self, q):
        from .retro import RetroLearner
        rl = RetroLearner(self.s)
        st = rl.status()
        return st | {"queue": rl.queue(q.get("status") or "pending", q.get("kind") or None, q.get("source") or None,
                                       int(q.get("limit", 300)))}

    def workflows(self, q):
        from .sdlc import workflows
        return workflows(self.s)

    def workflow(self, q):
        from .sdlc import view
        return view(self.s, q.get("job") or "")

    def owner(self, q):
        from .approvals import info
        return info(self.s)

    def network(self, q):
        from .expose import health, snippets, state
        st = health(self.s) if q.get("probe") == "1" else state(self.s)
        return st | {"snippets": snippets(st["url"]) if st.get("url") else {}}

    def post(self, path, body):
        if path == "/api/board/add":
            from . import board as B
            return B.add(self.s, body.get("kind") or "ask", body.get("text") or "", project=body.get("project") or None,
                         path=body.get("path") or "", parent=body.get("parent") or None, metric=body.get("metric") or None,
                         goal=body.get("goal") or None, current=body.get("current") or None, source="user")
        if path == "/api/board/update":
            from . import board as B
            return B.update(self.s, body["id"], status=body.get("status") or None, current=body.get("current") or None,
                            evidence=body.get("evidence") or None, goal=body.get("goal") or None)
        if path == "/api/setup/provider":
            from .onboarding import set_provider
            return set_provider(self.s, body.get("kind", ""), body.get("key") or "", body.get("base_url") or "")
        if path == "/api/setup/provider_test":
            from .onboarding import check_provider
            return check_provider(self.s, body.get("kind", ""))
        if path == "/api/setup/provider_rm":
            from .onboarding import remove_provider
            return remove_provider(self.s, body.get("kind", ""))
        if path == "/api/setup/phone_test":
            from .phone import push
            return {"sent": push(self.s, "Commons test", "Phone approvals are set up: Needs-you questions will arrive "
                                                         "here with Yes / No buttons.")}
        if path == "/api/setup/setting":
            from . import settings
            return settings.put(self.s, body.get("key", ""), body.get("value"))
        if path == "/api/vault/set":
            from . import vault as V
            return V.add(self.s, body.get("name", ""), body.get("value") or "", body.get("service") or body.get("name", ""),
                         body.get("base_urls") or [], V.parse_auth(body.get("auth") or "bearer"),
                         body.get("description") or "", body.get("risk") or "normal")
        if path == "/api/vault/rm":
            from . import vault as V
            return V.remove(self.s, body.get("name", ""))
        if path == "/api/agent/update":
            from .agentcli import request
            return request(self.s, body.get("agent", ""))
        if path == "/api/board/answer":
            from . import board as B
            return B.answer(self.s, body["id"], body.get("text") or "", by="user (dashboard)")
        if path == "/api/retro/run":
            from .retro import RetroLearner
            r = RetroLearner(self.s).run(body.get("sources") or ["all"], body.get("paths") or {},
                                         dry_run=bool(body.get("dry_run", True)), auto_approve=body.get("auto_approve") or None,
                                         budget=body.get("budget"), use_llm=None if body.get("use_llm", True) else False,
                                         project=body.get("project") or None)
            return r
        if path == "/api/retro/decide":
            from .retro import RetroLearner
            ids = body.get("ids") or []
            return RetroLearner(self.s).decide("all" if ids == "all" else ids, bool(body.get("approve")))
        if path == "/api/notifications/read":
            self.s.x("UPDATE notifications SET read=1" + (" WHERE id=?" if body.get("id") else ""),
                     (body["id"],) if body.get("id") else ())
            return {"ok": True}
        if path == "/api/trust/set":
            from .trust import Trust
            return Trust(self.s).set_manual(body["agent"], None if body.get("score") in (None, "") else float(body["score"]))
        if path == "/api/maintenance/run":
            from .background import tick
            return tick(self.s, (body.get("job"),) if body.get("job") else None, force=True)
        if path == "/api/discover/add":
            from .skilldiscovery import Discovery
            return Discovery(self.s).add(body["id"], body.get("name") or None, self.s.ensure_agent("dashboard-user"),
                                         bool(body.get("allow_license")))
        if path == "/api/discover/run":
            from .skilldiscovery import Discovery
            d = Discovery(self.s)
            return d.discover() | {"updates": d.check_updates()}
        if path == "/api/import/run":
            from .skillimport import run
            return run(self.s, body.get("dir") or ".", [p for p in (body.get("paths") or "").split(",") if p],
                       False, bool(body.get("replace_local")), self.s.ensure_agent("dashboard-user"))
        if path.startswith("/api/mcp/"):
            from .mcphub import Hub
            h = Hub(self.s)
            if path == "/api/mcp/approve":
                return h.decide(body["id"], bool(body["approve"]))
            if path == "/api/mcp/health":
                return h.health(body.get("name") or None)
            if path == "/api/mcp/add_preset":
                return h.add_preset(body["preset"], body.get("name") or None, body.get("params") or {},
                                    body.get("refs") or {}, discover=True)
            if path == "/api/mcp/policy":
                return h.set_policy(body["effect"], body.get("server") or "*", body.get("tool") or "*",
                                    body.get("agent") or "*")
            if path == "/api/mcp/remove":
                return h.remove(body["name"])
        if path.startswith("/api/skill/"):
            from .skillpool import SkillPool
            sp, who = SkillPool(self.s), self.s.ensure_agent("dashboard-user")
            if path == "/api/skill/review":
                return sp.review(body["name"], int(body["version"]), bool(body["approve"]), who, "via dashboard")
            if path == "/api/skill/rollback":
                return sp.rollback(body["name"], int(body["version"]), who)
        if path == "/api/routing/decide":
            from .providers import ProviderLayer
            return ProviderLayer(self.s).decide(body["id"], bool(body["approve"]))
        if path == "/api/models/refresh":
            from .providers import ProviderLayer
            return ProviderLayer(self.s).watch()
        if path.startswith("/api/librarian/"):
            from .librarian import Librarian
            lib = Librarian(self.s)
            if path == "/api/librarian/run":
                return lib.run(full=bool(body.get("full")))
            if path == "/api/librarian/decide":
                return lib.decide(body["id"], bool(body["approve"]))
            if path == "/api/librarian/undo":
                return lib.undo(body["id"])
            if path == "/api/librarian/config":
                return lib.configure(auto_approve=bool(body["auto_approve"]))
        if path == "/api/prefs/forget":
            return self.o.prefs.forget(body.get("id"), bool(body.get("everything")))
        if path == "/api/prefs/confirm":
            return self.o.prefs.confirm(body["id"], body.get("statement") or None)
        if path == "/api/conflicts/resolve":
            return self.s.resolve_conflict(body["id"], body["keep"])
        if path == "/api/killswitch":
            return self.o.set_paused(bool(body["paused"]), body.get("reason", "dashboard kill switch"))
        if path == "/api/job/cancel":
            return self.o.cancel(body["id"], "cancelled from dashboard")
        if path == "/api/job/retry":
            return self.o.retry_job(body["id"])
        if path == "/api/subtask/retry":
            return self.o.retry_step(body["id"])
        if path == "/api/subtask/reassign":
            return self.o.reassign_step(body["id"], body.get("agent") or "")
        if path == "/api/leftover/resolve":
            return self.o.resolve_leftover(body["id"], "resolved from dashboard")
        if path == "/api/kb/refresh":
            from .kb import KB
            return KB(self.s).build(body.get("bucket") or "misc")
        if path == "/api/graph/rebuild":
            from .graphlinks import rebuild
            return rebuild(self.s)
        if path == "/api/lease/revoke":
            return self.b.revoke(body["id"])
        if path in ("/api/item/update", "/api/item/delete", "/api/items/bulk"):
            return self._items(path, body)
        if path.startswith("/api/project/"):
            from . import projects as P
            if path == "/api/project/group":
                return P.group(self.s, body["into"], list(body.get("ids") or []), body.get("title"))
            if path == "/api/project/ungroup":
                return P.ungroup(self.s, body["id"])
            if path == "/api/project/update":
                return P.update(self.s, body["id"], body.get("title"), body.get("kind"))
            if path == "/api/project/dismiss":
                return P.dismiss(self.s, body["key"])
        raise CommonsError("unknown action")

    def _items(self, path, body):
        who = self.s.ensure_agent("dashboard-user")
        if path == "/api/item/update":
            kw = {}
            if body.get("content") is not None:
                kw["content"] = body["content"]
            if body.get("title") is not None:
                kw["title"] = body["title"]
            if "tags" in body and body.get("tags") is not None:
                raw = body["tags"]
                if isinstance(raw, str):
                    raw = [t.strip() for t in raw.split(",")]
                if not isinstance(raw, list):
                    raise CommonsError("tags must be a list")
                kw["tags"] = raw
            if "project" in body:
                kw["project"] = body.get("project") or ""
            return self.s.update_item(body["id"], agent_id=who, **kw)
        if path == "/api/item/delete":
            return self.s.delete_item(body["id"], agent_id=who)
        if path == "/api/item/tier":
            from .tiers import set_tier
            return set_tier(self.s, body["id"], body["tier"])
        ids = body.get("ids") or []
        if not isinstance(ids, list):
            raise CommonsError("ids must be a list")
        action = body.get("action")
        if action not in ("delete", "tag", "untag", "move"):
            raise CommonsError("unknown bulk action")
        tag = (body.get("tag") or "").strip().lower()
        if action in ("tag", "untag") and not tag:
            raise CommonsError("tag required")
        if action == "move" and "project" not in body:
            raise CommonsError("project required")
        out = []
        for iid in ids:
            try:
                if action == "delete":
                    out.append(self.s.delete_item(iid, agent_id=who))
                elif action == "move":
                    out.append(self.s.update_item(iid, project=body.get("project") or "", agent_id=who))
                else:
                    item = self.s.get_item(iid)
                    if not item:
                        raise CommonsError(f"unknown id {iid}")
                    tags = list(item.get("tags") or [])
                    if action == "tag":
                        if tag not in tags:
                            tags.append(tag)
                    else:
                        tags = [t for t in tags if t != tag]
                    out.append(self.s.update_item(iid, tags=tags, agent_id=who))
            except CommonsError as e:
                out.append({"id": iid, "error": str(e)})
        return {"results": out}


def metrics(api: API) -> str:
    st = api.overview({})
    lines = [f"commons_{k} {v}" for k, v in st.items() if isinstance(v, (int, float))]
    for r in api.u.summary("agent"):
        lab = r["label"].replace('"', "")
        lines += [f'commons_cost_usd{{agent="{lab}"}} {r["cost"]}',
                  f'commons_tokens_total{{agent="{lab}"}} {r["tokens_in"] + r["tokens_out"]}']
    return "\n".join(lines) + "\n"


def make_handler(api: API, token: str | None):
    class H(BaseHTTPRequestHandler):
        # keep-alive: a page's API calls reuse one connection instead of paying a TCP (and TLS) handshake each
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        role = "admin"

        def _authed(self, q) -> bool:
            """Shared token (admin), or a per-user dashboard token (`commons dashboard-user add NAME --role
            viewer|admin`). Viewers can read but not POST."""
            users = dashboard_users(api.s)
            if not token and not users:
                if not _host_allowed(self.headers.get("Host", ""), api.s):
                    self.deny = (f"unrecognised Host {self.headers.get('Host')!r}: open the dashboard by IP or add the "
                                 "name to COMMONS_DASHBOARD_HOSTS (tokenless dashboards refuse other names: DNS rebinding)")
                    return False
                self.role = "admin"
                return True
            cand = self.headers.get("Authorization", "").removeprefix("Bearer ").strip() or q.get("token", "")
            if not cand:
                for part in self.headers.get("Cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "commons_token":
                        cand = v
            if not cand:
                return False
            if token and hmac.compare_digest(cand, token):
                self.role = "admin"
                return True
            h = hashlib.sha256(cand.encode()).hexdigest()
            for u in users:
                if hmac.compare_digest(h, u["hash"]):
                    self.role = u["role"]
                    self.user = u["name"]
                    return True
            return False

        def _ctx(self):
            from .dashboard_v81 import CTX
            CTX.req = {"host": self.headers.get("Host", ""), "bind": str(self.server.server_address[0]),
                       "client": str(self.client_address[0]), "role": self.role,
                       "has_auth": bool(token or dashboard_users(api.s))}

        def _events(self):
            """Server-Sent Events: pushes a 'change' event whenever activity happens (jobs, memories, usage, audit,
            notifications), plus a heartbeat. The page re-renders live views on change instead of polling."""
            self.close_connection = True  # no Content-Length: the stream ends when the socket closes
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            last, beat = None, time.time()
            try:
                for _ in range(int(os.environ.get("COMMONS_SSE_MAX_SECONDS", "3600"))):
                    fp = change_fingerprint(api.s)
                    if fp != last:
                        n = api.s.q("SELECT id,title,level FROM notifications WHERE read=0 ORDER BY ts DESC LIMIT 3")
                        self.wfile.write(f"event: change\ndata: {json.dumps({'fp': fp, 'notifications': [dict(x) for x in n]})}\n\n".encode())
                        self.wfile.flush()
                        last = fp
                    elif time.time() - beat > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        beat = time.time()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else
                                                         json.dumps(body, default=str)).encode()
            gz = len(data) > 1400 and "gzip" in (self.headers.get("Accept-Encoding") or "")
            if gz:  # the page and big JSON lists shrink 4-6x: what a phone or a Tailscale hop waits for
                data = PAGE_GZ if data is PAGE_BYTES else gzip.compress(data, compresslevel=5)
            extra = dict(extra or {})
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":  # health checks ask with HEAD: same status and headers, no body
                self.wfile.write(data)

        def _locked(self) -> bool:
            """The owner chose to put the whole dashboard behind their password, and this browser isn't signed in."""
            from . import settings
            from .approvals import has_pin
            return bool(settings.get(api.s, "dashboard_lock")) and has_pin(api.s) and not self._approver()

        def _approver(self) -> bool:
            from .approvals import session_ok
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "commons_approver" and session_ok(api.s, v):
                    return True
            return False

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path.startswith("/approve/") and _host_allowed(self.headers.get("Host", ""), api.s):
                from .approvals import render
                return self._send(200, render(api.s, u.path.split("/")[2], self._approver()), "text/html; charset=utf-8")
            if self._locked():
                if u.path in ("/", "/index.html"):
                    from .approvals import render
                    return self._send(200, render(api.s, "login", False), "text/html; charset=utf-8")
                return self._send(401, {"error": "the dashboard is locked: sign in at /", "need_login": True})
            if not self._authed(q):
                return self._send(401, {"error": getattr(self, "deny", None) or "token required (?token=...)"})
            secure = "; Secure" if getattr(self.server, "tls", False) else ""
            extra = {"Set-Cookie": f"commons_token={q['token']}; HttpOnly; SameSite=Strict; Path=/{secure}"} if q.get("token") else None
            try:
                if u.path == "/events":
                    return self._events()
                if u.path in ("/", "/index.html"):
                    # same bytes until the hub is upgraded: a browser that has them revalidates and gets a bodiless 304
                    cache = {"ETag": PAGE_ETAG, "Cache-Control": "private, no-cache"} | (extra or {})
                    if PAGE_ETAG in (self.headers.get("If-None-Match") or ""):
                        self.send_response(304)
                        for k, v in cache.items():
                            self.send_header(k, v)
                        self.end_headers()
                        return
                    return self._send(200, PAGE_BYTES, "text/html; charset=utf-8", cache)
                if u.path == "/graph.html":
                    if q.get("scope") and q.get("id"):
                        from .graphlinks import subgraph
                        g = subgraph(api.s, q["scope"], q["id"], int(q.get("depth", 2)))
                    else:
                        g = api.s.export_graph(q.get("project") or None, q.get("items", "1") == "1")
                    from . import graphview
                    if q.get("community"):  # a cluster opened from the overview
                        g = graphview.community(g, q["community"])
                    elif q.get("level") != "all":  # big graphs open as a cluster map (level=all forces everything)
                        g = graphview.overview(g)
                    return self._send(200, graphview.render(g), "text/html; charset=utf-8")
                if u.path == "/drive/raw":
                    from .dashboard_v7 import drive_raw
                    data, mime = drive_raw(api, q)
                    safe = mime if mime.startswith(("image/", "application/pdf", "text/plain")) and "svg" not in mime else "application/octet-stream"
                    return self._send(200, data, safe, {"Content-Disposition": "inline" if safe != "application/octet-stream" else "attachment",
                                                         "X-Content-Type-Options": "nosniff"})
                if u.path == "/metrics":
                    return self._send(200, metrics(api), "text/plain; version=0.0.4")
                self._ctx()
                name = u.path.removeprefix("/api/")
                fn = getattr(api, name, None) if u.path.startswith("/api/") and name.isidentifier() and name != "post" else None
                if not fn:
                    return self._send(404, {"error": "not found"})
                return self._send(200, fn(q))
            except (CommonsError, KeyError) as e:
                return self._send(400, {"error": str(e)})

        def do_POST(self):
            # read the body before any early answer: unread bytes make the socket close with a reset, and the
            # browser sees a network error instead of the 401/403 explaining what to do
            raw = self.rfile.read(min(int(self.headers.get("Content-Length", 0) or 0), 64 << 20))
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/board/quick" and self.headers.get("X-Commons") == "1":  # phone button: the signature is the auth
                try:
                    b = json.loads(raw or b"{}")
                    from .phone import quick_answer
                    return self._send(200, quick_answer(api.s, b.get("id", ""), b.get("answer", ""), b.get("sig", "")))
                except (CommonsError, ValueError) as e:
                    return self._send(403, {"error": str(e)})
            if u.path.startswith("/approve/") and self.headers.get("X-Commons") == "1" \
                    and _host_allowed(self.headers.get("Host", ""), api.s):
                from . import approvals as AP
                try:
                    b = json.loads(raw or b"{}")
                    # ponytail: behind a tunnel every remote browser shares the proxy's address, so they share one
                    # lockout; the owner on the LAN still has their own
                    client = str(self.client_address[0])
                    if u.path == "/approve/logout":
                        return self._send(200, {"signed_out": True}, extra={
                            "Set-Cookie": "commons_approver=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
                    if u.path == "/approve/unlock":
                        out = AP.unlock(api.s, str(b.get("pin") or ""), client)
                    elif b.get("set_pin"):
                        out = AP.set_pin(api.s, str(b["set_pin"]), current=str(b.get("current") or ""),
                                         code=str(b.get("code") or ""), client=client)
                    else:
                        cookie = next((v for k, _, v in (p.strip().partition("=") for p in
                                       self.headers.get("Cookie", "").split(";")) if k == "commons_approver"), "")
                        out = AP.answer(api.s, u.path.split("/")[2], b.get("choice", ""), str(b.get("pin") or ""),
                                        cookie, client, str(b.get("note") or ""))
                    secure = "; Secure" if getattr(self.server, "tls", False) or \
                        self.headers.get("X-Forwarded-Proto") == "https" else ""
                    return self._send(200, out, extra={"Set-Cookie": f"commons_approver={AP.session_token(api.s)}; HttpOnly; "
                                                               f"SameSite=Strict; Path=/; Max-Age={AP.SESSION_S}{secure}"})
                except (CommonsError, ValueError) as e:
                    return self._send(403, {"error": str(e)})
            if self._locked():
                return self._send(401, {"error": "the dashboard is locked: sign in at /", "need_login": True})
            if not self._authed(q):
                return self._send(401, {"error": getattr(self, "deny", None) or "token required"})
            if self.headers.get("X-Commons") != "1":
                return self._send(403, {"error": "missing X-Commons header"})
            if self.role != "admin":
                return self._send(403, {"error": "read-only dashboard user"})
            try:
                body = json.loads(raw or b"{}")
                self._ctx()
                from . import approvals as AP
                if u.path.startswith(OWNER_ONLY) and AP.has_pin(api.s) and not self._approver():
                    return self._send(403, {"error": "this change needs your owner password", "need_pin": True})
                if u.path == "/api/setup/setting" and body.get("key") in ("auto_approve_all", "dashboard_lock") \
                        and body.get("value") and body.get("value") not in ("0", "false", "off") and not AP.has_pin(api.s):
                    return self._send(403, {"error": "set your owner password first (the banner at the top of the "
                                                     "dashboard): this switch must stay one only you can flip",
                                            "approve_url": "/approve/pin"})
                if u.path == "/api/board/answer" and not self._approver():
                    from .approvals import needs_proof
                    from . import board as B
                    if needs_proof(B.get(api.s, body.get("id", "")).get("text", "")):
                        return self._send(403, {"error": "key approvals need you: open /approve/" + body.get("id", ""),
                                                "approve_url": "/approve/" + body.get("id", "")})
                return self._send(200, api.post(u.path, body))
            except (CommonsError, KeyError, ValueError) as e:
                return self._send(400, {"error": str(e)})
    return H


# changes an agent on the LAN could use to approve itself, reach a key, mint a key, or pick who does the work: once a
# PIN exists they need the browser the owner unlocked with it
OWNER_ONLY = ("/api/setup/", "/api/vault/", "/api/mcp/approve", "/api/mcp/policy", "/api/mcp/add_preset",
              "/api/mcp/remove", "/api/trust/set", "/api/connect/key", "/api/routing/policy", "/api/routing/mode",
              "/api/librarian/config", "/api/skill/review", "/api/killswitch", "/api/subtask/", "/api/job/retry")


def _host_allowed(host_hdr: str, s: Store | None = None) -> bool:
    """A tokenless dashboard trusts the network, so it must not answer a page from another site that re-pointed its
    own DNS name at us (DNS rebinding). Rebinding always arrives under the attacker's hostname, so accept IP
    literals, localhost, this machine's names, and COMMONS_DASHBOARD_HOSTS / COMMONS_PUBLIC_URL hosts."""
    import socket
    h = (host_hdr or "").strip().lower()
    h = h[1:h.index("]")] if h.startswith("[") and "]" in h else h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if not h:
        return True  # HTTP/1.0 clients without Host can't be a browser rebinding attack
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    names = {"localhost", socket.gethostname().lower(), socket.gethostname().lower().split(".")[0] + ".local"}
    names |= {x.strip().lower() for x in os.environ.get("COMMONS_DASHBOARD_HOSTS", "").split(",") if x.strip()}
    pub = urlparse(os.environ.get("COMMONS_PUBLIC_URL", "")).hostname
    if pub:
        names.add(pub.lower())
    if s is not None:  # the address the owner set on Setup (a tunnel or proxy hostname) needs no extra env var
        from . import settings
        dash = urlparse(settings.get(s, "dashboard_url") or "").hostname
        if dash:
            names.add(dash.lower())
    return h in names or h.endswith(".localhost") or h.endswith(".ts.net")


def dashboard_users(s: Store) -> list[dict]:
    return [json.loads(r["value"]) for r in s.q("SELECT value FROM meta WHERE key LIKE 'dash_user:%'")]


def add_dashboard_user(s: Store, name: str, role: str = "viewer") -> dict:
    import secrets as _sec
    if role not in ("viewer", "admin"):
        raise CommonsError("role must be viewer|admin")
    tok = "cmd_" + _sec.token_urlsafe(24)
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"dash_user:{name}", json.dumps(
        {"name": name, "role": role, "hash": hashlib.sha256(tok.encode()).hexdigest()})))
    s.audit(None, "dashboard.user.add", name, {"role": role})
    return {"name": name, "role": role, "token": tok, "note": "shown once; only its hash is stored"}


def change_fingerprint(s: Store) -> str:
    parts = []
    for t, c in (("subtasks", "updated_at"), ("items", "created_at"), ("usage", "ts"), ("audit", "ts"),
                 ("notifications", "ts"), ("jobs", "updated_at"), ("agents", "last_seen")):
        try:
            parts.append(str(s.q(f"SELECT MAX({c}) m FROM {t}")[0]["m"]))
        except Exception:  # noqa: BLE001
            parts.append("-")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def self_signed_cert(dir_: str | None = None, host: str = "localhost") -> tuple[str, str]:
    """Create (once) a self-signed cert for the dashboard/server under ~/.commons/tls using `cryptography`."""
    import datetime as dt
    from pathlib import Path
    d = Path(dir_ or Path.home() / ".commons" / "tls")
    d.mkdir(parents=True, exist_ok=True)
    cert, key = d / "cert.pem", d / "key.pem"
    if cert.exists() and key.exists():
        return str(cert), str(key)
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    import ipaddress
    sans = [x509.DNSName(host), x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5))
         .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=825))
         .add_extension(x509.SubjectAlternativeName(sans), critical=False).sign(k, hashes.SHA256()))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    os.chmod(key, 0o600)
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    return str(cert), str(key)


def _private_host(host: str) -> bool:
    """True for loopback, RFC1918/ULA LAN, link-local and Tailscale CGNAT (100.64.0.0/10) addresses.
    Wildcard binds (0.0.0.0/::) and public IPs/hostnames are not private."""
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_unspecified:
        return False
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return ip.is_loopback or ip.is_private or ip.is_link_local


def make_server(store: Store, host: str = "127.0.0.1", port: int = 8766, token: str | None = None,
                tls_cert: str | None = None, tls_key: str | None = None) -> ThreadingHTTPServer:
    token = token or os.environ.get("COMMONS_DASHBOARD_TOKEN") or None
    if not token and not dashboard_users(store) and not _private_host(host):
        raise CommonsError("refusing to bind dashboard to a public/wildcard host without a token "
                           "(private, LAN and tailnet addresses are allowed; set a token to expose publicly)")
    if os.environ.get("COMMONS_DASHBOARD_REQUIRE_TOKEN") in ("1", "true", "yes") and not token and not dashboard_users(store):
        raise CommonsError("COMMONS_DASHBOARD_REQUIRE_TOKEN is set but no dashboard token/users are configured")
    srv = ThreadingHTTPServer((host, port), make_handler(API(store), token))
    srv.daemon_threads = True
    srv.tls = False
    tls_cert = tls_cert or os.environ.get("COMMONS_TLS_CERT")
    tls_key = tls_key or os.environ.get("COMMONS_TLS_KEY")
    if tls_cert:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tls_cert, tls_key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        srv.tls = True
    elif host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: dashboard on a non-loopback host without TLS; use --tls-cert/--tls-key or --tls-self-signed")
    return srv


def serve_dashboard(store: Store, host="127.0.0.1", port=8766, token=None, background=False,
                    tls_cert: str | None = None, tls_key: str | None = None):
    srv = make_server(store, host, port, token, tls_cert, tls_key)
    from .background import start_background
    start_background(store)
    from . import approvals as AP
    if not AP.has_pin(store):  # a fresh start: the first visitor claims the hub for the next 30 minutes
        import sys
        AP.open_claim(store)
        print(f"Open the dashboard within {AP.CLAIM_S // 60} minutes to choose your owner password. Headless? Setup code: "
              f"{AP.setup_code(store, rotate=True)}", file=sys.stderr, flush=True)
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
    print(f"Commons dashboard on {'https' if srv.tls else 'http'}://{host}:{port}/" + ("?token=<token>" if (token or os.environ.get('COMMONS_DASHBOARD_TOKEN')) else ""))
    srv.serve_forever()


# The page is assembled from web/: page.html (the shell), then the stylesheets and scripts in order. No build step:
# every file is plain CSS or JS, served inline in one response.
WEB = Path(__file__).with_name("web")
CSS_FILES = ["legacy.css", "theme.css", "components.css", "shell.css", "pages/home.css", "pages/system.css", "pages/work.css", "pages/agents.css", "pages/knowledge.css", "pages/projects.css"]
JS_FILES = ["legacy.js", "kit.js", "shell.js", "pages/home.js", "pages/system.js", "pages/work.js", "pages/agents.js", "pages/knowledge.js", "pages/projects.js"]


def _page() -> str:
    read = lambda names: "\n".join((WEB / n).read_text() for n in names)  # noqa: E731
    return (WEB / "page.html").read_text().replace("/*@CSS*/", read(CSS_FILES), 1).replace("/*@JS*/", read(JS_FILES), 1)


PAGE = _page()


def _install_v7():
    global PAGE
    from . import dashboard_v7 as v7
    v7.install(API)
    # only the icons of v7 and v25 ship: web/pages/work.js replaced every page their JS defined (Mission control,
    # Changes, Task breakdown, Routing, Presence, Drive, Workflow), so that JS was ~12 KB gzipped of dead weight.
    # v81's JS stays: agents.js's Connect page calls its cx* helpers.
    js = "Object.assign(ICON," + json.dumps(v7.ICONS) + ");\n"
    from . import dashboard_v81 as v81
    v81.install(API)
    js += "\nICON.Connect=" + json.dumps(v81.ICON) + ";\n" + v81.JS
    from . import dashboard_v25 as v25
    js += "\nICON.Workflow=" + json.dumps(v25.ICON) + ";\n"
    PAGE = PAGE.replace("async function render(){", js + "\nasync function render(){", 1)


_install_v7()
# the page is the same bytes for every request: compress it once, and give it a validator so a reload revalidates
PAGE_BYTES = PAGE.encode()
PAGE_GZ = gzip.compress(PAGE_BYTES, 9)
PAGE_ETAG = 'W/"' + hashlib.sha256(PAGE_BYTES).hexdigest()[:20] + '"'
