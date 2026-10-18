/* ==========================================================================
   COMMONS SYSTEM WORKSPACE (system.js)
   Pages: Setup, Secrets, Recurring, System
   Linear / Vercel design, calm, precise, interactive, accessible.
   ========================================================================== */
(function () {
"use strict";

const ICONS = {
  check: '<path d="M20 6L9 17l-5-5"/>',
  circle: '<circle cx="12" cy="12" r="9"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  arrowUp: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  arrowDown: '<path d="M12 5v14M19 12l-7 7-7-7"/>',
  trash: '<path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  refresh: '<path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>',
  pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
  play: '<polygon points="5 3 19 12 5 21 5 3"/>',
  lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  key: '<path d="M21 2l-2 2m-1.5 1.5L14 9l-2-2-4 4 2 2-6 6v3h3l6-6 2 2 4-4-2-2 3.5-3.5a5 5 0 0 0-7-7z"/>',
  bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  grip: '<circle cx="9" cy="6" r="1.5"/><circle cx="15" cy="6" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="18" r="1.5"/><circle cx="15" cy="18" r="1.5"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
};

function sysSvg(name, size = 16, cls = "") {
  const path = ICONS[name] || "";
  return `<svg class="${cls}" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
}

/* Format time intervals in plain English */
function formatInterval(seconds) {
  const s = Math.round(+seconds || 0);
  if (s <= 0) return "on demand";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

function showInlineSaved(elId, msg = "Saved") {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = "✓ " + msg;
  el.classList.add("visible");
  setTimeout(() => el.classList.remove("visible"), 2400);
}

/* ==========================================================================
   GLOBAL ACTION HANDLERS (available for inline onclick / onchange)
   ========================================================================== */

window.suSaveSetting = async function (key, val, elId) {
  try {
    const res = await postRaw("setup/setting", { key, value: val });
    if (res && res.error) {
      toast(res.error, "err");
    } else {
      showInlineSaved(elId, "Saved");
    }
  } catch (err) {
    toast(String(err), "err");
  }
};

window.suProv = async function (kind, btnId, statusId) {
  const input = document.getElementById("pk_" + kind);
  const val = input ? input.value.trim() : "";
  const payload = kind === "ollama" ? { kind, base_url: val } : { kind, key: val };
  try {
    const res = await postRaw("setup/provider", payload);
    if (res && res.error) {
      toast(res.error, "err");
    } else {
      if (input && kind !== "ollama") input.value = "";
      showInlineSaved(statusId, "Key saved");
      toast(`${kind} provider updated`);
      setTimeout(render, 400);
    }
  } catch (err) {
    toast(String(err), "err");
  }
};

window.suProvTest = async function (kind, btnId, resultId) {
  const btn = document.getElementById(btnId);
  const resEl = document.getElementById(resultId);
  if (btn) btn.disabled = true;
  if (resEl) resEl.innerHTML = `<span class="badge info">Testing connection...</span>`;
  try {
    const r = await postRaw("setup/provider_test", { kind });
    if (r && r.ok) {
      if (resEl) resEl.innerHTML = `<span class="badge ok">${sysSvg("check", 12)} Connected (${r.models} models available)</span>`;
      toast(`${kind}: connection verified`);
    } else {
      const msg = (r && r.error) ? r.error : "Connection test failed";
      if (resEl) resEl.innerHTML = `<span class="badge bad">${E(msg)}</span>`;
      toast(msg, "err");
    }
  } catch (err) {
    if (resEl) resEl.innerHTML = `<span class="badge bad">${E(String(err))}</span>`;
  } finally {
    if (btn) btn.disabled = false;
  }
};

window.suChainMove = function (i, delta) {
  const c = window.suChain;
  if (!c) return;
  const target = i + delta;
  if (target < 0 || target >= c.length) return;
  const temp = c[i];
  c[i] = c[target];
  c[target] = temp;
  render();
};

window.suChainDel = function (i) {
  const c = window.suChain;
  if (!c) return;
  c.splice(i, 1);
  render();
};

window.suChainAdd = function () {
  const el = document.getElementById("suNew");
  const val = el ? el.value.trim() : "";
  if (!val) return;
  if (!window.suChain) window.suChain = [];
  window.suChain.push(val);
  el.value = "";
  render();
};

window.suSaveChain = async function (elId) {
  if (!window.suChain) return;
  try {
    const res = await postRaw("setup/setting", { key: "judge_chain", value: window.suChain });
    if (res && res.error) {
      toast(res.error, "err");
    } else {
      showInlineSaved(elId, "Order saved");
      toast("Decision chain updated");
    }
  } catch (err) {
    toast(String(err), "err");
  }
};

window.suCred = async function () {
  const vn = document.getElementById("vn");
  const vs = document.getElementById("vs");
  const vu = document.getElementById("vu");
  const va = document.getElementById("va");
  const vb = document.getElementById("vb");
  const vr = document.getElementById("vr");
  const vd = document.getElementById("vd");
  const vv = document.getElementById("vv");
  if (!vn || !vv || !vu) return;

  const name = vn.value.trim();
  const val = vv.value;
  const base = vu.value.trim();
  const fv = id => ((document.getElementById(id) || {}).value || "").trim();
  if (!name || !val || !base) {
    toast("Name, base URL and secret value are required", "err");
    return;
  }
  if (fv("vpu").length < 8 || fv("vsc").length < 3) {
    toast("Say what the key is for and what it can do (its scope)", "err");
    return;
  }
  const auth = va && va.value === "basic:" ? "basic:" + (vb ? vb.value.trim() : "") : (va ? va.value : "bearer");
  try {
    const res = await postRaw("vault/set", {
      name,
      service: vs ? vs.value.trim() : name,
      base_urls: [base],
      auth,
      risk: vr ? vr.value : "normal",
      description: vd ? vd.value.trim() : "",
      purpose: fv("vpu"), scope: fv("vsc"), where: fv("vw"), what: fv("vwh"),
      value: val,
    });
    if (res && res.error) {
      toast(res.error, "err");
    } else {
      toast(`Credential ${name} stored in vault`);
      render();
    }
  } catch (err) {
    toast(String(err), "err");
  }
};

window.suPhoneTest = async function (statusId) {
  try {
    const res = await postRaw("setup/phone_test", {});
    if (res && res.error) {
      toast(res.error, "err");
    } else {
      showInlineSaved(statusId, "Test notification dispatched");
      toast("Phone test sent");
    }
  } catch (err) {
    toast(String(err), "err");
  }
};

window.suSignOut = async function () {
  try {
    await fetch("/approve/logout", {
      method: "POST",
      headers: { "X-Commons": "1" },
    });
    toast("Signed out of owner session");
    render();
  } catch (err) {
    toast(String(err), "err");
  }
};

window.suPauseMenu = function (btn) {
  if (window.K && K.menu) {
    K.menu(btn, [
      {
        label: "Confirm pause all work",
        kind: "bad",
        run: () => post("killswitch", { paused: true, reason: "Paused from System page" }),
      },
      { label: "Cancel" },
    ]);
  } else if (confirm("Pause all background tasks and agent work?")) {
    post("killswitch", { paused: true, reason: "Paused from System page" });
  }
};

window.suResumeWork = function () {
  post("killswitch", { paused: false });
};

window.suScrollTo = function (id) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: "smooth" });
};

window.openRecurringDrawer = function (id, sched) {
  if (!window.K || !K.drawer) return;
  K.drawer.open({
    id: "rec-" + id,
    title: sched.name,
    subtitle: `Schedule: ${sched.cron} (${sched.tz || "UTC"})`,
    html: `
      <div style="display:flex;flex-direction:column;gap:var(--space-4)">
        <div>
          <span class="badge ${sched.enabled ? "ok" : "warn"}">${sched.enabled ? "Enabled" : "Disabled"}</span>
          ${sched.pattern ? `<span class="badge info">${E(sched.pattern)}</span>` : ""}
          ${sched.project_id ? `<span class="badge">${E(sched.project_id)}</span>` : ""}
        </div>
        <div>
          <h4 style="margin:0 0 var(--space-1) 0;font-size:var(--text-sm)">Task prompt</h4>
          <pre style="white-space:pre-wrap;font-size:var(--text-xs);background:var(--bg-surface);padding:var(--space-3);border-radius:var(--radius-md);border:1px solid var(--border-subtle)">${E(sched.task || "")}</pre>
        </div>
        ${sched.instructions ? `
          <div>
            <h4 style="margin:0 0 var(--space-1) 0;font-size:var(--text-sm)">Instructions</h4>
            <div style="font-size:var(--text-xs);color:var(--fg-muted)">${E(sched.instructions)}</div>
          </div>
        ` : ""}
        ${sched.acceptance ? `
          <div>
            <h4 style="margin:0 0 var(--space-1) 0;font-size:var(--text-sm)">Acceptance criteria</h4>
            <div style="font-size:var(--text-xs);color:var(--fg-muted)">${E(sched.acceptance)}</div>
          </div>
        ` : ""}
        ${sched.quality_ref ? `
          <div>
            <h4 style="margin:0 0 var(--space-1) 0;font-size:var(--text-sm)">Last verified reference output</h4>
            <pre style="white-space:pre-wrap;font-size:var(--text-xs);background:var(--bg-surface);padding:var(--space-3);border-radius:var(--radius-md);border:1px solid var(--border-subtle)">${E(sched.quality_ref)}</pre>
          </div>
        ` : ""}
        <div>
          <h4 style="margin:0 0 var(--space-1) 0;font-size:var(--text-sm)">Schedule metadata</h4>
          ${K.kv({
            "Schedule ID": sched.id,
            "Next run": sched.next_run || "Not scheduled",
            "Last run": sched.last_run || "Never",
            "Last job": sched.last_job || "None",
            "Owner agent": sched.owner_agent || sched.created_by || "System",
          })}
        </div>
      </div>
    `,
    actions: [
      {
        label: "Trigger run now",
        kind: "pri",
        run: () => post("maintenance/run", { job: "recurring" }).then(() => toast("Maintenance triggered for recurring tasks")),
      },
    ],
  });
};

window.suVaultNote = async function (name, mode, useId) {
  const fv = id => ((document.getElementById(id) || {}).value || "").trim();
  const body = { name };
  if (mode === "describe") { body.purpose = fv("vn-pu"); body.scope = fv("vn-sc"); }
  else if (mode === "remove") { body.remove_id = useId; }
  else {
    body.where = fv("vn-w"); body.what = fv("vn-wh"); body.project = fv("vn-pr");
    if (body.where.length < 3) { toast("Say where it is used", "err"); return; }
  }
  const r = await postRaw("vault/note", body);
  if (r && r.error) { toast(r.error, "err"); return; }
  toast(mode === "remove" ? "Place removed" : "Saved");
  render();
  const fresh = await api("setup").catch(() => null);
  const c = fresh && (fresh.credentials || []).find(x => x.credential === name);
  if (c) openSecretDrawer(c);
};

window.openSecretDrawer = function (cred) {
  if (!window.K || !K.drawer) return;
  K.drawer.open({
    id: "cred-" + cred.credential,
    title: cred.credential,
    subtitle: `Service: ${cred.service}`,
    html: `
      <div style="display:flex;flex-direction:column;gap:var(--space-4)">
        <div>
          <span class="badge ${cred.risk === "high" ? "bad" : cred.risk === "low" ? "info" : "ok"}">${E(cred.risk)} risk</span>
          ${cred.kind === "login" ? '<span class="badge accent">Login credential</span>' : ""}
        </div>
        <div>
          <h4 style="margin:0 0 var(--space-1) 0;font-size:var(--text-sm)">Authorized destinations</h4>
          <div style="font-size:var(--text-xs);color:var(--fg-muted)">${E((cred.base_urls || []).join(", ") || "No restrictions declared")}</div>
        </div>
        <div>
          <h4 style="margin:0 0 var(--space-1) 0;font-size:var(--text-sm)">What it's for</h4>
          <input id="vn-pu" value="${E(cred.purpose || "")}" placeholder="What the key is for" aria-label="What the key is for" style="width:100%">
          <h4 style="margin:var(--space-3) 0 var(--space-1) 0;font-size:var(--text-sm)">What it can do (scope)</h4>
          <input id="vn-sc" value="${E(cred.scope || "")}" placeholder="read-only, DNS edit on one zone, full admin…" aria-label="Scope" style="width:100%">
          <div style="margin-top:var(--space-2);display:flex;justify-content:flex-end"><button type="button" class="btn sm" onclick="suVaultNote(${J(cred.credential)}, 'describe')">Save</button></div>
        </div>
        <div>
          <h4 style="margin:0 0 var(--space-1) 0;font-size:var(--text-sm)">Where it's used (${E((cred.used_in || []).length)})</h4>
          ${(cred.used_in || []).length ? `<div class="list">${cred.used_in.map(u => `<div class="row"><div class="row-content"><div class="row-title"><b>${E(u.where)}</b>${u.project ? ` <span class="badge">${E(u.project)}</span>` : ""}</div>
            <div class="row-subtitle">${E(u.what || "")}${u.what ? " · " : ""}added by ${E(u.by)} ${E(K.fmt.ago(u.at) || "")}</div></div>
            <div class="row-actions"><button type="button" class="btn sm bad" onclick="suVaultNote(${J(cred.credential)}, 'remove', ${J(u.id)})">Remove</button></div></div>`).join("")}</div>`
            : '<div style="font-size:var(--text-xs);color:var(--fg-muted)">No places recorded yet. Add every server, app or job that holds this key.</div>'}
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:var(--space-2);margin-top:var(--space-2)">
            <input id="vn-w" placeholder="Where, e.g. Poise server (LXC 108) .env" aria-label="Where it is used">
            <input id="vn-wh" placeholder="What it does there" aria-label="What it does there">
            <input id="vn-pr" placeholder="Project (optional)" aria-label="Project">
          </div>
          <div style="margin-top:var(--space-2);display:flex;justify-content:flex-end"><button type="button" class="btn sm" onclick="suVaultNote(${J(cred.credential)}, 'add')">Add place</button></div>
        </div>
        ${cred.calls_30d && cred.calls_30d.count ? `<div style="font-size:var(--text-xs);color:var(--fg-muted)">${E(cred.calls_30d.count)} calls through Commons in 30 days, last by ${E(cred.calls_30d.last_by || "?")} ${E(K.fmt.ago(cred.calls_30d.last) || "")}</div>` : ""}
        ${cred.description ? `
          <div>
            <h4 style="margin:0 0 var(--space-1) 0;font-size:var(--text-sm)">Other notes</h4>
            <div style="font-size:var(--text-xs);color:var(--fg-muted)">${E(cred.description)}</div>
          </div>
        ` : ""}
        <div>
          <h4 style="margin:0 0 var(--space-1) 0;font-size:var(--text-sm)">Metadata</h4>
          ${K.kv({
            "Reference": `vault://${cred.credential}`,
            "Service": cred.service,
            "Deposited by": cred.from || "admin",
            "Username": cred.username || "None",
          })}
        </div>
      </div>
    `,
    actions: [
      {
        label: "Copy reference",
        run: () => K.copy(`vault://${cred.credential}`),
      },
      {
        label: "Remove from vault",
        kind: "bad",
        run: () => {
          if (confirm(`Remove ${cred.credential} from the encrypted vault?`)) {
            post("vault/rm", { name: cred.credential });
            K.drawer.close();
          }
        },
      },
    ],
  });
};

