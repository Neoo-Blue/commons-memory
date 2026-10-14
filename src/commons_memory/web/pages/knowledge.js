/* Knowledge workspace. Replaces the legacy pages on V. Loaded last, so the
   function declarations below replace legacy memEdit, cite, retroRun, retroDecide and retroAll. */

let knGen = 0;
let knPopEl = null;

function knSvg(inner) {
  return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + inner + "</svg>";
}
const KN_PATH = {
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16.2 16.2 20 20"/>',
  memory: '<circle cx="12" cy="12" r="7"/><path d="M12 8.5v4l2.2 1.4"/>',
  decision: '<path d="m12 3 2.1 4.4 4.8.7-3.5 3.4.8 4.8L12 14.2 7.8 16.3l.8-4.8L5.1 8.1l4.8-.7z"/>',
  fact: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4.5M12 16.2v.4"/>',
  handoff: '<path d="M4 8h10M14 8l-3-3M14 8l-3 3M20 16H10M10 16l3-3M10 16l3 3"/>',
  observation: '<path d="M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.5"/>',
};
function knKindIcon(kind, hot) {
  const p = KN_PATH[kind] || KN_PATH.memory;
  return '<span class="kn-kind' + (hot ? " hot" : "") + '" title="' + E(kind || "memory") + '">' + knSvg(p) + "</span>";
}
function knLead(text) { return '<p class="page-head-subtitle">' + E(text) + "</p>"; }
function knBegin(name) {
  knPopClose();
  const m = document.getElementById("m");
  if (m && m.dataset.tab !== name && window.K) m.innerHTML = K.skeleton(6);
}
function knNeed(r) { if (r && r.error) throw new Error(r.error); return r; }
function knNum(v) { return window.K && K.fmt ? K.fmt.num(v || 0) : String(v || 0); }
function knChip(on, attrs, label) {
  return '<button type="button" class="chip' + (on ? " on" : "") + '" aria-pressed="' + (on ? "true" : "false") + '" ' + attrs + ">" + E(label) + "</button>";
}
function knTitle(x) {
  const summary = String(x.summary || "").replace(/\s+/g, " ").trim();
  const title = String(x.title || "").trim();
  if (title) return title;
  return summary.slice(0, 180) || x.id || "Memory";
}
function knTagList(s) { return String(s || "").split(",").map(t => t.trim()).filter(Boolean); }
function knTagChips(s) {
  return knTagList(s).map(t => '<button type="button" class="chip" data-kn="droptag" data-tag="' + E(t) + '" aria-label="Remove tag ' + E(t) + '">' + E(t) + ' <span aria-hidden="true">x</span></button>').join("");
}
function knMaybeKv(s) {
  if (s == null || s === "") return '<span class="kn-hint">empty</span>';
  let v = s;
  if (typeof s === "string") {
    try { v = JSON.parse(s); } catch (e) { return '<span class="kn-mono">' + E(String(s).slice(0, 180)) + "</span>"; }
  }
  if (v && typeof v === "object") return K.kv(v);
  return '<span class="kn-mono">' + E(String(v).slice(0, 180)) + "</span>";
}
function knPopClose() {
  if (!knPopEl) return;
  if (knPopEl._off) knPopEl._off();
  knPopEl.remove();
  knPopEl = null;
}
function knPlace(el, anchor) {
  el.style.position = "fixed";
  el.style.zIndex = "80";
  document.body.appendChild(el);
  const r = anchor.getBoundingClientRect();
  const mh = el.offsetHeight, mw = Math.max(el.offsetWidth, 220);
  el.style.width = Math.min(mw, innerWidth - 16) + "px";
  const top = r.bottom + 4;
  const above = r.top - el.offsetHeight - 4;
  el.style.top = (top + el.offsetHeight > innerHeight - 8 && above >= 8 ? above : top) + "px";
  el.style.left = Math.max(8, Math.min(r.left, innerWidth - el.offsetWidth - 8)) + "px";
}
function knWatchPop(el) {
  const down = ev => { if (!el.contains(ev.target)) knPopClose(); };
  const onKey = ev => {
    if (ev.key !== "Escape" || !knPopEl) return;
    ev.preventDefault();
    ev.stopPropagation();
    knPopClose();
  };
  setTimeout(() => document.addEventListener("pointerdown", down, true), 0);
  window.addEventListener("keydown", onKey, true);
  el._off = () => {
    document.removeEventListener("pointerdown", down, true);
    window.removeEventListener("keydown", onKey, true);
  };
  knPopEl = el;
}
function knPick(anchor, title, items, onPick) {
  knPopClose();
  const el = document.createElement("div");
  el.className = "menu kn-pop";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", title);
  const h = document.createElement("div");
  h.className = "kn-pop-h";
  h.textContent = title;
  const input = document.createElement("input");
  input.className = "kn-pop-q";
  input.type = "search";
  input.setAttribute("aria-label", title);
  input.placeholder = "Filter";
  const list = document.createElement("div");
  list.className = "kn-pop-list";
  el.append(h, input, list);
  const draw = () => {
    const q = input.value;
    const ranked = items.map(it => {
      const f = window.K && K.fuzzy ? K.fuzzy(q, it.label) : (!q || it.label.toLowerCase().includes(q.toLowerCase()) ? { score: 0 } : null);
      return f ? Object.assign({ score: f.score }, it) : null;
    }).filter(Boolean).sort((a, b) => b.score - a.score);
    list.textContent = "";
    if (!ranked.length) {
      const n = document.createElement("div");
      n.className = "kn-pop-note";
      n.textContent = "No matches";
      list.appendChild(n);
      return;
    }
    ranked.forEach(it => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "menu-item" + (it.on ? " on" : "");
      b.setAttribute("role", "menuitem");
      b.textContent = it.label;
      b.addEventListener("click", () => { knPopClose(); onPick(it); });
      list.appendChild(b);
    });
  };
  input.addEventListener("input", draw);
  el.addEventListener("keydown", ev => {
    if (ev.key === "Enter") {
      const b = list.querySelector(".menu-item");
      if (b) { ev.preventDefault(); b.click(); }
    }
  });
  draw();
  knPlace(el, anchor);
  knWatchPop(el);
  input.focus();
}
function knTagPop(anchor, ids) {
  if (!ids.length) return toast("Select rows first", "err");
  knPopClose();
  const el = document.createElement("div");
  el.className = "menu kn-pop";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", "Add tags");
  const h = document.createElement("div");
  h.className = "kn-pop-h";
  h.textContent = "Add tags";
  const stage = document.createElement("div");
  stage.className = "kn-stage";
  const input = document.createElement("input");
  input.className = "kn-pop-q";
  input.setAttribute("aria-label", "Tag");
  input.placeholder = "Type a tag, then Enter";
  const go = document.createElement("button");
  go.type = "button";
  go.className = "pri";
  go.textContent = "Add tags";
  const tags = [];
  const paint = () => {
    stage.textContent = "";
    tags.forEach((t, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = t + " x";
      b.setAttribute("aria-label", "Remove " + t);
      b.addEventListener("click", () => { tags.splice(i, 1); paint(); });
      stage.appendChild(b);
    });
    go.disabled = !tags.length;
  };
  const add = raw => {
    String(raw || "").split(",").map(t => t.trim()).filter(Boolean).forEach(t => { if (!tags.includes(t)) tags.push(t); });
    input.value = "";
    paint();
  };
  input.addEventListener("keydown", ev => {
    if (ev.key === "Enter" || ev.key === ",") { ev.preventDefault(); add(input.value); }
  });
  go.addEventListener("click", async () => {
    const use = tags.slice();
    knPopClose();
    if (!use.length) return;
    let failed = 0;
    for (const tag of use) {
      const r = await postRaw("items/bulk", { ids, action: "tag", tag });
      if (!r || r.error) { failed++; toast((r && r.error) || "tag failed", "err"); break; }
      const bad = (r.results || []).filter(x => x.error);
      if (bad.length) { failed += bad.length; toast(bad.length + " failed: " + bad[0].error, "err"); }
    }
    if (!failed) toast("Tags added", "ok");
    render();
  });
  el.append(h, stage, input, go);
  paint();
  knPlace(el, anchor);
  knWatchPop(el);
  input.focus();
}
function knMove(anchor, ids) {
  if (!ids.length) return toast("Select rows first", "err");
  const items = [{ label: "No project", value: "" }].concat((window.mprojects || []).map(p => ({ label: p.name, value: p.name })));
  knPick(anchor, "Move to project", items, it => knMoveCommit(ids, it.value));
}
function knMoveCommit(ids, project) {
  const prev = {};
  ids.forEach(id => {
    const row = (window._mfetched || window.mrows || []).find(x => x.id === id);
    prev[id] = window._kmove && window._kmove[id] !== undefined ? window._kmove[id] : (row ? row.project : undefined);
  });
  K.optimistic({
    label: project ? "Moved to " + project : "Cleared the project",
    apply() {
      window._kmove = window._kmove || {};
      ids.forEach(id => { window._kmove[id] = project || ""; });
      render();
    },
    revert() {
      ids.forEach(id => {
        if (prev[id] === undefined) delete window._kmove[id];
        else window._kmove[id] = prev[id];
      });
      render();
    },
    async commit() {
      const r = await postRaw("items/bulk", { ids, action: "move", project: project || "" });
      if (!r || r.error) return r || { error: "move failed" };
      const bad = (r.results || []).filter(x => x.error);
      ids.forEach(id => {
        if (bad.some(b => b.id === id)) {
          if (prev[id] === undefined) delete window._kmove[id];
          else window._kmove[id] = prev[id];
        } else delete window._kmove[id];
      });
      if (bad.length) toast(bad.length + " failed: " + bad[0].error, "err");
      render();
      return {};
    },
  });
}
function knTier(ids, tier) {
  if (!ids.length) return;
  const prev = {};
  ids.forEach(id => {
    const row = (window.mrows || []).find(x => x.id === id);
    prev[id] = (window._ktier && window._ktier[id]) || (row && row.tier) || "warm";
  });
  const label = tier === "pin" ? "Pinned" : tier === "archive" ? "Archived" : "Set to warm";
  K.optimistic({
    label,
    apply() {
      window._ktier = window._ktier || {};
      const shown = tier === "pin" ? "hot" : tier === "archive" ? "cold" : "warm";
      ids.forEach(id => { window._ktier[id] = shown; });
      render();
    },
    revert() {
      ids.forEach(id => { window._ktier[id] = prev[id]; });
      render();
    },
    async commit() {
      const bad = [];
      let first = null;
      for (const id of ids) {
        const r = await postRaw("item/tier", { id, tier });
        if (!r || r.error) { bad.push(id); if (!first) first = r || { error: "tier failed" }; }
        else delete window._ktier[id];
      }
      if (bad.length === ids.length && first) return first;
      bad.forEach(id => { window._ktier[id] = prev[id]; });
      if (bad.length) toast(bad.length + " failed", "err");
      render();
      return {};
    },
  });
}
function knTierMenu(anchor, ids) {
  if (!ids.length) return;
  K.menu(anchor, [
    { label: "Pin", run: () => knTier(ids, "pin") },
    { label: "Warm", run: () => knTier(ids, "warm") },
    { label: "Archive", run: () => knTier(ids, "archive") },
  ]);
}
function knDelete(ids) {
  if (!ids.length) return;
  const prevOpen = window.mopen;
  K.optimistic({
    label: ids.length > 1 ? "Deleted " + ids.length : "Deleted",
    apply() {
      window.mhide = window.mhide || {};
      ids.forEach(id => { window.mhide[id] = 1; });
      window.msel = (window.msel || []).filter(id => !ids.includes(id));
      if (ids.includes(window.mopen)) {
        window.mopen = null;
        window.medit = null;
        window._draft = null;
        knGen++;
        if (window.K) K.drawer.close();
      }
      render();
    },
    revert() {
      ids.forEach(id => delete window.mhide[id]);
      if (prevOpen && ids.includes(prevOpen)) window.mopen = prevOpen;
      render();
    },
    async commit() {
      const r = ids.length === 1
        ? await postRaw("item/delete", { id: ids[0] })
        : await postRaw("items/bulk", { ids, action: "delete" });
      if (!r || r.error) return r || { error: "delete failed" };
      const bad = (r.results || []).filter(x => x.error);
      ids.forEach(id => delete window.mhide[id]);
      if (bad.length) {
        toast(bad.length + " failed: " + bad[0].error, "err");
        render();
      }
      return {};
    },
  });
}
function knDecorate(x) {
  const tier = window._ktier && window._ktier[x.id];
  const move = window._kmove && window._kmove[x.id];
  if (!tier && move === undefined) return x;
  return Object.assign({}, x, tier ? { tier } : {}, move !== undefined ? { project: move } : {});
}
function knVisible(x) {
  if ((window.mhide || {})[x.id]) return false;
  if (window.ma && (x.agent || "") !== window.ma) return false;
  if (window.mtr && (x.tier || "warm") !== window.mtr) return false;
  if (+window.mwhen) {
    const t = Date.parse(x.created_at || "");
    if (!(t > 0) || Date.now() - t > +window.mwhen) return false;
  }
  return true;
}
function knClearFilters() {
  window.mk = "";
  window.mp = "";
  window.mc = false;
  window.ma = "";
  window.mtr = "";
  window.mwhen = 0;
  render();
}
function knFiltersOn() {
  return !!(window.mk || window.mp || window.mc || window.ma || window.mtr || +window.mwhen);
}
function knNodeName(nodes, id) {
  const n = nodes && nodes[id];
  return String((n && (n.name || n.label)) || id || "").slice(0, 42);
}
function knMemHtml(detail, links, id) {
  const editing = window.medit === id;
  if (editing) {
    const draft = window._draft && window._draft.id === id ? window._draft : null;
    const body = draft ? draft.content : (detail.content || detail.statement || detail.text || "");
    const tags = draft ? draft.tags : (Array.isArray(detail.tags) ? detail.tags.join(", ") : "");
    return '<div class="kn-editor"><label for="med">Content</label><textarea id="med" aria-label="Content">' + E(body) + "</textarea>"
      + '<label for="metags">Tags</label><input id="metags" aria-label="Tags" value="' + E(tags) + '" placeholder="tags, comma separated">'
      + '<div class="kn-tagchips" id="metagbox">' + knTagChips(tags) + "</div>"
      + '<p class="kn-hint">Escape closes. Command or Ctrl+Enter saves.</p></div>';
  }
  const body = detail.content || detail.statement || detail.text || JSON.stringify(detail, null, 1);
  const tier = (window._ktier && window._ktier[id]) || detail.tier || "warm";
  const meta = {
    kind: detail.kind || "",
    project: detail.project || "",
    agent: detail.agent || "",
    tier,
    created: detail.created_at || "",
    updated: detail.updated_at || "",
    conflict: detail.conflict ? "yes" : "no",
    verified: detail.authenticated ? "yes" : "no",
  };
  if (detail.meta && typeof detail.meta === "object") {
    Object.keys(detail.meta).forEach(k => { if (meta[k] == null) meta[k] = detail.meta[k]; });
  }
  const tags = Array.isArray(detail.tags) ? detail.tags : [];
  const nodes = (links && links.edges) ? links.nodes || {} : {};
  const edges = (links && links.edges) || detail.neighbors || [];
  const edgeHtml = edges.map(e => '<div class="kn-sub"><span>' + E(knNodeName(nodes, e.src)) + "</span><span>" + E(e.type || "link") + "</span><span>" + E(knNodeName(nodes, e.dst)) + "</span></div>").join("");
  const cited = ((links && links.kb) || []).map(k => '<button type="button" data-kn="cite-kb" data-bucket="' + E(k.bucket) + '" data-section="' + E(k.section || "") + '">' + E(k.bucket) + " / " + E(k.section || "") + "</button>").join("");
  const project = detail.project || "";
  const related = (window._mfetched || []).filter(x => x.id !== id && project && x.project === project).slice(0, 6);
  const relHtml = related.map(x => '<button type="button" data-kn="show" data-id="' + E(x.id) + '">' + E(knTitle(x)) + "<small>" + E([x.kind, x.agent].filter(Boolean).join(" · ")) + "</small></button>").join("");
  const seg = (t, label) => '<button type="button" data-kn="settier" data-id="' + E(id) + '" data-t="' + t + '" class="' + (((t === "pin" && tier === "hot") || (t === "warm" && tier === "warm") || (t === "archive" && tier === "cold")) ? "on" : "") + '" aria-pressed="' + (((t === "pin" && tier === "hot") || (t === "warm" && tier === "warm") || (t === "archive" && tier === "cold")) ? "true" : "false") + '">' + label + "</button>";
  return '<div class="kn-block" style="margin-top:0"><div class="seg kn-tierseg" role="group" aria-label="Tier">' + seg("pin", "Pin") + seg("warm", "Warm") + seg("archive", "Archive") + "</div>"
    + (tags.length ? '<div class="kn-tagchips">' + tags.map(t => '<span class="chip">' + E(t) + "</span>").join("") + "</div>" : "")
    + '<button type="button" data-kn="move" data-id="' + E(id) + '">Move to project</button> <button type="button" data-kn="copy" data-id="' + E(id) + '">Copy id</button></div>'
    + "<pre class=\"kn-body\">" + E(String(body)) + "</pre>"
    + '<div class="kn-block"><h2>Details</h2>' + K.kv(meta) + "</div>"
    + '<div class="kn-block"><h2>Graph neighbours</h2><div class="kn-rel" style="max-height:220px;overflow:auto">' + (edgeHtml || '<span class="kn-hint">No links yet.</span>') + "</div></div>"
    + '<div class="kn-block"><h2>Cited in</h2><div class="kn-sources">' + (cited || '<span class="kn-hint">Not cited in a bucket yet.</span>') + "</div></div>"
    + '<div class="kn-block"><h2>Related</h2><div class="kn-rel">' + (relHtml || '<span class="kn-hint">Nothing else in this project is on this page.</span>') + "</div></div>";
}
function knBindEditor(id) {
  setTimeout(() => {
    const tags = document.getElementById("metags");
    const box = document.getElementById("metagbox");
    if (tags && box && !tags.dataset.knBound) {
      tags.dataset.knBound = "1";
      tags.addEventListener("input", () => { box.innerHTML = knTagChips(tags.value); });
    }
    const med = document.getElementById("med");
    if (med && window.medit === id) {
      med.focus();
      try { med.setSelectionRange(med.value.length, med.value.length); } catch (err) {}
    }
  }, 40);
}
function knMemOpen(detail, links, id) {
  const gen = ++knGen;
  const editing = window.medit === id;
  const title = knTitle(detail.title ? detail : { title: detail.title, summary: detail.summary || detail.content || detail.id, id: detail.id });
  K.drawer.open({
    id: "mem:" + id,
    title: title || id,
    subtitle: [detail.kind, detail.project, detail.agent].filter(Boolean).join(" · "),
    html: knMemHtml(detail, links, id),
    actions: editing
      ? [
        { label: "Cancel", run: () => { window.medit = null; window._draft = null; render(); } },
        { label: "Save", kind: "primary", run: () => memSave(id) },
      ]
      : [
        { label: "Edit", kind: "primary", run: () => memEdit(id) },
        { label: "Open in graph", run: () => { window.gs = { scope: "node", id }; go("Graph"); } },
        { label: "Delete", kind: "danger", run: () => knDelete([id]) },
      ],
    onClose() {
      if (gen !== knGen) return;
      if (window.mopen === id) {
        window.mopen = null;
        window.medit = null;
        window._draft = null;
        render();
      }
    },
  });
  if (editing) knBindEditor(id);
}
function knMemRow(x, i) {
  const on = (window.msel || []).includes(x.id);
  const cur = i === window.mcur;
  const hot = x.tier === "hot";
  const when = rel(x.created_at) || "";
  const bits = [x.kind, x.project, x.agent].filter(Boolean).map(s => "<span>" + E(s) + "</span>").join("");
  const flags = (x.conflict ? '<span class="badge bad">Conflict</span>' : "")
    + (x.agent && !x.authenticated ? '<span class="badge" title="Written by a named agent without an api key">Unverified</span>' : "")
    + (x.tier === "cold" ? '<span class="badge">Archived</span>' : "");
  return '<div class="mrow row' + (cur ? " cur" : "") + (on ? " on" : "") + '" data-i="' + i + '">'
    + '<label class="kn-check"><input type="checkbox" data-m="sel" data-id="' + E(x.id) + '" data-i="' + i + '"' + (on ? " checked" : "") + ' aria-label="Select"></label>'
    + knKindIcon(x.kind, hot)
    + '<button type="button" class="row-content" data-m="open" data-id="' + E(x.id) + '"><div class="row-title">' + E(knTitle(x)) + '</div><div class="row-subtitle kn-sub">' + bits + flags + "</div></button>"
    + '<span class="kn-when">' + E(when) + "</span>"
    + '<div class="row-actions"><button type="button" data-m="open" data-id="' + E(x.id) + '">Open</button>'
    + '<button type="button" data-m="edit" data-id="' + E(x.id) + '">Edit</button>'
    + '<button type="button" data-kn="tier-menu" data-id="' + E(x.id) + '">Tier</button>'
    + '<button type="button" data-m="graph" data-id="' + E(x.id) + '">Graph</button>'
    + '<button type="button" class="bad" data-kn="del" data-id="' + E(x.id) + '">Delete</button></div></div>';
}

