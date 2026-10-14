/* kit.js: the dashboard's shared UI kit, exposed as window.K.
   It is concatenated after legacy.js into the same <script>, so the top-level function declarations at the end of
   this file (go, toast, pal, palClose, stopPoll) replace legacy's versions everywhere, inline handlers included.
   Everything else lives inside the IIFE. */
var K = (() => {
"use strict";
const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
const errText = e => String((e && e.message) || e || "failed");
const svg = (inner, cls = "") => inner ? `<svg class="${cls}" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>` : "";
const BOLT = '<path d="M13 2L4.5 13.5H12L11 22l8.5-11.5H12z"/>';
const typing = el => !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName || ""));
const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"]),[contenteditable="true"]';

/* ---------- handler registry: buttons in generated HTML call functions without inline JS strings ---------- */
const REG = new Map();
let regN = 0;
function reg(fn) {  // ponytail: capped at 500, oldest evicted; a button older than 500 registrations loses its handler
  const id = "k" + (++regN);
  REG.set(id, fn);
  if (REG.size > 500) REG.delete(REG.keys().next().value);
  return id;
}

/* ---------- formatting ---------- */
const UNITS = ["", "k", "M", "B", "T"];
function num(n) {
  n = +n || 0;
  let v = n, i = 0;
  while (Math.abs(v) >= 999.5 && i < 4) { v /= 1000; i++; }
  return `${i && Math.abs(v) >= 100 ? Math.round(v) : +v.toFixed(1)}${UNITS[i]}`;
}
const tokens = n => num(n) + " tok";
const USD2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const USD4 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 });
function money(x) { x = +x || 0; return (x && Math.abs(x) < 1 ? USD4 : USD2).format(x); }
function ago(t, now = Date.now()) {  // ISO string, epoch seconds, or epoch milliseconds
  const n = typeof t === "number" || /^\d+(\.\d+)?$/.test(String(t ?? "")) ? +t : NaN;
  const ms = n === n ? (n > 1e12 ? n : n * 1000) : Date.parse(t);
  if (!(ms > 0)) return "";
  let s = Math.round((now - ms) / 1000);
  const future = s < 0;
  s = Math.abs(s);
  if (s < 5) return "just now";
  const [v, u] = s < 60 ? [s, "s"] : s < 3600 ? [s / 60, "m"] : s < 86400 ? [s / 3600, "h"]
    : s < 2592000 ? [s / 86400, "d"] : s < 31536000 ? [s / 2592000, "mo"] : [s / 31536000, "y"];
  return future ? `in ${Math.floor(v)}${u}` : `${Math.floor(v)}${u} ago`;
}

/* ---------- small HTML builders (all text escaped) ---------- */
function kv(obj, max = 60) {
  if (obj == null || typeof obj !== "object") obj = { value: obj };
  return `<div class="kv">${Object.entries(obj).map(([k, v]) => {
    const s = typeof v === "string" ? v : (JSON.stringify(v) ?? String(v));
    const val = s.length > max
      ? `<button type="button" class="kv-v kv-more" data-kv-full="${E(s)}" aria-expanded="false" title="Show the full value">${E(s.slice(0, max))}…</button>`
      : `<span class="kv-v">${E(s)}</span>`;
    return `<span class="kv-pill"><span class="kv-k">${E(k)}</span>${val}</span>`;
  }).join("")}</div>`;
}
function spark(values, { w = 120, h = 28 } = {}) {
  w = +w || 120; h = +h || 28;
  let v = (values || []).map(x => +x || 0);
  if (!v.length) return "";
  if (v.length === 1) v = [v[0], v[0]];
  const lo = Math.min(0, ...v), range = (Math.max(...v) - lo) || 1, n = v.length;
  const pts = v.map((y, i) => `${(1 + i * (w - 2) / (n - 1)).toFixed(1)},${(h - 2 - (y - lo) / range * (h - 4)).toFixed(1)}`).join(" ");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polygon points="1,${h - 2} ${pts} ${w - 1},${h - 2}" fill="currentColor" fill-opacity=".12"/><polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}
function empty({ icon = "", title = "", hint = "", actions = [] } = {}) {
  const ico = icon ? `<div class="ico">${ICON[icon] ? svg(ICON[icon]) : E(icon)}</div>` : "";
  const acts = actions.length ? `<div class="empty-actions">${actions.map((a, i) =>
    `<button type="button"${i ? "" : ' class="pri"'} data-kact="${reg(a.run)}">${E(a.label)}</button>`).join("")}</div>` : "";
  return `<div class="empty">${ico}<b>${E(title)}</b>${hint ? `<div class="hint">${E(hint)}</div>` : ""}${acts}</div>`;
}
const skeleton = (rows = 3) => `<div class="skeleton" role="status" aria-label="Loading">${
  Array.from({ length: rows }, (_, i) => `<div class="skeleton-line" style="width:${[100, 86, 72, 92][i % 4]}%"></div>`).join("")}</div>`;
