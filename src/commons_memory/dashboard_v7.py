"""v0.7 dashboard views: Mission control, Task breakdown, Routing, Presence & messages, Drive."""
from __future__ import annotations

import base64

from .store import CommonsError


def mission(self, q):
    from .scheduler import Scheduler
    return Scheduler(self.s).mission_control()


def jobs_index(self, q):
    return [dict(r) for r in self.s.q("SELECT id, title, status, created_at FROM jobs ORDER BY created_at DESC LIMIT 100")]


def breakdown_view(self, q):
    from .breakdown import breakdown
    jid = q.get("job") or (self.s.q("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1") or [{"id": None}])[0]["id"]
    if not jid:
        return {"job": None}
    return breakdown(self.s, jid)


def routing_view(self, q):
    from .routing import Router
    r = Router(self.s)
    return {"mode": r.mode(), "policy": r.policy(), "tiers": r.tiers(), "decisions": r.decisions(int(q.get("limit", 60)))}


def presence_view(self, q):
    from .messaging import Dispatcher, Messaging, board
    return {"board": board(self.s), "messages": Messaging(self.s).history(None, limit=40),
            "dead": Dispatcher.dead_letters(self.s, 20)}


def drive_ls(self, q):
    from .drive import Drive
    d = Drive(self.s)
    return {"list": d.list(q.get("path") or "/", q.get("project") or None, recursive=False), "usage": d.usage(),
            "projects": [""] + [r["name"] for r in self.s.q("SELECT name FROM projects ORDER BY name")]}


def drive_file(self, q):
    from .drive import Drive
    d = Drive(self.s)
    p = q.get("project") or None
    out = d.read(q["path"], p, version=int(q["version"]) if q.get("version") else None, max_tokens=int(q.get("max_tokens", 20000)))
    if not q["path"].startswith("/mnt/"):
        out["history"] = d.history(q["path"], p)
        out["links"] = d.stat(q["path"], p).get("links", [])
    return out


def drive_raw(api, q) -> tuple[bytes, str]:
    from .drive import Drive
    data, info = Drive(api.s).read_bytes(q["path"], q.get("project") or None, int(q["version"]) if q.get("version") else None)
    return data, info.get("mime") or "application/octet-stream"


def post(self, path, body):
    """Returns NotImplemented when the path isn't a v0.7 action."""
    if path == "/api/sched/project":
        from .scheduler import Scheduler
        return Scheduler(self.s).configure(body["project"], priority=body.get("priority"), max_concurrent=body.get("max_concurrent"),
                                           weight=body.get("weight"), budget_cost=body.get("budget_cost"), paused=body.get("paused"))
    if path == "/api/sched/job":
        from .scheduler import Scheduler
        return Scheduler(self.s).set_job(body["job"], priority=body.get("priority"))
    if path == "/api/sched/preempt":
        from .scheduler import Scheduler
        return Scheduler(self.s).preempt(int(body.get("below", 1)), bool(body.get("requeue")))
    if path == "/api/sched/resume":
        from .scheduler import Scheduler
        return Scheduler(self.s).resume(body.get("job"))
    if path == "/api/routing/policy":
        from .routing import Router
        return Router(self.s).set_policy(body["task_type"], effort=body.get("effort") or None, tier=body.get("tier") or None)
    if path == "/api/routing/mode":
        from .routing import Router
        return {"mode": Router(self.s).set_mode(body["mode"])}
    if path == "/api/msg/send":
        from .messaging import Messaging
        m = Messaging(self.s)
        return m.send(m._aid("user"), body["to"], body["text"], mtype=body.get("type", "message"))
    if path == "/api/wake/redrive":
        from .messaging import Dispatcher
        return {"redriven": Dispatcher.redrive(self.s, body.get("id"))}
    if path.startswith("/api/drive/"):
        from .drive import Drive
        d = Drive(self.s)
        p = body.get("project") or None
        me = self.s.ensure_agent("user")
        if path == "/api/drive/upload":
            data = base64.b64decode(body["b64"]) if body.get("b64") is not None else body.get("text", "").encode()
            return d.write(body["path"], data, project=p, agent_id=me, message=body.get("message", "dashboard upload"),
                           expected_version=body.get("expected_version"))
        if path == "/api/drive/delete":
            return d.delete(body["path"], p, me)
        if path == "/api/drive/restore":
            return d.restore(body["path"], body.get("version"), p, me)
        if path == "/api/drive/move":
            return d.move(body["path"], body["dst"], p, me)
        raise CommonsError("unknown drive action")
    return NotImplemented