V["Memory"] = async function () {
  knBegin("Memory");
  const q = window._mq || "", kind = window.mk || "", proj = window.mp || "";
  const params = new URLSearchParams({ q, limit: "120", max_tokens: "16000" });
  if (kind) params.set("kind", kind);
  if (proj) params.set("project", proj);
  if (window.mc) params.set("conflict", "1");
  const [r0, c0, p0, a0] = await Promise.all([
    api("search?" + params),
    api("conflicts"),
    api("projects"),
    api("agents"),
  ]);
  knNeed(r0); knNeed(c0); knNeed(p0); knNeed(a0);
  const conflicts = Array.isArray(c0) ? c0 : [];
  const projects = Array.isArray(p0) ? p0 : [];
  const agents = Array.isArray(a0) ? a0 : [];
  window.mprojects = projects;
  let fetched = (r0.results || []).map(knDecorate);
  if (window.mfocus && !fetched.some(x => x.id === window.mfocus)) {
    try {
      const d = await api("item?id=" + encodeURIComponent(window.mfocus) + "&max_tokens=4000");
      if (d && d.id && !d.error) fetched.unshift(knDecorate({
        id: d.id, kind: d.kind || "memory", title: d.title || "", summary: d.summary || d.title || "",
        project: d.project, agent: d.agent, created_at: d.created_at, conflict: !!d.conflict,
        tier: d.tier || "warm", authenticated: !!d.authenticated,
      }));
    } catch (err) { toast(String(err.message || err), "err"); }
  }
  window._mfetched = fetched;
  const rows = fetched.filter(knVisible);
  window.mrows = rows;
  if (window.mcur == null || window.mcur >= rows.length) window.mcur = rows.length ? 0 : -1;
  let detail = null, links = null;
  if (window.mopen) {
    try {
      const pair = await Promise.all([
        api("item?id=" + encodeURIComponent(window.mopen) + "&max_tokens=20000"),
        api("backlinks?id=" + encodeURIComponent(window.mopen)).catch(() => null),
      ]);
      if (pair[0] && pair[0].error) throw new Error(pair[0].error);
      detail = pair[0];
      links = pair[1] && !pair[1].error ? pair[1] : null;
    } catch (err) {
      toast(String(err.message || err), "err");
      window.mopen = null;
      window.medit = null;
      detail = null;
    }
  }
  const kinds = ["memory", "decision", "fact", "handoff", "observation"];
  if (kind && !kinds.includes(kind)) kinds.push(kind);
  const agentNames = [];
  agents.forEach(a => { if (a.name && !agentNames.includes(a.name)) agentNames.push(a.name); });
  fetched.forEach(x => { if (x.agent && !agentNames.includes(x.agent)) agentNames.push(x.agent); });
  const kindChips = kinds.map(k => knChip(kind === k, 'data-m="kind" data-k="' + E(k) + '"', k)).join("");
  const projChips = knChip(!proj, 'data-m="proj" data-k=""', "All")
    + projects.map(p => knChip(proj === p.name, 'data-m="proj" data-k="' + E(p.name) + '"', p.name)).join("");
  const agentChips = knChip(!window.ma, 'data-kn="agent" data-agent=""', "All")
    + agentNames.map(n => knChip(window.ma === n, 'data-kn="agent" data-agent="' + E(n) + '"', n)).join("");
  const tiers = [["", "All"], ["hot", "Pinned"], ["warm", "Warm"], ["cold", "Archived"]];
  const tierChips = tiers.map(([v, label]) => knChip((window.mtr || "") === v, 'data-kn="tierf" data-tier="' + v + '"', label)).join("");
  const dates = [["0", "All"], ["86400000", "24 hours"], ["604800000", "7 days"], ["2592000000", "30 days"]];
  const dateChips = dates.map(([v, label]) => knChip(String(+window.mwhen || 0) === String(+v), 'data-kn="when" data-ms="' + v + '"', label)).join("");
  const shown = rows.length;
  const loaded = (r0.results || []).length;
  const total = r0.total == null ? loaded : r0.total;
  const count = (shown === loaded ? shown + " shown" : shown + " of " + loaded + " shown")
    + (total > loaded ? " · " + total + " match" : "");
  const list = rows.length
    ? '<div class="list">' + rows.map(knMemRow).join("") + "</div>"
    : K.empty({
      icon: "Memory",
      title: "No memories",
      hint: "Try another query, or clear the filters.",
      actions: knFiltersOn() ? [{ label: "Clear filters", run: knClearFilters }] : [],
    });
  const sel = (window.msel || []).filter(id => rows.some(x => x.id === id));
  const bulk = sel.length
    ? '<div class="kn-bulk" role="toolbar" aria-label="Bulk actions"><b>' + sel.length + ' selected</b>'
      + '<button type="button" data-kn="bulk-tag">Add tags</button>'
      + '<button type="button" data-kn="bulk-move">Move</button>'
      + '<button type="button" data-kn="bulk-tier">Tier</button>'
      + '<button type="button" class="bad" data-kn="bulk-del">Delete</button>'
      + '<button type="button" data-kn="bulk-clear">Clear</button></div>'
    : "";
  const conflictBody = conflicts.length
    ? '<div class="list">' + conflicts.map(x => {
      const a = x.a_item || {}, b = x.b_item || {};
      const btn = (keep, item) => '<button type="button" data-m="keep" data-id="' + E(x.id) + '" data-keep="' + E(keep) + '">Keep ' + E(String(item.value || keep).slice(0, 60)) + " (" + E(item.agent || "unknown") + ")</button>";
      return '<div class="row"><div class="row-content"><div class="row-title">' + E(x.subject) + "." + E(x.predicate) + '</div></div><div class="row-actions kn-always">' + btn(x.a, a) + btn(x.b, b) + "</div></div>";
    }).join("") + "</div>"
    : '<p class="kn-hint">No open conflicts.</p>';
  if (detail && window.mopen) knMemOpen(detail, links, window.mopen);
  else if (!window.mopen && window.K && document.querySelector(".drawer.open") && location.hash.indexOf("drawer=mem:") >= 0) {
    K.drawer.close();
  }
  return '<div class="kn kn-mem">' + knLead("Search what agents have remembered.")
    + '<div class="kn-search">' + knSvg(KN_PATH.search)
    + '<input id="mq" type="search" value="' + E(q) + '" placeholder="Search memory" autocomplete="off" aria-label="Search memory">'
    + '<button type="button" class="kn-recent" data-m="recent">Recent</button></div>'
    + '<div class="kn-filters' + (window.knMore ? " open" : "") + '">'
    + '<div class="kn-filter"><span class="kn-filter-label" id="fl-kind">Kind</span><div class="kn-chips" role="group" aria-labelledby="fl-kind">' + kindChips + "</div></div>"
    + '<div class="kn-filter"><span class="kn-filter-label" id="fl-proj">Project</span><div class="kn-chips" role="group" aria-labelledby="fl-proj">' + projChips + knChip(!!window.mc, 'data-m="conf" data-k="1"', "Conflicts") + "</div></div>"
    + '<div class="kn-filter"><span class="kn-filter-label" id="fl-agent" title="Agent, tier and date apply to the memories loaded for this search.">Agent</span><div class="kn-chips" role="group" aria-labelledby="fl-agent">' + agentChips + "</div></div>"
    + '<div class="kn-filter"><span class="kn-filter-label" id="fl-tier" title="Agent, tier and date apply to the memories loaded for this search.">Tier</span><div class="kn-chips" role="group" aria-labelledby="fl-tier">' + tierChips + "</div></div>"
    + '<div class="kn-filter"><span class="kn-filter-label" id="fl-date" title="Agent, tier and date apply to the memories loaded for this search.">Date</span><div class="kn-chips" role="group" aria-labelledby="fl-date">' + dateChips + "</div></div>"
    + '<button type="button" class="kn-more" aria-expanded="' + !!window.knMore + '" onclick="window.knMore=!window.knMore;this.closest(\'.kn-filters\').classList.toggle(\'open\',window.knMore);this.setAttribute(\'aria-expanded\',window.knMore);this.textContent=window.knMore?\'Fewer filters\':\'More filters\'">' + (window.knMore ? "Fewer filters" : "More filters") + "</button>"
    + "</div>"
    + '<div class="kn-section-h"><span class="kn-count" role="status">' + E(count) + "</span>"
    + (knFiltersOn() ? '<button type="button" data-kn="clear-filters">Clear filters</button>' : "") + "</div>"
    + list + bulk
    + '<section class="kn-section"><div class="kn-section-h"><h2>Conflicts</h2><span class="kn-count">' + conflicts.length + "</span></div>" + conflictBody + "</section>"
    + '<p class="kn-hint">j and k move. x selects. e edits. Enter opens. Slash focuses search.</p></div>';
};