async function copy(text) {
  text = String(text ?? "");
  try { await navigator.clipboard.writeText(text); }
  catch (e) {  // plain http on a LAN address has no Clipboard API
    const back = document.activeElement, ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;top:0;opacity:0";
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { /* reported below */ }
    ta.remove();
    if (back && back.focus) back.focus();
    if (!ok) { toastFn("Could not copy to the clipboard", { kind: "err" }); return false; }
  }
  toastFn("Copied to the clipboard", { kind: "ok", ms: 2000 });
  return true;
}

/* ---------- event bus ---------- */
const BUS = new Map();
function on(type, fn) {
  if (!BUS.has(type)) BUS.set(type, new Set());
  BUS.get(type).add(fn);
  return () => BUS.get(type).delete(fn);
}
function emit(type, data) {
  (BUS.get(type) || []).forEach(fn => { try { fn(data); } catch (e) { console.error(e); } });
}
let refreshT = 0, seenFp;
function refresh() {  // coalesce live "change" bursts into one re-render, and never clobber a field the user is editing
  clearTimeout(refreshT);
  refreshT = setTimeout(() => {
    refreshT = 0;
    try {
      if (document.querySelector("details[open], input:focus, textarea:focus, select:focus")) return;
    } catch (e) { return; }
    if (typeof render === "function") render();
  }, 250);
}
function noteChange(data) {
  if (data && typeof data === "object" && "fp" in data) {
    if (data.fp === seenFp) return;
    seenFp = data.fp;
  }
  emit("change", data);
}

/* ---------- toasts ---------- */
const TICO = {
  ok: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  err: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5M12 16v.5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.5v.5"/>',
};
function toastFn(msg, o = {}) {
  if (typeof o === "string") o = { kind: o };
  const kind = TICO[o.kind] ? o.kind : "info", box = $("#toasts") || document.body;
  let stack = box.querySelector(".toast-stack");
  if (!stack) { stack = document.createElement("div"); stack.className = "toast-stack"; box.appendChild(stack); }
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.setAttribute("role", kind === "err" ? "alert" : "status");
  el.innerHTML = svg(TICO[kind], "toast-ico") + '<div class="toast-msg"></div>';
  el.querySelector(".toast-msg").textContent = String(msg ?? "");
  let anim = null, ended = false;
  const end = why => {  // why: timeout | close | action | evict | undo; everything but undo counts as "done"
    if (ended) return;
    ended = true;
    if (anim) anim.cancel();
    el.classList.add("out");
    setTimeout(() => el.remove(), 250);
    if (why !== "undo" && o.ondone) { try { o.ondone(why); } catch (e) { console.error(e); } }
  };
  el._kEnd = end;
  const button = (label, cls, fn, aria) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = cls; b.textContent = label;
    if (aria) b.setAttribute("aria-label", aria);
    b.addEventListener("click", fn);
    el.appendChild(b);
  };
  if (o.action) button(o.action.label, "toast-act", () => { end("action"); o.action.run && o.action.run(); });
  if (o.undo) button("Undo", "toast-act toast-undo", () => { end("undo"); o.undo(); });
  button("×", "toast-x", () => end("close"), "Dismiss");
  const ms = o.ms ?? (o.undo ? 5000 : kind === "err" ? 6000 : 4000);
  if (ms > 0 && ms < Infinity) {  // the progress animation is the countdown: pausing it pauses the toast
    const bar = document.createElement("div");
    bar.className = "progress"; bar.style.transformOrigin = "left";
    el.appendChild(bar);
    anim = bar.animate([{ transform: "scaleX(1)" }, { transform: "scaleX(0)" }], { duration: ms, easing: "linear", fill: "forwards" });
    anim.onfinish = () => end("timeout");
    const pause = () => anim.pause();
    const play = () => { if (!ended && !el.matches(":hover") && !el.contains(document.activeElement)) anim.play(); };
    el.addEventListener("mouseenter", pause);
    el.addEventListener("mouseleave", play);
    el.addEventListener("focusin", pause);
    el.addEventListener("focusout", () => setTimeout(play));
  }
  stack.appendChild(el);
  const live = [...stack.children].filter(c => !c.classList.contains("out"));
  for (let i = 0; i < live.length - 3; i++) live[i]._kEnd("evict");
  return { el, close: () => end("close") };
}

