/* pages/agents.js: the Agents workspace (Agents, Presence, Capacity, Usage, Models, Connect, Skills, Discover, MCP hub).
   Loaded after legacy.js, kit.js and shell.js; it reassigns those V pages. Inline handlers call window.AG, page state
   (filters, searches, ranges) lives in window.AGS so a live re-render keeps it. Every piece of data goes through E(). */
(() => {
"use strict";
const S = window.AGS = window.AGS || { q: {}, live: "all", plat: "", range: 14, agent: "", by: "agent", dm: "trending",
  dk: "", dq: "", mcpTab: "", ptest: {} };
const arr = x => Array.isArray(x) ? x : [];
const obj = x => x && typeof x === "object" && !Array.isArray(x) ? x : {};
const P = {
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  mail: '<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="m4.5 7.5 7.5 5.5 7.5-5.5"/>',
  ext: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  copy: '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2"/><path d="M15.5 8.5V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v9.5a1 1 0 0 0 1 1h3.5"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4.5H15"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  star: '<path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 16.4l-4.8 2.5.9-5.4-3.9-3.8 5.4-.8z"/>',
  warn: '<path d="M12 4.5 3 19.5h18z"/><path d="M12 10v4.5M12 17v.3"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  term: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="m7 9.5 3 2.5-3 2.5M12.5 15H17"/>',
  key: '<circle cx="8" cy="15.5" r="3.5"/><path d="m10.6 13 8-8M15.5 8l2.5 2.5M17.6 6l2.4 2.4"/>',
  pulse: '<path d="M3 12h4l2.5-6 5 12 2.5-6H21"/>',
  trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 12.5h9l1-12.5"/>',
  send: '<path d="M4.5 12 19.5 5l-4 14-3.5-5.5z"/><path d="m12 13.5 7.5-8.5"/>',
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  bolt: '<path d="M13 2.5 5 13.5h6.5L11 21.5l8-11h-6.5z"/>',
};
const ico = (p, cls = "") => `<svg class="ag-i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${P[p] || p || ""}</svg>`;
const pct = n => `${Math.round(+n || 0)}%`;
const plural = (n, one, many) => `${K.fmt.num(n)} ${n === 1 ? one : (many || one + "s")}`;
const tms = t => { const n = typeof t === "number" ? (t > 1e12 ? t : t * 1000) : Date.parse(t || ""); return n > 0 ? n : NaN; };
function until(t) {  // "in 2h 18m"; a countdown, finer than K.fmt.ago
  const ms = tms(t);
  if (!(ms > 0)) return "";
  let m = Math.round((ms - Date.now()) / 60000);
  if (m <= 0) return "now";
  if (m < 60) return `in ${m}m`;
  const h = Math.floor(m / 60); m %= 60;
  if (h < 48) return `in ${h}h${m ? " " + m + "m" : ""}`;
  return `in ${Math.floor(h / 24)}d ${h % 24}h`;
}
const when = t => { const ms = tms(t); return ms > 0 ? new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : ""; };
const safeUrl = u => /^https?:\/\//i.test(String(u || "")) ? String(u) : "#";
const errMsg = e => String((e && e.message) || e || "failed");

/* ---------- identity: initials avatar, a stable colour per agent, presence state ---------- */
function initials(n) {
  const w = String(n || "?").split(/[\s\-_.]+/).filter(Boolean);
  return ((w.length > 1 ? w[0][0] + w[1][0] : (w[0] || "?").slice(0, 2)) || "?").toUpperCase();
}
function slot(n) { let h = 0; for (const c of String(n)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h % 8 + 1; }
const avatar = (n, dot, size = "") => `<span class="ag-av ${size}" style="--c:var(--ag-s${slot(n)})" aria-hidden="true">${E(initials(n))}${dot ? `<span class="status-dot ${dot}"></span>` : ""}</span>`;
function stateOf(p, claims, seen) {
  if (seen === false || (p && p.seen === false)) return { cls: "offline", label: "Never seen", live: false };
  if (claims && claims.length) return { cls: "busy", label: "Working", live: true };
  const s = p && p.state;
  if (s === "online") return { cls: "live", label: "Online", live: true };
  if (s === "idle") return { cls: "idle", label: "Idle", live: true };
  if (s === "sleeping") return { cls: "idle", label: "Asleep", live: false };
  return { cls: "offline", label: "Offline", live: false };
}
const stateTag = st => `<span class="ag-state"><span class="status-dot ${st.cls}"></span>${E(st.label)}</span>`;
const WAKE = { instant: "Holds a wake stream open: woken within a second", "long-poll": "Long-polls Commons for work",
  push: "Woken through a webhook, ntfy or command channel", polling: "Finds work only when it next checks in", offline: "Not reachable right now" };

/* ---------- small shared pieces ---------- */
function head(title, sub, actions = "") {
  return `<div class="ag-head"><div><h2>${E(title)}</h2>${sub ? `<p>${E(sub)}</p>` : ""}</div>${actions ? `<div class="ag-head-act">${actions}</div>` : ""}</div>`;
}
const stats = items => `<div class="ag-stats">${items.map(([v, l, cls]) => `<div class="ag-stat ${cls || ""}"><b>${E(v)}</b><span>${E(l)}</span></div>`).join("")}</div>`;
function sec(title, body, { n, actions = "", sub = "", id = "" } = {}) {
  return `<section class="ag-sec"${id ? ` id="${E(id)}"` : ""}><div class="ag-sec-h"><h3>${E(title)}${n != null ? ` <span class="ag-n">${E(n)}</span>` : ""}</h3>${sub ? `<span class="ag-sec-sub">${E(sub)}</span>` : ""}${actions ? `<div class="ag-sec-act">${actions}</div>` : ""}</div>${body}</section>`;
}
const matchQ = (q, text) => { const t = String(text || "").toLowerCase(); return String(q || "").toLowerCase().split(/\s+/).filter(Boolean).every(w => t.includes(w)); };
const fattr = (key, text) => `data-q="${E(String(text || "").toLowerCase())}"${matchQ(S.q[key], text) ? "" : " hidden"}`;
function searchBox(key, label) {
  return `<label class="ag-search">${ico("search")}<input type="search" id="agq-${E(key)}" value="${E(S.q[key] || "")}" placeholder="${E(label)}" aria-label="${E(label)}" autocomplete="off" oninput="AG.find(this,${J(key)})" onkeydown="if(event.key==='Escape'&&this.value){event.stopPropagation();this.value='';AG.find(this,${J(key)})}"></label>`;
}
const noMatch = key => `<div class="ag-nomatch" data-fnone="${E(key)}" hidden>Nothing matches your search.</div>`;
function seg(opts, cur, fn, label) {
  return `<div class="seg" role="group" aria-label="${E(label)}">${opts.map(([v, l]) => `<button type="button" class="${v === cur ? "on" : ""}" aria-pressed="${v === cur}" onclick="${fn}(${J(v)})">${E(l)}</button>`).join("")}</div>`;
}
const empty = (icon, title, hint, actions) => `<div class="ag-empty">${K.empty({ icon, title, hint, actions: actions || [] })}</div>`;
const failed = e => `<div class="ag-err" role="alert">${ico("warn")}<div><b>This page could not load</b><div>${E(errMsg(e))}</div></div><button type="button" onclick="render()">Try again</button></div>`;
function skel(page) {  // a skeleton only when arriving from another page; a refresh keeps the current frame
  const m = $("#m");
  if (m && m.dataset.tab !== page) m.innerHTML = `<div class="ag-page"><div class="ag-skel-h"></div>${K.skeleton(6)}</div>`;
}
const kvs = o => `<span class="ag-kvs">${Object.entries(obj(o)).map(([k, v]) => `<span class="ag-kv"><span>${E(k)}</span><b>${E(typeof v === "string" ? v : JSON.stringify(v))}</b></span>`).join("")}</span>`;
const badge = (text, kind = "") => `<span class="badge ${kind}">${E(text)}</span>`;
const code = s => `<code class="ag-code">${E(s)}</code>`;
async function act(path, body, okMsg, { rerender = true } = {}) {
  let r;
  try { r = await postRaw(path, body); } catch (e) { r = { error: errMsg(e) }; }
  if (!r || r.error) { K.toast((r && r.error) || "That did not work", { kind: "err" }); return null; }
  if (okMsg) K.toast(okMsg, { kind: "ok" });
  if (rerender) render();
  return r;
}
const busy = (btn, label) => { if (btn) { btn.disabled = true; btn.dataset.l = btn.innerHTML; btn.textContent = label; } };
const idle = btn => { if (btn && btn.dataset.l) { btn.disabled = false; btn.innerHTML = btn.dataset.l; } };

/* capacity bar: used (solid), reserved for the person (hatched), free for Commons (track) */
function meter(w, mini) {
  const u = Math.min(100, +w.used_pct || 0), r = Math.max(0, Math.min(100 - u, +w.reserve_pct || 0)), f = +w.free_for_commons_pct || 0;
  const sev = f <= 0 ? "bad" : f < 15 ? "warn" : "";
  return `<div class="ag-meter ${sev} ${mini ? "mini" : ""}" role="img" aria-label="${E(`used ${pct(u)}, reserved ${pct(r)}, free for Commons ${pct(f)}`)}"><span class="u" style="width:${u}%"></span>${r ? `<span class="r" style="width:${r}%"></span>` : ""}</div>`;
}
const winName = w => String(w.window || "").replace(/^[a-z0-9_]+:/i, "").replace(/_/g, " ");

/* ---------- live search filtering, without a re-render ---------- */
function applyFind(key) {
  const q = S.q[key] || "", scope = document.querySelector(`[data-fscope="${key}"]`);
  if (!scope) return;
  let n = 0;
  scope.querySelectorAll("[data-q]").forEach(el => { const ok = matchQ(q, el.dataset.q); el.hidden = !ok; if (ok) n++; });
  const none = document.querySelector(`[data-fnone="${key}"]`);
  if (none) none.hidden = n > 0;
  const c = document.querySelector(`[data-fcount="${key}"]`);
  if (c) c.textContent = q ? `${n} shown` : c.dataset.all || "";
}

/* ---------- reset countdowns tick while a page shows them ---------- */
setInterval(() => document.querySelectorAll("[data-until]").forEach(el => { el.textContent = until(el.dataset.until) || el.textContent; }), 30000);

/* =================================================================================================================
   Agents: the fleet
   ================================================================================================================= */
async function fleetData() {
  const [ag, pv, cap, mp] = await Promise.all([api("agents"), api("presence_view").catch(() => ({})),
    api("capacity").catch(() => ({})), api("models_page").catch(() => ({}))]);
  if (ag && ag.error) throw new Error(ag.error);
  const board = arr(obj(pv).board), quotas = arr(obj(cap).agents), models = arr(obj(mp).models), clis = arr(obj(mp).clis);
  // Commons' own internal accounts (the librarian, the hub, the dashboard user, provider bookkeeping) aren't fleet members
  const internal = n => ["cli", "user", "librarian", "commons", "dashboard-user"].includes(n) || /^(provider|cf):/.test(n || "");
  return arr(ag).filter(x => !internal(x.name)).map(x => {
    const p = board.find(b => b.agent === x.name) || null, q = quotas.find(c => c.agent === x.name) || null;
    const ms = models.filter(m => arr(m.agents).includes(x.name)).map(m => m.id);
    const cli = clis.find(c => c.agent === x.name) || null;
    return { ...x, p, q, ms, cli, st: stateOf(p, arr(x.claims), x.seen), unread: p ? +p.unread || 0 : 0 };
  });
}
function agentCard(a) {
  const meta = [a.platform || a.vendor, a.model].filter(Boolean).join(" · ");
  const claims = arr(a.claims);
  const now = claims.length
    ? claims.map(c => `<div class="ag-claim">${badge(c.type || "step", "warn")}<span class="ag-claim-t">${E(String(c.title || "").slice(0, 90))}</span>${c.lease_left_s != null ? `<span class="ag-sub">${E(Math.max(0, Math.round(c.lease_left_s / 60)))}m lease left</span>` : ""}</div>`).join("")
    : a.last_activity ? `<div class="ag-last"><span class="ag-sub">${E(a.last_activity.kind)} ${E(K.fmt.ago(a.last_activity.created_at))}</span><span class="ag-clamp">${E(String(a.last_activity.summary || "").slice(0, 160))}</span></div>`
      : `<span class="ag-sub">${a.st.cls === "offline" ? "Nothing recent" : "Idle, waiting for work"}</span>`;
  const wins = a.q ? arr(a.q.windows).slice(0, 3) : [];
  const learned = Object.entries(obj(a.learned));
  const seen = a.last_seen ? K.fmt.ago(a.last_seen) : "";
  const q = [a.name, meta, a.st.label, a.availability, a.ms.join(" "), claims.map(c => c.title).join(" ")].join(" ");
  return `<article class="ag-card" tabindex="0" role="button" aria-label="${E("Open " + a.name)}" ${fattr("agents", q)}
    onclick="AG.agent(${J(a.name)})" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click()}">
    <div class="ag-card-top">${avatar(a.name, a.st.cls, "lg")}<div class="ag-card-id"><div class="ag-card-name">${E(a.name)}</div>${meta || a.availability ? `<div class="ag-sub">${E([meta, a.availability].filter(Boolean).join(" · "))}</div>` : ""}</div>
      <div class="ag-card-state">${stateTag(a.st)}${seen ? `<span class="ag-sub">${E(seen)}</span>` : ""}</div></div>
    <div class="ag-now">${now}</div>
    <div class="ag-card-facts">
      <div><span class="ag-k">Models</span>${a.ms.length ? `<div class="ag-models"><b>${a.ms.length}</b>${a.ms.slice(0, 2).map(m => `<span class="ag-chip">${E(String(m).split("/").pop())}</span>`).join("")}${a.ms.length > 2 ? `<span class="ag-sub">+${a.ms.length - 2}</span>` : ""}</div>` : `<div class="ag-sub">${E(a.model || "None listed")}</div>`}</div>
      <div><span class="ag-k">Quota</span>${wins.length ? `<div class="ag-wins">${wins.map(w => `<div class="ag-win"><span class="ag-win-n">${E(winName(w))}</span>${meter(w, true)}<span class="ag-win-v">${pct(w.free_for_commons_pct)} free</span></div>`).join("")}</div>` : '<div class="ag-sub">Not reported</div>'}</div>
    </div>
    <div class="ag-card-foot">
      ${a.cli ? `<span class="ag-foot-i">${ico("term")}${E(a.cli.version || "?")}${a.cli.update_available ? ` ${badge((a.cli.latest || "update") + " available", "warn")}` : ""}</span>` : ""}
      ${learned.length ? `<span class="ag-foot-i ag-learned" title="Success rate per task type">${learned.slice(0, 3).map(([k, v]) => `${E(k)} ${Math.round((+v.success_rate || 0) * 100)}%<span class="ag-sub"> (${E(v.n)})</span>`).join(" · ")}</span>` : ""}
      ${a.unread ? `<span class="ag-foot-i ag-unread" title="Unread messages">${ico("mail")}${E(a.unread)} unread</span>` : ""}
      <span class="ag-foot-go">${ico("chev")}</span>
    </div>
  </article>`;
}
V.Agents = async function () {
  skel("Agents");
  let all;
  try { all = await fleetData(); } catch (e) { return `<div class="ag-page">${head("Agents", "")}${failed(e)}</div>`; }
  const plats = [...new Set(all.map(a => a.platform || a.vendor).filter(Boolean))].sort();
  if (S.plat && !plats.includes(S.plat)) S.plat = "";
  const order = { busy: 0, live: 1, idle: 2, offline: 3 };
  const shown = all.filter(a => (S.live !== "live" || a.st.live) && (!S.plat || (a.platform || a.vendor) === S.plat))
    .sort((x, y) => order[x.st.cls] - order[y.st.cls] || String(y.last_seen || "").localeCompare(String(x.last_seen || "")));
  const n = k => all.filter(k).length;
  const ups = n(a => a.cli && a.cli.update_available), unread = all.reduce((s, a) => s + a.unread, 0);
  const tb = `<div class="ag-bar">${searchBox("agents", "Search agents, models, work")}${seg([["all", "All"], ["live", "Live"]], S.live, "AG.setLive", "Show")}
    ${plats.length ? `<select aria-label="Platform" onchange="AG.setPlat(this.value)"><option value="">All platforms</option>${plats.map(p => `<option ${p === S.plat ? "selected" : ""} value="${E(p)}">${E(p)}</option>`).join("")}</select>` : ""}
    <span class="ag-count" data-fcount="agents" data-all="${E(plural(shown.length, "agent"))}">${E(S.q.agents ? "" : plural(shown.length, "agent"))}</span></div>`;
  const body = !all.length
    ? empty("Agents", "No agents yet", "Connect your first AI and it shows up here with its status, models and quota.", [{ label: "Connect an agent", run: () => go("Connect") }])
    : !shown.length
      ? empty("Agents", S.live === "live" ? "No agent is live right now" : "No agents on this platform", "Live means online, idle or working on a step.", [{ label: "Show all agents", run: () => { S.live = "all"; S.plat = ""; render(); } }])
      : `<div class="ag-fleet" data-fscope="agents">${shown.map(agentCard).join("")}</div>${noMatch("agents")}`;
  const out = `<div class="ag-page">${head("Agents", "Every AI connected to this hub: what it is doing, which models it runs and how much quota it has left.",
    `<button type="button" class="pri" onclick="go('Connect')">${ico("plus")}Connect an agent</button>`)}
    ${stats([[n(a => a.st.cls === "live" || a.st.cls === "idle"), "online or idle"], [n(a => a.st.cls === "busy"), "working on a step"],
      [n(a => a.q && a.q.can_take_work), "can take work"], [ups, ups === 1 ? "update available" : "updates available"], [unread, "unread messages"]])}
    ${tb}${body}</div>`;
  if (S.q.agents) setTimeout(() => applyFind("agents"));
  return out;
};

/* ---------- agent drawer ---------- */
async function agentBody(name, ctx) {
  const [d, pv] = await Promise.all([api("agent_detail?name=" + encodeURIComponent(name)), api("presence_view").catch(() => ({}))]);
  if (!d || d.error) throw new Error((d && d.error) || "unknown agent");
  const p = obj(d.presence), st = stateOf(p, [], p.seen), prof = obj(d.profile), u = obj(d.usage), cli = d.cli;
  const sub = ctx && ctx.el.querySelector(".drawer-sub");
  if (sub) { sub.textContent = [d.platform || d.vendor, d.model].filter(Boolean).join(" · ") || "Agent"; sub.hidden = false; }
  const fact = (k, v) => `<div class="ag-fact"><dt>${E(k)}</dt><dd>${v}</dd></div>`;
  const dots = (v, lo, hi) => `<span class="ag-dots" role="img" aria-label="${E(`${v} of 5, ${lo} to ${hi}`)}">${[1, 2, 3, 4, 5].map(i => `<i class="${i <= Math.round(+v) ? "on" : ""}"></i>`).join("")}</span><span class="ag-sub">${E(+v <= 2 ? lo : +v >= 4 ? hi : "average")}</span>`;
  const exp = Object.entries(obj(prof.expertise)).sort((a, b) => b[1] - a[1]);
  const msgs = arr(obj(pv).messages).filter(m => m.to === name || m.from === name).slice(-5).reverse();
  const recent = arr(d.recent);
  const wins = arr(u.windows);
  return `<div class="ag-dr">
    <div class="ag-dr-hero">${avatar(name, st.cls, "xl")}<div><div class="ag-dr-state">${stateTag(st)}${p.last_seen ? `<span class="ag-sub">last seen ${E(K.fmt.ago(p.last_seen))}</span>` : ""}</div>
      <div class="ag-dr-badges">${badge(u.can_take_work ? "Can take work" : "Waiting for quota", u.can_take_work ? "ok" : "warn")}${p.wake_mode ? `<span class="badge" title="${E(WAKE[p.wake_mode] || "")}">wake: ${E(p.wake_mode)}</span>` : ""}${d.unread ? badge(d.unread + " unread", "info") : ""}</div></div></div>
    ${p.status ? `<p class="ag-dr-status">${E(p.status)}</p>` : ""}
    <section class="ag-dr-sec"><h4>Presence</h4><dl class="ag-facts">
      ${fact("Wake mode", E(WAKE[p.wake_mode] || p.wake_mode || "unknown"))}
      ${fact("Live connections", E(p.live_connections || 0))}
      ${fact("Wake channels", arr(p.channels).length ? arr(p.channels).map(c => badge(c)).join(" ") : '<span class="ag-sub">none</span>')}
      ${fact("Last heartbeat", p.last_heartbeat ? E(K.fmt.ago(p.last_heartbeat)) : '<span class="ag-sub">never</span>')}
      ${p.client ? fact("Client", E(p.client)) : ""}
    </dl></section>
    <section class="ag-dr-sec"><h4>Quota</h4>${wins.length ? `<div class="ag-capwins">${wins.map(capWin).join("")}</div>${!u.can_take_work && u.waits_until ? `<p class="ag-sub">Waiting on ${E(winName({ window: u.blocking_window || "" }))}; resets <span data-until="${E(u.waits_until)}">${E(until(u.waits_until))}</span>.</p>` : ""}` : '<p class="ag-sub">No usage windows reported. Agents report them with quota_report, or run commons quota sync.</p>'}</section>
    ${cli ? `<section class="ag-dr-sec"><h4>Agent tool</h4><div class="ag-cli">${ico("term")}<div><b>${E(cli.version || "unknown version")}</b><div class="ag-sub">${cli.update_available ? E((cli.latest || "") + " is available") : cli.latest ? "Up to date" : "Checked by updating"}${cli.updated_at ? " · updated " + E(K.fmt.ago(cli.updated_at)) : ""}${cli.ok === false ? " · last update failed" : ""}</div></div>
      ${cli.requested ? '<span class="ag-sub">Queued: runs within 10 minutes</span>' : `<button type="button" class="${cli.update_available ? "pri" : ""}" onclick="AG.update(${J(name)},this,true)">${ico("up")}Update now</button>`}</div></section>` : ""}
    <section class="ag-dr-sec"><h4>Profile</h4>${exp.length ? `<div class="ag-exp">${exp.map(([k, v]) => `<div class="ag-exp-r"><span>${E(k)}</span><div class="progress"><div class="progress-bar" style="width:${Math.round(+v * 100)}%"></div></div><b>${Math.round(+v * 100)}%</b></div>`).join("")}</div>` : '<p class="ag-sub">No expertise reported yet. Agents describe themselves with set_profile.</p>'}
      <dl class="ag-facts">${prof.cost != null ? fact("Cost", dots(prof.cost, "cheap", "expensive")) : ""}${prof.speed != null ? fact("Speed", dots(prof.speed, "slow", "fast")) : ""}${prof.max_concurrent != null ? fact("Capacity", E(plural(+prof.max_concurrent, "step") + " at once")) : ""}</dl></section>
    <section class="ag-dr-sec"><h4>Models <span class="ag-n">${arr(d.models).length}</span></h4>${arr(d.models).length ? `<div class="ag-chips">${arr(d.models).map(m => `<span class="ag-chip">${E(m)}</span>`).join("")}</div>` : `<p class="ag-sub">${d.model ? "Runs " + E(d.model) + "; no model list reported." : "No model list reported."}</p>`}</section>
    <section class="ag-dr-sec"><h4>Recent steps</h4>${recent.length ? `<div class="ag-steps">${recent.map(r => `<button type="button" class="ag-step" onclick="AG.job(${J(r.job_id)})"><span class="pill ${E(r.status)}">${E(String(r.status || "").replace(/_/g, " "))}</span><span class="ag-step-t"><b>${E(r.title)}</b><span class="ag-sub">${E(r.job)}</span></span><span class="ag-sub">${E(K.fmt.ago(r.updated_at))}</span>${ico("chev")}</button>`).join("")}</div>` : '<p class="ag-sub">No steps yet.</p>'}</section>
    <section class="ag-dr-sec"><h4>Message ${E(name)}</h4>
      <div class="ag-compose"><textarea id="ag-msg" rows="3" placeholder="${E("Write to " + name + "; it arrives at its next check-in")}" aria-label="${E("Message to " + name)}" onkeydown="if((event.metaKey||event.ctrlKey)&&event.key==='Enter'){event.preventDefault();AG.send(${J(name)},null,this)}"></textarea>
      <div class="ag-compose-f"><span class="ag-sub">${K.keys.kbd("mod+enter")} to send</span><button type="button" onclick="AG.send(${J(name)},this)">${ico("send")}Send</button></div></div>
      ${msgs.length ? `<div class="ag-msgs">${msgs.map(msgRow).join("")}</div>` : ""}</section>
  </div>`;
}
function drawer(o) {  // K.drawer.open, tagged so this file's drawer styling applies from the first paint
  const ctx = K.drawer.open(o);
  if (ctx && ctx.el) ctx.el.classList.add("ag-drawer");
  return ctx;
}
function openAgent(name) {
  drawer({ id: "agent:" + name, title: name, subtitle: "Agent", render: ctx => agentBody(name, ctx) });
}
K.drawer.on("agent:", id => openAgent(id.slice(6)));

/* =================================================================================================================
   Presence
   ================================================================================================================= */
function msgRow(m) {
  const txt = m.text || m.subject || "";
  return `<div class="ag-msg" tabindex="0" role="button" aria-expanded="false" onclick="this.classList.toggle('open');this.setAttribute('aria-expanded',this.classList.contains('open'))" onkeydown="if(event.key==='Enter'){event.preventDefault();this.click()}">
    <div class="ag-msg-h"><span class="pill ${E(m.type)}">${E(m.type || "message")}</span><b>${E(m.from || "Commons")}</b>${ico("arrow", "ag-i-s")}<b>${E(m.to || "everyone")}</b><span class="ag-sub ag-msg-t">${E(K.fmt.ago(m.ts))}</span></div>
    <div class="ag-msg-b">${E(String(txt).slice(0, 600))}</div></div>`;
}
V.Presence = async function () {
  skel("Presence");
  let d;
  try { d = await api("presence_view"); if (d && d.error) throw new Error(d.error); } catch (e) { return `<div class="ag-page">${head("Presence", "")}${failed(e)}</div>`; }
  const board = arr(d.board), msgs = arr(d.messages), dead = arr(d.dead);
  const c = s => board.filter(a => a.seen !== false && a.state === s).length;
  const rows = board.map(a => {
    const st = stateOf(a, [], a.seen);
    return `<div class="ag-row click" tabindex="0" role="button" aria-label="${E("Open " + a.agent)}" ${fattr("presence", a.agent + " " + st.label + " " + (a.status || "") + " " + a.wake_mode)} onclick="AG.agent(${J(a.agent)})" onkeydown="if(event.key==='Enter'){event.preventDefault();this.click()}">
      ${avatar(a.agent, st.cls)}<div class="ag-row-main"><div class="ag-row-t">${E(a.agent)}</div><div class="ag-sub ag-ellip">${E(a.status || (a.last_seen ? "last seen " + K.fmt.ago(a.last_seen) : "never seen"))}</div></div>
      <div class="ag-pr-cols"><span class="ag-pr-c">${stateTag(st)}</span>
        <span class="ag-pr-c" title="${E(WAKE[a.wake_mode] || "")}"><span class="ag-k">Wake</span>${E(a.wake_mode || "-")}</span>
        <span class="ag-pr-c"><span class="ag-k">Channels</span>${arr(a.channels).length ? arr(a.channels).map(x => E(x)).join(", ") : '<span class="ag-sub">none</span>'}</span>
        <span class="ag-pr-c"><span class="ag-k">Live</span>${E(a.live_connections || 0)}</span>
        <span class="ag-pr-c">${a.unread ? `<span class="ag-unread">${ico("mail")}${E(a.unread)}</span>` : '<span class="ag-sub">0</span>'}</span></div>
      ${ico("chev", "ag-row-go")}</div>`;
  }).join("");
  const opts = `<option value="*">Everyone</option>` + board.map(a => `<option value="${E(a.agent)}" ${a.agent === S.to ? "selected" : ""}>${E(a.agent)}</option>`).join("");
  return `<div class="ag-page">${head("Presence", "Who is online, how each agent gets woken when work arrives, and the messages between them.")}
    ${stats([[c("online"), "online"], [c("idle"), "idle"], [c("sleeping"), "asleep"], [board.filter(a => a.seen === false || a.state === "offline").length, "offline"], [board.filter(a => a.wakeable).length, "wakeable now"]])}
    ${sec("Presence board", board.length ? `<div class="ag-bar">${searchBox("presence", "Search agents")}</div><div class="ag-list ag-presence" data-fscope="presence"><div class="ag-row ag-row-h" aria-hidden="true"><span></span><span>Agent</span><div class="ag-pr-cols"><span>State</span><span>Wake</span><span>Channels</span><span>Live</span><span>Unread</span></div><span></span></div>${rows}</div>${noMatch("presence")}`
      : empty("Presence", "No agents yet", "Agents appear here once they connect.", [{ label: "Connect an agent", run: () => go("Connect") }]), { n: board.length })}
    <div class="ag-cols">
      ${sec("Messages", `<div class="ag-compose ag-compose-row"><select id="ag-mto" aria-label="Send to" onchange="AGS.to=this.value">${opts}</select><input id="ag-mtx" type="text" placeholder="Write a message" aria-label="Message" onkeydown="if(event.key==='Enter'){event.preventDefault();AG.sendTo(this)}"><button type="button" class="pri" onclick="AG.sendTo(this)">${ico("send")}Send</button></div>
        ${msgs.length ? `<div class="ag-msgs ag-list ${msgs.length > 8 ? "clip" : ""}">${msgs.slice().reverse().map(msgRow).join("")}</div>${msgs.length > 8 ? `<button type="button" class="ag-showall" onclick="this.previousElementSibling.classList.remove('clip');this.remove()">Show all ${E(msgs.length)} messages</button>` : ""}` : empty("", "No messages yet", "Messages between you and your agents appear here.")}`, { n: msgs.length })}
      ${sec("Dead-lettered wakes", dead.length ? `<div class="ag-list">${dead.map(x => `<div class="ag-row"><div class="ag-row-main"><div class="ag-row-t">${E(x.event)}</div><div class="ag-sub">${E(x.last_error || "")}</div></div><span class="ag-sub">${E(plural(+x.attempts || 0, "attempt"))}</span></div>`).join("")}</div>`
        : `<div class="ag-quiet">${ico("check")}Every wake was delivered.</div>`, { n: dead.length || null, sub: "Wakes that failed after every retry", actions: dead.length ? `<button type="button" onclick="AG.redrive(this)">${ico("refresh")}Redrive all</button>` : "" })}
    </div></div>`;
};

/* =================================================================================================================
   Capacity
   ================================================================================================================= */
function capWin(w) {
  return `<div class="ag-capwin"><div class="ag-capwin-h"><span class="ag-capwin-n">${E(winName(w))}</span>${w.period && w.period !== winName(w) ? `<span class="ag-sub">${E(w.period)}</span>` : ""}<span class="ag-capwin-free"><b>${pct(w.free_for_commons_pct)}</b> free</span></div>
    ${meter(w)}<div class="ag-capwin-f"><span>used ${pct(w.used_pct)}</span><span>reserved ${pct(w.reserve_pct)}</span>${w.resets_at ? `<span class="ag-capwin-r">${ico("clock", "ag-i-s")}resets <span data-until="${E(w.resets_at)}">${E(until(w.resets_at))}</span></span>` : ""}</div></div>`;
}
const legend = `<div class="ag-legend" aria-hidden="true"><span><i class="u"></i>Used</span><span><i class="r"></i>Reserved for you</span><span><i class="f"></i>Free for Commons</span></div>`;
V.Capacity = async function () {
  skel("Capacity");
  let d;
  try { d = await api("capacity"); if (d && d.error) throw new Error(d.error); } catch (e) { return `<div class="ag-page">${head("Capacity", "")}${failed(e)}</div>`; }
  const ag = arr(d.agents), sc = arr(d.schedules);
  const cards = ag.map(a => `<div class="ag-capcard"><div class="ag-capcard-h">${avatar(a.agent)}<b>${E(a.agent)}</b>${a.can_take_work ? badge("Can take work", "ok") : a.waits_until ? `<span class="badge warn">Waiting · resets <span data-until="${E(a.waits_until)}">${E(until(a.waits_until))}</span></span>` : badge("Waiting for quota", "warn")}</div>
    ${arr(a.windows).map(capWin).join("")}</div>`).join("");
  const free = ag.filter(a => a.can_take_work).length;
  const tight = ag.flatMap(a => arr(a.windows)).filter(w => (+w.free_for_commons_pct || 0) < 15).length;
  const schedRows = sc.map(x => `<div class="ag-row"><div class="ag-row-main"><div class="ag-row-t">${E(x.name)}</div><div class="ag-sub ag-ellip">${E(x.how || x.acceptance || "")}</div></div>
    <div class="ag-sc-cols"><span class="ag-sc-c">${x.owner ? avatar(x.owner, "", "sm") + E(x.owner) : '<span class="ag-sub">no owner</span>'}</span><span class="ag-sc-c">${code(x.cron)} <span class="ag-sub">${E(x.tz || "")}</span></span>
      <span class="ag-sc-c"><span class="ag-k">Next</span><span data-until="${E(x.next_run || "")}">${E(until(x.next_run))}</span></span>
      <span class="ag-sc-c">${x.last_slot ? badge(x.delivered ? "Delivered" : "Missed", x.delivered ? "ok" : "bad") : '<span class="ag-sub">First run pending</span>'}${x.last_status ? `<span class="ag-sub ag-ellip"> ${E(x.last_status)}</span>` : ""}</span></div></div>`).join("");
  return `<div class="ag-page">${head("Capacity", "How much of each agent's subscription Commons may use before the next reset. Your own share is kept in reserve.")}
    ${ag.length ? stats([[`${free} of ${ag.length}`, "agents can take work"], [ag.length - free, "waiting for a reset"], [tight, tight === 1 ? "window under 15% free" : "windows under 15% free"]]) : ""}
    ${sec("Usage windows", ag.length ? `${legend}<div class="ag-capgrid">${cards}</div><p class="ag-note">Reserve is the larger of the floor and your usual use before each reset.</p>`
      : empty("Capacity", "No usage windows reported yet", "Run commons quota sync --url <hub> (reads aiusage) or have agents call quota_report."), { n: ag.length || null })}
    ${sec("Scheduled tasks", sc.length ? `<div class="ag-list">${schedRows}</div>` : empty("", "No scheduled tasks registered", "Agents add their recurring work with schedule_report."),
      { n: sc.length || null, sub: "A missed or quota-blocked run is taken over by another agent to the same bar" })}</div>`;
};

/* =================================================================================================================
   Usage: inline SVG charts drawn at the container's real width after render
   ================================================================================================================= */
const U = { data: null };
function niceMax(v) { if (!(v > 0)) return 1; const p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p; }
const dayLabel = (i, n) => { const d = new Date(Date.now() - (n - 1 - i) * 864e5); return `${d.getMonth() + 1}/${d.getDate()}`; };
const dayLong = (i, n) => new Date(Date.now() - (n - 1 - i) * 864e5).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
function drawChart(el, c) {
  const W = Math.max(260, el.clientWidth), H = W < 520 ? 170 : 210, L = 46, R = 6, T = 10, B = 24, pw = W - L - R, ph = H - T - B, n = c.labels.length;
  const tot = c.labels.map((_, i) => c.series.reduce((s, x) => s + (x.values[i] || 0), 0));
  const max = niceMax(Math.max(...tot)), band = pw / n, bw = Math.max(3, Math.min(26, band * 0.62));
  let g = "";
  for (let k = 0; k <= 4; k++) {
    const v = max * k / 4, y = T + ph - ph * k / 4;
    g += `<line x1="${L}" x2="${W - R}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" class="${k ? "ag-grid" : "ag-base"}"/><text x="${L - 8}" y="${(y + 3.5).toFixed(1)}" text-anchor="end" class="ag-axis">${E(c.fmt(v))}</text>`;
  }
  const step = Math.ceil(n / (W < 520 ? 5 : 8));
  c.labels.forEach((l, i) => { if ((n - 1 - i) % step === 0) g += `<text x="${(L + band * i + band / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle" class="ag-axis">${E(l)}</text>`; });
  let bars = "";
  c.labels.forEach((_, i) => {
    const x = L + band * i + (band - bw) / 2;
    let y = T + ph, segs = "";
    const live = c.series.map(s => ({ s, v: s.values[i] || 0 })).filter(o => o.v > 0);
    live.forEach((o, j) => {
      const h = o.v / max * ph, top = j === live.length - 1, gap = j ? 2 : 0, hh = Math.max(0, h - gap);
      y -= h;
      if (hh < 0.5) return;
      const r = top ? Math.min(4, hh, bw / 2) : 0, x1 = x.toFixed(1), x2 = (x + bw).toFixed(1), yb = (y + h - gap).toFixed(1), yt = y.toFixed(1);
      segs += top ? `<path d="M${x1} ${yb}V${(y + r).toFixed(1)}Q${x1} ${yt} ${(x + r).toFixed(1)} ${yt}H${(x + bw - r).toFixed(1)}Q${x2} ${yt} ${x2} ${(y + r).toFixed(1)}V${yb}Z" fill="${o.s.color}"/>`
        : `<rect x="${x1}" y="${yt}" width="${bw.toFixed(1)}" height="${hh.toFixed(1)}" fill="${o.s.color}"/>`;
    });
    bars += `<g class="ag-col" data-i="${i}"><rect class="ag-hit" x="${(L + band * i).toFixed(1)}" y="${T}" width="${band.toFixed(1)}" height="${ph}"/>${segs}</g>`;
  });
  el.innerHTML = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${E(c.aria)}">${g}${bars}</svg><div class="ag-tip" hidden></div>`;
  const svg = el.querySelector("svg"), tip = el.querySelector(".ag-tip");
  const show = i => {
    if (i == null || i < 0 || i >= n) { tip.hidden = true; svg.classList.remove("hov"); svg.querySelectorAll(".ag-col.on").forEach(x => x.classList.remove("on")); return; }
    el._i = i;
    svg.querySelectorAll(".ag-col").forEach(x => x.classList.toggle("on", +x.dataset.i === i));
    svg.classList.add("hov");
    tip.textContent = "";
    const h = document.createElement("div"); h.className = "ag-tip-h"; h.textContent = c.long(i); tip.appendChild(h);
    const rows = c.series.length > 1 ? c.series.filter(s => s.values[i]).concat([{ name: "Total", total: true, values: tot }]) : c.series;
    rows.forEach(s => {
      const r = document.createElement("div"); r.className = "ag-tip-r" + (s.total ? " tot" : "");
      const v = document.createElement("b"); v.textContent = c.fmt(s.values[i] || 0, true);
      const k = document.createElement("span"); if (!s.total && c.series.length > 1) { k.className = "ag-key"; k.style.background = s.color; }
      const nm = document.createElement("span"); nm.textContent = s.name;
      r.append(v, k, nm); tip.appendChild(r);
    });
    if (c.series.length > 1 && !c.series.some(s => s.values[i])) { const z = document.createElement("div"); z.className = "ag-sub"; z.textContent = "No spend"; tip.appendChild(z); }
    tip.hidden = false;
    const cx = L + band * i + band / 2, tw = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(W - tw, cx + 12 + tw > W ? cx - tw - 12 : cx + 12)) + "px";
    tip.style.top = T + "px";
  };
  const at = e => { const g = e.target.closest && e.target.closest(".ag-col"); show(g ? +g.dataset.i : null); };
  svg.addEventListener("pointermove", at);
  svg.addEventListener("pointerdown", at);  // a tap on touch screens
  svg.addEventListener("pointerleave", () => show(null));
  el.onkeydown = e => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight" && e.key !== "Escape") return;
    e.preventDefault();
    if (e.key === "Escape") { show(null); return; }
    show(Math.max(0, Math.min(n - 1, (el._i == null ? n : el._i) + (e.key === "ArrowLeft" ? -1 : 1))));
  };
  el.onblur = () => show(null);
}
function drawUsage() {
  const d = U.data;
  if (!d) return;
  const a = document.querySelector('[data-chart="spend"]'), b = document.querySelector('[data-chart="tokens"]');
  if (a) drawChart(a, { labels: d.labels, series: d.series, fmt: (v, full) => full ? K.fmt.money(v) : v >= 100 ? "$" + Math.round(v) : "$" + (+v.toFixed(2)), long: i => dayLong(i, d.labels.length), aria: d.ariaSpend });
  if (b) drawChart(b, { labels: d.labels, series: [{ name: "Tokens", color: "var(--ag-ink)", values: d.tokens }], fmt: (v, full) => full ? Math.round(v).toLocaleString() + " tokens" : K.fmt.num(v), long: i => dayLong(i, d.labels.length), aria: d.ariaTokens });
}
let roT = 0;
if (window.ResizeObserver) new ResizeObserver(() => { clearTimeout(roT); roT = setTimeout(() => { if (document.querySelector("[data-chart]")) drawUsage(); }, 120); }).observe(document.querySelector("main#m") || document.body);
V.Usage = async function () {
  skel("Usage");
  let ser, u;
  try {
    [ser, u] = await Promise.all([api("usage_series?days=30"), api("usage?by=" + encodeURIComponent(S.by))]);
    if (ser && ser.error) throw new Error(ser.error);
    if (u && u.error) throw new Error(u.error);
  } catch (e) { return `<div class="ag-page">${head("Usage", "")}${failed(e)}</div>`; }
  const days = +ser.days || 30, n = Math.min(S.range, days), off = days - n;
  const names = Object.keys(obj(ser.series)).sort();  // fixed order for the whole page: colour follows the agent, never its rank
  if (S.agent && !names.includes(S.agent)) S.agent = "";
  const color = nm => { const i = names.indexOf(nm); return i < 8 ? `var(--ag-s${i + 1})` : "var(--ag-other)"; };
  let series = names.filter(nm => !S.agent || nm === S.agent).map(nm => ({ name: nm, color: color(nm), values: ser.series[nm].slice(off) }));
  if (series.length > 8) {  // past eight agents, fold the smallest into Other instead of inventing hues
    const keep = series.slice().sort((x, y) => y.values.reduce((s, v) => s + v, 0) - x.values.reduce((s, v) => s + v, 0)).slice(0, 7).map(s => s.name);
    const rest = series.filter(s => !keep.includes(s.name));
    series = series.filter(s => keep.includes(s.name)).concat([{ name: "Other", color: "var(--ag-other)", values: Array.from({ length: n }, (_, i) => rest.reduce((t, s) => t + s.values[i], 0)) }]);
  }
  // per-agent daily tokens when an agent is picked (token_series), else everyone's
  const tokens = arr(S.agent && ser.token_series && ser.token_series[S.agent] ? ser.token_series[S.agent] : ser.tokens).slice(off), labels = Array.from({ length: n }, (_, i) => dayLabel(i, n));
  const daySpend = labels.map((_, i) => series.reduce((s, x) => s + x.values[i], 0));
  const spend = daySpend.reduce((s, v) => s + v, 0), tok = tokens.reduce((s, v) => s + v, 0);
  U.data = { labels, series, tokens, ariaSpend: `Spend per day, last ${n} days, ${K.fmt.money(spend)} total`, ariaTokens: `Tokens per day, last ${n} days, ${K.fmt.num(tok)} total` };
  setTimeout(drawUsage);
  const top = series.map(s => [s.name, s.values.reduce((a, v) => a + v, 0)]).sort((x, y) => y[1] - x[1])[0];
  const cutoff = new Date(Date.now() - (n - 1) * 864e5).toISOString().slice(0, 10);
  const allDays = arr(u.daily).slice().sort((x, y) => String(y.key).localeCompare(String(x.key)));
  const daily = S.allDays ? allDays : allDays.filter(r => String(r.key) >= cutoff);
  const rows = arr(u.rows), maxC = Math.max(1e-9, ...rows.map(r => +r.cost || 0)), maxT = Math.max(1, ...rows.map(r => (+r.tokens_in || 0) + (+r.tokens_out || 0)));
  const legendHtml = series.length > 1 ? `<div class="ag-clegend">${series.map(s => `<span><i style="background:${s.color}"></i>${E(s.name)} <b>${E(K.fmt.money(s.values.reduce((a, v) => a + v, 0)))}</b></span>`).join("")}</div>` : "";
  const filt = `<div class="ag-bar ag-filters">${seg([[7, "7 days"], [14, "14 days"], [30, "30 days"]], S.range, "AG.setRange", "Range")}
    <select aria-label="Agent" onchange="AG.setAgent(this.value)"><option value="">All agents</option>${names.map(nm => `<option ${nm === S.agent ? "selected" : ""} value="${E(nm)}">${E(nm)}</option>`).join("")}</select></div>`;
  const tile = (label, value, hint, extra = "") => `<div class="tile"><div class="tile-header"><span class="tile-label">${E(label)}</span></div><div class="tile-value">${E(value)}</div><div class="tile-footer"><span class="tile-hint">${E(hint)}</span></div>${extra}</div>`;
  const anySpend = names.length > 0;
  return `<div class="ag-page">${head("Usage", "Spend and tokens your agents report with report_usage. The worker reports for its own runs.")}
    ${filt}
    <div class="ag-tiles">
      ${tile(`Spend, last ${n} days`, K.fmt.money(spend), S.agent ? S.agent : "all agents", `<div class="ag-tile-spark">${K.spark(daySpend, { w: 120, h: 28 })}</div>`)}
      ${tile("Average per day", K.fmt.money(spend / n), top && top[1] > 0 && !S.agent ? `most from ${top[0]}` : `over ${n} days`)}
      ${tile(`Tokens, last ${n} days`, K.fmt.num(tok), S.agent ? S.agent : "all agents")}
      ${tile("Last 24 hours", K.fmt.money(+u.spent_24h || 0), "all agents")}
    </div>
    <section class="ag-sec"><div class="ag-chartcard"><div class="ag-sec-h"><h3>Spend per day</h3><span class="ag-sec-sub">${S.agent ? E(S.agent) : series.length > 1 ? "stacked by agent" : ""}</span></div>
      ${anySpend ? `<div class="ag-chart" data-chart="spend" tabindex="0" aria-label="${E(U.data.ariaSpend + ". Use the arrow keys to read each day.")}"></div>${legendHtml}` : empty("Usage", "No spend yet", "Agents log cost with report_usage; the worker does it automatically.")}</div></section>
    <div class="ag-cols">
      <section class="ag-sec"><div class="ag-chartcard"><div class="ag-sec-h"><h3>Tokens per day</h3><span class="ag-sec-sub">${E(S.agent || "all agents")}</span></div>
        ${tok ? `<div class="ag-chart" data-chart="tokens" tabindex="0" aria-label="${E(U.data.ariaTokens + ". Use the arrow keys to read each day.")}"></div>` : empty("", "No tokens in this range", "")}</div></section>
      <section class="ag-sec"><div class="ag-chartcard ag-dailycard"><div class="ag-sec-h"><h3>Daily totals</h3><span class="ag-sec-sub">all agents · calendar days in UTC</span>${allDays.length > daily.length || S.allDays ? `<button type="button" class="ag-link ag-sec-act" onclick="AGS.allDays=!AGS.allDays;render()">${S.allDays ? "Only this range" : `All ${E(allDays.length)} days`}</button>` : ""}</div>${daily.length ? `<div class="ag-daily"><div class="ag-row ag-row-h" aria-hidden="true"><span>Day</span><span>Spend</span><span>Tokens</span><span>Calls</span></div>${daily.map(r => `<div class="ag-row"><span>${E(new Date(r.key + "T12:00:00Z").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" }))}</span><b>${E(K.fmt.money(r.cost))}</b><span>${E(K.fmt.num((+r.tokens_in || 0) + (+r.tokens_out || 0)))}</span><span>${E(r.calls || 0)}</span></div>`).join("")}</div>` : '<div class="ag-quiet">No usage in this range.</div>'}</div></section>
    </div>
    ${sec("Breakdown", `<div class="ag-bar">${seg([["agent", "Agent"], ["project", "Project"], ["job", "Job"], ["model", "Model"]], S.by, "AG.setBy", "Group by")}<span class="ag-count">all time · ${E(plural(rows.length, "row"))}</span></div>
      ${rows.length ? `<div class="ag-list ag-brk"><div class="ag-row ag-row-h" aria-hidden="true"><span>${E(S.by)}</span><span>Cost</span><span>Tokens in / out</span><span>Time</span><span>Calls</span></div>${rows.map(r => `<div class="ag-row">
        <span class="ag-brk-n">${S.by === "agent" ? `<i class="ag-key" style="background:${names.includes(r.label) ? color(r.label) : "var(--ag-other)"}"></i>` : ""}<span class="ag-ellip" title="${E(r.label)}">${E(r.label)}</span></span>
        <span class="ag-brk-v"><span class="ag-hbar"><i style="width:${(100 * (+r.cost || 0) / maxC).toFixed(1)}%"></i></span><b>${E(K.fmt.money(r.cost))}</b></span>
        <span class="ag-brk-v"><span class="ag-hbar t"><i style="width:${(100 * ((+r.tokens_in || 0) + (+r.tokens_out || 0)) / maxT).toFixed(1)}%"></i></span><span>${E(K.fmt.num(r.tokens_in))} / ${E(K.fmt.num(r.tokens_out))}</span></span>
        <span>${E(Math.round(+r.seconds || 0).toLocaleString())}s</span><span>${E(r.calls || 0)}</span></div>`).join("")}</div>` : '<div class="ag-quiet">Nothing reported yet.</div>'}`)}
  </div>`;
};

/* =================================================================================================================
   Models
   ================================================================================================================= */
V.Models = async function () {
  skel("Models");
  let m;
  try { m = await api("models_page"); if (m && m.error) throw new Error(m.error); } catch (e) { return `<div class="ag-page">${head("Models", "")}${failed(e)}</div>`; }
  const sv = obj(m.seats), provs = arr(m.providers), clis = arr(m.clis), roles = Object.entries(obj(m.roles)), routing = arr(m.routing), props = arr(m.proposals), models = arr(m.models);
  const cap = +sv.cap || 0, spent = +sv.spent_24h || 0, frac = cap ? Math.min(1, spent / cap) : 0;
  const seatState = sv.enabled ? (sv.capped ? ["Paused at cap", "bad"] : ["On", "ok"]) : ["Off", ""];
  const seats = `<div class="ag-panel ag-seats"><div class="ag-seats-h"><div><div class="ag-seats-t">API seats <span id="ag-seat-b" class="badge ${seatState[1]}">${E(seatState[0])}</span></div>
      <p class="ag-sub">Models from your own keys take steps when no subscription agent is free; never code, tests or releases.</p></div>
      <label class="switch" title="Turn API seats on or off"><input type="checkbox" ${sv.enabled ? "checked" : ""} aria-label="API seats" onchange="AG.seats(this)"><span class="switch-slider"></span></label></div>
    <div class="ag-seats-b"><div><span class="ag-k">Tier models</span>${Object.keys(obj(sv.models)).length ? `<div class="ag-tiers">${Object.entries(sv.models).map(([t, x]) => `<span class="ag-tier"><span class="ag-sub">${E(t)}</span>${code(x)}</span>`).join("")}</div>` : '<div class="ag-sub">Add a provider key on Setup (Anthropic is used first) or set seat_models.</div>'}</div>
      <div><span class="ag-k">Last 24 hours</span><div class="ag-spend"><b>${E(K.fmt.money(spent))}</b><span class="ag-sub">of ${E(K.fmt.money(cap))} cap · ${E(plural(+sv.runs_24h || 0, "run"))}</span></div>
      <div class="progress ${frac >= 1 ? "bad" : frac > 0.8 ? "warn" : "ok"}" role="progressbar" aria-label="Spend against the daily cap" aria-valuenow="${Math.round(frac * 100)}" aria-valuemin="0" aria-valuemax="100"><div class="progress-bar" style="width:${(frac * 100).toFixed(1)}%"></div></div></div></div></div>`;
  const tools = clis.length ? `<div class="ag-list">${clis.map(c => `<div class="ag-row">${avatar(c.agent, "", "sm")}<div class="ag-row-main"><div class="ag-row-t">${E(c.agent)}</div><div class="ag-sub">${c.updated_at ? "updated " + E(K.fmt.ago(c.updated_at)) : "never updated"}${c.ok === false ? ' · <span class="ag-bad">last update failed</span>' : ""}</div></div>
      <span class="ag-ver">${code(c.version || "?")}${c.update_available ? badge((c.latest || "") + " available", "warn") : c.latest ? '<span class="ag-sub">up to date</span>' : '<span class="ag-sub">checked by updating</span>'}</span>
      <div class="ag-acts ag-acts-on">${c.requested ? '<span class="ag-sub">Queued: runs within 10 min</span>' : `<button type="button" class="${c.update_available ? "pri" : ""}" onclick="AG.update(${J(c.agent)},this)">${ico("up")}Update now</button>`}</div></div>`).join("")}</div>` : "";
  const provRows = provs.map(p => {
    const t = S.ptest[p.name];
    const res = t ? (t.pending ? '<span class="ag-sub">Testing…</span>' : t.ok ? `<span class="ag-ok">${ico("check", "ag-i-s")}Connected · ${E(plural(+t.models || 0, "model"))}</span>` : `<span class="ag-bad">${ico("warn", "ag-i-s")}${E(t.error || "failed")}</span>`) : "";
    return `<div class="ag-row ag-prov"><div class="ag-row-main"><div class="ag-row-t">${E(p.name)} ${p.kind && p.kind !== p.name ? badge(p.kind) : ""}${p.enabled === false ? " " + badge("disabled") : ""}</div>
      <div class="ag-sub">${code(p.key_ref || "no key ref")} · ${E(plural(+p.models || 0, "model"))}${p.last_refresh ? " · refreshed " + E(K.fmt.ago(p.last_refresh)) : ""}</div>
      ${p.last_error && !t ? `<div class="ag-bad ag-ellip">${ico("warn", "ag-i-s")}${E(p.last_error)}</div>` : ""}<div class="ag-ptest" id="ag-pt-${E(p.name)}" aria-live="polite">${res}</div></div>
      <div class="ag-acts ag-acts-on"><button type="button" onclick="AG.ptest(${J(p.name)},this)">${ico("pulse")}Test connection</button></div></div>`;
  }).join("");
  const modelRows = models.slice(0, 200).map(x => `<div class="ag-row ag-mrow" ${fattr("models", [x.id, x.provider, arr(x.agents).join(" ")].join(" "))}>
    <span class="ag-mrow-id">${code(x.id)}${x.is_new ? " " + badge("new", "accent") : ""}</span><span>${x.context ? E(K.fmt.num(x.context)) : '<span class="ag-sub">-</span>'}</span>
    <span class="ag-sub">${x.cost_in != null ? "$" + E(x.cost_in) : "-"} · ${x.cost_out != null ? "$" + E(x.cost_out) : "-"}</span>
    <span class="ag-sub">${Object.entries(obj(x.scores)).map(([k, v]) => E(k) + " " + Math.round(v * 100) + "%").join(" · ") || "-"}</span>
    <span class="ag-sub ag-ellip">${E(arr(x.agents).join(", ") || "-")}</span></div>`).join("");
  const out = `<div class="ag-page">${head("Models", "Which models your agents and API keys can run, what each role uses, and how work is routed.",
    `<button type="button" onclick="AG.refreshModels(this)">${ico("refresh")}Refresh model lists</button>`)}
    ${seats}
    ${props.length ? sec("Reallocation proposals", `<div class="ag-list ag-decide">${props.map(p => `<div class="ag-row ag-dec ${p.status === "proposed" ? "on" : ""}"><div class="ag-row-main">
        <div class="ag-row-t">${badge(p.task_type || "general")} ${code(p.from_model || "none")} ${ico("arrow", "ag-i-s")} ${code(p.to_model)}</div>
        <div class="ag-dec-ev">${kvs(p.evidence)}</div></div>
        <div class="ag-acts ag-acts-on">${p.status === "proposed" ? `<button type="button" class="pri" onclick="AG.decide(${J(p.id)},true,this)">Apply</button><button type="button" onclick="AG.decide(${J(p.id)},false,this)">Reject</button>` : `<span class="pill ${E(p.status)}">${E(p.status)}</span>`}</div></div>`).join("")}</div>`,
      { n: props.filter(p => p.status === "proposed").length || null, sub: "Commons suggests a better model per task type from outcomes" }) : ""}
    ${sec("Providers", provs.length ? `<div class="ag-list">${provRows}</div>` : empty("Models", "No providers yet", "Add an API key on Setup, or run commons models add-provider openrouter openrouter --key-ref env://OPENROUTER_API_KEY.", [{ label: "Open Setup", run: () => go("Setup") }]),
      { n: provs.length || null, sub: "Refresh lists, probe new models and rebuild routing with the button above" })}
    ${clis.length ? sec("Agent tools", tools, { n: clis.length, sub: "Each agent's own CLI, checked every 10 minutes on its machine" }) : ""}
    <div class="ag-cols">
      ${sec("LLM roles", `<div class="ag-list ag-roles">${roles.map(([r, v]) => `<div class="ag-row"><span class="ag-row-t">${E(r.replace(/_/g, " "))}</span><span>${v.model ? code(v.model) + (v.explicit || !v.model ? "" : ' <span class="ag-sub">via distributor</span>') : '<span class="ag-sub">heuristic</span>'}</span></div>`).join("")}</div>`, { n: roles.length })}
      ${sec("Routing table", routing.length ? `<div class="ag-list ag-routes">${routing.map(r => `<div class="ag-row"><div class="ag-row-main"><div class="ag-row-t">${badge(r.task_type)} ${E(r.agent || "")}</div><div class="ag-sub">${code(r.model || "")} · ${E(r.reason || "")}</div></div><span class="ag-sub">${E(r.source || "")}</span></div>`).join("")}</div>`
        : '<div class="ag-quiet">No routes yet. Refreshing model lists builds them.</div>', { n: routing.length || null })}
    </div>
    ${sec("Model registry", models.length ? `<div class="ag-bar">${searchBox("models", "Search models, providers, agents")}<span class="ag-count" data-fcount="models" data-all="${E(plural(models.length, "model"))}">${E(plural(models.length, "model"))}</span></div>
      <div class="ag-list ag-models-t" data-fscope="models"><div class="ag-row ag-row-h ag-mrow" aria-hidden="true"><span>Model</span><span>Context</span><span>$ per M in · out</span><span>Scores</span><span>Agents</span></div>${modelRows}</div>${noMatch("models")}${models.length > 200 ? `<p class="ag-note">Showing the first 200 of ${E(models.length)}.</p>` : ""}`
      : '<div class="ag-quiet">The registry fills when a provider is added and its model list refreshed.</div>', { n: models.length || null })}
  </div>`;
  if (S.q.models) setTimeout(() => applyFind("models"));
  return out;
};

/* =================================================================================================================
   Connect: a four-step flow with a live connection status (uses the cx state and helpers from dashboard_v81)
   ================================================================================================================= */
const CX = { blocks: [], t: 0, last: null };
function cxStatusHtml(s) {
  const nm = E((s && s.name) || cx.name);
  if (!s || !s.exists) return `<div class="ag-live ag-live-wait"><span class="status-dot busy"></span><div><b>Waiting for ${nm} to connect</b><div class="ag-sub">Checks every 3 seconds. Run the command above on the machine where the AI runs.</div></div></div>`;
  if (s.connected) return `<div class="ag-live ag-live-ok"><span class="status-dot live"></span><div><b>${nm} is connected</b><div class="ag-sub">Last seen ${E(K.fmt.ago(s.last_seen) || "just now")}. It shows up on Agents with its status and models.</div></div><button type="button" onclick="AG.agent(${J(s.name)})">Open agent</button></div>`;
  if (s.last_seen) return `<div class="ag-live ag-live-idle"><span class="status-dot idle"></span><div><b>${nm} was last seen ${E(K.fmt.ago(s.last_seen))}</b><div class="ag-sub">Its key exists. It counts as connected once it checks in again.</div></div></div>`;
  return `<div class="ag-live ag-live-wait"><span class="status-dot busy"></span><div><b>${nm} has a key but has not connected yet</b><div class="ag-sub">Checks every 3 seconds.</div></div></div>`;
}
function cxPoll() {
  clearInterval(CX.t);
  CX.t = setInterval(async () => {
    if (tab !== "Connect") { clearInterval(CX.t); return; }
    let s; try { s = await api("connect_status?name=" + encodeURIComponent(cx.watch || cx.name)); } catch (e) { return; }
    const was = CX.last && CX.last.connected;
    CX.last = s;
    const el = $("#ag-cxs"); if (el) el.innerHTML = cxStatusHtml(s);
    const st = document.querySelector('[data-cxstep="4"]'); if (st) st.classList.toggle("is-done", !!s.connected);
    if (s.connected && !was) K.toast(`${s.name} is connected`, { kind: "ok" });
  }, 3000);
}
V.Connect = async function () {
  skel("Connect");
  let b, s;
  try {
    [b, s] = await Promise.all([cxBundle(), api("connect_status?name=" + encodeURIComponent(cx.watch || cx.name)).catch(() => null)]);
    if (b && b.error) throw new Error(b.error);
    if (!cx.url) cx.url = b.detected_url;
  } catch (e) { return `<div class="ag-page">${head("Connect", "")}${failed(e)}</div>`; }
  CX.last = s; CX.blocks = arr(b.blocks);
  cxPoll();
  const done2 = !!cx.key || !!(s && s.exists);
  const step = (n, title, body, done, sub = "") => `<li class="ag-step-li ${done ? "is-done" : ""}" data-cxstep="${n}"><span class="ag-step-n" aria-hidden="true"><span>${n}</span>${ico("check")}</span><div class="ag-step-b"><h3>${E(title)}</h3>${sub ? `<p class="ag-sub">${E(sub)}</p>` : ""}${body}</div></li>`;
  const plats = Object.entries(obj(b.platforms));
  const keyBox = cx.key ? `<div class="ag-keybox" role="status"><div class="ag-keybox-h">${ico("key")}<b>Key for ${E(cx.name)}</b>${badge("role " + cx.role)}<span class="ag-sub">Shown once; only its hash is stored. It is already filled into the command below.</span></div>
    <div class="ag-codeblock"><pre id="cxkey">${E(cx.key)}</pre><button type="button" class="ag-copy" onclick="K.copy(cx.key)" aria-label="Copy the key">${ico("copy")}Copy</button></div>
    <button type="button" class="ag-link" onclick="cx.key=null;cx.b=null;render()">Hide and forget</button></div>` : "";
  const keyForm = b.can_create_key ? `<div class="ag-form">
      <label class="ag-fld"><span>Agent name</span><input id="cxn" type="text" value="${E(cx.name)}" autocomplete="off" onchange="cxSet('name',this.value.trim()||'my-agent')"></label>
      <label class="ag-fld"><span>Role</span><select id="cxr" onchange="cx.role=this.value">${["read", "write", "admin"].map(r => `<option ${r === cx.role ? "selected" : ""}>${r}</option>`).join("")}</select></label>
      <label class="ag-fld"><span>Project <i>optional</i></span><input id="cxp" type="text" value="${E(cx.project)}" placeholder="all projects" autocomplete="off" onchange="cx.project=this.value.trim()"></label>
      <button type="button" class="pri" onclick="AG.cxKey(this)">${ico("key")}Create agent key</button></div>`
    : `<div class="ag-quiet">${ico("warn")}Key creation is off on this dashboard: ${E(b.key_gate_reason)}. Run ${code("commons connect " + cx.platform + " --name NAME --new-key")} on the server.</div>`;
  const blockHtml = CX.blocks.map((x, i) => x.kind === "setup" ? "" : x.collapsed
    ? `<details class="ag-more"><summary>${ico("chev")}${E(x.title)}</summary><div class="ag-codeblock"><pre>${E(x.text)}</pre><button type="button" class="ag-copy" onclick="AG.cxCopy(${i})">${ico("copy")}Copy</button></div></details>`
    : `<div class="ag-blk"><div class="ag-blk-t">${E(x.title)}</div><div class="ag-codeblock"><pre>${E(x.text)}</pre><button type="button" class="ag-copy" onclick="AG.cxCopy(${i})">${ico("copy")}Copy</button></div></div>`).join("");
  const setup = CX.blocks.find(x => x.kind === "setup");
  const setupHtml = setup ? `<div class="ag-steps-opt">${arr(setup.steps).map(st => `<label class="ag-opt"><input type="checkbox" ${(cx.skip || []).includes(st.id) ? "" : "checked"} onchange="cxStep(${J(st.id)},this.checked)"><span>${E(st.label)}</span></label>`).join("")}</div>
      <div class="ag-codeblock big"><pre>${E(setup.base + (cx.skip || []).map(k => " --no-" + k).join(""))}</pre><button type="button" class="ag-copy pri" onclick="AG.cxCopy(${CX.blocks.indexOf(setup)})">${ico("copy")}Copy</button></div>
      <p class="ag-sub">Run it where the AI runs. It saves the key once (hidden prompt, a 0600 file), then does every ticked step and prints what it did.${cx.key ? " Paste the key above when it asks." : ""}</p>` : "";
  return `<div class="ag-page ag-connect">${head("Connect an agent", "Pick the AI, create its key, then copy one command. This page watches for it to check in.")}
    <ol class="ag-stepper">
      ${step(1, "Choose the AI", `<div class="ag-plats" role="group" aria-label="AI">${plats.map(([k, l]) => `<button type="button" class="chip ${k === cx.platform ? "on" : ""}" aria-pressed="${k === cx.platform}" onclick="cxSet('platform',${J(k)})">${E(l)}</button>`).join("")}</div>
        <p class="ag-sub">${E(b.hint || "")}</p>
        <details class="ag-more" ${cx.url && cx.url !== b.detected_url ? "open" : ""}><summary>${ico("chev")}Hub address <span class="ag-sub">${E(cx.url)}</span></summary>
        <div class="ag-form"><label class="ag-fld grow"><span>MCP URL</span><input id="cxu" type="text" value="${E(cx.url)}" onchange="cxSet('url',this.value.trim())"></label><button type="button" onclick="cxSet('url','')">Reset</button></div>
        <p class="ag-sub">Detected ${code(b.detected_url)}. Use an address the AI's machine can reach.</p></details>`, true)}
      ${step(2, "Create its key", keyBox + keyForm, done2, "Each AI gets its own key, so you can see who did what and revoke one without touching the others.")}
      ${step(3, "Run one command", setupHtml + blockHtml, false)}
      ${step(4, "Watch it connect", `<div id="ag-cxs" aria-live="polite">${cxStatusHtml(s)}</div>`, !!(s && s.connected))}
    </ol>
    ${sec("Troubleshooting", `<div class="ag-list ag-trouble">${arr(b.troubleshooting).map(t => `<label class="ag-row"><input type="checkbox" aria-label="${E("Checked: " + t.symptom)}"><div class="ag-row-main"><div class="ag-row-t">${E(t.symptom)}</div><div class="ag-sub">${E(t.fix)}</div></div></label>`).join("")}</div>
      <p class="ag-note">CLI equivalent: ${code(`commons connect ${cx.platform} --name ${cx.name} --new-key`)} · JSON: ${code(`GET /api/connect?platform=${cx.platform}`)}</p>`)}
  </div>`;
};

/* =================================================================================================================
   Skills
   ================================================================================================================= */
V.Skills = async function () {
  skel("Skills");
  let sk;
  try { sk = await api("skills"); if (sk && sk.error) throw new Error(sk.error); } catch (e) { return `<div class="ag-page">${head("Skills", "")}${failed(e)}</div>`; }
  sk = arr(sk);
  const pend = sk.filter(x => arr(x.pending).length);
  const row = x => {
    const st = obj(x.stats), sr = st.success_rate == null ? null : Math.round(st.success_rate * 100);
    return `<div class="ag-row click" tabindex="0" role="button" ${fattr("skills", [x.name, x.description, arr(x.tags).join(" ")].join(" "))} onclick="AG.skill(${J(x.name)})" onkeydown="if(event.key==='Enter'){event.preventDefault();this.click()}">
      <span class="ag-skico">${ico(ICON.Skills)}</span><div class="ag-row-main"><div class="ag-row-t">${E(x.name)} <span class="ag-sub">v${E(x.active_version ?? "-")}</span>${arr(x.pending).map(p => " " + badge(`v${p.version} ${p.status}`, p.status === "flagged" ? "bad" : "warn")).join("")}</div>
      <div class="ag-sub ag-clamp1">${E(x.description || "")}</div>${x.forked_from ? `<div class="ag-sub">fork of ${E(x.forked_from)}</div>` : ""}</div>
      <div class="ag-sk-tags">${arr(x.tags).slice(0, 3).map(t => `<span class="ag-tag">${E(t)}</span>`).join("")}</div>
      <span class="ag-sk-use"><b>${E(K.fmt.num(st.uses || 0))}</b> uses${sr == null ? "" : ` · ${sr}% success`}</span>
      <div class="ag-acts"><button type="button" onclick="event.stopPropagation();AG.skill(${J(x.name)})">History</button></div></div>`;
  };
  const out = `<div class="ag-page">${head("Skills", "Agents fetch these on demand with skill_get; nothing to install locally. New versions are scanned and reviewed before they go live.",
    `<button type="button" onclick="go('Import')">Import from this machine</button><button type="button" onclick="go('Discover')">${ico("search")}Discover skills</button>`)}
    ${pend.length ? sec("Waiting for review", `<div class="ag-list ag-decide">${pend.map(x => arr(x.pending).map(p => `<div class="ag-row ag-dec on"><div class="ag-row-main"><div class="ag-row-t">${E(x.name)} v${E(p.version)} ${badge(p.status, p.status === "flagged" ? "bad" : "warn")}</div><div class="ag-sub">Review the diff and scan findings, then approve or reject.</div></div><div class="ag-acts ag-acts-on"><button type="button" class="pri" onclick="AG.skill(${J(x.name)})">Review</button></div></div>`).join("")).join("")}</div>`, { n: pend.reduce((s, x) => s + arr(x.pending).length, 0) }) : ""}
    ${sec("Skill library", sk.length ? `<div class="ag-bar">${searchBox("skills", "Search skills and tags")}<span class="ag-count" data-fcount="skills" data-all="${E(plural(sk.length, "skill"))}">${E(plural(sk.length, "skill"))}</span></div><div class="ag-list" data-fscope="skills">${sk.map(row).join("")}</div>${noMatch("skills")}`
      : empty("Skills", "No skills in the pool yet", "Import the skills installed on this machine, or add some from Discover.", [{ label: "Import skills", run: () => go("Import") }, { label: "Discover", run: () => go("Discover") }]), { n: sk.length || null })}</div>`;
  if (S.q.skills) setTimeout(() => applyFind("skills"));
  return out;
};
async function skillBody(name) {
  const [h, all] = await Promise.all([api("skill_history?name=" + encodeURIComponent(name)), api("skills").catch(() => [])]);
  if (h && h.error) throw new Error(h.error);
  const x = arr(all).find(s => s.name === name) || {}, st = obj(x.stats);
  const finding = f => typeof f === "string" ? f : JSON.stringify(f);
  return `<div class="ag-dr">${x.description ? `<p class="ag-dr-desc">${E(x.description)}</p>` : ""}
    <dl class="ag-facts">${x.author ? `<div class="ag-fact"><dt>Author</dt><dd>${E(x.author)}</dd></div>` : ""}<div class="ag-fact"><dt>Active</dt><dd>v${E(x.active_version ?? "-")}</dd></div>
      <div class="ag-fact"><dt>Uses</dt><dd>${E(st.uses || 0)}${st.success_rate == null ? "" : ` · ${Math.round(st.success_rate * 100)}% success`}</dd></div>${arr(x.tags).length ? `<div class="ag-fact"><dt>Tags</dt><dd>${arr(x.tags).map(t => `<span class="ag-tag">${E(t)}</span>`).join(" ")}</dd></div>` : ""}</dl>
    <section class="ag-dr-sec"><h4>Versions <span class="ag-n">${arr(h).length}</span></h4><div class="ag-vers">${arr(h).map(v => `<div class="ag-ver-c ${["pending", "flagged"].includes(v.status) ? "on" : ""}">
      <div class="ag-ver-h"><b>v${E(v.version)}</b><span class="pill ${E(v.status)}">${E(v.status)}</span>${v.version === x.active_version && v.status !== "active" ? badge("active", "ok") : ""}<span class="ag-sub">${E(v.author || "")} · ${E(when(v.created_at) || v.created_at || "")}</span></div>
      ${v.changelog ? `<div class="ag-ver-l">${E(v.changelog)}</div>` : ""}
      ${arr(v.findings).length ? `<div class="ag-findings">${ico("warn")}<div>${arr(v.findings).map(f => `<div>${E(finding(f))}</div>`).join("")}</div></div>` : ""}
      ${v.review && v.review.by ? `<div class="ag-sub">Review: ${E(v.review.verdict)} by ${E(v.review.by)}${v.review.notes ? " · " + E(v.review.notes) : ""}</div>` : ""}
      ${v.diff ? `<details class="ag-more"><summary>${ico("chev")}Diff</summary><pre class="ag-diff">${E(v.diff)}</pre></details>` : ""}
      <div class="ag-ver-a">${["pending", "flagged"].includes(v.status) ? `<button type="button" class="pri" onclick="AG.skillReview(${J(name)},${+v.version},true,this)">Approve</button><button type="button" class="bad" onclick="AG.skillReview(${J(name)},${+v.version},false,this)">Reject</button>` : ""}
        ${(v.status === "superseded" || v.status === "approved") && v.version !== x.active_version ? `<button type="button" onclick="AG.skillRollback(${J(name)},${+v.version},this)">Roll back to v${E(v.version)}</button>` : ""}</div></div>`).join("") || '<p class="ag-sub">No versions.</p>'}</div></section></div>`;
}
const openSkill = name => drawer({ id: "skill:" + name, title: name, subtitle: "Skill", render: () => skillBody(name) });
K.drawer.on("skill:", id => openSkill(id.slice(6)));

/* =================================================================================================================
   Discover
   ================================================================================================================= */
const DISC = { groups: [] };
function discGroups(rows) {
  const gs = [], gi = {};
  rows.forEach(r => {
    const k = String(r.id).split(":")[0];
    if (!(k in gi)) { gi[k] = gs.length; gs.push({ repo: k, h: r, items: [], seen: {} }); }
    const g = gs[gi[k]], sk = r.name + "|" + r.kind, j = g.seen[sk];
    if (j === undefined) { g.seen[sk] = g.items.length; g.items.push(r); } else if (r.imported_skill && !g.items[j].imported_skill) g.items[j] = r;
  });
  return gs;
}
const discAct = r => r.imported_skill ? `<span class="pill ${E(r.status)}">${E(r.status || "imported")}</span>` : r.path ? `<button type="button" class="ag-add" onclick="event.stopPropagation();AG.dAdd(${J(r.id)},${!!r.license_ok},${J(r.license || "")})">${ico("plus")}Add</button>` : '<span class="ag-sub">repo or list</span>';
V.Discover = async function () {
  skel("Discover");
  let d;
  try { d = await api(`discover?mode=${encodeURIComponent(S.dm)}&q=${encodeURIComponent(S.dq)}&kind=${encodeURIComponent(S.dk)}`); if (d && d.error) throw new Error(d.error); }
  catch (e) { return `<div class="ag-page">${head("Discover", "")}${failed(e)}</div>`; }
  const st = obj(d.status), gs = DISC.groups = discGroups(arr(d.rows));
  const lic = r => r.license_ok ? `<span class="ag-sub">${E(r.license)}</span>` : `<span class="badge warn" title="Not on the license allow-list">${E(r.license || "no license")}</span>`;
  const rowHtml = g => { const r = g.h; return `<div class="ag-row click ag-repo" tabindex="0" role="button" onclick="AG.repo(${J(g.repo)})" onkeydown="if(event.key==='Enter'&&event.target===this){event.preventDefault();this.click()}">
    <div class="ag-row-main"><div class="ag-row-t">${E(g.repo)} <a href="https://github.com/${E(g.repo)}" target="_blank" rel="noopener" class="ag-ext" onclick="event.stopPropagation()" aria-label="${E("Open " + g.repo + " on GitHub")}">${ico("ext")}</a></div>
      <div class="ag-sub ag-clamp1">${E(String(r.description || "").slice(0, 180))}</div>
      <div class="ag-items">${g.items.slice(0, 6).map(x => `<span class="ag-item"><b>${E(x.name)}</b><span class="ag-sub">${E(x.kind)}</span>${discAct(x)}</span>`).join("")}${g.items.length > 6 ? `<span class="ag-sub">+${g.items.length - 6} more</span>` : ""}</div></div>
    <div class="ag-repo-m"><div><span class="ag-stars">${ico("star", "ag-i-s")}${E(K.fmt.num(r.stars || 0))}</span>${r.delta_7d > 0 ? `<span class="ag-ok">+${E(r.delta_7d)} this week</span>` : ""}</div>
      <div><span class="ag-sub">${E(K.fmt.num(r.forks || 0))} forks</span>${lic(r)}${r.days_since_push < 9999 ? `<span class="ag-sub">${E(r.days_since_push)}d ago</span>` : ""}</div></div></div>`; };
  return `<div class="ag-page">${head("Discover", "Skills, rules and agent files trending on GitHub. Imports are pinned to a commit and reviewed before any agent can use them.",
    `<button type="button" class="pri" onclick="AG.scan(this)">${ico("refresh")}Scan GitHub now</button>`)}
    <div class="ag-bar">${seg([["trending", "Trending"], ["popular", "All-time popular"]], S.dm, "AG.setDm", "Sort")}
      <label class="ag-search">${ico("search")}<input type="search" id="ag-dq" value="${E(S.dq)}" placeholder="Search skills, repos, topics" aria-label="Search discovered skills" autocomplete="off" oninput="AG.dSearch(this.value)"></label>
      <select aria-label="Kind" onchange="AG.setDk(this.value)">${["", "skill", "plugin", "rules", "agents_md", "list"].map(k => `<option ${k === S.dk ? "selected" : ""} value="${k}">${k ? k.replace("_", ".") : "All kinds"}</option>`).join("")}</select>
      <span class="ag-count">${E(plural(+st.entries || 0, "entry", "entries"))} · ${st.last_run ? "scanned " + E(K.fmt.ago(st.last_run.ts) || when(st.last_run.ts)) : "never scanned"} · ${st.token_ref ? "token " + code(st.token_ref) : badge("no token", "warn")}</span></div>
    ${gs.length ? `<p class="ag-note">${E(plural(gs.length, "repo"))} · ${E(plural(gs.reduce((a, g) => a + g.items.length, 0), "distinct skill, rule or agent file", "distinct skills, rules and agent files"))}</p><div class="ag-list">${gs.slice(0, 60).map(rowHtml).join("")}</div>`
      : S.dq || S.dk ? empty("Discover", "Nothing matches", "Try another search or kind.", [{ label: "Clear filters", run: () => { S.dq = ""; S.dk = ""; render(); } }])
        : empty("Discover", "No discovered skills yet", "Set a token ref (commons skills configure --token-ref env://GITHUB_TOKEN), then scan GitHub. The scheduler refreshes daily.")}
    <p class="ag-note">Upstream changes arrive as new pending versions with diffs. Imports never auto-activate: the review gate checks the license allow-list and scans for secrets and prompt injection.</p></div>`;
};
async function repoBody(repo) {
  let g = DISC.groups.find(x => x.repo === repo);
  if (!g) { const d = await api(`discover?mode=${encodeURIComponent(S.dm)}&q=${encodeURIComponent(repo)}`); g = discGroups(arr(obj(d).rows)).find(x => x.repo === repo); }
  if (!g) throw new Error("That repo is no longer in the discovery list");
  const r = g.h;
  return `<div class="ag-dr"><p class="ag-dr-desc">${E(r.description || "")}</p>
    <dl class="ag-facts"><div class="ag-fact"><dt>Stars</dt><dd>${E(K.fmt.num(r.stars || 0))} <span class="ag-sub">+${E(r.delta_1d || 0)} today · +${E(r.delta_7d || 0)} this week</span></dd></div>
      <div class="ag-fact"><dt>Forks</dt><dd>${E(r.forks || 0)}</dd></div><div class="ag-fact"><dt>License</dt><dd>${r.license_ok ? E(r.license) : `<span class="badge warn">${E(r.license || "none")}</span> <span class="ag-sub">not on the allow-list</span>`}</dd></div>
      <div class="ag-fact"><dt>Updated</dt><dd>${r.days_since_push < 9999 ? E(r.days_since_push) + " days ago" : "unknown"}</dd></div></dl>
    <p><a href="https://github.com/${E(repo)}" target="_blank" rel="noopener">${ico("ext", "ag-i-s")}Open on GitHub</a></p>
    <section class="ag-dr-sec"><h4>What it offers <span class="ag-n">${g.items.length}</span></h4><div class="ag-list">${g.items.map(x => `<div class="ag-row"><div class="ag-row-main"><div class="ag-row-t"><a href="${E(safeUrl(x.url))}" target="_blank" rel="noopener">${E(x.name)}</a> ${badge(x.kind)}</div><div class="ag-sub ag-ellip">${E(x.path || "")}</div></div><div class="ag-acts ag-acts-on">${discAct(x)}</div></div>`).join("")}</div></section></div>`;
}
const openRepo = repo => drawer({ id: "repo:" + repo, title: repo, subtitle: "GitHub repository", render: () => repoBody(repo) });
K.drawer.on("repo:", id => openRepo(id.slice(5)));

/* =================================================================================================================
   MCP hub
   ================================================================================================================= */
const MCP = { d: null };
const mcpDot = s => s === "up" ? "live" : s === "down" ? "ag-down" : "idle";
function parseArgs(a) { try { const o = JSON.parse(a); return o && typeof o === "object" ? kvs(o) : code(String(a).slice(0, 160)); } catch (e) { return code(String(a || "").slice(0, 160)); } }
V["MCP hub"] = async function () {
  skel("MCP hub");
  let m;
  try { m = await api("mcp"); if (m && m.error) throw new Error(m.error); } catch (e) { return `<div class="ag-page">${head("MCP hub", "")}${failed(e)}</div>`; }
  MCP.d = m;
  const servers = arr(m.servers), tools = arr(m.tools), pol = arr(m.policies), appr = arr(m.approvals), calls = arr(m.calls), cat = arr(m.catalog);
  const tabs = [["tools", "Tools", tools.length], ["policies", "Policies", pol.length], ["calls", "Call log", calls.length], ["catalog", "Catalog", cat.length]];
  if (!tabs.some(t => t[0] === S.mcpTab)) S.mcpTab = servers.length ? "tools" : "catalog";
  const srv = servers.map(x => `<div class="ag-srv" tabindex="0" role="button" aria-label="${E("Open " + x.name)}" onclick="AG.server(${J(x.name)})" onkeydown="if(event.key==='Enter'&&event.target===this){event.preventDefault();this.click()}">
    <div class="ag-srv-h"><span class="status-dot ${mcpDot(x.status)}"></span><b>${E(x.name)}</b>${badge(x.transport || "")}<span class="ag-sub ag-srv-s">${E(x.status || "unknown")}</span></div>
    <div class="ag-srv-n"><b>${E(x.tools || 0)}</b> tools <span class="ag-sub">· ${E(x.calls_24h || 0)} calls in 24h · ${E(plural(+x.restarts || 0, "restart"))}</span></div>
    ${x.last_error ? `<div class="ag-bad ag-clamp1">${ico("warn", "ag-i-s")}${E(String(x.last_error).slice(0, 140))}</div>` : ""}
    <div class="ag-srv-a"><button type="button" onclick="event.stopPropagation();AG.health(${J(x.name)},this)">${ico("pulse")}Health check</button><button type="button" class="ag-ghost-bad" onclick="event.stopPropagation();AG.mcpRemove(${J(x.name)})" aria-label="${E("Remove " + x.name)}">${ico("trash")}Remove</button></div></div>`).join("");
  const toolsHtml = `<div class="ag-bar">${searchBox("mtools", "Search tools")}</div><div class="ag-list" data-fscope="mtools">${tools.slice(0, 150).map(t => `<div class="ag-row" ${fattr("mtools", t.server + " " + t.name + " " + t.description + " " + t.risk)}><div class="ag-row-main"><div class="ag-row-t">${code(t.server + "__" + t.name)}</div><div class="ag-sub ag-clamp1">${E(t.description || "")}</div></div><span class="pill ${E(t.risk)}">${E(t.risk)}</span></div>`).join("") || '<div class="ag-quiet">No tools discovered yet.</div>'}</div>${noMatch("mtools")}`;
  const polHtml = `<div class="ag-list">${pol.map(p => `<div class="ag-row"><span class="pill ${p.effect === "deny" ? "denied" : p.effect === "approve" ? "pending" : "ok"}">${E(p.effect)}</span><div class="ag-row-main"><div class="ag-row-t">${code(p.server + "__" + p.tool)}</div><div class="ag-sub">agent ${E(p.agent)}</div></div></div>`).join("") || '<div class="ag-quiet">Defaults apply: read and write tools are allowed, destructive tools need approval.</div>'}</div>
    <div class="ag-form ag-polform"><label class="ag-fld"><span>Effect</span><select id="pe"><option>deny</option><option>approve</option><option>allow</option></select></label>
      <label class="ag-fld"><span>Agent</span><input id="pa" type="text" placeholder="agent or *"></label><label class="ag-fld"><span>Server</span><input id="ps" type="text" placeholder="server or *"></label>
      <label class="ag-fld"><span>Tool</span><input id="pt" type="text" placeholder="tool glob, e.g. delete_*"></label><button type="button" onclick="AG.policy(this)">${ico("plus")}Add rule</button></div>`;
  const callsHtml = `<div class="ag-list ag-calls">${calls.map(c => `<div class="ag-row"><span class="ag-sub ag-call-t" title="${E(c.ts)}">${E(K.fmt.ago(c.ts) || c.ts)}</span><div class="ag-row-main"><div class="ag-row-t">${code(c.server + "__" + c.tool)} <span class="ag-sub">${E(c.agent || "")}</span></div><div class="ag-sub ag-ellip">${E(String(c.args || "").slice(0, 120))}</div></div>
    <span class="ag-call-s"><span class="pill ${E(c.status)}">${E(c.status)}</span>${c.cached ? ' <span class="pill">cache</span>' : ""}</span><span class="ag-sub ag-call-ms">${c.ms ? E(c.ms) + " ms" : ""}</span></div>`).join("") || '<div class="ag-quiet">No calls yet. Arguments are redacted in this log.</div>'}</div>`;
  const catHtml = `<div class="ag-bar">${searchBox("mcat", "Search the catalog")}<span class="ag-count">v${E(m.catalog_version)} · extend in ${code("~/.commons/mcp_catalog.json")}</span></div>
    <div class="ag-list" data-fscope="mcat">${cat.map(c => `<div class="ag-row" ${fattr("mcat", c.id + " " + c.title + " " + c.transport + " " + (c.auth || ""))}><div class="ag-row-main"><div class="ag-row-t">${E(c.title || c.id)} ${c.verified ? badge("verified", "ok") : badge("community")}</div><div class="ag-sub">${code(c.id)} · ${E(c.transport)}${c.auth ? " · " + E(c.auth) : ""}</div></div>
      <a class="ag-ext" href="${E(safeUrl(c.docs))}" target="_blank" rel="noopener" aria-label="${E("Docs for " + c.title)}">${ico("ext")}<span>Docs</span></a><div class="ag-acts ag-acts-on"><button type="button" onclick="AG.mcpAddOpen(${J(c.id)})">${ico("plus")}Add</button></div></div>`).join("")}</div>${noMatch("mcat")}
    <p class="ag-note">Verified presets were checked against each vendor's own docs.</p>`;
  const panes = { tools: toolsHtml, policies: polHtml, calls: callsHtml, catalog: catHtml };
  const out = `<div class="ag-page">${head("MCP hub", "Upstream MCP servers your agents reach through Commons, with per-tool policies, approvals and a redacted call log.",
    `<button type="button" class="${appr.length ? "" : "pri"}" onclick="AGS.focusCat=1;AG.mcpTab('catalog')">${ico("plus")}Add a server</button>`)}
    ${appr.length ? sec("Waiting for your approval", `<div class="ag-list ag-decide">${appr.map(a => `<div class="ag-row ag-dec on"><div class="ag-row-main"><div class="ag-row-t">${code(a.server + "__" + a.tool)} ${a.agent ? `<span class="ag-sub">requested by</span> ${E(a.agent)}` : ""}</div><div class="ag-dec-ev">${parseArgs(a.args)}</div></div>
      <div class="ag-acts ag-acts-on"><button type="button" class="pri" onclick="AG.approve(${J(a.id)},true,this)">${ico("check")}Approve</button><button type="button" class="bad" onclick="AG.approve(${J(a.id)},false,this)">Deny</button></div></div>`).join("")}</div>`, { n: appr.length, sub: "Destructive tool calls wait here until you decide" }) : ""}
    ${sec("Servers", servers.length ? `<div class="ag-srvgrid">${srv}</div>` : empty("MCP hub", "No upstream MCP servers yet", "Add one from the catalog below, or import your existing configs with commons mcp import."),
      { n: servers.length || null, sub: appr.length ? "" : "No approvals waiting" })}
    <section class="ag-sec"><div class="tabs" role="tablist" aria-label="MCP hub sections">${tabs.map(([k, l, c]) => `<button type="button" role="tab" class="tabs-item ${k === S.mcpTab ? "on" : ""}" aria-selected="${k === S.mcpTab}" onclick="AG.mcpTab(${J(k)})">${E(l)} <span class="ag-n">${E(c)}</span></button>`).join("")}</div>
      <div role="tabpanel">${panes[S.mcpTab]}</div></section></div>`;
  ["mtools", "mcat"].forEach(k => { if (S.q[k]) setTimeout(() => applyFind(k)); });
  if (S.focusCat) { S.focusCat = 0; setTimeout(() => { const i = $("#agq-mcat"); if (i) { i.focus(); i.scrollIntoView({ block: "center" }); } }); }
  return out;
};
async function serverBody(name) {
  const m = MCP.d && arr(MCP.d.servers).some(x => x.name === name) ? MCP.d : (MCP.d = await api("mcp"));
  const x = arr(m.servers).find(s => s.name === name);
  if (!x) throw new Error("That server is no longer connected");
  const tools = arr(m.tools).filter(t => t.server === name), calls = arr(m.calls).filter(c => c.server === name).slice(0, 10);
  const pol = arr(m.policies).filter(p => p.server === name || p.server === "*");
  return `<div class="ag-dr"><div class="ag-dr-badges"><span class="ag-state"><span class="status-dot ${mcpDot(x.status)}"></span>${E(x.status || "unknown")}</span>${badge(x.transport || "")}<span class="ag-sub">${E(x.calls_24h || 0)} calls in 24h · ${E(plural(+x.restarts || 0, "restart"))}</span></div>
    ${x.last_error ? `<div class="ag-findings">${ico("warn")}<div>${E(x.last_error)}</div></div>` : ""}
    <section class="ag-dr-sec"><h4>Tools <span class="ag-n">${tools.length}</span></h4><div class="ag-list">${tools.map(t => `<div class="ag-row"><div class="ag-row-main"><div class="ag-row-t">${code(t.name)}</div><div class="ag-sub">${E(t.description || "")}</div></div><span class="pill ${E(t.risk)}">${E(t.risk)}</span></div>`).join("") || '<div class="ag-quiet">No tools discovered. Run a health check.</div>'}</div></section>
    <section class="ag-dr-sec"><h4>Policies that apply</h4>${pol.length ? `<div class="ag-list">${pol.map(p => `<div class="ag-row"><span class="pill ${p.effect === "deny" ? "denied" : p.effect === "approve" ? "pending" : "ok"}">${E(p.effect)}</span><span>${code(p.server + "__" + p.tool)} <span class="ag-sub">agent ${E(p.agent)}</span></span></div>`).join("")}</div>` : '<p class="ag-sub">Defaults: read and write allowed, destructive needs approval.</p>'}</section>
    <section class="ag-dr-sec"><h4>Recent calls</h4>${calls.length ? `<div class="ag-list">${calls.map(c => `<div class="ag-row"><span class="pill ${E(c.status)}">${E(c.status)}</span><div class="ag-row-main"><div class="ag-row-t">${code(c.tool)} <span class="ag-sub">${E(c.agent || "")}</span></div></div><span class="ag-sub">${E(K.fmt.ago(c.ts) || "")}${c.ms ? " · " + E(c.ms) + " ms" : ""}</span></div>`).join("")}</div>` : '<p class="ag-sub">No calls yet.</p>'}</section></div>`;
}
const openServer = name => drawer({ id: "mcp:" + name, title: name, subtitle: "MCP server", render: () => serverBody(name),
  actions: [{ label: "Health check", run: () => AG.health(name) }, { label: "Remove", kind: "danger", run: () => AG.mcpRemove(name) }] });
K.drawer.on("mcp:", id => openServer(id.slice(4)));
async function addBody(id) {
  const m = MCP.d || (MCP.d = await api("mcp")), c = arr(m.catalog).find(x => x.id === id);
  if (!c) throw new Error("Unknown catalog entry");
  return `<div class="ag-dr"><div class="ag-dr-badges">${c.verified ? badge("verified", "ok") : badge("community")}${badge(c.transport)}${c.auth ? `<span class="ag-sub">${E(c.auth)}</span>` : ""}</div>
    ${c.notes ? `<p class="ag-dr-desc">${E(c.notes)}</p>` : ""}<p><a href="${E(safeUrl(c.docs))}" target="_blank" rel="noopener">${ico("ext", "ag-i-s")}Vendor docs</a></p>
    <form class="ag-dr-form" onsubmit="event.preventDefault();AG.mcpAdd(${J(c.id)})">
      ${arr(c.params).map(p => `<label class="ag-fld"><span>${E(p)}</span><input type="text" id="mp_${E(p)}" placeholder="${E(p)}" autocomplete="off"></label>`).join("")}
      ${arr(c.secrets).map(x => `<label class="ag-fld"><span>${E(x)} secret reference</span><input type="text" id="mr_${E(x)}" value="env://${E(x)}" autocomplete="off"><i class="ag-sub">A reference only (env://, vault://, op://). Never paste a raw key.</i></label>`).join("")}
      ${!arr(c.params).length && !arr(c.secrets).length ? '<p class="ag-sub">Nothing to fill in. Connect it and Commons discovers its tools.</p>' : ""}
      <button type="submit" class="pri">${ico("plus")}Connect ${E(c.title || c.id)}</button></form></div>`;
}
const openAdd = id => drawer({ id: "mcpadd:" + id, title: "Add " + ((MCP.d && (arr(MCP.d.catalog).find(x => x.id === id) || {}).title) || id), subtitle: "From the MCP catalog", render: () => addBody(id) });
K.drawer.on("mcpadd:", id => openAdd(id.slice(7)));

/* =================================================================================================================
   Handlers called from the markup
   ================================================================================================================= */
let dT = 0;
window.AG = {
  find(el, key) { S.q[key] = el.value; applyFind(key); },
  agent: openAgent, skill: openSkill, repo: openRepo, server: openServer, mcpAddOpen: openAdd,
  setLive(v) { S.live = v; render(); }, setPlat(v) { S.plat = v; render(); },
  setRange(v) { S.range = +v; render(); }, setAgent(v) { S.agent = v; render(); }, setBy(v) { S.by = v; render(); },
  setDm(v) { S.dm = v; render(); }, setDk(v) { S.dk = v; render(); },
  dSearch(v) { S.dq = v; clearTimeout(dT); dT = setTimeout(() => render(), 300); },
  mcpTab(k) { S.mcpTab = k; render(); },
  job(id) { location.hash = "#Workflow?drawer=job:" + encodeURIComponent(id); },
  async update(agent, btn, inDrawer) {
    busy(btn, "Queuing…");
    const r = await act("agent/update", { agent }, `Update queued for ${agent}: it runs within 10 minutes`, { rerender: !inDrawer });
    idle(btn);
    if (r && inDrawer) { openAgent(agent); render(); }
  },
  async send(to, btn, ta) {
    ta = ta || $("#ag-msg");
    const text = ta ? ta.value.trim() : "";
    if (!text) { K.toast("Write the message first", { kind: "err" }); if (ta) ta.focus(); return; }
    busy(btn, "Sending…");
    const r = await act("msg/send", { to, text }, `Sent to ${to}`, { rerender: false });
    idle(btn);
    if (r) openAgent(to);
  },
  async sendTo(btn) {
    const to = ($("#ag-mto") || {}).value || "*", inp = $("#ag-mtx"), text = inp ? inp.value.trim() : "";
    if (!text) { K.toast("Write the message first", { kind: "err" }); if (inp) inp.focus(); return; }
    S.to = to;
    if (await act("msg/send", { to, text }, to === "*" ? "Sent to every agent" : `Sent to ${to}`) && inp) inp.value = "";
  },
  async redrive(btn) { busy(btn, "Redriving…"); const r = await act("wake/redrive", {}, null); idle(btn); if (r) K.toast(`${r.redriven || 0} wakes sent again`, { kind: "ok" }); },
  seats(cb) {
    const on = cb.checked, b = $("#ag-seat-b"), was = b ? [b.textContent, b.className] : null;
    K.optimistic({
      apply: () => { if (b) { b.textContent = on ? "On" : "Off"; b.className = "badge " + (on ? "ok" : ""); } },
      revert: () => { cb.checked = !on; if (b && was) { b.textContent = was[0]; b.className = was[1]; } },
      commit: async () => { const r = await postRaw("setup/setting", { key: "api_seats", value: on }); if (r && !r.error) render(); return r; },
      label: on ? "API seats turned on" : "API seats turned off",
    });
  },
  async ptest(name, btn) {
    S.ptest[name] = { pending: true };
    const el = document.getElementById("ag-pt-" + name);
    if (el) el.innerHTML = '<span class="ag-sub">Testing…</span>';
    busy(btn, "Testing…");
    let r;
    try { r = await postRaw("setup/provider_test", { kind: name }); } catch (e) { r = { ok: false, error: errMsg(e) }; }
    idle(btn);
    S.ptest[name] = r && !r.error ? r : { ok: false, error: (r && r.error) || "failed" };
    render();
  },
  async refreshModels(btn) { busy(btn, "Refreshing…"); await act("models/refresh", {}, "Model lists refreshed and routing rebuilt"); idle(btn); },
  async decide(id, approve, btn) { busy(btn, approve ? "Applying…" : "Rejecting…"); await act("routing/decide", { id, approve }, approve ? "Routing updated" : "Proposal rejected"); idle(btn); },
  async cxKey(btn) {
    const name = ($("#cxn").value || "").trim(), role = $("#cxr").value, project = ($("#cxp").value || "").trim();
    if (!name) { K.toast("Give the agent a name first", { kind: "err" }); $("#cxn").focus(); return; }
    busy(btn, "Creating…");
    const r = await postRaw("connect/key", { name, role, project, platform: cx.platform, url: cx.url }).catch(e => ({ error: errMsg(e) }));
    idle(btn);
    if (!r || r.error) { K.toast((r && r.error) || "Could not create the key", { kind: "err" }); return; }
    Object.assign(cx, { name, role, project, key: r.api_key, b: r.bundle, watch: name });
    K.toast(`Key created for ${name}`, { kind: "ok" });
    render();
  },
  cxCopy(i) { const x = CX.blocks[i]; if (!x) return; K.copy(x.kind === "setup" ? x.base + (cx.skip || []).map(k => " --no-" + k).join("") : x.text); },
  async skillReview(name, version, approve, btn) {
    busy(btn, approve ? "Approving…" : "Rejecting…");
    const r = await act("skill/review", { name, version, approve }, approve ? `${name} v${version} approved` : `${name} v${version} rejected`);
    idle(btn); if (r) openSkill(name);
  },
  async skillRollback(name, version, btn) {
    busy(btn, "Rolling back…");
    const r = await act("skill/rollback", { name, version }, `${name} rolled back to v${version}`);
    idle(btn); if (r) openSkill(name);
  },
  async dAdd(id, ok, license) {
    if (!ok && !confirm(`License ${license || "(none)"} is not on the allow-list. Import anyway after checking its terms?`)) return;
    if (!await act("discover/add", { id, allow_license: !ok }, "Added to the pool as a pending version for review")) return;
    const d = new URLSearchParams(location.hash.split("?")[1] || "").get("drawer");
    if (d && d.startsWith("repo:")) { DISC.groups = []; openRepo(d.slice(5)); }
  },
  async scan(btn) { busy(btn, "Scanning…"); await act("discover/run", {}, "GitHub scan finished"); idle(btn); },
  async approve(id, approve, btn) { busy(btn, approve ? "Approving…" : "Denying…"); await act("mcp/approve", { id, approve }, approve ? "Approved: the call runs now" : "Denied"); idle(btn); },
  async health(name, btn) { busy(btn, "Checking…"); const r = await act("mcp/health", { name }, null); idle(btn); if (r) K.toast(`${name}: health check done`, { kind: "ok" }); },
  async mcpRemove(name) {
    if (!confirm(`Remove ${name}? Agents lose its tools until you add it again.`)) return;
    if (await act("mcp/remove", { name }, `${name} removed`)) K.drawer.close();
  },
  async policy(btn) {
    const v = id => ($("#" + id) || {}).value || "";
    busy(btn, "Adding…");
    await act("mcp/policy", { effect: v("pe"), agent: v("pa"), server: v("ps"), tool: v("pt") }, "Policy rule added");
    idle(btn);
  },
  async mcpAdd(id) {
    const c = arr(obj(MCP.d).catalog).find(x => x.id === id);
    if (!c) return;
    const params = {}, refs = {};
    arr(c.params).forEach(p => { const el = document.getElementById("mp_" + p); if (el && el.value) params[p] = el.value; });
    arr(c.secrets).forEach(x => { const el = document.getElementById("mr_" + x); if (el && el.value) refs[x] = el.value; });
    if (await act("mcp/add_preset", { preset: id, params, refs }, `${c.title || id} connected; discovering its tools`)) K.drawer.close();
  },
};
})();