function knMd(s) {
  return E(s || "")
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[\[(misc\/[\w-]+)\]\]/g, (m, x) => '<a href="#" data-kn="bucket" data-id="' + E(x) + '">' + E(x) + "</a>")
    .replace(/\[((?:mem|dec|fct|hof|chk|ent|prj|ses|agt|tsk|job|sub|lft|prf|rec|lea|usg)_[0-9a-f]+)\]/g, (m, id) => '<a href="#" class="cite" data-kn="cite" data-id="' + E(id) + '">[' + id.slice(0, 7) + "...]</a>")
    .replace(/\n/g, "<br>");
}
V["Knowledge"] = async function () {
  knBegin("Knowledge");
  const L = knNeed(await api("kb_list"));
  const buckets = Array.isArray(L) ? L : [];
  const b = window.kb || (buckets[0] && buckets[0].bucket) || "misc";
  window.kb = b;
  const P = knNeed(await api("kb_page?bucket=" + encodeURIComponent(b)));
  const sections = P.sections || [];
  const sec = window._ks && P.pages && P.pages[window._ks] ? window._ks : sections[0];
  window._ks = sec || "";
  const pg = (P.pages && P.pages[sec]) || { content: "", citations: [], updated_at: "" };
  const updated = rel(pg.updated_at) || pg.updated_at || "never";
  const cites = pg.citations || [];
  const side = buckets.length
    ? buckets.map(x => '<button type="button" class="kn-bucket' + (x.bucket === b ? " on" : "") + '" data-kn="bucket" data-id="' + E(x.bucket) + '"><span>' + E(x.bucket) + "</span>" + (x.dirty ? '<span class="badge">Stale</span>' : "") + "</button>").join("")
    : '<p class="kn-hint">No buckets yet.</p>';
  const tabs = sections.map(s => '<button type="button" class="tabs-item' + (s === sec ? " on" : "") + '" role="tab" aria-selected="' + (s === sec ? "true" : "false") + '" data-kn="section" data-id="' + E(s) + '">' + E((P.titles && P.titles[s]) || s) + "</button>").join("");
  const sources = cites.map(id => '<button type="button" class="chip" data-kn="cite" data-id="' + E(id) + '">' + E(String(id).slice(0, 12)) + "</button>").join("");
  const article = sections.length
    ? '<h2>' + E((P.titles && P.titles[sec]) || "") + "</h2>"
      + '<p class="kn-hint">' + E(b) + " · updated " + E(updated) + " · " + cites.length + " sources</p>"
      + '<div class="kn-prose">' + knMd(pg.content) + "</div>"
      + (sources ? '<div class="kn-block"><h2>Sources</h2><div class="kn-sources">' + sources + "</div></div>" : "")
    : K.empty({ icon: "Knowledge", title: "Nothing in this bucket", hint: "Rebuild it after agents write into the record." });
  return '<div class="kn">' + knLead("Buckets maintained by the librarian, with citations back to the record.")
    + '<div class="kn-kb"><div class="card kn-buckets" role="navigation" aria-label="Buckets">' + side
    + '<button type="button" class="pri" data-kn="kb-rebuild">Rebuild ' + E(b) + "</button></div>"
    + '<article class="card kn-article"><div class="tabs" role="tablist">' + tabs + "</div>" + article + "</article></div></div>";
};