/* ---------- optimistic updates ---------- */
const PENDING = new Set();
function optimistic({ apply, revert, commit, label = "Done", ms = 5000 } = {}) {
  return new Promise(resolve => {
    try { if (apply) apply(); } catch (e) { toastFn(errText(e), { kind: "err" }); resolve("failed"); return; }
    let settled = false;
    const undo = () => {
      if (settled) return;
      settled = true; PENDING.delete(flush);
      try { if (revert) revert(); } catch (e) { console.error(e); }
      resolve("undone");
    };
    const flush = async () => {
      if (settled) return;
      settled = true; PENDING.delete(flush);
      try {
        const r = commit ? await commit() : null;
        if (r && r.error) throw new Error(r.error);
        resolve("committed");
      } catch (e) {
        try { if (revert) revert(); } catch (_) { /* the error toast below is what matters */ }
        toastFn("Reverted: " + errText(e), { kind: "err" });
        resolve("failed");
      }
    };
    PENDING.add(flush);
    toastFn(label, { kind: "ok", ms, undo, ondone: flush });
  });
}
window.addEventListener("pagehide", () => PENDING.forEach(f => f()));  // best effort: commit before the tab goes away

/* ---------- router: #Page?query, with drawer=<id> for deep-linked drawers ---------- */
function readHash() {
  const raw = location.hash.slice(1), i = raw.indexOf("?");
  let page = i < 0 ? raw : raw.slice(0, i);
  try { page = decodeURI(page); } catch (e) { /* keep it raw */ }
  return { page: T.includes(page) ? page : "Overview", u: new URLSearchParams(i < 0 ? "" : raw.slice(i + 1)) };
}
function keyOf(p) {  // what decides the rendered view: the page plus every param except drawer
  const u = new URLSearchParams(p.u);
  u.delete("drawer");
  const q = u.toString();
  return p.page + (q ? "?" + q : "");
}
const hashFor = (page, u) => { const q = u.toString(); return "#" + encodeURI(page) + (q ? "?" + q : ""); };
function applyQuery(u) {  // same params legacy.js reads once at load
  if (u.get("scope")) window.gs = { scope: u.get("scope"), id: u.get("id") || "" };
  if (u.get("bucket")) window.kb = u.get("bucket");
  if (u.get("path")) window.oc_jump = u.get("path");
  if (u.get("mode")) window.oc_mode = u.get("mode");
  if (u.get("cite") && typeof cite === "function") setTimeout(() => cite(u.get("cite")), 800);
}
const RECENT = "commons.recent";
function recent() {
  try {
    const a = JSON.parse(localStorage.getItem(RECENT) || "[]");
    return Array.isArray(a) ? a.filter(x => typeof x === "string") : [];
  } catch (e) { return []; }
}
function remember(t) {
  try { localStorage.setItem(RECENT, JSON.stringify([t].concat(recent().filter(x => x !== t)).slice(0, 8))); } catch (e) { /* private mode */ }
}
const R = { key: "" };
function closeOverlays() { mClose(false); pClose(false); if (D.el) dClose(true); }
function nav(t) {  // go(): renders once; the hashchange it causes finds the same key and does nothing
  closeOverlays();
  tab = t; R.key = t; remember(t);
  location.hash = encodeURI(t);
  const side = $("#side");
  if (side) side.classList.remove("open");
  render();
}
function route() {  // hashchange and popstate (Back/Forward, edited URL, plain #links); both may fire, so idempotent
  const p = readHash(), k = keyOf(p);
  if (k !== R.key) {
    R.key = k;
    closeOverlays();
    tab = p.page; applyQuery(p.u); remember(tab);
    render();
  }
  syncDrawer(p);
}