def install(API) -> None:
    for f in (mission, jobs_index, breakdown_view, routing_view, presence_view, drive_ls, drive_file):
        setattr(API, f.__name__, f)
    orig = API.post

    def post_v7(self, path, body):
        r = post(self, path, body)
        return orig(self, path, body) if r is NotImplemented else r
    API.post = post_v7


TABS = ["Mission control", "Task breakdown", "Routing", "Presence", "Drive"]
ICONS = {
    "Mission control": '<circle cx="12" cy="12" r="9"/><path d="M12 12l5-3M12 3v2M21 12h-2M12 21v-2M3 12h2"/>',
    "Task breakdown": '<rect x="9" y="2.5" width="6" height="4" rx="1"/><rect x="2.5" y="17.5" width="6" height="4" rx="1"/><rect x="15.5" y="17.5" width="6" height="4" rx="1"/><rect x="9" y="10" width="6" height="4" rx="1"/><path d="M12 6.5V10M12 14v1.5H5.5v2M12 15.5h6.5v2"/>',
    "Routing": '<path d="M4 6h10l4 4-4 4H4M4 18h7"/>',
    "Presence": '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4-6 8-6s7 2 8 6"/><circle cx="19" cy="5" r="2" fill="currentColor"/>',
    "Drive": '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
}

