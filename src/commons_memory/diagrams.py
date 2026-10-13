"""Interactive, self-contained HTML diagrams of the code graph and job plans (v0.8).

Inspired by Archify (https://github.com/tt-a1i/archify, MIT): source-backed architecture diagrams as standalone
HTML artifacts. This is an independent implementation (no Archify code): a layered DAG layout in vanilla JS/SVG,
no CDN or network access, with search, click-to-inspect details, upstream/downstream reach tracing, pan/zoom and
SVG/PNG export.
"""
from __future__ import annotations

import html
import json
import re

from .store import CommonsError, Store

STATUS_COLORS = {"done": "#2e7d32", "running": "#1565c0", "claimed": "#1565c0", "ready": "#6a1b9a", "pending": "#757575",
                 "blocked": "#ef6c00", "failed": "#c62828", "cancelled": "#9e9e9e", "review": "#00838f"}


def code_graph(s: Store, project: str | None) -> dict:
    pid = s.project_id(project) if project else None
    cond, args = ("WHERE project_id=?", [pid]) if pid else ("", [])
    ents = {r["id"]: dict(r) for r in s.q(f"SELECT id, type, name, path, line FROM entities {cond}", args)}
    if not ents:
        raise CommonsError("no code entities; run `commons index <repo>` first")
    files = {e["path"]: e for e in ents.values() if e["type"] == "file"}
    sym_by_file: dict[str, list] = {}
    for e in ents.values():
        if e["type"] not in ("file", "module", "project") and e["path"] in files:
            sym_by_file.setdefault(e["path"], []).append(e)
    ids = list(ents)
    rels = []
    for i in range(0, len(ids), 400):
        ch = ids[i:i + 400]
        ph = ",".join("?" * len(ch))
        rels += [dict(r) for r in s.q(f"SELECT src, dst, type FROM relations WHERE src IN ({ph}) AND type IN ('imports','calls')", ch)]
    path_of = {k: v["path"] for k, v in ents.items()}
    agg: dict[tuple, dict] = {}
    for r in rels:
        a, b = path_of.get(r["src"]), path_of.get(r["dst"])
        if r["type"] == "imports" and ents.get(r["dst"], {}).get("type") == "module":  # import of a module entity
            mod = ents[r["dst"]]["name"].replace(".", "/")
            b = next((p for p in files if p.endswith(mod + ".py") or p.endswith(mod + "/__init__.py")), None)
        if a and b and a != b and a in files and b in files:
            e = agg.setdefault((a, b), {"from": files[a]["id"], "to": files[b]["id"], "kind": r["type"], "weight": 0})
            e["weight"] += 1
            if r["type"] == "calls":
                e["kind"] = "calls"
    src = s.q("SELECT value FROM meta WHERE key=?", (f"codesource:{pid}",)) if pid else []
    base = src[0]["value"] if src else ""  # https://github.com/owner/repo/blob/<full commit>/
    nodes = []
    for p, f in files.items():
        syms = sorted(sym_by_file.get(p, []), key=lambda e: e["line"] or 0)
        top = p.split("/")[0] if "/" in p else "."
        detail = {"path": p, "symbols": [f"{e['type']} {e['name']} :{e['line'] or ''}" for e in syms[:60]],
                  "symbol_count": len(syms)}
        if base:
            detail["source"] = base + p
            detail["links"] = [{"label": f"{e['type']} {e['name']}", "url": f"{base}{p}#L{e['line']}"}
                               for e in syms[:25] if e["line"]]
        nodes.append({"id": f["id"], "label": p.split("/")[-1], "group": top, "title": p, "detail": detail})
    commit = base.rstrip("/").rsplit("/", 1)[-1][:12] if base else None
    return {"title": f"Code graph: {project or 'all projects'}" + (f" @ {commit}" if commit else ""), "kind": "code",
            "nodes": nodes, "edges": list(agg.values()),
            "legend": "edges: imports / calls (aggregated per file); groups = top-level directory"
                      + ("; click a file for source links pinned to the indexed commit" if commit else "")}