/* ---------- drawer ---------- */
const D = { el: null, bd: null, opts: null, prev: null, seq: 0, n: 0 };
const OPENERS = new Map();
let pendingDrawer = null;
function setDrawerParam(id) {
  const p = readHash();
  if (p.u.get("drawer") === id) return;
  const had = p.u.has("drawer");
  p.u.set("drawer", id);
  if (had) history.replaceState(history.state && history.state.kdrawer ? { kdrawer: id } : null, "", hashFor(p.page, p.u));
  else history.pushState({ kdrawer: id }, "", hashFor(p.page, p.u));
}
function clearDrawerParam(id) {
  const p = readHash();
  if (p.u.get("drawer") !== id) return;
  if (history.state && history.state.kdrawer === id) { history.back(); return; }  // we pushed it: step back off it
  p.u.delete("drawer");
  history.replaceState(null, "", hashFor(p.page, p.u));
}
function openerFor(id) {
  let best = null;
  for (const [prefix, fn] of OPENERS) if (id.startsWith(prefix) && (!best || prefix.length > best[0].length)) best = [prefix, fn];
  return best && best[1];
}
function syncDrawer(p = readHash()) {
  const id = p.u.get("drawer");
  pendingDrawer = null;
  if (!id) { if (D.el && D.opts.id) dClose(true); return; }
  if (D.el && String(D.opts.id) === id) return;
  const fn = openerFor(id);
  if (!fn) { pendingDrawer = id; return; }  // its page may register the opener later
  try { fn(id); } catch (e) { toastFn(errText(e), { kind: "err" }); }
}
function trap(root, e) {
  const f = [...root.querySelectorAll(FOCUSABLE)].filter(x => x.offsetParent !== null);
  if (!f.length) { e.preventDefault(); root.focus(); return; }
  const first = f[0], last = f[f.length - 1], cur = document.activeElement;
  if (e.shiftKey && (cur === first || cur === root)) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && cur === last) { e.preventDefault(); first.focus(); }
}
function drawerKey(e) {
  if (e.key === "Escape") {
    if (!e.defaultPrevented) { e.preventDefault(); dClose(); }
    e.stopPropagation();
    return;
  }
  if (e.key === "Tab") trap(D.el, e);
  // modal: page shortcuts behind it stay quiet, but typing keeps legacy's field handlers and mod+K still opens the palette
  if (!typing(e.target) && !((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k")) e.stopPropagation();
}
function dOpen(o = {}) {
  mClose(false); pClose(false);
  const prevId = D.el && D.opts.id;
  if (D.el) {
    if (D.opts.onClose && D.opts !== o) { try { D.opts.onClose(); } catch (e) { console.error(e); } }
  } else {
    D.prev = document.activeElement;
    const tid = "kdr" + (++D.n), bd = document.createElement("div"), el = document.createElement("aside");
    bd.className = "drawer-backdrop";
    bd.addEventListener("click", () => dClose());
    el.className = "drawer"; el.tabIndex = -1;
    el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); el.setAttribute("aria-labelledby", tid);
    el.innerHTML = `<div class="drawer-head"><div class="drawer-titles"><h2 class="drawer-title" id="${tid}"></h2><div class="drawer-sub"></div></div><button type="button" class="drawer-x" aria-label="Close">×</button></div><div class="drawer-body"></div><footer class="drawer-foot"></footer>`;
    el.querySelector(".drawer-x").addEventListener("click", () => dClose());
    el.addEventListener("keydown", drawerKey);
    document.body.append(bd, el);
    const app = $(".app");
    if (app) app.inert = true;
    requestAnimationFrame(() => { el.classList.add("open"); bd.classList.add("open"); });
    D.el = el; D.bd = bd;
  }
  D.opts = o;
  const seq = ++D.seq, el = D.el, body = el.querySelector(".drawer-body"), foot = el.querySelector(".drawer-foot");
  el.querySelector(".drawer-title").textContent = o.title || "";
  const sub = el.querySelector(".drawer-sub");
  sub.textContent = o.subtitle || ""; sub.hidden = !o.subtitle;
  const ctx = { el, body, close: () => dClose() };
  foot.textContent = "";
  foot.hidden = !(o.actions && o.actions.length);
  (o.actions || []).forEach(a => {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = a.label;
    b.className = { primary: "pri", danger: "bad" }[a.kind] || a.kind || "";
    b.addEventListener("click", async () => { try { await a.run(ctx); } catch (e) { toastFn(errText(e), { kind: "err" }); } });
    foot.appendChild(b);
  });
  if (o.render) {  // the renderer returns page HTML that already escaped its data, like the V page renderers
    body.innerHTML = skeleton(4);
    Promise.resolve().then(() => o.render(ctx)).then(
      h => { if (seq === D.seq) body.innerHTML = h || ""; },
      e => { if (seq === D.seq) body.innerHTML = `<div class="failed">${E(errText(e))}</div>`; });
  } else body.innerHTML = o.html || "";
  if (o.id) setDrawerParam(String(o.id));
  else if (prevId) clearDrawerParam(String(prevId));
  el.focus();
  return ctx;
}
function dClose(fromRoute) {
  if (!D.el) return;
  const { el, bd, opts, prev } = D;
  D.el = D.bd = D.opts = D.prev = null; D.seq++;
  el.inert = true;
  el.classList.remove("open"); bd.classList.remove("open");
  setTimeout(() => { el.remove(); bd.remove(); }, 300);
  const app = $(".app");
  if (app) app.inert = false;
  if (opts.id && !fromRoute) clearDrawerParam(String(opts.id));
  if (opts.onClose) { try { opts.onClose(); } catch (e) { console.error(e); } }
  if (prev && prev.isConnected && prev.focus) prev.focus();
}
function drawerOn(prefix, fn) {
  OPENERS.set(prefix, fn);
  if (pendingDrawer && pendingDrawer.startsWith(prefix)) syncDrawer();
}

/* ---------- menu ---------- */
const M = { el: null, anchor: null, off: null };
function menu(anchor, items) {
  if (M.el) { const same = M.anchor === anchor; mClose(false); if (same) return; }
  const el = document.createElement("div");
  el.className = "menu"; el.setAttribute("role", "menu");
  (items || []).filter(Boolean).forEach(it => {
    const b = document.createElement("button");
    b.type = "button"; b.tabIndex = -1; b.setAttribute("role", "menuitem");
    b.className = "menu-item" + (it.danger ? " danger" : "");
    b.innerHTML = iconMarkup(it.icon) + '<span class="menu-label"></span>' + (it.kbd ? kbd(it.kbd) : "");
    b.querySelector(".menu-label").textContent = it.label;
    if (it.icon && !iconMarkup(it.icon)) { const s = document.createElement("span"); s.className = "menu-ico"; s.textContent = String(it.icon); b.prepend(s); }
    b.addEventListener("click", () => { mClose(true); try { if (it.run) it.run(); } catch (e) { toastFn(errText(e), { kind: "err" }); } });
    el.appendChild(b);
  });
  el.addEventListener("keydown", e => {
    const bs = [...el.querySelectorAll(".menu-item")], n = bs.length, i = bs.indexOf(document.activeElement);
    const j = e.key === "ArrowDown" ? (i + 1) % n : e.key === "ArrowUp" ? (i < 0 ? n - 1 : (i - 1 + n) % n)
      : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : -1;
    if (j >= 0 && n) { e.preventDefault(); bs[j].focus(); }
    else if (e.key === "Escape") { e.preventDefault(); mClose(true); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (bs[i]) bs[i].click(); }
    else if (e.key === "Tab") mClose(false);
    e.stopPropagation();  // keys inside an open menu never reach page shortcuts
  });
  el.style.position = "fixed";
  document.body.appendChild(el);
  const r = anchor.getBoundingClientRect(), mh = el.offsetHeight, mw = el.offsetWidth;
  const below = r.bottom + 4, above = r.top - mh - 4;
  el.style.top = (below + mh > innerHeight - 8 && above >= 8 ? above : below) + "px";
  el.style.left = Math.max(8, Math.min(r.left, innerWidth - mw - 8)) + "px";
  const down = e => { if (!el.contains(e.target) && !anchor.contains(e.target)) mClose(false); };
  const scroll = e => { if (!el.contains(e.target)) mClose(false); };
  const resize = () => mClose(false);
  document.addEventListener("pointerdown", down, true);
  window.addEventListener("scroll", scroll, true);
  window.addEventListener("resize", resize);
  M.off = () => {
    document.removeEventListener("pointerdown", down, true);
    window.removeEventListener("scroll", scroll, true);
    window.removeEventListener("resize", resize);
  };
  M.el = el; M.anchor = anchor;
  anchor.setAttribute("aria-haspopup", "menu"); anchor.setAttribute("aria-expanded", "true");
  const first = el.querySelector(".menu-item");
  if (first) first.focus();
  return { el, close: () => mClose(true) };
}
function mClose(refocus) {
  if (!M.el) return;
  const a = M.anchor;
  M.off(); M.el.remove(); M.el = M.anchor = M.off = null;
  a.setAttribute("aria-expanded", "false");
  if (refocus && a.isConnected) a.focus();
}

/* ---------- fuzzy matching: subsequence, scored for word starts, runs and prefixes ---------- */
function fuzzy(q, s) {
  q = String(q || "").toLowerCase().trim(); s = String(s || "");
  if (!q) return { score: 0, pos: [] };
  const pos = [];
  let score = 0, j = 0;
  for (let i = 0; i < s.length && j < q.length; i++) {
    if (s[i].toLowerCase() !== q[j]) continue;
    const start = i === 0 || /[\s\-_/.:(]/.test(s[i - 1]) || (s[i] !== s[i].toLowerCase() && s[i - 1] === s[i - 1].toLowerCase());
    score += start ? 8 : pos.length && pos[pos.length - 1] === i - 1 ? 6 : 1;
    pos.push(i); j++;
  }
  if (j < q.length) return null;
  if (s.toLowerCase().startsWith(q)) score += 12;
  return { score: score - (pos[pos.length - 1] - pos[0]) * 0.2 - s.length * 0.05, pos };
}
const SVG_OK = new Set(Object.values(typeof ICON === "object" && ICON ? ICON : {}).concat(BOLT));
function iconMarkup(icon) {  // only known icon geometry becomes SVG; anything else is text, never HTML
  if (!icon) return "";
  const inner = ICON && ICON[icon] ? ICON[icon] : SVG_OK.has(icon) ? icon : "";
  return inner ? svg(inner) : "";
}
function hl(s, pos) {
  if (!pos || !pos.length) return E(s);
  const hit = new Set(pos);
  return String(s).split("").map((c, i) => hit.has(i) ? `<b>${E(c)}</b>` : E(c)).join("");
}

/* ---------- command palette ---------- */
const P = { el: null, input: null, list: null, q: "", sel: 0, flat: [], remote: [], seq: 0, timer: 0, prev: null };
const ACTIONS = new Map();
function action(a) { const id = a.id || a.label; ACTIONS.set(id, a); return () => ACTIONS.delete(id); }
const PAGEKEYS = { Overview: "g o", Board: "g b", Agents: "g a", Workflow: "g w", Memory: "g m", Setup: "g s" };
const groupOf = t => typeof wsOf === "function" ? wsOf(t)[0] : (GROUPS.find(g => g[1].includes(t)) || [""])[0];
function paletteSections(q) {
  const pages = T.map((t, i) => ({ label: t, sub: groupOf(t), icon: ICON[t], kbd: PAGEKEYS[t] || (i < 9 ? String(i + 1) : ""), run: () => nav(t) }));
  const acts = [...ACTIONS.values()].map(a => ({ label: a.label, kw: a.keywords, icon: a.icon || BOLT, kbd: a.kbd, sub: "action", run: a.run }));
  if (!q) {
    const rec = recent().filter(t => t !== tab && T.includes(t)).slice(0, 5).map(t => Object.assign({}, pages[T.indexOf(t)], { sub: "recent" }));
    return [["Recent", rec], ["Pages", pages], ["Actions", acts]];
  }
  const rank = xs => xs.map(x => {
    const a = fuzzy(q, x.label), b = x.kw ? fuzzy(q, x.kw) : null;
    if (!a && !b) return null;
    return Object.assign({}, x, { score: Math.max(a ? a.score : -1e9, b ? b.score - 10 : -1e9), pos: a ? a.pos : [] });
  }).filter(Boolean).sort((x, y) => y.score - x.score);
  const secs = [["Pages", rank(pages)], ["Actions", rank(acts)]].filter(s => s[1].length).sort((x, y) => y[1][0].score - x[1][0].score);
  secs.push(["Memory", [{ label: `Search memory for "${q}"`, icon: ICON.Memory, run: () => { window._mq = q; window._mfocus = true; nav("Memory"); } }]]);
  if (P.remote.length) {  // places and memories from /api/palette, opened exactly as legacy palGo does
    secs.push(["Matches", P.remote.map(x => ({ label: x.label, sub: x.sub, icon: x.kind === "place" ? ICON["Org chart"] : ICON.Memory,
      run: () => { palItems = [x]; palGo(0); } }))]);
  }
  return secs;
}
function pDraw() {
  let h = "";
  P.flat = [];
  for (const [title, items] of paletteSections(P.q.trim())) {
    if (!items.length) continue;
    h += `<div class="palette-sec" role="presentation">${E(title)}</div>`;
    for (const it of items) {
      const i = P.flat.push(it) - 1;
      h += `<div class="palette-item" role="option" id="kpo${i}" data-i="${i}" aria-selected="false">${iconMarkup(it.icon)}<span class="palette-label">${hl(it.label, it.pos)}</span>${it.sub ? `<small>${E(it.sub)}</small>` : ""}${it.kbd ? `<span class="palette-kbd">${kbd(it.kbd)}</span>` : ""}</div>`;
    }
  }
  P.list.innerHTML = h || '<div class="palette-none">No matches</div>';
  pSel(Math.min(P.sel, P.flat.length - 1));
}
function pSel(i) {
  const old = P.list.querySelector(".palette-item.sel");
  if (old) { old.classList.remove("sel"); old.setAttribute("aria-selected", "false"); }
  P.sel = Math.max(0, i);
  const el = document.getElementById("kpo" + P.sel);
  if (!el) { P.input.removeAttribute("aria-activedescendant"); return; }
  el.classList.add("sel"); el.setAttribute("aria-selected", "true");
  P.input.setAttribute("aria-activedescendant", el.id);
  el.scrollIntoView({ block: "nearest" });
}
function pRemote() {
  clearTimeout(P.timer);
  const q = P.q.trim(), seq = ++P.seq;
  if (q.length < 2) return;
  P.timer = setTimeout(async () => {
    let r;
    try { r = await api("palette?q=" + encodeURIComponent(q)); } catch (e) { return; }
    if (seq !== P.seq || !P.el || !Array.isArray(r)) return;
    P.remote = r;
    pDraw();
  }, 150);
}
function pPick(i) {
  const it = P.flat[i];
  if (!it) return;
  pClose();
  try { it.run(); } catch (e) { toastFn(errText(e), { kind: "err" }); }
}
function pOpen(q = "") {
  if (P.el) { P.input.focus(); P.input.select(); return; }
  mClose(false);
  Object.assign(P, { prev: document.activeElement, q, sel: 0, remote: [] });
  const el = document.createElement("div");
  el.className = "palette open";
  el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); el.setAttribute("aria-label", "Command palette");
  el.innerHTML = `<div class="palette-box"><input class="palette-q" type="text" role="combobox" aria-expanded="true" aria-controls="kpal-list" aria-autocomplete="list" autocomplete="off" spellcheck="false" aria-label="Search pages, actions and memory" placeholder="Jump to a page, run an action, search memory"><div class="palette-list" id="kpal-list" role="listbox" aria-label="Results"></div><div class="palette-foot"><span>${kbd("arrowup")}${kbd("arrowdown")} move</span><span>${kbd("enter")} open</span><span>${kbd("escape")} close</span></div></div>`;
  P.el = el; P.input = el.querySelector(".palette-q"); P.list = el.querySelector(".palette-list");
  P.input.value = q;
  el.addEventListener("mousedown", e => { if (e.target === el) { e.preventDefault(); pClose(); } });
  P.input.addEventListener("input", () => { P.q = P.input.value; P.sel = 0; P.remote = []; pDraw(); pRemote(); });
  P.input.addEventListener("keydown", e => {
    const n = P.flat.length, back = e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey);
    if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Tab") { e.preventDefault(); if (n) pSel((P.sel + (back ? n - 1 : 1)) % n); }
    else if (e.key === "Enter") { e.preventDefault(); pPick(P.sel); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); pClose(); }
  });
  P.list.addEventListener("mousemove", e => { const it = e.target.closest(".palette-item"); if (it && +it.dataset.i !== P.sel) pSel(+it.dataset.i); });
  P.list.addEventListener("click", e => { const it = e.target.closest(".palette-item"); if (it) pPick(+it.dataset.i); });
  document.body.appendChild(el);
  pDraw();
  pRemote();
  P.input.focus();
}
function pClose(restore = true) {
  if (!P.el) return;
  clearTimeout(P.timer); P.seq++;
  P.el.remove(); P.el = null;
  if (restore && P.prev && P.prev.isConnected && P.prev.focus) P.prev.focus();
  P.prev = null;
}

