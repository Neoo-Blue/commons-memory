"""Export the graph to Mermaid, GraphML, JSON and a self-contained static HTML viewer."""
from __future__ import annotations

import html
import json
from xml.sax.saxutils import escape


def to_mermaid(g: dict, max_nodes: int = 300) -> str:
    ids = list(g["nodes"])[:max_nodes]
    alias = {n: f"n{i}" for i, n in enumerate(ids)}
    lines = ["graph LR"]
    for n in ids:
        d = g["nodes"][n]
        label = f"{d['type']}: {d['name']}".replace('"', "'")[:60]
        lines.append(f'  {alias[n]}["{label}"]')
    for e in g["edges"]:
        if e["src"] in alias and e["dst"] in alias:
            lines.append(f"  {alias[e['src']]} -->|{e['type']}| {alias[e['dst']]}")
    return "\n".join(lines) + "\n"


def to_graphml(g: dict) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
           '<key id="type" for="node" attr.name="type" attr.type="string"/>',
           '<key id="name" for="node" attr.name="name" attr.type="string"/>',
           '<key id="rel" for="edge" attr.name="type" attr.type="string"/>',
           '<graph id="commons" edgedefault="directed">']
    for n, d in g["nodes"].items():
        out.append(f'<node id="{escape(n)}"><data key="type">{escape(d["type"])}</data>'
                   f'<data key="name">{escape(str(d["name"]))}</data></node>')
    for i, e in enumerate(g["edges"]):
        out.append(f'<edge id="e{i}" source="{escape(e["src"])}" target="{escape(e["dst"])}">'
                   f'<data key="rel">{escape(e["type"])}</data></edge>')
    out += ["</graph>", "</graphml>"]
    return "\n".join(out) + "\n"


HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Commons graph</title>
<style>body{margin:0;font:12px system-ui;background:#0f1117;color:#ddd}#c{display:block}
#p{position:fixed;top:8px;left:8px;background:#1b1e27cc;padding:8px;border-radius:6px;max-width:340px}
input{width:200px}</style></head><body><div id="p"><b>Commons graph</b> — <span id="s"></span><br>
<input id="q" placeholder="filter by name…"> <small>drag to pan, wheel to zoom, click node</small><div id="i"></div></div>
<canvas id="c"></canvas><script>
const G=__DATA__;const C=document.getElementById('c'),X=C.getContext('2d');
const col={project:'#f6c344',file:'#5aa9e6',function:'#7bd389',method:'#a1e3a1',class:'#e07a5f',module:'#9a8c98',
memory:'#c77dff',decision:'#ff6b6b',fact:'#ffd6a5',handoff:'#4cc9f0',checkpoint:'#90e0ef',agent:'#f72585',session:'#b5179e',concept:'#bdb2ff',task:'#ffadad'};
const N=Object.entries(G.nodes).map(([id,d],i)=>({id,...d,x:Math.cos(i)*300*Math.random(),y:Math.sin(i)*300*Math.random(),vx:0,vy:0}));
const M=Object.fromEntries(N.map(n=>[n.id,n]));const E=G.edges.filter(e=>M[e.src]&&M[e.dst]);
document.getElementById('s').textContent=N.length+' nodes, '+E.length+' edges';
let ox=0,oy=0,sc=1,q='',sel=null;function rs(){C.width=innerWidth;C.height=innerHeight}rs();onresize=rs;
function step(){const k=40;for(let a of N){for(let b of N){if(a===b)continue;let dx=a.x-b.x,dy=a.y-b.y,d=dx*dx+dy*dy+.01;if(d<90000){let f=k*k/d;a.vx+=dx*f*.01;a.vy+=dy*f*.01}}}
for(let e of E){let a=M[e.src],b=M[e.dst],dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)+.01,f=(d-k)*.01;a.vx+=dx/d*f*d*.05;a.vy+=dy/d*f*d*.05;b.vx-=dx/d*f*d*.05;b.vy-=dy/d*f*d*.05}
for(let n of N){n.vx-=n.x*.001;n.vy-=n.y*.001;n.x+=Math.max(-10,Math.min(10,n.vx));n.y+=Math.max(-10,Math.min(10,n.vy));n.vx*=.6;n.vy*=.6}}
function draw(){X.setTransform(1,0,0,1,0,0);X.clearRect(0,0,C.width,C.height);X.setTransform(sc,0,0,sc,C.width/2+ox,C.height/2+oy);
X.strokeStyle='#ffffff22';for(let e of E){let a=M[e.src],b=M[e.dst];X.beginPath();X.moveTo(a.x,a.y);X.lineTo(b.x,b.y);X.stroke()}
for(let n of N){let hit=q&&String(n.name).toLowerCase().includes(q);X.fillStyle=col[n.type]||'#888';X.beginPath();X.arc(n.x,n.y,hit||n===sel?7:4,0,7);X.fill();
if(hit||n===sel||sc>1.6){X.fillStyle='#eee';X.fillText(n.name,n.x+6,n.y+3)}}}
let it=0;(function loop(){if(it++<400)step();draw();requestAnimationFrame(loop)})();
let drag=null;C.onmousedown=e=>drag=[e.clientX-ox,e.clientY-oy];onmouseup=()=>drag=null;onmousemove=e=>{if(drag){ox=e.clientX-drag[0];oy=e.clientY-drag[1]}};
C.onwheel=e=>{sc*=e.deltaY<0?1.1:.9;e.preventDefault()};
C.onclick=e=>{const x=(e.clientX-C.width/2-ox)/sc,y=(e.clientY-C.height/2-oy)/sc;sel=N.find(n=>(n.x-x)**2+(n.y-y)**2<64)||null;
document.getElementById('i').innerHTML=sel?'<hr><b>'+esc(sel.name)+'</b><br>'+sel.type+'<br><code>'+sel.id+'</code><br>'+E.filter(e=>e.src===sel.id||e.dst===sel.id).slice(0,20).map(e=>e.src===sel.id?'→ '+e.type+' '+esc(M[e.dst].name):'← '+e.type+' '+esc(M[e.src].name)).join('<br>'):''};
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
document.getElementById('q').oninput=e=>q=e.target.value.toLowerCase();
</script></body></html>"""


def to_html(g: dict) -> str:
    data = json.dumps(g).replace("</", "<\\/")
    return HTML.replace("__DATA__", data)


def export(g: dict, fmt: str) -> str:
    fmt = fmt.lower()
    if fmt == "mermaid":
        return to_mermaid(g)
    if fmt == "graphml":
        return to_graphml(g)
    if fmt == "html":
        return to_html(g)
    if fmt == "json":
        return json.dumps(g, indent=1)
    raise ValueError("format must be mermaid|graphml|html|json")


__all__ = ["export", "to_mermaid", "to_graphml", "to_html", "html"]