def job_graph(s: Store, job_id: str) -> dict:
    from .breakdown import breakdown
    b = breakdown(s, job_id)
    nodes = [{"id": n["key"], "label": n["key"], "group": n["status"], "color": STATUS_COLORS.get(n["status"]),
              "title": n["title"],
              "detail": {"title": n["title"], "type": n["type"], "status": n["status"], "agent": n["agent"],
                         "model": n["model"], "effort": n["effort"], "difficulty": n["difficulty"],
                         "why": n["rationale"], "attempts": f"{n['attempts']}/{n['max_attempts']}",
                         "escalations": n["escalations"], "cost": n["cost"], "tokens": n["tokens"],
                         "result": n["result"], "artifacts": n["artifacts"]}} for n in b["nodes"]]
    edges = [{"from": e["from"], "to": e["to"], "kind": e["kind"], "weight": 1} for e in b["edges"] if e["from"]]
    j = b["job"]
    return {"title": f"Job plan: {j['title']} ({j['status']})", "kind": "job", "nodes": nodes, "edges": edges,
            "legend": f"{b['planning']['summary']} - cost ${b['totals']['cost']}, {b['totals']['tokens']} tokens; "
                      "edges: dep / review"}


def render(graph: dict) -> str:
    data = json.dumps(graph, default=str).replace("</", "<\\/")
    return TEMPLATE.replace("__TITLE__", html.escape(graph["title"])).replace("__DATA__", data)


def code_diagram(s: Store, project: str | None) -> str:
    return render(code_graph(s, project))


def job_diagram(s: Store, job_id: str) -> str:
    return render(job_graph(s, job_id))


TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;font:13px system-ui,sans-serif;background:#0f1115;color:#e6e6e6;display:flex;height:100vh}
#main{flex:1;position:relative;overflow:hidden}#side{width:340px;border-left:1px solid #2a2d35;padding:12px;overflow:auto;background:#151820}
header{position:absolute;top:0;left:0;right:0;padding:8px 12px;background:#151820cc;display:flex;gap:8px;align-items:center;z-index:2}
header h1{font-size:14px;margin:0 12px 0 0;font-weight:600}input,button{background:#22252d;color:#eee;border:1px solid #333;border-radius:6px;padding:4px 8px}
svg{width:100%;height:100%;cursor:grab}.n rect{rx:6;stroke:#0008;stroke-width:1}.n text{fill:#fff;font-size:11px;pointer-events:none}
.e{fill:none;stroke:#5a6070;stroke-opacity:.55}.e.calls{stroke:#6c8cff}.e.review{stroke:#00bcd4;stroke-dasharray:4 3}
.dim{opacity:.12}.hl rect{stroke:#ffd54f;stroke-width:3}.e.hl{stroke:#ffd54f;stroke-opacity:1}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:#0f1115;padding:8px;border-radius:6px}
footer{position:absolute;bottom:6px;left:12px;color:#888;font-size:11px}a{color:#8ab4ff}
</style></head><body><div id="main"><header><h1>__TITLE__</h1>
<input id="q" placeholder="search…"><button id="up">upstream</button><button id="down">downstream</button>
<button id="clr">clear</button><button id="fit">fit</button><button id="svgb">SVG</button><button id="pngb">PNG</button></header>
<svg id="g"><g id="vp"></g></svg>
<footer><span id="legend"></span> · generated by Commons · diagram style inspired by <a href="https://github.com/tt-a1i/archify">Archify</a> (MIT)</footer></div>
<div id="side"><b>Click a node</b><div id="det"></div></div>
<script>
const G=__DATA__;document.getElementById('legend').textContent=G.legend||'';
const N=G.nodes,E=G.edges,byId={};N.forEach(n=>byId[n.id]=n);
const out={},inn={};N.forEach(n=>{out[n.id]=[];inn[n.id]=[]});
E.forEach(e=>{if(byId[e.from]&&byId[e.to]){out[e.from].push(e.to);inn[e.to].push(e.from)}});
// layering: longest path on a DFS-acyclic subgraph
const st={},order=[],back=new Set();
function dfs(v){st[v]=1;for(const w of out[v]){if(st[w]==1)back.add(v+'>'+w);else if(!st[w])dfs(w)}st[v]=2;order.push(v)}
N.forEach(n=>{if(!st[n.id])dfs(n.id)});const layer={};
order.reverse().forEach(v=>{layer[v]=layer[v]||0;for(const w of out[v]){if(back.has(v+'>'+w))continue;layer[w]=Math.max(layer[w]||0,layer[v]+1)}});
const L={};N.forEach(n=>{(L[layer[n.id]]=L[layer[n.id]]||[]).push(n.id)});
const ks=Object.keys(L).map(Number).sort((a,b)=>a-b);
// barycentre ordering sweeps
const pos={};ks.forEach(k=>L[k].forEach((v,i)=>pos[v]=i));
for(let it=0;it<4;it++)ks.forEach(k=>{L[k].sort((a,b)=>bc(a)-bc(b));L[k].forEach((v,i)=>pos[v]=i)});
function bc(v){const p=inn[v].concat(out[v]);return p.length?p.reduce((s,w)=>s+pos[w],0)/p.length:pos[v]}
const W=170,H=34,GX=60,GY=70,XY={};let maxw=0;
ks.forEach(k=>{maxw=Math.max(maxw,L[k].length)});
ks.forEach(k=>{const row=L[k],off=(maxw-row.length)*(W+GX)/2;row.forEach((v,i)=>XY[v]=[off+i*(W+GX),k*(H+GY)+60])});
const pal=['#3949ab','#00897b','#8e24aa','#f4511e','#6d4c41','#039be5','#7cb342','#d81b60','#5e35b1','#fb8c00'],gc={};
function col(n){if(n.color)return n.color;if(!(n.group in gc))gc[n.group]=pal[Object.keys(gc).length%pal.length];return gc[n.group]}
const NS='http://www.w3.org/2000/svg',vp=document.getElementById('vp');
const eEls=E.filter(e=>XY[e.from]&&XY[e.to]).map(e=>{const p=document.createElementNS(NS,'path');const[a,b]=[XY[e.from],XY[e.to]];
 const x1=a[0]+W/2,y1=a[1]+H,x2=b[0]+W/2,y2=b[1],my=(y1+y2)/2;p.setAttribute('d',`M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`);
 p.setAttribute('class','e '+(e.kind||''));p.setAttribute('stroke-width',Math.min(6,1+Math.log2(e.weight||1)));p.dataset.f=e.from;p.dataset.t=e.to;vp.appendChild(p);
 if(e.label){const t=document.createElementNS(NS,'text');t.setAttribute('x',(x1+x2)/2+6);t.setAttribute('y',my);t.setAttribute('fill','#c8ccd6');t.setAttribute('font-size','11');t.textContent=e.label;vp.appendChild(t)}return p});
const nEls={};N.forEach(n=>{const g=document.createElementNS(NS,'g');g.setAttribute('class','n');const[x,y]=XY[n.id];g.setAttribute('transform',`translate(${x},${y})`);
 const r=document.createElementNS(NS,'rect');r.setAttribute('width',W);r.setAttribute('height',H);r.setAttribute('fill',col(n));g.appendChild(r);
 const t=document.createElementNS(NS,'text');t.setAttribute('x',8);t.setAttribute('y',21);t.textContent=(n.label||n.id).slice(0,26);g.appendChild(t);
 const ti=document.createElementNS(NS,'title');ti.textContent=n.title||n.label;g.appendChild(ti);
 g.onclick=ev=>{ev.stopPropagation();sel(n.id)};vp.appendChild(g);nEls[n.id]=g});
let cur=null;function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function sel(id){cur=id;const n=byId[id],d=Object.assign({},n.detail||{}),lk=d.links||[],src=d.source;delete d.links;delete d.source;
 document.getElementById('det').innerHTML=`<h3>${esc(n.title||n.label)}</h3>${src?`<p><a href="${esc(src)}" target="_blank" rel="noopener">source at this commit</a></p>`:''}${lk.length?'<p>'+lk.map(l=>`<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)}</a>`).join('<br>')+'</p>':''}<pre>${esc(JSON.stringify(d,null,2))}</pre>
 <p>in: ${inn[id].length} · out: ${out[id].length}</p>`;trace(new Set([id]))}
function reach(id,adj){const s=new Set([id]),q=[id];while(q.length){const v=q.pop();for(const w of adj[v])if(!s.has(w)){s.add(w);q.push(w)}}return s}
function trace(s){Object.entries(nEls).forEach(([k,g])=>{g.classList.toggle('dim',!s.has(k));g.classList.toggle('hl',k==cur)});
 eEls.forEach(p=>{const on=s.has(p.dataset.f)&&s.has(p.dataset.t);p.classList.toggle('dim',!on);p.classList.toggle('hl',on&&s.size>1)})}
function clear(){cur=null;Object.values(nEls).forEach(g=>g.classList.remove('dim','hl'));eEls.forEach(p=>p.classList.remove('dim','hl'))}
document.getElementById('up').onclick=()=>cur&&trace(reach(cur,inn));document.getElementById('down').onclick=()=>cur&&trace(reach(cur,out));
document.getElementById('clr').onclick=clear;
document.getElementById('q').oninput=ev=>{const q=ev.target.value.toLowerCase();if(!q)return clear();
 trace(new Set(N.filter(n=>(n.label+' '+(n.title||'')+' '+JSON.stringify(n.detail||{})).toLowerCase().includes(q)).map(n=>n.id)))};
// pan / zoom
const svg=document.getElementById('g');let tx=20,ty=20,sc=1,drag=null;function ap(){vp.setAttribute('transform',`translate(${tx},${ty}) scale(${sc})`)}
svg.onwheel=ev=>{ev.preventDefault();const f=ev.deltaY<0?1.1:0.9,r=svg.getBoundingClientRect(),mx=ev.clientX-r.left,my=ev.clientY-r.top;tx=mx-(mx-tx)*f;ty=my-(my-ty)*f;sc*=f;ap()};
svg.onmousedown=ev=>{drag=[ev.clientX-tx,ev.clientY-ty]};window.onmouseup=()=>drag=null;
window.onmousemove=ev=>{if(drag){tx=ev.clientX-drag[0];ty=ev.clientY-drag[1];ap()}};svg.onclick=()=>{};
function fit(){const b=vp.getBBox(),r=svg.getBoundingClientRect();sc=Math.min(r.width/(b.width+40),(r.height-60)/(b.height+40),1.5);tx=-b.x*sc+20;ty=-b.y*sc+50;ap()}
document.getElementById('fit').onclick=fit;fit();
function svgText(){const c=svg.cloneNode(true);c.setAttribute('xmlns',NS);const s=document.createElementNS(NS,'style');s.textContent=document.querySelector('style').textContent;c.prepend(s);return new XMLSerializer().serializeToString(c)}
function dl(u,n){const a=document.createElement('a');a.href=u;a.download=n;a.click()}
document.getElementById('svgb').onclick=()=>dl(URL.createObjectURL(new Blob([svgText()],{type:'image/svg+xml'})),'diagram.svg');
document.getElementById('pngb').onclick=()=>{const img=new Image(),r=svg.getBoundingClientRect();img.onload=()=>{const c=document.createElement('canvas');c.width=r.width*2;c.height=r.height*2;
 const x=c.getContext('2d');x.fillStyle='#0f1115';x.fillRect(0,0,c.width,c.height);x.scale(2,2);x.drawImage(img,0,0);dl(c.toDataURL('image/png'),'diagram.png')};
 img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svgText())};
</script></body></html>"""


# ------------------------------------------------------------------ sequence and lifecycle (v0.18)
def job_sequence(s: Store, job_id: str) -> dict:
    """How a job moved between agents over time: assignment, claim, result, review verdicts, messages."""
    j = s.q("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not j:
        raise CommonsError(f"unknown job {job_id}")
    j, ev = dict(j[0]), []
    lab = lambda a: s.label(a)[1] if a else "Commons"  # noqa: E731
    sub = s.label(j["submitted_by"])[1] if j.get("submitted_by") else "user"
    ev.append((j["created_at"], sub, "Commons", f"submit: {j['title'][:60]}"))
    for t in s.q("SELECT * FROM subtasks WHERE job_id=? ORDER BY created_at", (job_id,)):
        t = dict(t)
        who = lab(t["claimed_by"] or t["assigned_agent"])
        if t["assigned_agent"] or t["claimed_by"]:
            ev.append((t["created_at"] + 0.001, "Commons", who, f"assign {t['type']}: {t['title'][:50]}"))
        if t.get("started_at"):
            ev.append((t["started_at"], who, "Commons", f"claim (attempt {t['attempts']})"))
        if t["status"] in ("done", "failed", "cancelled"):
            verdict = f" VERDICT {t['verdict']}" if t.get("verdict") else ""
            target = lab(s.q("SELECT claimed_by FROM subtasks WHERE id=?", (t["gate_of"],))[0]["claimed_by"]) \
                if t.get("gate_of") and s.q("SELECT 1 FROM subtasks WHERE id=?", (t["gate_of"],)) else "Commons"
            ev.append((t["updated_at"], who, target, f"{t['status']}{verdict}: {(t['result'] or '')[:50]}"))
    for m in s.q("SELECT sender, recipient, subject, body, created_at FROM messages WHERE meta LIKE ? ORDER BY created_at",
                 (f'%{job_id}%',)):
        ev.append((m["created_at"], lab(m["sender"]), lab(m["recipient"]), (m["subject"] or m["body"])[:60]))
    ev.sort(key=lambda e: e[0])
    parts = list(dict.fromkeys([e[1] for e in ev] + [e[2] for e in ev]))
    return {"title": f"Job sequence: {j['title']} ({j['status']})", "participants": parts,
            "messages": [{"from": a, "to": b, "label": l, "ts": t} for t, a, b, l in ev]}


def code_sequence(s: Store, symbol: str, project: str | None = None, depth: int = 4) -> dict:
    """The call path from one function through the code index, as a sequence between files."""
    pid = s.project_id(project) if project else None
    cond = " AND project_id=?" if pid else ""
    root = s.q(f"SELECT id, name, path FROM entities WHERE type IN ('function','method') AND (name=? OR name LIKE ?){cond} "
               "ORDER BY length(name) LIMIT 1", (symbol, f"%.{symbol}", *([pid] if pid else [])))
    if not root:
        raise CommonsError(f"no function named {symbol!r} in the code index")
    seen, msgs = {root[0]["id"]}, []
    file_of = lambda e: (e["path"] or "?").split("/")[-1]  # noqa: E731

    def walk(eid, d):
        if d >= depth or len(msgs) > 60:
            return
        me = dict(s.q("SELECT id, name, path FROM entities WHERE id=?", (eid,))[0])
        for r in s.q("SELECT e.id, e.name, e.path FROM relations x JOIN entities e ON e.id=x.dst WHERE x.src=? AND "
                     "x.type='calls' ORDER BY e.path, e.line", (eid,)):
            msgs.append({"from": file_of(me), "to": file_of(r), "label": f"{r['name']}()"})
            if r["id"] not in seen:
                seen.add(r["id"])
                walk(r["id"], d + 1)
    walk(root[0]["id"], 0)
    parts = list(dict.fromkeys([file_of(root[0])] + [m["from"] for m in msgs] + [m["to"] for m in msgs]))
    return {"title": f"Call sequence: {root[0]['name']}()", "participants": parts, "messages": msgs}


def lifecycle(s: Store, kind: str = "subtask") -> dict:
    """The real state machine of work items, with how often each transition happened (from history)."""
    counts: dict[tuple, int] = {}
    add = lambda a, b, n=1: counts.__setitem__((a, b), counts.get((a, b), 0) + n)  # noqa: E731
    if kind == "subtask":
        for r in s.q("SELECT status, attempts, started_at FROM subtasks"):
            claimed = bool(r["started_at"]) or r["status"] in ("claimed", "done", "failed")
            if claimed:
                add("pending", "claimed")
            if r["status"] in ("done", "failed"):
                add("claimed", r["status"])
            elif r["status"] == "cancelled":
                add("claimed" if claimed else "pending", "cancelled")
            if (r["attempts"] or 0) > 1:
                add("failed", "pending", r["attempts"] - 1)
        for action, frm, to in (("subtask.pause", "claimed", "paused"), ("subtask.timeout", "claimed", "pending"),
                                ("subtask.wait_quota", "pending", "waiting for quota")):
            n = s.q("SELECT COUNT(*) n FROM audit WHERE action=?", (action,))[0]["n"]
            if n:
                add(frm, to, n)
        if counts.get(("claimed", "paused")):
            add("paused", "pending", counts[("claimed", "paused")])
        if counts.get(("pending", "waiting for quota")):
            add("waiting for quota", "pending", counts[("pending", "waiting for quota")])
    elif kind == "board":
        last: dict[str, str] = {}
        for r in s.q("SELECT action, target, detail FROM audit WHERE action IN ('board.add','board.update') ORDER BY ts"):
            if r["action"] == "board.add":
                last[r["target"]] = "open"
                continue
            st = (json.loads(r["detail"] or "{}") or {}).get("status")
            if st and r["target"] in last and st != last[r["target"]]:
                add(last[r["target"]], st)
                last[r["target"]] = st
    else:
        raise CommonsError("lifecycle kind is subtask or board")
    if not counts:
        raise CommonsError(f"no {kind} history yet")
    states = sorted({x for k in counts for x in k})
    return {"title": f"Lifecycle: {kind}s (transitions seen in history)", "kind": "lifecycle",
            "nodes": [{"id": st, "label": st, "group": st, "color": STATUS_COLORS.get(st), "title": st,
                       "detail": {"in": sum(v for (a, b), v in counts.items() if b == st),
                                  "out": sum(v for (a, b), v in counts.items() if a == st)}} for st in states],
            "edges": [{"from": a, "to": b, "kind": "transition", "weight": n, "label": str(n)} for (a, b), n in counts.items()],
            "legend": "states and how many times each transition happened"}


def render_sequence(seq: dict) -> str:
    data = json.dumps(seq, default=str).replace("</", "<\\/")
    return SEQ_TEMPLATE.replace("__TITLE__", html.escape(seq["title"])).replace("__DATA__", data)


SEQ_TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
body{margin:0;font:13px system-ui,sans-serif;background:#0f1115;color:#e6e6e6}header{padding:10px 14px;background:#151820;
position:sticky;top:0;display:flex;gap:10px;align-items:center}h1{font-size:14px;margin:0}input,button{background:#22252d;color:#eee;
border:1px solid #333;border-radius:6px;padding:4px 8px}.wrap{overflow:auto}svg text{fill:#e6e6e6;font-size:12px}
.life{stroke:#3a3f4b;stroke-dasharray:4 4}.box{fill:#3949ab}.msg{stroke:#8ab4ff;fill:none}.self{stroke:#ffb74d;fill:none}
.dim{opacity:.15}footer{padding:8px 14px;color:#888;font-size:11px}</style></head><body>
<header><h1>__TITLE__</h1><input id="q" placeholder="filter messages…"><button id="svgb">SVG</button></header>
<div class="wrap"><svg id="s"></svg></div><footer>generated by Commons · sequence diagram</footer><script>
const D=__DATA__,NS='http://www.w3.org/2000/svg',P=D.participants,M=D.messages,CW=190,TOP=60,RH=34,svg=document.getElementById('s');
const X={};P.forEach((p,i)=>X[p]=80+i*CW);const W=80+P.length*CW,H=TOP+40+M.length*RH+40;svg.setAttribute('width',W);svg.setAttribute('height',H);
svg.innerHTML='<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0L10,5L0,10z" fill="#8ab4ff"/></marker></defs>';
const el=(t,a,txt)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(txt!=null)e.textContent=txt;svg.appendChild(e);return e};
P.forEach(p=>{el('line',{x1:X[p],y1:TOP+28,x2:X[p],y2:H-20,class:'life'});el('rect',{x:X[p]-75,y:TOP-6,width:150,height:30,rx:6,class:'box'});
 el('text',{x:X[p],y:TOP+14,'text-anchor':'middle'},p.slice(0,22))});
const rows=M.map((m,i)=>{const y=TOP+56+i*RH,g=[];if(m.from===m.to){g.push(el('path',{d:`M${X[m.from]},${y} h40 v14 h-40`,class:'self','marker-end':'url(#a)'}))}
 else{g.push(el('line',{x1:X[m.from],y1:y,x2:X[m.to],y2:y,class:'msg','marker-end':'url(#a)'}))}
 const lx=Math.min(X[m.from],X[m.to])+8;g.push(el('text',{x:m.from===m.to?X[m.from]+46:lx,y:y-6},(i+1)+'. '+m.label));return g});
document.getElementById('q').oninput=e=>{const q=e.target.value.toLowerCase();M.forEach((m,i)=>rows[i].forEach(n=>n.classList.toggle('dim',!!q&&!(m.label+' '+m.from+' '+m.to).toLowerCase().includes(q))))};
document.getElementById('svgb').onclick=()=>{const c=svg.cloneNode(true);c.setAttribute('xmlns',NS);const a=document.createElement('a');
 a.href=URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(c)],{type:'image/svg+xml'}));a.download='sequence.svg';a.click()};
</script></body></html>"""


# ------------------------------------------------------------------ architecture delta (v0.9)
def snapshot(s: Store, project: str, keep: int = 10) -> dict:
    """Record the file-level dependency graph after an index run so `delta` can show how the architecture moved."""
    from .ids import now
    pid = s.find_project(project)
    g = code_graph(s, project)
    path_of = {n["id"]: n["title"] for n in g["nodes"]}
    snap = {"ts": now(), "files": sorted(path_of.values()),
            "edges": sorted({(path_of[e["from"]], path_of[e["to"]], e["kind"]) for e in g["edges"]
                             if e["from"] in path_of and e["to"] in path_of})}
    c = s.q("SELECT value FROM meta WHERE key=?", (f"codecommit:{pid}",))
    snap["commit"] = c[0]["value"] if c else None
    r = s.q("SELECT value FROM meta WHERE key=?", (f"codesnap:{pid}",))
    snaps = (json.loads(r[0]["value"]) if r else []) + [snap]
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"codesnap:{pid}", json.dumps(snaps[-keep:])))
    return {"files": len(snap["files"]), "edges": len(snap["edges"]), "snapshots": len(snaps[-keep:])}


def delta_graph(s: Store, project: str, frm: str = "", to: str = "") -> dict:
    """What changed between two index runs (default: the last two): added/removed files and dependencies. frm / to
    pick snapshots by position (-1 = latest, 0 = oldest kept) or by commit prefix."""
    pid = s.find_project(project)
    r = s.q("SELECT value FROM meta WHERE key=?", (f"codesnap:{pid}",)) if pid else []
    snaps = json.loads(r[0]["value"]) if r else []
    if len(snaps) < 2:
        raise CommonsError("need two index runs of this project to diff (index again after changes)")

    def pick(ref, default):
        if not ref:
            return snaps[default]
        if re.fullmatch(r"-?\d+", ref):
            return snaps[int(ref)]
        hit = [x for x in snaps if (x.get("commit") or "").startswith(ref)]
        if not hit:
            raise CommonsError(f"no snapshot at commit {ref}; kept: {[x.get('commit') for x in snaps]}")
        return hit[-1]
    a, b = pick(frm, -2), pick(to, -1)
    fa, fb = set(a["files"]), set(b["files"])
    ea, eb = {tuple(e[:2]) for e in a["edges"]}, {tuple(e[:2]) for e in b["edges"]}
    nodes = {}
    for f in fa | fb:
        t = "added file" if f not in fa else "removed file" if f not in fb else "file"
        nodes[f] = {"type": t, "name": f.split("/")[-1], "path": f}
    edges = [{"src": x, "dst": y, "type": "added dependency" if (x, y) not in ea else "removed dependency"
              if (x, y) not in eb else "dependency"} for x, y in ea | eb]
    changed = {f for f, d in nodes.items() if d["type"] != "file"} | \
        {e["src"] for e in edges if e["type"] != "dependency"} | {e["dst"] for e in edges if e["type"] != "dependency"}
    near = {e["src"] for e in edges if e["dst"] in changed} | {e["dst"] for e in edges if e["src"] in changed}
    show = changed | near if changed else set(nodes)  # focus on what moved and its direct neighbours
    return {"nodes": {k: v for k, v in nodes.items() if k in show},
            "edges": [e for e in edges if e["src"] in show and e["dst"] in show],
            "scope": f"delta {a.get('commit') or ''}..{b.get('commit') or ''}".strip(),
            "summary": {"files_added": len(fb - fa), "files_removed": len(fa - fb),
                        "deps_added": len(eb - ea), "deps_removed": len(ea - eb)}}


def make(s: Store, kind: str, target: str = "") -> str:
    """kind = code (target project) | workflow / job (job id) | sequence (job id, or a function name for its call path)
    | lifecycle (subtask | board) | delta (project, or project:from:to with snapshot positions or commits)."""
    if kind == "sequence":
        return render_sequence(job_sequence(s, target) if s.q("SELECT 1 FROM jobs WHERE id=?", (target,))
                               else code_sequence(s, target))
    if kind == "lifecycle":
        return render(lifecycle(s, target or "subtask"))
    if kind == "workflow":
        kind = "job"
    if kind == "delta":
        from .graphview import render as gv_render
        project, _, rng = target.partition(":")
        frm, _, to = rng.partition(":")
        target = project
        g = delta_graph(s, project, frm, to)
        sm = g["summary"]
        return gv_render(g, f"Architecture delta: {target} (+{sm['files_added']}/-{sm['files_removed']} files, "
                         f"+{sm['deps_added']}/-{sm['deps_removed']} deps)")
    return code_diagram(s, target or None) if kind == "code" else job_diagram(s, target)  # job = workflow