/* ---------- keyboard shortcuts ---------- */
const MODS = ["ctrl", "alt", "meta", "shift"];
const ALIAS = { mod: MAC ? "meta" : "ctrl", cmd: "meta", control: "ctrl", option: "alt" };
function step(s) {  // "Shift+Ctrl+P" -> "ctrl+shift+p"; "mod" is meta on a Mac, ctrl elsewhere
  const m = /^((?:(?:ctrl|control|alt|option|meta|cmd|mod|shift)\+)*)(.+)$/i.exec(String(s).trim());
  if (!m) return "";
  const mods = new Set(m[1].toLowerCase().split("+").filter(Boolean).map(x => ALIAS[x] || x));
  const k = m[2].toLowerCase();
  return MODS.filter(x => mods.has(x)).concat(k === "esc" ? "escape" : k).join("+");
}
const parse = combo => String(combo).trim().split(/\s+/).map(step);
function fromEvent(e) {  // shift is implied by symbols like "?" so it is dropped for them
  const k = e.key === " " ? "space" : String(e.key || ""), symbol = k.length === 1 && !/[a-z]/i.test(k), m = [];
  if (e.ctrlKey) m.push("ctrl");
  if (e.altKey) m.push("alt");
  if (e.metaKey) m.push("meta");
  if (e.shiftKey && !symbol) m.push("shift");
  return m.concat(k.toLowerCase()).join("+");
}
const KEYNAME = { meta: MAC ? "⌘" : "Meta", ctrl: "Ctrl", alt: MAC ? "⌥" : "Alt", shift: MAC ? "⇧" : "Shift",
  escape: "Esc", enter: "↵", arrowup: "↑", arrowdown: "↓", arrowleft: "←", arrowright: "→", space: "Space" };
