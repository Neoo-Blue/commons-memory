"""Interactive, self-contained HTML diagrams of the code graph and job plans (v0.8).

Inspired by Archify (https://github.com/tt-a1i/archify, MIT): source-backed architecture diagrams as standalone
HTML artifacts. This is an independent implementation (no Archify code): a layered DAG layout in vanilla JS/SVG,
no CDN or network access, with search, click-to-inspect details, upstream/downstream reach tracing, pan/zoom and
SVG/PNG export.
"""
from __future__ import annotations

import html
import json

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
    nodes = []
    for p, f in files.items():
        syms = sorted(sym_by_file.get(p, []), key=lambda e: e["line"] or 0)
        top = p.split("/")[0] if "/" in p else "."
        nodes.append({"id": f["id"], "label": p.split("/")[-1], "group": top, "title": p,
                      "detail": {"path": p, "symbols": [f"{e['type']} {e['name']} :{e['line'] or ''}" for e in syms[:60]],
                                 "symbol_count": len(syms)}})
    return {"title": f"Code graph: {project or 'all projects'}", "kind": "code", "nodes": nodes, "edges": list(agg.values()),
            "legend": "edges: imports / calls (aggregated per file); groups = top-level directory"}


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
 p.setAttribute('class','e '+(e.kind||''));p.setAttribute('stroke-width',Math.min(6,1+Math.log2(e.weight||1)));p.dataset.f=e.from;p.dataset.t=e.to;vp.appendChild(p);return p});
const nEls={};N.forEach(n=>{const g=document.createElementNS(NS,'g');g.setAttribute('class','n');const[x,y]=XY[n.id];g.setAttribute('transform',`translate(${x},${y})`);
 const r=document.createElementNS(NS,'rect');r.setAttribute('width',W);r.setAttribute('height',H);r.setAttribute('fill',col(n));g.appendChild(r);
 const t=document.createElementNS(NS,'text');t.setAttribute('x',8);t.setAttribute('y',21);t.textContent=(n.label||n.id).slice(0,26);g.appendChild(t);
 const ti=document.createElementNS(NS,'title');ti.textContent=n.title||n.label;g.appendChild(ti);
 g.onclick=ev=>{ev.stopPropagation();sel(n.id)};vp.appendChild(g);nEls[n.id]=g});
let cur=null;function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function sel(id){cur=id;const n=byId[id];document.getElementById('det').innerHTML=`<h3>${esc(n.title||n.label)}</h3><pre>${esc(JSON.stringify(n.detail||{},null,2))}</pre>
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
