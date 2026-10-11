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

    def projects(self, q):
        return [dict(r) for r in self.s.q("SELECT id,name FROM projects ORDER BY name")]

    # ---- mutations
    def post(self, path, body):
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


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Commons dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1117;--card:#181b24;--fg:#e6e6e6;--mut:#8b93a7;--acc:#6ea8fe;--ok:#4caf50;--bad:#ef5350;--warn:#ffb300}
*{box-sizing:border-box}body{margin:0;font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg)}
nav{display:flex;gap:4px;flex-wrap:wrap;padding:8px 12px;background:#12141c;border-bottom:1px solid #222;position:sticky;top:0}
nav b{margin-right:12px}nav a{color:var(--mut);padding:6px 10px;border-radius:6px;cursor:pointer;text-decoration:none}
nav a.on{background:var(--card);color:var(--fg)}main{padding:14px;max-width:1300px;margin:auto}
.card{background:var(--card);border-radius:8px;padding:12px;margin-bottom:12px}
table{width:100%;border-collapse:collapse}td,th{padding:5px 6px;border-bottom:1px solid #262a36;text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:500}.mut{color:var(--mut)}.pill{padding:1px 7px;border-radius:9px;background:#2a3040;font-size:12px}
.done,.approve,.active{color:var(--ok)}.failed,.reject,.cancelled{color:var(--bad)}.claimed,.awaiting_gate,.running{color:var(--warn)}
button{background:#2a3040;color:var(--fg);border:1px solid #39405a;border-radius:6px;padding:3px 9px;cursor:pointer}
button.bad{border-color:var(--bad)}input{background:#11131a;color:var(--fg);border:1px solid #333;border-radius:6px;padding:5px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}.big{font-size:24px;font-weight:600}
.bar{height:14px;background:var(--acc);border-radius:3px}pre{white-space:pre-wrap;background:#11131a;padding:8px;border-radius:6px;max-height:300px;overflow:auto}
iframe{width:100%;height:78vh;border:0;border-radius:8px}
</style></head><body>
<nav><b>⛬ Commons</b><span id="tabs"></span><span style="margin-left:auto" id="ks"></span></nav><main id="m">loading…</main>
<script>
const T=["Overview","Agents","Orchestration","Sessions","Usage","Memory","Preferences","Graph","Secrets","Recurring"];
let tab=location.hash.slice(1)||"Overview";
const $=s=>document.querySelector(s),E=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const api=p=>fetch("/api/"+p,{credentials:"same-origin"}).then(r=>r.json());
const post=(p,b)=>fetch("/api/"+p,{method:"POST",headers:{"X-Commons":"1","Content-Type":"application/json"},body:JSON.stringify(b)}).then(r=>r.json()).then(()=>render());
const pill=s=>`<span class="pill ${E(s)}">${E(s)}</span>`;
function tabs(){$("#tabs").innerHTML=T.map(t=>`<a class="${t==tab?'on':''}" onclick="go('${t}')">${t}</a>`).join("")}
function go(t){tab=t;location.hash=t;render()}
function bars(rows,key,fmt){const mx=Math.max(1e-9,...rows.map(r=>r[key]));return rows.map(r=>`<tr><td>${E(r.label??r.key)}</td><td style="width:55%"><div class="bar" style="width:${100*r[key]/mx}%"></div></td><td>${fmt(r[key])}</td></tr>`).join("")}
const V={
async Overview(){const o=await api("overview");return `<div class="grid">${["items","entities","relations","agents","sessions","running_jobs","open_leftovers","conflicts","leases"].map(k=>`<div class="card"><div class="mut">${k.replace("_"," ")}</div><div class="big">${o[k]}</div></div>`).join("")}
<div class="card"><div class="mut">spent (24h)</div><div class="big">$${o.spent_24h}</div></div></div>
<div class="card mut">DB ${E(o.db)} · embedder ${E(o.embedder)} · ${o.paused?'<b class="failed">PAUSED: '+E(o.paused)+'</b>':'orchestration running'}</div>`+await V.Agents()},
async Agents(){const a=await api("agents");return `<div class="card"><h3>Live agent activity</h3><table><tr><th>agent</th><th>platform</th><th>last seen</th><th>now doing (claims · lease left)</th><th>last activity</th><th>learned</th></tr>${a.map(x=>`<tr><td><b>${E(x.name)}</b><div class="mut">${E(x.model||"")}</div></td><td>${E(x.platform||x.vendor||"")} ${x.availability?pill(x.availability):""}</td><td>${E(x.last_seen)}</td>
<td>${x.claims.map(c=>`<div>${pill(c.type)} ${E(c.title.slice(0,60))} <span class="mut">· ${c.lease_left_s}s</span></div>`).join("")||'<span class="mut">idle</span>'}</td>
<td class="mut">${x.last_activity?E(x.last_activity.kind+": "+x.last_activity.summary.slice(0,80)):""}</td><td class="mut">${Object.entries(x.learned).map(([k,v])=>`${k} ${(v.success_rate*100).toFixed(0)}% (${v.n})`).join("<br>")}</td></tr>`).join("")}</table></div>`},
async Orchestration(){const j=await api("jobs");return j.map(x=>`<div class="card"><b>${E(x.title)}</b> ${pill(x.status)} <span class="mut">${E(x.pattern)} · tokens ${x.used.tokens||0} · $${(x.used.cost||0).toFixed(4)}${x.budget&&Object.keys(x.budget).length?" · budget "+E(JSON.stringify(x.budget)):""}</span>
<button onclick="openJob('${x.job_id}')">open</button> ${x.status=="running"?`<button class="bad" onclick="post('job/cancel',{id:'${x.job_id}'})">cancel</button>`:""}<div id="j_${x.job_id}"></div></div>`).join("")||'<div class="card mut">No jobs yet. Submit one with submit_task.</div>'},
async Sessions(){const [s,l]=await Promise.all([api("sessions"),api("leftovers")]);return `<div class="card"><h3>Leftovers queue (${l.length})</h3><table>${l.map(x=>`<tr><td>${pill(x.task_type)} p${x.priority}</td><td>${E(x.text)}</td><td class="mut">${E(x.created_at)}</td><td><button onclick="post('leftover/resolve',{id:'${x.id}'})">done</button></td></tr>`).join("")}</table></div>
<div class="card"><h3>Sessions</h3><table><tr><th>goal</th><th>status</th><th>agent</th><th>project</th><th>updated</th><th>leftovers</th></tr>${s.map(x=>`<tr><td>${E(x.goal)}</td><td>${pill(x.status)}</td><td>${E(x.agent)}</td><td>${E(x.project)}</td><td class="mut">${E(x.updated_at)}</td><td class="mut">${x.leftovers.map(y=>E(y.text)+" ("+y.status+")").join("<br>")}</td></tr>`).join("")}</table></div>`},
async Usage(){const by=window.ub||"agent";const u=await api("usage?by="+by);return `<div class="card">Group by ${["agent","project","job","model"].map(b=>`<button onclick="ub='${b}';render()">${b}</button>`).join(" ")} <span class="mut">· 24h spend $${u.spent_24h.toFixed(4)}</span>
<h3>Cost by ${by}</h3><table>${bars(u.rows,"cost",v=>"$"+v.toFixed(4))}</table><h3>Tokens by ${by}</h3><table>${bars(u.rows.map(r=>({...r,t:r.tokens_in+r.tokens_out})),"t",v=>v+" tok")}</table>
<h3>Daily cost</h3><table>${bars(u.daily.slice().reverse(),"cost",v=>"$"+v.toFixed(4))}</table>
<h3>Detail</h3><table><tr><th>${by}</th><th>in</th><th>out</th><th>$</th><th>time s</th><th>calls</th></tr>${u.rows.map(r=>`<tr><td>${E(r.label)}</td><td>${r.tokens_in}</td><td>${r.tokens_out}</td><td>${r.cost}</td><td>${r.seconds}</td><td>${r.calls}</td></tr>`).join("")}</table></div>`},
async Memory(){const q=window.mq||"";const [r,c]=await Promise.all([api("search?q="+encodeURIComponent(q)),api("conflicts")]);return `<div class="card"><input id="q" value="${E(q)}" placeholder="search memory…" size=40 onkeydown="if(event.key=='Enter'){mq=this.value;render()}"> <button onclick="mq=$('#q').value;render()">search</button> <button onclick="mq='';render()">recent</button>
<table>${r.results.map(x=>`<tr><td><a href="#" onclick="show('${x.id}');return false" class="mut">${x.id}</a></td><td>${pill(x.kind)}</td><td>${E(x.project||"")}/${E(x.agent||"")}${x.conflict?' <b class="failed">⚠conflict</b>':""}${x.authenticated?"":' <span class="mut" title="written without an api key">unverified</span>'}</td><td>${E(x.summary)}</td></tr>`).join("")}</table><pre id="det" hidden></pre></div>
<div class="card"><h3>Conflicts (${c.length})</h3>${c.map(x=>`<div><b>${E(x.subject)}.${E(x.predicate)}</b>: <button onclick="post('conflicts/resolve',{id:'${x.id}',keep:'${x.a}'})">keep “${E(x.a_item?.value)}” (${E(x.a_item?.agent)})</button> <button onclick="post('conflicts/resolve',{id:'${x.id}',keep:'${x.b}'})">keep “${E(x.b_item?.value)}” (${E(x.b_item?.agent)})</button></div>`).join("")||'<span class="mut">none</span>'}</div>`},
async Preferences(){const p=await api("prefs");return `<div class="card"><h3>What Commons learned about you</h3><p class="mut">Everything here is injected into agents' task pickups. Edit, confirm or delete anything.</p><table><tr><th>preference</th><th>scope</th><th>confidence</th><th>source</th><th>evidence</th><th></th></tr>${p.map(x=>`<tr><td><input value="${E(x.statement)}" id="p_${x.id}" size=45></td><td>${E(x.scope)}</td><td>${(x.confidence*100).toFixed(0)}% <span class="mut">×${x.observations}</span></td><td class="mut">${E(x.source)}</td><td class="mut">${E((x.evidence||[]).slice(-1)[0]||"").slice(0,90)}</td>
<td><button onclick="post('prefs/confirm',{id:'${x.id}',statement:$('#p_${x.id}').value})">save/confirm</button> <button class="bad" onclick="post('prefs/forget',{id:'${x.id}'})">forget</button></td></tr>`).join("")}</table><p><button class="bad" onclick="confirm('Delete ALL preferences?')&&post('prefs/forget',{everything:true})">forget everything</button></p></div>`},
async Graph(){const p=await api("projects");const cur=window.gp||"";return `<div class="card">Project: <select onchange="gp=this.value;render()"><option value="">(all)</option>${p.map(x=>`<option ${x.name==cur?"selected":""}>${E(x.name)}</option>`).join("")}</select> <a href="/graph.html?project=${encodeURIComponent(cur)}" target="_blank">open full screen</a></div><iframe src="/graph.html?project=${encodeURIComponent(cur)}&items=1"></iframe>`},
async Secrets(){const a=await api("audit");return `<div class="card"><h3>Secret references</h3><p class="mut">Values are never stored or displayed.</p><table>${a.refs.map(r=>`<tr><td><code>${E(r.ref)}</code></td><td>${E(r.description)}</td><td class="mut">agents: ${E(r.allowed_agents.join(", ")||"any")} · max ttl ${r.max_ttl}s</td></tr>`).join("")}</table>
<h3>Leases</h3><table>${a.leases.map(l=>`<tr><td>${l.id}</td><td><code>${E(l.ref)}</code></td><td>${E(l.agent)}</td><td>${E(l.scope)}</td><td class="mut">exp ${E(l.expires_at)}</td><td>${l.active?`<button class="bad" onclick="post('lease/revoke',{id:'${l.id}'})">revoke</button>`:'<span class="mut">'+(l.revoked?"revoked":"expired")+"</span>"}</td></tr>`).join("")}</table>
<h3>Audit log</h3><table>${a.log.map(l=>`<tr><td class="mut">${E(l.ts)}</td><td>${E(l.action)}</td><td>${E(l.target)}</td><td class="mut">${E(l.detail||"")}</td></tr>`).join("")}</table></div>`},
async Recurring(){const r=await api("recurring");return `<div class="card"><h3>Recurring schedules</h3><table><tr><th>name</th><th>cron</th><th>next run</th><th>last run</th><th>last job</th><th>task</th></tr>${r.map(x=>`<tr><td>${E(x.name)}</td><td><code>${E(x.cron)}</code></td><td>${E(x.next_run)}</td><td class="mut">${E(x.last_run)}</td><td class="mut">${E(x.last_job||"")}</td><td>${E(x.task.slice(0,120))}</td></tr>`).join("")}</table></div>`}};
async function openJob(id){const j=await api("job?id="+id);$("#j_"+id).innerHTML=`<table><tr><th>key</th><th>type</th><th>status</th><th>agent</th><th>gate</th><th>attempts</th><th>result</th></tr>${j.subtasks.map(s=>`<tr><td>${E(s.key)}</td><td>${pill(s.type)}</td><td>${pill(s.status)}</td><td>${E(s.agent)}</td><td>${s.gates.length?"gated by "+s.gates.length:s.gate_of?"gate":""} ${s.verdict?pill(s.verdict):""}</td><td>${s.attempts}</td><td><details><summary class="mut">${E((s.result||"").slice(0,70))}</summary><pre>${E(s.result||"")}</pre></details></td></tr>`).join("")}</table>${j.result?`<h4>Merged result</h4><pre>${E(j.result)}</pre>`:""}`}
async function show(id){const d=await api("item?id="+id);const el=$("#det");el.hidden=false;el.textContent=JSON.stringify(d,null,1)}
async function render(){tabs();const o=await api("overview");$("#ks").innerHTML=o.paused?`<b class="failed">PAUSED</b> <button onclick="post('killswitch',{paused:false})">resume</button>`:`<button class="bad" onclick="post('killswitch',{paused:true,reason:'dashboard kill switch'})">⏻ kill switch</button>`;
try{$("#m").innerHTML=await V[tab]()}catch(e){$("#m").innerHTML='<div class="card failed">'+E(e)+'</div>'}}
render();setInterval(()=>{if(["Overview","Agents","Orchestration"].includes(tab)&&!document.querySelector("details[open]"))render()},5000);
</script></body></html>"""