function kbd(combo) {
  return parse(combo).map(s => s.split(/\+(?!$)/).map(x => `<kbd>${E(KEYNAME[x] || x)}</kbd>`).join("")).join(" ");
}
const BINDS = [];
function bind(combo, fn, desc = "") {
  const steps = parse(combo), sig = steps.join(" ");
  const old = BINDS.findIndex(b => b.sig === sig);
  if (old >= 0) BINDS.splice(old, 1);
  const b = { combo, steps, sig, fn, desc };
  BINDS.push(b);
  return () => { const i = BINDS.indexOf(b); if (i >= 0) BINDS.splice(i, 1); };
}
const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
const prefixOf = (a, b) => a.length < b.length && a.every((x, i) => x === b[i]);
let buf = [], bufAt = 0;
function onKey(e) {  // window capture: runs before legacy's document handler, so a key handled here stops there
  if (e.defaultPrevented || e.isComposing || e.repeat || /^(Shift|Control|Alt|Meta)$/.test(e.key)) return;
  if (D.el && e.key === "Escape") { e.preventDefault(); e.stopPropagation(); dClose(); return; }
  if (P.el || M.el || D.el || typing(e.target)) return;  // palette, menu and drawer own their keys; never fire while typing
  const st = fromEvent(e), now = Date.now();
  let seq = (now - bufAt < 1500 ? buf : []).concat(st);
  bufAt = now;
  let hit = BINDS.find(b => same(b.steps, seq));
  if (!hit && !BINDS.some(b => prefixOf(seq, b.steps))) { seq = [st]; hit = BINDS.find(b => same(b.steps, seq)); }
  buf = !hit && BINDS.some(b => prefixOf(seq, b.steps)) ? seq : [];
  if (hit) { e.preventDefault(); e.stopPropagation(); hit.fn(e); }
}
const LEGACY_KEYS = [  // handled by legacy.js's own keydown listener
  [["mod+k", "/"], "Command palette (on Memory, / focuses the search box)"],
  [["1-9"], "Jump to the first nine pages"], [["[", "]"], "Previous or next page"],
  [["r"], "Refresh the data on this page"],
  [["j", "k"], "Memory: move down or up"], [["x"], "Memory: select the row"], [["e"], "Memory: edit the row"],
  [["enter"], "Memory: expand the row"], [["+", "-"], "Org chart: zoom in or out"], [["esc"], "Close a drawer, menu or palette"],
];
function help() {
  if (D.el && D.opts.id === "keys") { dClose(); return; }
  const row = (keys, desc) => `<div class="keys-row"><span class="keys-k">${keys.map(kbd).join(" ")}</span><span class="keys-d">${E(desc)}</span></div>`;
  dOpen({ id: "keys", title: "Keyboard shortcuts", subtitle: "They never fire while you are typing in a field.",
    html: `<div class="keys">${BINDS.filter(b => b.desc).map(b => row([b.combo], b.desc)).join("")}${LEGACY_KEYS.map(([k, d]) => row(k, d)).join("")}</div>` });
}

