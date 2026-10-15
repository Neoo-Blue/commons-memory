/* Home workspace: Overview (the owner's cockpit) and Board. Reassigns V["Overview"] and V["Board"].
   Drawers deep-link as #Page?drawer=agent:<name> | job:<id> | board:<id>. Everything lives in this IIFE: generated
   buttons carry data-hm="<action>" and one delegated listener runs them, so nothing leaks into the shared script. */
(() => {
"use strict";
const SYS = ["cli", "user", "librarian", "commons", "dashboard-user", "reviewer"];  // status.SYSTEM on the hub
const OPEN = ["open", "doing", "blocked"];
const APPROVAL = /\[(vault|mcp|gate|rework) ([A-Za-z0-9_]+)\]/;  // approvals.QUESTION: these need the owner's page
const APPROVAL_LABEL = { vault: "Key use", mcp: "Tool call", gate: "Workflow gate", rework: "Rework limit" };
const KINDS = ["ask", "target", "expectation", "action", "delivered", "question"];
const S = {
  ans: {},      // question id -> {open, draft}: "Answer differently" boxes survive live re-renders
  gone: {},     // question ids answered but not yet committed (inside the Undo window)
  pend: {},     // board item id -> status shown while its change waits in the Undo window
  st: "open", pj: "", q: "",  // Board filters
  compose: false, cd: { kind: "ask", text: "", project: "", path: "" },
  actAll: false,
  evd: {},      // board item id -> unsaved evidence typed in its drawer
  board: null, agents: [], team: [],
};
const arr = x => Array.isArray(x) ? x : [];
const ago = t => K.fmt.ago(t);
const cap = s => { s = String(s || ""); return s ? s[0].toUpperCase() + s.slice(1) : ""; };

/* ---------- icons (24px grid, 1.7 stroke via CSS) ---------- */
const P = {
  check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  x: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  play: '<path d="M8.5 6v12l9.5-6z"/>',
  undo: '<path d="M9 14 4.5 9.5 9 5"/><path d="M4.5 9.5h10a5 5 0 0 1 0 10H11"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  chev: '<path d="m9.5 6 6 6-6 6"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  doc: '<path d="M6.5 3.5h7.5l4 4v13h-11.5z"/><path d="M14 3.5v4h4M9.5 12h5M9.5 15.5h3.5"/>',
  board: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M8 9h8M8 12.5h5M8 16h6.5"/>',
  user: '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c.8-3.6 3.6-5.5 7-5.5s6.2 1.9 7 5.5"/>',
  key: '<circle cx="8" cy="15.5" r="3.5"/><path d="m10.6 13 8.4-8.5M16.5 7l2.5 2.5M14.5 9l1.8 1.8"/>',
  plug: '<path d="M9 3.5v4M15 3.5v4M7 7.5h10v3a5 5 0 0 1-10 0z"/><path d="M12 15.5v5"/>',
  book: '<path d="M4.5 5h5a2.5 2.5 0 0 1 2.5 2.5V20a2 2 0 0 0-2-2H4.5z"/><path d="M19.5 5h-5A2.5 2.5 0 0 0 12 7.5V20a2 2 0 0 1 2-2h5.5z"/>',
  tag: '<path d="M3.5 12V4.5H11l9 9-7.5 7.5z"/><circle cx="7.8" cy="8.8" r="1.2"/>',
  folder: '<path d="M3.5 7a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
  spark: '<path d="M12 3.5l1.9 5.6 5.6 1.9-5.6 1.9L12 18.5l-1.9-5.6L4.5 11l5.6-1.9z"/>',
  layers: '<path d="m12 4.5 8 4.2-8 4.3-8-4.3z"/><path d="m4 12.8 8 4.2 8-4.2"/><path d="m4 16.5 8 4.2 8-4.2" opacity=".5"/>',
  shield: '<path d="M12 3.5 19 6v5.5c0 4.4-2.9 7.6-7 9-4.1-1.4-7-4.6-7-9V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
  bot: '<rect x="4.5" y="8" width="15" height="11" rx="3"/><path d="M12 4.5V8M9.5 13v1.5M14.5 13v1.5"/>',
  move: '<path d="M4 8h13M13.5 4.5 17 8l-3.5 3.5M20 16H7M10.5 12.5 7 16l3.5 3.5"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
  ext: '<path d="M13.5 4.5h6v6M19.5 4.5 11 13M18 14v5.5H4.5V6H10"/>',
  dot: '<circle cx="12" cy="12" r="2.5"/>',
  calm: '<circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.3 2.4 2.4 4.6-4.9"/>',
  up: '<path d="M12 18.5v-13M6.5 11 12 5.5l5.5 5.5"/>',
  down: '<path d="M12 5.5v13M6.5 13 12 18.5l5.5-5.5"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
  // board statuses
  s_open: '<circle cx="12" cy="12" r="7.5"/>',
  s_doing: '<circle cx="12" cy="12" r="7.5"/><path d="M12 4.5a7.5 7.5 0 0 1 0 15z" fill="currentColor" stroke="none"/>',
  s_blocked: '<circle cx="12" cy="12" r="7.5"/><path d="m7 17 10-10"/>',
  s_done: '<circle cx="12" cy="12" r="8" fill="currentColor" stroke="none"/><path class="hm-tick" d="m8.5 12.2 2.4 2.4 4.6-4.8"/>',
  s_dropped: '<circle cx="12" cy="12" r="7.5"/><path d="M8.5 12h7"/>',
};
const ic = (n, cls = "") => `<svg class="hm-i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${P[n] || P.dot}</svg>`;
const initials = n => { const s = String(n || "?"), p = s.split(/[-_.\s]+/).filter(Boolean); return (p.length > 1 ? p[0][0] + p[1][0] : s.slice(0, 2)).toUpperCase(); };
const hue = n => { let h = 7; for (const c of String(n || "")) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
const av = (n, dot = "", cls = "") => `<span class="hm-av ${cls}" style="--h:${hue(n)}" aria-hidden="true">${E(initials(n))}${dot ? `<span class="status-dot ${dot}"></span>` : ""}</span>`;
const badge = (txt, kind = "") => `<span class="badge ${kind}">${E(txt)}</span>`;
const sec = (title, extra = "", right = "") => `<div class="hm-sec-h"><h2>${title}</h2>${extra}<span class="hm-sp"></span>${right}</div>`;
const link = (label, page) => `<a class="hm-link" href="#${encodeURI(page)}">${E(label)}${ic("chev")}</a>`;

/* ---------- agents: presence dot and what each is doing ---------- */
function presenceOf(a, t) {  // a: /api/agents row, t: /api/team row (either may be missing)
  const ms = a && a.last_seen ? Date.parse(a.last_seen) : NaN, age = ms > 0 ? (Date.now() - ms) / 1000 : Infinity;
  const busy = !!(a && a.claims && a.claims.length) || (t && t.state === "working");
  const dot = busy ? "busy" : (t && t.live) || age < 300 ? "live" : age < 900 ? "idle" : "offline";
  return { dot, live: dot === "live" || dot === "busy", seen: age < Infinity ? ago(a.last_seen) : "never seen" };
}
const STATE = { needs_you: ["Needs you", "warn"], blocked: ["Blocked", "bad"], working: ["Working", "ok"], done: ["Done", "info"], idle: ["Idle", ""] };
function doingOf(a, t) {
  const c = a && a.claims && a.claims[0];
  if (c) return E(c.title) + (a.claims.length > 1 ? ` <span class="hm-mut">+${a.claims.length - 1} more</span>` : "");
  if (t && (t.headline || t.working_on)) return E(t.headline || t.working_on);
  if (a && a.last_activity) return `<span class="hm-mut">Last: ${E(a.last_activity.summary)}</span>`;
  return `<span class="hm-mut">No activity yet</span>`;
}

/* ---------- decision cards (Overview and Board share them) ---------- */
function qCard(x, inDrawer = false) {
  if (S.gone[x.id]) return "";
  const m = APPROVAL.exec(x.text || ""), text = m ? x.text.replace(m[0], "").trim() : x.text || "";
  const who = x.source || (m ? "Commons" : "An agent"), st = S.ans[x.id] || {};
  const meta = [x.project, x.blocks ? "Holds up: " + x.blocks : ""].filter(Boolean).map(E).join(" · ");
  const choices = arr(x.choices);
  let acts;
  if (m) {
    acts = `<a class="hm-btn pri" href="/approve/${encodeURIComponent(x.id)}">${ic("shield")}Review</a>${inDrawer ? "" : `<button type="button" class="hm-ghost" data-hm="board" data-id="${E(x.id)}">Details</button>`}`;
  } else {
    const free = `<div class="hm-q-free"${choices.length && !st.open ? " hidden" : ""}><textarea data-hm-an="${E(x.id)}" rows="2" placeholder="Write your answer" aria-label="Your answer to: ${E(text.slice(0, 80))}">${E(st.draft || "")}</textarea><div class="hm-q-free-acts"><span class="hm-hint">${K.keys.kbd("mod+enter")} to send</span>${choices.length ? `<button type="button" class="hm-ghost" data-hm="ans-toggle" data-id="${E(x.id)}">Cancel</button>` : ""}<button type="button" class="pri" data-hm="ans-send" data-id="${E(x.id)}">Send answer</button></div></div>`;
    acts = choices.length
      ? choices.map(c => `<button type="button" class="hm-choice" data-hm="ans-choice" data-id="${E(x.id)}" data-v="${E(c)}">${E(c)}</button>`).join("")
        + `<button type="button" class="hm-ghost" data-hm="ans-toggle" data-id="${E(x.id)}" aria-expanded="${!!st.open}">Answer differently</button>`
        + free
      : free;
  }
  return `<article class="hm-q${m ? " is-approval" : ""}" data-qid="${E(x.id)}">
<div class="hm-q-head">${m ? `<span class="hm-av hm-av-sys" aria-hidden="true">${ic(m[1] === "vault" ? "key" : m[1] === "mcp" ? "plug" : "layers")}</span>` : av(who)}
<span class="hm-q-who"><b>${E(who)}</b> ${m ? "needs your approval" : "asks"}</span>${m ? badge(APPROVAL_LABEL[m[1]], "warn") : ""}<span class="hm-sp"></span><time class="hm-time" datetime="${E(x.created_at || "")}">${E(ago(x.created_at))}</time></div>
<p class="hm-q-text">${E(text)}</p>${meta ? `<div class="hm-q-meta">${meta}</div>` : ""}
<div class="hm-q-acts">${acts}</div></article>`;
}
function needsSection(qs) {
  qs = qs.filter(x => !S.gone[x.id]);
  if (!qs.length) return `<div class="hm-calm" role="status">${ic("calm")}<span>Nothing needs you right now. Agents ask here when they need a decision.</span></div>`;
  return `<section class="hm-sec hm-needs" aria-label="Needs you">${sec(`Needs you <span class="hm-count warn">${qs.length}</span>`, `<span class="hm-sec-hint">Your answer goes straight to the agent that asked.</span>`)}<div class="hm-qgrid">${qs.map(x => qCard(x)).join("")}</div></section>`;
}
function answer(id, text, from) {
  text = String(text || "").trim();
  if (!text) { K.toast("Write the answer first", { kind: "err" }); const t = from && from.closest(".hm-q") && from.closest(".hm-q").querySelector("textarea"); if (t) t.focus(); return; }
  const cards = () => document.querySelectorAll(`[data-qid="${CSS.escape(id)}"]`);
  K.optimistic({
    label: `Answered: ${text.length > 48 ? text.slice(0, 48) + "…" : text}`,
    apply: () => { S.gone[id] = 1; cards().forEach(c => c.classList.add("is-gone")); },
    revert: () => { delete S.gone[id]; cards().forEach(c => c.classList.remove("is-gone")); },
    commit: () => postRaw("board/answer", { id, text }),
  }).then(r => { if (r === "committed") { delete S.ans[id]; refreshDrawer(); Promise.resolve(render()).finally(() => { delete S.gone[id]; }); } });
}

/* ---------- Overview ---------- */
function skel(page) {
  const m = $("#m");
  if (!m || m.dataset.tab === page) return;
  const tiles = `<div class="hm-kpis">${"<div class=\"hm-kpi\"><div class=\"skeleton skeleton-line\" style=\"width:40%\"></div><div class=\"skeleton skeleton-line\" style=\"height:26px;width:60%\"></div></div>".repeat(4)}</div>`;
  m.innerHTML = `<div class="hm" aria-busy="true">${page === "Overview" ? tiles : ""}<div class="hm-panel">${K.skeleton(6)}</div></div>`;
}
function failed(e) {
  return `<div class="hm"><div class="hm-panel">${K.empty({ icon: "Overview", title: "Could not load this page", hint: String((e && e.message) || e), actions: [{ label: "Try again", run: () => render() }] })}</div></div>`;
}
function setupStrip(su) {
  if (!su || !(su.done < su.total)) return "";
  const next = (arr(su.steps).find(x => !x.done) || {}).step || "", pct = Math.round(100 * su.done / su.total);
  const href = /^Owner password/.test(next) ? "/approve/pin" : "#Setup";
  return `<div class="hm-setup"><div class="hm-ring" style="--p:${pct}" role="img" aria-label="${su.done} of ${su.total} setup steps done"><span>${su.done}/${su.total}</span></div>
<div class="hm-setup-t"><b>Finish setting up Commons</b><span>Next: ${E(next)}</span></div><a class="hm-btn" href="${href}">Continue setup${ic("arrow")}</a></div>`;
}
function kpi(label, value, foot, page, extra = "") {
  return `<a class="hm-kpi" href="#${encodeURI(page)}"><span class="hm-kpi-l">${label}</span><span class="hm-kpi-v">${value}</span><span class="hm-kpi-f">${foot}</span>${extra}</a>`;
}
function jobProgress(j) {
  const subs = arr(j.subtasks), done = subs.filter(s => s.status === "done").length;
  const cur = subs.find(s => s.status === "claimed" || s.status === "awaiting_gate") || subs.find(s => s.status === "failed") || subs.find(s => s.status === "pending");
  return { subs, done, total: subs.length, cur };
}
const STEP = { pending: ["Queued", ""], claimed: ["In progress", "info"], awaiting_gate: ["Awaiting check", "warn"], done: ["Done", "ok"], failed: ["Failed", "bad"], cancelled: ["Cancelled", ""] };
function jobRow(j) {
  const p = jobProgress(j), pct = p.total ? Math.round(100 * p.done / p.total) : 0, st = p.cur ? STEP[p.cur.status] || [p.cur.status, ""] : null;
  return `<button type="button" class="hm-job" data-hm="job" data-id="${E(j.job_id)}">
<span class="hm-job-top"><span class="hm-job-t">${E(j.title)}</span><span class="hm-job-pat">${E(String(j.pattern || "").replace(/_/g, " "))}</span></span>
<span class="progress" role="progressbar" aria-label="Steps done" aria-valuemin="0" aria-valuemax="${p.total}" aria-valuenow="${p.done}"><span class="progress-bar" style="width:${pct}%"></span></span>
<span class="hm-job-sub"><span class="hm-job-n"><span class="hm-num">${p.done}/${p.total}</span> steps</span>${p.cur ? `<span class="hm-job-now"><span class="hm-job-cur">${E(p.cur.title || p.cur.key)}</span>${p.cur.agent ? `<span class="hm-mut">${E(p.cur.agent)}</span>` : ""}${badge(st[0], st[1])}</span>` : ""}</span>
${ic("chev", "hm-job-go")}</button>`;
}
const VERB = { "agent.register": "Agent connected", "board.add": "Added to the board", "board.update": "Board item updated", "board.answer": "Question answered",
  "board.repeat": "Asked again", "release.announce": "Release announced", "librarian.run": "Librarian ran", "approvals.pin_set": "Owner password set",
  "skill.version": "Skill version published", "skills.import.local": "Skills imported", "drive.write": "File written", "job.retry": "Job retried",
  "job.cancel": "Job cancelled", "subtask.retry": "Step retried", "subtask.reassign": "Step moved", "killswitch": "Kill switch" };
function actIcon(t) {
  return /^(memory|fact|decision|observation|handoff|preference|document)$/.test(t) ? "doc" : /^board\./.test(t) ? "board" : /^agent\./.test(t) ? "user"
    : /^(job|subtask|workflow)\./.test(t) ? "layers" : /^skill/.test(t) ? "spark" : /^mcp\./.test(t) ? "plug" : /^(approvals|vault|trust)\./.test(t) ? "key"
    : /^librarian/.test(t) ? "book" : /^release/.test(t) ? "tag" : /^drive/.test(t) ? "folder" : "dot";
}
function actText(e, names, items, jobs) {
  const s = String(e.text || "");
  if (/^\s*\{/.test(s)) { try { const o = JSON.parse(s); if (o && typeof o === "object") return K.kv(o); } catch (err) { /* plain text */ } }
  if (/^agt_\w+$/.test(s) && names[s]) return `<b>${E(names[s])}</b>`;
  if (/^brd_\w+$/.test(s) && items[s]) return E(items[s].replace(APPROVAL, "").trim());
  if (/^job_\w+$/.test(s) && jobs[s]) return E(jobs[s]);
  return /^[a-z]{3}_[0-9a-f]{12,}$/.test(s) ? `<code class="hm-id">${E(s)}</code>` : E(s);
}
function activity(act, names, items, jobs) {
  const rows = [];
  for (const e of arr(act)) {  // one line for the same event repeated back to back
    const last = rows[rows.length - 1];
    if (last && last.type === e.type && last.text === e.text && last.agent === e.agent) { last.n++; continue; }
    rows.push(Object.assign({ n: 1 }, e));
  }
  if (!rows.length) return K.empty({ icon: "Overview", title: "Quiet so far", hint: "Activity from agents, the librarian and tool calls streams in here." });
  const shown = S.actAll ? rows : rows.slice(0, 8);
  return `<ol class="hm-feed">${shown.map(e => {
    const mem = /^(memory|fact|decision|observation|handoff|preference|document)$/.test(e.type);
    const verb = VERB[e.type] || cap(String(e.type).replace(/[._]/g, " "));
    return `<li><span class="hm-feed-i">${ic(actIcon(e.type))}</span><div class="hm-feed-b"><div class="hm-feed-h"><span class="hm-feed-v">${E(verb)}</span>${e.agent ? `<span class="hm-mut">${E(e.agent)}</span>` : mem ? "" : `<span class="hm-mut">Commons</span>`}${e.n > 1 ? `<span class="hm-x">×${e.n}</span>` : ""}<span class="hm-sp"></span><time class="hm-time">${E(ago(Date.now() - (+e.ago || 0) * 1000))}</time></div><div class="hm-feed-t">${actText(e, names, items, jobs)}</div></div></li>`;
  }).join("")}</ol>${rows.length > 8 ? `<button type="button" class="hm-more" data-hm="act-more" aria-expanded="${S.actAll}">${S.actAll ? "Show less" : `Show ${rows.length - 8} more`}</button>` : ""}`;
}

V["Overview"] = async function () {
  skel("Overview");
  try {
    const [o, act, us, ag, su, tm, jobs, bd] = await Promise.all([api("overview"), api("activity").catch(() => []), api("usage_series?days=14").catch(() => null),
      api("agents").catch(() => []), api("setup").catch(() => null), api("team").catch(() => []), api("jobs?status=running").catch(() => []), api("board").catch(() => null)]);
    if (!o || o.error) throw new Error((o && o.error) || "The hub did not answer");
    S.agents = arr(ag); S.team = arr(tm); if (bd && bd.items) S.board = bd;
    S.lastJobs = arr(jobs);
    const byName = Object.fromEntries(S.agents.map(a => [a.name, a])), names = Object.fromEntries(S.agents.map(a => [a.id, a.name]));
    const items = Object.fromEntries(arr(bd && bd.items).map(x => [x.id, x.text])), runJobs = arr(jobs).filter(j => j.status === "running");
    const jobT = Object.fromEntries(runJobs.map(j => [j.job_id, j.title]));
    // spend: the last bucket of usage_series is the last 24 h, the one before it the 24 h before that
    const series = us && us.series ? Object.values(us.series) : [], days = (us && us.days) || 0;
    const cost = [...Array(days).keys()].map(d => series.reduce((a, v) => a + (+v[d] || 0), 0));
    const today = +o.spent_24h || 0, prev = cost.length > 1 ? cost[cost.length - 2] : 0;
    const delta = prev > 0 ? Math.round(100 * (today - prev) / prev) : null;
    const spendFoot = delta === null ? "No spend the day before" : `<span class="hm-delta">${ic(delta >= 0 ? "up" : "down")}${Math.abs(delta)}%</span> vs the previous 24 h`;
    const teamRows = S.team.map(t => [t.agent, byName[t.agent], t]);
    S.agents.filter(a => !SYS.includes(a.name) && !S.team.some(t => t.agent === a.name)).forEach(a => teamRows.push([a.name, a, null]));
    const live = teamRows.filter(([, a, t]) => presenceOf(a, t).live).length;
    const subs = runJobs.flatMap(j => arr(j.subtasks)), inProg = subs.filter(s => s.status === "claimed").length, queued = subs.filter(s => s.status === "pending").length;
    const asks = arr(bd && bd.items).filter(x => x.kind === "ask"), openAsks = asks.filter(x => OPEN.includes(x.status)).length, rep = asks.reduce((a, x) => a + (x.repeats || 0), 0);
    const tokens = arr(us && us.tokens), tokToday = tokens.length ? tokens[tokens.length - 1] : 0;
    const spark = cost.length ? `<span class="hm-kpi-spark" title="Daily spend, last ${days} days">${K.spark(cost, { w: 132, h: 36 })}</span>` : "";
    const minis = [["Memories", o.items, "Memory"], ["Graph entities", o.entities, "Graph"], ["Relations", o.relations, "Graph"], ["Conflicts", o.conflicts, "Memory", "conflicts"],
      ["Open leftovers", o.open_leftovers, "Sessions"]].map(([l, v, pg, act]) => `<a class="hm-mini${act && +v ? " is-warn" : ""}" href="#${encodeURI(pg)}"${act ? ` data-hm="${act}"` : ""}><span class="hm-mini-v">${E(K.fmt.num(v))}</span><span class="hm-mini-l">${E(l)}</span></a>`).join("")
      + (tokens.length ? `<a class="hm-mini" href="#Usage"><span class="hm-mini-v">${E(K.fmt.num(tokToday))}</span><span class="hm-mini-l">Tokens today</span>${K.spark(tokens, { w: 64, h: 18 })}</a>` : "");
    const sysAgents = S.agents.filter(a => SYS.includes(a.name));
    const team = teamRows.length ? `<div class="hm-team">${teamRows.map(([n, a, t]) => {
      const p = presenceOf(a, t), s = t && STATE[t.state];
      return `<button type="button" class="hm-agent" data-hm="agent" data-name="${E(n)}">${av(n, p.dot)}<span class="hm-agent-b"><span class="hm-agent-h"><b>${E(n)}</b>${s && s[1] ? badge(s[0], s[1]) : `<span class="hm-mut">${E(s ? s[0] : "Idle")} · ${E(p.seen)}</span>`}</span><span class="hm-agent-d">${doingOf(a, t)}</span></span></button>`;
    }).join("")}</div>` : K.empty({ icon: "Agents", title: "No agents connected yet", hint: "Connect one with: commons connect --agent claude-code --platform claude-code", actions: [{ label: "Connect an agent", run: () => go("Connect") }] });
    const sys = sysAgents.length ? `<div class="hm-sys"><span class="hm-mut">System</span>${sysAgents.map(a => `<button type="button" class="hm-sys-a" data-hm="agent" data-name="${E(a.name)}"><span class="status-dot ${presenceOf(a, null).dot}"></span>${E(a.name)}</button>`).join("")}</div>` : "";
    const jobsHTML = runJobs.length ? `<div class="hm-jobs">${runJobs.map(jobRow).join("")}</div>`
      : K.empty({ icon: "Orchestration", title: "No jobs running", hint: "Submit one with commons submit \"Build X\" or the submit_task tool." });
    return `<div class="hm hm-ov">${setupStrip(su)}${needsSection(arr(o.needs_user))}
<section class="hm-kpis" aria-label="Key numbers">
${kpi("Spend, last 24 h", E(K.fmt.money(today)), spendFoot, "Usage", spark)}
${kpi("Running jobs", E(o.running_jobs || 0), `${inProg} in progress · ${queued} queued`, "Orchestration")}
${kpi("Agents live now", `${live}<span class="hm-kpi-of">/${teamRows.length}</span>`, live ? `${teamRows.length - live} idle or away` : "None checked in the last 5 minutes", "Agents")}
${kpi("Open asks", E(openAsks), rep ? `<span class="hm-warn">${rep} asked twice or more</span>` : `${arr(bd && bd.items).filter(x => x.status === "done").length} delivered`, "Board")}
</section><nav class="hm-minis" aria-label="More counts">${minis}</nav>
<section class="hm-sec" aria-label="Team">${sec("Team", `<span class="hm-count">${teamRows.length}</span>`, link("All agents", "Agents"))}${team}${sys}</section>
<div class="hm-cols"><section class="hm-sec" aria-label="Active jobs">${sec("Active jobs", `<span class="hm-count">${runJobs.length}</span>`, link("Orchestration", "Orchestration"))}${jobsHTML}</section>
<section class="hm-sec" aria-label="Activity">${sec("Activity", "", `<span class="hm-live"><span class="status-dot live"></span>Live</span>`)}<div class="hm-panel hm-panel-flush">${activity(act, names, items, jobT)}</div></section></div>
<div class="hm-foot"><span>Database ${E(o.db)}</span><span>Embedder ${E(o.embedder)}</span><span>${o.paused ? `<b class="hm-bad">Paused: ${E(o.paused)}</b>` : "Orchestration running"}</span><span>Press ${K.keys.kbd("?")} for shortcuts</span></div></div>`;
  } catch (e) { return failed(e); }
};

/* ---------- drawers ---------- */
let DR = null;  // the open drawer of ours: {ctx, build}, so actions can refresh it in place
function openDrawer(o) {
  const build = f => Promise.resolve(o.build(f)).then(r => {  // a builder may return {html, title, subtitle} to fix the head
    if (r && typeof r === "object") {
      const el = DR && DR.ctx.el, t = el && el.querySelector(".drawer-title"), st = el && el.querySelector(".drawer-sub");
      if (t && r.title) t.textContent = r.title;
      if (st && r.subtitle) { st.textContent = r.subtitle; st.hidden = false; }
      r = r.html;
    }
    return `<div class="hm-d">${r}</div>`;
  });
  const ctx = K.drawer.open({ id: o.id, title: o.title, subtitle: o.subtitle, actions: o.actions, render: () => build(true), onClose: () => { if (DR && DR.ctx === ctx) DR = null; } });
  DR = { ctx, build };
}
async function refreshDrawer(fresh = true) {
  const d = DR;
  if (!d || !d.ctx.el.isConnected) return;
  try { const h = await d.build(fresh); if (DR === d) d.ctx.body.innerHTML = h; } catch (e) { K.toast(String(e.message || e), { kind: "err" }); }
}
const dsec = (title, body, right = "") => `<section class="hm-dsec"><h3 class="hm-dsec-h">${title}<span class="hm-sp"></span>${right}</h3>${body}</section>`;
const dl = rows => `<dl class="hm-dl">${rows.filter(r => r && r[1] !== "" && r[1] != null).map(([k, v]) => `<dt>${E(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
const none = t => `<p class="hm-none">${E(t)}</p>`;

const WAKE = { instant: "Holds a live stream, so it is woken within a second", "long-poll": "Long-polls the hub, so it is woken within seconds",
  push: "Woken through a webhook or push channel", polling: "Checks in now and then and finds work at its next check", offline: "Not connected: it picks up work when it next starts" };
async function needAgents() {
  if (!S.agents.length) { const [a, t] = await Promise.all([api("agents").catch(() => []), api("team").catch(() => [])]); S.agents = arr(a); S.team = arr(t); }
}
async function agentBody(name) {
  const [d] = await Promise.all([api("agent_detail?name=" + encodeURIComponent(name)), needAgents()]);
  if (!d || d.error) throw new Error((d && d.error) || "Unknown agent");
  const a = S.agents.find(x => x.name === name), t = S.team.find(x => x.agent === name), pr = d.presence || {};
  const busy = !!(a && a.claims && a.claims.length) || (t && t.state === "working");
  const p = { dot: busy ? "busy" : { online: "live", idle: "idle", sleeping: "idle" }[pr.state] || "offline", seen: pr.last_seen ? ago(pr.last_seen) : a && a.last_seen ? ago(a.last_seen) : "never" };
  const st = { online: "Online", idle: "Idle", offline: "Offline", sleeping: "Asleep" }[pr.state] || cap(pr.state || "unknown");
  const head = `<div class="hm-dhero">${av(name, p.dot, "hm-av-lg")}<div><div class="hm-dhero-t">${E(st)}${t && STATE[t.state] && STATE[t.state][1] ? " " + badge(STATE[t.state][0], STATE[t.state][1]) : ""}</div><div class="hm-mut">${E(WAKE[pr.wake_mode] || "")}</div></div></div>`;
  const claims = arr(a && a.claims).map(c => `<li><span class="hm-li-t">${E(c.title)}</span><span class="hm-mut">${E(c.type)}${c.lease_left_s != null ? ` · lease ${E(c.lease_left_s)}s left` : ""}</span></li>`).join("");
  const now = claims ? `<ul class="hm-list">${claims}</ul>` : t && (t.headline || t.working_on) ? `<p class="hm-p">${E(t.headline || t.working_on)}</p>${arr(t.bullets).length ? `<ul class="hm-bul">${t.bullets.map(b => `<li>${E(b)}</li>`).join("")}</ul>` : ""}` : none("Nothing claimed right now.");
  const u = d.usage || {}, wins = arr(u.windows);
  const usage = (wins.length ? wins.map(w => `<div class="hm-win"><div class="hm-win-h"><b>${E(w.label || String(w.window).replace(/^[a-z]+:/, ""))}</b><span class="hm-mut">${E(w.period || "")}</span><span class="hm-sp"></span><span class="hm-num">${E(w.free_for_commons_pct)}% free</span></div><div class="hm-bar" title="used ${E(w.used_pct)}% · reserve ${E(w.reserve_pct)}%"><span class="hm-bar-u" style="width:${Math.min(100, +w.used_pct || 0)}%"></span><span class="hm-bar-r" style="width:${Math.max(0, Math.min(100 - (+w.used_pct || 0), +w.reserve_pct || 0))}%"></span></div><div class="hm-win-f hm-mut"><span>${E(w.used_pct)}% used · ${E(w.reserve_pct)}% kept in reserve</span>${w.resets_at ? `<span>Resets ${E(ago(w.resets_at))}</span>` : ""}</div></div>`).join("") : none("No usage windows reported."))
    + `<p class="hm-p">${u.can_take_work ? `${ic("check", "hm-ok")} Can take work now` : `${ic("stop", "hm-warn")} Waiting${u.waits_until ? " until " + E(ago(u.waits_until)) : ""}${u.blocking_window ? " for " + E(u.blocking_window) : ""}`}</p>`;
  const exp = Object.entries((d.profile && d.profile.expertise) || {}).sort((x, y) => y[1] - x[1]);
  const prof = d.profile || {};
  const strengths = (exp.length ? `<div class="hm-exp">${exp.map(([k, v]) => `<span class="hm-exp-k">${E(k)}</span><span class="hm-bar"><span class="hm-bar-u" style="width:${Math.round(100 * (+v || 0))}%"></span></span><span class="hm-num hm-mut">${Math.round(100 * (+v || 0))}</span>`).join("")}</div>` : none("No expertise recorded yet."))
    + dl([["Cost", prof.cost != null ? E(prof.cost) + " of 5" : ""], ["Speed", prof.speed != null ? E(prof.speed) + " of 5" : ""], ["At most", prof.max_concurrent != null ? E(prof.max_concurrent) + " steps at once" : ""]]);
  const recent = arr(d.recent).length ? `<div class="hm-steps">${d.recent.map(r => { const s = STEP[r.status] || [r.status, ""]; return `<button type="button" class="hm-step-link" data-hm="job" data-id="${E(r.job_id)}"><span class="hm-li-t">${E(r.title)}</span><span class="hm-mut">${E(r.job)} · ${E(ago(r.updated_at))}</span>${badge(s[0], s[1])}</button>`; }).join("")}</div>` : none("No steps yet.");
  const cli = d.cli ? dl([["Version", E(d.cli.version || "unknown")], ["Latest", E(d.cli.latest || "unknown")], ["Checked", E(ago(d.cli.updated_at || d.cli.seen_at))], ["Last update", E(d.cli.last_result || "")]])
    + (d.cli.update_available ? `<button type="button" data-hm="cli-update" data-name="${E(name)}">${ic("refresh")}${d.cli.requested ? "Update requested" : "Update to " + E(d.cli.latest)}</button>` : "") : none("This agent has not reported a CLI version.");
  const html = head + dl([["Last seen", E(p.seen)], ["Wake mode", E(pr.wake_mode || "")], ["Live connections", E(pr.live_connections || 0)], ["Channels", E(arr(pr.channels).join(", ") || "none")],
    ["Client", E(pr.client || "")], ["Platform", E([d.platform, d.vendor, d.model].filter(Boolean).join(" · "))], ["Unread messages", E(d.unread || 0)]])
    + dsec("Doing now", now) + dsec("Usage windows", usage) + dsec("Models", arr(d.models).length ? `<div class="hm-chips">${d.models.map(m => `<span class="badge">${E(m)}</span>`).join("")}</div>` : none("No models reported."))
    + dsec("Strengths", strengths) + dsec("Recent steps", recent) + dsec("CLI", cli);
  return { html, subtitle: [d.platform, d.vendor, d.model].filter(Boolean).join(" · ") || (SYS.includes(name) ? "System agent" : "Agent") };
}
function agentDrawer(name) {
  const a = S.agents.find(x => x.name === name);
  openDrawer({ id: "agent:" + name, title: name, subtitle: (a && [a.platform, a.model].filter(Boolean).join(" · ")) || (SYS.includes(name) ? "System agent" : "Agent"),
    build: () => agentBody(name), actions: [{ label: "Open the Agents page", run: () => { K.drawer.close(); go("Agents"); } }] });
}

async function jobBody(id) {
  const [d] = await Promise.all([api("job_detail?id=" + encodeURIComponent(id)), needAgents()]);
  if (!d || d.error) throw new Error((d && d.error) || "Unknown job");
  const j = d.job || {}, subs = arr(d.subtasks), wf = d.workflow, done = subs.filter(s => s.status === "done").length;
  const used = j.used || {}, jst = { running: "info", done: "ok", failed: "bad", cancelled: "" }[j.status] || "";
  const acts = [j.status === "failed" ? `<button type="button" class="pri" data-hm="job-retry" data-id="${E(j.id)}">${ic("refresh")}Retry job</button>` : "",
    j.status === "running" ? `<button type="button" class="hm-danger" data-hm="job-cancel" data-id="${E(j.id)}">${ic("stop")}Cancel job</button>` : ""].join("");
  const head = `<div class="hm-dsum">${badge(cap(j.status), jst)}${badge(String(j.pattern || "").replace(/_/g, " "))}<span class="hm-mut"><span class="hm-num">${done}/${subs.length}</span> steps done</span>${acts ? `<span class="hm-sp"></span>${acts}` : ""}</div>
<div class="progress" role="progressbar" aria-label="Steps done" aria-valuemin="0" aria-valuemax="${subs.length}" aria-valuenow="${done}"><div class="progress-bar" style="width:${subs.length ? Math.round(100 * done / subs.length) : 0}%"></div></div>`
    + dl([["Project", E(j.project || "")], ["Submitted by", E(j.submitted_by || "")], ["Started", E(ago(j.created_at))], ["Updated", E(ago(j.updated_at))],
      ["Spent", `<span class="hm-num">${E(K.fmt.money(used.cost || 0))} · ${E(K.fmt.tokens(used.tokens || 0))}</span>`],
      ["Folder", d.folder ? `<span class="hm-copyable"><code class="hm-id">${E(d.folder)}</code><button type="button" class="hm-icon-btn" data-hm="copy" data-v="${E(d.folder)}" aria-label="Copy the folder path">${ic("copy")}</button></span>` : ""]]);
  let spine = "";
  if (wf && arr(wf.stages).length) {
    spine = dsec("Stages", `<ol class="hm-spine" style="--n:${wf.stages.length}">${wf.stages.map(s => {
      const gate = arr(s.gates).find(g => g.status !== "done" && g.type === "owner");
      return `<li class="hm-stage is-${E(s.status)}"><span class="hm-stage-bar"></span><span class="hm-stage-n">${E(cap(s.stage))}</span><span class="hm-stage-a">${E(s.agent || (s.status === "pending" ? "not started" : ""))}</span>${s.rework ? `<span class="hm-stage-x">rework ×${E(s.rework)}</span>` : ""}${gate ? `<span class="hm-stage-x hm-warn">needs you</span>` : ""}</li>`;
    }).join("")}</ol>`, `<a class="hm-link" href="#Workflow">Open in Workflow${ic("chev")}</a>`);
    if (arr(wf.trace).length) spine += dsec("Acceptance criteria", `<ul class="hm-trace">${wf.trace.map(t => `<li>${t.tests === "pass" ? ic("check", "hm-ok") : t.tests === "fail" ? ic("x", "hm-bad") : ic("dot", "hm-mut")}<b>${E(t.ac)}</b><span>${E(t.text)}</span></li>`).join("")}</ul>`);
  }
  const steps = subs.map(s => {
    const lab = STEP[s.status] || [s.status, ""], agentStep = !["owner", "tally"].includes(s.type), r = s.route || {};
    const canRetry = agentStep && ["failed", "claimed", "awaiting_gate"].includes(s.status) && ["running", "failed"].includes(j.status);
    const canMove = agentStep && ["pending", "claimed"].includes(s.status) && j.status === "running";
    const verdict = s.verdict ? badge(s.verdict === "approve" ? "Approved" : s.verdict === "reject" ? "Rejected" : s.verdict, s.verdict === "approve" ? "ok" : s.verdict === "reject" ? "bad" : "") : "";
    return `<li class="hm-step"><span class="hm-step-dot is-${E(s.status)}"></span><div class="hm-step-b"><div class="hm-step-h"><span class="hm-li-t">${E(s.title || s.key)}</span>${verdict}${badge(lab[0], lab[1])}</div>
<div class="hm-step-m hm-mut">${s.type === "owner" ? "You" : E(s.agent || "unassigned")}${r.effort || r.model ? ` · <span title="${E(r.rationale || "")}">${E(r.model || "default model")}${r.effort ? ", " + E(r.effort) + " effort" : ""}</span>` : ""} · <span class="hm-num">${E(s.attempts)}/${E(s.max_attempts)}</span> attempts</div>
${s.result ? `<details class="hm-res"><summary>${E(String(s.result).split("\n")[0].slice(0, 90))}</summary><pre>${E(s.result)}</pre></details>` : ""}
${canRetry || canMove ? `<div class="hm-step-acts">${canRetry ? `<button type="button" data-hm="step-retry" data-id="${E(s.id)}">${ic("refresh")}Retry step</button>` : ""}${canMove ? `<button type="button" data-hm="step-move" data-id="${E(s.id)}" data-agent="${E(s.agent || "")}">${ic("move")}Move to agent</button>` : ""}</div>` : ""}</div></li>`;
  }).join("");
  return { title: j.title, subtitle: [String(j.pattern || "").replace(/_/g, " ") + " job", j.id].join(" · "),
    html: head + spine + dsec("Steps", steps ? `<ol class="hm-steplist">${steps}</ol>` : none("No steps yet."))
      + (j.result ? dsec("Merged result", `<pre class="hm-pre">${E(j.result)}</pre>`) : "") };
}
function jobDrawer(id) {
  const cached = arr(S.lastJobs).find(j => j.job_id === id);
  openDrawer({ id: "job:" + id, title: (cached && cached.title) || "Job", subtitle: id, build: () => jobBody(id),
    actions: [{ label: "Open Orchestration", run: () => { K.drawer.close(); go("Orchestration"); } }] });
}
async function ownerPost(path, body, okMsg) {
  const r = await postRaw(path, body);
  if (!r || r.error) { K.toast((r && r.error) || "That did not work", { kind: "err" }); return false; }
  K.toast(okMsg, { kind: "ok" }); render(); refreshDrawer(); return true;
}

/* ---------- Board ---------- */
const stOf = x => S.pend[x.id] || x.status;
function boardView() {
  const d = S.board || { items: [], projects: [] }, fq = S.q.trim().toLowerCase(), pj = S.pj;
  const scope = x => (!pj || x.project === pj) && (!fq || `${x.text} ${x.path || ""} ${x.evidence || ""} ${x.project || ""}`.toLowerCase().includes(fq));
  const keep = x => scope(x) && (S.st === "open" ? OPEN.includes(stOf(x)) : S.st === "all" || stOf(x) === S.st);
  const counts = { open: 0, all: 0, done: 0, dropped: 0 };
  d.items.filter(scope).forEach(x => { counts.all++; if (OPEN.includes(stOf(x))) counts.open++; else if (counts[stOf(x)] != null) counts[stOf(x)]++; });
  return { d, keep, counts };
}
const SICON = { open: "s_open", doing: "s_doing", blocked: "s_blocked", done: "s_done", dropped: "s_dropped" };
const SLABEL = { open: "Open", doing: "In progress", blocked: "Blocked", done: "Done", dropped: "Dropped" };
function bRow(x, dep) {
  const st = stOf(x), q = x.kind === "question", m = q && APPROVAL.exec(x.text || ""), text = m ? x.text.replace(m[0], "").trim() : x.text;
  const target = x.kind === "target" && (x.metric || x.goal) ? `<span class="hm-target"><b>${E(x.metric || "")}</b> goal <b>${E(x.goal || "?")}</b> · now <b>${E(x.current || "?")}</b></span>` : "";
  const acts = q && OPEN.includes(st)
    ? m ? `<a class="hm-act hm-btn" href="/approve/${encodeURIComponent(x.id)}">${ic("shield")}<span>Review</span></a>` : `<button type="button" class="hm-act" data-hm="board" data-id="${E(x.id)}">Answer</button>`
    : OPEN.includes(st)
      ? `${st !== "doing" ? `<button type="button" class="hm-act" data-hm="b-set" data-s="doing" data-id="${E(x.id)}" aria-label="Start: ${E(x.text.slice(0, 60))}">${ic("play")}<span>Start</span></button>` : ""}<button type="button" class="hm-act" data-hm="b-set" data-s="done" data-id="${E(x.id)}" aria-label="Mark done: ${E(x.text.slice(0, 60))}">${ic("check")}<span>Done</span></button><button type="button" class="hm-act" data-hm="b-set" data-s="dropped" data-id="${E(x.id)}" aria-label="Drop: ${E(x.text.slice(0, 60))}">${ic("x")}<span>Drop</span></button>`
      : `<button type="button" class="hm-act" data-hm="b-set" data-s="open" data-id="${E(x.id)}" aria-label="Reopen: ${E(x.text.slice(0, 60))}">${ic("undo")}<span>Reopen</span></button>`;
  return `<div class="hm-brow is-${E(st)}" style="--dep:${dep}" data-bid="${E(x.id)}"><span class="hm-bst is-${E(st)}" title="${E(SLABEL[st] || st)}">${ic(SICON[st] || "s_open")}</span>
<button type="button" class="hm-brow-main" data-hm="board" data-id="${E(x.id)}"><span class="hm-brow-t">${m ? badge(APPROVAL_LABEL[m[1]], "warn") + " " : ""}${target}${E(text)}${x.repeats ? ` <span class="badge bad">asked ${x.repeats + 1}×</span>` : ""}</span>
<span class="hm-brow-m"><span class="hm-kind">${E(x.kind)}</span>${x.source ? `<span>${E(x.source)}</span>` : ""}<span>${E(ago(x.updated_at || x.created_at))}</span>${x.evidence ? `<span class="hm-ev">${st === "dropped" ? "Why" : q ? "Answer" : "Evidence"}: ${E(x.evidence)}</span>` : ""}</span></button>
<span class="hm-brow-acts">${acts}</span></div>`;
}
function bList() {
  const { d, keep } = boardView(), by = {}, kids = {};
  d.items.forEach(x => by[x.id] = x);
  d.items.forEach(x => { const k = x.parent && by[x.parent] ? x.parent : ""; (kids[k] = kids[k] || []).push(x); });
  const live = x => keep(x) || (kids[x.id] || []).some(live);
  const walk = (p, dep) => (kids[p] || []).filter(live).map(x => bRow(x, dep) + walk(x.id, dep + 1)).join("");
  const groups = new Map(), head = x => (x.project || "No project") + (x.path ? " / " + x.path : "");
  (kids[""] || []).forEach(x => { if (!groups.has(head(x))) groups.set(head(x), []); });  // fixed order: groups never jump when a row leaves
  (kids[""] || []).filter(live).forEach(x => groups.get(head(x)).push(x));
  for (const [h, xs] of groups) if (!xs.length) groups.delete(h);
  if (!groups.size) {
    const filtered = S.q || S.pj;
    return K.empty({ icon: "Board", title: filtered ? "Nothing matches" : S.st === "open" ? "Nothing open" : `Nothing ${S.st === "all" ? "here" : S.st}`,
      hint: filtered ? "Try another search or project." : "Agents add your asks with board_add. You can add one yourself.",
      actions: filtered ? [{ label: "Clear filters", run: () => { S.q = ""; S.pj = ""; redraw(); } }] : [{ label: "New ask", run: () => compose(true) }] });
  }
  return `<div class="hm-blist">${[...groups].map(([h, xs]) => `<div class="hm-bgroup" role="group" aria-label="${E(h)}"><h3 class="hm-bgroup-h">${ic("folder")}${E(h)}<span class="hm-count">${xs.length}</span></h3>${xs.map(x => bRow(x, 0) + walk(x.id, 1)).join("")}</div>`).join("")}</div>`;
}
function bSeg() {
  const { counts } = boardView();
  return ["open", "all", "done", "dropped"].map(k => `<button type="button" role="tab" class="seg-item${k === S.st ? " on" : ""}" aria-selected="${k === S.st}" data-hm="b-st" data-s="${k}">${cap(k)} <span class="hm-segn">${counts[k]}</span></button>`).join("");
}
function updList() {
  const l = $("#hm-blist"), s = $("#hm-bseg");
  if (l) l.innerHTML = bList();
  if (s) s.innerHTML = bSeg();
}
function composer() {
  if (!S.compose) return "";
  const c = S.cd, projects = arr(S.board && S.board.projects);
  return `<form class="hm-compose" data-hm-form="compose" aria-label="New ask"><textarea id="hm-c-text" rows="2" required placeholder="What was asked, in your exact words" aria-label="What was asked">${E(c.text)}</textarea>
<div class="hm-compose-row"><label class="hm-fld"><span>Kind</span><select id="hm-c-kind">${KINDS.map(k => `<option${k === c.kind ? " selected" : ""}>${k}</option>`).join("")}</select></label>
<label class="hm-fld"><span>Project</span><input id="hm-c-proj" list="hm-c-pl" value="${E(c.project || S.pj)}" placeholder="none" autocomplete="off"></label><datalist id="hm-c-pl">${projects.map(p => `<option>${E(p)}</option>`).join("")}</datalist>
<label class="hm-fld"><span>Layer</span><input id="hm-c-path" value="${E(c.path)}" placeholder="e.g. ui/dashboard" autocomplete="off"></label><span class="hm-sp"></span>
<button type="button" class="hm-ghost" data-hm="compose-cancel">Cancel</button><button type="submit" class="pri">Add to the board</button></div></form>`;
}
function compose(on) {
  S.compose = on;
  if (tab !== "Board") go("Board");
  else redraw();
  if (on) {  // after a page switch the composer appears when the Board has loaded
    const focus = (n = 0) => { const t = $("#hm-c-text"); if (t) t.focus(); else if (n < 40) setTimeout(() => focus(n + 1), 50); };
    focus();
  }
}
async function addItem() {
  const v = id => (($(id) || {}).value || "").trim(), text = v("#hm-c-text");
  if (!text) { K.toast("Write what was asked first", { kind: "err" }); return; }
  const r = await postRaw("board/add", { kind: v("#hm-c-kind") || "ask", text, project: v("#hm-c-proj"), path: v("#hm-c-path") });
  if (!r || r.error) { K.toast((r && r.error) || "Could not add it", { kind: "err" }); return; }
  K.toast(r.repeated ? `Already on the board: now asked ${(r.repeats || 0) + 1} times` : "Added to the board", { kind: "ok", action: { label: "Open", run: () => boardDrawer(r.id) } });
  S.cd = { kind: S.cd.kind, text: "", project: S.cd.project, path: S.cd.path }; S.compose = false;
  render();
}
function setStatus(id, status) {
  const x = arr(S.board && S.board.items).find(i => i.id === id);
  if (!x) return;
  const typed = (($("#hm-ev-" + id) || {}).value || "").trim();
  const body = { id, status, evidence: "" };
  if (status === "done") body.evidence = typed || (x.evidence ? "" : "Marked done by the owner on the dashboard");
  if (status === "dropped") body.evidence = typed || "Dropped by the owner on the dashboard";
  const words = { doing: "Started", done: "Marked done", dropped: "Dropped", open: "Reopened" }[status];
  K.optimistic({
    label: `${words}: ${x.text.length > 40 ? x.text.slice(0, 40) + "…" : x.text}`,
    apply: () => { S.pend[id] = status; updList(); refreshDrawer(false); },
    revert: () => { delete S.pend[id]; updList(); refreshDrawer(false); },
    commit: () => postRaw("board/update", body),
  }).then(r => { if (r === "committed") { delete S.evd[id]; Promise.resolve(render()).finally(() => { delete S.pend[id]; refreshDrawer(); }); } });
}

V["Board"] = async function () {
  skel("Board");
  try {
    const d = await api("board");
    if (!d || d.error || !Array.isArray(d.items)) throw new Error((d && d.error) || "The hub did not answer");
    S.board = d;
    return boardPage();
  } catch (e) { return failed(e); }
};
function redraw() {  // the Board again from what it already loaded: filters and the composer change without a fetch
  const m = $("#m");
  if (tab === "Board" && m && S.board) m.innerHTML = boardPage();
}
function boardPage() {
  const d = S.board;
  if (S.pj && !d.projects.includes(S.pj)) S.pj = "";
  const by = Object.fromEntries(d.items.map(x => [x.id, x]));
  const asks = d.items.filter(x => x.kind === "ask"), tg = d.items.filter(x => x.kind === "target"), rep = asks.reduce((a, x) => a + (x.repeats || 0), 0);
  const qs = d.items.filter(x => x.kind === "question" && OPEN.includes(stOf(x)) && (!S.pj || x.project === S.pj))
    .map(x => Object.assign({}, x, { blocks: x.parent && by[x.parent] ? by[x.parent].text.slice(0, 120) : null }));
  const stat = (l, v, cls = "") => `<div class="hm-bstat"><span class="hm-bstat-v ${cls}">${v}</span><span class="hm-bstat-l">${l}</span></div>`;
  return `<div class="hm hm-board"><div class="hm-bhead"><div class="hm-bstats">${stat("Open asks", asks.filter(x => OPEN.includes(stOf(x))).length)}${stat("Asked twice or more", rep, rep ? "hm-bad" : "")}${stat("Targets met", `${tg.filter(x => x.status === "done").length}<span class="hm-mut">/${tg.length}</span>`)}${stat("Delivered", d.items.filter(x => x.status === "done").length)}</div>
<button type="button" class="pri hm-new" data-hm="compose" aria-expanded="${S.compose}">${ic("plus")}New ask</button></div>
${needsSection(qs)}
<section class="hm-sec" aria-label="Board items">${sec("Everything asked, measured and delivered", "", "")}<p class="hm-sec-p">Per project and layer. Agents read it at every check-in.</p>
<div class="hm-toolbar"><div class="seg" role="tablist" aria-label="Status" id="hm-bseg">${bSeg()}</div>
<select id="hm-bpj" aria-label="Project"><option value="">All projects</option>${d.projects.map(p => `<option value="${E(p)}"${p === S.pj ? " selected" : ""}>${E(p)}</option>`).join("")}</select>
<label class="hm-search">${ic("search")}<input id="hm-bq" type="search" value="${E(S.q)}" placeholder="Search the board" aria-label="Search the board" autocomplete="off"></label></div>
${composer()}<div id="hm-blist">${bList()}</div></section></div>`;
}

async function boardBody(id, fresh) {
  if (fresh || !S.board) {
    const d = await api("board");
    if (d && Array.isArray(d.items)) S.board = d;
  }
  const items = arr(S.board && S.board.items), x = items.find(i => i.id === id);
  if (!x) throw new Error("This board item no longer exists");
  if (fresh) [S.audit] = await Promise.all([api("audit").catch(() => null), needAgents()]);
  const names = Object.fromEntries(S.agents.map(a => [a.id, a.name])), st = stOf(x), q = x.kind === "question";
  const parent = x.parent && items.find(i => i.id === x.parent), kids = items.filter(i => i.parent === x.id);
  const acts = q && OPEN.includes(st) ? "" : OPEN.includes(st)
    ? `${st !== "doing" ? `<button type="button" data-hm="b-set" data-s="doing" data-id="${E(id)}">${ic("play")}Start</button>` : ""}<button type="button" class="pri" data-hm="b-set" data-s="done" data-id="${E(id)}">${ic("check")}Mark done</button><button type="button" data-hm="b-set" data-s="dropped" data-id="${E(id)}">${ic("x")}Drop</button>`
    : `<button type="button" data-hm="b-set" data-s="open" data-id="${E(id)}">${ic("undo")}Reopen</button>`;
  let body = `${q && OPEN.includes(st) ? "" : `<p class="hm-dtext">${E(x.text)}</p>`}<div class="hm-dsum"><span class="hm-bst is-${E(st)}">${ic(SICON[st] || "s_open")}</span>${badge(SLABEL[st] || st)}${badge(x.kind)}${x.repeats ? badge(`asked ${x.repeats + 1}×`, "bad") : ""}</div>`;
  if (acts) body += `<div class="hm-dacts">${acts}</div>`;
  if (q && OPEN.includes(st)) body += `<div class="hm-dq">${qCard(Object.assign({}, x, { blocks: parent ? parent.text.slice(0, 120) : null }), true)}</div>`;
  if (x.kind === "target") body += dsec("Target", dl([["Metric", E(x.metric || "")], ["Goal", E(x.goal || "")]]) + `<div class="hm-inline"><label class="hm-fld"><span>Now</span><input id="hm-cu-${E(id)}" value="${E(x.current || "")}" placeholder="current value"></label><button type="button" data-hm="b-current" data-id="${E(id)}">Save</button></div>`);
  body += dsec(q ? "Answer" : st === "dropped" ? "Reason" : "Evidence", q ? (x.evidence ? `<p class="hm-p">${E(x.evidence)}</p>` : none("Not answered yet."))
    : `<textarea id="hm-ev-${E(id)}" rows="3" placeholder="What proves it: a commit, file, benchmark, item id or URL" aria-label="Evidence">${E(S.evd[id] ?? x.evidence ?? "")}</textarea><div class="hm-inline"><span class="hm-hint">Done uses this as its evidence. Drop uses it as the reason.</span><span class="hm-sp"></span><button type="button" data-hm="b-ev" data-id="${E(id)}">Save evidence</button></div>`);
  body += dl([["Project", E(x.project || "none")], ["Layer", E(x.path || "")], ["From", E(x.source || "")], ["Created", E(ago(x.created_at))], ["Updated", E(ago(x.updated_at))], ["Closed", x.closed_at ? E(ago(x.closed_at)) : ""],
    ["ID", `<span class="hm-copyable"><code class="hm-id">${E(x.id)}</code><button type="button" class="hm-icon-btn" data-hm="copy" data-v="${E(x.id)}" aria-label="Copy the id">${ic("copy")}</button></span>`]]);
  if (parent) body += dsec("Part of", `<button type="button" class="hm-step-link" data-hm="board" data-id="${E(parent.id)}"><span class="hm-li-t">${E(parent.text)}</span>${badge(parent.kind)}</button>`);
  if (kids.length) body += dsec(`Under this ${E(x.kind)}`, `<div class="hm-steps">${kids.map(k => `<button type="button" class="hm-step-link" data-hm="board" data-id="${E(k.id)}"><span class="hm-bst is-${E(stOf(k))}">${ic(SICON[stOf(k)] || "s_open")}</span><span class="hm-li-t">${E(k.text)}</span>${badge(k.kind)}</button>`).join("")}</div>`);
  const ACT = { "board.add": "Added", "board.update": "Updated", "board.repeat": "Asked again", "board.answer": "Answered" };
  const hist = arr(S.audit && S.audit.log).filter(r => r.target === id).map(r => {
    let det = null; try { det = JSON.parse(r.detail || "null"); } catch (e) { /* not JSON */ }
    if (det && typeof det === "object") det = Object.fromEntries(Object.entries(det).filter(([, v]) => v !== "" && v != null));
    return `<li><span class="hm-tl-dot"></span><div><div><b>${E(ACT[r.action] || r.action)}</b> <span class="hm-mut">${E(r.agent_id ? names[r.agent_id] || r.agent_id : "you or Commons")} · ${E(ago(r.ts))}</span></div>${det && typeof det === "object" && Object.keys(det).length ? K.kv(det) : ""}</div></li>`;
  }).join("");
  body += dsec("History", hist ? `<ol class="hm-tl">${hist}</ol>` : none(`Created ${ago(x.created_at)}. No later changes in the recent audit log.`));
  return { html: body, title: cap(x.kind), subtitle: [x.project || "No project", x.path].filter(Boolean).join(" / ") };
}
function boardDrawer(id) {
  const x = arr(S.board && S.board.items).find(i => i.id === id);
  openDrawer({ id: "board:" + id, title: x ? cap(x.kind) : "Board item", subtitle: x ? [x.project || "No project", x.path].filter(Boolean).join(" / ") : id, build: f => boardBody(id, f) });
}

/* ---------- one delegated listener for every data-hm action ---------- */
document.addEventListener("click", e => {
  const b = e.target.closest && e.target.closest("[data-hm]");
  if (!b) return;
  const act = b.dataset.hm, id = b.dataset.id;
  switch (act) {
    case "agent": if (!K.drawer.openId("agent:" + b.dataset.name)) agentDrawer(b.dataset.name); break;  // Agents owns it
    case "job": if (!K.drawer.openId("job:" + id)) jobDrawer(id); break;  // Work owns the job drawer
    case "board": boardDrawer(id); break;
    case "conflicts": window.mc = true; break;  // the link itself goes to Memory
    case "act-more": S.actAll = !S.actAll; render(); break;
    case "ans-choice": answer(id, b.dataset.v); break;
    case "ans-send": answer(id, (b.closest(".hm-q").querySelector("textarea") || {}).value, b); break;
    case "ans-toggle": {
      const s = S.ans[id] = S.ans[id] || {};
      s.open = !s.open;
      document.querySelectorAll(`[data-qid="${CSS.escape(id)}"]`).forEach(c => {
        const f = c.querySelector(".hm-q-free"), t = c.querySelector('[data-hm="ans-toggle"]');
        if (f) f.hidden = !s.open;
        if (t) t.setAttribute("aria-expanded", String(s.open));
      });
      const card = b.closest(".hm-q"), t = card && card.querySelector("textarea");
      if (s.open && t) t.focus();
      break;
    }
    case "copy": K.copy(b.dataset.v); break;
    case "cli-update": ownerPost("agent/update", { agent: b.dataset.name }, "Update requested: the agent updates at its next check-in"); break;
    case "job-retry": ownerPost("job/retry", { id }, "The job is running again"); break;
    case "job-cancel": if (confirm("Cancel this job? Steps not finished yet stop.")) ownerPost("job/cancel", { id }, "Job cancelled"); break;
    case "step-retry": ownerPost("subtask/retry", { id }, "The step is back in the queue"); break;
    case "step-move": {
      const others = S.agents.filter(a => !SYS.includes(a.name) && a.name !== b.dataset.agent);
      if (!others.length) { K.toast("No other agent to move it to", { kind: "info" }); break; }
      K.menu(b, others.map(a => ({ label: a.name, icon: "Agents", run: () => ownerPost("subtask/reassign", { id, agent: a.name }, `Moved to ${a.name}`) })));
      break;
    }
    case "b-set": setStatus(id, b.dataset.s); break;
    case "b-ev": {
      const v = (($("#hm-ev-" + id) || {}).value || "").trim();
      if (!v) { K.toast("Write the evidence first", { kind: "err" }); break; }
      postRaw("board/update", { id, evidence: v }).then(r => { if (!r || r.error) K.toast((r && r.error) || "Could not save", { kind: "err" }); else { delete S.evd[id]; K.toast("Evidence saved", { kind: "ok" }); render(); refreshDrawer(); } });
      break;
    }
    case "b-current": postRaw("board/update", { id, current: (($("#hm-cu-" + id) || {}).value || "").trim() }).then(r => { if (!r || r.error) K.toast((r && r.error) || "Could not save", { kind: "err" }); else { K.toast("Current value saved", { kind: "ok" }); render(); refreshDrawer(); } }); break;
    case "b-st": S.st = b.dataset.s; updList(); break;
    case "compose": compose(!S.compose); break;
    case "compose-cancel": compose(false); break;
  }
});
document.addEventListener("input", e => {
  const t = e.target;
  if (t.dataset && t.dataset.hmAn) { (S.ans[t.dataset.hmAn] = S.ans[t.dataset.hmAn] || {}).draft = t.value; return; }
  if (t.id === "hm-bq") { S.q = t.value; updList(); return; }
  if (t.id && t.id.startsWith("hm-ev-")) { S.evd[t.id.slice(6)] = t.value; return; }
  const f = { "hm-c-text": "text", "hm-c-proj": "project", "hm-c-path": "path", "hm-c-kind": "kind" }[t.id];
  if (f) S.cd[f] = t.value;
});
document.addEventListener("change", e => {
  if (e.target.id === "hm-bpj") { S.pj = e.target.value; redraw(); }
  if (e.target.id === "hm-c-kind") S.cd.kind = e.target.value;
});
document.addEventListener("submit", e => {
  if (e.target.dataset && e.target.dataset.hmForm === "compose") { e.preventDefault(); addItem(); }
});
document.addEventListener("keydown", e => {
  const t = e.target;
  if (t.dataset && t.dataset.hmAn && e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); answer(t.dataset.hmAn, t.value, t); }
  else if (t.id === "hm-c-text" && e.key === "Enter" && !e.shiftKey) { e.preventDefault(); addItem(); }
  else if (t.closest && t.closest(".hm-compose") && e.key === "Escape") { e.preventDefault(); e.stopPropagation(); compose(false); }
});

// agent: and job: drawers belong to the Agents and Work pages, which load after this file and replace these registrations;
// these only answer if those pages are absent
K.drawer.on("agent:", id => agentDrawer(id.slice(6)));
K.drawer.on("job:", id => jobDrawer(id.slice(4)));
K.drawer.on("board:", id => boardDrawer(id.slice(6)));
K.action({ id: "home-new-ask", label: "New ask on the board", keywords: "add board request todo", icon: P.plus, run: () => compose(true) });
K.action({ id: "home-needs-you", label: "Questions that need you", keywords: "answer approve decisions", icon: P.calm, run: () => go("Overview") });
})();