/* Filter handler for Secrets page */
window.secFilterState = window.secFilterState || { q: "", tab: "all" };
window.secSetFilter = function (key, val) {
  window.secFilterState[key] = val;
  clearTimeout(window._secT);
  if (key === "q") window._secT = setTimeout(render, 250); else render();  // typing refetches once the user pauses
};

/* Filter handler for Recurring page */
window.recFilterQuery = window.recFilterQuery || "";
window.recSetQuery = function (q, typing) {
  window.recFilterQuery = q;
  clearTimeout(window._recT);
  if (typing) window._recT = setTimeout(render, 250); else render();
};

/* Drag and drop for decision chain */
let dragSrcIndex = null;
window.suDragStart = function (e, idx) {
  dragSrcIndex = idx;
  e.dataTransfer.effectAllowed = "move";
  e.currentTarget.classList.add("dragging");
};
window.suDragOver = function (e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = "move";
};
window.suDragDrop = function (e, targetIdx) {
  e.preventDefault();
  if (dragSrcIndex === null || dragSrcIndex === targetIdx) return;
  const c = window.suChain;
  if (!c) return;
  const item = c.splice(dragSrcIndex, 1)[0];
  c.splice(targetIdx, 0, item);
  dragSrcIndex = null;
  render();
};
window.suDragEnd = function (e) {
  dragSrcIndex = null;
  e.currentTarget.classList.remove("dragging");
};

/* ==========================================================================
   PAGE: Setup
   Sectioned settings page (Vercel / Linear inspired)
   ========================================================================== */