function cite(id) {
  if (!id) return;
  const gen = ++knGen;
  K.drawer.open({
    id: "cite:" + id,
    title: "Citation",
    subtitle: id,
    render: async () => {
      const b = knNeed(await api("backlinks?id=" + encodeURIComponent(id)));
      let d = {};
      try { d = await api("item?id=" + encodeURIComponent(id) + "&max_tokens=20000") || {}; } catch (e) { d = {}; }
      if (d && d.error) d = {};
      const label = Array.isArray(b.label) ? b.label : ["", id];
      const body = d.content || d.statement || d.text || "";
      const nodes = b.nodes || {};
      const edges = (b.edges || []).map(e => '<div class="kn-sub"><span>' + E(knNodeName(nodes, e.src)) + "</span><span>" + E(e.type || "link") + "</span><span>" + E(knNodeName(nodes, e.dst)) + "</span></div>").join("");
      const cited = (b.kb || []).map(k => '<button type="button" data-kn="cite-kb" data-bucket="' + E(k.bucket) + '" data-section="' + E(k.section || "") + '">' + E(k.bucket) + " / " + E(k.section || "") + "</button>").join("");
      return "<p class=\"kn-hint\">" + E(label[0] || "") + " · " + E(label[1] || id) + "</p>"
        + '<p><button type="button" data-kn="show" data-id="' + E(id) + '">Open in memory</button> '
        + '<button type="button" data-m="graph" data-id="' + E(id) + '">Open in graph</button></p>'
        + (body ? "<pre class=\"kn-body\">" + E(String(body).slice(0, 4000)) + "</pre>" : '<p class="kn-hint">No stored text for this id.</p>')
        + '<div class="kn-block"><h2>Cited in</h2><div class="kn-sources">' + (cited || '<span class="kn-hint">No other citations.</span>') + "</div></div>"
        + '<div class="kn-block"><h2>Links</h2><div class="kn-rel" style="max-height:240px;overflow:auto">' + (edges || '<span class="kn-hint">No links.</span>') + "</div></div>";
    },
    onClose() { if (gen !== knGen) return; },
  });
}