JS = r"""
V["Mission control"]=async function(){const d=await api("mission");const t=d.totals;
return `<div class="grid4"><div class="card stat"><div class="mut">Running jobs</div><div class="big">${t.running_jobs}</div></div><div class="card stat"><div class="mut">Subtasks in flight</div><div class="big">${t.inflight}</div></div><div class="card stat"><div class="mut">Done last hour</div><div class="big">${t.done_last_hour}</div></div><div class="card stat"><div class="mut">Agents</div><div class="big">${d.agents.filter(a=>a.state=="online").length}/${d.agents.length}</div><div class="mut">online</div></div></div>
<div class="card"><h3>Projects <button class="bad" onclick="post('sched/preempt',{below:1,requeue:false})">pause priority&lt;1</button> <button onclick="post('sched/resume',{})">resume all</button></h3>${d.projects.length?d.projects.map(p=>`<div style="margin:10px 0"><b>${E(p.project||"(no project)")}</b> ${p.paused?pill("paused"):""} <span class="mut">prio ${p.priority} · in flight ${p.inflight}${p.max_concurrent?"/"+p.max_concurrent:""} · queued ${p.queued} · ${p.throughput_per_h}/h · $${p.cost}</span>
 <button onclick="post('sched/project',{project:${J(p.project)},priority:${(p.priority||0)+1}})">▲</button><button onclick="post('sched/project',{project:${J(p.project)},priority:${(p.priority||0)-1}})">▼</button><button onclick="post('sched/project',{project:${J(p.project)},paused:${!p.paused}})">${p.paused?"unpause":"pause"}</button>
 <table>${p.jobs.map(j=>`<tr><td><a href="#Task breakdown" onclick="window.bj=${J(j.id)};go('Task breakdown');return false">${E(j.title.slice(0,70))}</a></td><td>${pill(j.status)}</td><td class="mut">p${j.priority||0}</td><td style="width:30%"><div style="background:#8883;border-radius:4px"><div style="width:${Math.round(j.progress*100)}%;background:#4caf50;height:8px;border-radius:4px"></div></div></td><td class="mut">${Object.entries(j.subtasks).map(([k,v])=>E(k)+":"+v).join(" ")}</td></tr>`).join("")}</table></div>`).join(""):empty("🛰","Nothing running","Submit jobs in several projects; they run concurrently with fair share.")}</div>
<div class="card"><h3>Agents · load vs capacity</h3><table>${d.agents.map(a=>{const never=a.seen===false;const cls=never?"offline":dotFor(a.state);return `<tr><td><span class="dot ${cls}"></span><b>${E(a.agent)}</b></td><td>${pill(never?"offline":a.state)} ${never?'<span class="mut">never seen</span>':""}</td><td style="width:25%"><div style="background:#8883;border-radius:4px"><div style="width:${Math.min(100,Math.round(a.load/Math.max(1,a.capacity)*100))}%;background:#2196f3;height:8px;border-radius:4px"></div></div></td><td class="mut">${a.load}/${a.capacity}</td><td class="mut">${a.working_on.map(w=>E(w.title.slice(0,40))).join("<br>")}</td></tr>`}).join("")}</table></div>`};

V["Task breakdown"]=async function(){const jobs=await api("jobs_index");const d=await api("breakdown_view?job="+(window.bj||""));
if(!d.job)return empty("🧩","No jobs yet","<code>commons submit \"...\"</code>");
const jobSel=`<div class="card"><h3>Job <select onchange="window.bj=this.value;window.bn=null;render()">${jobs.map(j=>`<option value="${E(j.id)}" ${j.id==d.job.id?"selected":""}>${E((j.title||"").slice(0,60))} · ${E(j.status)}</option>`).join("")}</select></h3>`;
if(!d.nodes||!d.nodes.length)return jobSel+empty("🧩","No subtasks","This job has no breakdown yet.")+"</div>";
const lv={},deps={},vis=new Set();(d.edges||[]).forEach(e=>{(deps[e.to]=deps[e.to]||[]).push(e.from)});
const L=k=>{if(lv[k]!==undefined)return lv[k];if(vis.has(k))return 0;vis.add(k);const m=(deps[k]||[]).reduce((acc,x)=>Math.max(acc,L(x)+1),0);vis.delete(k);lv[k]=m;return m};d.nodes.forEach(n=>L(n.key));
const cols={};d.nodes.forEach(n=>{(cols[lv[n.key]]=cols[lv[n.key]]||[]).push(n)});const W=190,H=58,pos={};
Object.entries(cols).forEach(([c,ns])=>ns.forEach((n,i)=>pos[n.key]=[20+c*(W+50),20+i*(H+24)]));
const vals=Object.values(pos);const mw=(vals.length?Math.max(...vals.map(p=>p[0])):20)+W+30,mh=(vals.length?Math.max(...vals.map(p=>p[1])):20)+H+30;
const col={done:"#4caf50",claimed:"#2196f3",pending:"#9e9e9e",failed:"#f44336",blocked:"#ff9800",cancelled:"#777"};
const svg=`<svg width="${mw}" height="${mh}" style="max-width:100%">${d.edges.map(e=>{const a=pos[e.from],b=pos[e.to];if(!a||!b)return"";return `<path d="M${a[0]+W},${a[1]+H/2} C${a[0]+W+25},${a[1]+H/2} ${b[0]-25},${b[1]+H/2} ${b[0]},${b[1]+H/2}" fill="none" stroke="${e.kind=="review"?"#ff9800":"#888"}" stroke-dasharray="${e.kind=="review"?"4 3":""}"/>`}).join("")}
${d.nodes.map(n=>{const p=pos[n.key];return `<g style="cursor:pointer" onclick="window.bn=${J(n.key)};render()"><rect x="${p[0]}" y="${p[1]}" width="${W}" height="${H}" rx="8" fill="${col[n.status]||"#999"}22" stroke="${col[n.status]||"#999"}" stroke-width="${window.bn==n.key?3:1.2}"/><text x="${p[0]+8}" y="${p[1]+18}" font-size="12" font-weight="bold" fill="currentColor">${E(n.key)} · ${E(n.type)}</text><text x="${p[0]+8}" y="${p[1]+34}" font-size="11" fill="currentColor">${E((n.agent||"unassigned").slice(0,14))} ${n.model?"· "+E(n.model.split("/").pop().slice(0,16)):""}</text><text x="${p[0]+8}" y="${p[1]+49}" font-size="10" fill="currentColor" opacity=".7">${E(n.status)} · ${E(n.effort||"-")} · try ${n.attempts} · $${n.cost}</text></g>`}).join("")}</svg>`;
const n=d.nodes.find(x=>x.key==window.bn)||d.nodes[0];
return `<div class="card"><h3>Job <select onchange="window.bj=this.value;window.bn=null;render()">${jobs.map(j=>`<option value="${E(j.id)}" ${j.id==d.job.id?"selected":""}>${E((j.title||"").slice(0,60))} · ${E(j.status)}</option>`).join("")}</select></h3>
<div class="mut">${E(d.job.project||"")} · ${pill(d.job.status)} · pattern ${E(d.planning.pattern)} · ${E(d.planning.summary)} · total $${d.totals.cost} · ${d.totals.tokens} tokens${d.job.after.length?" · waits for "+d.job.after.map(E).join(", "):""}</div>
<details><summary>Planning step</summary><pre style="white-space:pre-wrap">${E(d.job.body)}</pre>${d.planning.events.map(e=>`<div class="mut">${E(e.event)} ${E(e.by||"")}: ${E(e.detail.slice(0,400))}</div>`).join("")}</details>
<div style="overflow:auto">${svg}</div></div>
<div class="cols"><div class="card"><h3>Tree</h3><ul>${d.nodes.filter(x=>!x.gate_of).map(x=>`<li><a href="#" onclick="window.bn=${J(x.key)};render();return false">${E(x.key)}</a> ${pill(x.status)} ${E(x.title.slice(0,60))}<ul>${d.nodes.filter(g=>g.gate_of==x.key).map(g=>`<li><a href="#" onclick="window.bn=${J(g.key)};render();return false">${E(g.key)}</a> ${pill(g.type)} ${pill(g.status)} ${g.verdict?E(JSON.stringify(g.verdict).slice(0,40)):""}</li>`).join("")}</ul></li>`).join("")}</ul></div>
<div class="card"><h3>${E(n.key)} · ${E(n.title)}</h3><table><tr><td>agent</td><td>${E(n.agent||"-")}</td></tr><tr><td>model</td><td><code>${E(n.model||"-")}</code></td></tr><tr><td>effort</td><td>${E(n.effort||"-")} (difficulty ${E(n.difficulty||"-")})</td></tr><tr><td>why</td><td class="mut">${E(n.rationale||"-")}</td></tr><tr><td>attempts</td><td>${n.attempts}/${n.max_attempts}</td></tr><tr><td>tokens / cost</td><td>${n.tokens.in}+${n.tokens.out} · $${n.cost}</td></tr></table>
${n.escalations.length>1?`<h4>Escalations</h4><table>${n.escalations.map(x=>`<tr><td>#${x.attempt}</td><td>${E(x.agent||"")}</td><td><code>${E(x.model||"")}</code></td><td>${E(x.effort||"")}</td><td>${pill(x.outcome||"open")}</td></tr>`).join("")}</table>`:""}
<h4>Timeline</h4><ul class="feed">${n.timeline.map(t=>`<li><span class="t">${new Date(t.at*1000).toLocaleTimeString()}</span><span>${E(t.event)} ${E(t.by||"")} <span class="mut">${E(t.detail)}</span></span></li>`).join("")}</ul>
${n.artifacts.length?`<h4>Artifacts</h4>${n.artifacts.map(a=>`<div><a href="#Drive" onclick="window.dp=${J(a.project||"")};window.df=${J(a.path)};go('Drive');return false">${E(a.path)}</a> v${a.version}</div>`).join("")}`:""}
<h4>Output</h4><pre style="white-space:pre-wrap;max-height:300px;overflow:auto">${E(n.result||"")}${n.result_truncated?"…":""}</pre></div></div>`};

V["Routing"]=async function(){const d=await api("routing_view");const lv=["minimal","low","medium","high","max"],tiers=["cheap","standard","top","coding"];
const by={};d.decisions.forEach(x=>{const k=(x.model||"?")+" · "+(x.level||"?");const b=by[k]=by[k]||{n:0,ok:0,cost:0,q:0,qn:0};b.n++;if(x.outcome=="success"||x.outcome=="approved")b.ok++;b.cost+=x.cost||x.est_cost||0;if(x.quality!=null){b.q+=x.quality;b.qn++}});
return `<div class="card"><h3>Mode ${["quality-first","balanced","cost-first"].map(m=>`<button class="${d.mode==m?"pri":""}" onclick="post('routing/mode',{mode:'${m}'})">${m}</button>`).join(" ")}</h3></div>
<div class="cols"><div class="card"><h3>Policy (editable)</h3><table><tr><th>task type</th><th>effort</th><th>tier</th></tr>${Object.entries(d.policy).map(([t,p])=>`<tr><td>${pill(t)}</td><td><select onchange="post('routing/policy',{task_type:${J(t)},effort:this.value})">${lv.map(l=>`<option ${p.effort==l?"selected":""}>${l}</option>`).join("")}</select></td><td><select onchange="post('routing/policy',{task_type:${J(t)},tier:this.value})">${tiers.map(l=>`<option ${p.tier==l?"selected":""}>${l}</option>`).join("")}</select></td></tr>`).join("")}</table></div>
<div class="card"><h3>Cost &amp; quality per tier</h3><table><tr><th>model · effort</th><th>tasks</th><th>success</th><th>avg quality</th><th>cost</th></tr>${Object.entries(by).map(([k,b])=>`<tr><td><code>${E(k)}</code></td><td>${b.n}</td><td>${Math.round(b.ok/b.n*100)}%</td><td>${b.qn?(b.q/b.qn).toFixed(2):"–"}</td><td>$${b.cost.toFixed(4)}</td></tr>`).join("")||'<tr><td class="mut">no decisions yet</td></tr>'}</table></div></div>
<div class="card"><h3>Recent assignments</h3><table><tr><th>task</th><th>agent</th><th>model</th><th>effort</th><th>difficulty</th><th>why</th><th>outcome</th><th>cost</th></tr>${d.decisions.map(x=>`<tr><td>${pill(x.task_type)} <span class="mut">${E((x.subtask_id||"").slice(-8))}</span></td><td>${E(x.agent||x.agent_id||"")}</td><td><code>${E(x.model||"-")}</code></td><td>${E(x.level||"")}</td><td>${E(x.difficulty||"")}</td><td class="mut">${E((x.rationale||"").slice(0,120))}</td><td>${pill(x.outcome||"open")}</td><td>$${(x.cost||x.est_cost||0).toFixed(4)}</td></tr>`).join("")}</table></div>`};

V["Presence"]=async function(){const d=await api("presence_view");
return `<div class="card"><h3>Presence board</h3><table><tr><th>agent</th><th>state</th><th>status</th><th>live</th><th>wake channels</th><th>unread</th></tr>${d.board.map(a=>{const never=a.seen===false;const cls=never?"offline":dotFor(a.state);return `<tr><td><span class="dot ${cls}"></span><b>${E(a.agent)}</b></td><td>${pill(never?"offline":a.state)} ${never?'<span class="mut">never seen</span>':""}</td><td class="mut">${E(a.status||"")}</td><td>${a.live_connections}</td><td>${a.channels.map(pill).join(" ")||'<span class="mut">none</span>'}</td><td>${a.unread||0}</td></tr>`}).join("")}</table></div>
<div class="cols"><div class="card"><h3>Messages</h3><div><input id="mto" placeholder="to (agent or *)" style="width:30%"> <input id="mtx" placeholder="message" style="width:50%"> <button onclick="post('msg/send',{to:$('#mto').value,text:$('#mtx').value})">send</button></div><ul class="feed">${d.messages.map(m=>`<li><span class="t">${new Date(m.ts*1000).toLocaleTimeString()}</span><span>${pill(m.type)} <b>${E(m.from)}</b> → ${E(m.to)}: ${E((m.text||"").slice(0,200))}</span></li>`).join("")}</ul></div>
<div class="card"><h3>Dead-lettered wakes ${d.dead.length?`<button onclick="post('wake/redrive',{})">redrive all</button>`:""}</h3>${d.dead.length?`<table>${d.dead.map(x=>`<tr><td>${E(x.event)}</td><td class="mut">${E(x.last_error||"")}</td><td>${x.attempts}</td></tr>`).join("")}</table>`:'<p class="mut">none</p>'}</div></div>`};

V["Drive"]=async function(){const P=window.dp||"",path=window.dpath||"/";const d=await api(`drive_ls?project=${encodeURIComponent(P)}&path=${encodeURIComponent(path)}`);
let pv="";if(window.df){try{const f=await api(`drive_file?project=${encodeURIComponent(P)}&path=${encodeURIComponent(window.df)}`);const raw=`/drive/raw?project=${encodeURIComponent(P)}&path=${encodeURIComponent(window.df)}`;
pv=`<div class="card"><h3>${E(window.df)} <a href="${raw}" download>download</a> ${f.history?`<button class="bad" onclick="post('drive/delete',{project:${J(P)},path:${J(window.df)}});window.df=null">delete</button>`:""}</h3><div class="mut">v${f.version||"-"} · ${f.size||""} B · ${E(f.mime||"")} ${f.locked_by?"· locked by "+E(f.locked_by):""} ${(f.links||[]).map(l=>"· "+E(l.rel)+" "+E(l.ref)).join(" ")}</div>
${f.binary?((f.mime||"").startsWith("image/")?`<img src="${raw}" style="max-width:100%">`:(f.mime=="application/pdf"?`<iframe src="${raw}" style="width:100%;height:600px;border:0"></iframe>`:'<p class="mut">binary file</p>')):`<textarea id="dtext" style="width:100%;height:340px;font-family:monospace">${E(f.text)}</textarea><button class="pri" onclick="post('drive/upload',{project:${J(P)},path:${J(window.df)},text:$('#dtext').value,expected_version:${f.version||0}})">save (new version)</button>`}
${f.history?`<h4>History</h4><table>${f.history.map(h=>`<tr><td>v${h.version}</td><td>${E(h.author||"")}</td><td class="mut">${E(h.message||"")}</td><td>${new Date(h.at*1000).toLocaleString()}</td><td><button onclick="post('drive/restore',{project:${J(P)},path:${J(window.df)},version:${h.version}})">restore</button></td></tr>`).join("")}</table>`:""}</div>`}catch(e){pv=""}}
const up=`<input type="file" id="dup"> <button onclick="(async()=>{const f=$('#dup').files[0];if(!f)return;const b=await f.arrayBuffer();let s='';new Uint8Array(b).forEach(c=>s+=String.fromCharCode(c));post('drive/upload',{project:${J(P)},path:${J(path.replace(/\/$/,""))}+'/'+f.name,b64:btoa(s)})})()">upload</button>`;
return `<div class="card"><h3>Commons Drive · <select onchange="window.dp=this.value;window.dpath='/';window.df=null;render()">${d.projects.map(p=>`<option value="${E(p)}" ${p==P?"selected":""}>${E(p||"(shared)")}</option>`).join("")}</select> <span class="mut">${E(path)}</span></h3>${up}
<table>${path!="/"?`<tr><td><a href="#" onclick="window.dpath=${J(path.split("/").slice(0,-1).join("/")||"/")};render();return false">..</a></td></tr>`:""}${(d.list.dirs||[]).map(x=>`<tr><td>📁 <a href="#" onclick="window.dpath=${J(x)};render();return false">${E(x.split("/").pop())}</a></td></tr>`).join("")}${(d.list.mounts||[]).map(x=>`<tr><td>🖴 <a href="#" onclick="window.dpath=${J(x)};render();return false">${E(x)}</a></td></tr>`).join("")}${(d.list.files||[]).map(f=>`<tr><td>${f.dir?"📁":"📄"} <a href="#" onclick="${f.dir?`window.dpath=${J(f.path)}`:`window.df=${J(f.path)}`};render();return false">${E(f.path.split("/").pop())}</a></td><td class="mut">${f.version?"v"+f.version:""}</td><td class="mut">${f.size??""}</td><td class="mut">${E(f.updated_by||"")}</td></tr>`).join("")}</table>
<div class="mut">${d.usage.map(u=>`${E(u.project||"shared")}: ${u.files} files, ${(u.bytes/1e6).toFixed(2)} MB`).join(" · ")}</div></div>${pv}`};
"""
