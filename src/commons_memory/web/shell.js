// The app shell: five workspaces instead of 32 flat pages, breadcrumbs and live hub status in the header, a "More"
// menu instead of a permanent kill switch, and on phones a bottom bar plus swipeable sub-tabs. Loaded after legacy.js
// and kit.js; it replaces legacy tabs() and owns #tabs, #crumbs, #hstat, #subtabs and #bnav.
function wsList(){return [
  ["Home",'<path d="M3.5 11.5 12 4l8.5 7.5"/><path d="M5.5 10v9.5h13V10"/>',["Overview","Board"]],
  ["Projects",'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>',["Projects","Tasks"]],
  ["Work",'<rect x="3.5" y="5" width="17" height="14" rx="2.5"/><path d="M8 5V3.5h8V5M3.5 11h17"/>',["Workflow","Orchestration","Task breakdown","Mission control","Changes","Sessions","Drive","Routing"]],
  ["Agents",'<circle cx="9" cy="8.5" r="3.2"/><path d="M3 19.5c.7-3.3 3.1-5 6-5s5.3 1.7 6 5"/><path d="M16 5.6a3.2 3.2 0 0 1 0 5.8M18.6 14.7c1.4.8 2.2 2.4 2.5 4.8"/>',["Agents","Presence","Capacity","Usage","Models","Connect","Skills","Discover","MCP hub"]],
  ["Knowledge",'<path d="M5 4.5h10.5L19 8v11.5H5z"/><path d="M15 4.5V8h4M8.5 12h7M8.5 15.5h5"/>',["Memory","Knowledge","Infrastructure","Context","Graph","Preferences","Org chart","Librarian","Import","Retro learning"]],
  ["System",'<circle cx="12" cy="12" r="3"/><path d="M12 3.5v2.2M12 18.3v2.2M3.5 12h2.2M18.3 12h2.2M6 6l1.6 1.6M16.4 16.4 18 18M6 18l1.6-1.6M16.4 7.6 18 6"/>',["Setup","Secrets","Recurring","System"]],
];}
// function declarations, not consts: legacy.js renders once before this part of the script has run
function wsOf(t){const ws=wsList();return ws.find(w=>w[2].includes(t))||ws[0]}
function shellOpenState(){if(!window._cws){try{window._cws=JSON.parse(localStorage.cws||"{}")}catch(e){window._cws={}}}return window._cws}
function shellToggle(name){const o=shellOpenState();o[name]=!o[name];try{localStorage.cws=JSON.stringify(o)}catch(e){}tabs()}
function shellLink(t){const on=t==tab;return `<a href="#${encodeURI(t)}" class="${on?"on":""}" ${on?'aria-current="page"':""} onclick="go(${J(t)});return false"><svg viewBox="0 0 24 24" aria-hidden="true">${ICON[t]||""}</svg><span>${E(t)}</span></a>`}
function tabs(){
  const cur=wsOf(tab);
  const WS=wsList(),opened=shellOpenState();
  $("#tabs").innerHTML=WS.map(([name,ico,pages])=>{const open=(name in opened)?opened[name]:name==cur[0];return `<div class="ws ${name==cur[0]?"cur":""}"><button class="ws-h" aria-expanded="${open}" onclick="shellToggle(${J(name)})"><svg viewBox="0 0 24 24" aria-hidden="true">${ico}</svg><span>${name}</span><svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg></button>${open?`<div class="ws-items">${pages.map(shellLink).join("")}</div>`:""}</div>`}).join("");
  const c=$("#crumbs");if(c)c.innerHTML=`<span class="ws-name">${cur[0]}</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>`;
  $("#ttl").textContent=tab;document.title=tab+" · Commons";
  const st=$("#subtabs");if(st)st.innerHTML=cur[2].length>1?cur[2].map(t=>`<a href="#${encodeURI(t)}" class="${t==tab?"on":""}" onclick="go(${J(t)});return false">${E(t)}</a>`).join(""):"";
  const on=st&&st.querySelector("a.on");if(on&&on.scrollIntoView)on.scrollIntoView({block:"nearest",inline:"center"});
  const b=$("#bnav");if(b)b.innerHTML=WS.map(([name,ico,pages])=>`<a href="#${encodeURI(pages[0])}" class="${name==cur[0]?"on":""}" onclick="go(${J(name==cur[0]?tab:pages[0])});return false"><svg viewBox="0 0 24 24" aria-hidden="true">${ico}</svg><span>${name}</span></a>`).join("");
}
// header status: what's running, today's spend, and a clear banner while everything is paused
function shellStatus(o){
  const h=$("#hstat");if(!h||!o)return;
  h.innerHTML=`<span class="status-dot ${o.running_jobs?"live":""}"></span><span>${o.running_jobs||0} running</span><span class="sep">·</span><span>$${(+o.spent_24h||0).toFixed(2)} today</span>`;
  const ks=$("#ks");if(ks)ks.innerHTML=o.paused?`<div class="paused-banner" role="status"><span>All work is paused</span><button class="pri" onclick="post('killswitch',{paused:false})">Resume</button></div>`:"";
}
function shellMore(btn){
  const items=[{label:"Pause all work",run:()=>post("killswitch",{paused:true,reason:"dashboard"}),danger:true},
               {label:"Switch theme",kbd:"t",run:()=>theme()},
               {label:"Keyboard shortcuts",kbd:"?",run:()=>window.K&&K.keys&&K.keys.help?K.keys.help():null},
               {label:"Setup",run:()=>go("Setup")}];
  if(window.K&&K.menu)K.menu(btn,items);else if(confirm("Pause all work?"))items[0].run();
}
if(window.K&&K.action){K.action({id:"pause",label:"Pause all work",keywords:"kill switch stop",run:()=>post("killswitch",{paused:true,reason:"command palette"})});
  K.action({id:"resume",label:"Resume all work",keywords:"kill switch start",run:()=>post("killswitch",{paused:false})});}