function knPackHtml(c, id) {
  const diff = c.diff || { added: [], removed: [], changed: [], from: null };
  const added = (diff.added || []).map(t => '<li>Added ' + E(t) + "</li>").join("");
  const removed = (diff.removed || []).map(t => '<li>Removed ' + E(t) + "</li>").join("");
  const changed = (diff.changed || []).map(x => '<details class="kn-diff"><summary>' + E(x.title || "Section") + "</summary><pre class=\"kn-pre\">" + E(x.diff || "") + "</pre></details>").join("");
  return '<p class="kn-hint">Compared with ' + E(diff.from || "nothing") + ". " + (diff.added || []).length + " added, " + (diff.removed || []).length + " removed, " + (diff.changed || []).length + " changed.</p>"
    + ((added || removed) ? "<ul>" + added + removed + "</ul>" : "")
    + changed
    + '<div class="kn-block"><h2>L1, the cache-friendly prefix</h2><pre class="kn-pre">' + E(c.l1 || "") + "</pre></div>";
}
function knPackOpen(c, id) {
  const gen = ++knGen;
  const pack = (c.packs || []).find(p => p.id === id) || { name: id };
  K.drawer.open({
    id: "pack:" + id,
    title: pack.name || id,
    subtitle: pack.agent ? pack.agent + " · " + (pack.created_at || "") : (pack.created_at || ""),
    html: knPackHtml(c, id),
    onClose() {
      if (gen !== knGen) return;
      if (window.cxs === id) { window.cxs = ""; render(); }
    },
  });
}
V["Context"] = async function () {
  knBegin("Context");
  const sel = window.cxs || "";
  const c = knNeed(await api("context" + (sel ? "?id=" + encodeURIComponent(sel) : "")));
  const sv = c.savings || {};
  const stat = (k, v, sub) => '<div class="card stat"><div class="mut">' + E(k) + '</div><div class="big">' + v + '</div><div class="mut">' + E(sub || "") + "</div></div>";
  const packs = c.packs || [];
  const list = packs.length
    ? '<div class="list">' + packs.map(p => {
      const width = k => (100 * ((p.tokens && p.tokens[k]) || 0) / Math.max(1, p.full_tokens || 1)).toFixed(1);
      return '<button type="button" class="row kn-rowbtn kn-pack' + (p.id === sel ? " on" : "") + '" data-kn="pack" data-id="' + E(p.id) + '">'
        + '<div class="row-content"><div class="row-title">' + E(p.name || p.id) + " " + (p.parent ? '<span class="badge">Delta</span>' : '<span class="badge">Base</span>') + "</div>"
        + '<div class="row-subtitle">' + E((p.gist || "").slice(0, 120)) + "</div>"
        + '<div class="kn-layers" title="L0 to L3"><span style="width:' + width("L0") + '%"></span><span style="width:' + width("L1") + '%"></span><span style="width:' + width("L2") + '%"></span><span style="width:' + width("L3") + '%"></span></div></div>'
        + '<div class="kn-when">' + ["L0", "L1", "L2", "L3"].map(k => knNum(p.tokens && p.tokens[k])).join(" · ") + "<br>" + knNum(p.tokens_saved) + " saved</div></button>";
    }).join("") + "</div>"
    : K.empty({ icon: "Context", title: "No context packs yet", hint: "Agents save a pack before a handoff, then load one layer at a time." });
  if (sel && c.diff) {
    const sig = sel + ":" + (c.l1 || "").length + ":" + ((c.diff.changed || []).length);
    if (window._packSig !== sig || !document.querySelector(".drawer.open")) {
      window._packSig = sig;
      knPackOpen(c, sel);
    }
  }
  return '<div class="kn">' + knLead("Packs agents save before a handoff, loaded one layer at a time.")
    + '<div class="grid">'
    + stat("Tokens saved by layering", knNum(sv.tokens_saved_by_layering), (sv.loads || 0) + " loads · " + knNum(sv.tokens_loaded) + " tokens loaded")
    + stat("Provider cache hits", knNum(sv.provider_cached_tokens), "Cached input tokens")
    + stat("Dedup", sv.dedup_ratio == null ? "n/a" : String(sv.dedup_ratio) + "x", (sv.section_refs || 0) + " section refs, " + (sv.blobs || 0) + " blobs")
    + stat("Packs", knNum(packs.length), "")
    + "</div>" + list + "</div>";
};

V["Graph"] = async function () {
  knBegin("Graph");
  const sc = knNeed(await api("graph_scopes"));
  const g = window.gs || { scope: "", id: "" };
  const opt = (scope, v, t) => '<option value="' + scope + "|" + E(v) + '"' + (g.scope === scope && g.id === v ? " selected" : "") + ">" + E(t) + "</option>";
  const depth = String(window.gd || 2);
  const src = g.scope
    ? "/graph.html?scope=" + encodeURIComponent(g.scope) + "&id=" + encodeURIComponent(g.id || "") + "&depth=" + encodeURIComponent(depth)
    : "/graph.html?items=1";
  const depths = ["1", "2", "3"].map(d => '<button type="button" class="' + (d === depth ? "on" : "") + '" data-kn="depth" data-d="' + d + '" aria-pressed="' + (d === depth ? "true" : "false") + '">' + d + "</button>").join("");
  return '<div class="kn kn-graph">' + knLead("Entities and the links between them.")
    + '<div class="card"><div class="kn-controls"><label>Scope<select aria-label="Graph scope" onchange="const p=this.value.split(&#39;|&#39;);window.gs={scope:p[0],id:p.slice(1).join(&#39;|&#39;)};render()">'
    + '<option value="|">Everything</option>'
    + "<optgroup label=\"Projects\">" + (sc.projects || []).map(p => opt("project", p, p)).join("") + "</optgroup>"
    + "<optgroup label=\"Jobs\">" + (sc.jobs || []).map(j => opt("job", j.id, String(j.title || "").slice(0, 60))).join("") + "</optgroup>"
    + "<optgroup label=\"Workflows\">" + (sc.workflows || []).map(w => opt("workflow", w, w)).join("") + "</optgroup>"
    + "<optgroup label=\"Schedules\">" + (sc.schedules || []).map(s => opt("schedule", s.id, s.title)).join("") + "</optgroup>"
    + (g.scope === "node" ? opt("node", g.id, "Node " + g.id) : "")
    + "</select></label>"
    + '<div><span class="kn-filter-label">Depth</span><div class="seg" role="group" aria-label="Depth">' + depths + "</div></div>"
    + '<a href="' + src + '" target="_blank" rel="noopener">Full screen</a>'
    + '<button type="button" class="pri" onclick="post(\'graph/rebuild\',{})">Rebuild links</button></div></div>'
    + '<iframe title="Graph" src="' + src + '"></iframe></div>';
};

V["Preferences"] = async function () {
  knBegin("Preferences");
  const p = knNeed(await api("prefs"));
  const rows = Array.isArray(p) ? p : [];
  const list = rows.length
    ? '<div class="list">' + rows.map(x => {
      const ev = ((x.evidence || []).slice(-1)[0] || "");
      const conf = x.confidence == null ? "" : Math.round(x.confidence * 100) + "%";
      return '<div class="row kn-pref"><div class="row-content"><input value="' + E(x.statement || "") + '" aria-label="Preference">'
        + '<div class="kn-sub"><span>' + E(x.scope || "general") + "</span><span>" + E(conf) + (x.observations ? " · " + x.observations + " seen" : "") + "</span><span>" + E(x.source || "") + "</span></div>"
        + (ev ? '<div class="kn-hint">' + E(String(ev).slice(0, 160)) + "</div>" : "")
        + '</div><div class="row-actions kn-always"><button type="button" class="pri" data-kn="pref-save" data-id="' + E(x.id) + '">Confirm</button>'
        + '<button type="button" class="bad" data-kn="pref-forget" data-id="' + E(x.id) + '">Forget</button></div></div>';
    }).join("") + "</div>"
    : K.empty({ icon: "Preferences", title: "No preferences yet", hint: "Commons adds them when an agent notices how you like to work." });
  return '<div class="kn">' + knLead("What Commons has learned about how you like to work. Agents read this at every pickup.")
    + list
    + (rows.length ? '<p><button type="button" class="bad" data-kn="pref-all">Forget everything</button></p>' : "")
    + "</div>";
};