/* ---------- wiring ---------- */
document.addEventListener("click", e => {
  const t = e.target.closest && e.target.closest("[data-kact],[data-kv-full]");
  if (!t) return;
  if (t.dataset.kact) { const fn = REG.get(t.dataset.kact); if (fn) { e.preventDefault(); fn(e); } return; }
  const open = t.getAttribute("aria-expanded") === "true";  // kv: toggle between the short and the full value
  if (!open) t.dataset.kvShort = t.textContent;
  t.textContent = open ? t.dataset.kvShort : t.dataset.kvFull;
  t.setAttribute("aria-expanded", String(!open));
});
window.addEventListener("keydown", onKey, true);
window.addEventListener("hashchange", route);
window.addEventListener("popstate", route);
Object.entries(PAGEKEYS).forEach(([t, c]) => bind(c, () => nav(t), "Go to " + t));
bind("?", help, "Show keyboard shortcuts");
action({ id: "theme", label: "Toggle light or dark theme", keywords: "appearance mode dark light", kbd: "t", run: () => theme() });
action({ id: "refresh", label: "Refresh the data on this page", keywords: "reload render update", kbd: "r", run: () => render() });
action({ id: "shortcuts", label: "Keyboard shortcuts", keywords: "keys help hotkeys", kbd: "?", run: help });
action({ id: "copy-link", label: "Copy a link to this view", keywords: "url share permalink", run: () => copy(location.origin + location.pathname + location.hash) });
OPENERS.set("keys", help);
R.key = keyOf(readHash());
remember(tab);
setTimeout(() => syncDrawer(), 0);  // a #Page?drawer=<id> link opens once every script has registered its openers

return {
  toast: toastFn, optimistic, menu, action, copy, kv, spark, empty, skeleton, on, emit, refresh, noteChange, fuzzy, go: nav,
  drawer: { open: dOpen, close: () => dClose(), on: drawerOn },
  palette: { open: pOpen, close: () => pClose(), isOpen: () => !!P.el },
  keys: { bind, help, parse, fromEvent, kbd },
  fmt: { num, money, ago, tokens },
};
})();

/* Overrides of legacy.js globals (function declarations: the last one in the script wins). */
function go(t) { K.go(t); }
function toast(msg, k = "ok") { return K && K.toast(msg, { kind: k }); }
function pal() { K.palette.open(); }
function palClose() { K.palette.close(); }
// legacy's EventSource calls stopPoll on open, message and change. The last function declaration in this script wins,
// so this keeps the poll from restarting. legacy's "change" listener hands its payload to K.noteChange.
function stopPoll() {
  if (window._poll) { clearInterval(window._poll); window._poll = 0; }
}
