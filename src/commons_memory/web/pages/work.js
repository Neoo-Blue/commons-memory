/* ==========================================================================
   COMMONS WORK WORKSPACE (pages/work.js)
   Workflow (Jobs Cockpit), Orchestration, Task breakdown, Mission control,
   Changes, Sessions, Drive, Routing, and the shared Job Detail Drawer.
   Zero framework, vanilla JS, accessible, responsive at 1440px and 390px.
   ========================================================================== */

(function () {
  "use strict";

  // State globals
  window.work_status_filter = window.work_status_filter || "all";
  window.work_pattern_filter = window.work_pattern_filter || "";
  window.work_project_filter = window.work_project_filter || "";
  window.work_search = window.work_search || "";
  window.cjob = window.cjob || "";
  window.cagent = window.cagent || "";
  window.csearch = window.csearch || "";
  window.cdiff_expanded = window.cdiff_expanded || {};
  window.orch_expanded = window.orch_expanded || {};
  window.bj = window.bj || "";
  window.bn = window.bn || "";
  window.dp = window.dp || "";
  window.dpath = window.dpath || "/";
  window.df = window.df || "";
  window.wf_collapsed = window.wf_collapsed || {};

  /* ---------- Helpers ---------- */
  function fmtMoney(n) {
    if (window.K && window.K.fmt && window.K.fmt.money) return window.K.fmt.money(n);
    const v = Number(n) || 0;
    return "$" + v.toFixed(4);
  }

  function fmtTokens(n) {
    if (window.K && window.K.fmt && window.K.fmt.tokens) return window.K.fmt.tokens(n);
    const v = Number(n) || 0;
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M tok";
    if (v >= 1e3) return Math.round(v / 1e3) + "k tok";
    return v + " tok";
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

  function statusDotHtml(st) {
    const s = String(st || "").toLowerCase();
    let cls = "idle";
    if (s === "running" || s === "claimed" || s === "live") cls = "live";
    else if (s === "waiting_owner" || s === "awaiting_gate" || s === "needs_user" || s === "busy") cls = "busy";
    else if (s === "failed" || s === "reject" || s === "error") cls = "bad";
    else if (s === "done" || s === "approved" || s === "verified" || s === "pass") cls = "idle";
    return `<span class="status-dot ${cls}"></span>`;
  }

  function badgeHtml(st) {
    const s = String(st || "").toLowerCase();
    let cls = "";
    if (s === "done" || s === "approved" || s === "verified" || s === "pass") cls = "ok";
    else if (s === "running" || s === "claimed" || s === "in_progress") cls = "accent";
    else if (s === "waiting_owner" || s === "awaiting_gate" || s === "needs_user" || s === "warn") cls = "warn";
    else if (s === "failed" || s === "reject" || s === "fail" || s === "bad") cls = "bad";
    else if (s === "info") cls = "info";
    return `<span class="badge ${cls}">${E(st || "unknown")}</span>`;
  }

  function avatarHtml(name) {
    const n = String(name || "agent").trim();
    const initials = n.slice(0, 2).toUpperCase();
    return `<span class="avatar" title="${E(n)}"><span>${E(initials)}</span></span>`;
  }

  /* ---------- Action Handlers ---------- */
  window.workRetryStep = async function (stepId, jobId) {
    const r = await postRaw("subtask/retry", { id: stepId });
    if (r && !r.error) {
      if (window.K && window.K.toast) window.K.toast("Step retried", { kind: "ok" });
      else if (typeof toast === "function") toast("Step retried");
      if (jobId) window.openJobDrawer(jobId);
      render();
    } else {
      const err = (r && r.error) || "Failed to retry step";
      if (window.K && window.K.toast) window.K.toast(err, { kind: "err" });
      else if (typeof toast === "function") toast(err, "err");
    }
  };

  window.workMoveStepMenu = async function (anchorEl, stepId, jobId) {
    try {
      const agents = await api("agents");
      if (!agents || !agents.length) {
        if (window.K && window.K.toast) window.K.toast("No connected agents", { kind: "warn" });
        return;
      }
      const items = agents.map(a => ({
        label: a.name + (a.model ? " (" + a.model.split("/").pop() + ")" : ""),
        icon: "Agents",
        run: async () => {
          const r = await postRaw("subtask/reassign", { id: stepId, agent: a.name });
          if (r && !r.error) {
            if (window.K && window.K.toast) window.K.toast("Moved step to " + a.name, { kind: "ok" });
            else if (typeof toast === "function") toast("Moved step to " + a.name);
            if (jobId) window.openJobDrawer(jobId);
            render();
          } else {
            const err = (r && r.error) || "Failed to reassign step";
            if (window.K && window.K.toast) window.K.toast(err, { kind: "err" });
            else if (typeof toast === "function") toast(err, "err");
          }
        }
      }));
      if (window.K && window.K.menu) {
        window.K.menu(anchorEl, items);
      }
    } catch (e) {
      if (window.K && window.K.toast) window.K.toast(String(e.message || e), { kind: "err" });
    }
  };

  window.workRetryJob = async function (jobId) {
    const r = await postRaw("job/retry", { id: jobId });
    if (r && !r.error) {
      if (window.K && window.K.toast) window.K.toast("Job retried", { kind: "ok" });
      else if (typeof toast === "function") toast("Job retried");
      if (jobId) window.openJobDrawer(jobId);
      render();
    } else {
      const err = (r && r.error) || "Failed to retry job";
      if (window.K && window.K.toast) window.K.toast(err, { kind: "err" });
      else if (typeof toast === "function") toast(err, "err");
    }
  };

  window.workCancelJob = async function (jobId) {
    const r = await postRaw("job/cancel", { id: jobId });
    if (r && !r.error) {
      if (window.K && window.K.toast) window.K.toast("Job cancelled", { kind: "ok" });
      else if (typeof toast === "function") toast("Job cancelled");
      if (window.K && window.K.drawer) window.K.drawer.close();
      render();
    } else {
      const err = (r && r.error) || "Failed to cancel job";
      if (window.K && window.K.toast) window.K.toast(err, { kind: "err" });
      else if (typeof toast === "function") toast(err, "err");
    }
  };

  /* ---------- Unified Job Detail Drawer ---------- */
  window.openJobDrawer = async function (jobId) {
    if (!jobId) return;
    if (!window.K || !window.K.drawer) {
      window.location.hash = "#Task breakdown";
      window.bj = jobId;
      render();
      return;
    }

    // fetch first: the title, subtitle and the actions that make sense depend on the job's state
    let first = await api("job_detail?id=" + encodeURIComponent(jobId)).catch(() => null);
    const j = (first && first.job) || {};
    const acts = [];
    if (j.status === "failed") acts.push({ label: "Retry job", kind: "primary", run: async () => { await window.workRetryJob(jobId); } });
    if (j.status === "running" || j.status === "failed") acts.push({ label: "Cancel job", kind: "danger", run: async () => { await window.workCancelJob(jobId); } });
    window.K.drawer.open({
      id: "job:" + jobId,
      title: j.title || ("Job " + jobId.slice(0, 10)),
      subtitle: [j.pattern, j.project || "no project", j.status].filter(Boolean).join(" · "),
      render: async () => {
        const data = first || await api("job_detail?id=" + encodeURIComponent(jobId));
        first = null;
        if (!data || !data.job) {
          return `<div class="empty"><p class="mut">Job not found.</p></div>`;
        }

        const job = data.job;
        const subtasks = data.subtasks || [];
        const folder = data.folder || ("/jobs/" + job.id);
        const wf = data.workflow;

        // Stats summary
        const doneCount = subtasks.filter(s => s.status === "done").length;
        const totalCount = subtasks.length;
        const pct = totalCount ? Math.round((doneCount / totalCount) * 100) : 0;
        const costStr = fmtMoney(job.used ? job.used.cost : 0);
        const tokStr = fmtTokens(job.used ? job.used.tokens : 0);

        // Header Card
        let out = `<div class="drawer-job-wrap">
          <div class="drawer-job-banner">
            <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;flex-wrap:wrap;">
              <div>
                <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                  ${statusDotHtml(job.status)}
                  <b style="font-size:13.5px;color:var(--fg-default);">${E(job.title)}</b>
                </div>
                <div style="font-size:11.5px;color:var(--fg-muted);margin-top:2px;">
                  Pattern: <code>${E(job.pattern)}</code> · Project: <b>${E(job.project || "none")}</b> · Submitted by: <b>${E(job.submitted_by || "user")}</b>
                </div>
              </div>
              <div>${badgeHtml(job.status)}</div>
            </div>

            <div class="drawer-job-stats" style="margin-top:8px;">
              <div class="drawer-stat-item">
                <span class="drawer-stat-lbl">Progress</span>
                <span class="drawer-stat-val">${doneCount}/${totalCount} (${pct}%)</span>
              </div>
              <div class="drawer-stat-item">
                <span class="drawer-stat-lbl">Spend</span>
                <span class="drawer-stat-val">${costStr}</span>
              </div>
              <div class="drawer-stat-item">
                <span class="drawer-stat-lbl">Tokens</span>
                <span class="drawer-stat-val">${tokStr}</span>
              </div>
              <div class="drawer-stat-item">
                <span class="drawer-stat-lbl">Updated</span>
                <span class="drawer-stat-val">${fmtAgo(job.updated_at)}</span>
              </div>
            </div>

            <div class="drawer-links-bar">
              <a href="#Drive" class="drawer-link-btn" onclick="window.dp=${J(job.project || '')};window.dpath=${J(folder)};go('Drive');K.drawer.close();return false">
                <svg class="wico" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/></svg> Workspace: <code>${E(folder)}</code>
              </a>
              <a href="#Changes" class="drawer-link-btn" onclick="window.cjob=${J(job.id)};go('Changes');K.drawer.close();return false">
                <svg class="wico" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3.5h12v17l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 11.5h6"/></svg> Changes for this job
              </a>
            </div>
          </div>`;

        // If SDLC job, render the full 6-stage spine, gates, savings meter, agent hierarchy & trace matrix
        if (job.pattern === "sdlc" && wf) {
          const stages = wf.stages || [];
          const nodes = wf.nodes || [];
          const trace = wf.trace || [];
          const savings = wf.savings || { handed: 0, naive: 0 };

          // Savings Meter
          const handed = Number(savings.handed) || 0;
          const naive = Number(savings.naive) || 0;
          const ratio = (naive > 0 && handed > 0) ? (naive / handed).toFixed(1) : null;
          const savingsPct = naive > 0 ? Math.min(100, Math.max(2, Math.round((handed / naive) * 100))) : 0;
          const savingsRatioTxt = (naive > 0 && ratio && Number(ratio) >= 1.1) ? ` (${ratio}x less)` : "";

          out += `<div class="card" style="padding:12px;margin:0;">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:12px;font-weight:600;">
              <span>SDLC Stage Spine &amp; Owner Gates</span>
              <span class="mut sm">Savings: ${fmtTokens(handed)} vs ${fmtTokens(naive)}${savingsRatioTxt}</span>
            </div>
            <div class="work-progress-track" style="margin:8px 0 10px 0;">
              <div class="work-progress-bar" style="width:${savingsPct}%;"></div>
            </div>
            <div class="drawer-spine">`;

          stages.forEach(st => {
            const gates = st.gates || [];
            let gatesHtml = "";
            gates.forEach(g => {
              const isWaiting = g.status === "waiting_owner" || g.status === "pending";
              gatesHtml += `<span class="drawer-gate-badge ${isWaiting ? 'waiting' : ''}" title="${E(g.title || g.type)}">
                ${g.type === "owner" ? "You approve" : E(g.title || "Review")}: ${badgeHtml(g.verdict || g.status)}
              </span>`;
            });

            out += `<div class="drawer-stage-card ${E(st.status || 'pending')}">
              <div class="drawer-stage-head">
                <span class="drawer-stage-title">${E(st.stage || "").toUpperCase()}</span>
                <span class="mut sm"><code>${E(st.command || ("/" + st.stage))}</code></span>
                <div>${badgeHtml(st.status)}</div>
              </div>
              <div class="drawer-stage-body">
                <div><b>${E(st.agent || "unassigned")}</b> ${st.model ? `<span class="mut sm">(${E(st.model.split("/").pop())})</span>` : ""}</div>
                <div class="mut sm">${fmtTokens(st.tokens)} · $${(+st.cost || 0).toFixed(2)}</div>
              </div>
              ${gatesHtml ? `<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px;">${gatesHtml}</div>` : ""}
            </div>`;
          });

          out += `</div></div>`;

          // Traceability Summary
          if (trace.length) {
            const gaps = trace.filter(t => t.tests === "missing").length;
            const failedTrace = trace.filter(t => t.tests === "fail").length;
            const covered = trace.length - gaps;
            out += `<div class="card" style="padding:12px;margin:0;">
              <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:12px;font-weight:600;">
                <span>Traceability Matrix (${covered}/${trace.length} covered)</span>
                ${gaps > 0 ? `<span class="badge bad">${gaps} missing</span>` : `<span class="badge ok">all verified</span>`}
              </div>
              <div class="work-scroll-wrap" style="margin-top:8px;">
                <table style="width:100%;font-size:11.5px;border-collapse:collapse;">
                  <tr style="border-bottom:1px solid var(--border-subtle);"><th style="text-align:left;padding:4px 6px;">AC</th><th style="text-align:left;padding:4px 6px;">Criterion</th><th style="text-align:left;padding:4px 6px;">Tests</th><th style="text-align:left;padding:4px 6px;">Review</th></tr>
                  ${trace.map(t => `<tr><td style="padding:4px 6px;"><b>${E(t.ac)}</b></td><td style="padding:4px 6px;">${E(t.text)}</td><td style="padding:4px 6px;">${badgeHtml(t.tests)}</td><td style="padding:4px 6px;">${badgeHtml(t.verdict || "pending")}</td></tr>`).join("")}
                </table>
              </div>
            </div>`;
          }
        } else {
          // Other patterns: Step graph / sequence
          out += `<div class="card" style="padding:12px;margin:0;">
            <div style="font-size:12px;font-weight:600;margin-bottom:8px;">Execution Steps &amp; Lifecycle</div>
            <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
              ${subtasks.map((s, i) => `
                <span class="badge ${s.status === 'done' ? 'ok' : s.status === 'running' ? 'accent' : s.status === 'failed' ? 'bad' : ''}">
                  ${E(s.key || s.type)}
                </span>
                ${i < subtasks.length - 1 ? '<span class="mut sm">→</span>' : ''}
              `).join("")}
            </div>
          </div>`;
        }

        // Subtasks Detailed List with Owner Actions
        out += `<div style="display:flex;flex-direction:column;gap:8px;">
          <div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.04em;color:var(--fg-muted);">
            Subtasks (${subtasks.length})
          </div>`;

        subtasks.forEach(s => {
          const route = s.route || {};
          const isPendingOrClaimed = s.status === "pending" || s.status === "claimed" || s.status === "running";
          const isFailed = s.status === "failed";
          const attemptsTxt = (s.attempts != null) ? `Attempt ${s.attempts}/${s.max_attempts || 3}` : "";

          out += `<div class="drawer-step-item">
            <div class="drawer-step-head">
              <div style="display:flex;align-items:center;gap:6px;">
                ${statusDotHtml(s.status)}
                <span class="drawer-step-title">${E(s.key)}: ${E(s.title || s.type)}</span>
              </div>
              <div>${badgeHtml(s.verdict || s.status)}</div>
            </div>

            <div class="drawer-step-meta">
              <span>Agent: <b>${E(s.agent || "unassigned")}</b></span>
              ${route.model ? `<span>Model: <code>${E(route.model.split("/").pop())}</code></span>` : ""}
              ${route.effort ? `<span>Effort: <span class="badge info">${E(route.effort)}</span></span>` : ""}
              ${attemptsTxt ? `<span class="mut sm">${E(attemptsTxt)}</span>` : ""}
            </div>

            ${route.rationale ? `<div style="font-size:11px;color:var(--fg-muted);background:var(--bg-surface);padding:4px 8px;border-radius:4px;border:1px solid var(--border-subtle);">${E(route.rationale)}</div>` : ""}

            ${s.result ? `<details style="font-size:11.5px;"><summary class="mut" style="cursor:pointer;">Result preview</summary><pre style="max-height:160px;overflow:auto;margin:4px 0;background:var(--bg-input);padding:6px;border-radius:4px;font-size:11px;">${E(s.result)}</pre></details>` : ""}

            <div class="drawer-step-actions">
              ${(isFailed || isPendingOrClaimed) ? `
                <button type="button" class="btn sm" onclick="workRetryStep(${J(s.id)}, ${J(job.id)})">Retry step</button>
                <button type="button" class="btn sm" onclick="workMoveStepMenu(this, ${J(s.id)}, ${J(job.id)})">Move to agent...</button>
              ` : ""}
            </div>
          </div>`;
        });

        out += `</div></div>`;
        return out;
      },
      actions: acts
    });
  };

  // Register deep-linking for job drawer
  if (window.K && window.K.drawer && window.K.drawer.on) {
    window.K.drawer.on("job:", (drawerId) => {
      const jid = drawerId.replace(/^job:/, "");
      window.openJobDrawer(jid);
    });
  }

  /* ==========================================================================
     PAGE 1: Workflow (The Jobs Cockpit)
     ========================================================================== */
  V["Workflow"] = async function () {
    const [jobsData, indexData, missionData, projectsData] = await Promise.all([
      api("jobs").catch(() => []),
      api("jobs_index").catch(() => []),
      api("mission").catch(() => ({})),
      api("projects").catch(() => [])
    ]);

    const jobs = Array.isArray(jobsData) ? jobsData : [];
    const indexRows = Array.isArray(indexData) ? indexData : [];
    const projects = Array.isArray(projectsData) ? projectsData : [];
    const projectJobsMap = {};
    if (missionData && missionData.projects) {
      missionData.projects.forEach(p => {
        (p.jobs || []).forEach(j => {
          projectJobsMap[j.id] = p.project;
        });
      });
    }

    const idxMap = {};
    indexRows.forEach(r => { idxMap[r.id] = r; });

    // Normalize jobs
    const normalized = jobs.map(j => {
      const idx = idxMap[j.job_id] || {};
      const subs = j.subtasks || [];
      const doneSubs = subs.filter(s => s.status === "done").length;
      const totalSubs = subs.length;
      const progress = totalSubs ? doneSubs / totalSubs : 0;
      const activeSub = subs.find(s => s.status === "running" || s.status === "claimed" || s.status === "awaiting_gate")
        || subs.find(s => s.status === "pending")
        || subs[subs.length - 1];

      const needsOwner = subs.some(s =>
        (s.type === "owner" && (s.status === "pending" || s.status === "awaiting_gate" || s.status === "waiting_owner"))
        || s.status === "waiting_owner"
      );

      return {
        id: j.job_id,
        title: j.title || "Untitled job",
        pattern: j.pattern || "standard",
        status: j.status || "unknown",
        needsOwner: needsOwner,
        progress: progress,
        doneSubs: doneSubs,
        totalSubs: totalSubs,
        activeStep: activeSub ? (activeSub.key + ": " + (activeSub.title || activeSub.type)) : "none",
        activeAgent: activeSub ? (activeSub.agent || "unassigned") : "unassigned",
        cost: (j.used && j.used.cost) ? j.used.cost : 0,
        tokens: (j.used && j.used.tokens) ? j.used.tokens : 0,
        createdAt: idx.created_at || (j.used && j.used.seconds ? Date.now() - j.used.seconds * 1000 : Date.now()),
        project: projectJobsMap[j.job_id] || ""
      };
    });

    // Extract unique patterns and projects for filters
    const patterns = Array.from(new Set(normalized.map(j => j.pattern).filter(Boolean)));
    const filterStatus = window.work_status_filter || "all";
    const filterPattern = window.work_pattern_filter || "";
    const filterProject = window.work_project_filter || "";
    const filterSearch = (window.work_search || "").trim().toLowerCase();

    // Filter jobs
    const filtered = normalized.filter(j => {
      if (filterStatus === "running" && j.status !== "running") return false;
      if (filterStatus === "needs_owner" && !j.needsOwner) return false;
      if (filterStatus === "failed" && j.status !== "failed") return false;
      if (filterStatus === "done" && j.status !== "done") return false;
      if (filterPattern && j.pattern !== filterPattern) return false;
      if (filterProject && j.project !== filterProject) return false;
      if (filterSearch) {
        const text = `${j.title} ${j.pattern} ${j.id} ${j.activeAgent} ${j.project}`.toLowerCase();
        if (!text.includes(filterSearch)) return false;
      }
      return true;
    });

    // Counts for segmented buttons
    const countAll = normalized.length;
    const countRunning = normalized.filter(j => j.status === "running").length;
    const countNeedsOwner = normalized.filter(j => j.needsOwner).length;
    const countFailed = normalized.filter(j => j.status === "failed").length;
    const countDone = normalized.filter(j => j.status === "done").length;

    let rowsHtml = "";
    if (!filtered.length) {
      rowsHtml = `<div style="padding:var(--space-6);">
        ${window.K && window.K.empty ? window.K.empty({
          icon: "Workflow",
          title: "No matching jobs found",
          hint: "Try resetting your filter or search query.",
          actions: [{ label: "Clear filters", run: () => { window.work_status_filter = "all"; window.work_pattern_filter = ""; window.work_project_filter = ""; window.work_search = ""; render(); } }]
        }) : '<div class="empty"><b>No matching jobs found</b></div>'}
      </div>`;
    } else {
      rowsHtml = filtered.map(j => {
        const pct = Math.round(j.progress * 100);
        const barCls = j.status === "failed" ? "bad" : j.needsOwner ? "warn" : "";

        return `<div class="work-job-row" onclick="openJobDrawer(${J(j.id)})" role="button" tabindex="0">
          <div class="row-prefix">${statusDotHtml(j.status)}</div>

          <div class="work-job-main">
            <div class="work-job-title-row">
              <span class="work-job-title">${E(j.title)}</span>
              <span class="badge ${j.pattern === 'sdlc' ? 'accent' : ''}">${E(j.pattern)}</span>
              ${j.project ? `<span class="badge info">${E(j.project)}</span>` : ""}
              ${j.needsOwner ? `<span class="badge warn">needs you</span>` : ""}
            </div>
            <div class="work-job-meta-row">
              <span>Step: <b>${E(j.activeStep)}</b></span>
              <span>Agent: <b>${E(j.activeAgent)}</b></span>
            </div>
          </div>

          <div class="work-job-progress-col">
            <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--fg-muted);">
              <span>${j.doneSubs}/${j.totalSubs} steps</span>
              <span>${pct}%</span>
            </div>
            <div class="work-progress-track">
              <div class="work-progress-bar ${barCls}" style="width:${pct}%;"></div>
            </div>
          </div>

          <div class="work-job-cost-col">
            <span class="work-job-cost-val">${fmtMoney(j.cost)}</span>
            <span class="work-job-age">${fmtAgo(j.createdAt)}</span>
          </div>

          <div class="work-job-actions" onclick="event.stopPropagation()">
            <button type="button" class="btn sm" onclick="openJobDrawer(${J(j.id)})">Inspect</button>
            ${j.status === 'running' ? `<button type="button" class="btn sm bad" onclick="workCancelJob(${J(j.id)})">Cancel</button>` : ""}
            ${j.status === 'failed' ? `<button type="button" class="btn sm pri" onclick="workRetryJob(${J(j.id)})">Retry</button>` : ""}
          </div>
        </div>`;
      }).join("");
    }

    return `<div class="work-page">
      <div class="work-head">
        <div class="work-head-main">
          <h2 class="work-title">Jobs Cockpit</h2>
          <p class="work-subtitle">Unified view of all active, gated, and completed workflow executions</p>
        </div>
        <div class="work-head-actions">
          <button type="button" class="btn" onclick="render()">Refresh</button>
        </div>
      </div>

      <div class="work-toolbar">
        <div class="seg" role="tablist" aria-label="Status filter">
          <button type="button" class="seg-item ${filterStatus === 'all' ? 'on' : ''}" onclick="window.work_status_filter='all';render()">All (${countAll})</button>
          <button type="button" class="seg-item ${filterStatus === 'running' ? 'on' : ''}" onclick="window.work_status_filter='running';render()">Running (${countRunning})</button>
          <button type="button" class="seg-item ${filterStatus === 'needs_owner' ? 'on' : ''}" onclick="window.work_status_filter='needs_owner';render()">Needs owner (${countNeedsOwner})</button>
          <button type="button" class="seg-item ${filterStatus === 'failed' ? 'on' : ''}" onclick="window.work_status_filter='failed';render()">Failed (${countFailed})</button>
          <button type="button" class="seg-item ${filterStatus === 'done' ? 'on' : ''}" onclick="window.work_status_filter='done';render()">Done (${countDone})</button>
        </div>

        <select class="work-select" aria-label="Filter by pattern" onchange="window.work_pattern_filter=this.value;render()">
          <option value="">All patterns</option>
          ${patterns.map(p => `<option value="${E(p)}" ${filterPattern === p ? 'selected' : ''}>${E(p)}</option>`).join("")}
        </select>

        <select class="work-select" aria-label="Filter by project" onchange="window.work_project_filter=this.value;render()">
          <option value="">All projects</option>
          ${projects.map(p => `<option value="${E(p.name)}" ${filterProject === p.name ? 'selected' : ''}>${E(p.name)}</option>`).join("")}
        </select>

        <input type="search" class="work-search" placeholder="Search jobs, agents, steps..." value="${E(window.work_search || '')}" oninput="window.work_search=this.value;render()" aria-label="Search jobs">
      </div>

      <div class="work-job-list">
        ${rowsHtml}
      </div>
    </div>`;
  };

  /* ==========================================================================
     PAGE 2: Orchestration
     ========================================================================== */
  V["Orchestration"] = async function () {
    const jobs = await api("jobs");
    if (!jobs || !jobs.length) {
      return `<div class="work-page">
        <div class="work-head">
          <div class="work-head-main">
            <h2 class="work-title">Orchestration</h2>
            <p class="work-subtitle">Parallel execution topology and task allocation</p>
          </div>
        </div>
        <div class="card" style="padding:var(--space-6);">
          ${window.K && window.K.empty ? window.K.empty({
            icon: "Orchestration",
            title: "No active or queued jobs",
            hint: "Submit tasks using the submit_task tool or the CLI."
          }) : '<div class="empty"><b>No active or queued jobs</b></div>'}
        </div>
      </div>`;
    }

    return `<div class="work-page">
      <div class="work-head">
        <div class="work-head-main">
          <h2 class="work-title">Orchestration</h2>
          <p class="work-subtitle">Parallel execution topology, subtask lifecycle and token telemetry</p>
        </div>
        <div class="work-head-actions">
          <button type="button" class="btn" onclick="render()">Refresh</button>
        </div>
      </div>

      <div style="display:flex;flex-direction:column;gap:var(--space-3);">
        ${jobs.map(x => {
          const isExp = !!window.orch_expanded[x.job_id];
          const subtasks = x.subtasks || [];
          const costStr = fmtMoney(x.used ? x.used.cost : 0);
          const tokStr = fmtTokens(x.used ? x.used.tokens : 0);

          return `<div class="card" style="margin:0;padding:var(--space-4);">
            <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:var(--space-3);flex-wrap:wrap;">
              <div style="flex:1;min-width:240px;">
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                  ${statusDotHtml(x.status)}
                  <a href="#" style="font-size:var(--text-sm);font-weight:700;color:var(--fg-default);text-decoration:none;" onclick="openJobDrawer(${J(x.job_id)});return false">
                    ${E(x.title)}
                  </a>
                  ${badgeHtml(x.status)}
                  <span class="badge ${x.pattern === 'sdlc' ? 'accent' : ''}">${E(x.pattern)}</span>
                </div>
                <div style="font-size:var(--text-xs);color:var(--fg-muted);margin-top:4px;">
                  Tokens: ${tokStr} · Spend: ${costStr} ${x.budget && Object.keys(x.budget).length ? " · Budget: " + E(JSON.stringify(x.budget)) : ""}
                </div>
              </div>

              <div style="display:flex;align-items:center;gap:6px;">
                <button type="button" class="btn sm" onclick="openJobDrawer(${J(x.job_id)})">Inspect in drawer</button>
                <button type="button" class="btn sm" onclick="window.orch_expanded[${J(x.job_id)}]=!window.orch_expanded[${J(x.job_id)}];render()">${isExp ? 'Hide subtasks' : 'Subtasks (' + subtasks.length + ')'}</button>
                ${x.status === 'running' ? `<button type="button" class="btn sm bad" onclick="workCancelJob(${J(x.job_id)})">Cancel</button>` : ""}
              </div>
            </div>

            <!-- Lifecycle step pipeline -->
            <div style="margin-top:12px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;background:var(--bg-surface);padding:8px 12px;border-radius:var(--radius-sm);border:1px solid var(--border-subtle);">
              ${subtasks.map((s, i) => `
                <span class="badge ${s.status === 'done' ? 'ok' : s.status === 'running' ? 'accent' : s.status === 'failed' ? 'bad' : ''}" style="cursor:pointer;" onclick="openJobDrawer(${J(x.job_id)})" title="${E(s.title || s.key)}">
                  ${E(s.key)} ${s.agent ? '· ' + E(s.agent) : ''}
                </span>
                ${i < subtasks.length - 1 ? '<span class="mut sm">→</span>' : ''}
              `).join("")}
            </div>

            ${isExp ? `
              <div class="work-scroll-wrap" style="margin-top:12px;">
                <table style="width:100%;font-size:12px;border-collapse:collapse;">
                  <tr style="border-bottom:1px solid var(--border-subtle);"><th style="text-align:left;padding:6px;">Key</th><th style="text-align:left;padding:6px;">Type</th><th style="text-align:left;padding:6px;">Status</th><th style="text-align:left;padding:6px;">Agent</th><th style="text-align:left;padding:6px;">Verdict</th><th style="text-align:left;padding:6px;">Attempts</th></tr>
                  ${subtasks.map(s => `<tr style="border-bottom:1px solid var(--border-subtle);"><td style="padding:6px;"><b>${E(s.key)}</b></td><td style="padding:6px;">${badgeHtml(s.type)}</td><td style="padding:6px;">${badgeHtml(s.status)}</td><td style="padding:6px;">${E(s.agent || 'unassigned')}</td><td style="padding:6px;">${s.verdict ? badgeHtml(s.verdict) : '-'}</td><td style="padding:6px;">${s.attempts || 0}</td></tr>`).join("")}
                </table>
              </div>
            ` : ""}
          </div>`;
        }).join("")}
      </div>
    </div>`;
  };

  /* ==========================================================================
     PAGE 3: Task breakdown
     ========================================================================== */
  V["Task breakdown"] = async function () {
    const jobs = await api("jobs_index");
    const d = await api("breakdown_view?job=" + (window.bj || ""));

    if (!d || !d.job) {
      return `<div class="work-page">
        <div class="work-head">
          <div class="work-head-main">
            <h2 class="work-title">Task Breakdown</h2>
            <p class="work-subtitle">Topological DAG view and step inspection</p>
          </div>
        </div>
        <div class="card" style="padding:var(--space-6);">
          ${window.K && window.K.empty ? window.K.empty({
            icon: "Task breakdown",
            title: "No tasks to break down",
            hint: "Submit a job to inspect its dependency structure."
          }) : '<div class="empty"><b>No tasks found</b></div>'}
        </div>
      </div>`;
    }

    const jobSel = `<div class="card" style="margin:0;padding:var(--space-4);">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:var(--space-3);flex-wrap:wrap;">
        <div style="display:flex;align-items:center;gap:8px;flex:1;min-width:240px;">
          <label style="font-weight:600;font-size:var(--text-xs);">Job:</label>
          <select class="work-select" style="flex:1;max-width:400px;" onchange="window.bj=this.value;window.bn=null;render()">
            ${jobs.map(j => `<option value="${E(j.id)}" ${j.id === d.job.id ? 'selected' : ''}>${E((j.title || '').slice(0, 60))} · ${E(j.status)}</option>`).join("")}
          </select>
          ${badgeHtml(d.job.status)}
        </div>
        <div>
          <button type="button" class="btn pri" onclick="openJobDrawer(${J(d.job.id)})">Inspect in drawer</button>
        </div>
      </div>
      <div style="font-size:var(--text-xs);color:var(--fg-muted);margin-top:8px;">
        Pattern: <code>${E(d.planning ? d.planning.pattern : "")}</code> · Total cost: ${fmtMoney(d.totals ? d.totals.cost : 0)} · Tokens: ${fmtTokens(d.totals ? d.totals.tokens : 0)}
      </div>
      ${d.job.body ? `<details style="margin-top:8px;font-size:var(--text-xs);"><summary class="mut" style="cursor:pointer;">Planning instructions</summary><pre style="white-space:pre-wrap;background:var(--bg-surface);padding:8px;border-radius:4px;margin-top:4px;">${E(d.job.body)}</pre></details>` : ""}
    </div>`;

    if (!d.nodes || !d.nodes.length) {
      return `<div class="work-page">${jobSel}<div class="card" style="padding:var(--space-6);"><p class="mut">This job has no breakdown steps yet.</p></div></div>`;
    }

    // SVG DAG Layout
    const lv = {}, deps = {}, vis = new Set();
    (d.edges || []).forEach(e => { (deps[e.to] = deps[e.to] || []).push(e.from); });
    const calcLevel = k => {
      if (lv[k] !== undefined) return lv[k];
      if (vis.has(k)) return 0;
      vis.add(k);
      const m = (deps[k] || []).reduce((acc, x) => Math.max(acc, calcLevel(x) + 1), 0);
      vis.delete(k);
      lv[k] = m;
      return m;
    };
    d.nodes.forEach(n => calcLevel(n.key));
    const cols = {};
    d.nodes.forEach(n => { (cols[lv[n.key]] = cols[lv[n.key]] || []).push(n); });
    const W = 190, H = 58, pos = {};
    Object.entries(cols).forEach(([c, ns]) => ns.forEach((n, i) => pos[n.key] = [20 + c * (W + 50), 20 + i * (H + 24)]));
    const vals = Object.values(pos);
    const mw = (vals.length ? Math.max(...vals.map(p => p[0])) : 20) + W + 30;
    const mh = (vals.length ? Math.max(...vals.map(p => p[1])) : 20) + H + 30;
    const col = { done: "#4caf50", claimed: "#2196f3", pending: "#9e9e9e", failed: "#f44336", blocked: "#ff9800", cancelled: "#777" };

    const svg = `<svg width="${mw}" height="${mh}" style="max-width:100%">${(d.edges || []).map(e => {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) return "";
      return `<path d="M${a[0] + W},${a[1] + H / 2} C${a[0] + W + 25},${a[1] + H / 2} ${b[0] - 25},${b[1] + H / 2} ${b[0]},${b[1] + H / 2}" fill="none" stroke="${e.kind === 'review' ? '#ff9800' : '#888'}" stroke-dasharray="${e.kind === 'review' ? '4 3' : ''}"/>`;
    }).join("")}
    ${d.nodes.map(n => {
      const p = pos[n.key];
      return `<g style="cursor:pointer" onclick="window.bn=${J(n.key)};render()">
        <rect x="${p[0]}" y="${p[1]}" width="${W}" height="${H}" rx="8" fill="${col[n.status] || '#999'}22" stroke="${col[n.status] || '#999'}" stroke-width="${window.bn === n.key ? 3 : 1.2}"/>
        <text x="${p[0] + 8}" y="${p[1] + 18}" font-size="12" font-weight="bold" fill="currentColor">${E(n.key)} · ${E(n.type)}</text>
        <text x="${p[0] + 8}" y="${p[1] + 34}" font-size="11" fill="currentColor">${E((n.agent || "unassigned").slice(0, 14))} ${n.model ? "· " + E(n.model.split("/").pop().slice(0, 16)) : ""}</text>
        <text x="${p[0] + 8}" y="${p[1] + 49}" font-size="10" fill="currentColor" opacity=".7">${E(n.status)} · ${E(n.effort || "-")} · $${n.cost || 0}</text>
      </g>`;
    }).join("")}</svg>`;

    const selectedNode = d.nodes.find(x => x.key === window.bn) || d.nodes[0];

    return `<div class="work-page">
      <div class="work-head">
        <div class="work-head-main">
          <h2 class="work-title">Task Breakdown</h2>
          <p class="work-subtitle">Topological graph, event timeline and node inspector</p>
        </div>
      </div>

      ${jobSel}

      <div class="card" style="margin:0;padding:var(--space-4);">
        <div style="font-size:12px;font-weight:600;margin-bottom:8px;">Dependency Graph (DAG)</div>
        <div class="work-scroll-wrap">${svg}</div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(320px, 1fr));gap:var(--space-4);">
        <div class="card" style="margin:0;padding:var(--space-4);">
          <div style="font-size:12px;font-weight:600;margin-bottom:8px;">Execution Timeline</div>
          <ul class="feed" style="margin:0;padding:0;list-style:none;">
            ${(d.timeline || []).map(e => `
              <li style="padding:6px 0;border-bottom:1px solid var(--border-subtle);font-size:11.5px;">
                <span class="mut sm" style="display:inline-block;width:60px;">${e.at ? new Date(e.at * 1000).toLocaleTimeString() : ""}</span>
                <b>${E(e.part || "")}</b> ${E(e.event)} <b>${E(e.by || "")}</b> <span class="mut">${E((e.detail || "").slice(0, 100))}</span>
              </li>
            `).join("") || '<li class="mut sm">No timeline events yet.</li>'}
          </ul>
        </div>

        <div class="card" style="margin:0;padding:var(--space-4);">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
            <div style="font-size:12px;font-weight:600;">Node: ${E(selectedNode.key)} · ${E(selectedNode.title)}</div>
            <button type="button" class="btn sm" onclick="openJobDrawer(${J(d.job.id)})">Open in drawer</button>
          </div>
          <table style="width:100%;font-size:12px;border-collapse:collapse;">
            <tr><td class="mut" style="padding:4px 0;width:35%;">Status</td><td>${badgeHtml(selectedNode.status)}</td></tr>
            <tr><td class="mut" style="padding:4px 0;">Agent</td><td><b>${E(selectedNode.agent || "unassigned")}</b></td></tr>
            <tr><td class="mut" style="padding:4px 0;">Model</td><td><code>${E(selectedNode.model || "-")}</code></td></tr>
            <tr><td class="mut" style="padding:4px 0;">Effort</td><td>${E(selectedNode.effort || "-")} (diff ${E(selectedNode.difficulty || "-")})</td></tr>
            <tr><td class="mut" style="padding:4px 0;">Rationale</td><td class="mut sm">${E(selectedNode.rationale || "-")}</td></tr>
            <tr><td class="mut" style="padding:4px 0;">Attempts</td><td>${selectedNode.attempts || 0}/${selectedNode.max_attempts || 3}</td></tr>
            <tr><td class="mut" style="padding:4px 0;">Cost</td><td>$${selectedNode.cost || 0}</td></tr>
          </table>

          ${selectedNode.result ? `
            <div style="margin-top:10px;">
              <div style="font-size:11.5px;font-weight:600;margin-bottom:4px;">Output Result</div>
              <pre style="max-height:160px;overflow:auto;background:var(--bg-input);padding:6px;border-radius:4px;font-size:11px;white-space:pre-wrap;">${E(selectedNode.result)}</pre>
            </div>
          ` : ""}
        </div>
      </div>
    </div>`;
  };

  /* ==========================================================================
     PAGE 4: Mission control
     ========================================================================== */
  V["Mission control"] = async function () {
    const d = await api("mission");
    const t = d.totals || {};
    const onlineAgents = (d.agents || []).filter(a => a.state === "online").length;

    return `<div class="work-page">
      <div class="work-head">
        <div class="work-head-main">
          <h2 class="work-title">Mission Control</h2>
          <p class="work-subtitle">Fleet scheduler, fair-share concurrency and agent load</p>
        </div>
        <div class="work-head-actions">
          <button type="button" class="btn bad" onclick="post('sched/preempt',{below:1,requeue:false})">Pause priority &lt; 1</button>
          <button type="button" class="btn" onclick="post('sched/resume',{})">Resume all</button>
        </div>
      </div>

      <!-- Metric Tiles -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(160px, 1fr));gap:var(--space-3);">
        <div class="tile">
          <div class="tile-header"><span class="tile-label">Running jobs</span></div>
          <div class="tile-value">${fmtNum(t.running_jobs)}</div>
        </div>
        <div class="tile">
          <div class="tile-header"><span class="tile-label">Subtasks in flight</span></div>
          <div class="tile-value">${fmtNum(t.inflight)}</div>
        </div>
        <div class="tile">
          <div class="tile-header"><span class="tile-label">Done last hour</span></div>
          <div class="tile-value">${fmtNum(t.done_last_hour)}</div>
        </div>
        <div class="tile">
          <div class="tile-header"><span class="tile-label">Agents online</span></div>
          <div class="tile-value">${onlineAgents}/${(d.agents || []).length}</div>
        </div>
      </div>

      <!-- Projects Section -->
      <div class="card" style="margin:0;padding:var(--space-4);">
        <div style="font-size:var(--text-sm);font-weight:700;margin-bottom:var(--space-3);">Projects &amp; Scheduling</div>
        ${(d.projects && d.projects.length) ? d.projects.map(p => `
          <div style="margin-bottom:var(--space-4);padding-bottom:var(--space-3);border-bottom:1px solid var(--border-subtle);">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
              <div style="display:flex;align-items:center;gap:8px;">
                <b style="font-size:13.5px;">${E(p.project || "(shared)")}</b>
                ${p.paused ? `<span class="badge warn">paused</span>` : ""}
                <span class="mut sm">prio ${p.priority} · inflight ${p.inflight}${p.max_concurrent ? "/" + p.max_concurrent : ""} · queued ${p.queued} · ${p.throughput_per_h}/h · $${p.cost}</span>
              </div>
              <div style="display:flex;gap:4px;">
                <button type="button" class="btn sm" onclick="post('sched/project',{project:${J(p.project)},priority:${(p.priority || 0) + 1}})">▲</button>
                <button type="button" class="btn sm" onclick="post('sched/project',{project:${J(p.project)},priority:${(p.priority || 0) - 1}})">▼</button>
                <button type="button" class="btn sm" onclick="post('sched/project',{project:${J(p.project)},paused:${!p.paused}})">${p.paused ? "unpause" : "pause"}</button>
              </div>
            </div>

            <div class="work-scroll-wrap">
              <table style="width:100%;font-size:12px;border-collapse:collapse;">
                ${(p.jobs || []).map(j => {
                  const pct = Math.round((j.progress || 0) * 100);
                  return `<tr>
                    <td style="padding:6px;"><a href="#" onclick="openJobDrawer(${J(j.id)});return false" style="font-weight:600;color:var(--fg-default);">${E(j.title.slice(0, 60))}</a></td>
                    <td style="padding:6px;">${badgeHtml(j.status)}</td>
                    <td class="mut sm" style="padding:6px;">p${j.priority || 0}</td>
                    <td style="padding:6px;width:25%;">
                      <div class="work-progress-track"><div class="work-progress-bar" style="width:${pct}%;"></div></div>
                    </td>
                    <td style="padding:6px;text-align:right;">
                      <button type="button" class="btn sm" onclick="openJobDrawer(${J(j.id)})">Inspect</button>
                    </td>
                  </tr>`;
                }).join("")}
              </table>
            </div>
          </div>
        `).join("") : '<p class="mut sm">No active projects running.</p>'}
      </div>

      <!-- Agents Load vs Capacity -->
      <div class="card" style="margin:0;padding:var(--space-4);">
        <div style="font-size:var(--text-sm);font-weight:700;margin-bottom:var(--space-3);">Agents · Load vs Capacity</div>
        <div class="work-scroll-wrap">
          <table style="width:100%;font-size:12px;border-collapse:collapse;">
            <tr style="border-bottom:1px solid var(--border-subtle);"><th style="text-align:left;padding:6px;">Agent</th><th style="text-align:left;padding:6px;">Status</th><th style="text-align:left;padding:6px;width:30%;">Load / Capacity</th><th style="text-align:left;padding:6px;">Currently working on</th></tr>
            ${(d.agents || []).map(a => {
              const never = a.seen === false;
              const loadPct = Math.min(100, Math.round((a.load || 0) / Math.max(1, a.capacity || 1) * 100));
              return `<tr>
                <td style="padding:6px;"><b>${E(a.agent)}</b></td>
                <td style="padding:6px;">${never ? badgeHtml("offline") : badgeHtml(a.state)}</td>
                <td style="padding:6px;">
                  <div style="display:flex;align-items:center;gap:8px;">
                    <div class="work-progress-track" style="flex:1;"><div class="work-progress-bar" style="width:${loadPct}%;"></div></div>
                    <span class="mut sm">${a.load}/${a.capacity}</span>
                  </div>
                </td>
                <td class="mut sm" style="padding:6px;">${(a.working_on || []).map(w => E(w.title.slice(0, 40))).join(", ") || "idle"}</td>
              </tr>`;
            }).join("")}
          </table>
        </div>
      </div>
    </div>`;
  };

  /* ==========================================================================
     PAGE 5: Changes (Modern Diff Viewer)
     ========================================================================== */
  function parseUnifiedDiff(rawDiff) {
    if (!rawDiff) return [];
    const files = [];
    const lines = rawDiff.split("\n");
    let curFile = null;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (line.startsWith("diff --git ")) {
        const parts = line.split(" ");
        let name = parts[3] ? parts[3].replace(/^b\//, "") : (parts[2] ? parts[2].replace(/^a\//, "") : "modified-file");
        curFile = { name: name, lines: [], addCount: 0, delCount: 0 };
        files.push(curFile);
      } else if (!curFile && (line.startsWith("--- a/") || line.startsWith("+++ b/"))) {
        const name = line.replace(/^(--- a\/|\+\+\+ b\/)/, "");
        curFile = { name: name, lines: [], addCount: 0, delCount: 0 };
        files.push(curFile);
      } else if (curFile) {
        if (!rawDiff.includes("diff --git ") && line.startsWith("--- a/") && curFile.lines.length > 0) {
          const name = line.replace(/^--- a\//, "");
          curFile = { name: name, lines: [], addCount: 0, delCount: 0 };
          files.push(curFile);
        } else {
          curFile.lines.push(line);
          if (line.startsWith("+") && !line.startsWith("+++")) curFile.addCount++;
          else if (line.startsWith("-") && !line.startsWith("---")) curFile.delCount++;
        }
      } else {
        curFile = { name: "changes.diff", lines: [line], addCount: 0, delCount: 0 };
        files.push(curFile);
      }
    }
    return files.filter(f => f.lines.length > 0);
  }

  V["Changes"] = async function () {
    const d = await api("changes_view?agent=" + encodeURIComponent(window.cagent || ""));
    const changes = d.changes || [];
    const agents = d.agents || [];
    const filterJob = window.cjob || "";

    const filteredChanges = changes.filter(c => {
      if (filterJob && (!c.path || !c.path.includes(filterJob))) return false;
      if (window.csearch) {
        const q = window.csearch.toLowerCase();
        const str = `${c.by} ${c.path} ${c.repo} ${c.commit} ${(c.files || []).join(" ")}`.toLowerCase();
        if (!str.includes(q)) return false;
      }
      return true;
    });

    return `<div class="work-page">
      <div class="work-head">
        <div class="work-head-main">
          <h2 class="work-title">Changes</h2>
          <p class="work-subtitle">Unified diffs and tracked repository modifications across job steps</p>
        </div>
        <div class="work-head-actions">
          <button type="button" class="btn" onclick="render()">Refresh</button>
        </div>
      </div>

      <div class="work-toolbar">
        <select class="work-select" aria-label="Filter by agent" onchange="window.cagent=this.value;render()">
          <option value="">Every agent</option>
          ${agents.map(a => `<option value="${E(a)}" ${window.cagent === a ? 'selected' : ''}>${E(a)}</option>`).join("")}
        </select>

        <input type="search" class="work-search" placeholder="Search files, commits, paths..." value="${E(window.csearch || '')}" oninput="window.csearch=this.value;render()" aria-label="Search changes">

        ${filterJob ? `
          <button type="button" class="chip" onclick="window.cjob='';render()">
            Job: ${E(filterJob.slice(0, 10))} <span class="chip-remove">&times;</span>
          </button>
        ` : ""}
      </div>

      ${filteredChanges.length ? filteredChanges.map((c, ci) => {
        const jobIdMatch = (c.path || "").match(/\/jobs\/([^\/]+)/);
        const jid = jobIdMatch ? jobIdMatch[1] : null;
        const diffFiles = parseUnifiedDiff(c.diff);

        return `<div class="card" style="margin:0;padding:var(--space-4);">
          <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;flex-wrap:wrap;">
            <div>
              <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                <b>${E(c.by || "agent")}</b>
                <span class="mut sm">· ${fmtAgo(c.at)} ·</span>
                <code style="font-size:11px;">${E(c.path)}</code>
                ${c.commit ? `<span class="badge info">commit ${E(c.commit.slice(0, 8))}</span>` : ""}
                ${c.verified ? badgeHtml(c.verified) : ""}
              </div>
              ${c.repo ? `<div style="font-size:11px;color:var(--fg-muted);margin-top:2px;">Repo: <code>${E(c.repo)}${c.branch ? '@' + E(c.branch) : ''}</code></div>` : ""}
            </div>

            ${jid ? `<div><button type="button" class="btn sm" onclick="openJobDrawer(${J(jid)})">Open job drawer</button></div>` : ""}
          </div>

          <!-- Files summary -->
          ${(c.files || []).length ? `
            <div style="display:flex;gap:4px;flex-wrap:wrap;margin:10px 0 6px 0;">
              ${c.files.map(f => `<span class="badge" style="font-family:var(--font-mono);font-size:10.5px;">${E(f)}</span>`).join("")}
            </div>
          ` : ""}

          <!-- Multi-file Diff Viewer -->
          ${diffFiles.length ? diffFiles.map((df, dfi) => {
            const expKey = `${ci}_${dfi}`;
            const isCollapsed = window.cdiff_expanded[expKey] === false;

            return `<div class="diff-file-card" style="margin-top:8px;">
              <div class="diff-file-head" onclick="window.cdiff_expanded[${J(expKey)}]=!window.cdiff_expanded[${J(expKey)}];render()">
                <div style="display:flex;align-items:center;gap:6px;">
                  <span style="font-size:11px;color:var(--fg-muted);">${isCollapsed ? '▸' : '▾'}</span>
                  <span class="diff-file-path">${E(df.name)}</span>
                </div>
                <div class="diff-stats">
                  <span class="diff-add">+${df.addCount}</span>
                  <span class="diff-del">-${df.delCount}</span>
                </div>
              </div>

              ${!isCollapsed ? `
                <pre class="diff-code">${df.lines.map(line => {
                  let cls = "";
                  if (line.startsWith("@@")) cls = "hunk";
                  else if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
                  else if (line.startsWith("-") && !line.startsWith("---")) cls = "del";
                  return `<div class="diff-line ${cls}">${E(line) || " "}</div>`;
                }).join("")}</pre>
              ` : ""}
            </div>`;
          }).join("") : (c.stat ? `<pre style="font-size:11px;background:var(--bg-input);padding:8px;border-radius:4px;">${E(c.stat)}</pre>` : "")}
        </div>`;
      }).join("") : `
        <div class="card" style="padding:var(--space-6);">
          ${window.K && window.K.empty ? window.K.empty({
            icon: "Changes",
            title: "No recorded changes yet",
            hint: "Agents report git commits and file diffs as they complete subtasks."
          }) : '<div class="empty"><b>No recorded changes yet</b></div>'}
        </div>
      `}
    </div>`;
  };

  /* ==========================================================================
     PAGE 6: Sessions
     ========================================================================== */
  V["Sessions"] = async function () {
    const [sessions, leftovers] = await Promise.all([
      api("sessions").catch(() => []),
      api("leftovers").catch(() => [])
    ]);

    return `<div class="work-page">
      <div class="work-head">
        <div class="work-head-main">
          <h2 class="work-title">Sessions &amp; Leftovers</h2>
          <p class="work-subtitle">Track agent sessions, open leftovers and deferred items</p>
        </div>
        <div class="work-head-actions">
          <button type="button" class="btn" onclick="render()">Refresh</button>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(160px, 1fr));gap:var(--space-3);">
        <div class="tile">
          <div class="tile-header"><span class="tile-label">Leftovers queue</span></div>
          <div class="tile-value">${fmtNum(leftovers.length)}</div>
        </div>
        <div class="tile">
          <div class="tile-header"><span class="tile-label">Active sessions</span></div>
          <div class="tile-value">${fmtNum((sessions || []).filter(s => s.status !== "done").length)}</div>
        </div>
        <div class="tile">
          <div class="tile-header"><span class="tile-label">Total sessions</span></div>
          <div class="tile-value">${fmtNum((sessions || []).length)}</div>
        </div>
      </div>

      <!-- Leftovers Queue -->
      <div class="card" style="margin:0;padding:var(--space-4);">
        <div style="font-size:var(--text-sm);font-weight:700;margin-bottom:var(--space-3);">
          Leftovers Queue (${leftovers.length})
        </div>
        ${leftovers.length ? `
          <div class="work-scroll-wrap">
            <table style="width:100%;font-size:12px;border-collapse:collapse;">
              <tr style="border-bottom:1px solid var(--border-subtle);"><th style="text-align:left;padding:6px;">Type &amp; Priority</th><th style="text-align:left;padding:6px;">Item</th><th style="text-align:left;padding:6px;">Created</th><th style="text-align:right;padding:6px;">Action</th></tr>
              ${leftovers.map(x => `
                <tr style="border-bottom:1px solid var(--border-subtle);">
                  <td style="padding:6px;">
                    <span class="badge info">${E(x.task_type || "task")}</span>
                    <span class="badge warn">p${x.priority}</span>
                  </td>
                  <td style="padding:6px;">${E(x.text)}</td>
                  <td class="mut sm" style="padding:6px;">${fmtAgo(x.created_at)}</td>
                  <td style="padding:6px;text-align:right;">
                    <button type="button" class="btn sm" onclick="post('leftover/resolve',{id:${J(x.id)}})">Done</button>
                  </td>
                </tr>
              `).join("")}
            </table>
          </div>
        ` : '<p class="mut sm">No pending leftovers in the queue.</p>'}
      </div>

      <!-- Sessions List -->
      <div class="card" style="margin:0;padding:var(--space-4);">
        <div style="font-size:var(--text-sm);font-weight:700;margin-bottom:var(--space-3);">
          Agent Sessions
        </div>
        ${(sessions && sessions.length) ? `
          <div class="work-scroll-wrap">
            <table style="width:100%;font-size:12px;border-collapse:collapse;">
              <tr style="border-bottom:1px solid var(--border-subtle);"><th style="text-align:left;padding:6px;">Goal</th><th style="text-align:left;padding:6px;">Status</th><th style="text-align:left;padding:6px;">Agent</th><th style="text-align:left;padding:6px;">Project</th><th style="text-align:left;padding:6px;">Updated</th><th style="text-align:left;padding:6px;">Leftovers</th></tr>
              ${sessions.map(x => `
                <tr style="border-bottom:1px solid var(--border-subtle);">
                  <td style="padding:6px;font-weight:600;">${E(x.goal)}</td>
                  <td style="padding:6px;">${badgeHtml(x.status)}</td>
                  <td style="padding:6px;">${E(x.agent || "-")}</td>
                  <td style="padding:6px;">${x.project ? `<span class="badge">${E(x.project)}</span>` : "-"}</td>
                  <td class="mut sm" style="padding:6px;">${fmtAgo(x.updated_at)}</td>
                  <td class="mut sm" style="padding:6px;">${(x.leftovers || []).map(y => E(y.text) + " (" + badgeHtml(y.status) + ")").join("<br>") || "-"}</td>
                </tr>
              `).join("")}
            </table>
          </div>
        ` : '<p class="mut sm">No sessions recorded yet.</p>'}
      </div>
    </div>`;
  };

  /* ==========================================================================
     PAGE 7: Routing
     ========================================================================== */
  V["Routing"] = async function () {
    const d = await api("routing_view");
    const lv = ["minimal", "low", "medium", "high", "max"];
    const tiers = ["cheap", "standard", "top", "coding"];

    const by = {};
    (d.decisions || []).forEach(x => {
      const k = (x.model || "?") + " · " + (x.level || "?");
      const b = by[k] = by[k] || { n: 0, ok: 0, cost: 0, q: 0, qn: 0 };
      b.n++;
      if (x.outcome === "success" || x.outcome === "approved") b.ok++;
      b.cost += x.cost || x.est_cost || 0;
      if (x.quality != null) { b.q += x.quality; b.qn++; }
    });

    return `<div class="work-page">
      <div class="work-head">
        <div class="work-head-main">
          <h2 class="work-title">Routing Policy</h2>
          <p class="work-subtitle">Automated tier and model selection with inline policy adjustments</p>
        </div>
        <div class="work-head-actions">
          <div class="seg" role="tablist" aria-label="Routing mode">
            ${["quality-first", "balanced", "cost-first"].map(m => `
              <button type="button" class="seg-item ${d.mode === m ? 'on' : ''}" onclick="post('routing/mode',{mode:'${m}'})">${m}</button>
            `).join("")}
          </div>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(320px, 1fr));gap:var(--space-4);">
        <!-- Policy Table (Editable Inline) -->
        <div class="card" style="margin:0;padding:var(--space-4);">
          <div style="font-size:var(--text-sm);font-weight:700;margin-bottom:var(--space-3);">Task Policy (Editable)</div>
          <div class="work-scroll-wrap">
            <table style="width:100%;font-size:12px;border-collapse:collapse;">
              <tr style="border-bottom:1px solid var(--border-subtle);"><th style="text-align:left;padding:6px;">Task type</th><th style="text-align:left;padding:6px;">Effort</th><th style="text-align:left;padding:6px;">Tier</th></tr>
              ${Object.entries(d.policy || {}).map(([t, p]) => `
                <tr style="border-bottom:1px solid var(--border-subtle);">
                  <td style="padding:6px;"><b>${E(t)}</b></td>
                  <td style="padding:6px;">
                    <select class="work-select" onchange="post('routing/policy',{task_type:${J(t)},effort:this.value})">
                      ${lv.map(l => `<option ${p.effort === l ? 'selected' : ''}>${l}</option>`).join("")}
                    </select>
                  </td>
                  <td style="padding:6px;">
                    <select class="work-select" onchange="post('routing/policy',{task_type:${J(t)},tier:this.value})">
                      ${tiers.map(l => `<option ${p.tier === l ? 'selected' : ''}>${l}</option>`).join("")}
                    </select>
                  </td>
                </tr>
              `).join("")}
            </table>
          </div>
        </div>

        <!-- Cost & Quality per Tier -->
        <div class="card" style="margin:0;padding:var(--space-4);">
          <div style="font-size:var(--text-sm);font-weight:700;margin-bottom:var(--space-3);">Cost &amp; Quality per Model/Effort</div>
          <div class="work-scroll-wrap">
            <table style="width:100%;font-size:12px;border-collapse:collapse;">
              <tr style="border-bottom:1px solid var(--border-subtle);"><th style="text-align:left;padding:6px;">Model · Effort</th><th style="text-align:left;padding:6px;">Tasks</th><th style="text-align:left;padding:6px;">Success</th><th style="text-align:left;padding:6px;">Avg quality</th><th style="text-align:left;padding:6px;">Cost</th></tr>
              ${Object.entries(by).map(([k, b]) => `
                <tr style="border-bottom:1px solid var(--border-subtle);">
                  <td style="padding:6px;"><code>${E(k)}</code></td>
                  <td style="padding:6px;">${b.n}</td>
                  <td style="padding:6px;">${Math.round(b.ok / b.n * 100)}%</td>
                  <td style="padding:6px;">${b.qn ? (b.q / b.qn).toFixed(2) : "-"}</td>
                  <td style="padding:6px;">${fmtMoney(b.cost)}</td>
                </tr>
              `).join("") || '<tr><td colspan="5" class="mut sm" style="padding:6px;">No routing decisions recorded yet.</td></tr>'}
            </table>
          </div>
        </div>
      </div>

      <!-- Recent Routing Assignments -->
      <div class="card" style="margin:0;padding:var(--space-4);">
        <div style="font-size:var(--text-sm);font-weight:700;margin-bottom:var(--space-3);">Recent Task Assignments</div>
        <div class="work-scroll-wrap">
          <table style="width:100%;font-size:12px;border-collapse:collapse;">
            <tr style="border-bottom:1px solid var(--border-subtle);"><th style="text-align:left;padding:6px;">Task</th><th style="text-align:left;padding:6px;">Agent</th><th style="text-align:left;padding:6px;">Model</th><th style="text-align:left;padding:6px;">Effort</th><th style="text-align:left;padding:6px;">Difficulty</th><th style="text-align:left;padding:6px;">Why</th><th style="text-align:left;padding:6px;">Outcome</th><th style="text-align:left;padding:6px;">Cost</th></tr>
            ${(d.decisions || []).map(x => `
              <tr style="border-bottom:1px solid var(--border-subtle);">
                <td style="padding:6px;"><b>${E(x.task_type)}</b> <span class="mut sm">${E((x.subtask_id || "").slice(-8))}</span></td>
                <td style="padding:6px;">${E(x.agent || x.agent_id || "")}</td>
                <td style="padding:6px;"><code>${E(x.model || "-")}</code></td>
                <td style="padding:6px;">${badgeHtml(x.level || "")}</td>
                <td style="padding:6px;">${E(x.difficulty || "-")}</td>
                <td class="mut sm" style="padding:6px;">${E((x.rationale || "").slice(0, 100))}</td>
                <td style="padding:6px;">${badgeHtml(x.outcome || "open")}</td>
                <td style="padding:6px;">${fmtMoney(x.cost || x.est_cost || 0)}</td>
              </tr>
            `).join("") || '<tr><td colspan="8" class="mut sm" style="padding:6px;">No recent assignments.</td></tr>'}
          </table>
        </div>
      </div>
    </div>`;
  };

  /* ==========================================================================
     PAGE 8: Drive
     ========================================================================== */
  V["Drive"] = async function () {
    const P = window.dp || "";
    const path = window.dpath || "/";
    const d = await api(`drive_ls?project=${encodeURIComponent(P)}&path=${encodeURIComponent(path)}`);

    let pv = "";
    if (window.df) {
      try {
        const f = await api(`drive_file?project=${encodeURIComponent(P)}&path=${encodeURIComponent(window.df)}`);
        const raw = `/drive/raw?project=${encodeURIComponent(P)}&path=${encodeURIComponent(window.df)}`;

        pv = `<div class="card" style="margin-top:var(--space-4);padding:var(--space-4);">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
            <div>
              <b style="font-size:13.5px;color:var(--fg-default);">${E(window.df)}</b>
              <div class="mut sm">v${f.version || "-"} · ${f.size || 0} B · ${E(f.mime || "")} ${f.locked_by ? "· locked by " + E(f.locked_by) : ""}</div>
            </div>
            <div style="display:flex;align-items:center;gap:6px;">
              <a href="${raw}" download class="btn sm">Download</a>
              ${f.history ? `<button type="button" class="btn sm bad" onclick="post('drive/delete',{project:${J(P)},path:${J(window.df)}});window.df=null">Delete</button>` : ""}
            </div>
          </div>

          <div style="margin-top:12px;">
            ${f.binary ? ((f.mime || "").startsWith("image/") ? `<img src="${raw}" style="max-width:100%;border-radius:var(--radius-md);">` : (f.mime === "application/pdf" ? `<iframe src="${raw}" style="width:100%;height:500px;border:0;border-radius:var(--radius-md);"></iframe>` : `<p class="mut">Binary file preview not available.</p>`)) : `
              <textarea id="dtext" style="width:100%;height:300px;font-family:var(--font-mono);font-size:12px;background:var(--bg-input);color:var(--fg-default);border:1px solid var(--border-default);border-radius:var(--radius-sm);padding:8px;box-sizing:border-box;">${E(f.text || "")}</textarea>
              <div style="margin-top:8px;">
                <button type="button" class="btn pri" onclick="post('drive/upload',{project:${J(P)},path:${J(window.df)},text:$('#dtext').value,expected_version:${f.version || 0}})">Save new version</button>
              </div>
            `}
          </div>

          ${(f.history && f.history.length) ? `
            <div style="margin-top:16px;">
              <div style="font-size:12px;font-weight:700;margin-bottom:6px;">Version History</div>
              <div class="work-scroll-wrap">
                <table style="width:100%;font-size:11.5px;border-collapse:collapse;">
                  <tr style="border-bottom:1px solid var(--border-subtle);"><th style="text-align:left;padding:4px 6px;">Version</th><th style="text-align:left;padding:4px 6px;">Author</th><th style="text-align:left;padding:4px 6px;">Message</th><th style="text-align:left;padding:4px 6px;">Date</th><th style="text-align:right;padding:4px 6px;">Action</th></tr>
                  ${f.history.map(h => `<tr><td style="padding:4px 6px;">v${h.version}</td><td style="padding:4px 6px;">${E(h.author || "")}</td><td class="mut" style="padding:4px 6px;">${E(h.message || "")}</td><td class="mut" style="padding:4px 6px;">${new Date(h.at * 1000).toLocaleString()}</td><td style="padding:4px 6px;text-align:right;"><button type="button" class="btn sm" onclick="post('drive/restore',{project:${J(P)},path:${J(window.df)},version:${h.version}})">Restore</button></td></tr>`).join("")}
                </table>
              </div>
            </div>
          ` : ""}
        </div>`;
      } catch (e) {
        pv = `<div class="card" style="padding:12px;"><p class="mut">Could not load preview: ${E(String(e.message || e))}</p></div>`;
      }
    }

    // Build interactive Breadcrumbs
    const pathParts = path.split("/").filter(Boolean);
    const crumbs = [`<span class="drive-crumb-item" onclick="window.dpath='/';window.df=null;render()">/</span>`];
    let accum = "";
    pathParts.forEach((pt, i) => {
      accum += "/" + pt;
      const curAccum = accum;
      crumbs.push(`<span class="drive-crumb-sep">/</span>`);
      crumbs.push(`<span class="drive-crumb-item" onclick="window.dpath=${J(curAccum)};window.df=null;render()">${E(pt)}</span>`);
    });

    return `<div class="work-page">
      <div class="work-head">
        <div class="work-head-main">
          <h2 class="work-title">Commons Drive</h2>
          <p class="work-subtitle">Shared workspace files, job artifacts and versioned storage</p>
        </div>
        <div class="work-head-actions">
          <select class="work-select" aria-label="Select project" onchange="window.dp=this.value;window.dpath='/';window.df=null;render()">
            ${(d.projects || []).map(p => `<option value="${E(p)}" ${p === P ? 'selected' : ''}>${E(p || "(shared)")}</option>`).join("")}
          </select>
        </div>
      </div>

      <!-- Breadcrumbs -->
      <div class="drive-crumbs">
        <span>Path:</span> ${crumbs.join(" ")}
      </div>

      <!-- Dropzone Upload -->
      <div class="drive-dropzone-box" id="drive-dropzone" tabindex="0" role="button" aria-label="Upload files" onclick="$('#dup').click()">
        <span class="drive-dropzone-icon">⇪</span>
        <div class="drive-dropzone-title">Upload file to ${E(path)}</div>
        <div class="drive-dropzone-hint">Drag and drop or click to browse</div>
        <input type="file" id="dup" style="display:none" onchange="(async()=>{
          const f=this.files[0];
          if(!f)return;
          const b=await f.arrayBuffer();
          let s='';
          new Uint8Array(b).forEach(c=>s+=String.fromCharCode(c));
          const targetPath = (window.dpath || '/').replace(/\\/$/,'') + '/' + f.name;
          await postRaw('drive/upload',{project:${J(P)},path:targetPath,b64:btoa(s)});
          if (window.K && window.K.toast) window.K.toast('Uploaded ' + f.name, { kind: 'ok' });
          render();
        })()">
      </div>

      <!-- File Browser -->
      <div class="card" style="margin:0;padding:var(--space-4);">
        <div class="work-scroll-wrap">
          <table style="width:100%;font-size:12px;border-collapse:collapse;">
            ${path !== "/" ? `
              <tr style="border-bottom:1px solid var(--border-subtle);">
                <td colspan="4" style="padding:6px;">
                  <svg class="wico" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/></svg> <a href="#" style="font-weight:600;color:var(--fg-default);" onclick="window.dpath=${J(path.split('/').slice(0, -1).join('/') || '/')};render();return false">.. (parent directory)</a>
                </td>
              </tr>
            ` : ""}
            ${(d.list.dirs || []).map(x => `
              <tr style="border-bottom:1px solid var(--border-subtle);">
                <td style="padding:6px;"><svg class="wico" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/></svg> <a href="#" style="font-weight:600;color:var(--fg-default);" onclick="window.dpath=${J(x)};window.df=null;render();return false">${E(x.split('/').pop())}</a></td>
                <td class="mut sm" style="padding:6px;">folder</td>
                <td class="mut sm" style="padding:6px;">-</td>
                <td class="mut sm" style="padding:6px;">-</td>
              </tr>
            `).join("")}
            ${(d.list.mounts || []).map(x => `
              <tr style="border-bottom:1px solid var(--border-subtle);">
                <td style="padding:6px;"><svg class="wico" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3.5" y="13" width="17" height="6.5" rx="2"/><path d="M5.5 13l2.5-7.5h8L18.5 13M7.5 16.2h.01"/></svg> <a href="#" style="font-weight:600;color:var(--fg-default);" onclick="window.dpath=${J(x)};window.df=null;render();return false">${E(x)}</a></td>
                <td class="mut sm" style="padding:6px;">mount</td>
                <td class="mut sm" style="padding:6px;">-</td>
                <td class="mut sm" style="padding:6px;">-</td>
              </tr>
            `).join("")}
            ${(d.list.files || []).map(f => `
              <tr style="border-bottom:1px solid var(--border-subtle);">
                <td style="padding:6px;">${f.dir ? '<svg class="wico" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/></svg>' : '<svg class="wico" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6.5 3.5h7l4 4v13h-11z"/><path d="M13.5 3.5v4h4"/></svg>'} <a href="#" style="color:var(--fg-default);font-weight:500;" onclick="${f.dir ? `window.dpath=${J(f.path)}` : `window.df=${J(f.path)}`};render();return false">${E(f.path.split('/').pop())}</a></td>
                <td class="mut sm" style="padding:6px;">${f.version ? 'v' + f.version : '-'}</td>
                <td class="mut sm" style="padding:6px;">${f.size != null ? f.size + ' B' : '-'}</td>
                <td class="mut sm" style="padding:6px;">${E(f.updated_by || '-')}</td>
              </tr>
            `).join("")}
            ${!(d.list.dirs || []).length && !(d.list.mounts || []).length && !(d.list.files || []).length ? `
              <tr style="border-bottom:1px solid var(--border-subtle);">
                <td colspan="4" class="mut sm" style="padding:16px;text-align:center;">This folder is empty. Upload a file above to get started.</td>
              </tr>
            ` : ""}
          </table>
        </div>

        <!-- Drive Usage Summary -->
        <div style="font-size:11px;color:var(--fg-muted);margin-top:12px;padding-top:8px;border-top:1px solid var(--border-subtle);">
          ${(d.usage || []).map(u => `${E(u.project || "shared")}: ${u.files} files, ${(u.bytes / 1e6).toFixed(2)} MB`).join(" · ")}
        </div>
      </div>

      ${pv}
    </div>`;
  };

})();
