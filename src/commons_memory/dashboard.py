"""Local web dashboard (stdlib only): `commons dashboard` → http://127.0.0.1:8766

Localhost-only by default. Binding to a non-loopback host requires a token (COMMONS_DASHBOARD_TOKEN / --token).
Mutations require POST + the `X-Commons: 1` header (blocks cross-site form CSRF) and the token when set.
Secrets: only references, leases and audit events are ever shown — never values.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
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
        st = self.s.stats()
        return st | {"paused": self.o.paused(), "spent_24h": round(self.u.spent_today(), 4),
                     "open_leftovers": len(self.o.leftovers()),
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
                        "model": a["model"], "last_seen": iso(a["last_seen"]), "availability": prof.get("availability"),
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

    def usage(self, q):
        return {"by": q.get("by", "agent"), "rows": self.u.summary(q.get("by", "agent")),
                "daily": self.u.summary("day"), "spent_24h": self.u.spent_today()}

    def search(self, q):
        return self.s.search(q.get("q", ""), project=q.get("project") or None, max_tokens=int(q.get("max_tokens", 2000)),
                             limit=50)

    def timeline(self, q):
        return self.s.timeline(project=q.get("project") or None, limit=100, max_tokens=4000)

    def item(self, q):
        return self.s.expand(q["id"], depth=1)

    def conflicts(self, q):
        out = []
        for c in self.s.list_conflicts():
            out.append(c | {"a_item": self.s.get_item(c["a"]), "b_item": self.s.get_item(c["b"])})
        return out

    def prefs(self, q):
        return self.o.prefs.get(include_all=True, max_tokens=100000)["preferences"]

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
        return {"providers": pl.providers(), "models": pl.models(), "roles": pl.roles(), "routing": pl.routing(),
                "proposals": pl.proposals(None)[:50]}

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
        for r in self.s.q("SELECT ts, action, target FROM audit ORDER BY ts DESC LIMIT 25"):
            ev.append({"ts": r["ts"], "type": r["action"], "text": str(r["target"] or "")[:140], "agent": None})
        for r in self.s.q("SELECT ts, server, tool, status, agent_id FROM mcp_calls ORDER BY ts DESC LIMIT 15"):
            ev.append({"ts": r["ts"], "type": "mcp." + r["status"], "text": f"{r['server']}__{r['tool']}",
                       "agent": self.s.label(r["agent_id"])[1] if r["agent_id"] else None})
        ev.sort(key=lambda e: -(e["ts"] or 0))
        return [e | {"ago": round(now() - (e["ts"] or now()))} for e in ev[:40]]

    def usage_series(self, q):
        days = int(q.get("days", 14))
        start = now() - days * 86400
        rows = self.s.q("SELECT CAST((ts-?)/86400 AS INT) d, agent_id, SUM(cost) c, SUM(tokens_in+tokens_out) t "
                        "FROM usage WHERE ts>=? GROUP BY d, agent_id", (start, start))
        agents = sorted({r["agent_id"] for r in rows if r["agent_id"]}, key=str)
        series = {self.s.label(a)[1]: [0.0] * days for a in agents}
        tokens = [0] * days
        for r in rows:
            if 0 <= r["d"] < days:
                if r["agent_id"]:
                    series[self.s.label(r["agent_id"])[1]][r["d"]] += r["c"] or 0
                tokens[r["d"]] += r["t"] or 0
        return {"days": days, "series": series, "tokens": tokens}

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

    def discover(self, q):
        from .skilldiscovery import Discovery
        d = Discovery(self.s)
        return {"status": d.status(), "rows": d.list(q.get("mode", "trending"), q.get("q", ""), q.get("kind", ""), 60)}

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
    def post(self, path, body):
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
        raise CommonsError("unknown action")


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
        def log_message(self, *a):
            pass

        def _authed(self, q) -> bool:
            if not token:
                return True
            cand = self.headers.get("Authorization", "").removeprefix("Bearer ").strip() or q.get("token", "")
            if not cand:
                for part in self.headers.get("Cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "commons_token":
                        cand = v
            return hmac.compare_digest(cand, token)

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else (body if isinstance(body, str) else
                                                         json.dumps(body, default=str)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self._authed(q):
                return self._send(401, {"error": "token required (?token=...)"})
            extra = {"Set-Cookie": f"commons_token={token}; HttpOnly; SameSite=Strict; Path=/"} if token and q.get("token") else None
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, PAGE, "text/html; charset=utf-8", extra)
                if u.path == "/graph.html":
                    if q.get("scope") and q.get("id"):
                        from .graphlinks import subgraph
                        g = subgraph(api.s, q["scope"], q["id"], int(q.get("depth", 2)))
                    else:
                        g = api.s.export_graph(q.get("project") or None, q.get("items", "1") == "1")
                    return self._send(200, to_html(g), "text/html; charset=utf-8")
                if u.path == "/metrics":
                    return self._send(200, metrics(api), "text/plain; version=0.0.4")
                name = u.path.removeprefix("/api/")
                fn = getattr(api, name, None) if u.path.startswith("/api/") and name.isidentifier() and name != "post" else None
                if not fn:
                    return self._send(404, {"error": "not found"})
                return self._send(200, fn(q))
            except (CommonsError, KeyError) as e:
                return self._send(400, {"error": str(e)})

        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self._authed(q):
                return self._send(401, {"error": "token required"})
            if self.headers.get("X-Commons") != "1":
                return self._send(403, {"error": "missing X-Commons header"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                return self._send(200, api.post(u.path, body))
            except (CommonsError, KeyError, ValueError) as e:
                return self._send(400, {"error": str(e)})
    return H


def make_server(store: Store, host: str = "127.0.0.1", port: int = 8766, token: str | None = None) -> ThreadingHTTPServer:
    token = token or os.environ.get("COMMONS_DASHBOARD_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise CommonsError("refusing to bind dashboard to a non-loopback host without a token")
    return ThreadingHTTPServer((host, port), make_handler(API(store), token))


def serve_dashboard(store: Store, host="127.0.0.1", port=8766, token=None, background=False):
    srv = make_server(store, host, port, token)
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
    print(f"Commons dashboard on http://{host}:{port}/" + ("?token=<token>" if (token or os.environ.get('COMMONS_DASHBOARD_TOKEN')) else ""))
    srv.serve_forever()


PAGE = r"""<!doctype html><html lang="en" data-theme="dark"><head><meta charset="utf-8"><title>Commons dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark light">
<style>
:root{--bg:#0b0d13;--bg2:#10131c;--card:rgba(24,28,40,.72);--card2:#161a26;--line:rgba(255,255,255,.07);--fg:#e8eaf2;--mut:#8a93ab;
--acc:#7c9cff;--acc2:#b07cff;--grad:linear-gradient(135deg,#7c9cff,#b07cff 55%,#ff7cc8);--ok:#3ddc97;--bad:#ff5d73;--warn:#ffc35c;
--r:14px;--sh:0 8px 30px rgba(0,0,0,.35);--font:"Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,"JetBrains Mono",Menlo,monospace}
[data-theme=light]{--bg:#f5f6fb;--bg2:#eceff7;--card:rgba(255,255,255,.85);--card2:#fff;--line:rgba(10,20,50,.09);--fg:#141826;--mut:#5d6680;--sh:0 6px 24px rgba(30,40,90,.08)}
*{box-sizing:border-box}html,body{height:100%}body{margin:0;font:14px/1.5 var(--font);background:var(--bg);color:var(--fg);
background-image:radial-gradient(1200px 600px at 85% -10%,rgba(124,156,255,.13),transparent),radial-gradient(900px 500px at -10% 110%,rgba(176,124,255,.10),transparent);background-attachment:fixed}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:6px}
.app{display:grid;grid-template-columns:232px 1fr;min-height:100vh}
aside{position:sticky;top:0;height:100vh;overflow:auto;padding:16px 10px;border-right:1px solid var(--line);background:linear-gradient(180deg,var(--bg2),transparent)}
.logo{display:flex;align-items:center;gap:10px;font-weight:800;font-size:17px;padding:4px 10px 16px;letter-spacing:-.02em}
.logo i{width:30px;height:30px;border-radius:9px;background:var(--grad);display:grid;place-items:center;font-style:normal;color:#fff;box-shadow:0 4px 18px rgba(124,156,255,.45)}
.nav-h{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);padding:12px 12px 4px}
.nav a{display:flex;align-items:center;gap:10px;padding:7px 12px;margin:1px 0;border-radius:10px;color:var(--mut);cursor:pointer;font-weight:500;transition:background .15s,color .15s}
.nav a:hover{background:var(--line);color:var(--fg);text-decoration:none}.nav a.on{background:linear-gradient(90deg,rgba(124,156,255,.18),rgba(176,124,255,.06));color:var(--fg);box-shadow:inset 2px 0 0 var(--acc)}
.nav svg{width:17px;height:17px;flex:none;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.nav kbd{margin-left:auto;font-size:10px;opacity:.5}
header{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;padding:12px 22px;backdrop-filter:blur(14px);background:color-mix(in srgb,var(--bg) 70%,transparent);border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:19px;letter-spacing:-.02em}.spacer{flex:1}
.search{display:flex;align-items:center;gap:8px;min-width:260px;padding:7px 12px;border-radius:10px;border:1px solid var(--line);background:var(--card2);color:var(--mut);cursor:pointer}
kbd{font:11px var(--mono);padding:1px 6px;border-radius:5px;border:1px solid var(--line);background:var(--bg2);color:var(--mut)}
main{padding:20px 22px 60px;max-width:1440px;width:100%;margin:0 auto;animation:fade .25s ease}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.card{background:var(--card);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:var(--r);padding:16px;margin-bottom:16px;box-shadow:var(--sh)}
.card h3{margin:0 0 10px;font-size:14.5px;letter-spacing:-.01em}.card h2{letter-spacing:-.02em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:14px;margin-bottom:16px}.grid .card{margin:0}
.stat .mut{font-size:12px;text-transform:capitalize}.big{font-size:27px;font-weight:750;letter-spacing:-.03em}
.stat.hero{background:var(--grad);color:#fff;border:0}.stat.hero .mut{color:rgba(255,255,255,.85)}
.cols{display:grid;grid-template-columns:1.4fr 1fr;gap:16px}.cols>.card{margin:0}
table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
tr:last-child td{border-bottom:0}th{color:var(--mut);font-weight:600;font-size:12px}tbody tr,table tr{transition:background .12s}table tr:hover td{background:rgba(124,156,255,.04)}
.mut{color:var(--mut)}code{font:12px var(--mono);background:var(--bg2);padding:1px 5px;border-radius:5px}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;background:rgba(124,156,255,.12);color:var(--acc);font-size:11.5px;font-weight:600;white-space:nowrap}
.pill.done,.pill.approve,.pill.active,.pill.up,.pill.ok,.pill.read,.pill.applied,.pill.approved{background:rgba(61,220,151,.13);color:var(--ok)}
.pill.failed,.pill.reject,.pill.cancelled,.pill.down,.pill.destructive,.pill.denied,.pill.flagged,.pill.error,.pill.rejected{background:rgba(255,93,115,.13);color:var(--bad)}
.pill.claimed,.pill.awaiting_gate,.pill.running,.pill.pending,.pill.write,.pill.proposed,.pill.approval_required,.pill.new{background:rgba(255,195,92,.14);color:var(--warn)}
.done,.approve,.active{color:var(--ok)}.failed,.reject,.cancelled{color:var(--bad)}.claimed,.awaiting_gate,.running{color:var(--warn)}
button{font:inherit;font-size:12.5px;font-weight:600;background:var(--card2);color:var(--fg);border:1px solid var(--line);border-radius:9px;padding:5px 11px;cursor:pointer;transition:transform .08s,border-color .15s,background .15s}
button:hover{border-color:var(--acc)}button:active{transform:scale(.97)}button.pri{background:var(--grad);color:#fff;border:0}button.bad{color:var(--bad);border-color:rgba(255,93,115,.35)}
input,select{font:inherit;background:var(--card2);color:var(--fg);border:1px solid var(--line);border-radius:9px;padding:6px 10px}
.bar{height:10px;border-radius:99px;background:var(--grad)}pre{white-space:pre-wrap;font:12px/1.5 var(--mono);background:var(--bg2);padding:10px;border-radius:10px;max-height:320px;overflow:auto;border:1px solid var(--line)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--mut);margin-right:6px;vertical-align:middle}
.dot.live{background:var(--ok);box-shadow:0 0 0 0 rgba(61,220,151,.6);animation:pulse 1.8s infinite}.dot.busy{background:var(--warn);animation:pulse 1.2s infinite}.dot.idle{background:#4b5470}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(61,220,151,.55)}70%{box-shadow:0 0 0 9px rgba(61,220,151,0)}100%{box-shadow:0 0 0 0 rgba(61,220,151,0)}}
.feed{list-style:none;margin:0;padding:0;max-height:430px;overflow:auto}.feed li{display:flex;gap:10px;padding:8px 2px;border-bottom:1px solid var(--line);animation:slide .35s ease both}
.feed li .t{min-width:46px;color:var(--mut);font-size:11px;text-align:right}@keyframes slide{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:none}}
.empty{text-align:center;padding:34px 10px;color:var(--mut)}.empty b{display:block;color:var(--fg);font-size:15px;margin:8px 0 4px}.empty .ico{font-size:30px;opacity:.8}
#toasts{position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;gap:8px;z-index:50}
.toast{min-width:240px;max-width:380px;padding:11px 14px;border-radius:12px;background:var(--card2);border:1px solid var(--line);box-shadow:var(--sh);animation:slide .25s ease;font-size:13px}
.toast.ok{border-left:3px solid var(--ok)}.toast.err{border-left:3px solid var(--bad)}
#pal{position:fixed;inset:0;background:rgba(5,7,12,.55);backdrop-filter:blur(4px);display:none;z-index:40;align-items:flex-start;justify-content:center;padding-top:12vh}
#pal.on{display:flex}#pal .box{width:min(640px,92vw);background:var(--card2);border:1px solid var(--line);border-radius:16px;box-shadow:0 30px 80px rgba(0,0,0,.5);overflow:hidden;animation:fade .15s}
#pal input{width:100%;border:0;border-bottom:1px solid var(--line);border-radius:0;padding:16px 18px;font-size:16px;background:transparent}#pal input:focus{outline:none}
#pal .res{max-height:52vh;overflow:auto;padding:6px}#pal .it{display:flex;gap:10px;align-items:center;padding:9px 12px;border-radius:10px;cursor:pointer}
#pal .it.sel,#pal .it:hover{background:rgba(124,156,255,.14)}#pal .it small{color:var(--mut);margin-left:auto;font-size:11px}
.ocwrap{position:relative;overflow:hidden;height:70vh;border-radius:12px;background:radial-gradient(circle at 1px 1px,var(--line) 1px,transparent 0) 0 0/22px 22px;cursor:grab}
.ocwrap.drag{cursor:grabbing}.ocstage{transform-origin:0 0;position:absolute;left:0;top:0;padding:30px;transition:transform .05s}
.minimap{position:absolute;right:12px;bottom:12px;width:190px;height:120px;border:1px solid var(--line);border-radius:10px;background:var(--card2);overflow:hidden;opacity:.92}
.minimap .mm{transform-origin:0 0;pointer-events:none}.minimap .vp{position:absolute;border:1.5px solid var(--acc);background:rgba(124,156,255,.12);border-radius:3px}
.zoombar{position:absolute;left:12px;top:12px;display:flex;gap:6px;z-index:2}
.orgchart,.orgchart ul{display:flex;justify-content:center;padding-top:20px;position:relative;margin:0;padding-left:0}
.orgchart li{list-style:none;text-align:center;position:relative;padding:20px 5px 0}
.orgchart li::before,.orgchart li::after{content:"";position:absolute;top:0;right:50%;border-top:1.5px solid color-mix(in srgb,var(--acc) 35%,transparent);width:50%;height:20px}
.orgchart li::after{right:auto;left:50%;border-left:1.5px solid color-mix(in srgb,var(--acc) 35%,transparent)}.orgchart li:only-child::before,.orgchart li:only-child::after{display:none}
.orgchart li:only-child{padding-top:0}.orgchart li:first-child::before,.orgchart li:last-child::after{border:0 none}
.orgchart li:last-child::before{border-right:1.5px solid color-mix(in srgb,var(--acc) 35%,transparent);border-radius:0 8px 0 0}.orgchart li:first-child::after{border-radius:8px 0 0 0}
.orgchart ul ul::before{content:"";position:absolute;top:0;left:50%;border-left:1.5px solid color-mix(in srgb,var(--acc) 35%,transparent);height:20px}
.orgchart>li{padding-top:0}.orgchart>li::before,.orgchart>li::after{display:none}
.oc{display:inline-block;background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:8px 11px;min-width:110px;max-width:200px;cursor:pointer;font-size:12px;box-shadow:var(--sh);transition:transform .15s,border-color .15s;position:relative}
.oc:hover{transform:translateY(-2px);border-color:var(--acc)}.oc.sel{border-color:var(--acc);box-shadow:0 0 0 3px rgba(124,156,255,.3)}
.oc.root{background:var(--grad);color:#fff;font-weight:750;border:0}.oc.project{border-top:3px solid #7c9cff}.oc.group{border-top:3px solid #b07cff}
.oc.agent{border-top:3px solid #3ddc97}.oc.skill{border-top:3px solid #ffc35c}.oc.job,.oc.workflow{border-top:3px solid #ff7cc8}
.oc .hc{display:none;position:absolute;left:50%;top:calc(100% + 6px);transform:translateX(-50%);z-index:9;width:240px;text-align:left;padding:10px;border-radius:10px;background:var(--card2);border:1px solid var(--line);box-shadow:var(--sh);color:var(--fg);font-weight:400}
.oc:hover .hc{display:block}
.ix{font:11px var(--mono);color:var(--acc)}.cnt{color:var(--mut);font-size:11px}.outline{white-space:pre;font:12.5px/1.55 var(--mono)}
iframe{width:100%;height:76vh;border:0;border-radius:12px;background:var(--bg2)}.kb{line-height:1.7}.cite{font-size:11px;color:var(--acc)}
.menu-btn{display:none}
@media (max-width:900px){.app{grid-template-columns:1fr}aside{position:fixed;z-index:30;width:250px;transform:translateX(-100%);transition:transform .2s;background:var(--bg2)}
aside.open{transform:none}.menu-btn{display:inline-block}.search{min-width:0}.search span{display:none}.cols{grid-template-columns:1fr}main{padding:14px}
div[style*="grid-template-columns:220px"],div[style*="grid-template-columns:1.1fr"]{grid-template-columns:1fr!important}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style></head><body>
<div class="app"><aside id="side"><div class="logo"><i>⛬</i>Commons</div><nav class="nav" id="tabs" aria-label="Pages"></nav></aside>
<div><header><button class="menu-btn" aria-label="Menu" onclick="$('#side').classList.toggle('open')">☰</button><h1 id="ttl">Overview</h1><div class="spacer"></div>
<div class="search" role="button" tabindex="0" onclick="pal()" onkeydown="if(event.key=='Enter')pal()">⌕ <span>Search or jump to…</span> <kbd>⌘K</kbd></div>
<button onclick="theme()" aria-label="Toggle theme" title="Toggle theme (t)">◐</button><span id="ks"></span></header><main id="m" aria-live="polite">loading…</main></div></div>
<div id="pal" role="dialog" aria-modal="true" aria-label="Command palette" onclick="if(event.target.id=='pal')palClose()"><div class="box"><input id="palq" placeholder="Jump to a page, find a place, search memory…" autocomplete="off"><div class="res" id="palr"></div></div></div>
<div id="toasts" aria-live="assertive"></div>
<script>
const T=["Overview","Agents","Orchestration","Sessions","Usage","Memory","Knowledge","Preferences","Graph","Org chart","Skills","Discover","Import","Models","MCP hub","Context","Librarian","Secrets","Recurring"];
const ICON={Overview:'<path d="M3 12l9-8 9 8M5 10v10h14V10"/>',Agents:'<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5M16 4.5a3.5 3.5 0 010 7M18.5 14.8c1.6.8 2.6 2.6 3 5.2"/>',Orchestration:'<circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7 7.5l4 8.5M17 7.5l-4 8.5M7.5 6h9"/>',Sessions:'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M8 14h5"/>',Usage:'<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',Memory:'<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',Knowledge:'<path d="M4 5a2 2 0 012-2h13v16H6a2 2 0 00-2 2zM4 19V5M9 7h6"/>',Preferences:'<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',Graph:'<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="8" r="2.5"/><circle cx="9" cy="18" r="2.5"/><path d="M8.3 7l7.4.6M7 8.3l1.5 7.3M16.4 10l-5.6 6.4"/>',"Org chart":'<rect x="9" y="2.5" width="6" height="5" rx="1.2"/><rect x="2.5" y="16.5" width="6" height="5" rx="1.2"/><rect x="15.5" y="16.5" width="6" height="5" rx="1.2"/><path d="M12 7.5V12M5.5 16.5V12h13v4.5"/>',Skills:'<path d="M14.7 6.3a4 4 0 015 5L12 19l-5-5 7.7-7.7zM7 14l-4 4 3 3 4-4"/>',Models:'<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 1.5v3.5M15 1.5v3.5M9 19v3.5M15 19v3.5M1.5 9H5M1.5 15H5M19 9h3.5M19 15h3.5"/><rect x="9" y="9" width="6" height="6" rx="1"/>',"MCP hub":'<circle cx="12" cy="12" r="3"/><path d="M12 2v7M12 15v7M2 12h7M15 12h7M5 5l4.5 4.5M14.5 14.5L19 19M19 5l-4.5 4.5M9.5 14.5L5 19"/>',Context:'<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/><path d="M3 17.5l9 5 9-5" opacity=".5"/>',Discover:'<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',Import:'<path d="M12 3v12M7 10l5 5 5-5M4 17v3h16v-3"/>',Librarian:'<path d="M4 4h4v16H4zM10 4h4v16h-4zM16.5 4.5l3.8-1 3.2 15.5-3.8 1z"/>',Secrets:'<rect x="4" y="10.5" width="16" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 018 0v3.5"/>',Recurring:'<path d="M20 11a8 8 0 10-2.3 5.7M20 4v7h-7"/>'};
const GROUPS=[["Live",["Overview","Agents","Orchestration","Sessions","Usage"]],["Knowledge",["Memory","Knowledge","Context","Preferences","Graph","Org chart","Librarian"]],["Capabilities",["Skills","Discover","Import","Models","MCP hub"]],["Admin",["Secrets","Recurring"]]];
let [tab,hq]=(decodeURI(location.hash.slice(1))||"Overview").split("?");if(!T.includes(tab))tab="Overview";
if(hq){const u=new URLSearchParams(hq);if(u.get("scope"))window.gs={scope:u.get("scope"),id:u.get("id")||""};if(u.get("bucket"))window.kb=u.get("bucket");if(u.get("path"))window.oc_jump=u.get("path");if(u.get("mode"))window.oc_mode=u.get("mode");if(u.get("cite"))setTimeout(()=>cite(u.get("cite")),800)}
const $=s=>document.querySelector(s),E=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const api=p=>fetch("/api/"+p,{credentials:"same-origin"}).then(r=>r.json());
const post=(p,b)=>fetch("/api/"+p,{method:"POST",headers:{"X-Commons":"1","Content-Type":"application/json"},body:JSON.stringify(b)}).then(r=>r.json()).then(r=>{toast(r&&r.error?r.error:(p.replace(/\//g," › ")+" ✓"),r&&r.error?"err":"ok");render()}).catch(e=>toast(String(e),"err"));
function toast(msg,k="ok"){const t=document.createElement("div");t.className="toast "+k;t.textContent=msg;$("#toasts").appendChild(t);setTimeout(()=>{t.style.opacity=0;t.style.transition="opacity .4s"},3200);setTimeout(()=>t.remove(),3700)}
const empty=(ico,title,hint)=>`<div class="empty"><div class="ico">${ico}</div><b>${E(title)}</b>${hint||""}</div>`;
const ago=s=>s<60?s+"s":s<3600?Math.round(s/60)+"m":s<86400?Math.round(s/3600)+"h":Math.round(s/86400)+"d";
function spark(vals,w=160,h=38){const mx=Math.max(1e-9,...vals),n=vals.length;if(!n)return"";const pts=vals.map((v,i)=>[i*(w/(Math.max(1,n-1))),h-3-(v/mx)*(h-8)]);const d=pts.map((p,i)=>(i?"L":"M")+p[0].toFixed(1)+" "+p[1].toFixed(1)).join(" ");
return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><defs><linearGradient id="sg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#7c9cff" stop-opacity=".45"/><stop offset="1" stop-color="#7c9cff" stop-opacity="0"/></linearGradient></defs><path d="${d} L${w} ${h} L0 ${h}Z" fill="url(#sg)"/><path d="${d}" fill="none" stroke="#9fb4ff" stroke-width="2"/></svg>`}
const PAL=["#7c9cff","#b07cff","#ff7cc8","#3ddc97","#ffc35c","#5fd4ff","#ff8a5c","#c3e88d"];
function stacked(series,days){const names=Object.keys(series);if(!names.length)return empty("📉","No spend yet","<div>Agents log cost with <code>report_usage</code>; the worker does it automatically.</div>");
const tot=[...Array(days).keys()].map(d=>names.reduce((a,n)=>a+series[n][d],0)),mx=Math.max(1e-9,...tot),W=760,H=200,bw=W/days-6;
let svg=`<svg viewBox="0 0 ${W} ${H+22}" width="100%" role="img" aria-label="Daily cost by agent">`;for(let d=0;d<days;d++){let y=H;names.forEach((n,i)=>{const v=series[n][d],h=v/mx*(H-10);if(h>0){y-=h;svg+=`<rect x="${d*(W/days)+3}" y="${y}" width="${bw}" height="${h}" rx="3" fill="${PAL[i%PAL.length]}"><title>${E(n)}: $${v.toFixed(4)}</title></rect>`}})
const dt=new Date(Date.now()-(days-1-d)*864e5);svg+=`<text x="${d*(W/days)+3+bw/2}" y="${H+16}" fill="currentColor" opacity=".5" font-size="10" text-anchor="middle">${dt.getMonth()+1}/${dt.getDate()}</text>`}
return svg+`</svg><div>${names.map((n,i)=>`<span style="margin-right:12px"><span class="dot" style="background:${PAL[i%PAL.length]}"></span>${E(n)}</span>`).join("")}</div>`}
function theme(){const r=document.documentElement;r.dataset.theme=r.dataset.theme=="dark"?"light":"dark";localStorage.ctheme=r.dataset.theme}
if(localStorage.ctheme)document.documentElement.dataset.theme=localStorage.ctheme;else if(matchMedia("(prefers-color-scheme: light)").matches)document.documentElement.dataset.theme="light";
let palItems=[],palSel=0;
function pal(){$("#pal").classList.add("on");$("#palq").value="";palRender("");setTimeout(()=>$("#palq").focus(),10)}
function palClose(){$("#pal").classList.remove("on")}
async function palRender(q){const pages=T.filter(t=>t.toLowerCase().includes(q.toLowerCase())).map(t=>({kind:"page",label:t,sub:"page",go:t}));
palItems=pages;if(q.length>1){try{palItems=pages.concat(await api("palette?q="+encodeURIComponent(q)))}catch(e){}}palSel=0;palDraw()}
function palDraw(){$("#palr").innerHTML=palItems.map((x,i)=>`<div class="it ${i==palSel?"sel":""}" onclick="palGo(${i})">${x.kind=="page"?"↪":x.kind=="place"?"⌖":"◆"} ${E(x.label)}<small>${E(x.sub||"")}</small></div>`).join("")||'<div class="empty">No results</div>'}
function palGo(i){const x=palItems[i];if(!x)return;palClose();if(x.kind=="page")go(x.go);else if(x.kind=="place"){oc_root=x.path.includes("/")?x.path.split("/")[0]:"workspace";oc_jump=x.path;go("Org chart")}else{mq=x.id;go("Memory");setTimeout(()=>show(x.id),400)}}
let palT;document.addEventListener("input",e=>{if(e.target.id=="palq"){clearTimeout(palT);palT=setTimeout(()=>palRender(e.target.value),120)}});
document.addEventListener("keydown",e=>{const inField=/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()=="k"){e.preventDefault();pal();return}
if($("#pal").classList.contains("on")){if(e.key=="Escape")palClose();else if(e.key=="ArrowDown"){palSel=Math.min(palItems.length-1,palSel+1);palDraw();e.preventDefault()}else if(e.key=="ArrowUp"){palSel=Math.max(0,palSel-1);palDraw();e.preventDefault()}else if(e.key=="Enter")palGo(palSel);return}
if(inField)return;if(e.key=="/"){e.preventDefault();pal()}else if(e.key=="t")theme();else if(e.key=="r")render();else if(e.key=="?")toast("Shortcuts: ⌘K or / search · 1-9 pages · [ ] prev/next page · t theme · r refresh · +/- zoom org chart","ok");
else if(/^[1-9]$/.test(e.key))go(T[+e.key-1]);else if(e.key=="]"||e.key=="[")go(T[(T.indexOf(tab)+(e.key=="]"?1:T.length-1))%T.length]);else if((e.key=="+"||e.key=="=")&&tab=="Org chart")ocZoom(1.15);else if(e.key=="-"&&tab=="Org chart")ocZoom(1/1.15)});
const pill=s=>`<span class="pill ${E(s)}">${E(s)}</span>`;
function tabs(){$("#tabs").innerHTML=GROUPS.map(([g,ts])=>`<div class="nav-h">${g}</div>`+ts.map(t=>`<a href="#${encodeURI(t)}" class="${t==tab?'on':''}" ${t==tab?'aria-current="page"':""} onclick="go('${t}');return false"><svg viewBox="0 0 24 24">${ICON[t]||""}</svg>${t}${T.indexOf(t)<9?`<kbd>${T.indexOf(t)+1}</kbd>`:""}</a>`).join("")).join("");$("#ttl").textContent=tab;document.title=tab+" · Commons"}
function go(t){tab=t;location.hash=encodeURI(t);$("#side").classList.remove("open");render()}
function bars(rows,key,fmt){const mx=Math.max(1e-9,...rows.map(r=>r[key]));return rows.map(r=>`<tr><td>${E(r.label??r.key)}</td><td style="width:55%"><div class="bar" style="width:${100*r[key]/mx}%"></div></td><td>${fmt(r[key])}</td></tr>`).join("")}
const V={
async Overview(){const [o,act,us,ag]=await Promise.all([api("overview"),api("activity"),api("usage_series?days=14"),api("agents")]);
const cost=[...Array(us.days).keys()].map(d=>Object.values(us.series).reduce((a,v)=>a+v[d],0));
const st=(k,l,extra="")=>`<div class="card stat"><div class="mut">${l}</div><div class="big">${E(o[k])}</div>${extra}</div>`;
return `<div class="grid"><div class="card stat hero"><div class="mut">Spent · 24h</div><div class="big">$${(+o.spent_24h).toFixed(4)}</div>${spark(cost,170,34)}</div>
${st("running_jobs","Running jobs")}${st("agents","Agents")}${st("items","Memories",`<div class="mut" style="font-size:11px">tokens/day</div>${spark(us.tokens,150,26)}`)}${st("open_leftovers","Open leftovers")}${st("conflicts","Conflicts")}${st("entities","Graph entities")}${st("relations","Relations")}</div>
<div class="cols"><div class="card"><h3>Agents now</h3>${ag.length?`<table>${ag.slice(0,10).map(x=>{const age=x.last_seen?(Date.now()-Date.parse(x.last_seen))/1e3:1e9;const cls=x.claims.length?"busy":age<300?"live":"idle";return `<tr><td><span class="dot ${cls}" title="${cls}"></span><b>${E(x.name)}</b> <span class="mut">${E(x.platform||"")}</span></td><td>${x.claims.length?x.claims.map(c=>pill(c.type)+" "+E(c.title.slice(0,40))).join("<br>"):'<span class="mut">idle</span>'}</td><td class="mut">${x.last_seen?ago(Math.round(age))+" ago":"never"}</td></tr>`}).join("")}</table>`:empty("🤖","No agents yet","<div><code>commons connect --agent claude-code --platform claude-code</code></div>")}</div>
<div class="card"><h3>Live activity</h3>${act.length?`<ul class="feed">${act.map((e,i)=>`<li style="animation-delay:${i*18}ms"><span class="t">${ago(e.ago)}</span><span>${pill(e.type)} ${E(e.text)} ${e.agent?`<span class="mut">· ${E(e.agent)}</span>`:""}</span></li>`).join("")}</ul>`:empty("✨","Quiet so far","<div>Activity from agents, the librarian and MCP calls streams in here.</div>")}</div></div>
<div class="card mut">DB ${E(o.db)} · embedder ${E(o.embedder)} · ${o.paused?'<b class="failed">PAUSED: '+E(o.paused)+'</b>':'orchestration running'} · press <kbd>?</kbd> for shortcuts</div>`},
async Agents(){const a=await api("agents");return `<div class="card"><h3>Live agent activity</h3><table><tr><th>agent</th><th>platform</th><th>last seen</th><th>now doing (claims · lease left)</th><th>last activity</th><th>learned</th></tr>${a.map(x=>`<tr><td><b>${E(x.name)}</b><div class="mut">${E(x.model||"")}</div></td><td>${E(x.platform||x.vendor||"")} ${x.availability?pill(x.availability):""}</td><td>${E(x.last_seen)}</td>
<td>${x.claims.map(c=>`<div>${pill(c.type)} ${E(c.title.slice(0,60))} <span class="mut">· ${c.lease_left_s}s</span></div>`).join("")||'<span class="mut">idle</span>'}</td>
<td class="mut">${x.last_activity?E(x.last_activity.kind+": "+x.last_activity.summary.slice(0,80)):""}</td><td class="mut">${Object.entries(x.learned).map(([k,v])=>`${k} ${(v.success_rate*100).toFixed(0)}% (${v.n})`).join("<br>")}</td></tr>`).join("")}</table></div>`},
async Orchestration(){const j=await api("jobs");return j.map(x=>`<div class="card"><b>${E(x.title)}</b> ${pill(x.status)} <span class="mut">${E(x.pattern)} · tokens ${x.used.tokens||0} · $${(x.used.cost||0).toFixed(4)}${x.budget&&Object.keys(x.budget).length?" · budget "+E(JSON.stringify(x.budget)):""}</span>
<button onclick="openJob('${x.job_id}')">open</button> ${x.status=="running"?`<button class="bad" onclick="post('job/cancel',{id:'${x.job_id}'})">cancel</button>`:""}<div id="j_${x.job_id}"></div></div>`).join("")||`<div class="card">${empty("🧩","No jobs yet","<div>Submit one: <code>commons submit \"Build X\"</code> or the <code>submit_task</code> tool.</div>")}</div>`},
async Sessions(){const [s,l]=await Promise.all([api("sessions"),api("leftovers")]);return `<div class="card"><h3>Leftovers queue (${l.length})</h3><table>${l.map(x=>`<tr><td>${pill(x.task_type)} p${x.priority}</td><td>${E(x.text)}</td><td class="mut">${E(x.created_at)}</td><td><button onclick="post('leftover/resolve',{id:'${x.id}'})">done</button></td></tr>`).join("")}</table></div>
<div class="card"><h3>Sessions</h3><table><tr><th>goal</th><th>status</th><th>agent</th><th>project</th><th>updated</th><th>leftovers</th></tr>${s.map(x=>`<tr><td>${E(x.goal)}</td><td>${pill(x.status)}</td><td>${E(x.agent)}</td><td>${E(x.project)}</td><td class="mut">${E(x.updated_at)}</td><td class="mut">${x.leftovers.map(y=>E(y.text)+" ("+y.status+")").join("<br>")}</td></tr>`).join("")}</table></div>`},
async Usage(){const by=window.ub||"agent";const [u,ser]=await Promise.all([api("usage?by="+by),api("usage_series?days=14")]);return `<div class="card"><h3>Daily cost by agent · 14 days</h3>${stacked(ser.series,ser.days)}</div><div class="card">Group by ${["agent","project","job","model"].map(b=>`<button onclick="ub='${b}';render()">${b}</button>`).join(" ")} <span class="mut">· 24h spend $${u.spent_24h.toFixed(4)}</span>
<h3>Cost by ${by}</h3><table>${bars(u.rows,"cost",v=>"$"+v.toFixed(4))}</table><h3>Tokens by ${by}</h3><table>${bars(u.rows.map(r=>({...r,t:r.tokens_in+r.tokens_out})),"t",v=>v+" tok")}</table>
<h3>Daily cost</h3><table>${bars(u.daily.slice().reverse(),"cost",v=>"$"+v.toFixed(4))}</table>
<h3>Detail</h3><table><tr><th>${by}</th><th>in</th><th>out</th><th>$</th><th>time s</th><th>calls</th></tr>${u.rows.map(r=>`<tr><td>${E(r.label)}</td><td>${r.tokens_in}</td><td>${r.tokens_out}</td><td>${r.cost}</td><td>${r.seconds}</td><td>${r.calls}</td></tr>`).join("")}</table></div>`},
async Memory(){const q=window.mq||"";const [r,c]=await Promise.all([api("search?q="+encodeURIComponent(q)),api("conflicts")]);return `<div class="card"><input id="q" value="${E(q)}" placeholder="search memory…" size=40 onkeydown="if(event.key=='Enter'){mq=this.value;render()}"> <button onclick="mq=$('#q').value;render()">search</button> <button onclick="mq='';render()">recent</button>
<table>${r.results.map(x=>`<tr><td><a href="#" onclick="show('${x.id}');return false" class="mut">${x.id}</a></td><td>${pill(x.kind)}</td><td>${E(x.project||"")}/${E(x.agent||"")}${x.conflict?' <b class="failed">⚠conflict</b>':""}${x.authenticated?"":' <span class="mut" title="written without an api key">unverified</span>'}</td><td>${E(x.summary)}</td></tr>`).join("")}</table><pre id="det" hidden></pre></div>
<div class="card"><h3>Conflicts (${c.length})</h3>${c.map(x=>`<div><b>${E(x.subject)}.${E(x.predicate)}</b>: <button onclick="post('conflicts/resolve',{id:'${x.id}',keep:'${x.a}'})">keep “${E(x.a_item?.value)}” (${E(x.a_item?.agent)})</button> <button onclick="post('conflicts/resolve',{id:'${x.id}',keep:'${x.b}'})">keep “${E(x.b_item?.value)}” (${E(x.b_item?.agent)})</button></div>`).join("")||'<span class="mut">none</span>'}</div>`},
async Preferences(){const p=await api("prefs");return `<div class="card"><h3>What Commons learned about you</h3><p class="mut">Everything here is injected into agents' task pickups. Edit, confirm or delete anything.</p><table><tr><th>preference</th><th>scope</th><th>confidence</th><th>source</th><th>evidence</th><th></th></tr>${p.map(x=>`<tr><td><input value="${E(x.statement)}" id="p_${x.id}" size=45></td><td>${E(x.scope)}</td><td>${(x.confidence*100).toFixed(0)}% <span class="mut">×${x.observations}</span></td><td class="mut">${E(x.source)}</td><td class="mut">${E((x.evidence||[]).slice(-1)[0]||"").slice(0,90)}</td>
<td><button onclick="post('prefs/confirm',{id:'${x.id}',statement:$('#p_${x.id}').value})">save/confirm</button> <button class="bad" onclick="post('prefs/forget',{id:'${x.id}'})">forget</button></td></tr>`).join("")}</table><p><button class="bad" onclick="confirm('Delete ALL preferences?')&&post('prefs/forget',{everything:true})">forget everything</button></p></div>`},
async Graph(){const sc=await api("graph_scopes");const g=window.gs||{scope:"",id:""};
const opt=(scope,v,t)=>`<option value="${scope}|${E(v)}" ${g.scope==scope&&g.id==v?"selected":""}>${E(t)}</option>`;
const src=g.scope?`/graph.html?scope=${g.scope}&id=${encodeURIComponent(g.id)}&depth=${window.gd||2}`:"/graph.html?items=1";
return `<div class="card">View: <select onchange="const [a,b]=this.value.split('|');gs={scope:a,id:b};render()"><option value="|">everything</option>
<optgroup label="Projects">${sc.projects.map(p=>opt("project",p,p)).join("")}</optgroup>
<optgroup label="Jobs / tasks">${sc.jobs.map(j=>opt("job",j.id,j.title.slice(0,60))).join("")}</optgroup>
<optgroup label="Workflows">${sc.workflows.map(w=>opt("workflow",w,w)).join("")}</optgroup>
<optgroup label="Schedules">${sc.schedules.map(s=>opt("schedule",s.id,s.title)).join("")}</optgroup>
${g.scope=="node"?opt("node",g.id,"node "+g.id):""}</select>
depth <select onchange="gd=this.value;render()">${[1,2,3].map(d=>`<option ${d==(window.gd||2)?"selected":""}>${d}</option>`).join("")}</select>
<a href="${src}" target="_blank">open full screen</a> · <button onclick="post('graph/rebuild',{})">rebuild links</button></div><iframe src="${src}"></iframe>`},
async Knowledge(){const L=await api("kb_list");const b=window.kb||(L[0]&&L[0].bucket)||"misc";const P=await api("kb_page?bucket="+encodeURIComponent(b));const sec=window.ks&&P.pages[window.ks]?window.ks:P.sections[0];
const md=s=>E(s).replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/`([^`]+)`/g,"<code>$1</code>").replace(/\[\[(misc\/[\w-]+)\]\]/g,(m,x)=>`<a href="#" onclick="kb='${x}';render();return false">${x}</a>`).replace(/\[((?:mem|dec|fct|hof|chk|ent|prj|ses|agt|tsk|job|sub|lft|prf|rec|lea|usg)_[0-9a-f]+)\]/g,(m,id)=>`<a href="#" class="cite" onclick="cite('${id}');return false">[${id.slice(0,7)}…]</a>`).replace(/\n/g,"<br>");
const pg=P.pages[sec]||{content:"",citations:[]};
return `<div style="display:grid;grid-template-columns:220px 1fr 320px;gap:12px"><div class="card"><h3>Buckets</h3>${L.map(x=>`<div><a href="#" onclick="kb='${E(x.bucket)}';ks='';render();return false" ${x.bucket==b?'style="font-weight:700"':""}>${E(x.bucket)}</a> <span class="mut">${x.dirty?"·stale":""}</span></div>`).join("")}<p><button onclick="post('kb/refresh',{bucket:'${E(b)}'})">rebuild ${E(b)}</button></p></div>
<div class="card"><div>${P.sections.map(s=>`<button onclick="ks='${s}';render()" ${s==sec?'style="border-color:var(--acc)"':""}>${E(P.titles[s])}</button>`).join(" ")}</div>
<h2>${E(P.titles[sec]||"")} <span class="mut" style="font-size:12px">${E(b)} · updated ${E(pg.updated_at||"")} · ${pg.citations.length} sources</span></h2><div class="kb">${md(pg.content)}</div></div>
<div class="card" id="bl"><h3>Source</h3><p class="mut">Click a citation to see the record, its graph neighbours and where else it is cited.</p></div></div>`}};
async function cite(id){const b=await api("backlinks?id="+id);const d=await api("item?id="+id).catch(()=>({}));$("#bl").innerHTML=`<h3>${E(b.label[1])}</h3><div class="mut">${E(b.label[0])} · <code>${id}</code></div>
<p><a href="#" onclick="gs={scope:'node',id:'${id}'};go('Graph');return false">open in graph</a> · <a href="#" onclick="mq='${id}';go('Memory');show('${id}');return false">open in memory</a></p>
<pre>${E((d.content||d.statement||d.text||JSON.stringify(d,null,1)).slice(0,1200))}</pre><h4>Cited in</h4>${b.kb.map(k=>`<div><a href="#" onclick="kb='${E(k.bucket)}';ks='${k.section}';render();return false">${E(k.bucket)} / ${k.section}</a></div>`).join("")}
<h4>Links</h4>${b.edges.slice(0,25).map(e=>`<div class="mut">${E(b.nodes[e.src].name.slice(0,30))} —${e.type}→ ${E(b.nodes[e.dst].name.slice(0,30))}</div>`).join("")}`}
Object.assign(V,{
async Secrets(){const a=await api("audit");return `<div class="card"><h3>Secret references</h3><p class="mut">Values are never stored or displayed.</p><table>${a.refs.map(r=>`<tr><td><code>${E(r.ref)}</code></td><td>${E(r.description)}</td><td class="mut">agents: ${E(r.allowed_agents.join(", ")||"any")} · max ttl ${r.max_ttl}s</td></tr>`).join("")}</table>
<h3>Leases</h3><table>${a.leases.map(l=>`<tr><td>${l.id}</td><td><code>${E(l.ref)}</code></td><td>${E(l.agent)}</td><td>${E(l.scope)}</td><td class="mut">exp ${E(l.expires_at)}</td><td>${l.active?`<button class="bad" onclick="post('lease/revoke',{id:'${l.id}'})">revoke</button>`:'<span class="mut">'+(l.revoked?"revoked":"expired")+"</span>"}</td></tr>`).join("")}</table>
<h3>Audit log</h3><table>${a.log.map(l=>`<tr><td class="mut">${E(l.ts)}</td><td>${E(l.action)}</td><td>${E(l.target)}</td><td class="mut">${E(l.detail||"")}</td></tr>`).join("")}</table></div>`},
async Recurring(){const r=await api("recurring");return `<div class="card"><h3>Recurring schedules</h3><table><tr><th>name</th><th>cron</th><th>next run</th><th>last run</th><th>last job</th><th>task</th></tr>${r.map(x=>`<tr><td>${E(x.name)}</td><td><code>${E(x.cron)}</code></td><td>${E(x.next_run)}</td><td class="mut">${E(x.last_run)}</td><td class="mut">${E(x.last_job||"")}</td><td>${E(x.task.slice(0,120))}</td></tr>`).join("")}</table></div>`}});
Object.assign(V,{
async "Org chart"(){const root=window.oc_root||"workspace",mode=window.oc_mode||"chart";window.oc_open=window.oc_open||{};
if(window.oc_jump){const j=window.oc_jump;window.oc_jump=null;window.oc_center=1;const parts=j.replace(/^\w+\//,"").split(".");let pre=j.includes("/")?j.split("/")[0]+"/":"";for(let i=1;i<parts.length;i++)oc_open[pre+parts.slice(0,i).join(".")]=1;window.oc_sel=j}
const top=await api("tree_children?ref="+root);
const kids=async(ref)=>(await api("tree_children?ref="+encodeURIComponent(ref))).children;
async function nodeHtml(c){const open=oc_open[c.path];let sub="";if(open&&c.count){const ch=await kids(c.path);sub=`<ul>${(await Promise.all(ch.map(nodeHtml))).join("")}</ul>`}
return `<li><div class="oc ${c.kind} ${oc_sel==c.path?"sel":""}" tabindex="0" role="treeitem" aria-expanded="${!!open}" onkeydown="if(event.key=='Enter')ocT('${c.path}')" onclick="ocT('${c.path}')"><span class="ix">${E(c.path.replace(/^\w+\//,""))}</span> ${E(c.label.slice(0,38))}${c.count?` <span class="cnt">${open?"▾":"▸"} ${c.count}</span>`:""}<div class="mut" style="font-size:10px">${E(c.kind)}</div><div class="hc"><b>${E(c.label)}</b><div class="mut">${E(c.kind)} · path <code>${E(c.path)}</code>${c.id?` · <code>${E(c.id.slice(0,16))}</code>`:""}</div>${c.summary?`<div style="margin-top:6px">${E(c.summary)}</div>`:""}${c.count?`<div class="mut" style="margin-top:6px">${c.count} children · click to ${open?"collapse":"expand"}</div>`:""}</div></div>${sub}</li>`}
const crumbs=(window.oc_sel||"").replace(/^\w+\//,"").split(".").filter(Boolean);
const bc=[`<a href="#" onclick="oc_sel='';render();return false">${{workspace:"Workspace",agents:"Agents",skills:"Skill library"}[root]}</a>`].concat(crumbs.map((_,i)=>{const p=(root=="workspace"?"":root+"/")+crumbs.slice(0,i+1).join(".");return `<a href="#" onclick="oc_sel='${p}';render();return false">${p}</a>`})).join(" › ");
let body;if(mode=="outline"){const o=await api("tree_outline?depth=4&ref="+encodeURIComponent(oc_sel||root));body=`<pre class="outline">${E(o.text)}</pre>`}
else body=`<div class="ocwrap" id="ocw"><div class="zoombar"><button onclick="ocZoom(1.2)" aria-label="Zoom in">＋</button><button onclick="ocZoom(1/1.2)" aria-label="Zoom out">－</button><button onclick="ocFit()">fit</button></div><div class="ocstage" id="ocs"><ul class="orgchart" role="tree"><li><div class="oc root">${E({workspace:"Workspace",agents:"Agents",skills:"Skill library"}[root])} <span class="cnt" style="color:#fff">${top.children.length}</span></div><ul>${(await Promise.all(top.children.map(nodeHtml))).join("")}</ul></li></ul></div><div class="minimap" aria-hidden="true"><div class="mm" id="ocmm"></div><div class="vp" id="ocvp"></div></div></div>`;
if(!top.children.length)body=empty("🌱","Nothing to organize yet","<div>Projects, jobs, sessions, code and skills appear here as agents work.</div>");
return `<div class="card">Hierarchy ${["workspace","agents","skills"].map(r=>`<button ${r==root?'style="border-color:var(--acc)"':""} onclick="oc_root='${r}';oc_open={};oc_sel='';render()">${r=="workspace"?"Workspace › projects":r=="agents"?"Agent › model › tasks":"By skill"}</button>`).join(" ")}
· ${["chart","outline"].map(m=>`<button ${m==mode?'style="border-color:var(--acc)"':""} onclick="oc_mode='${m}';render()">${m}</button>`).join(" ")}
· <input id="ocq" placeholder="search & jump…" onkeydown="if(event.key=='Enter')ocFind()"> <button onclick="ocFind()">find</button> <button onclick="oc_open={};render()">collapse all</button>
<div class="mut" style="margin-top:6px">📍 ${bc}</div><div id="ochits"></div></div><div class="card">${body}</div>`},
async Skills(){const S=await api("skills");const h=window.sh?await api("skill_history?name="+encodeURIComponent(window.sh)):null;
return `<div style="display:grid;grid-template-columns:1.1fr 1fr;gap:12px"><div class="card"><h3>Skill library (${S.length})</h3><p class="mut">Agents fetch these on demand with skill_get — nothing to install locally.</p><table><tr><th>skill</th><th>active</th><th>uses · success</th><th>tags</th><th></th></tr>${S.map(x=>`<tr><td><b>${E(x.name)}</b><div class="mut">${E((x.description||"").slice(0,90))}</div>${x.forked_from?`<div class="mut">fork of ${E(x.forked_from)}</div>`:""}</td><td>v${x.active_version??"-"} ${x.pending.map(p=>pill(p.status)+" v"+p.version).join(" ")}</td><td>${x.stats.uses} · ${x.stats.success_rate==null?"–":(x.stats.success_rate*100).toFixed(0)+"%"}</td><td class="mut">${E(x.tags.join(", "))}</td><td><button onclick="sh='${E(x.name)}';render()">history</button></td></tr>`).join("")}</table></div>
<div class="card">${h?`<h3>${E(window.sh)} — history</h3>${h.map(v=>`<div class="card"><b>v${v.version}</b> ${pill(v.status)} <span class="mut">${E(v.author||"")} · ${E(v.created_at)} · ${E(v.changelog||"")}</span>
${v.findings&&v.findings.length?`<div class="failed">⚠ ${E(v.findings.map(f=>typeof f=="string"?f:JSON.stringify(f)).join("; "))}</div>`:""}${v.review&&v.review.by?`<div class="mut">review: ${E(v.review.verdict)} by ${E(v.review.by)} ${E(v.review.notes||"")}</div>`:""}
${["pending","flagged"].includes(v.status)?`<button onclick="post('skill/review',{name:'${E(window.sh)}',version:${v.version},approve:true})">approve</button> <button class="bad" onclick="post('skill/review',{name:'${E(window.sh)}',version:${v.version},approve:false})">reject</button>`:""}
${v.status=="superseded"||v.status=="approved"?`<button onclick="post('skill/rollback',{name:'${E(window.sh)}',version:${v.version}})">roll back to v${v.version}</button>`:""}
${v.diff?`<details><summary class="mut">diff</summary><pre>${E(v.diff)}</pre></details>`:""}</div>`).join("")}`:'<p class="mut">Pick a skill to see versions, diffs, scan findings and reviews.</p>'}</div></div>`},
async Models(){const m=await api("models_page");return `<div class="card"><h3>Providers</h3><table><tr><th>name</th><th>kind</th><th>key ref (never the value)</th><th>models</th><th>refreshed</th><th>error</th></tr>${m.providers.map(p=>`<tr><td>${E(p.name)}</td><td>${E(p.kind)}</td><td><code>${E(p.key_ref||"–")}</code></td><td>${p.models}</td><td class="mut">${E(p.last_refresh||"")}</td><td class="failed">${E(p.last_error||"")}</td></tr>`).join("")||'<tr><td class="mut">No providers. commons models add-provider openrouter openrouter --key-ref env://OPENROUTER_API_KEY</td></tr>'}</table><button onclick="post('models/refresh',{})">refresh lists · probe new · rebuild routing</button></div>
<div class="card"><h3>LLM roles</h3><table>${Object.entries(m.roles).map(([r,v])=>`<tr><td>${r}</td><td>${v.model?`<code>${E(v.model)}</code>`:'<span class="mut">heuristic</span>'} ${v.explicit||!v.model?"":'<span class="mut">(via distributor)</span>'}</td></tr>`).join("")}</table></div>
<div class="card"><h3>Routing table</h3><table><tr><th>task type</th><th>agent</th><th>model</th><th>why</th><th>source</th></tr>${m.routing.map(r=>`<tr><td>${pill(r.task_type)}</td><td>${E(r.agent)}</td><td><code>${E(r.model)}</code></td><td class="mut">${E(r.reason)}</td><td class="mut">${E(r.source)}</td></tr>`).join("")}</table></div>
<div class="card"><h3>Reallocation proposals</h3><table>${m.proposals.map(p=>`<tr><td>${pill(p.task_type)}</td><td><code>${E(p.from_model||"–")}</code> → <code>${E(p.to_model)}</code></td><td class="mut">${E(JSON.stringify(p.evidence).slice(0,140))}</td><td>${p.status=="proposed"?`<button onclick="post('routing/decide',{id:'${p.id}',approve:true})">apply</button> <button class="bad" onclick="post('routing/decide',{id:'${p.id}',approve:false})">reject</button>`:pill(p.status)}</td></tr>`).join("")||'<tr><td class="mut">none</td></tr>'}</table></div>
<div class="card"><h3>Model registry (${m.models.length})</h3><table><tr><th>model</th><th>ctx</th><th>$/M in·out</th><th>scores</th><th>agents</th></tr>${m.models.slice(0,200).map(x=>`<tr><td><code>${E(x.id)}</code> ${x.is_new?pill("new"):""}</td><td>${x.context||""}</td><td class="mut">${x.cost_in??""} · ${x.cost_out??""}</td><td class="mut">${Object.entries(x.scores||{}).map(([k,v])=>k+" "+(v*100).toFixed(0)+"%").join(" ")}</td><td class="mut">${E((x.agents||[]).join(", "))}</td></tr>`).join("")}</table></div>`},
async Librarian(){const L=await api("librarian");const fq=window.fq||"";const f=fq?await api("find?q="+encodeURIComponent(fq)):null;
return `<div class="card"><h3>Librarian</h3><span>mode <b>${E(L.mode)}</b> · auto-approve <b>${L.auto_approve?"on":"off"}</b> <button onclick="post('librarian/config',{auto_approve:${!L.auto_approve}})">turn ${L.auto_approve?"off":"on"}</button> · budget $${L.budget} · ${L.new_since_last_run} new items since last run</span>
<p><button onclick="post('librarian/run',{})">run now (incremental)</button> <button onclick="post('librarian/run',{full:true})">full pass</button></p>
${L.last_run?`<div class="mut">last run ${E(new Date(L.last_run.ts*1000).toLocaleString())}: scanned ${L.last_run.items_scanned}, tagged ${L.last_run.tagged}, assigned ${L.last_run.assigned}, links +${L.last_run.links_added}, summaries ${L.last_run.summaries}, merges ${L.last_run.dedupe.merged.length} (proposed ${L.last_run.dedupe.proposed.length}), index ${L.last_run.indexed} terms</div>`:""}</div>
<div class="card"><h3>Where to find…</h3><input id="fq" value="${E(fq)}" size=40 placeholder="e.g. billing invoices" onkeydown="if(event.key=='Enter'){fq=this.value;render()}"> ${L.suggested.map(s=>`<button onclick="fq='${E(s)}';render()">${E(s)}</button>`).join(" ")}
${f?`<table>${f.locations.map(l=>`<tr><td><a href="#" onclick="oc_root='${l.path.includes("/")?l.path.split("/")[0]:"workspace"}';oc_jump='${l.path}';go('Org chart');return false"><code>${E(l.path)}</code></a></td><td>${pill(l.kind)}</td><td>${E(l.label)}</td><td class="mut">${E(l.rid||"")}</td></tr>`).join("")||'<tr><td class="mut">nothing</td></tr>'}</table>`:""}</div>
<div class="card"><h3>Review queue (${L.proposals.length})</h3><table>${L.proposals.map(p=>`<tr><td>${pill(p.kind)}</td><td>${E(p.target_label.slice(0,70))} <span class="mut">${E(p.target)}</span></td><td class="mut">${E(p.reason)}</td><td>${p.kind=="merge"?`<button onclick="post('librarian/decide',{id:'${p.id}',approve:true})">merge</button>`:`<button onclick="post('librarian/decide',{id:'${p.id}',approve:true})">ack</button>`} <button class="bad" onclick="post('librarian/decide',{id:'${p.id}',approve:false})">dismiss</button></td></tr>`).join("")||'<tr><td class="mut">empty</td></tr>'}</table></div>
<div class="card"><h3>Activity (every change is undoable)</h3><table>${L.recent.map(e=>`<tr><td class="mut">${E(e.ts)}</td><td>${pill(e.action)}</td><td>${E(e.target)}</td><td class="mut">${E(e.before).slice(0,60)} → ${E(e.after).slice(0,80)}</td><td>${e.status=="applied"&&e.reversible?`<button onclick="post('librarian/undo',{id:'${e.id}'})">undo</button>`:pill(e.status)}</td></tr>`).join("")}</table></div>`}});
Object.assign(V,{
async "MCP hub"(){const m=await api("mcp");const add=window.mcp_add;
const catRow=c=>`<tr><td><b>${E(c.id)}</b> ${c.verified?'<span class="pill ok" title="verified against vendor docs">verified</span>':'<span class="pill pending">community</span>'}<div class="mut">${E(c.title)}</div></td><td>${pill(c.transport)}</td><td class="mut">${E(c.auth||"")}</td><td><a href="${E(c.docs)}" target="_blank" rel="noopener">docs ↗</a></td><td><button onclick="mcp_add='${E(c.id)}';render()">Add</button></td></tr>`;
const form=add?(()=>{const c=m.catalog.find(x=>x.id==add);return `<div class="card"><h3>Add ${E(c.title)}</h3>${c.notes?`<p class="mut">${E(c.notes)}</p>`:""}
${c.params.map(p=>`<div><label>${E(p)} <input id="mp_${E(p)}" placeholder="${E(p)}"></label></div>`).join("")}${c.secrets.map(x=>`<div><label>${E(x)} secret ref <input id="mr_${E(x)}" value="env://${E(x)}" size="34"></label> <span class="mut">reference only — never paste a raw key</span></div>`).join("")}
<p><button class="pri" onclick="mcpAdd('${E(c.id)}')">Connect</button> <button onclick="mcp_add=null;render()">cancel</button></p></div>`})():"";
return form+`<div class="grid">${m.servers.map(x=>`<div class="card stat"><div><span class="dot ${x.status=="up"?"live":x.status=="down"?"":"idle"}" style="${x.status=="down"?"background:var(--bad)":""}"></span><b>${E(x.name)}</b> ${pill(x.transport)}</div><div class="big">${x.tools}<span class="mut" style="font-size:13px"> tools</span></div><div class="mut">${x.calls_24h} calls/24h · restarts ${x.restarts}</div>${x.last_error?`<div class="failed" style="font-size:11px">${E(x.last_error.slice(0,90))}</div>`:""}<p><button onclick="post('mcp/health',{name:'${E(x.name)}'})">health check</button> <button class="bad" onclick="confirm('Remove ${E(x.name)}?')&&post('mcp/remove',{name:'${E(x.name)}'})">remove</button></p></div>`).join("")||`<div class="card" style="grid-column:1/-1">${empty("🔌","No upstream MCP servers yet","<div>Add one from the catalog below, or import your existing configs: <code>commons mcp import</code></div>")}</div>`}</div>
<div class="card"><h3>Approvals waiting (${m.approvals.length})</h3>${m.approvals.length?`<table>${m.approvals.map(a=>`<tr><td><code>${E(a.server)}__${E(a.tool)}</code></td><td class="mut">${E(a.agent||"")}</td><td><code>${E(a.args.slice(0,140))}</code></td><td><button class="pri" onclick="post('mcp/approve',{id:'${a.id}',approve:true})">approve</button> <button class="bad" onclick="post('mcp/approve',{id:'${a.id}',approve:false})">deny</button></td></tr>`).join("")}</table>`:'<span class="mut">Nothing needs approval.</span>'}</div>
<div class="cols"><div class="card"><h3>Tools (${m.tools.length})</h3><table>${m.tools.slice(0,150).map(t=>`<tr><td><code>${E(t.server)}__${E(t.name)}</code></td><td>${pill(t.risk)}</td><td class="mut">${E(t.description)}</td></tr>`).join("")||'<tr><td class="mut">—</td></tr>'}</table></div>
<div class="card"><h3>Policies</h3><table>${m.policies.map(p=>`<tr><td>${pill(p.effect)}</td><td>agent <code>${E(p.agent)}</code></td><td><code>${E(p.server)}__${E(p.tool)}</code></td></tr>`).join("")||'<tr><td class="mut">Defaults: read/write allowed, destructive needs approval.</td></tr>'}</table>
<p><select id="pe"><option>deny</option><option>approve</option><option>allow</option></select> <input id="pa" placeholder="agent or *" size=8> <input id="ps" placeholder="server" size=8> <input id="pt" placeholder="tool glob" size=10> <button onclick="post('mcp/policy',{effect:$('#pe').value,agent:$('#pa').value,server:$('#ps').value,tool:$('#pt').value})">add rule</button></p></div></div>
<div class="card"><h3>Call log <span class="mut">(args redacted)</span></h3><table><tr><th>when</th><th>agent</th><th>tool</th><th>status</th><th>ms</th><th>args</th></tr>${m.calls.map(c=>`<tr><td class="mut">${E(c.ts)}</td><td>${E(c.agent||"")}</td><td><code>${E(c.server)}__${E(c.tool)}</code></td><td>${pill(c.status)}${c.cached?' <span class="pill">cache</span>':""}</td><td>${c.ms||""}</td><td class="mut"><code>${E((c.args||"").slice(0,90))}</code></td></tr>`).join("")}</table></div>
<div class="card"><h3>Catalog <span class="mut">v${E(m.catalog_version)}</span></h3><p class="mut">Verified presets were checked against each vendor's own docs. Extend in <code>~/.commons/mcp_catalog.json</code>.</p><table>${m.catalog.map(catRow).join("")}</table></div>`}});
Object.assign(V,{
async Discover(){const mode=window.dm||"trending",q=window.dq||"",kind=window.dk||"";const d=await api(`discover?mode=${mode}&q=${encodeURIComponent(q)}&kind=${kind}`);const st=d.status;
return `<div class="card"><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">${[["trending","🔥 Hot / trending"],["popular","🏆 All-time popular"]].map(([m,l])=>`<button class="${m==mode?"pri":""}" onclick="dm='${m}';render()">${l}</button>`).join("")}
<input id="dq" value="${E(q)}" placeholder="search skills…" onkeydown="if(event.key=='Enter'){dq=this.value;render()}"> <select onchange="dk=this.value;render()">${["","skill","plugin","rules","agents_md","list"].map(k=>`<option ${k==kind?"selected":""} value="${k}">${k||"all kinds"}</option>`).join("")}</select>
<span class="spacer"></span><span class="mut">${st.entries} entries · last scan ${E(st.last_run?st.last_run.ts:"never")} · token ${st.token_ref?`<code>${E(st.token_ref)}</code>`:'<span class="pill pending">none</span>'}</span> <button onclick="post('discover/run',{})">scan GitHub now</button></div></div>
<div class="card">${d.rows.length?`<table><tr><th>skill</th><th>★</th><th>Δ 1d / 7d</th><th>forks</th><th>kind</th><th>license</th><th>updated</th><th></th></tr>${d.rows.map(r=>`<tr><td><b>${E(r.name)}</b> <a href="${E(r.url)}" target="_blank" rel="noopener" class="mut">${E(r.id)} ↗</a><div class="mut">${E(r.description.slice(0,110))}</div></td><td><b>${r.stars}</b></td><td>${r.delta_1d>0?`<span class="done">+${r.delta_1d}</span>`:"0"} / ${r.delta_7d>0?`<span class="done">+${r.delta_7d}</span>`:"0"}</td><td>${r.forks}</td><td>${pill(r.kind)}</td><td>${r.license_ok?pill("ok")+" ":`<span class="pill pending" title="not on allow-list">${E(r.license)}</span>`} ${r.license_ok?E(r.license):""}</td><td class="mut">${r.days_since_push<9999?r.days_since_push+"d ago":""}</td>
<td>${r.imported_skill?pill(r.status):r.path?`<button class="pri" onclick="post('discover/add',{id:'${E(r.id)}',allow_license:${!r.license_ok}&&confirm('License ${E(r.license)} is not on the allow-list. Import anyway after checking terms?')})">Add to pool</button>`:'<span class="mut">repo/list</span>'}</td></tr>`).join("")}</table>`:empty("🔭","No discovered skills yet","<div>Configure a token ref (<code>commons skills configure --token-ref env://GITHUB_TOKEN</code>) and click <b>scan GitHub now</b>. The scheduler refreshes daily.</div>")}</div>
<div class="card mut">Imports are pinned to a commit, run through the review gate (license allow-list, secret + prompt-injection scan) and never auto-activate. Upstream changes arrive as new pending versions with diffs.</div>`},
async Import(){const p=window.ip||"";const r=await api("import_scan?paths="+encodeURIComponent(p));
return `<div class="card"><h3>Import skills installed on this machine</h3><p class="mut">Scans ~/.claude/skills, project .claude/skills, Claude Code plugins, .cursor/rules, AGENTS.md, ~/.codex and custom paths. Duplicates of pool skills are skipped.</p>
<input id="ipth" size=50 value="${E(p)}" placeholder="extra paths, comma separated"> <button onclick="ip=$('#ipth').value;render()">rescan</button></div>
<div class="card"><h3>Found ${r.imported.length} new · ${r.duplicates.length} already in pool</h3>${r.imported.length?`<table>${r.imported.map(x=>`<tr><td><b>${E(x.name)}</b></td><td>${pill(x.kind)}</td><td class="mut"><code>${E(x.path)}</code></td></tr>`).join("")}</table>
<p><label><input type="checkbox" id="irl"> replace local copies with the Commons bootstrap pointer (originals backed up to ~/.commons/skill-backups)</label></p><button class="pri" onclick="post('import/run',{paths:$('#ipth').value,replace_local:$('#irl').checked})">Import ${r.imported.length} into pool</button>`:empty("📦","Nothing new to import","<div>Everything found is already in the pool.</div>")}
${r.duplicates.length?`<details><summary class="mut">duplicates</summary>${r.duplicates.map(x=>`<div class="mut"><code>${E(x.path)}</code> = ${E(x.same_as)}</div>`).join("")}</details>`:""}</div>`}});
Object.assign(V,{
async Context(){const sel=window.cxs||"";const c=await api("context"+(sel?"?id="+sel:""));const sv=c.savings;
return `<div class="grid"><div class="card stat hero"><div class="mut">Tokens saved by layering</div><div class="big">${sv.tokens_saved_by_layering.toLocaleString()}</div><div class="mut">${sv.loads} loads · ${sv.tokens_loaded.toLocaleString()} tokens actually loaded</div></div>
<div class="card stat"><div class="mut">Provider cache hits</div><div class="big">${sv.provider_cached_tokens.toLocaleString()}</div><div class="mut">cached input tokens</div></div>
<div class="card stat"><div class="mut">Dedup</div><div class="big">${sv.dedup_ratio??"–"}×</div><div class="mut">${sv.section_refs} section refs → ${sv.blobs} blobs</div></div>
<div class="card stat"><div class="mut">Packs</div><div class="big">${c.packs.length}</div></div></div>
<div class="cols"><div class="card"><h3>Context packs</h3>${c.packs.length?`<table><tr><th>pack</th><th>L0 · L1 · L2 · L3 tokens</th><th>new / reused chars</th><th>loads · saved</th></tr>${c.packs.map(p=>`<tr style="cursor:pointer" onclick="cxs='${p.id}';render()"><td><b>${E(p.name)}</b> ${p.parent?pill("delta"):pill("base")}<div class="mut">${E(p.gist.slice(0,90))}</div><div class="mut" style="font-size:11px">${E(p.agent||"")} · ${E(p.created_at)} · prefix <code>${E(p.prefix_hash)}</code></div></td>
<td>${["L0","L1","L2","L3"].map(l=>`<span title="${l}">${p.tokens[l]}</span>`).join(" · ")}<div style="display:flex;height:6px;border-radius:9px;overflow:hidden;margin-top:4px;background:var(--line)">${["L0","L1","L2","L3"].map((l,i)=>`<div style="width:${100*p.tokens[l]/Math.max(1,p.full_tokens)}%;background:${PAL[i]}"></div>`).join("")}</div></td><td>${p.new_chars} / <span class="done">${p.reused_chars}</span></td><td>${p.loads} · <b>${p.tokens_saved}</b></td></tr>`).join("")}</table>`:empty("🧳","No context packs yet","<div>Agents call <code>context_save</code> before a handoff and pass the pack id; receivers <code>context_load</code> only the layer they need.</div>")}</div>
<div class="card">${sel&&c.diff?`<h3>Pack ${E(sel)}</h3><div class="mut">vs parent ${E(c.diff.from||"–")}: +${c.diff.added.length} sections, −${c.diff.removed.length}, ~${c.diff.changed.length} changed</div>${c.diff.changed.map(x=>`<details><summary>${E(x.title)}</summary><pre>${E(x.diff)}</pre></details>`).join("")}<h4>L1 (cache-friendly prefix)</h4><pre>${E(c.l1)}</pre>`:'<p class="mut">Select a pack to see its delta vs the parent and its L1 brief.</p>'}</div></div>`}});
async function mcpAdd(id){const c=(await api("mcp")).catalog.find(x=>x.id==id);const params={},refs={};c.params.forEach(p=>{const v=$("#mp_"+p).value;if(v)params[p]=v});c.secrets.forEach(x=>{const v=$("#mr_"+x).value;if(v)refs[x]=v});window.mcp_add=null;post("mcp/add_preset",{preset:id,params,refs})}
function ocT(p){oc_sel=p;oc_open[p]=!oc_open[p];render()}
window.oc_tf=window.oc_tf||{x:0,y:0,k:1,fit:true};
function ocApply(){const st=$("#ocs");if(!st)return;st.style.transform=`translate(${oc_tf.x}px,${oc_tf.y}px) scale(${oc_tf.k})`;ocMini()}
function ocFit(){const w=$("#ocw"),st=$("#ocs");if(!w||!st)return;const k=Math.min(1.2,Math.max(.25,Math.min(w.clientWidth/st.scrollWidth,w.clientHeight/st.scrollHeight)));oc_tf={x:(w.clientWidth-st.scrollWidth*k)/2,y:10,k,fit:true};ocApply()}
function ocZoom(f,cx,cy){const w=$("#ocw");if(!w)return;cx=cx??w.clientWidth/2;cy=cy??w.clientHeight/2;const k=Math.min(3,Math.max(.15,oc_tf.k*f));oc_tf.x=cx-(cx-oc_tf.x)*k/oc_tf.k;oc_tf.y=cy-(cy-oc_tf.y)*k/oc_tf.k;oc_tf.k=k;oc_tf.fit=false;ocApply()}
function ocMini(){const w=$("#ocw"),st=$("#ocs"),mm=$("#ocmm"),vp=$("#ocvp");if(!mm||!st)return;const W=190,H=120,sw=st.scrollWidth,sh=st.scrollHeight,m=Math.min(W/sw,H/sh);
if(!mm.dataset.done){mm.innerHTML=st.innerHTML;mm.dataset.done=1}mm.style.transform=`scale(${m})`;mm.style.width=sw+"px";
vp.style.left=Math.max(0,-oc_tf.x/oc_tf.k*m)+"px";vp.style.top=Math.max(0,-oc_tf.y/oc_tf.k*m)+"px";vp.style.width=Math.min(W,w.clientWidth/oc_tf.k*m)+"px";vp.style.height=Math.min(H,w.clientHeight/oc_tf.k*m)+"px"}
function ocInit(){const w=$("#ocw");if(!w)return;if(oc_tf.fit)ocFit();else ocApply();let drag=null;
w.addEventListener("wheel",e=>{e.preventDefault();const r=w.getBoundingClientRect();ocZoom(e.deltaY<0?1.1:1/1.1,e.clientX-r.left,e.clientY-r.top)},{passive:false});
w.addEventListener("pointerdown",e=>{if(e.target.closest(".oc,button"))return;drag={x:e.clientX-oc_tf.x,y:e.clientY-oc_tf.y};w.classList.add("drag");w.setPointerCapture(e.pointerId)});
w.addEventListener("pointermove",e=>{if(!drag)return;oc_tf.x=e.clientX-drag.x;oc_tf.y=e.clientY-drag.y;oc_tf.fit=false;ocApply()});
w.addEventListener("pointerup",()=>{drag=null;w.classList.remove("drag")});
const sel=document.querySelector(".oc.sel");if(sel&&window.oc_center){window.oc_center=0;const r=sel.getBoundingClientRect(),wr=w.getBoundingClientRect();oc_tf.x+=wr.width/2-(r.left-wr.left+r.width/2);oc_tf.y+=wr.height/3-(r.top-wr.top);oc_tf.fit=false;ocApply()}}
async function ocFind(){const q=$("#ocq").value;const h=await api("tree_find?root="+(window.oc_root||"workspace")+"&q="+encodeURIComponent(q));$("#ochits").innerHTML=h.map(x=>`<div><a href="#" onclick="oc_jump='${x.path}';render();return false"><code>${E(x.path)}</code> ${E(x.breadcrumbs.join(" › "))}</a></div>`).join("")||'<span class="mut">no match</span>'}
async function openJob(id){const j=await api("job?id="+id);$("#j_"+id).innerHTML=`<table><tr><th>key</th><th>type</th><th>status</th><th>agent</th><th>gate</th><th>attempts</th><th>result</th></tr>${j.subtasks.map(s=>`<tr><td>${E(s.key)}</td><td>${pill(s.type)}</td><td>${pill(s.status)}</td><td>${E(s.agent)}</td><td>${s.gates.length?"gated by "+s.gates.length:s.gate_of?"gate":""} ${s.verdict?pill(s.verdict):""}</td><td>${s.attempts}</td><td><details><summary class="mut">${E((s.result||"").slice(0,70))}</summary><pre>${E(s.result||"")}</pre></details></td></tr>`).join("")}</table>${j.result?`<h4>Merged result</h4><pre>${E(j.result)}</pre>`:""}`}
async function show(id){const d=await api("item?id="+id);const el=$("#det");el.hidden=false;el.textContent=JSON.stringify(d,null,1)}
async function render(){tabs();const o=await api("overview");$("#ks").innerHTML=o.paused?`<span class="pill failed">PAUSED</span> <button class="pri" onclick="post('killswitch',{paused:false})">resume</button>`:`<button class="bad" title="Pause all orchestration" onclick="post('killswitch',{paused:true,reason:'dashboard kill switch'})">⏻ Kill switch</button>`;
try{const html=await V[tab]();const m=$("#m");if(m.dataset.tab!=tab){m.style.animation="none";m.offsetHeight;m.style.animation=""}m.dataset.tab=tab;m.innerHTML=html;if(tab=="Org chart")ocInit()}catch(e){$("#m").innerHTML='<div class="card failed">'+E(e)+'</div>'}}
render();setInterval(()=>{if(["Overview","Agents","Orchestration"].includes(tab)&&!document.querySelector("details[open]"))render()},5000);
</script></body></html>"""