/* ---------- notification bell (top bar): unread count, live from /events; the list opens below it ---------- */
function bellBadge(n){const b=document.getElementById("bellN");if(!b)return;n=+n||0;b.hidden=!n;b.textContent=n>99?"99+":String(n);
  const bt=document.getElementById("bell");if(bt)bt.setAttribute("aria-label",n?n+" unread notifications":"Notifications")}
function bellLoad(){return fetch("/api/notifications",{credentials:"same-origin"}).then(r=>r.json()).then(d=>{bellBadge(d.unread);return d})}
function bellClose(){const p=document.getElementById("bellp");if(p){p.remove();document.removeEventListener("pointerdown",bellAway,true);document.removeEventListener("keydown",bellKey,true)}}
function bellAway(e){const p=document.getElementById("bellp");if(p&&!p.contains(e.target)&&!document.getElementById("bell").contains(e.target))bellClose()}
function bellKey(e){if(e.key==="Escape"){bellClose();document.getElementById("bell").focus()}}
function bellOpen(btn){
  if(document.getElementById("bellp")){bellClose();return}
  const p=document.createElement("div");p.id="bellp";p.className="menu bellp";p.setAttribute("role","dialog");p.setAttribute("aria-label","Notifications");
  p.innerHTML='<div class="bellp-head"><b>Notifications</b><button type="button" class="btn sm" data-all>Mark all read</button></div><div class="bellp-list"><div class="bellp-empty">Loading…</div></div><button type="button" class="bellp-foot">Open System</button>';
  document.body.appendChild(p);
  const r=btn.getBoundingClientRect();p.style.top=(r.bottom+6)+"px";p.style.right=Math.max(8,innerWidth-r.right)+"px";
  document.addEventListener("pointerdown",bellAway,true);document.addEventListener("keydown",bellKey,true);
  p.querySelector(".bellp-foot").onclick=()=>{bellClose();go("System")};
  p.querySelector("[data-all]").onclick=()=>postRaw("notifications/read",{}).then(()=>fill());
  const fill=()=>bellLoad().then(d=>{
    const L=p.querySelector(".bellp-list");L.textContent="";
    if(!(d.items||[]).length){L.innerHTML='<div class="bellp-empty">Nothing yet</div>';return}
    d.items.forEach(n=>{const it=document.createElement("button");it.type="button";it.className="bellp-item"+(n.read?"":" unread")+" lv-"+(n.level||"info");
      it.innerHTML='<span class="bellp-dot"></span><span class="bellp-txt"><span class="bellp-t"></span><span class="bellp-b"></span></span><span class="bellp-ago"></span>';
      it.querySelector(".bellp-t").textContent=n.title||"";it.querySelector(".bellp-b").textContent=n.body||"";
      it.querySelector(".bellp-ago").textContent=window.K&&K.fmt&&K.fmt.ago?K.fmt.ago(n.ts):"";
      it.onclick=()=>{if(!n.read)postRaw("notifications/read",{id:n.id}).then(()=>fill());it.classList.toggle("open")};
      L.appendChild(it)})}).catch(e=>{p.querySelector(".bellp-list").innerHTML='<div class="bellp-empty">Could not load</div>'});
  fill();
}
bellLoad().catch(()=>{});