V["Setup"] = async function () {
  const [d, owner] = await Promise.all([
    api("setup"),
    api("owner").catch(() => ({ set: false })),
  ]);

  window.suChain = window.suChain || d.judges.map(j => j.entry);
  const ch = window.suChain;
  const rdy = e => e === "ask" || (d.judges.find(j => j.entry === e) || {}).ready;
  const opts = [
    "typesafe",
    "anthropic/claude-haiku-5-5",
    "anthropic/claude-sonnet-5-5",
    "openai/gpt-5-mini",
    "google/gemini-3-flash",
    "xai/grok-4-6",
    "openrouter/",
    "ollama/qwen3:8b",
    "role:gatekeeper",
    "agent:any",
    "ask",
  ];

  const sections = [
    { id: "sec-start", label: "Getting started", icon: "check" },
    { id: "sec-security", label: "Owner and security", icon: "shield" },
    { id: "sec-providers", label: "Model providers", icon: "key" },
    { id: "sec-chain", label: "Decision chain", icon: "circle" },
    { id: "sec-credentials", label: "Credentials", icon: "lock" },
    { id: "sec-features", label: "Features", icon: "check" },
    { id: "sec-limits", label: "Limits and choices", icon: "clock" },
    { id: "sec-integrations", label: "Integrations", icon: "bell" },
    { id: "sec-agents", label: "Connected agents", icon: "circle" },
  ];

  const pct = Math.round((d.done / (d.total || 1)) * 100);

  return `
    <div class="sys-page">
      <div class="page-head">
        <div>
          <h2 style="margin:0;font-size:var(--text-xl);font-weight:600">Settings and configuration</h2>
          <div style="font-size:var(--text-sm);color:var(--fg-muted);margin-top:2px">
            Configure keys, model providers, approval policies, and hub features
          </div>
        </div>
      </div>

      <!-- Mobile Section Selector -->
      <select class="sys-mobile-nav" onchange="suScrollTo(this.value)" aria-label="Jump to section">
        ${sections.map(s => `<option value="${s.id}">${E(s.label)}</option>`).join("")}
      </select>

      <div class="sys-layout">
        <!-- Desktop Sticky Navigation -->
        <nav class="sys-nav" aria-label="Settings sections">
          ${sections.map(s => `
            <button type="button" class="sys-nav-item" onclick="suScrollTo('${s.id}')">
              ${sysSvg(s.icon, 14)}
              <span>${E(s.label)}</span>
            </button>
          `).join("")}
        </nav>

        <!-- Main Content -->
        <div class="sys-content">

          <!-- Section 1: Getting Started -->
          <section class="sys-section" id="sec-start">
            <div class="sys-card">
              <div class="sys-card-header">
                <div>
                  <div class="sys-card-title">${sysSvg("check", 18)} Getting started</div>
                  <p class="sys-card-desc">Checklist for completing hub setup and readying connected agents.</p>
                </div>
                <span class="badge ${d.done === d.total ? "ok" : "warn"}">${d.done} of ${d.total} completed</span>
              </div>

              <div class="progress" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">
                <div class="progress-bar" style="width:${pct}%"></div>
              </div>

              <div class="sys-checklist">
                ${d.steps.map(x => `
                  <div class="sys-check-item ${x.done ? "done" : ""}">
                    <div class="sys-check-icon">${sysSvg(x.done ? "check" : "circle", 16)}</div>
                    <div class="sys-check-content">
                      <div class="sys-check-title">${E(x.step)}</div>
                      <div class="sys-check-desc">${E(x.detail)}</div>
                    </div>
                    ${!x.done && /Owner password/i.test(x.step) ? `
                      <button type="button" class="btn sm pri" onclick="pinUnlock()">Set password</button>
                    ` : ""}
                  </div>
                `).join("")}
              </div>
            </div>
          </section>

          <!-- Section 2: Owner and Security -->
          <section class="sys-section" id="sec-security">
            <div class="sys-card">
              <div class="sys-card-header">
                <div>
                  <div class="sys-card-title">${sysSvg("shield", 18)} Owner and security</div>
                  <p class="sys-card-desc">Protect sensitive actions, vault credentials, and admin operations.</p>
                </div>
                <span class="badge ${owner.set ? "ok" : "warn"}">${owner.set ? "Owner password configured" : "Password not set"}</span>
              </div>

              <div class="sys-setting-row">
                <div class="sys-setting-info">
                  <div class="sys-setting-label">Owner password protection</div>
                  <div class="sys-setting-hint">
                    ${owner.set ? "Owner password is active. Unlocked browser sessions remain authenticated for 14 days." : "No owner password set yet. Sensitive actions are open to local network callers."}
                  </div>
                </div>
                <div class="sys-setting-control">
                  <button type="button" class="btn sm" onclick="pinUnlock()">${owner.set ? "Change password" : "Set password"}</button>
                  ${owner.set ? `<button type="button" class="btn sm bad" onclick="suSignOut()">Sign out</button>` : ""}
                </div>
              </div>

              ${(() => {
                const lockSetting = d.settings.find(s => s.key === "dashboard_lock") || { value: false };
                return `
                  <div class="sys-setting-row">
                    <div class="sys-setting-info">
                      <div class="sys-setting-label">
                        <span>Lock dashboard to authenticated sessions</span>
                        <span class="sys-inline-saved" id="sv_dashboard_lock"></span>
                      </div>
                      <div class="sys-setting-hint">
                        When enabled, visitors must unlock with the owner password to access dashboard pages.
                      </div>
                    </div>
                    <div class="sys-setting-control">
                      <label class="switch">
                        <input type="checkbox" ${lockSetting.value ? "checked" : ""} onchange="suSaveSetting('dashboard_lock', this.checked, 'sv_dashboard_lock')">
                        <span class="switch-slider"></span>
                      </label>
                    </div>
                  </div>
                `;
              })()}
            </div>
          </section>

          <!-- Section 3: Model Providers -->
          <section class="sys-section" id="sec-providers">
            <div class="sys-card">
              <div class="sys-card-header">
                <div>
                  <div class="sys-card-title">${sysSvg("key", 18)} Model providers</div>
                  <p class="sys-card-desc">API keys for the decision agent, background roles, and fallback seats. Keys are stored in the encrypted vault.</p>
                </div>
              </div>

              <div class="sys-provider-list">
                ${d.providers.map(p => `
                  <div class="sys-provider-row">
                    <div class="sys-provider-header">
                      <div class="sys-provider-meta">
                        <span class="sys-provider-name">${E(p.label)}</span>
                        <span class="badge ${p.ready ? "ok" : "warn"}">${p.ready ? "Ready" : "Not configured"}</span>
                        <span class="sys-inline-saved" id="sv_prov_${p.kind}"></span>
                      </div>
                      ${p.ready ? `
                        <button type="button" class="btn sm" id="btn_test_${p.kind}" onclick="suProvTest('${p.kind}', 'btn_test_${p.kind}', 'res_test_${p.kind}')">Test connection</button>
                      ` : ""}
                    </div>

                    <div class="sys-provider-controls">
                      <input
                        class="sys-provider-input"
                        id="pk_${p.kind}"
                        type="${p.needs_key ? "password" : "text"}"
                        placeholder="${p.needs_key ? (p.ready ? "Replace API key" : "Paste API key") : "http://127.0.0.1:11434"}"
                        autocomplete="off">
                      <button type="button" class="btn sm pri" onclick="suProv('${p.kind}', 'pk_${p.kind}', 'sv_prov_${p.kind}')">Save</button>
                      ${p.ready ? `
                        <button type="button" class="btn sm bad" onclick="confirm(${J('Remove key for ' + p.label + '?')}) && post('setup/provider_rm', {kind: ${J(p.kind)}})">Remove</button>
                      ` : ""}
                    </div>

                    <div class="sys-test-result" id="res_test_${p.kind}"></div>
                  </div>
                `).join("")}
              </div>
            </div>
          </section>

          <!-- Section 4: Decision Chain -->
          <section class="sys-section" id="sec-chain">
            <div class="sys-card">
              <div class="sys-card-header">
                <div>
                  <div class="sys-card-title">${sysSvg("circle", 18)} Decision chain</div>
                  <p class="sys-card-desc">Evaluated top to bottom when an agent requests a credential. Entries without keys are skipped; you are always the final stop.</p>
                </div>
                <div style="display:flex;align-items:center;gap:var(--space-2)">
                  <span class="sys-inline-saved" id="sv_chain"></span>
                  <button type="button" class="btn sm pri" onclick="suSaveChain('sv_chain')">Save order</button>
                </div>
              </div>

              <div class="sys-chain-list" id="chain-container">
                ${ch.map((e, i) => `
                  <div class="sys-chain-item" draggable="true" ondragstart="suDragStart(event, ${i})" ondragover="suDragOver(event)" ondrop="suDragDrop(event, ${i})" ondragend="suDragEnd(event)">
                    <span class="sys-chain-handle" title="Drag to reorder">${sysSvg("grip", 14)}</span>
                    <div class="sys-chain-entry">
                      <code>${E(e)}</code>
                      <span class="badge ${rdy(e) ? "ok" : "warn"}">${rdy(e) ? "Ready" : "No key"}</span>
                    </div>
                    <div class="sys-chain-actions">
                      <button type="button" class="btn sm" aria-label="Move up" onclick="suChainMove(${i}, -1)" ${i === 0 ? "disabled" : ""}>${sysSvg("arrowUp", 14)}</button>
                      <button type="button" class="btn sm" aria-label="Move down" onclick="suChainMove(${i}, 1)" ${i === ch.length - 1 ? "disabled" : ""}>${sysSvg("arrowDown", 14)}</button>
                      <button type="button" class="btn sm bad" aria-label="Remove entry" onclick="suChainDel(${i})">${sysSvg("trash", 14)}</button>
                    </div>
                  </div>
                `).join("")}
              </div>

              <div style="display:flex;gap:var(--space-2);margin-top:var(--space-2);flex-wrap:wrap">
                <input id="suNew" list="suOpts" placeholder="Add typesafe, model, role:gatekeeper, agent:any, or ask" style="flex:1;min-width:240px">
                <datalist id="suOpts">
                  ${opts.map(o => `<option value="${o}">`).join("")}
                </datalist>
                <button type="button" class="btn" onclick="suChainAdd()">Add to chain</button>
              </div>
            </div>
          </section>

          <!-- Section 5: Credentials (Vault) -->
          <section class="sys-section" id="sec-credentials">
            <div class="sys-card">
              <div class="sys-card-header">
                <div>
                  <div class="sys-card-title">${sysSvg("lock", 18)} Vault credentials</div>
                  <p class="sys-card-desc">Agents access these through Commons via use_credential and never see raw values.</p>
                </div>
              </div>

              ${d.credentials.length ? `
                <div class="list">
                  ${d.credentials.map(c => `
                    <div class="row">
                      <div class="row-prefix">${sysSvg("key", 16)}</div>
                      <div class="row-content">
                        <div class="row-title">
                          <b>${E(c.credential)}</b>
                          ${c.kind === "login" ? '<span class="badge accent" style="margin-left:4px">Login</span>' : ""}
                          <span class="badge ${c.risk === "high" ? "bad" : c.risk === "low" ? "info" : "ok"}" style="margin-left:4px">${E(c.risk)} risk</span>
                        </div>
                        <div class="row-subtitle">
                          ${E(c.purpose || "Purpose not recorded")}${c.scope ? ` · can: ${E(c.scope)}` : ""}
                        </div>
                        <div class="row-subtitle">
                          ${E(c.service)} · used in ${E((c.used_in || []).length)} place${(c.used_in || []).length === 1 ? "" : "s"} · sent only to: ${E((c.base_urls || []).join(", ") || "nowhere yet")} · from: ${E(c.from || "the user")}
                          ${(c.missing || []).length ? `<span class="badge warn" style="margin-left:4px">needs ${E(c.missing.join(", "))}</span>` : ""}
                        </div>
                      </div>
                      <div class="row-actions">
                        <button type="button" class="btn sm" onclick="openSecretDrawer(${J(c)})">Details</button>
                        <button type="button" class="btn sm bad" onclick="confirm(${J('Remove ' + c.credential + ' from the vault?')}) && post('vault/rm', {name: ${J(c.credential)}})">Remove</button>
                      </div>
                    </div>
                  `).join("")}
                </div>
              ` : `
                <div class="empty">
                  <div class="ico">${sysSvg("lock", 24)}</div>
                  <b>No credentials stored in vault</b>
                  <div class="hint">Store a credential below or allow agents to deposit with vault_deposit.</div>
                </div>
              `}

              <details style="border:1px solid var(--border-default);border-radius:var(--radius-md);padding:var(--space-3);background:var(--bg-surface)">
                <summary style="cursor:pointer;font-weight:500;font-size:var(--text-sm)">Add a new credential</summary>
                <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));gap:var(--space-3);margin-top:var(--space-3)">
                  <input id="vn" placeholder="Name, e.g. radarr">
                  <input id="vs" placeholder="Service, e.g. radarr">
                  <input id="vu" placeholder="Allowed base URL, e.g. https://api.service.com">
                  <select id="va" aria-label="Auth header format">
                    <option value="header:X-Api-Key">Header X-Api-Key</option>
                    <option value="bearer">Bearer token</option>
                    <option value="query:apikey">Query parameter ?apikey=</option>
                    <option value="header:X-Plex-Token">Header X-Plex-Token</option>
                    <option value="basic:">Basic auth (user + password)</option>
                  </select>
                  <input id="vb" placeholder="Username (Basic auth only)">
                  <select id="vr" aria-label="Risk assessment">
                    <option value="normal">Normal risk</option>
                    <option value="low">Low risk</option>
                    <option value="high">High risk (every write asks you)</option>
                  </select>
                  <input id="vpu" placeholder="What it's for (required), e.g. Poise sends sign-in codes">
                  <input id="vsc" placeholder="What it can do (required), e.g. read-only, DNS edit on one zone">
                  <input id="vw" placeholder="Where it's used, e.g. Poise server (LXC 108) .env">
                  <input id="vwh" placeholder="What it does there">
                  <input id="vd" placeholder="Other notes">
                  <input id="vv" type="password" placeholder="Secret value" autocomplete="off">
                </div>
                <div style="margin-top:var(--space-3);display:flex;justify-content:flex-end">
                  <button type="button" class="btn pri" onclick="suCred()">Store credential</button>
                </div>
              </details>
            </div>
          </section>

          <!-- Section 6: Features -->
          <section class="sys-section" id="sec-features">
            <div class="sys-card">
              <div class="sys-card-header">
                <div>
                  <div class="sys-card-title">${sysSvg("check", 18)} Features</div>
                  <p class="sys-card-desc">All features are recommended; toggle off features not needed for your setup.</p>
                </div>
              </div>

              <div>
                ${d.settings.filter(x => typeof x.default === "boolean").map(x => `
                  <div class="sys-setting-row">
                    <div class="sys-setting-info">
                      <div class="sys-setting-label">
                        <span>${E(x.help)}</span>
                        ${x.risky ? '<span class="badge warn">Your risk</span>' : x.paid ? '<span class="badge warn">Costs money</span>' : '<span class="badge ok">Recommended</span>'}
                        <span class="sys-inline-saved" id="sv_${x.key}"></span>
                      </div>
                      <div class="sys-setting-hint">Setting key: <code>${E(x.key)}</code></div>
                    </div>
                    <div class="sys-setting-control">
                      <label class="switch">
                        <input type="checkbox" ${x.value ? "checked" : ""} onchange="suSaveSetting('${x.key}', this.checked, 'sv_${x.key}')">
                        <span class="switch-slider"></span>
                      </label>
                    </div>
                  </div>
                `).join("")}
              </div>
            </div>
          </section>

          <!-- Section 7: Limits and Choices -->
          <section class="sys-section" id="sec-limits">
            <div class="sys-card">
              <div class="sys-card-header">
                <div>
                  <div class="sys-card-title">${sysSvg("clock", 18)} Limits and choices</div>
                  <p class="sys-card-desc">Configure capacity thresholds, seat models, and workflow plan chains.</p>
                </div>
              </div>

              <div>
                ${d.settings.filter(x => typeof x.default === "number").map(x => `
                  <div class="sys-setting-row">
                    <div class="sys-setting-info">
                      <div class="sys-setting-label">
                        <span>${E(x.help)}</span>
                        <span class="sys-inline-saved" id="sv_${x.key}"></span>
                      </div>
                      <div class="sys-setting-hint">Setting key: <code>${E(x.key)}</code></div>
                    </div>
                    <div class="sys-setting-control">
                      <input
                        type="number"
                        min="0"
                        ${x.key === "reserve_pct" ? 'max="90"' : 'step="0.5"'}
                        value="${x.value}"
                        style="width:90px"
                        onchange="suSaveSetting('${x.key}', this.value, 'sv_${x.key}')">
                    </div>
                  </div>
                `).join("")}

                ${d.settings.filter(x => typeof x.default === "string" && x.choices).map(x => `
                  <div class="sys-setting-row">
                    <div class="sys-setting-info">
                      <div class="sys-setting-label">
                        <span>${E(x.help)}</span>
                        <span class="sys-inline-saved" id="sv_${x.key}"></span>
                      </div>
                      <div class="sys-setting-hint">Setting key: <code>${E(x.key)}</code></div>
                    </div>
                    <div class="sys-setting-control">
                      <select onchange="suSaveSetting('${x.key}', this.value, 'sv_${x.key}')">
                        ${x.choices.map(c => `<option ${c === x.value ? "selected" : ""}>${E(c)}</option>`).join("")}
                      </select>
                    </div>
                  </div>
                `).join("")}

                ${(() => {
                  const sm = d.settings.find(x => x.key === "seat_models");
                  if (!sm) return "";
                  const val = Array.isArray(sm.value) ? sm.value.join(", ") : String(sm.value || "");
                  return `
                    <div class="sys-setting-row">
                      <div class="sys-setting-info">
                        <div class="sys-setting-label">
                          <span>${E(sm.help)} (comma separated)</span>
                          <span class="sys-inline-saved" id="sv_seat_models"></span>
                        </div>
                        <div class="sys-setting-hint">Fallback models used when no subscription agent is available.</div>
                      </div>
                      <div class="sys-setting-control" style="width:100%;max-width:320px">
                        <input
                          value="${E(val)}"
                          placeholder="anthropic/claude-haiku-5-5, openai/gpt-5-mini"
                          style="width:100%"
                          onchange="suSaveSetting('seat_models', this.value, 'sv_seat_models')">
                      </div>
                    </div>
                  `;
                })()}

                ${(() => {
                  const pc = d.settings.find(x => x.key === "plan_chain");
                  if (!pc) return "";
                  const val = Array.isArray(pc.value) ? pc.value.join(", ") : String(pc.value || "");
                  return `
                    <div class="sys-setting-row">
                      <div class="sys-setting-info">
                        <div class="sys-setting-label">
                          <span>${E(pc.help)} (comma separated)</span>
                          <span class="sys-inline-saved" id="sv_plan_chain"></span>
                        </div>
                        <div class="sys-setting-hint">Sequence of agents or models for multi-step planning.</div>
                      </div>
                      <div class="sys-setting-control" style="width:100%;max-width:320px">
                        <input
                          value="${E(val)}"
                          placeholder="anthropic/claude-sonnet-5-5, google/gemini-3-flash"
                          style="width:100%"
                          onchange="suSaveSetting('plan_chain', this.value, 'sv_plan_chain')">
                      </div>
                    </div>
                  `;
                })()}
              </div>
            </div>
          </section>

          <!-- Section 8: Integrations -->
          <section class="sys-section" id="sec-integrations">
            <div class="sys-card">
              <div class="sys-card-header">
                <div>
                  <div class="sys-card-title">${sysSvg("bell", 18)} Integrations</div>
                  <p class="sys-card-desc">Webhooks, push notifications, and external repositories.</p>
                </div>
              </div>

              <!-- Phone push notification test -->
              <div class="sys-setting-row">
                <div class="sys-setting-info">
                  <div class="sys-setting-label">
                    <span>Mobile push notifications (ntfy)</span>
                    <span class="sys-inline-saved" id="sv_phone_test"></span>
                  </div>
                  <div class="sys-setting-hint">
                    Subscribe to the configured ntfy topic on your device to receive approval requests with one-tap answers.
                  </div>
                </div>
                <div class="sys-setting-control">
                  <button type="button" class="btn sm" onclick="suPhoneTest('sv_phone_test')">Send test notification</button>
                </div>
              </div>

              <!-- GitHub repositories -->
              <div class="sys-setting-row">
                <div class="sys-setting-info">
                  <div class="sys-setting-label">
                    <span>GitHub repos to watch (owner/repo, comma separated)</span>
                    <span class="sys-inline-saved" id="sv_github_watch"></span>
                  </div>
                  <div class="sys-setting-hint">
                    Failed CI runs, new issues, or PRs become automated tasks for active agents.
                  </div>
                </div>
                <div class="sys-setting-control" style="width:100%;max-width:320px">
                  <input
                    value="${E((d.github_watch || []).join(", "))}"
                    placeholder="owner/repo, owner/second-repo"
                    style="width:100%"
                    onchange="suSaveSetting('github_watch', this.value, 'sv_github_watch')">
                </div>
              </div>

              <!-- Inbound webhook URL -->
              <div class="sys-setting-row">
                <div class="sys-setting-info">
                  <div class="sys-setting-label">Inbound webhook endpoint</div>
                  <div class="sys-setting-hint">
                    Post payloads from GitHub webhooks or scripts to <code>${E(d.hook_path || "/hooks")}</code>.
                  </div>
                </div>
                <div class="sys-setting-control">
                  <button type="button" class="btn sm" onclick="K.copy(location.origin + ${J(d.hook_path || "")})">Copy webhook URL</button>
                </div>
              </div>

              <!-- Graphiti Knowledge Graph -->
              ${(() => {
                const g = d.settings.find(x => x.key === "graphiti_url");
                if (!g) return "";
                return `
                  <div class="sys-setting-row">
                    <div class="sys-setting-info">
                      <div class="sys-setting-label">
                        <span>Graphiti service URL</span>
                        <span class="sys-inline-saved" id="sv_graphiti_url"></span>
                      </div>
                      <div class="sys-setting-hint">${E(g.help || "External knowledge graph mirror.")}</div>
                    </div>
                    <div class="sys-setting-control" style="width:100%;max-width:320px">
                      <input
                        value="${E(g.value || "")}"
                        placeholder="http://localhost:8000"
                        style="width:100%"
                        onchange="suSaveSetting('graphiti_url', this.value, 'sv_graphiti_url')">
                    </div>
                  </div>
                `;
              })()}
            </div>
          </section>

          <!-- Section 9: Connected Agents -->
          <section class="sys-section" id="sec-agents">
            <div class="sys-card">
              <div class="sys-card-header">
                <div>
                  <div class="sys-card-title">${sysSvg("circle", 18)} Connected agents</div>
                  <p class="sys-card-desc">Status of registered agents and handover completeness.</p>
                </div>
                <button type="button" class="btn sm" onclick="go('Connect')">Connect agent →</button>
              </div>

              ${d.agents.length ? `
                <div class="list">
                  ${d.agents.map(a => `
                    <div class="row">
                      <div class="row-prefix">
                        <span class="status-dot ${a.connected ? "live" : ""}"></span>
                      </div>
                      <div class="row-content">
                        <div class="row-title"><b>${E(a.name)}</b></div>
                        <div class="row-subtitle">
                          ${a.connected ? '<span class="badge ok">Connected</span>' : '<span class="badge warn">Never connected</span>'}
                          · ${a.handover_complete ? '<span class="badge ok">Handover complete</span>' : `<span style="color:var(--warn)">Still owes: ${E((a.missing || []).join(", ") || "profile")}</span>`}
                        </div>
                      </div>
                    </div>
                  `).join("")}
                </div>
              ` : `
                <div class="empty">
                  <div class="ico">${sysSvg("circle", 24)}</div>
                  <b>No agents registered yet</b>
                  <div class="hint">Connect your first agent to start delegating tasks.</div>
                </div>
              `}
            </div>
          </section>

        </div>
      </div>
    </div>
  `;
};

