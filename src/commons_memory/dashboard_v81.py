"""v0.8.1 dashboard: 'Connect an agent' page + JSON API (GET /api/connect, /api/connect_status;
POST /api/connect/key, /api/connect/render)."""
from __future__ import annotations

import threading

from .store import CommonsError

CTX = threading.local()  # per-request: host header, bind, client ip, has_auth, role (set by the handler)


def _req() -> dict:
    return getattr(CTX, "req", None) or {}


def connect(self, q, key=None):
    from . import connect as C
    r = _req()
    url = q.get("url") or C.detect_url(r.get("host", ""))
    gate, why = C.key_creation_allowed(r.get("bind", "127.0.0.1"), r.get("client", ""), r.get("has_auth", False),
                                       r.get("role", "admin"))
    return C.bundle(q.get("platform") or "claude-code", url, q.get("name") or "my-agent", key, q.get("project") or "") \
        | {"detected_url": C.detect_url(r.get("host", "")), "can_create_key": gate, "key_gate_reason": why}


def connect_status(self, q):
    from .connect import status
    return status(self.s, q.get("name", ""))


def join_lines(hub: str, code: str) -> dict:
    """The three ways to use a join code: a terminal line, a sentence for a bot, the connector URL for a chat app."""
    import ipaddress
    from urllib.parse import urlsplit
    hub = hub.rstrip("/").removesuffix("/mcp")
    u = urlsplit(hub)
    try:
        ip = ipaddress.ip_address(u.hostname or "")
        trusted = ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")  # this machine, or the tailnet
    except ValueError:
        trusted = (u.hostname or "").endswith((".ts.net", "localhost"))
    return {"terminal": f"curl -fsSL {hub}/join/{code} | sh",
            "bot": f"Join my Commons: read {hub}/join/{code}/agent and follow it.",
            "connector": f"{hub}/mcp",
            "plain_http": u.scheme == "http" and not trusted}


def connect_join(self, q):
    """Who has joined with a code, and whether Commons can wake each one."""
    from .join import status
    return status(self.s, q.get("code", ""))


def post(self, path, body):
    if not path.startswith("/api/connect/"):
        return NotImplemented
    from . import connect as C
    r = _req()
    ok, why = C.key_creation_allowed(r.get("bind", "127.0.0.1"), r.get("client", ""), r.get("has_auth", False),
                                     r.get("role", "admin"))
    if not ok:
        raise CommonsError(f"key creation not allowed here: {why}")
    url = body.get("url") or C.detect_url(r.get("host", ""))
    if path == "/api/connect/key":
        k = C.create_key(self.s, body.get("name", ""), body.get("role") or "write", body.get("project") or "",
                         body.get("platform") or "")
        return k | {"bundle": connect(self, body | {"name": k["name"], "url": url}, k["api_key"])}
    if path == "/api/connect/join":  # one link for the whole machine: no key is shown or copied
        from .join import create
        j = create(self.s, body.get("role") or "write", body.get("project") or "", hub=url)
        return j | join_lines(url, j["code"]) | {"hub": url.rstrip("/").removesuffix("/mcp")}
    if path == "/api/connect/join_close":
        from .join import close
        return close(self.s, body.get("code", ""))
    if path == "/api/connect/wake":  # an agent Commons can't wake: the sentence that makes it wakeable, keeping its key
        from .join import create
        name = body.get("name", "")
        if not self.s.get_agent(name):
            raise CommonsError(f"unknown agent {name!r}")
        j = create(self.s, body.get("role") or "write", ttl=7 * 86400, uses=0, hub=url)  # it keeps its key: nothing to claim
        hub = url.rstrip("/").removesuffix("/mcp")
        return j | {"bot": f"Make yourself wakeable by Commons: read {hub}/join/{j['code']}/agent?as={name} and follow it."}
    if path == "/api/connect/render":  # re-render with a key the browser already holds (admin only, never stored)
        return connect(self, body | {"url": url}, body.get("key") or None)
    raise CommonsError("unknown connect action")


def install(API) -> None:
    for f in (connect, connect_status, connect_join):
        setattr(API, f.__name__, f)
    orig = API.post

    def post_v81(self, path, body):
        res = post(self, path, body)
        return orig(self, path, body) if res is NotImplemented else res
    API.post = post_v81


ICON = '<path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 01-10 0zM12 16v5"/>'