V["Org chart"] = async function () {
  knBegin("Org chart");
  const qel = document.getElementById("ocq");
  if (qel) window._ocq = qel.value;
  const root = window.oc_root || "workspace";
  const mode = window.oc_mode || "chart";
  window.oc_open = window.oc_open || {};
  window.oc_sel = window.oc_sel || "";
  if (window.oc_jump) {
    const j = window.oc_jump;
    window.oc_jump = null;
    window.oc_center = 1;
    const parts = j.replace(/^\w+\//, "").split(".");
    const pre = j.includes("/") ? j.split("/")[0] + "/" : "";
    for (let i = 1; i < parts.length; i++) oc_open[pre + parts.slice(0, i).join(".")] = 1;
    window.oc_sel = j;
  }
  const top = knNeed(await api("tree_children?ref=" + encodeURIComponent(root)));
  const kids = async ref => (await api("tree_children?ref=" + encodeURIComponent(ref))).children || [];
  async function nodeHtml(c) {
    const open = !!oc_open[c.path];
    let sub = "";
    if (open && c.count) {
      const ch = await kids(c.path);
      sub = "<ul>" + (await Promise.all(ch.map(nodeHtml))).join("") + "</ul>";
    }
    const mark = c.count ? ' <span class="cnt">' + (open ? "v " : "> ") + c.count + "</span>" : "";
    return '<li><div class="oc ' + E(c.kind || "") + (oc_sel === c.path ? " sel" : "") + '" tabindex="0" role="treeitem" aria-expanded="' + (open ? "true" : "false") + '" onkeydown="if(event.key===\'Enter\')ocT(' + J(c.path) + ')" onclick="ocT(' + J(c.path) + ')">'
      + '<span class="ix">' + E(String(c.path || "").replace(/^\w+\//, "")) + "</span> " + E(String(c.label || "").slice(0, 38)) + mark
      + '<div class="mut" style="font-size:10px">' + E(c.kind || "") + "</div>"
      + '<div class="hc"><b>' + E(c.label || "") + '</b><div class="mut">' + E(c.kind || "") + " · path <code>" + E(c.path || "") + "</code>" + (c.id ? " · <code>" + E(String(c.id).slice(0, 16)) + "</code>" : "") + "</div>"
      + (c.summary ? '<div style="margin-top:6px">' + E(c.summary) + "</div>" : "")
      + (c.count ? '<div class="mut" style="margin-top:6px">' + c.count + " children</div>" : "")
      + "</div></div>" + sub + "</li>";
  }
  const names = { workspace: "Workspace", agents: "Agents", skills: "Skill library" };
  const crumbs = String(window.oc_sel || "").replace(/^\w+\//, "").split(".").filter(Boolean);
  let bc = '<a href="#" data-kn="ocsel" data-path="">' + E(names[root] || root) + "</a>";
  crumbs.forEach((part, i) => {
    const p = (root === "workspace" ? "" : root + "/") + crumbs.slice(0, i + 1).join(".");
    bc += ' <span aria-hidden="true">/</span> <a href="#" data-kn="ocsel" data-path="' + E(p) + '">' + E(part) + "</a>";
  });
  const roots = [
    ["workspace", "Workspace / projects"],
    ["agents", "Agent / model / tasks"],
    ["skills", "By skill"],
  ].map(([r, label]) => '<button type="button" class="' + (r === root ? "on" : "") + '" data-kn="ocroot" data-root="' + r + '">' + label + "</button>").join("");
  const modes = ["chart", "outline"].map(m => '<button type="button" class="' + (m === mode ? "on" : "") + '" data-kn="ocmode" data-mode="' + m + '">' + m + "</button>").join("");
  let body;
  if (!top.children || !top.children.length) {
    body = K.empty({ icon: "Org chart", title: "Nothing to organize yet", hint: "Projects, jobs, sessions, code and skills appear here as agents work." });
  } else if (mode === "outline") {
    const o = knNeed(await api("tree_outline?depth=4&ref=" + encodeURIComponent(window.oc_sel || root)));
    body = '<pre class="outline">' + E(o.text || "") + "</pre>";
  } else {
    const nodes = await Promise.all(top.children.map(nodeHtml));
    body = '<div class="ocwrap" id="ocw"><div class="zoombar"><button type="button" onclick="ocZoom(1.2)" aria-label="Zoom in">+</button><button type="button" onclick="ocZoom(1/1.2)" aria-label="Zoom out">-</button><button type="button" onclick="ocFit()">Fit</button></div>'
      + '<div class="ocstage" id="ols"><ul class="orgchart" role="tree"><li><div class="oc root">' + E(names[root] || root) + ' <span class="cnt">' + top.children.length + "</span></div><ul>"
      + nodes.join("") + '</ul></li></ul></div><div class="minimap" aria-hidden="true"><div class="mm" id="ocmm"></div><div class="vp" id="ocvp"></div></div></div>';
  }
  return '<div class="kn kn-org">' + knLead("How projects, agents and skills relate. Drag to pan, scroll to zoom.")
    + '<div class="card"><div class="seg" role="group" aria-label="Root">' + roots + "</div> "
    + '<div class="seg" role="group" aria-label="Mode">' + modes + "</div> "
    + '<input id="ocq" aria-label="Search the chart" placeholder="Search and jump" value="' + E(window._ocq || "") + '" oninput="window._ocq=this.value" onkeydown="if(event.key===\'Enter\')ocFind()"> '
    + '<button type="button" onclick="ocFind()">Find</button> <button type="button" data-kn="oc-collapse">Collapse all</button>'
    + '<div class="kn-bc">' + bc + '</div><div id="ochits" class="kn-hits"></div></div><div class="card">' + body + "</div></div>";
};

function knLibDecide(id, approve) {
  window._kdec = window._kdec || {};
  K.optimistic({
    label: approve ? "Approved" : "Rejected",
    apply() { window._kdec[id] = 1; render(); },
    revert() { delete window._kdec[id]; render(); },
    async commit() {
      const r = await postRaw("librarian/decide", { id, approve });
      if (!r || r.error) return r || { error: "could not decide" };
      delete window._kdec[id];
      render();
      return {};
    },
  });
}
function knLibUndo(id) {
  window._kundo = window._kundo || {};
  K.optimistic({
    label: "Undone",
    apply() { window._kundo[id] = 1; render(); },
    revert() { delete window._kundo[id]; render(); },
    async commit() {
      const r = await postRaw("librarian/undo", { id });
      if (!r || r.error) return r || { error: "undo failed" };
      delete window._kundo[id];
      render();
      return {};
    },
  });
}
let knFindT;
function knFind(v) {
  window._fq = v;
  clearTimeout(knFindT);
  knFindT = setTimeout(() => render(), 200);
}
V["Librarian"] = async function () {
  knBegin("Librarian");
  const fqEl = document.getElementById("fq");
  if (fqEl) window._fq = fqEl.value;
  const L = knNeed(await api("librarian"));
  const fq = window._fq || "";
  const f = fq ? knNeed(await api("find?q=" + encodeURIComponent(fq))) : null;
  const proposals = (L.proposals || []).filter(p => !(window._kdec || {})[p.id]);
  const recent = (L.recent || []).filter(e => !(window._kundo || {})[e.id]);
  const run = L.last_run;
  const dedupe = (run && run.dedupe) || {};
  const last = run
    ? "Last run " + E(new Date(run.ts * 1000).toLocaleString()) + ": scanned " + (run.items_scanned || 0)
      + ", tagged " + (run.tagged || 0) + ", assigned " + (run.assigned || 0)
      + ", links " + (run.links_added || 0) + ", summaries " + (run.summaries || 0)
      + ", merges " + ((dedupe.merged || []).length) + " (proposed " + ((dedupe.proposed || []).length) + ")."
    : "No run yet.";
  const queue = proposals.length
    ? '<div class="list">' + proposals.map(p => '<div class="row"><div class="row-content"><div class="row-title">' + E(p.target_label || p.target || "") + ' <span class="badge">' + E(p.kind || "") + "</span></div>"
      + '<div class="row-subtitle">' + E(p.reason || "") + "</div>" + (p.payload ? K.kv(p.payload) : "")
      + '</div><div class="row-actions kn-always"><button type="button" class="pri" data-kn="lib-yes" data-id="' + E(p.id) + '">' + (p.kind === "merge" ? "Merge" : "Approve") + "</button>"
      + '<button type="button" class="bad" data-kn="lib-no" data-id="' + E(p.id) + '">Reject</button></div></div>').join("") + "</div>"
    : K.empty({ icon: "Librarian", title: "Nothing to review", hint: "Run a pass to see proposals." });
  const hits = f
    ? ((f.locations || []).map(l => '<button type="button" class="row kn-rowbtn" data-kn="place" data-path="' + E(l.path || "") + '"><div class="row-content"><div class="row-title">' + E(l.label || l.path || "") + "</div><div class=\"row-subtitle kn-mono\">" + E(l.path || "") + "</div></div><span class=\"badge\">" + E(l.kind || "") + "</span></button>").join("") || '<p class="kn-hint">Nothing indexed matches.</p>')
    : "";
  const activity = recent.length
    ? '<div class="list">' + recent.map(e => {
      const can = e.status === "applied" && e.reversible;
      return '<div class="row"><div class="row-content"><div class="row-title">' + E(e.action || "") + " · " + E(e.target || "") + "</div>"
        + '<div class="kn-hint">' + E(e.ts || "") + " · " + E(e.status || "") + "</div>"
        + '<details class="kn-diff"><summary>Before and after</summary>'
        + '<div class="kn-sub"><span>Before</span></div>' + knMaybeKv(e.before)
        + '<div class="kn-sub"><span>After</span></div>' + knMaybeKv(e.after)
        + "</details></div><div class=\"row-actions kn-always\">" + (can ? '<button type="button" data-kn="lib-undo" data-id="' + E(e.id) + '">Undo</button>' : '<span class="badge">' + E(e.status || "") + "</span>") + "</div></div>";
    }).join("") + "</div>"
    : '<p class="kn-hint">No changes yet.</p>';
  const suggestions = (L.suggested || []).map(s => '<button type="button" class="chip" data-kn="suggest" data-q="' + E(s) + '">' + E(s) + "</button>").join(" ");
  return '<div class="kn">' + knLead("Sorts memory into projects, tags and merges. Every change can be undone.")
    + '<div class="card"><div class="kn-section-h"><h2>Pass</h2><span class="kn-count">' + E(L.mode || "") + " · " + (L.new_since_last_run || 0) + " new since last run</span></div>"
    + '<p class="kn-hint">' + last + "</p>"
    + '<div class="kn-controls"><label class="kn-checkline" data-kn="lib-auto"><span class="switch"><input type="checkbox"' + (L.auto_approve ? " checked" : "") + ' aria-label="Auto-approve"><span class="switch-slider"></span></span> Auto-approve</label>'
    + '<button type="button" class="pri" onclick="post(\'librarian/run\',{})">Run now</button>'
    + '<button type="button" onclick="post(\'librarian/run\',{full:true})">Full pass</button></div></div>'
    + '<section class="kn-section"><div class="kn-section-h"><h2>Review</h2><span class="kn-count">' + proposals.length + "</span></div>" + queue + "</section>"
    + '<section class="kn-section"><div class="kn-section-h"><h2>Where to find</h2></div>'
    + '<input id="fq" aria-label="Find" placeholder="For example, billing invoices" value="' + E(fq) + '" oninput="knFind(this.value)">'
    + '<div class="kn-chips">' + suggestions + "</div>" + (hits ? '<div class="list">' + hits + "</div>" : "") + "</section>"
    + '<section class="kn-section"><div class="kn-section-h"><h2>Activity</h2><span class="kn-count">' + recent.length + "</span></div>" + activity + "</section></div>";
};

V["Import"] = async function () {
  knBegin("Import");
  const ipth = document.getElementById("ipth");
  if (ipth) window.ip = ipth.value;
  const irl = document.getElementById("irl");
  if (irl) window._irl = irl.checked;
  const p = window.ip || "";
  const r = knNeed(await api("import_scan?paths=" + encodeURIComponent(p)));
  const imported = r.imported || [];
  const dupes = r.duplicates || [];
  const kinds = {};
  imported.forEach(x => { const k = x.kind || "other"; kinds[k] = (kinds[k] || 0) + 1; });
  const kindChips = Object.keys(kinds).sort().map(k => '<span class="badge">' + E(k) + " " + kinds[k] + "</span>").join(" ");
  const rows = imported.length
    ? '<div class="list">' + imported.map(x => '<div class="row"><div class="row-content"><div class="row-title">' + E(x.name || "") + ' <span class="badge">' + E(x.kind || "") + '</span></div><div class="row-subtitle kn-mono">' + E(x.path || "") + "</div></div></div>").join("") + "</div>"
    : K.empty({ icon: "Import", title: "Nothing new to import", hint: "Everything found is already in the pool." });
  const dupeHtml = dupes.length
    ? "<details><summary>" + dupes.length + " already in the pool</summary>" + dupes.map(x => '<div class="kn-hint kn-mono">' + E(x.path || "") + " = " + E(x.same_as || "") + "</div>").join("") + "</details>"
    : "";
  const go = imported.length
    ? '<label class="kn-checkline"><input type="checkbox" id="irl"' + (window._irl ? " checked" : "") + "> Replace local copies with the Commons bootstrap pointer. Originals are backed up under ~/.commons/skill-backups.</label>"
      + '<button type="button" class="pri" data-kn="import-go">Import ' + imported.length + " into the pool</button>"
    : "";
  return '<div class="kn kn-import">' + knLead("Bring skills already installed on this machine into the pool.")
    + '<div class="card"><p class="kn-hint">Scans Claude skills, project skills, plugins, Cursor rules, AGENTS.md, Codex and any extra paths. Duplicates of pool skills are skipped.</p>'
    + '<div class="kn-controls"><label>Extra paths<input id="ipth" aria-label="Extra paths" placeholder="Extra paths, comma separated" value="' + E(p) + '"></label>'
    + '<button type="button" data-kn="rescan">Rescan</button>' + go + "</div></div>"
    + '<section class="kn-section"><div class="kn-section-h"><h2>Found</h2><span class="kn-count">' + imported.length + " new · " + dupes.length + " already in the pool</span></div>"
    + (kindChips ? '<div class="kn-chips">' + kindChips + "</div>" : "")
    + rows + dupeHtml + "</section></div>";
};

function knRetroSync() {
  const llm = document.getElementById("rl_llm");
  const budget = document.getElementById("rl_b");
  const project = document.getElementById("rl_p");
  const auto = document.getElementById("rl_auto");
  if (!llm && !budget) return;
  window._rl = {
    llm: llm ? llm.checked : true,
    budget: budget ? budget.value : "1",
    project: project ? project.value : "",
    auto: auto ? auto.checked : false,
  };
}
function knRetroDecide(ids, ok) {
  if (!ids.length) return toast("Select rows first", "err");
  window._rhide = window._rhide || {};
  K.optimistic({
    label: ok ? "Approved " + ids.length : "Rejected " + ids.length,
    apply() { ids.forEach(id => { window._rhide[id] = 1; }); render(); },
    revert() { ids.forEach(id => delete window._rhide[id]); render(); },
    async commit() {
      const r = await postRaw("retro/decide", { ids, approve: !!ok });
      if (!r || r.error) return r || { error: "could not decide" };
      ids.forEach(id => delete window._rhide[id]);
      render();
      return {};
    },
  });
}
function retroDecide(ok) {
  const ids = [...document.querySelectorAll(".rqc:checked")].map(c => c.value).filter(id => !(window._rhide || {})[id]);
  knRetroDecide(ids, ok);
}
function retroAll(ok, anchor) {
  const ids = [...document.querySelectorAll(".rqc")].map(c => c.value).filter(id => !(window._rhide || {})[id]);
  if (!ids.length) return toast("Nothing in this queue", "err");
  const el = anchor && anchor.getBoundingClientRect ? anchor : document.body;
  K.menu(el, [{
    label: (ok ? "Approve " : "Reject ") + ids.length + " shown",
    danger: !ok,
    run: () => knRetroDecide(ids, ok),
  }]);
}
async function retroRun(dry) {
  knRetroSync();
  const form = window._rl || { llm: true, budget: "1", project: "", auto: false };
  window._rl = form;
  window._rlOut = dry ? "Estimating." : "Learning.";
  render();
  const r = await postRaw("retro/run", {
    dry_run: !!dry,
    use_llm: !!form.llm,
    budget: parseFloat(form.budget) || 1,
    project: form.project || "",
    auto_approve: !!form.auto,
  });
  window._rlOut = JSON.stringify(r, null, 1);
  render();
}
V["Retro learning"] = async function () {
  knBegin("Retro learning");
  knRetroSync();
  const form = window._rl || { llm: true, budget: "1", project: "", auto: false };
  const f = window.rq || { status: "pending", kind: "", source: "" };
  const d = knNeed(await api("retro?status=" + encodeURIComponent(f.status || "") + "&kind=" + encodeURIComponent(f.kind || "") + "&source=" + encodeURIComponent(f.source || "")));
  const detected = d.detected || {};
  const srcBadges = Object.entries(detected).map(([k, v]) => '<span class="badge' + (v ? " ok" : "") + '">' + E(k) + (v ? "" : " missing") + "</span>").join(" ");
  const kinds = ["", "preference", "decision", "fact", "leftover", "performance", "skill"];
  const sources = Object.keys(detected);
  if (f.source && !sources.includes(f.source)) sources.push(f.source);
  const run = (d.runs || [])[0];
  const queue = (d.queue || []).filter(r => !(window._rhide || {})[r.id]);
  const pending = f.status === "pending";
  const rows = queue.length
    ? '<div class="list">' + '<div class="row"><label class="kn-check"><input type="checkbox" aria-label="Select all shown" onchange="document.querySelectorAll(\'.rqc\').forEach(c=>c.checked=this.checked)"></label><div class="row-content"><div class="row-title">Select all shown</div></div></div>'
      + queue.map(r => {
        const src = r.source || {};
        const when = src.ts ? new Date(src.ts * 1000).toLocaleDateString() : "";
        const why = r.payload && r.payload.rationale ? '<div class="kn-hint">Why: ' + E(r.payload.rationale) + "</div>" : "";
        return '<div class="row"><label class="kn-check"><input type="checkbox" class="rqc" value="' + E(r.id) + '" aria-label="Select"></label>'
          + '<div class="row-content"><div class="row-title">' + E(r.kind || "") + " · " + (r.confidence == null ? "" : Number(r.confidence).toFixed(2)) + "</div>"
          + '<div class="kn-learn">' + E(String(r.text || "").slice(0, 400)) + "</div>" + why
          + '<div class="kn-sub"><span>' + E(src.kind || r.source_kind || "") + "</span><span>" + E(String(src.title || src.path || "").slice(0, 60)) + "</span><span>" + E(when) + "</span><span>" + E(r.project || "") + "</span><span>" + E(r.method || "") + "</span></div></div>"
          + (pending ? '<div class="row-actions kn-always"><button type="button" class="pri" data-kn="retro-one" data-ok="1" data-id="' + E(r.id) + '">Approve</button><button type="button" class="bad" data-kn="retro-one" data-ok="0" data-id="' + E(r.id) + '">Reject</button></div>' : "")
          + "</div>";
      }).join("") + "</div>"
    : K.empty({ icon: "Retro learning", title: "Nothing in this queue", hint: "Run a dry run to see what Commons would learn." });
  const opt = (v, cur, label) => '<option value="' + E(v) + '"' + (v === cur ? " selected" : "") + ">" + E(label || v || "All") + "</option>";
  return '<div class="kn">' + knLead("Learn from past chats, then approve what gets remembered.")
    + '<div class="grid">'
    + '<div class="card stat"><div class="mut">Pending review</div><div class="big">' + knNum(d.pending) + "</div></div>"
    + '<div class="card stat"><div class="mut">Last run</div><div class="big" style="font-size:16px">' + E(run ? new Date(run.started_at * 1000).toLocaleString() : "Never") + "</div>"
    + '<div class="mut">' + (run ? (run.stats.conversations || 0) + " conversations · queued " + (run.stats.queued || 0) : "") + "</div></div>"
    + '<div class="card stat"><div class="mut">Checkpoints</div><div class="big">' + knNum(d.checkpoints) + '</div><div class="mut">Re-runs only read new history</div></div></div>'
    + '<div class="card"><h2>Learn from past history</h2><p class="kn-hint">Detected on this machine</p><div class="kn-chips">' + srcBadges + "</div>"
    + '<div class="kn-controls"><label class="kn-checkline"><input type="checkbox" id="rl_llm"' + (form.llm ? " checked" : "") + "> Use the retro model when one is configured</label>"
    + '<label>Budget $<input id="rl_b" aria-label="Budget" size="4" value="' + E(form.budget || "1") + '"></label>'
    + '<label>Project <input id="rl_p" aria-label="Project" placeholder="(auto)" value="' + E(form.project || "") + '"></label>'
    + '<label class="kn-checkline"><input type="checkbox" id="rl_auto"' + (form.auto ? " checked" : "") + "> Auto-approve</label></div>"
    + '<button type="button" onclick="retroRun(true)">Dry run</button> <button type="button" class="pri" onclick="retroRun(false)">Run</button>'
    + '<pre id="rl_out" class="kn-pre"' + (window._rlOut ? "" : " hidden") + ">" + E(window._rlOut || "") + "</pre></div>"
    + '<section class="kn-section"><div class="kn-section-h"><h2>Review queue</h2><span class="kn-count">' + queue.length + " shown</span></div>"
    + '<div class="kn-controls"><label>Status<select aria-label="Status" onchange="rqSet(\'status\', this.value)">'
    + ["pending", "applied", "rejected"].map(x => opt(x, f.status, x)).join("") + "</select></label>"
    + '<label>Kind<select aria-label="Kind" onchange="rqSet(\'kind\', this.value)">' + kinds.map(x => opt(x, f.kind, x || "All")).join("") + "</select></label>"
    + '<label>Source<select aria-label="Source" onchange="rqSet(\'source\', this.value)">' + opt("", f.source, "All") + sources.map(x => opt(x, f.source, x)).join("") + "</select></label>"
    + (pending ? '<button type="button" class="pri" onclick="retroDecide(true)">Approve selected</button><button type="button" class="bad" onclick="retroDecide(false)">Reject selected</button><button type="button" onclick="retroAll(true, this)">Approve all shown</button><button type="button" onclick="retroAll(false, this)">Reject all shown</button>' : "")
    + "</div>" + rows + "</section></div>";
};

function memEdit(id) {
  window.mopen = id;
  window.medit = id;
  window._draft = null;
  const i = (window.mrows || []).findIndex(x => x.id === id);
  if (i >= 0) window.mcur = i;
  render();
  setTimeout(() => {
    const t = document.getElementById("med");
    if (t) { t.focus(); try { t.setSelectionRange(t.value.length, t.value.length); } catch (err) {} }
  }, 60);
}

document.addEventListener("click", e => {
  const el = e.target.closest && e.target.closest("[data-kn]");
  if (!el) return;
  const legacy = e.target.closest("[data-m],[data-bulk]");
  if (legacy && el.contains(legacy)) return;
  if (el.tagName === "A") e.preventDefault();
  const act = el.dataset.kn;
  const id = el.dataset.id || "";
  if (act === "agent") { window.ma = window.ma === (el.dataset.agent || "") ? "" : (el.dataset.agent || ""); render(); return; }
  if (act === "tierf") { window.mtr = window.mtr === (el.dataset.tier || "") ? "" : (el.dataset.tier || ""); render(); return; }
  if (act === "when") {
    const ms = el.dataset.ms || "0";
    window.mwhen = String(+window.mwhen || 0) === String(+ms) ? 0 : +ms;
    render();
    return;
  }
  if (act === "clear-filters") { knClearFilters(); return; }
  if (act === "del") { knDelete([id]); return; }
  if (act === "tier-menu") { knTierMenu(el, [id]); return; }
  if (act === "settier") { knTier([id || window.mopen], el.dataset.t); return; }
  if (act === "move") { knMove(el, [id]); return; }
  if (act === "bulk-move") { knMove(el, memIds()); return; }
  if (act === "bulk-tag") { knTagPop(el, memIds()); return; }
  if (act === "bulk-tier") { knTierMenu(el, memIds()); return; }
  if (act === "bulk-del") {
    const ids = memIds();
    if (!ids.length) return;
    K.menu(el, [{ label: "Delete " + ids.length, danger: true, run: () => knDelete(ids) }]);
    return;
  }
  if (act === "bulk-clear") { window.msel = []; render(); return; }
  if (act === "copy") { K.copy(id); return; }
  if (act === "show") {
    window.mopen = id;
    window.mfocus = id;
    window.medit = null;
    const i = (window.mrows || []).findIndex(x => x.id === id);
    if (i >= 0) window.mcur = i;
    if (tab !== "Memory") go("Memory");
    else render();
    return;
  }
  if (act === "droptag") {
    const tags = document.getElementById("metags");
    if (!tags) return;
    const drop = el.dataset.tag || "";
    tags.value = knTagList(tags.value).filter(t => t !== drop).join(", ");
    tags.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }
  if (act === "cite") { cite(id); return; }
  if (act === "bucket") { window.kb = id; window._ks = ""; render(); return; }
  if (act === "section") { window._ks = id; render(); return; }
  if (act === "kb-rebuild") { post("kb/refresh", { bucket: window.kb || id }); return; }
  if (act === "cite-kb") {
    window.kb = el.dataset.bucket || "";
    window._ks = el.dataset.section || "";
    go("Knowledge");
    return;
  }
  if (act === "pack") { window.cxs = window.cxs === id ? "" : id; window._packSig = ""; render(); return; }
  if (act === "depth") { window.gd = el.dataset.d || "2"; render(); return; }
  if (act === "pref-save") {
    const row = el.closest(".kn-pref");
    const input = row && row.querySelector("input");
    post("prefs/confirm", { id, statement: input ? input.value : "" });
    return;
  }
  if (act === "pref-forget") { post("prefs/forget", { id }); return; }
  if (act === "pref-all") {
    K.menu(el, [{ label: "Forget every preference", danger: true, run: () => post("prefs/forget", { everything: true }) }]);
    return;
  }
  if (act === "ocroot") { window.oc_root = el.dataset.root; window.oc_open = {}; window.oc_sel = ""; render(); return; }
  if (act === "ocmode") { window.oc_mode = el.dataset.mode; render(); return; }
  if (act === "ocsel") { window.oc_sel = el.dataset.path || ""; render(); return; }
  if (act === "oc-collapse") { window.oc_open = {}; render(); return; }
  if (act === "lib-yes") { knLibDecide(id, true); return; }
  if (act === "lib-no") { knLibDecide(id, false); return; }
  if (act === "lib-undo") { knLibUndo(id); return; }
  if (act === "lib-auto") {
    const input = el.querySelector("input");
    setTimeout(() => post("librarian/config", { auto_approve: !!(input && input.checked) }), 0);
    return;
  }
  if (act === "suggest") { window._fq = el.dataset.q || ""; render(); return; }
  if (act === "place") {
    const path = el.dataset.path || "";
    window.oc_root = path.includes("/") ? path.split("/")[0] : "workspace";
    window.oc_jump = path;
    go("Org chart");
    return;
  }
  if (act === "rescan") { window.ip = (document.getElementById("ipth") || {}).value || ""; render(); return; }
  if (act === "import-go") {
    const box = document.getElementById("irl");
    post("import/run", { paths: (document.getElementById("ipth") || {}).value || "", replace_local: !!(box && box.checked) });
    return;
  }
  if (act === "retro-one") { knRetroDecide([id], el.dataset.ok === "1"); return; }
});

K.drawer.on("mem:", function (raw) {
  const id = String(raw || "").slice(4);
  if (!id) return;
  window.mopen = id;
  window.mfocus = id;
  window.medit = null;
  if (tab !== "Memory") go("Memory");
  else render();
});
K.drawer.on("cite:", function (raw) { cite(String(raw || "").slice(5)); });
K.drawer.on("pack:", function (raw) {
  window.cxs = String(raw || "").slice(5);
  window._packSig = "";
  if (tab !== "Context") go("Context");
  else render();
});

if (["Memory", "Knowledge", "Context", "Graph", "Preferences", "Org chart", "Librarian", "Import", "Retro learning"].includes(tab)) render();
