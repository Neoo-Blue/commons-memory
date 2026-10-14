/* ==========================================================================
   COMMONS PROJECTS WORKSPACE (pages/projects.js)
   Projects, Project Detail, Tasks, and the Change Drawer.
   Vanilla JS, no build step, no new libraries, no em dashes anywhere.
   ========================================================================== */

(function () {
  "use strict";

  // Register icons (inline SVG paths, 1.6 stroke)
  ICON.Projects = '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>';
  ICON.Tasks = '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>';

  // State globals
  window.PRJ = window.PRJ || {};
  window.PRJ_STATE = window.PRJ_STATE || {
    filter: "",
    taskFilter: "",
    timelineEvents: [],
    timelineNext: null,
    timelineType: "highlights",
    timelineAgent: "",
    activeTab: "timeline",
    diffExpanded: {},
  };

  const S = window.PRJ_STATE;

  /* ---------- Helpers ---------- */
  function fmtMoney(n) {
    if (window.K && window.K.fmt && window.K.fmt.money) return window.K.fmt.money(n);
    const v = Number(n) || 0;
    return "$" + v.toFixed(4);
  }

  function fmtNum(n) {
    if (window.K && window.K.fmt && window.K.fmt.num) return window.K.fmt.num(n);
    return String(n || 0);
  }

  function fmtAgo(t) {
    if (!t) return "";
    if (window.K && window.K.fmt && window.K.fmt.ago) return window.K.fmt.ago(t);
    if (typeof ago === "function") {
      const n = typeof t === "number" ? t : Date.parse(t);
      if (n > 0) {
        const s = Math.max(0, Math.round((Date.now() - (n > 1e12 ? n : n * 1000)) / 1000));
        return ago(s) + " ago";
      }
    }
    return String(t);
  }

  function avatarHtml(name) {
    const n = String(name || "agent").trim();
    const initials = n.slice(0, 2).toUpperCase();
    return `<span class="avatar" title="${E(n)}"><span>${E(initials)}</span></span>`;
  }

  function statusDotHtml(st) {
    const s = String(st || "").toLowerCase();
    let cls = "idle";
    if (s === "running" || s === "claimed" || s === "live") cls = "live";
    else if (s === "waiting_owner" || s === "awaiting_gate" || s === "needs_user" || s === "busy" || s === "queued" || s === "paused") cls = "busy";
    else if (s === "failed" || s === "reject" || s === "error" || s === "bad") cls = "bad";
    else if (s === "done" || s === "approved" || s === "verified" || s === "pass") cls = "idle";
    return `<span class="status-dot ${cls}"></span>`;
  }

  function eventTypeIcon(type) {
    switch (type) {
      case "decision":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>';
      case "handoff":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
      case "session":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M8 14h5"/></svg>';
      case "job":
      case "job_done":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M13 2L4.5 13.5H12L11 22l8.5-11.5H12z"/></svg>';
      case "step":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M5 13l4 4L19 7"/></svg>';
      case "work":
      case "work_done":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3.5" y="6" width="17" height="13" rx="2"/><path d="M8 6V4h8v2"/></svg>';
      case "ask":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 1.5-2 2-2 3M12 16.5v.5"/></svg>';
      case "change":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 3v18M18 3v18M6 8h12M6 16h12"/></svg>';
      case "memory":
      case "fact":
      case "note":
      case "observation":
      case "document":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>';
      default:
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/></svg>';
    }
  }

  function renderDiffHtml(diffText) {
    if (!diffText) return '<div class="mut sm" style="padding:8px 12px">No diff content recorded.</div>';
    const lines = String(diffText).split("\n");
    return lines.map(line => {
      let cls = "proj-diff-line";
      if (line.startsWith("+") && !line.startsWith("+++")) cls += " proj-diff-add";
      else if (line.startsWith("-") && !line.startsWith("---")) cls += " proj-diff-del";
      else if (line.startsWith("@@")) cls += " proj-diff-hunk";
      return `<span class="${cls}">${E(line)}</span>`;
    }).join("");
  }

  /* ---------- Command Palette & Keys Setup ---------- */
  if (window.K && window.K.action) {
    K.action({ id: "go-projects", label: "Projects", keywords: "projects product overview", run: () => go("Projects") });
    K.action({ id: "go-tasks", label: "Tasks", keywords: "tasks loose jobs one-off", run: () => go("Tasks") });
  }

  if (window.K && window.K.keys && window.K.keys.bind) {
    window.K.keys.bind("g p", () => go("Projects"), "Go to Projects");
  }

  /* ---------- Change Drawer Registration ---------- */
  if (window.K && window.K.drawer && window.K.drawer.on) {
    window.K.drawer.on("change:", (rawId) => {
      const path = String(rawId || "").slice(7);
      window.PRJ.openChangeDrawer(path);
    });
  }

  window.PRJ.openChangeDrawer = function (path) {
    if (!path) return;
    if (!window.K || !window.K.drawer) return;

    window.K.drawer.open({
      id: "change:" + path,
      title: "Recorded change",
      subtitle: path,
      actions: [
        {
          label: "Copy diff",
          kind: "primary",
          run: async (ctx) => {
            if (ctx.diffContent) {
              await K.copy(ctx.diffContent);
            }
          },
        },
      ],
      render: async (ctx) => {
        let chg;
        try {
          chg = await api("project_change?path=" + encodeURIComponent(path));
        } catch (e) {
          return `<div class="empty"><div class="mut">Could not load change details: ${E(e && e.message ? e.message : e)}</div></div>`;
        }
        if (!chg) return '<div class="empty"><div class="mut">Change not found.</div></div>';

        ctx.diffContent = chg.diff || "";
        const commitStr = chg.commit ? chg.commit.slice(0, 10) : "";
        const branchStr = chg.branch || "";
        const filesList = Array.isArray(chg.files) ? chg.files : [];

        return `<div class="change-drawer-wrap">
          <div class="change-drawer-meta-grid">
            <div class="change-meta-item">
              <span class="change-meta-lbl">Author</span>
              <span class="change-meta-val" style="display:flex;align-items:center;gap:6px;">
                ${avatarHtml(chg.by || "agent")} <span>${E(chg.by || "agent")}</span>
              </span>
            </div>
            <div class="change-meta-item">
              <span class="change-meta-lbl">Time</span>
              <span class="change-meta-val">${E(fmtAgo(chg.ts || chg.at))}</span>
            </div>
            <div class="change-meta-item">
              <span class="change-meta-lbl">Commit & Branch</span>
              <span class="change-meta-val" style="font-family:var(--font-mono);">
                ${commitStr ? `<b>${E(commitStr)}</b>` : ""} ${branchStr ? `(${E(branchStr)})` : ""}
              </span>
            </div>
            <div class="change-meta-item">
              <span class="change-meta-lbl">Repository</span>
              <span class="change-meta-val" style="font-family:var(--font-mono);">${E(chg.repo || "local")}</span>
            </div>
          </div>

          ${filesList.length ? `
            <div>
              <div style="font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;color:var(--fg-subtle);margin-bottom:6px;">
                Changed Files (${filesList.length})
              </div>
              <div class="proj-files-row">
                ${filesList.map(f => `<span class="proj-file-chip">${E(f)}</span>`).join("")}
              </div>
            </div>
          ` : ""}

          <div>
            <div style="font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;color:var(--fg-subtle);margin-bottom:6px;">
              Diff
            </div>
            <div class="proj-diff-container" style="max-height:500px;">
              ${renderDiffHtml(chg.diff)}
            </div>
          </div>
        </div>`;
      },
    });
  };

  /* ---------- Action Handlers ---------- */
  window.PRJ.filterProjects = function (val) {
    S.filter = String(val || "").toLowerCase().trim();
    const cards = document.querySelectorAll(".proj-card");
    let matchCount = 0;
    cards.forEach(c => {
      const t = (c.dataset.search || "").toLowerCase();
      const match = !S.filter || t.includes(S.filter);
      c.style.display = match ? "" : "none";
      if (match) matchCount++;
    });
    const nomatch = document.getElementById("proj-nomatch");
    if (nomatch) nomatch.style.display = (cards.length > 0 && matchCount === 0) ? "block" : "none";
  };

  window.PRJ.groupSuggestion = function (into, key) {
    const card = document.getElementById("sugg-" + key);
    if (!card) return;
    const checkedBoxes = Array.from(card.querySelectorAll('input[type="checkbox"]:checked'));
    const ids = checkedBoxes.map(cb => cb.value);
    const titleIn = card.querySelector(".proj-sugg-input");
    const title = titleIn ? titleIn.value.trim() : "";

    if (!ids.length) {
      if (window.K && window.K.toast) window.K.toast("Select at least one member to group", { kind: "err" });
      return;
    }

    if (window.K && window.K.optimistic) {
      window.K.optimistic({
        apply: () => {
          card.style.display = "none";
        },
        revert: () => {
          card.style.display = "";
        },
        commit: async () => {
          const r = await postRaw("project/group", { into: ids.includes(into) ? into : ids[0], ids, title });
          if (r && !r.error) {
            render();
            return r;
          }
          throw new Error((r && r.error) || "Failed to group projects");
        },
        label: "Grouped into " + (title || "project"),
      });
    } else {
      post("project/group", { into, ids, title });
    }
  };

  window.PRJ.dismissSuggestion = async function (key) {
    const card = document.getElementById("sugg-" + key);
    if (card) card.style.display = "none";
    const r = await postRaw("project/dismiss", { key });
    if (r && r.error) {
      if (card) card.style.display = "";
      if (window.K && window.K.toast) window.K.toast(r.error, { kind: "err" });
    }
  };

  function plural(n, w) { return `${n} ${w}${n === 1 ? "" : "s"}`; }
  window.PRJ.renameProject = function (pid, currentTitle, el) {
    el = el || document.querySelector(`[data-proj-title="${CSS.escape(pid)}"]`);
    if (!el) { location.hash = "#Projects?p=" + encodeURIComponent(pid); return; }
    const inp = document.createElement("input");
    inp.className = "proj-rename"; inp.value = currentTitle || ""; inp.maxLength = 80;
    inp.setAttribute("aria-label", "Project name");
    const done = async save => {
      if (inp.dataset.done) return;
      inp.dataset.done = "1";
      const next = inp.value.trim();
      if (save && next && next !== currentTitle) {
        const r = await postRaw("project/update", { id: pid, title: next });
        if (r && r.error && window.K) window.K.toast(r.error, { kind: "err" });
      }
      render();
    };
    inp.addEventListener("keydown", e => { if (e.key === "Enter") done(true); else if (e.key === "Escape") done(false); });
    inp.addEventListener("blur", () => done(true));
    inp.addEventListener("click", e => e.stopPropagation());
    el.replaceWith(inp); inp.focus(); inp.select();
  };

  window.PRJ.openKindMenu = function (btn, pid, currentKind) {
    if (!window.K || !window.K.menu) return;
    const kinds = [
      { label: "Project", kind: "project" },
      { label: "Task", kind: "task" },
      { label: "Folder", kind: "folder" },
      { label: "Automatic", kind: "" },
    ];
    const items = kinds.map(k => ({
      label: k.label + (k.kind === (currentKind || "") ? " ✓" : ""),
      run: async () => {
        await post("project/update", { id: pid, kind: k.kind });
      },
    }));
    window.K.menu(btn, items);
  };

  window.PRJ.ungroupMember = function (mid, mname, pid) {
    if (!mid) return;
    if (window.K && window.K.optimistic) {
      window.K.optimistic({
        apply: () => {
          const chip = document.getElementById("mchip-" + mid);
          if (chip) chip.style.display = "none";
        },
        revert: () => {
          const chip = document.getElementById("mchip-" + mid);
          if (chip) chip.style.display = "";
        },
        commit: async () => {
          const r = await postRaw("project/ungroup", { id: mid });
          if (r && !r.error) {
            render();
            return r;
          }
          throw new Error((r && r.error) || "Failed to ungroup member");
        },
        label: "Ungrouped " + mname,
      });
    } else {
      post("project/ungroup", { id: mid });
    }
  };

  window.PRJ.openProjectRowMenu = function (btn, p, allProjects) {
    if (!window.K || !window.K.menu) return;
    const others = (allProjects || []).filter(x => x.id !== p.id);
    const items = [
      {
        label: "Rename",
        run: () => window.PRJ.renameProject(p.id, p.title),
      },
      {
        label: "Make it a task",
        run: () => post("project/update", { id: p.id, kind: "task" }),
      },
      {
        label: "Not a project",
        run: () => post("project/update", { id: p.id, kind: "folder" }),
      },
    ];

    if (others.length) {
      items.push({
        label: "Add to another project...",
        run: () => {
          const menuItems = others.map(tgt => ({
            label: tgt.title || tgt.name,
            run: () => post("project/group", { into: tgt.id, ids: [p.id] }),
          }));
          window.K.menu(btn, menuItems);
        },
      });
    }

    window.K.menu(btn, items);
  };

  window.PRJ.switchTimelineType = function (pid, typ) {
    S.timelineType = typ;
    window.PRJ.reloadTimeline(pid);
  };

  window.PRJ.switchTimelineAgent = function (pid, ag) {
    S.timelineAgent = ag;
    window.PRJ.reloadTimeline(pid);
  };

  window.PRJ.reloadTimeline = async function (pid) {
    const feed = document.getElementById("proj-timeline-feed");
    if (!feed) return;
    feed.innerHTML = window.K && window.K.skeleton ? window.K.skeleton(5) : '<div class="mut sm">Loading timeline...</div>';
    let url = "project_timeline?id=" + encodeURIComponent(pid) + "&types=" + encodeURIComponent(S.timelineType);
    if (S.timelineAgent) url += "&agent=" + encodeURIComponent(S.timelineAgent);
    try {
      const res = await api(url);
      S.timelineEvents = res.events || [];
      S.timelineNext = res.next;
      window.PRJ.renderTimelineFeed(pid);
    } catch (e) {
      feed.innerHTML = `<div class="empty"><div class="mut">Failed to load timeline: ${E(e && e.message ? e.message : e)}</div></div>`;
    }
  };

  window.PRJ.loadOlderTimeline = async function (pid) {
    if (!S.timelineNext) return;
    const btn = document.getElementById("proj-load-older-btn");
    if (btn) btn.disabled = true;
    let url = "project_timeline?id=" + encodeURIComponent(pid) + "&before=" + encodeURIComponent(S.timelineNext) + "&types=" + encodeURIComponent(S.timelineType);
    if (S.timelineAgent) url += "&agent=" + encodeURIComponent(S.timelineAgent);
    try {
      const res = await api(url);
      const newEvs = res.events || [];
      S.timelineEvents = S.timelineEvents.concat(newEvs);
      S.timelineNext = res.next;
      window.PRJ.renderTimelineFeed(pid);
    } catch (e) {
      if (window.K && window.K.toast) window.K.toast("Could not load older events", { kind: "err" });
      if (btn) btn.disabled = false;
    }
  };

  window.PRJ.handleEventClick = function (refJson) {
    if (!refJson) return;
    let ref;
    try {
      ref = typeof refJson === "object" ? refJson : JSON.parse(refJson);
    } catch (e) {
      return;
    }
    if (!ref || !window.K || !window.K.drawer) return;

    if (ref.kind === "item" && ref.id) {
      window.K.drawer.openId("mem:" + ref.id);
    } else if (ref.kind === "job" && ref.id) {
      window.K.drawer.openId("job:" + ref.id);
    } else if (ref.kind === "board" && ref.id) {
      window.K.drawer.openId("board:" + ref.id);
    } else if (ref.kind === "change" && ref.path) {
      window.K.drawer.openId("change:" + ref.path);
    } else if (ref.kind === "session" && ref.id) {
      go("Sessions");
    } else if (ref.kind === "claim" && ref.id) {
      go("Workflow");
    }
  };

  window.PRJ.renderTimelineFeed = function (pid) {
    const feed = document.getElementById("proj-timeline-feed");
    if (!feed) return;

    if (!S.timelineEvents || !S.timelineEvents.length) {
      feed.innerHTML = window.K && window.K.empty
        ? window.K.empty({ icon: "Projects", title: "No timeline events", hint: "No events recorded for the selected filters." })
        : '<div class="empty"><div class="mut">No timeline events recorded.</div></div>';
      return;
    }

    // Group events by Day
    const days = {};
    S.timelineEvents.forEach(ev => {
      let dayKey = "Earlier";
      if (ev.at) {
        const d = new Date(ev.at);
        if (!isNaN(d.getTime())) {
          dayKey = d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
        }
      }
      if (!days[dayKey]) days[dayKey] = [];
      days[dayKey].push(ev);
    });

    let h = "";
    Object.keys(days).forEach(day => {
      h += `<div class="proj-day-group">
        <div class="proj-day-label">${E(day)}</div>`;
      days[day].forEach(ev => {
        const hasRef = !!ev.ref;
        h += `<div class="proj-event-row" ${hasRef ? `onclick="PRJ.handleEventClick(${J(ev.ref)})"` : ""}>
          <div class="proj-event-ico-wrap">${eventTypeIcon(ev.type)}</div>
          <div class="proj-event-main">
            <div class="proj-event-title-row">
              <span class="proj-event-title">${E(ev.title || "Untitled")}</span>
              <span class="proj-event-time">${E(fmtAgo(ev.at))}</span>
            </div>
            ${ev.text ? `<div class="proj-event-body">${E(ev.text)}</div>` : ""}
            <div class="proj-event-meta">
              ${ev.agent ? `<span class="proj-event-agent-tag">${avatarHtml(ev.agent)} ${E(ev.agent)}</span>` : ""}
              <span class="badge sm" style="font-size:10.5px;">${E(ev.type)}</span>
            </div>
          </div>
        </div>`;
      });
      h += `</div>`;
    });

    if (S.timelineNext) {
      h += `<div style="text-align:center;padding:12px 0;">
        <button type="button" id="proj-load-older-btn" class="btn" onclick="PRJ.loadOlderTimeline(${J(pid)})">Load older events</button>
      </div>`;
    }

    feed.innerHTML = h;
  };

  /* ==========================================================================
     PAGE: Projects
     ========================================================================== */
  V["Projects"] = async function () {
    const rawHash = location.hash.slice(1);
    const qIndex = rawHash.indexOf("?");
    const qs = new URLSearchParams(qIndex >= 0 ? rawHash.slice(qIndex + 1) : "");
    const pid = qs.get("p");
    const activeTab = qs.get("tab") || "timeline";

    if (pid) {
      return await renderProjectDetail(pid, activeTab);
    }
    return await renderProjectsList();
  };

  /* ---------- Projects List View ---------- */
  async function renderProjectsList() {
    let data;
    try {
      data = await api("project_list");
    } catch (e) {
      return `<div class="proj-page">
        <div class="empty">
          <b>Could not load projects</b>
          <div class="hint">${E(e && e.message ? e.message : e)}</div>
          <div class="empty-actions"><button class="pri" onclick="render()">Try again</button></div>
        </div>
      </div>`;
    }

    const projects = (data && data.projects) || [];
    const folders = (data && data.folders) || [];
    const suggestions = (data && data.suggestions) || [];

    // Register project titles in the command palette
    if (window.K && window.K.action) {
      projects.forEach(p => {
        K.action({
          id: "open-proj-" + p.id,
          label: "Open project " + (p.title || p.name),
          keywords: "project " + p.name + " " + (p.title || ""),
          run: () => {
            location.hash = "#Projects?p=" + encodeURIComponent(p.id);
          },
        });
      });
    }

    let out = `<div class="proj-page">
      <div class="page-head">
        <div class="page-head-main">
          <h2 class="page-head-title">Projects</h2>
          <p class="page-head-subtitle">Products and workstreams shared across AI agents</p>
        </div>
        <div class="page-head-actions">
          <label class="ag-search" style="min-width:240px">
            <svg class="ag-i" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
            <input type="search" id="proj-filter-in" value="${E(S.filter || "")}" placeholder="Filter projects..." autocomplete="off" oninput="PRJ.filterProjects(this.value)">
          </label>
        </div>
      </div>`;

    // Suggestions Cards
    if (suggestions.length) {
      suggestions.forEach(sug => {
        const members = sug.members || [];
        out += `<div class="proj-sugg-card" id="sugg-${E(sug.key)}">
          <div class="proj-sugg-head">
            <div class="proj-sugg-title-row">
              <span class="proj-sugg-badge">Suggested Group</span>
              <span class="proj-sugg-title">Looks like one project: ${E(members.map(m => m.name).join(", "))}</span>
            </div>
            <button type="button" class="btn sm" onclick="PRJ.dismissSuggestion(${J(sug.key)})">Dismiss</button>
          </div>
          <div class="proj-sugg-body">
            <div class="proj-sugg-members">
              ${members.map(m => `
                <label class="proj-sugg-member-label">
                  <input type="checkbox" value="${E(m.id)}" checked>
                  <span>${E(m.name)}</span>
                  <span class="proj-sugg-member-count">(${plural(m.items || 0, 'item')})</span>
                </label>
              `).join("")}
            </div>
            <div class="proj-sugg-input-group">
              <label class="proj-sugg-input-label">Name</label>
              <input type="text" class="proj-sugg-input" value="${E(sug.title || "")}" placeholder="Project title">
              <button type="button" class="pri sm" onclick="PRJ.groupSuggestion(${J(sug.into)}, ${J(sug.key)})">Group into one project</button>
            </div>
          </div>
        </div>`;
      });
    }

    // Projects Grid
    if (projects.length) {
      out += `<div class="proj-grid" id="proj-card-grid">`;
      projects.forEach(p => {
        const members = p.members || [];
        const agents = p.agents || [];
        const counts = p.counts || {};
        const searchKeywords = [p.title, p.name, members.join(" "), agents.join(" ")].join(" ").toLowerCase();

        out += `<div class="proj-card" data-search="${E(searchKeywords)}" onclick="if(!event.target.closest('button,a,.proj-card-menu-btn')){location.hash='#Projects?p=${encodeURIComponent(p.id)}'}">
          <div class="proj-card-head">
            <div class="proj-card-title-wrap">
              <a href="#Projects?p=${encodeURIComponent(p.id)}" class="proj-card-title" data-proj-title="${E(p.id)}" onclick="location.hash='#Projects?p=${encodeURIComponent(p.id)}';return false">
                ${E(p.title || p.name)}
              </a>
              ${p.title && p.title !== p.name ? `<span class="proj-card-subtitle">${E(p.name)}</span>` : ""}
            </div>
            <button type="button" class="proj-card-menu-btn" title="Project actions" onclick="event.stopPropagation();PRJ.openProjectRowMenu(this, ${J(p)}, ${J(projects)})">···</button>
          </div>

          ${members.length > 1 ? `
            <div class="proj-card-members">
              ${members.slice(0, 4).map(m => `<span class="chip sm">${E(m)}</span>`).join("")}
              ${members.length > 4 ? `<span class="badge sm">+${members.length - 4}</span>` : ""}
            </div>
          ` : ""}

          <div class="proj-card-stats">
            <span class="proj-card-stat-item" title="Memories">
              <span class="proj-card-stat-val">${counts.items || 0}</span> memories
            </span>
            <span class="proj-card-stat-item" title="Decisions">
              <span class="proj-card-stat-val">${counts.decisions || 0}</span> decisions
            </span>
            <span class="proj-card-stat-item" title="Sessions">
              <span class="proj-card-stat-val">${counts.sessions || 0}</span> sessions
            </span>
            ${counts.open_jobs ? `
              <span class="proj-card-stat-item" style="color:var(--acc);" title="Open jobs">
                <span class="proj-card-stat-val" style="color:var(--acc);">${counts.open_jobs}</span> open jobs
              </span>
            ` : ""}
            ${counts.open_asks ? `
              <span class="proj-card-stat-item" style="color:var(--warn);" title="Open asks">
                <span class="proj-card-stat-val" style="color:var(--warn);">${counts.open_asks}</span> asks
              </span>
            ` : ""}
          </div>

          <div class="proj-card-foot">
            <span>Last active ${E(fmtAgo(p.last))}</span>
            ${agents.length ? `
              <div class="proj-card-avatars" title="Contributors: ${E(agents.join(', '))}">
                ${agents.slice(0, 4).map(ag => avatarHtml(ag)).join("")}
              </div>
            ` : ""}
          </div>
        </div>`;
      });
      out += `</div>`;
      out += `<div id="proj-nomatch" class="empty" style="display:none;padding:32px 0;"><b>No projects match your filter</b></div>`;
    } else {
      out += window.K && window.K.empty
        ? window.K.empty({ icon: "Projects", title: "No projects yet", hint: "Work filed under repositories or folders will appear here as projects." })
        : '<div class="empty"><b>No projects yet</b></div>';
    }

    // Folders ("Not projects") Section
    if (folders.length) {
      out += `<details class="proj-folders-details">
        <summary class="proj-folders-summary">
          <span>Not projects (folders)</span>
          <span class="badge sm">${folders.length}</span>
        </summary>
        <div class="proj-folders-list">
          ${folders.map(f => {
            const counts = f.counts || {};
            return `<div class="proj-folder-row">
              <div class="proj-folder-info">
                <span class="proj-folder-name">${E(f.name)}</span>
                <span class="proj-folder-counts">
                  ${plural(counts.items || 0, 'item')} · ${plural(counts.sessions || 0, 'session')} · last active ${E(fmtAgo(f.last))}
                </span>
              </div>
              <button type="button" class="btn sm" onclick="post('project/update', { id: ${J(f.id)}, kind: 'project' })">This is a project</button>
            </div>`;
          }).join("")}
        </div>
      </details>`;
    }

    out += `</div>`;
    return out;
  }

  /* ---------- Detail View ---------- */
  async function renderProjectDetail(pid, activeTab) {
    let data;
    try {
      data = await api("project_detail?id=" + encodeURIComponent(pid));
      const ttl = document.getElementById("ttl");  // the header and tab show the project, not just "Projects"
      if (ttl && data && data.title) { ttl.textContent = data.title; document.title = data.title + " · Commons"; }
    } catch (e) {
      return `<div class="proj-page">
        <nav class="crumbs" style="margin-bottom:16px;">
          <a href="#Projects" class="crumb" onclick="go('Projects');return false">Projects</a>
          <span class="crumb-separator">/</span>
          <span class="crumb active">Error</span>
        </nav>
        <div class="empty">
          <b>Could not load project details</b>
          <div class="hint">${E(e && e.message ? e.message : e)}</div>
          <div class="empty-actions"><button class="pri" onclick="render()">Try again</button></div>
        </div>
      </div>`;
    }

    if (!data || !data.id) {
      return `<div class="proj-page">
        <nav class="crumbs" style="margin-bottom:16px;">
          <a href="#Projects" class="crumb" onclick="go('Projects');return false">Projects</a>
          <span class="crumb-separator">/</span>
          <span class="crumb active">Not found</span>
        </nav>
        <div class="empty">
          <b>Project not found</b>
          <div class="empty-actions"><button class="pri" onclick="go('Projects')">Back to projects</button></div>
        </div>
      </div>`;
    }

    const members = data.members || [];
    const counts = data.counts || {};
    const openJobs = (data.jobs || []).filter(j => ["running", "queued", "paused"].includes(j.status));
    const activeClaims = (data.claims || []).filter(c => c.status === "claimed" || c.status === "in_progress" || (!c.result && c.status !== "done"));
    const openAsks = (data.asks || []).filter(a => a.status === "open");
    const hasNowWork = openJobs.length > 0 || activeClaims.length > 0 || openAsks.length > 0;

    let out = `<div class="proj-page">
      <!-- Breadcrumbs -->
      <nav class="crumbs" style="margin-bottom:16px;">
        <a href="#Projects" class="crumb" onclick="go('Projects');return false">Projects</a>
        <span class="crumb-separator">/</span>
        <span class="crumb active">${E(data.title)}</span>
      </nav>

      <!-- Header -->
      <div class="proj-detail-head">
        <div class="proj-detail-topbar">
          <div class="proj-detail-title-row">
            <h2 class="proj-detail-title" title="Click to rename" data-proj-title="${E(data.id)}" onclick="PRJ.renameProject(${J(data.id)}, ${J(data.title)}, this)">
              ${E(data.title)}
            </h2>
            <button type="button" class="proj-kind-btn" title="Change kind" onclick="PRJ.openKindMenu(this, ${J(data.id)}, ${J(data.kind)})">
              ${E(data.kind)} ▾
            </button>
          </div>
        </div>

        <div class="proj-detail-members-row">
          <span style="font-size:11.5px;color:var(--fg-subtle);font-weight:500;">Includes:</span>
          ${members.map(m => `
            <span class="chip sm" id="mchip-${E(m.id)}">
              <span>${E(m.name)}</span>
              ${members.length > 1 && m.id !== data.id ? `
                <button type="button" class="chip-remove" title="Ungroup" onclick="PRJ.ungroupMember(${J(m.id)}, ${J(m.name)}, ${J(data.id)})">×</button>
              ` : ""}
            </span>
          `).join("")}
        </div>

        <div class="proj-detail-meta">
          <span>First active <b>${E(fmtAgo(data.first))}</b></span>
          <span>·</span>
          <span>Last active <b>${E(fmtAgo(data.last))}</b></span>
        </div>
      </div>

      <!-- Stats Strip -->
      <div class="proj-stats-strip">
        <div class="proj-stat-box">
          <span class="proj-stat-lbl">Memories</span>
          <span class="proj-stat-val">${fmtNum(counts.items)}</span>
        </div>
        <div class="proj-stat-box">
          <span class="proj-stat-lbl">Decisions</span>
          <span class="proj-stat-val">${fmtNum(counts.decisions)}</span>
        </div>
        <div class="proj-stat-box">
          <span class="proj-stat-lbl">Sessions</span>
          <span class="proj-stat-val">${fmtNum(counts.sessions)}</span>
        </div>
        <div class="proj-stat-box">
          <span class="proj-stat-lbl">Jobs</span>
          <span class="proj-stat-val">${fmtNum(counts.jobs)}</span>
        </div>
        <div class="proj-stat-box">
          <span class="proj-stat-lbl">Changes</span>
          <span class="proj-stat-val">${fmtNum(counts.changes)}</span>
        </div>
        <div class="proj-stat-box">
          <span class="proj-stat-lbl">30d Spend</span>
          <span class="proj-stat-val">${fmtMoney(data.cost_30d)}</span>
        </div>
      </div>`;

    // Now Band
    if (hasNowWork) {
      out += `<div class="proj-now-band">
        <div class="proj-now-head">
          <div class="proj-now-title">
            <span class="status-dot live"></span>
            <span>Active now</span>
          </div>
        </div>
        <div class="proj-now-items">
          ${openJobs.map(j => `
            <div class="proj-now-card" onclick="K.drawer.openId(${J('job:' + j.id)})">
              <div class="proj-now-card-top">
                <span class="proj-now-card-title">${E(j.title || j.id)}</span>
                <span class="badge sm accent">${E(j.status)}</span>
              </div>
              <div class="proj-now-card-sub">
                Progress: <b>${j.done}/${j.steps}</b> steps completed
              </div>
            </div>
          `).join("")}

          ${activeClaims.map(c => `
            <div class="proj-now-card" onclick="go('Workflow')">
              <div class="proj-now-card-top">
                <span class="proj-now-card-title">${E(c.what)}</span>
                <span class="badge sm warn">${E(c.status)}</span>
              </div>
              <div class="proj-now-card-sub" style="display:flex;align-items:center;gap:6px;">
                ${avatarHtml(c.agent)} <span>${E(c.agent || "agent")}</span>
              </div>
            </div>
          `).join("")}

          ${openAsks.map(a => `
            <div class="proj-now-card" onclick="K.drawer.openId(${J('board:' + a.id)})">
              <div class="proj-now-card-top">
                <span class="proj-now-card-title">${E(a.text)}</span>
                <span class="badge sm info">${E(a.kind)}</span>
              </div>
              <div class="proj-now-card-sub">
                Open on board · ${E(fmtAgo(a.at))}
              </div>
            </div>
          `).join("")}
        </div>
      </div>`;
    }

    // Tabs Navigation
    const tabsList = [
      { key: "timeline", label: "Timeline" },
      { key: "work", label: "Work (" + ((data.jobs || []).length + (data.claims || []).length) + ")" },
      { key: "changes", label: "Changes (" + (counts.changes || 0) + ")" },
      { key: "memory", label: "Memory (" + ((data.decisions || []).length + (data.handoffs || []).length) + ")" },
      { key: "people", label: "People (" + ((data.people || []).length) + ")" },
    ];

    out += `<div class="tabs" role="tablist">
      ${tabsList.map(t => `
        <button type="button" class="tabs-item ${activeTab === t.key ? "on" : ""}" role="tab" aria-selected="${activeTab === t.key}" onclick="location.hash='#Projects?p=${encodeURIComponent(data.id)}&tab=${t.key}'">
          ${E(t.label)}
        </button>
      `).join("")}
    </div>`;

    // Tab Content Panels
    if (activeTab === "timeline") {
      out += await renderDetailTimelineTab(data);
    } else if (activeTab === "work") {
      out += renderDetailWorkTab(data);
    } else if (activeTab === "changes") {
      out += renderDetailChangesTab(data);
    } else if (activeTab === "memory") {
      out += await renderDetailMemoryTab(data);
    } else if (activeTab === "people") {
      out += renderDetailPeopleTab(data);
    }

    out += `</div>`;
    return out;
  }

  /* ---------- Tab 1: Timeline ---------- */
  async function renderDetailTimelineTab(data) {
    let events = [];
    let nextCursor = null;
    let url = "project_timeline?id=" + encodeURIComponent(data.id) + "&types=" + encodeURIComponent(S.timelineType);
    if (S.timelineAgent) url += "&agent=" + encodeURIComponent(S.timelineAgent);

    try {
      const res = await api(url);
      events = res.events || [];
      nextCursor = res.next;
      S.timelineEvents = events;
      S.timelineNext = nextCursor;
    } catch (e) {
      return `<div class="empty"><div class="mut">Could not load timeline: ${E(e && e.message ? e.message : e)}</div></div>`;
    }

    const typeFilters = [
      { key: "highlights", label: "Highlights" },
      { key: "all", label: "Everything" },
      { key: "decision", label: "Decisions" },
      { key: "job,job_done,step,work,work_done", label: "Work" },
      { key: "change", label: "Changes" },
      { key: "session", label: "Sessions" },
      { key: "memory,fact,note,observation,document", label: "Memory" },
    ];

    const peopleList = data.people || [];

    let h = `<div class="proj-timeline-controls">
      <div class="proj-filter-chips">
        ${typeFilters.map(f => `
          <button type="button" class="chip sm ${S.timelineType === f.key ? "on" : ""}" onclick="PRJ.switchTimelineType(${J(data.id)}, ${J(f.key)})">
            ${E(f.label)}
          </button>
        `).join("")}
      </div>

      ${peopleList.length ? `
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-size:11.5px;color:var(--fg-subtle);">Agent:</span>
          <select class="ag-search" style="padding:4px 8px;font-size:12px;border-radius:var(--radius-sm);background:var(--bg-card);color:var(--fg-default);border:1px solid var(--border-default);" onchange="PRJ.switchTimelineAgent(${J(data.id)}, this.value)">
            <option value="">All agents</option>
            ${peopleList.map(p => `
              <option value="${E(p.agent)}" ${S.timelineAgent === p.agent ? "selected" : ""}>${E(p.agent)}</option>
            `).join("")}
          </select>
        </div>
      ` : ""}
    </div>

    <div class="proj-feed" id="proj-timeline-feed">`;

    // Group by day
    if (!events.length) {
      h += window.K && window.K.empty
        ? window.K.empty({ icon: "Projects", title: "No timeline events", hint: "No events recorded for this project yet." })
        : '<div class="empty"><div class="mut">No events found.</div></div>';
    } else {
      const days = {};
      events.forEach(ev => {
        let dayKey = "Earlier";
        if (ev.at) {
          const d = new Date(ev.at);
          if (!isNaN(d.getTime())) {
            dayKey = d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
          }
        }
        if (!days[dayKey]) days[dayKey] = [];
        days[dayKey].push(ev);
      });

      Object.keys(days).forEach(day => {
        h += `<div class="proj-day-group">
          <div class="proj-day-label">${E(day)}</div>`;
        days[day].forEach(ev => {
          const hasRef = !!ev.ref;
          h += `<div class="proj-event-row" ${hasRef ? `onclick="PRJ.handleEventClick(${J(ev.ref)})"` : ""}>
            <div class="proj-event-ico-wrap">${eventTypeIcon(ev.type)}</div>
            <div class="proj-event-main">
              <div class="proj-event-title-row">
                <span class="proj-event-title">${E(ev.title || "Untitled")}</span>
                <span class="proj-event-time">${E(fmtAgo(ev.at))}</span>
              </div>
              ${ev.text ? `<div class="proj-event-body">${E(ev.text)}</div>` : ""}
              <div class="proj-event-meta">
                ${ev.agent ? `<span class="proj-event-agent-tag">${avatarHtml(ev.agent)} ${E(ev.agent)}</span>` : ""}
                <span class="badge sm" style="font-size:10.5px;">${E(ev.type)}</span>
              </div>
            </div>
          </div>`;
        });
        h += `</div>`;
      });

      if (nextCursor) {
        h += `<div style="text-align:center;padding:12px 0;">
          <button type="button" id="proj-load-older-btn" class="btn" onclick="PRJ.loadOlderTimeline(${J(data.id)})">Load older events</button>
        </div>`;
      }
    }

    h += `</div>`;
    return h;
  }

  /* ---------- Tab 2: Work ---------- */
  function renderDetailWorkTab(data) {
    const jobs = data.jobs || [];
    const claims = data.claims || [];

    if (!jobs.length && !claims.length) {
      return window.K && window.K.empty
        ? window.K.empty({ icon: "Workflow", title: "No work records yet", hint: "Jobs and work claims filed under this project appear here." })
        : '<div class="empty"><b>No work records yet</b></div>';
    }

    let h = `<div style="display:flex;flex-direction:column;gap:24px;">`;

    // Jobs section
    h += `<div>
      <div style="font-size:14px;font-weight:600;color:var(--fg-default);margin-bottom:12px;display:flex;align-items:center;gap:8px;">
        <span>Jobs (${jobs.length})</span>
      </div>`;

    if (jobs.length) {
      h += `<div class="proj-table-wrap">
        <table class="proj-table">
          <thead>
            <tr>
              <th style="width:28px;"></th>
              <th>Job</th>
              <th>Pattern</th>
              <th>Progress</th>
              <th>Status</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            ${jobs.map(j => {
              const donePct = j.steps ? Math.round((j.done / j.steps) * 100) : 0;
              return `<tr onclick="K.drawer.openId(${J('job:' + j.id)})">
                <td>${statusDotHtml(j.status)}</td>
                <td style="font-weight:600;">${E(j.title || j.id)}</td>
                <td><code style="font-size:11px;">${E(j.pattern || "standard")}</code></td>
                <td>
                  <div style="display:flex;align-items:center;gap:8px;">
                    <span>${j.done}/${j.steps} (${donePct}%)</span>
                  </div>
                </td>
                <td><span class="badge sm ${j.status === 'done' ? 'ok' : j.status === 'failed' ? 'bad' : 'accent'}">${E(j.status)}</span></td>
                <td>${E(fmtAgo(j.updated_at || j.created_at))}</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>`;
    } else {
      h += `<div class="mut sm" style="padding:12px 0;">No jobs found.</div>`;
    }
    h += `</div>`;

    // Claims section
    h += `<div>
      <div style="font-size:14px;font-weight:600;color:var(--fg-default);margin-bottom:12px;display:flex;align-items:center;gap:8px;">
        <span>Work Claims (${claims.length})</span>
      </div>`;

    if (claims.length) {
      h += `<div class="proj-table-wrap">
        <table class="proj-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Task</th>
              <th>Status</th>
              <th>Time</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            ${claims.map(c => `
              <tr onclick="go('Workflow')">
                <td>
                  <div class="proj-table-agent-cell">
                    ${avatarHtml(c.agent)} <span>${E(c.agent || "agent")}</span>
                  </div>
                </td>
                <td style="font-weight:500;">${E(c.what)}</td>
                <td><span class="badge sm ${c.status === 'done' ? 'ok' : c.status === 'failed' ? 'bad' : 'warn'}">${E(c.status)}</span></td>
                <td>${E(fmtAgo(c.at))}</td>
                <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;color:var(--fg-muted);">${E(c.result || "No result text")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>`;
    } else {
      h += `<div class="mut sm" style="padding:12px 0;">No work claims found.</div>`;
    }
    h += `</div></div>`;

    return h;
  }

  /* ---------- Tab 3: Changes ---------- */
  function renderDetailChangesTab(data) {
    const changesList = data.changes || [];
    if (!changesList.length) {
      return window.K && window.K.empty
        ? window.K.empty({
            icon: "Changes",
            title: "No recorded changes yet",
            hint: "Agents record diffs with record_changes or a handoff with a commit.",
          })
        : '<div class="empty"><b>No recorded changes yet: agents record diffs with record_changes or a handoff with a commit</b></div>';
    }

    let h = `<div style="display:flex;flex-direction:column;gap:16px;">`;
    changesList.forEach((c, idx) => {
      const commitStr = c.commit ? c.commit.slice(0, 10) : "";
      const branchStr = c.branch || "";
      const filesList = Array.isArray(c.files) ? c.files : [];
      const diffExpanded = !!S.diffExpanded[c.path || idx];

      h += `<div class="proj-change-card">
        <div class="proj-change-head">
          <div class="proj-change-info">
            <div class="proj-change-title-row">
              ${commitStr ? `<span class="proj-commit-tag">${E(commitStr)}</span>` : ""}
              ${branchStr ? `<span class="proj-branch-tag">on ${E(branchStr)}</span>` : ""}
              <span style="font-size:12px;color:var(--fg-subtle);">${E(fmtAgo(c.ts || c.at))}</span>
            </div>
            <div style="font-size:12px;color:var(--fg-muted);display:flex;align-items:center;gap:6px;margin-top:2px;">
              ${avatarHtml(c.by)} <span>${E(c.by || "agent")}</span>
              ${c.repo ? `<span>·</span> <code>${E(c.repo)}</code>` : ""}
            </div>
          </div>
          <div style="display:flex;gap:8px;">
            <button type="button" class="btn sm" onclick="PRJ.openChangeDrawer(${J(c.path)})">Inspect in drawer</button>
          </div>
        </div>

        ${filesList.length ? `
          <div class="proj-files-row">
            ${filesList.map(f => `<span class="proj-file-chip">${E(f)}</span>`).join("")}
          </div>
        ` : ""}

        <details ${diffExpanded ? "open" : ""}>
          <summary style="font-size:12px;font-weight:500;color:var(--fg-muted);cursor:pointer;user-select:none;padding:4px 0;">
            ${c.diff ? "View diff (" + c.diff.length + " chars)" : "No diff"}
          </summary>
          <div class="proj-diff-container" style="margin-top:8px;">
            ${renderDiffHtml(c.diff)}
          </div>
        </details>
      </div>`;
    });
    h += `</div>`;
    return h;
  }

  /* ---------- Tab 4: Memory ---------- */
  async function renderDetailMemoryTab(data) {
    const decisions = data.decisions || [];
    const handoffs = data.handoffs || [];

    // Fetch memory timeline
    let memEvents = [];
    try {
      const res = await api("project_timeline?id=" + encodeURIComponent(data.id) + "&types=memory,fact,note,observation,document&limit=30");
      memEvents = res.events || [];
    } catch (e) {
      memEvents = [];
    }

    let h = `<div class="proj-mem-split">
      <!-- Decisions -->
      <div class="proj-mem-sec">
        <div class="proj-mem-sec-head">
          <h3 class="proj-mem-sec-title">Key Decisions (${decisions.length})</h3>
        </div>
        ${decisions.length ? `
          <div>
            ${decisions.map(d => `
              <div class="proj-mem-item" onclick="K.drawer.openId(${J('mem:' + d.id)})">
                <div style="margin-top:2px;">${avatarHtml(d.agent)}</div>
                <div class="proj-mem-item-main">
                  <div class="proj-mem-item-title">${E(d.title)}</div>
                  <div class="proj-mem-item-sub">${E(d.agent || "agent")} · ${E(fmtAgo(d.at))}</div>
                </div>
              </div>
            `).join("")}
          </div>
        ` : '<div class="mut sm" style="padding:8px 0;">No decisions recorded yet.</div>'}
      </div>

      <!-- Handoffs -->
      <div class="proj-mem-sec">
        <div class="proj-mem-sec-head">
          <h3 class="proj-mem-sec-title">Recent Handoffs (${handoffs.length})</h3>
        </div>
        ${handoffs.length ? `
          <div>
            ${handoffs.map(hf => `
              <div class="proj-mem-item" onclick="K.drawer.openId(${J('mem:' + hf.id)})">
                <div style="margin-top:2px;">${avatarHtml(hf.agent)}</div>
                <div class="proj-mem-item-main">
                  <div class="proj-mem-item-title">${E(hf.title)}</div>
                  <div class="proj-mem-item-sub">${E(hf.agent || "agent")} · ${E(fmtAgo(hf.at))}</div>
                </div>
              </div>
            `).join("")}
          </div>
        ` : '<div class="mut sm" style="padding:8px 0;">No handoffs recorded yet.</div>'}
      </div>
    </div>`;

    // Memory Stream
    h += `<div>
      <div style="font-size:14px;font-weight:600;color:var(--fg-default);margin-bottom:12px;">Memory Stream</div>`;
    if (memEvents.length) {
      h += `<div class="proj-feed">`;
      memEvents.forEach(ev => {
        h += `<div class="proj-event-row" ${ev.ref ? `onclick="PRJ.handleEventClick(${J(ev.ref)})"` : ""}>
          <div class="proj-event-ico-wrap">${eventTypeIcon(ev.type)}</div>
          <div class="proj-event-main">
            <div class="proj-event-title-row">
              <span class="proj-event-title">${E(ev.title || "Untitled")}</span>
              <span class="proj-event-time">${E(fmtAgo(ev.at))}</span>
            </div>
            ${ev.text ? `<div class="proj-event-body">${E(ev.text)}</div>` : ""}
            <div class="proj-event-meta">
              ${ev.agent ? `<span class="proj-event-agent-tag">${avatarHtml(ev.agent)} ${E(ev.agent)}</span>` : ""}
              <span class="badge sm">${E(ev.type)}</span>
            </div>
          </div>
        </div>`;
      });
      h += `</div>`;
    } else {
      h += `<div class="mut sm" style="padding:12px 0;">No memories or notes filed under this project yet.</div>`;
    }
    h += `</div>`;

    return h;
  }

  /* ---------- Tab 5: People ---------- */
  function renderDetailPeopleTab(data) {
    const people = data.people || [];
    if (!people.length) {
      return window.K && window.K.empty
        ? window.K.empty({ icon: "Agents", title: "No recorded activity by agents yet", hint: "Agents appear here as they contribute memories, decisions, or code." })
        : '<div class="empty"><b>No recorded activity by agents yet</b></div>';
    }

    return `<div class="proj-table-wrap">
      <table class="proj-table">
        <thead>
          <tr>
            <th>Agent</th>
            <th>Memories</th>
            <th>Decisions</th>
            <th>Sessions</th>
            <th>Steps done</th>
            <th>Work claims</th>
            <th>Last active</th>
          </tr>
        </thead>
        <tbody>
          ${people.map(p => `
            <tr onclick="K.drawer.openId(${J('agent:' + p.agent)})">
              <td>
                <div class="proj-table-agent-cell">
                  ${avatarHtml(p.agent)}
                  <span>${E(p.agent)}</span>
                </div>
              </td>
              <td>${fmtNum(p.memories)}</td>
              <td>${fmtNum(p.decisions)}</td>
              <td>${fmtNum(p.sessions)}</td>
              <td>${fmtNum(p.steps)}</td>
              <td>${fmtNum(p.claims)}</td>
              <td>${E(fmtAgo(p.last))}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>`;
  }

  /* ==========================================================================
     PAGE: Tasks
     ========================================================================== */
  window.PRJ.filterTasks = function (val) {
    S.taskFilter = String(val || "").toLowerCase().trim();
    const rows = document.querySelectorAll(".tasks-row");
    let matchCount = 0;
    rows.forEach(r => {
      const t = (r.dataset.search || "").toLowerCase();
      const match = !S.taskFilter || t.includes(S.taskFilter);
      r.style.display = match ? "" : "none";
      if (match) matchCount++;
    });
    const nomatch = document.getElementById("tasks-nomatch");
    if (nomatch) nomatch.style.display = (rows.length > 0 && matchCount === 0) ? "block" : "none";
  };

  window.PRJ.openAttachProjectMenu = function (btn, id, projects) {
    if (!window.K || !window.K.menu) return;
    const items = (projects || []).map(p => ({
      label: p.title || p.name,
      run: () => post("project/group", { into: p.id, ids: [id] }),
    }));
    window.K.menu(btn, items);
  };

  V["Tasks"] = async function () {
    let data;
    try {
      data = await api("project_list");
    } catch (e) {
      return `<div class="proj-page">
        <div class="empty">
          <b>Could not load tasks</b>
          <div class="hint">${E(e && e.message ? e.message : e)}</div>
          <div class="empty-actions"><button class="pri" onclick="render()">Try again</button></div>
        </div>
      </div>`;
    }

    const tasks = (data && data.tasks) || [];
    const jobs = (data && data.jobs) || [];
    const projects = (data && data.projects) || [];
    const hasAny = tasks.length > 0 || jobs.length > 0;

    let out = `<div class="proj-page">
      <div class="page-head">
        <div class="page-head-main">
          <h2 class="page-head-title">Tasks</h2>
          <p class="page-head-subtitle">One-off work and jobs outside a project</p>
        </div>
        <div class="page-head-actions">
          <label class="ag-search" style="min-width:240px">
            <svg class="ag-i" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
            <input type="search" id="tasks-filter-in" value="${E(S.taskFilter || "")}" placeholder="Filter tasks..." autocomplete="off" oninput="PRJ.filterTasks(this.value)">
          </label>
        </div>
      </div>`;

    if (!hasAny) {
      out += window.K && window.K.empty
        ? window.K.empty({ icon: "Tasks", title: "No tasks found", hint: "Small one-off tasks and loose jobs without a project appear here." })
        : '<div class="empty"><b>No one-off tasks or unassigned jobs</b></div>';
      out += `</div>`;
      return out;
    }

    out += `<div class="tasks-split">`;

    // Small Task-Sized Names
    if (tasks.length) {
      out += `<div>
        <div class="tasks-sec-head">
          <h3 class="tasks-sec-title">One-off work (${tasks.length})</h3>
        </div>
        <div class="tasks-list">
          ${tasks.map(t => {
            const counts = t.counts || {};
            const searchKw = [t.title, t.name].join(" ").toLowerCase();
            return `<div class="tasks-row" data-search="${E(searchKw)}" onclick="if(!event.target.closest('button,a')){location.hash='#Projects?p=${encodeURIComponent(t.id)}'}">
              <div class="tasks-row-left">
                <span class="status-dot idle"></span>
                <div class="tasks-row-title-wrap">
                  <a href="#Projects?p=${encodeURIComponent(t.id)}" class="tasks-row-title" onclick="location.hash='#Projects?p=${encodeURIComponent(t.id)}';return false">
                    ${E(t.title || t.name)}
                  </a>
                  <span class="tasks-row-sub">
                    ${plural(counts.items || 0, 'item')} · last active ${E(fmtAgo(t.last))}
                  </span>
                </div>
              </div>
              <div class="tasks-row-actions">
                <button type="button" class="btn sm" onclick="event.stopPropagation();post('project/update', { id: ${J(t.id)}, kind: 'project' })">Make it a project</button>
                ${projects.length ? `
                  <button type="button" class="btn sm" onclick="event.stopPropagation();PRJ.openAttachProjectMenu(this, ${J(t.id)}, ${J(projects)})">Attach to project ▾</button>
                ` : ""}
              </div>
            </div>`;
          }).join("")}
        </div>
      </div>`;
    }

    // Loose Jobs With No Project
    if (jobs.length) {
      out += `<div>
        <div class="tasks-sec-head">
          <h3 class="tasks-sec-title">Unassigned Jobs (${jobs.length})</h3>
        </div>
        <div class="tasks-list">
          ${jobs.map(j => {
            const searchKw = [j.title, j.id, j.status].join(" ").toLowerCase();
            return `<div class="tasks-row" data-search="${E(searchKw)}" onclick="if(!event.target.closest('button,a')){K.drawer.openId(${J('job:' + j.id)})}">
              <div class="tasks-row-left">
                ${statusDotHtml(j.status)}
                <div class="tasks-row-title-wrap">
                  <span class="tasks-row-title">${E(j.title || j.id)}</span>
                  <span class="tasks-row-sub">
                    Status: <b style="color:var(--fg-default);">${E(j.status)}</b> · updated ${E(fmtAgo(j.last))}
                  </span>
                </div>
              </div>
              <div class="tasks-row-actions">
                <button type="button" class="btn sm" onclick="event.stopPropagation();K.drawer.openId(${J('job:' + j.id)})">Inspect job</button>
              </div>
            </div>`;
          }).join("")}
        </div>
      </div>`;
    }

    out += `<div id="tasks-nomatch" class="empty" style="display:none;padding:32px 0;"><b>No tasks match your filter</b></div>`;
    out += `</div></div>`;
    return out;
  };

})();