JS = r"""
window.cx=window.cx||{platform:"claude-code",name:"my-agent",project:"",url:"",key:null,role:"write"};
function cxCopy(i){const t=document.getElementById("cxb"+i).innerText;(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>toast("copied ✓")).catch(()=>{const r=document.createRange();r.selectNodeContents(document.getElementById("cxb"+i));getSelection().removeAllRanges();getSelection().addRange(r);document.execCommand("copy");toast("copied ✓")})}
function cxStep(id,on){cx.skip=(cx.skip||[]).filter(k=>k!=id);if(!on)cx.skip.push(id);render()}
function cxSet(k,v){cx[k]=v;if(k!="url")cx.b=null;render()}
async function cxKey(){const name=$("#cxn").value.trim(),role=$("#cxr").value,project=$("#cxp").value.trim();if(!name){toast("name required","err");return}
const r=await postRaw("connect/key",{name,role,project,platform:cx.platform,url:cx.url});
if(r.error){toast(r.error,"err");return}Object.assign(cx,{name,role,project,key:r.api_key,b:r.bundle,watch:name});toast("key created for "+name);render();cxWatch()}
async function cxBundle(){if(cx.key){const r=await fetch("/api/connect/render",{method:"POST",headers:{"X-Commons":"1","Content-Type":"application/json"},body:JSON.stringify({platform:cx.platform,name:cx.name,project:cx.project,url:cx.url,key:cx.key})}).then(r=>r.json());if(!r.error)return r}
return api(`connect?platform=${encodeURIComponent(cx.platform)}&name=${encodeURIComponent(cx.name)}&project=${encodeURIComponent(cx.project)}&url=${encodeURIComponent(cx.url)}`)}
function cxWatch(){clearInterval(window.cxT);window.cxT=setInterval(async()=>{if(tab!="Connect"||!cx.watch){clearInterval(window.cxT);return}const s=await api("connect_status?name="+encodeURIComponent(cx.watch));const el=$("#cxs");if(el)el.innerHTML=cxSeen(s)},3000)}
const cxSeen=s=>!s.exists?'<span class="mut">no such agent yet</span>':s.last_seen?`<span class="dot ${s.connected?"live":"idle"}"></span><b>${E(s.name)}</b> last seen ${ago(s.seconds_ago)} ago ${s.connected?pill("connected"):""}`:`<span class="dot offline"></span><b>${E(s.name)}</b> <span class="mut">never seen</span>`;
V["Connect"]=async function(){const b=await cxBundle();if(!cx.url)cx.url=b.detected_url;const keyBox=cx.key?`<div class="card" style="border-color:var(--warn)"><h3>🔑 Key for ${E(cx.name)} <span class="mut">(role ${E(cx.role)}; shown once, only its hash is stored)</span></h3><pre id="cxkey">${E(cx.key)}</pre><button onclick="navigator.clipboard.writeText(cx.key).then(()=>toast('copied ✓'))">copy key</button> <button onclick="cx.key=null;cx.b=null;render()">hide &amp; forget</button><p class="mut">It is already filled into every block below.</p></div>`:"";
const st=cx.watch?await api("connect_status?name="+encodeURIComponent(cx.watch)):null;if(cx.watch)cxWatch();
return `<div class="card"><h3>Connect an agent</h3><p class="mut">Pick the AI, create its key, then copy one command. Every step is on by default.</p>
<p><label>MCP URL <input id="cxu" size="40" value="${E(cx.url)}" onchange="cxSet('url',this.value)"></label> <span class="mut">detected: <code>${E(b.detected_url)}</code></span> <button onclick="cxSet('url','')">reset</button></p>
<div class="seg" role="tablist">${Object.entries(b.platforms).map(([k,l])=>`<button role="tab" class="${k==cx.platform?"on":""}" aria-selected="${k==cx.platform}" onclick="cxSet('platform',${J(k)})">${E(l)}</button>`).join(" ")}</div><p class="mut">${E(b.hint)}</p></div>
<div class="card"><h3>Agent key</h3>${b.can_create_key?`<p><label>name <input id="cxn" value="${E(cx.name)}" size="16"></label> <label>role <select id="cxr">${["read","write","admin"].map(r=>`<option ${r==cx.role?"selected":""}>${r}</option>`).join("")}</select></label> <label>project <input id="cxp" value="${E(cx.project)}" size="14" placeholder="optional"></label> <button class="pri" onclick="cxKey()">Create agent key</button></p>`:`<p class="mut">Key creation is disabled on this dashboard: ${E(b.key_gate_reason)}. Use <code>commons connect ${E(cx.platform)} --name NAME --new-key</code> on the server.</p>`}
${st?`<p id="cxs">${cxSeen(st)}</p>`:'<p class="mut" id="cxs">Create a key to watch the agent connect live.</p>'}</div>${keyBox}
${b.blocks.map((x,i)=>x.kind=="setup"?`<div class="card"><h3>${E(x.title)} <button class="pri" onclick="cxCopy(${i})">copy</button></h3><div class="cxsteps">${x.steps.map(st=>`<label class="cxstep"><input type="checkbox" ${(cx.skip||[]).includes(st.id)?"":"checked"} onchange="cxStep(${J(st.id)},this.checked)"> ${E(st.label)} <span class="pill done">recommended</span></label>`).join("")}</div><pre id="cxb${i}">${E(x.base+(cx.skip||[]).map(k=>" --no-"+k).join(""))}</pre><p class="mut sm">Run it on the machine where the AI runs. It saves the key once (hidden prompt, 0600 file), then does every ticked step and prints what it did.${cx.key?" Paste the key above when it asks.":""}</p></div>`
:x.collapsed?`<details class="card"><summary><b>${E(x.title)}</b></summary><p><button onclick="cxCopy(${i})">copy</button></p><pre id="cxb${i}">${E(x.text)}</pre></details>`
:`<div class="card"><h3>${E(x.title)} <button onclick="cxCopy(${i})">copy</button></h3><pre id="cxb${i}">${E(x.text)}</pre></div>`).join("")}
<div class="card"><h3>Troubleshooting</h3><ul>${b.troubleshooting.map(t=>`<li style="margin:6px 0"><label><input type="checkbox"> <b>${E(t.symptom)}</b></label> <span class="mut">— ${E(t.fix)}</span></li>`).join("")}</ul>
<p class="mut">CLI equivalent: <code>commons connect ${E(cx.platform)} --name ${E(cx.name)} --new-key</code> · JSON: <code>GET /api/connect?platform=${E(cx.platform)}</code></p></div>`};
"""