/* ==========================================================================
   PAGE: Secrets
   Vault credentials, decision log, active leases, references, and audit events.
   ========================================================================== */
V["Secrets"] = async function () {
  const [audit, setup] = await Promise.all([
    api("audit"),
    api("setup").catch(() => ({ credentials: [], decisions: [] })),
  ]);

  const creds = setup.credentials || [];
  const decisions = setup.decisions || [];
  const leases = audit.leases || [];
  const refs = audit.refs || [];
  const logs = audit.log || [];

  const filter = window.secFilterState || { q: "", tab: "all" };
  const query = filter.q.toLowerCase().trim();

  const filteredCreds = creds.filter(c =>
    !query ||
    (c.credential && c.credential.toLowerCase().includes(query)) ||
    (c.service && c.service.toLowerCase().includes(query))
  );

  const filteredDecisions = decisions.filter(d =>
    !query ||
    (d.agent && d.agent.toLowerCase().includes(query)) ||
    (d.credential && d.credential.toLowerCase().includes(query)) ||
    (d.call && d.call.toLowerCase().includes(query)) ||
    (d.verdict && d.verdict.toLowerCase().includes(query)) ||
    (d.reason && d.reason.toLowerCase().includes(query))
  );

  const filteredLeases = leases.filter(l =>
    !query ||
    (l.ref && l.ref.toLowerCase().includes(query)) ||
    (l.agent && l.agent.toLowerCase().includes(query)) ||
    (l.id && l.id.toLowerCase().includes(query))
  );

  const filteredLogs = logs.filter(l =>
    !query ||
    (l.action && l.action.toLowerCase().includes(query)) ||
    (l.target && l.target.toLowerCase().includes(query)) ||
    (l.detail && l.detail.toLowerCase().includes(query))
  );

  return `
    <div class="sys-page">
      <div class="page-head">
        <div>
          <h2 style="margin:0;font-size:var(--text-xl);font-weight:600">Secrets and credentials</h2>
          <div style="font-size:var(--text-sm);color:var(--fg-muted);margin-top:2px">
            Encrypted vault credentials, runtime leases, decision logs, and audit trail
          </div>
        </div>
      </div>

      <!-- Top Summary Metrics -->
      <div class="sys-grid-stats">
        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Vault credentials</span>
            <span class="tile-badge">Encrypted</span>
          </div>
          <div class="tile-value">${creds.length}</div>
          <div class="tile-hint">Stored keys and logins</div>
        </div>

        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Active leases</span>
            <span class="tile-badge">Runtime</span>
          </div>
          <div class="tile-value">${leases.filter(l => l.active).length}</div>
          <div class="tile-hint">${leases.length} total granted</div>
        </div>

        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Decisions recorded</span>
            <span class="tile-badge">Audit</span>
          </div>
          <div class="tile-value">${decisions.length}</div>
          <div class="tile-hint">Policy evaluations</div>
        </div>

        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Audit log entries</span>
            <span class="tile-badge">Recent</span>
          </div>
          <div class="tile-value">${logs.length}</div>
          <div class="tile-hint">Security actions</div>
        </div>
      </div>

      <!-- Live Search & Filter Tabs -->
      <div class="sys-card" style="padding:var(--space-3) var(--space-4)">
        <div class="sys-filter-bar">
          <input
            class="sys-filter-input"
            id="sec-filter-in"
            type="search"
            placeholder="Filter by agent, credential, service, or call..."
            value="${E(filter.q)}"
            oninput="secSetFilter('q', this.value)">
          <div class="seg" role="tablist">
            ${[
              ["all", "All records"],
              ["creds", `Vault (${creds.length})`],
              ["decisions", `Decisions (${decisions.length})`],
              ["leases", `Leases (${leases.length})`],
              ["refs", `References (${refs.length})`],
              ["audit", `Audit log (${logs.length})`],
            ].map(([k, label]) => `
              <button
                type="button"
                class="seg-item ${filter.tab === k ? "active" : ""}"
                role="tab"
                aria-selected="${filter.tab === k}"
                onclick="secSetFilter('tab', '${k}')">
                ${E(label)}
              </button>
            `).join("")}
          </div>
        </div>
      </div>

      <!-- Section: Vault Credentials -->
      ${(filter.tab === "all" || filter.tab === "creds") ? `
        <div class="sys-card">
          <div class="sys-card-header">
            <div>
              <div class="sys-card-title">${sysSvg("lock", 18)} Vault credentials (${filteredCreds.length})</div>
              <p class="sys-card-desc">Keys and service tokens. Values are encrypted at rest and never displayed.</p>
            </div>
            <button type="button" class="btn sm" onclick="go('Setup')">Add to vault →</button>
          </div>

          ${filteredCreds.length ? `
            <div class="list">
              ${filteredCreds.map(c => `
                <div class="row">
                  <div class="row-prefix">${sysSvg("key", 16)}</div>
                  <div class="row-content">
                    <div class="row-title">
                      <b>${E(c.credential)}</b>
                      <span class="badge ${c.risk === "high" ? "bad" : c.risk === "low" ? "info" : "ok"}" style="margin-left:4px">${E(c.risk)} risk</span>
                      ${c.kind === "login" ? '<span class="badge accent" style="margin-left:4px">Login</span>' : ""}
                    </div>
                    <div class="row-subtitle">
                      Service: <b>${E(c.service)}</b> · sent to: ${E((c.base_urls || []).join(", ") || "declared base")} · from: ${E(c.from || "admin")}
                    </div>
                  </div>
                  <div class="row-actions">
                    <button type="button" class="btn sm" onclick="K.copy(${J('vault://' + c.credential)})">Copy ref</button>
                    <button type="button" class="btn sm" onclick="openSecretDrawer(${J(c)})">Inspect</button>
                    <button type="button" class="btn sm bad" onclick="confirm(${J('Remove ' + c.credential + ' from the vault?')}) && post('vault/rm', {name: ${J(c.credential)}})">Remove</button>
                  </div>
                </div>
              `).join("")}
            </div>
          ` : `
            <div class="empty">
              <div class="ico">${sysSvg("lock", 24)}</div>
              <b>No matching vault credentials</b>
              <div class="hint">${query ? "Try clearing the search filter." : "Store keys on the Setup page."}</div>
            </div>
          `}
        </div>
      ` : ""}

      <!-- Section: Decision Log -->
      ${(filter.tab === "all" || filter.tab === "decisions") ? `
        <div class="sys-card">
          <div class="sys-card-header">
            <div>
              <div class="sys-card-title">${sysSvg("shield", 18)} Decision log (${filteredDecisions.length})</div>
              <p class="sys-card-desc">Policy evaluations when agents request credential use.</p>
            </div>
          </div>

          ${filteredDecisions.length ? `
            <div style="display:flex;flex-direction:column;gap:var(--space-2)">
              ${filteredDecisions.map(d => `
                <div class="sys-decision-card">
                  <div class="sys-decision-header">
                    <div style="display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap">
                      <span class="badge ${d.verdict === "allow" ? "ok" : d.verdict === "deny" ? "bad" : "warn"}">${E(d.verdict || "evaluated")}</span>
                      <b>${E(d.agent || "Agent")}</b>
                      <span style="color:var(--fg-muted)">requested</span>
                      <code>vault://${E(d.credential || "")}</code>
                    </div>
                    <span style="font-size:var(--text-xs);color:var(--fg-muted)">${K.fmt.ago(d.at)}</span>
                  </div>

                  <div style="display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap">
                    <span class="sys-decision-call">${E(d.call || "")}</span>
                    <span style="font-size:var(--text-xs);color:var(--fg-muted)">${E(d.reason || "")}</span>
                  </div>

                  ${d.justification ? `
                    <div style="margin-top:var(--space-1)">
                      ${K.kv({
                        "Agent rationale": d.justification,
                        "Verdict reason": d.reason || "Policy check",
                      })}
                    </div>
                  ` : ""}
                </div>
              `).join("")}
            </div>
          ` : `
            <div class="empty">
              <div class="ico">${sysSvg("shield", 24)}</div>
              <b>No credential decisions recorded</b>
              <div class="hint">Evaluations will appear here when agents call use_credential.</div>
            </div>
          `}
        </div>
      ` : ""}

      <!-- Section: Active Leases -->
      ${(filter.tab === "all" || filter.tab === "leases") ? `
        <div class="sys-card">
          <div class="sys-card-header">
            <div>
              <div class="sys-card-title">${sysSvg("clock", 18)} Active and recent leases (${filteredLeases.length})</div>
              <p class="sys-card-desc">Time-limited authorization tokens minted for agent sessions.</p>
            </div>
          </div>

          ${filteredLeases.length ? `
            <div class="list">
              ${filteredLeases.map(l => `
                <div class="row">
                  <div class="row-prefix">
                    <span class="badge ${l.active ? "ok" : "warn"}">${l.active ? "Active" : l.revoked ? "Revoked" : "Expired"}</span>
                  </div>
                  <div class="row-content">
                    <div class="row-title">
                      <code>${E(l.ref)}</code>
                      <span style="color:var(--fg-muted);font-weight:400;margin-left:4px">for ${E(l.agent || "agent")}</span>
                    </div>
                    <div class="row-subtitle">
                      Lease ID: <code>${E(l.id)}</code> · scope: ${E(l.scope || "default")} · expires: ${K.fmt.ago(l.expires_at)}
                    </div>
                  </div>
                  <div class="row-actions">
                    ${l.active ? `
                      <button type="button" class="btn sm bad" onclick="post('lease/revoke', {id: ${J(l.id)}})">Revoke lease</button>
                    ` : ""}
                  </div>
                </div>
              `).join("")}
            </div>
          ` : `
            <div class="empty">
              <div class="ico">${sysSvg("clock", 24)}</div>
              <b>No active leases</b>
              <div class="hint">Leases are minted dynamically when agents require temporary access.</div>
            </div>
          `}
        </div>
      ` : ""}

      <!-- Section: Secret References -->
      ${(filter.tab === "all" || filter.tab === "refs") ? `
        <div class="sys-card">
          <div class="sys-card-header">
            <div>
              <div class="sys-card-title">${sysSvg("key", 18)} Secret references (${refs.length})</div>
              <p class="sys-card-desc">Declared reference patterns and TTL constraints.</p>
            </div>
          </div>

          ${refs.length ? `
            <table>
              <thead>
                <tr>
                  <th>Reference</th>
                  <th>Description</th>
                  <th>Authorized agents</th>
                  <th>Max TTL</th>
                </tr>
              </thead>
              <tbody>
                ${refs.map(r => `
                  <tr>
                    <td><code>${E(r.ref)}</code></td>
                    <td>${E(r.description || "")}</td>
                    <td><span class="badge">${E((r.allowed_agents || []).join(", ") || "any")}</span></td>
                    <td style="font-family:var(--font-mono)">${formatInterval(r.max_ttl)}</td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          ` : `
            <div class="empty">
              <div class="ico">${sysSvg("key", 24)}</div>
              <b>No secret references declared</b>
            </div>
          `}
        </div>
      ` : ""}

      <!-- Section: Audit Log -->
      ${(filter.tab === "all" || filter.tab === "audit") ? `
        <div class="sys-card">
          <div class="sys-card-header">
            <div>
              <div class="sys-card-title">${sysSvg("info", 18)} Security audit log (${filteredLogs.length})</div>
              <p class="sys-card-desc">Chronological log of credential creations, updates, and access checks.</p>
            </div>
          </div>

          ${filteredLogs.length ? `
            <div class="list">
              ${filteredLogs.slice(0, 50).map(l => {
                let detailObj = null;
                try {
                  detailObj = JSON.parse(l.detail);
                } catch (_) {
                  detailObj = null;
                }
                return `
                  <div class="row">
                    <div class="row-prefix">
                      <span class="badge info">${E(l.action)}</span>
                    </div>
                    <div class="row-content">
                      <div class="row-title">
                        <b>${E(l.target || "System")}</b>
                        <span style="font-size:var(--text-xs);color:var(--fg-muted);margin-left:8px">${K.fmt.ago(l.ts)}</span>
                      </div>
                      <div class="row-subtitle">
                        ${detailObj ? K.kv(detailObj, 80) : E(l.detail || "")}
                      </div>
                    </div>
                  </div>
                `;
              }).join("")}
            </div>
          ` : `
            <div class="empty">
              <div class="ico">${sysSvg("info", 24)}</div>
              <b>No audit entries match filter</b>
            </div>
          `}
        </div>
      ` : ""}

    </div>
  `;
};

/* ==========================================================================
   PAGE: Recurring
   Scheduled maintenance jobs, cron tasks, next run, last result, and controls.
   ========================================================================== */
V["Recurring"] = async function () {
  const r = await api("recurring");
  const query = (window.recFilterQuery || "").toLowerCase().trim();

  const filtered = (r || []).filter(x =>
    !query ||
    (x.name && x.name.toLowerCase().includes(query)) ||
    (x.cron && x.cron.toLowerCase().includes(query)) ||
    (x.task && x.task.toLowerCase().includes(query)) ||
    (x.project_id && x.project_id.toLowerCase().includes(query))
  );

  const enabledCount = (r || []).filter(x => x.enabled).length;

  return `
    <div class="sys-page">
      <div class="page-head">
        <div>
          <h2 style="margin:0;font-size:var(--text-xl);font-weight:600">Recurring schedules</h2>
          <div style="font-size:var(--text-sm);color:var(--fg-muted);margin-top:2px">
            Automated maintenance tasks, scheduled digests, and recurring workflows
          </div>
        </div>
        <div>
          <button type="button" class="btn pri" onclick="post('maintenance/run', {job: 'recurring'}).then(() => toast('Maintenance pass triggered'))">
            ${sysSvg("refresh", 14)} Run due tasks now
          </button>
        </div>
      </div>

      <!-- Metric Tiles -->
      <div class="sys-grid-stats">
        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Registered schedules</span>
            <span class="tile-badge">Total</span>
          </div>
          <div class="tile-value">${(r || []).length}</div>
          <div class="tile-hint">Active tasks in scheduler</div>
        </div>

        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Enabled</span>
            <span class="tile-badge">Status</span>
          </div>
          <div class="tile-value">${enabledCount}</div>
          <div class="tile-hint">${(r || []).length - enabledCount} disabled</div>
        </div>

        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Next upcoming run</span>
            <span class="tile-badge">Queue</span>
          </div>
          <div class="tile-value" style="font-size:var(--text-lg)">
            ${(() => {
              const upcoming = (r || []).filter(x => x.enabled && x.next_run).sort((a, b) => (a.next_run > b.next_run ? 1 : -1))[0];
              return upcoming ? K.fmt.ago(upcoming.next_run) : "None scheduled";
            })()}
          </div>
          <div class="tile-hint">Calculated from cron patterns</div>
        </div>
      </div>

      <!-- Search & Filter Bar -->
      <div class="sys-card" style="padding:var(--space-3) var(--space-4)">
        <div class="sys-filter-bar">
          <input
            class="sys-filter-input"
            id="rec-filter-in"
            type="search"
            placeholder="Search by schedule name, cron expression, or task..."
            value="${E(window.recFilterQuery || "")}"
            oninput="recSetQuery(this.value, true)">
          ${query ? `
            <button type="button" class="btn sm" onclick="recSetQuery('')">Clear search</button>
          ` : ""}
        </div>
      </div>

      <!-- Schedule Cards -->
      <div class="sys-recurring-grid">
        ${filtered.length ? filtered.map(s => `
          <div class="sys-recurring-item">
            <div class="sys-recurring-header">
              <div class="sys-recurring-title-row">
                <span class="status-dot ${s.enabled ? "live" : ""}"></span>
                <b style="font-size:var(--text-md);color:var(--fg-default)">${E(s.name)}</b>
                <span class="sys-cron-pill">${E(s.cron)}</span>
                <span class="badge ${s.enabled ? "ok" : "warn"}">${s.enabled ? "Enabled" : "Disabled"}</span>
                ${s.pattern ? `<span class="badge info">${E(s.pattern)}</span>` : ""}
                ${s.project_id ? `<span class="badge">${E(s.project_id)}</span>` : ""}
              </div>

              <div class="sys-recurring-actions">
                <button type="button" class="btn sm pri" onclick="post('maintenance/run', {job: 'recurring'}).then(() => toast(${J('Maintenance pass triggered for ' + s.name)}))">
                  Run now
                </button>
                <button type="button" class="btn sm" onclick="openRecurringDrawer(${J(s.id)}, ${J(s)})">
                  Inspect
                </button>
              </div>
            </div>

            <div class="sys-recurring-body">
              ${E((s.task || "").slice(0, 240))}${s.task && s.task.length > 240 ? "..." : ""}
            </div>

            <div class="sys-recurring-footer">
              <div class="sys-recurring-meta">
                <span><b>Next run:</b> ${s.next_run ? K.fmt.ago(s.next_run) : "Not scheduled"}</span>
                <span>·</span>
                <span><b>Last run:</b> ${s.last_run ? K.fmt.ago(s.last_run) : "Never"}</span>
                ${s.last_job ? `
                  <span>·</span>
                  <span><b>Last job:</b> <a href="#Workflow?drawer=job:${encodeURIComponent(s.last_job)}" onclick="K.drawer.openId(${J('job:' + s.last_job)});return false"><code>${E(s.last_job)}</code></a></span>
                ` : ""}
                ${s.last_status ? `
                  <span>·</span>
                  <span class="badge ok">${E(s.last_status)}</span>
                ` : ""}
              </div>

              <div style="display:flex;align-items:center;gap:var(--space-2)">
                <span style="font-size:var(--text-xs);color:var(--fg-muted)">Schedule state</span>
                <label class="switch" title="Schedule state">
                  <input type="checkbox" ${s.enabled ? "checked" : ""} onchange="toast('Schedule state updated; save changes via schedule_report')">
                  <span class="switch-slider"></span>
                </label>
              </div>
            </div>
          </div>
        `).join("") : `
          <div class="empty">
            <div class="ico">${sysSvg("clock", 24)}</div>
            <b>${query ? "No schedules match search" : "No recurring schedules registered"}</b>
            <div class="hint">
              ${query ? "Clear your search to view all schedules." : "Register recurring workflows using schedule_report or commons scheduler."}
            </div>
          </div>
        `}
      </div>
    </div>
  `;
};

/* ==========================================================================
   PAGE: System
   Background jobs status, notifications, ACL, trust scores, escalations,
   routing validation, network snippets, and calm kill switch card.
   ========================================================================== */
V["System"] = async function () {
  const [d, ov, net] = await Promise.all([
    api("system"),
    api("overview").catch(() => ({ paused: false })),
    api("network").catch(() => ({ url: "", snippets: {} })),
  ]);

  const isPaused = Boolean(ov.paused);
  const unreadNotifs = (d.notifications || []).filter(n => !n.read);
  const bgJobs = (d.background && d.background.jobs) ? Object.entries(d.background.jobs) : [];
  const snippets = (net && net.snippets) ? Object.entries(net.snippets) : [];

  return `
    <div class="sys-page">
      <div class="page-head">
        <div>
          <h2 style="margin:0;font-size:var(--text-xl);font-weight:600">System maintenance and status</h2>
          <div style="font-size:var(--text-sm);color:var(--fg-muted);margin-top:2px">
            Core daemon health, background job schedules, escalations, trust scores, and network endpoints
          </div>
        </div>
      </div>

      <!-- Hero Card: Pause All Work (Kill Switch) -->
      <div class="sys-killswitch-card ${isPaused ? "paused" : ""}">
        <div class="sys-killswitch-info">
          <div class="sys-killswitch-dot"></div>
          <div>
            <div class="sys-killswitch-title">
              ${isPaused ? "Hub execution is currently paused" : "Hub operational and running normally"}
            </div>
            <div class="sys-killswitch-desc">
              ${isPaused
                ? "All background maintenance, agent work steps, and scheduled triggers are suspended."
                : "Active background maintenance, agent messaging, and scheduled workflows are executing as configured."}
            </div>
          </div>
        </div>
        <div>
          ${isPaused ? `
            <button type="button" class="btn pri" onclick="suResumeWork()">
              ${sysSvg("play", 14)} Resume all work
            </button>
          ` : `
            <button type="button" class="btn" onclick="suPauseMenu(this)">
              ${sysSvg("pause", 14)} Pause all work
            </button>
          `}
        </div>
      </div>

      <!-- Top Status Tiles -->
      <div class="sys-grid-stats">
        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Backend database</span>
            <span class="tile-badge">${E(d.dialect || "sqlite")}</span>
          </div>
          <div class="tile-value" style="font-size:var(--text-lg)">${E(d.dialect || "SQLite")}</div>
          <div class="tile-hint">${d.encrypted ? "Encrypted at rest (SQLCipher)" : "Standard filesystem storage"}</div>
        </div>

        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Network exposure</span>
            <span class="tile-badge">${E((d.network && d.network.provider) || "local")}</span>
          </div>
          <div class="tile-value" style="font-size:var(--text-sm);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
            ${d.network && d.network.url ? `<a href="${E(d.network.url)}" target="_blank" rel="noopener">${E(d.network.url)}</a>` : "Localhost only"}
          </div>
          <div class="tile-hint">
            ${d.network && d.network.health ? (d.network.health.ok ? "Auth enforced verified" : `Status ${d.network.health.status}`) : "Local daemon"}
          </div>
        </div>

        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Unread notifications</span>
            <span class="tile-badge">${unreadNotifs.length ? "Pending" : "Clear"}</span>
          </div>
          <div class="tile-value">${unreadNotifs.length}</div>
          <div class="tile-hint">Task questions and alerts</div>
        </div>

        <div class="tile">
          <div class="tile-header">
            <span class="tile-label">Escalations</span>
            <span class="tile-badge">${(d.escalations || []).length ? "Attention" : "None"}</span>
          </div>
          <div class="tile-value">${(d.escalations || []).length}</div>
          <div class="tile-hint">Unassigned or stuck subtasks</div>
        </div>
      </div>

      <!-- Notifications & Escalations Column -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(320px, 1fr));gap:var(--space-4)">

        <!-- Notifications -->
        <div class="sys-card">
          <div class="sys-card-header">
            <div>
              <div class="sys-card-title">${sysSvg("bell", 18)} System notifications</div>
              <p class="sys-card-desc">Alerts and escalation items requiring attention.</p>
            </div>
            ${(d.notifications || []).length ? `
              <button type="button" class="btn sm" onclick="post('notifications/read', {}).then(render)">Mark all read</button>
            ` : ""}
          </div>

          ${(d.notifications || []).length ? `
            <div class="list">
              ${d.notifications.slice(0, 15).map(n => `
                <div class="row">
                  <div class="row-prefix">
                    <span class="badge ${n.level === "error" ? "bad" : n.level === "warn" ? "warn" : "info"}">${E(n.level || "info")}</span>
                  </div>
                  <div class="row-content">
                    <div class="row-title"><b>${E(n.title)}</b></div>
                    <div class="row-subtitle">${E((n.body || "").slice(0, 180))}</div>
                  </div>
                  <div style="font-size:var(--text-xs);color:var(--fg-muted)">${K.fmt.ago(n.ts)}</div>
                </div>
              `).join("")}
            </div>
          ` : `
            <div class="empty">
              <div class="ico">${sysSvg("bell", 24)}</div>
              <b>No notifications</b>
              <div class="hint">Stuck or unassignable tasks will escalate here.</div>
            </div>
          `}
        </div>

        <!-- Escalations -->
        <div class="sys-card">
          <div class="sys-card-header">
            <div>
              <div class="sys-card-title">${sysSvg("shield", 18)} Active escalations</div>
              <p class="sys-card-desc">Subtasks that exceeded retries or require intervention.</p>
            </div>
          </div>

          ${(d.escalations || []).length ? `
            <div class="list">
              ${d.escalations.map(x => `
                <div class="row">
                  <div class="row-prefix">
                    <span class="badge ${x.level === "critical" ? "bad" : "warn"}">${E(x.level || "alert")}</span>
                  </div>
                  <div class="row-content">
                    <div class="row-title"><code>${E(x.subtask_id)}</code></div>
                    <div class="row-subtitle">${E(x.detail || "Subtask stalled")}</div>
                  </div>
                </div>
              `).join("")}
            </div>
          ` : `
            <div class="empty">
              <div class="ico">${sysSvg("shield", 24)}</div>
              <b>No active escalations</b>
              <div class="hint">All subtask executions are proceeding smoothly.</div>
            </div>
          `}
        </div>

      </div>

      <!-- Background Maintenance Jobs -->
      <div class="sys-card">
        <div class="sys-card-header">
          <div>
            <div class="sys-card-title">${sysSvg("clock", 18)} Background maintenance jobs</div>
            <p class="sys-card-desc">Periodic loops running in daemon threads or via external scheduler.</p>
          </div>
          <button type="button" class="btn sm" onclick="post('maintenance/run', {}).then(() => { toast('Triggered all maintenance jobs'); render(); })">
            ${sysSvg("refresh", 14)} Run all maintenance
          </button>
        </div>

        ${bgJobs.length ? `
          <table>
            <thead>
              <tr>
                <th>Job name</th>
                <th>Frequency</th>
                <th>Last executed</th>
                <th style="text-align:right">Trigger</th>
              </tr>
            </thead>
            <tbody>
              ${bgJobs.map(([name, job]) => `
                <tr>
                  <td><b>${E(name)}</b></td>
                  <td style="color:var(--fg-muted)">Every ${formatInterval(job.every)}</td>
                  <td>${job.last ? K.fmt.ago(job.last) : '<span style="color:var(--fg-subtle)">Never</span>'}</td>
                  <td style="text-align:right">
                    <button type="button" class="btn sm" onclick="post('maintenance/run', {job: ${J(name)}}).then(() => { toast(${J(name + ' triggered')}); render(); })">
                      Run
                    </button>
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        ` : `
          <div class="empty">
            <div class="ico">${sysSvg("clock", 24)}</div>
            <b>No background jobs declared</b>
          </div>
        `}
      </div>

      <!-- Agent Trust Scores -->
      <div class="sys-card">
        <div class="sys-card-header">
          <div>
            <div class="sys-card-title">${sysSvg("shield", 18)} Agent trust scores</div>
            <p class="sys-card-desc">Calculated automatically from task outcomes and conflict resolution records.</p>
          </div>
        </div>

        ${(d.trust || []).length ? `
          <table>
            <thead>
              <tr>
                <th>Agent</th>
                <th style="min-width:140px">Score</th>
                <th>Auth method</th>
                <th>Success rate</th>
                <th>Conflict loss</th>
                <th>Manual override</th>
              </tr>
            </thead>
            <tbody>
              ${d.trust.map(t => {
                const pct = Math.round((+t.score || 0) * 100);
                return `
                  <tr>
                    <td><b>${E(t.agent)}</b></td>
                    <td>
                      <div style="display:flex;align-items:center;gap:var(--space-2)">
                        <div class="sys-trust-bar">
                          <div class="sys-trust-fill" style="width:${pct}%"></div>
                        </div>
                        <span style="font-family:var(--font-mono);font-size:var(--text-xs)">${(+t.score).toFixed(2)}</span>
                        ${t.manual != null ? '<span class="badge warn" style="font-size:10px">Pinned</span>' : ""}
                      </div>
                    </td>
                    <td style="color:var(--fg-muted)">${E(t.auth || "token")}</td>
                    <td>${t.success_rate} (${t.evidence} runs)</td>
                    <td>${t.conflict_loss || 0}</td>
                    <td>
                      <input
                        style="width:90px"
                        placeholder="0.0 - 1.0"
                        value="${t.manual != null ? t.manual : ""}"
                        onchange="post('trust/set', {agent: ${J(t.agent)}, score: this.value}).then(render)">
                    </td>
                  </tr>
                `;
              }).join("")}
            </tbody>
          </table>
        ` : `
          <div class="empty">
            <div class="ico">${sysSvg("shield", 24)}</div>
            <b>No agent trust records yet</b>
          </div>
        `}
      </div>

      <!-- Project ACLs & Routing Validation -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(320px, 1fr));gap:var(--space-4)">

        <!-- Project ACLs -->
        <div class="sys-card">
          <div class="sys-card-header">
            <div>
              <div class="sys-card-title">${sysSvg("lock", 18)} Project ACL permissions</div>
              <p class="sys-card-desc">Access control limits for sensitive repositories.</p>
            </div>
          </div>

          ${(d.acl || []).length ? `
            <table>
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Agent</th>
                  <th>Role</th>
                </tr>
              </thead>
              <tbody>
                ${d.acl.map(a => `
                  <tr>
                    <td><b>${E(a.project)}</b></td>
                    <td>${E(a.agent)}</td>
                    <td><span class="badge ok">${E(a.role)}</span></td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          ` : `
            <div style="padding:var(--space-3);background:var(--bg-surface);border-radius:var(--radius-md);font-size:var(--text-xs);color:var(--fg-muted)">
              All projects are currently open. Use <code>commons acl grant PROJECT AGENT --role read|write|admin</code> to enforce project restrictions.
            </div>
          `}
        </div>

        <!-- Routing Validation -->
        <div class="sys-card">
          <div class="sys-card-header">
            <div>
              <div class="sys-card-title">${sysSvg("circle", 18)} Routing A/B validation</div>
              <p class="sys-card-desc">Side-by-side performance checks when routing switches occur.</p>
            </div>
          </div>

          ${(d.routing_validation || []).length ? `
            <table>
              <thead>
                <tr>
                  <th>Task type</th>
                  <th>Model transition</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                ${d.routing_validation.map(r => `
                  <tr>
                    <td>${E(r.task_type)}</td>
                    <td><code>${E(r.old_model)}</code> → <code>${E(r.new_model)}</code></td>
                    <td><span class="badge info">${E(r.status || "verifying")}</span></td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          ` : `
            <div style="padding:var(--space-3);background:var(--bg-surface);border-radius:var(--radius-md);font-size:var(--text-xs);color:var(--fg-muted)">
              No active routing candidate switches are currently under validation.
            </div>
          `}
        </div>

      </div>

      <!-- Network Snippets for Connecting Clients -->
      ${snippets.length ? `
        <div class="sys-card">
          <div class="sys-card-header">
            <div>
              <div class="sys-card-title">${sysSvg("copy", 18)} Client configuration snippets</div>
              <p class="sys-card-desc">Ready to use connection configurations for IDEs, CLIs, and agents.</p>
            </div>
          </div>

          <div style="display:flex;flex-direction:column;gap:var(--space-3)">
            ${snippets.map(([name, code]) => `
              <div style="display:flex;flex-direction:column;gap:var(--space-1);background:var(--bg-surface);border:1px solid var(--border-default);border-radius:var(--radius-md);padding:var(--space-3)">
                <div style="display:flex;align-items:center;justify-content:space-between">
                  <span style="font-family:var(--font-mono);font-size:var(--text-xs);font-weight:600">${E(name)}</span>
                  <button type="button" class="btn sm" onclick="K.copy(${J(code)})">Copy snippet</button>
                </div>
                <pre style="margin:0;font-size:var(--text-xs);white-space:pre-wrap;color:var(--fg-muted);background:transparent">${E(code)}</pre>
              </div>
            `).join("")}
          </div>
        </div>
      ` : ""}

    </div>
  `;
};

})();
