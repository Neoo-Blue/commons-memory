"""v0.25 dashboard: 'Workflow' page for six-stage SDLC jobs."""
from __future__ import annotations

ICON = '<rect x="3" y="5" width="4" height="14" rx="1"/><rect x="17" y="5" width="4" height="14" rx="1"/><path d="M7 12h10M13 8l4 4-4 4"/><circle cx="12" cy="12" r="1.5"/>'

JS = r"""
if (typeof T !== "undefined" && !T.includes("Workflow")) {
  T.push("Workflow");
}
if (typeof GROUPS !== "undefined") {
  const _wg = GROUPS.find(g => g[0] === "Work");
  if (_wg && !_wg[1].includes("Workflow")) {
    _wg[1].push("Workflow");
  }
}
if (typeof ICON !== "undefined" && !ICON.Workflow) {
  ICON.Workflow = '<rect x="3" y="5" width="4" height="14" rx="1"/><rect x="17" y="5" width="4" height="14" rx="1"/><path d="M7 12h10M13 8l4 4-4 4"/><circle cx="12" cy="12" r="1.5"/>';
}

window.wf_job = window.wf_job || null;
window.wf_collapsed = window.wf_collapsed || {};

function wfFmtK(n) {
  const v = Number(n) || 0;
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return Math.round(v / 1e3) + "k";
  return String(v);
}

V["Workflow"] = async function() {
  const workflows = await api("workflows");
  if (!workflows || !workflows.length) {
    return `<div class="card empty"><div class="ico">⚡</div><b>No workflows yet</b><div class="mut" style="margin-top:8px">Start one with <code>submit_task(task, pattern="sdlc")</code> or the <code>/sdlc</code> slash command.</div></div>`;
  }

  if (!window.wf_job || !workflows.some(w => w.id === window.wf_job)) {
    window.wf_job = workflows[0].id;
  }

  const data = await api("workflow?job=" + encodeURIComponent(window.wf_job));
  const job = data.job || {};
  const stages = data.stages || [];
  const nodes = data.nodes || [];
  const trace = data.trace || [];
  const savings = data.savings || { handed: 0, naive: 0 };

  window._wf_nodes = nodes;

  const nodeMap = {};
  const childrenMap = {};
  const rootNodes = [];
  nodes.forEach(n => {
    nodeMap[n.id] = n;
  });
  nodes.forEach(n => {
    if (!n.parent || !nodeMap[n.parent]) {
      rootNodes.push(n);
    } else {
      (childrenMap[n.parent] = childrenMap[n.parent] || []).push(n);
    }
  });

  const handed = Number(savings.handed) || 0;
  const naive = Number(savings.naive) || 0;
  const ratio = (naive > 0 && handed > 0) ? (naive / handed).toFixed(1) : null;
  const ratioText = (naive > 0 && ratio) ? (ratio >= 1.1 ? `, ${ratio}x less` : ", no saving yet") : "";
  const meterText = `${wfFmtK(handed)} vs ${wfFmtK(naive)}${ratioText}`;
  const pct = naive > 0 ? Math.min(100, Math.max(2, Math.round((handed / naive) * 100))) : (handed > 0 ? 100 : 0);

  // Trace calculations
  const totalTrace = trace.length;
  const gaps = trace.filter(t => t.tests === "missing").length;
  const covered = totalTrace - gaps;
  const failedTrace = trace.filter(t => t.tests === "fail").length;

  // Stages and Spine
  let spineHtml = "";
  for (let i = 0; i < stages.length; i++) {
    const st = stages[i];
    const stStatus = st.status || "pending";
    const stGates = st.gates || [];
    const reworkBadge = (st.rework && st.rework > 0)
      ? `<span class="pill failed" style="font-weight:700;">↺ ${st.rework} rework</span>`
      : "";
    const tierBadge = st.tier
      ? `<span class="wf-tier wf-tier-${E(st.tier)}">${E(st.tier)}</span>`
      : "";
    const tokCostStr = `${st.tokens ? st.tokens.toLocaleString() + " tok" : "0 tok"} · $${(+st.cost || 0).toFixed(2)}`;

    spineHtml += `<div class="wf-stage-card ${E(stStatus)}">
      <div class="wf-stage-head">
        <span class="wf-stage-title">${E((st.stage || "").toUpperCase())}</span>
        <code class="wf-stage-cmd">${E(st.command || ("/" + st.stage))}</code>
      </div>
      <div class="wf-stage-body">
        <div>${pill(stStatus)} ${reworkBadge}</div>
        <div style="margin-top:2px;"><b>${E(st.agent || "unassigned")}</b> ${st.model ? `<span class="mut sm">(${E(st.model.split("/").pop())})</span>` : ""}</div>
        <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">${tierBadge} ${st.effort ? `<span class="mut sm">${E(st.effort)}</span>` : ""}</div>
        <div class="mut sm" style="margin-top:2px;">${tokCostStr}</div>
      </div>
    </div>`;

    if (i < stages.length - 1 || stGates.length > 0) {
      let gatesHtml = "";
      stGates.forEach(g => {
        const isOwner = g.type === "owner";
        const isWaitingOwner = g.status === "waiting_owner";
        if (isWaitingOwner) {
          gatesHtml += `<a href="#Board" class="wf-gate-badge waiting_owner" onclick="go('Board');return false" title="Action required: click to approve on the Board">👤 ${isOwner ? "you approve" : E(g.title || "Gate")} <span class="pill blocked">needs you</span></a>`;
        } else if (isOwner) {
          if (g.status === "done") {
            gatesHtml += `<span class="wf-gate-badge done">👤 you approve <span class="pill done">${E(g.verdict || "approve")}</span></span>`;
          } else {
            gatesHtml += `<span class="wf-gate-badge pending">👤 you approve <span class="pill pending">${E(g.status || "pending")}</span></span>`;
          }
        } else {
          // Review gate
          if (g.verdict) {
            gatesHtml += `<span class="wf-gate-badge review ${E(g.verdict)}">⚖ ${E(g.title || "Review")}: <span class="pill ${E(g.verdict)}">${E(g.verdict)}</span></span>`;
          } else {
            gatesHtml += `<span class="wf-gate-badge review pending">⚖ ${E(g.title || "Review")}: <span class="pill pending">${E(g.status || "pending")}</span></span>`;
          }
        }
      });

      spineHtml += `<div class="wf-connector">
        <div class="wf-connector-gates">${gatesHtml}</div>
        <span class="wf-conn-arrow">→</span>
      </div>`;
    }
  }

  // Hierarchy Tree
  const treeRows = [];
  function walkTree(node, depth) {
    const hasKids = !!(childrenMap[node.id] && childrenMap[node.id].length);
    const isCollapsed = !!window.wf_collapsed[node.id];
    treeRows.push({ node, depth, hasKids, isCollapsed });
    if (hasKids && !isCollapsed) {
      childrenMap[node.id].forEach(child => walkTree(child, depth + 1));
    }
  }
  rootNodes.forEach(rn => walkTree(rn, 0));

  let hierarchyHtml = "";
  treeRows.forEach(row => {
    const n = row.node;
    const depth = row.depth;
    const hasKids = row.hasKids;
    const isCollapsed = row.isCollapsed;
    const toggleBtn = hasKids
      ? `<button type="button" class="wf-tree-btn" onclick="window.wf_collapsed[${J(n.id)}]=!window.wf_collapsed[${J(n.id)}];render();" aria-label="${isCollapsed ? 'Expand' : 'Collapse'}">${isCollapsed ? '▸' : '▾'}</button>`
      : `<span class="wf-tree-btn wf-spacer" aria-hidden="true">·</span>`;

    const tierBadge = n.tier
      ? `<span class="wf-tier wf-tier-${E(n.tier)}">${E(n.tier)}</span>`
      : "";
    const effortStr = n.effort ? `<span class="mut sm">${E(n.effort)}</span>` : "";
    const tokStr = `${n.tokens ? n.tokens.toLocaleString() + " tok" : "0 tok"}`;
    const costStr = (n.cost != null && n.cost > 0) ? ` · $${(+n.cost).toFixed(2)}` : "";

    const artHtml = (n.artifacts && n.artifacts.length)
      ? n.artifacts.map(a => `<a class="wf-art-link" href="/drive/raw?path=${encodeURIComponent(a.path)}" target="_blank" rel="noopener">📄 ${E(a.path.split("/").pop())} <span class="mut sm">(${E(a.kind)})</span></a>`).join(" ")
      : "";

    hierarchyHtml += `<div class="wf-tree-row" style="--depth:${depth}; padding-left: calc(8px + ${depth} * 18px);">
      ${toggleBtn}
      <div class="wf-tree-content">
        <span class="wf-tree-title">${E(n.title || n.id)}</span>
        ${pill(n.status)}
        ${n.level ? `<span class="pill sm mut">${E(n.level)}</span>` : ""}
        <span class="wf-tree-agent">
          ${n.agent ? `<b>${E(n.agent)}</b>` : '<span class="mut sm">unassigned</span>'}
          ${n.model ? `<code class="sm">${E(n.model.split("/").pop())}</code>` : ""}
        </span>
        ${tierBadge}
        ${effortStr}
        <span class="mut sm">${tokStr}${costStr}</span>
        ${artHtml}
      </div>
    </div>`;
  });

  // Traceability Matrix
  let traceHtml = "";
  if (!trace.length) {
    traceHtml = `<p class="mut" style="margin: 8px 0;">The spec has no numbered criteria yet.</p>`;
  } else {
    traceHtml = `<div style="overflow-x: auto;"><table class="wf-table">
      <thead>
        <tr>
          <th style="width: 70px;">AC</th>
          <th>Criterion</th>
          <th>Linked tasks</th>
          <th style="width: 90px;">Tests</th>
          <th style="width: 100px;">Review</th>
        </tr>
      </thead>
      <tbody>
        ${trace.map(t => {
          let testBadge = "";
          if (t.tests === "pass") {
            testBadge = `<span class="pill done">pass</span>`;
          } else if (t.tests === "fail") {
            testBadge = `<span class="pill failed">fail</span>`;
          } else {
            testBadge = `<span class="wf-gap">missing</span>`;
          }

          const reviewBadge = t.verdict
            ? pill(t.verdict)
            : `<span class="mut sm">pending</span>`;

          const taskLabels = (t.tasks && t.tasks.length)
            ? t.tasks.map(tid => {
                const tn = nodeMap[tid];
                return tn
                  ? `<span class="pill mut" style="font-weight: 500;" title="${E(tid)}">${E(tn.title)}</span>`
                  : `<code>${E(tid)}</code>`;
              }).join(" ")
            : `<span class="mut sm">none</span>`;

          return `<tr>
            <td><b>${E(t.ac)}</b></td>
            <td>${E(t.text)}</td>
            <td>${taskLabels}</td>
            <td>${testBadge}</td>
            <td>${reviewBadge}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table></div>`;
  }

  // Trace summary text
  const traceSummaryBadge = totalTrace > 0
    ? `<span class="pill ${gaps === 0 && failedTrace === 0 ? "done" : gaps > 0 ? "failed" : "running"}">${covered} of ${totalTrace} criteria covered</span>${gaps > 0 ? ` <span class="mut sm">(${gaps} gap${gaps === 1 ? "" : "s"})</span>` : ""}${failedTrace > 0 ? ` <span class="failed sm">· ${failedTrace} failing</span>` : ""}`
    : "";

  return `<style id="wf-style">
.wf-wrap { display: flex; flex-direction: column; gap: 16px; width: 100%; box-sizing: border-box; }
.wf-card { background: var(--card); border: 1px solid var(--line); border-radius: var(--r, 12px); padding: 16px; box-shadow: var(--sh); overflow-x: auto; box-sizing: border-box; }
.wf-picker-bar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; }
.wf-picker-form { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; flex: 1; min-width: 240px; }
.wf-picker-select { min-height: 44px; padding: 6px 12px; font-size: 13.5px; border-radius: 9px; background: var(--card2); color: var(--fg); border: 1px solid var(--line); max-width: 100%; }
.wf-spine { display: flex; align-items: stretch; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.wf-stage-card { flex: 1 1 150px; min-width: 135px; background: var(--card2); border: 1px solid var(--line); border-radius: 10px; padding: 12px; display: flex; flex-direction: column; justify-content: space-between; gap: 8px; box-shadow: var(--sh); box-sizing: border-box; }
.wf-stage-card.done { border-top: 3px solid var(--ok); }
.wf-stage-card.running { border-top: 3px solid var(--warn); }
.wf-stage-card.rework { border-top: 3px solid var(--bad); }
.wf-stage-card.failed { border-top: 3px solid var(--bad); }
.wf-stage-card.pending { border-top: 3px solid var(--line); opacity: 0.85; }
.wf-stage-head { display: flex; align-items: center; justify-content: space-between; gap: 6px; flex-wrap: wrap; }
.wf-stage-title { font-weight: 700; font-size: 13px; letter-spacing: -0.01em; }
.wf-stage-cmd { font: 11px var(--mono); color: var(--acc); background: var(--bg2); padding: 2px 5px; border-radius: 4px; }
.wf-stage-body { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.wf-connector { display: flex; flex-direction: column; align-items: center; justify-content: center; min-width: 80px; padding: 4px; text-align: center; gap: 6px; }
.wf-connector-gates { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.wf-conn-arrow { color: var(--mut); font-size: 15px; font-weight: bold; }
@media (max-width: 768px) {
  .wf-spine { flex-direction: column; }
  .wf-stage-card { width: 100%; min-width: 0; }
  .wf-connector { flex-direction: row; width: 100%; justify-content: center; padding: 8px 0; gap: 8px; }
  .wf-conn-arrow { transform: rotate(90deg); display: inline-block; }
  .wf-connector-gates { flex-direction: row; flex-wrap: wrap; justify-content: center; }
}
.wf-gate-badge { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 8px; font-size: 11.5px; font-weight: 600; text-decoration: none; border: 1px solid var(--line); background: var(--card2); min-height: 44px; color: var(--fg); box-sizing: border-box; }
.wf-gate-badge.waiting_owner { border-color: var(--bad); color: var(--bad); background: rgba(239,107,107,0.1); cursor: pointer; }
.wf-gate-badge:hover { text-decoration: none; border-color: var(--acc); }
.wf-tier { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }
.wf-tier-cheap { background: rgba(76,195,138,0.14); color: var(--ok); border: 1px solid rgba(76,195,138,0.3); }
.wf-tier-mid { background: rgba(64,150,255,0.14); color: #4096ff; border: 1px solid rgba(64,150,255,0.3); }
.wf-tier-top { background: rgba(227,179,65,0.16); color: var(--warn); border: 1px solid rgba(227,179,65,0.35); }
.wf-tree { display: flex; flex-direction: column; width: 100%; }
.wf-tree-row { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-bottom: 1px solid var(--line); min-height: 44px; box-sizing: border-box; transition: background 0.12s; }
.wf-tree-row:hover { background: var(--acc-soft); }
@media (max-width: 600px) {
  .wf-tree-row { padding-left: calc(4px + var(--depth, 0) * 10px) !important; }
}
.wf-tree-content { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; flex: 1; min-width: 0; }
.wf-tree-title { font-weight: 600; font-size: 13px; color: var(--fg); }
.wf-tree-agent { display: inline-flex; align-items: center; gap: 6px; }
.wf-tree-btn { min-width: 44px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; background: transparent; border: 0; color: var(--fg); cursor: pointer; font-size: 14px; padding: 0; }
.wf-spacer { cursor: default; opacity: 0.3; }
.wf-art-link { min-height: 44px; display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 6px; border: 1px solid var(--line); background: var(--card2); color: var(--acc); font-size: 11.5px; text-decoration: none; word-break: break-all; box-sizing: border-box; }
.wf-art-link:hover { border-color: var(--acc); text-decoration: none; }
.wf-table { width: 100%; border-collapse: collapse; }
.wf-table th, .wf-table td { padding: 10px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; }
.wf-table tr:hover td { background: var(--acc-soft); }
.wf-gap { display: inline-block; padding: 2px 7px; border-radius: 5px; border: 1.5px solid var(--bad); color: var(--bad); background: transparent; font-size: 11px; font-weight: 600; }
.wf-meter-box { background: var(--bg2); border: 1px solid var(--line); border-radius: 10px; padding: 14px; margin-top: 12px; }
.wf-meter-bar { height: 12px; border-radius: 99px; background: rgba(255,255,255,0.08); overflow: hidden; margin: 8px 0; }
[data-theme=light] .wf-meter-bar { background: rgba(0,0,0,0.08); }
.wf-meter-fill { height: 100%; border-radius: 99px; background: linear-gradient(90deg, var(--ok), var(--acc)); transition: width 0.3s ease; }
.wf-ctrl-btn { min-height: 44px; padding: 6px 14px; font-weight: 600; font-size: 12.5px; background: var(--card2); color: var(--fg); border: 1px solid var(--line); border-radius: 9px; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; }
.wf-ctrl-btn:hover { border-color: var(--acc); }
</style>
<div class="wf-wrap">
  <div class="wf-card">
    <div class="wf-picker-bar">
      <div class="wf-picker-form">
        <label for="wf-job-select" style="font-weight: 600; font-size: 13.5px;">Workflow:</label>
        <select id="wf-job-select" class="wf-picker-select" onchange="window.wf_job=this.value;render();" aria-label="Select workflow">
          ${workflows.map(w => `<option value="${E(w.id)}" ${w.id === window.wf_job ? "selected" : ""}>${E(w.title || w.id)} (${E(w.stage || w.status)})</option>`).join("")}
        </select>
        ${pill(job.status || "unknown")}
      </div>
      <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
        <span class="mut sm">pattern: <code>${E(job.pattern || "sdlc")}</code></span>
        ${job.updated_at ? `<span class="mut sm">updated ${ago(Math.max(0, Math.round((Date.now() - Date.parse(job.updated_at)) / 1000)))} ago</span>` : ""}
        <button type="button" class="wf-ctrl-btn" onclick="render();">refresh</button>
      </div>
    </div>
  </div>

  <div class="wf-card">
    <h3 style="margin-top:0;">Workflow spine</h3>
    <div class="wf-spine">
      ${spineHtml}
    </div>
  </div>

  <div class="wf-card">
    <h3 style="margin-top:0;">Budget &amp; savings</h3>
    <div class="grid" style="grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 12px;">
      <div class="card stat" style="margin: 0;">
        <div class="mut sm">Total tokens</div>
        <div class="big">${(job.tokens || 0).toLocaleString()}</div>
      </div>
      <div class="card stat" style="margin: 0;">
        <div class="mut sm">Total cost</div>
        <div class="big">$${(+job.cost || 0).toFixed(2)}</div>
      </div>
      <div class="card stat hero" style="margin: 0;">
        <div class="mut sm">Tokens handed vs naive</div>
        <div class="big" style="font-size: 18px; margin-top: 4px;">${meterText}</div>
      </div>
    </div>
    <div class="wf-meter-box">
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px; font-size:12.5px;">
        <span>Tokens handed to agents vs naive (every agent given all upstream output)</span>
        <b>${meterText}</b>
      </div>
      <div class="wf-meter-bar">
        <div class="wf-meter-fill" style="width: ${pct}%;"></div>
      </div>
      <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--mut);">
        <span>Handed: ${wfFmtK(handed)} (${pct}%)</span>
        <span>Naive: ${wfFmtK(naive)}</span>
      </div>
    </div>
  </div>

  <div class="wf-card">
    <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; margin-bottom:10px;">
      <h3 style="margin:0;">Agent hierarchy</h3>
      <div style="display:flex; gap:6px;">
        <button type="button" class="wf-ctrl-btn" onclick="window.wf_collapsed={};render();">expand all</button>
        <button type="button" class="wf-ctrl-btn" onclick="const c={};(window._wf_nodes||[]).forEach(n=>{c[n.id]=true;});window.wf_collapsed=c;render();">collapse all</button>
      </div>
    </div>
    <div class="wf-tree">
      ${hierarchyHtml}
    </div>
  </div>

  <div class="wf-card">
    <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; margin-bottom:10px;">
      <h3 style="margin:0;">Traceability matrix</h3>
      ${traceSummaryBadge}
    </div>
    ${traceHtml}
  </div>
</div>`;
};
"""
