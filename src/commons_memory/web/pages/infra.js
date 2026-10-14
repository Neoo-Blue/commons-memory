/* ==========================================================================
   COMMONS INFRASTRUCTURE WORKSPACE (pages/infra.js)
   Layered, zoomable infrastructure map, inventory list, ask view, and drawer.
   Vanilla JS, no build step, sentence case, no em dashes anywhere.
   ========================================================================== */

(function () {
  "use strict";

  // Register icon (24x24 SVG path, 1.6-1.75 stroke)
  ICON.Infrastructure = '<rect x="3" y="3" width="18" height="5" rx="1.5"/><rect x="3" y="11" width="18" height="5" rx="1.5"/><rect x="3" y="19" width="18" height="5" rx="1.5"/><circle cx="7" cy="5.5" r="1" fill="currentColor"/><circle cx="7" cy="13.5" r="1" fill="currentColor"/><circle cx="7" cy="21.5" r="1" fill="currentColor"/><path d="M14 5.5h3M14 13.5h3M14 21.5h3"/>';

  window.INFRA = window.INFRA || {};
  window.INFRA_STATE = window.INFRA_STATE || {
    view: "overview",
    q: "",
    zoom: "",
    sys: "",
    root: "",
    rootName: "",
    depth: "2",
    askQ: "",
    askText: "",
    jobPollTimer: null,
  };

  const S = window.INFRA_STATE;

  const KIND_COLORS = {
    machine: "#f97316",
    guest: "#3b82f6",
    service: "#22c55e",
    system: "#eab308",
    endpoint: "#a855f7",
    tunnel: "#ec4899",
    storage: "#14b8a6",
    credential: "#ef4444",
  };

  /* ---------- Helpers ---------- */
  function fmtBytes(bytes) {
    if (bytes == null || isNaN(bytes) || bytes === "") return "0 B";
    const b = Number(bytes);
    if (b <= 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB", "PB"];
    const i = Math.floor(Math.log(b) / Math.log(k));
    const val = b / Math.pow(k, i);
    const str = val >= 100 || Number.isInteger(val) ? Math.round(val) : val.toFixed(1);
    return str + " " + sizes[i];
  }

  function fmtGuestMem(mb) {
    if (!mb) return "0 MB";
    if (mb >= 1024) {
      const gb = mb / 1024;
      return (Number.isInteger(gb) ? gb : gb.toFixed(1)) + " GB";
    }
    return mb + " MB";
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

  function humanizeSource(srcStr) {
    const parts = String(srcStr || "").split(":");
    if (parts[0] === "discovery" && parts.length >= 2) {
      const collector = parts[1];
      const src = parts[2] || "";
      if (collector === "proxmox" || collector === "docker") {
        return src || collector;
      }
      if (src === "local" || !src) {
        return collector;
      }
      return src;
    }
    return srcStr;
  }

  function countMemoryFacts(overview) {
    let count = 0;
    const seen = new Set();
    function walk(n) {
      if (!n || !n.id || seen.has(n.id)) return;
      seen.add(n.id);
      const srcs = Array.isArray(n.sources) ? n.sources : [];
      if (srcs.some(s => s === "memory" || s.startsWith("memory"))) {
        count++;
      }
      if (Array.isArray(n.children)) {
        n.children.forEach(walk);
      }
    }
    (overview.tree || []).forEach(walk);
    (overview.systems || []).forEach(walk);
    return count;
  }

  function getStatusDotClass(node) {
    if (node && node.stale) return "busy";
    const st = (node && node.attrs && node.attrs.status) ? String(node.attrs.status).toLowerCase() : "";
    if (st === "running") return "live";
    return "offline";
  }

  function fmtGuestSpecs(attrs) {
    if (!attrs) return "";
    const parts = [];
    const ip = attrs.ip || (Array.isArray(attrs.ips) && attrs.ips.length ? attrs.ips[0] : null);
    if (ip) parts.push(ip);
    if (attrs.cores) {
      parts.push(attrs.cores === 1 ? "1 core" : attrs.cores + " cores");
    }
    if (attrs.memory_mb) {
      const gb = attrs.memory_mb / 1024;
      const gbStr = gb >= 1 ? (Number.isInteger(gb) ? gb + "GB" : gb.toFixed(1) + "GB") : attrs.memory_mb + "MB";
      parts.push(gbStr);
    } else if (attrs.memory) {
      parts.push(String(attrs.memory).replace(/\s*RAM$/i, ""));
    }
    return parts.join(" · ");
  }

  function truncateMiddle(str, maxLen) {
    if (!str) return "";
    maxLen = maxLen || 32;
    if (str.length <= maxLen) return str;
    const keep = Math.max(4, Math.floor((maxLen - 3) / 2));
    return str.slice(0, keep) + "..." + str.slice(-keep);
  }

  function getServicePorts(svc, gEdges, nodeMap) {
    const ports = [];
    const attrs = (svc && svc.attrs) || {};
    if (attrs.port) {
      ports.push(String(attrs.port));
    }
    if (Array.isArray(attrs.ports)) {
      attrs.ports.forEach(p => {
        const hostPart = String(p).split("->")[0].split("/")[0].trim();
        if (hostPart && !ports.includes(hostPart)) {
          ports.push(hostPart);
        }
      });
    }
    if (gEdges && nodeMap && svc.id) {
      gEdges.forEach(e => {
        if (e.src === svc.id && e.rel === "reachable_at") {
          const dst = nodeMap.get(e.dst);
          if (dst) {
            const epPort = (dst.attrs && dst.attrs.port) || (dst.name && dst.name.match(/:(\d+)(?:\/|$)/) ? dst.name.match(/:(\d+)(?:\/|$)/)[1] : null);
            if (epPort && !ports.includes(String(epPort))) {
              ports.push(String(epPort));
            }
          }
        }
      });
    }
    return ports;
  }

  function renderPortChips(ports) {
    if (!ports || !ports.length) return "";
    return ports.slice(0, 3).map(p => `<span class="infra-port-chip" title="Port ${E(p)}">${E(p)}</span>`).join("");
  }

  function getKindIcon(kind) {
    switch (kind) {
      case "place":
      case "network":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8M3.6 15h16.8M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/></svg>';
      case "machine":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h10M7 12h10M7 16h4"/></svg>';
      case "guest":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6v6H9z"/></svg>';
      case "service":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>';
      case "system":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 3v5M12 16v5M3 12h5M16 12h5"/></svg>';
      case "endpoint":
      case "tunnel":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>';
      case "storage":
      case "database":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>';
      case "credential":
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="4" y="10.5" width="16" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/></svg>';
      default:
        return '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="3" width="18" height="18" rx="2"/></svg>';
    }
  }

  /* ---------- URL & Hash Management ---------- */
  function updateUrl(params) {
    const rawHash = location.hash.slice(1);
    const qIndex = rawHash.indexOf("?");
    const qs = new URLSearchParams(qIndex >= 0 ? rawHash.slice(qIndex + 1) : "");

    Object.entries(params).forEach(([k, v]) => {
      if (v === null || v === undefined || v === "") qs.delete(k);
      else qs.set(k, v);
    });

    const str = qs.toString();
    const newHash = "#Infrastructure" + (str ? "?" + str : "");
    if (location.hash !== newHash) {
      location.hash = newHash;
    }
  }

  /* ---------- Action Handlers ---------- */
  INFRA.setView = function (v) {
    S.view = v;
    updateUrl({ view: v });
    render();
  };

  INFRA.setGraphScope = function (val) {
    S.root = val || "";
    S.rootName = "";
    updateUrl({ view: "graph", root: S.root || null, depth: S.root ? S.depth : null });
    render();
  };

  INFRA.setGraphDepth = function (d) {
    S.depth = String(d);
    updateUrl({ view: "graph", root: S.root || null, depth: String(d) });
    render();
  };

  INFRA.showInGraph = function (nodeId, nodeName) {
    S.view = "graph";
    S.root = nodeId || "";
    S.rootName = nodeName || "";
    S.depth = "2";
    updateUrl({ view: "graph", root: S.root, depth: "2" });
    if (typeof tab !== "undefined" && tab !== "Infrastructure") {
      go("Infrastructure");
    } else {
      render();
    }
  };

  INFRA.setSystemFilter = function (sys) {
    S.sys = S.sys === sys ? "" : sys;
    updateUrl({ sys: S.sys });
    render();
  };

  INFRA.zoom = function (nodeId) {
    S.zoom = nodeId || "";
    updateUrl({ zoom: S.zoom });
    render();
  };

  INFRA.onSearch = function (q) {
    S.q = String(q || "").trim();
    if (S.view === "list") {
      INFRA.filterList(S.q);
    } else if (S.view === "map") {
      INFRA.highlightMap(S.q);
    } else if (S.view === "overview") {
      INFRA.highlightOverview(S.q);
    }
  };

  INFRA.highlightOverview = function (q) {
    const term = (q || "").toLowerCase();
    const items = document.querySelectorAll(".infra-machine-cap-card, .infra-storage-row");
    items.forEach(c => {
      if (!term) {
        c.style.display = "";
        return;
      }
      const text = (c.textContent || "").toLowerCase();
      c.style.display = text.includes(term) ? "" : "none";
    });
  };

  /* Listen for graph explorer commons:open node request */
  if (!window._infraMessageBound) {
    window._infraMessageBound = true;
    window.addEventListener(
      "message",
      (ev) => {
        if (ev.origin !== location.origin) return;
        const d = ev.data;
        if (!d || typeof d !== "object" || d.type !== "commons:open" || typeof d.id !== "string" || !d.id) return;
        const rawHash = location.hash.slice(1);
        const page = rawHash.split("?")[0];
        if (page === "Infrastructure") {
          ev.stopImmediatePropagation();
          if (window.K && window.K.drawer && window.K.drawer.openId) {
            window.K.drawer.openId("infra:" + d.id);
          }
        }
      },
      true
    );
  }

  INFRA.filterList = function (q) {
    const rows = document.querySelectorAll(".infra-table tbody tr");
    const term = (q || "").toLowerCase();
    rows.forEach(r => {
      const match = !term || (r.dataset.search || "").includes(term);
      r.style.display = match ? "" : "none";
    });
  };

  INFRA.highlightMap = function (q) {
    const term = (q || "").toLowerCase();
    const items = document.querySelectorAll(".infra-guest-card, .infra-machine-card, .infra-svc-chip-row, .infra-host-chip");
    items.forEach(c => {
      if (!term) {
        c.classList.remove("infra-highlighted", "infra-dimmed");
        return;
      }
      const text = (c.dataset.search || "").toLowerCase();
      if (text.includes(term)) {
        c.classList.add("infra-highlighted");
        c.classList.remove("infra-dimmed");
        const moreBox = c.closest(".infra-services-more");
        if (moreBox) {
          moreBox.style.display = "flex";
          const toggleBtn = moreBox.nextElementSibling;
          if (toggleBtn && toggleBtn.classList.contains("infra-more-btn")) {
            toggleBtn.textContent = "Show less";
          }
        }
      } else {
        c.classList.remove("infra-highlighted");
        c.classList.add("infra-dimmed");
      }
    });
  };

  INFRA.toggleMore = function (btn, evt) {
    if (evt) evt.stopPropagation();
    const card = btn.closest(".infra-guest-card");
    if (!card) return;
    const moreBox = card.querySelector(".infra-services-more");
    if (!moreBox) return;
    const isHidden = moreBox.style.display === "none";
    moreBox.style.display = isHidden ? "flex" : "none";
    const count = moreBox.dataset.count || "more";
    btn.textContent = isHidden ? "Show less" : `+${count} more`;
  };

  INFRA.ask = async function (q) {
    S.askQ = q !== undefined ? q : (document.getElementById("infra-ask-input") ? document.getElementById("infra-ask-input").value : "");
    const box = document.getElementById("infra-ask-result");
    if (box) box.innerHTML = window.K && window.K.skeleton ? window.K.skeleton(4) : '<div class="mut sm">Loading...</div>';

    try {
      const res = await api("infra_describe?q=" + encodeURIComponent(S.askQ));
      S.askText = res && res.text ? res.text : "No description returned.";
      if (box) {
        box.innerHTML = `<pre class="infra-ask-pre">${E(S.askText)}</pre>`;
      }
    } catch (e) {
      if (box) box.innerHTML = `<div class="mut sm">Error: ${E(e && e.message ? e.message : e)}</div>`;
    }
  };

  INFRA.buildFromMemory = async function () {
    let est;
    try {
      est = await api("infra_estimate?full=1");
    } catch (e) {
      if (window.K && window.K.toast) window.K.toast("Could not get extraction estimate: " + (e && e.message ? e.message : e), { kind: "err" });
      return;
    }

    const noteCount = est.notes || 0;
    const batchCount = est.batches || 0;
    const costEst = est.estimate_usd ? Number(est.estimate_usd).toFixed(2) : "0.00";

    const ok = confirm(`Scan ${noteCount} memory notes across ${batchCount} batches? Estimated model cost is $${costEst}. This runs as a background task.`);
    if (!ok) return;

    try {
      const r = await postRaw("infra/extract", { full: true });
      if (r && r.error) {
        if (window.K && window.K.toast) window.K.toast(r.error, { kind: "err" });
        return;
      }
      if (window.K && window.K.toast) window.K.toast("Extraction started in background", { kind: "ok" });
      render();
    } catch (e) {
      if (window.K && window.K.toast) window.K.toast("Could not start extraction: " + (e && e.message ? e.message : e), { kind: "err" });
    }
  };

  INFRA.pollJob = async function () {
    try {
      const res = await api("infra_overview");
      if (res && res.job) {
        const j = res.job;
        const banner = document.getElementById("infra-job-banner");
        if (banner) {
          if (j.status === "running") {
            const pct = j.total > 0 ? Math.round((j.done / j.total) * 100) : 0;
            const bar = document.getElementById("infra-job-bar");
            const lbl = document.getElementById("infra-job-lbl");
            if (bar) bar.style.width = pct + "%";
            if (lbl) lbl.textContent = `Batch ${j.done} of ${j.total} (${pct}%) · $${(j.cost || 0).toFixed(2)}`;
          } else {
            clearInterval(S.jobPollTimer);
            S.jobPollTimer = null;
            render();
          }
        } else if (j.status === "running") {
          render();
        }
      } else {
        clearInterval(S.jobPollTimer);
        S.jobPollTimer = null;
      }
    } catch (e) {
      // quiet poll error
    }
  };

  /* ---------- Command Palette & Keys Setup ---------- */
  if (window.K && window.K.action) {
    K.action({
      id: "infra",
      label: "Infrastructure",
      keywords: "infrastructure map proxmox hosts containers lxc vm servers services",
      run: () => go("Infrastructure"),
    });
    K.action({
      id: "infra-find",
      label: "Find in infrastructure...",
      keywords: "find search lookup infrastructure hosts containers ips ports",
      run: () => {
        go("Infrastructure");
        setTimeout(() => {
          const inp = document.getElementById("infra-search-input");
          if (inp) inp.focus();
        }, 80);
      },
    });
  }

  /* ---------- Node Drawer (infra:<id>) ---------- */
  if (window.K && window.K.drawer && window.K.drawer.on) {
    window.K.drawer.on("infra:", (rawId) => {
      const id = String(rawId || "").slice(6);
      INFRA.openNodeDrawer(id);
    });
  }

  INFRA.openNodeDrawer = function (nid) {
    if (!nid || !window.K || !window.K.drawer) return;

    window.K.drawer.open({
      id: "infra:" + nid,
      title: "Loading...",
      subtitle: "Infrastructure node",
      actions: [
        {
          label: "Show in graph",
          kind: "primary",
          run: (ctx) => {
            ctx.close();
            INFRA.showInGraph(nid);
          },
        },
        {
          label: "Same as...",
          kind: "default",
          run: async (ctx) => {
            const target = prompt("Which node is this the same as? A name, IP, vmid or URL:");
            if (!target || !target.trim()) return;
            let keep;
            try { keep = (await api("infra_node?id=" + encodeURIComponent(target.trim()))).id; }
            catch (e) { if (window.K && window.K.toast) window.K.toast("Nothing in the map matches " + target.trim(), { kind: "err" }); return; }
            if (keep === nid) return;
            const r = await postRaw("infra/merge", { keep, drop: nid });
            if (r && r.error) {
              if (window.K && window.K.toast) window.K.toast(r.error, { kind: "err" });
              return;
            }
            if (window.K && window.K.toast) window.K.toast("Nodes merged", { kind: "ok" });
            ctx.close();
            render();
            if (r && r.id) {
              setTimeout(() => K.drawer.openId("infra:" + r.id), 200);
            }
          },
        },
        {
          label: "Hide node",
          kind: "bad",
          run: async (ctx) => {
            const ok = confirm("Hide this node from the infrastructure map?");
            if (!ok) return;
            const r = await postRaw("infra/hide", { id: nid, hidden: true });
            if (r && r.error) {
              if (window.K && window.K.toast) window.K.toast(r.error, { kind: "err" });
              return;
            }
            if (window.K && window.K.toast) window.K.toast("Node hidden", { kind: "ok" });
            ctx.close();
            render();
          },
        },
      ],
      render: async (ctx) => {
        let node;
        try {
          node = await api("infra_node?id=" + encodeURIComponent(nid));
        } catch (e) {
          return `<div class="empty"><div class="mut">Could not load node details: ${E(e && e.message ? e.message : e)}</div></div>`;
        }
        if (!node || !node.id) {
          return '<div class="empty"><div class="mut">Node not found.</div></div>';
        }

        const headTitle = ctx.el.querySelector(".drawer-title");
        const headSub = ctx.el.querySelector(".drawer-sub");
        if (headTitle) headTitle.textContent = node.name;
        if (headSub) {
          headSub.textContent = node.kind + (node.note ? " · " + node.note : "");
          headSub.hidden = false;
        }

        const sources = Array.isArray(node.sources) ? node.sources : [];
        const attrs = node.attrs || {};
        const chain = Array.isArray(node.chain) ? node.chain : [];
        const outLinks = Array.isArray(node.out) ? node.out : [];
        const inLinks = Array.isArray(node.in) ? node.in : [];
        const notes = Array.isArray(node.notes) ? node.notes : [];
        const projects = Array.isArray(node.projects) ? node.projects : [];
        const siblings = Array.isArray(node.siblings) ? node.siblings : [];
        const hostName = (chain.length > 0 && chain[0].name) ? chain[0].name : "the host";

        // Group relations
        const relGroups = {};
        outLinks.forEach(l => {
          const groupName = l.label || l.rel || "linked to";
          relGroups[groupName] = relGroups[groupName] || [];
          relGroups[groupName].push({ ...l, direction: "out" });
        });
        inLinks.forEach(l => {
          let groupName = "linked from";
          if (l.rel === "runs_on") groupName = "hosts";
          else if (l.rel === "part_of") groupName = "has part";
          else if (l.rel === "mounts") groupName = "mounted by";
          else if (l.rel === "talks_to") groupName = "used by";
          else if (l.rel === "uses_credential") groupName = "credential for";
          else if (l.rel === "reachable_at") groupName = "endpoint of";
          else if (l.rel === "stores_in") groupName = "holds data of";
          else if (l.rel === "exposed_via") groupName = "exposes";
          else if (l.label) groupName = l.label;
          relGroups[groupName] = relGroups[groupName] || [];
          relGroups[groupName].push({ ...l, direction: "in" });
        });

        // Filter and display key attrs
        const cleanAttrs = {};
        Object.entries(attrs).forEach(([k, v]) => {
          if (v !== null && v !== "" && v !== undefined && k !== "host") {
            cleanAttrs[k] = v;
          }
        });

        return `<div class="infra-drawer-content">
          <!-- Badges & Status Strip -->
          <div class="infra-drawer-meta-row">
            <span class="badge sm">${E(node.kind)}</span>
            ${sources.map(s => {
              const isDisc = s.startsWith("discovery");
              const isVault = s.startsWith("vault");
              const cls = isDisc ? "ok" : isVault ? "accent" : "info";
              return `<span class="badge sm ${cls}">${E(s)}</span>`;
            }).join("")}
            ${node.stale ? `<span class="badge sm warn">stale · not seen since ${E(fmtAgo(node.last_seen))}</span>` : ""}
            ${node.last_seen && !node.stale ? `<span class="mut sm">seen ${E(fmtAgo(node.last_seen))}</span>` : ""}
            <button type="button" class="btn sm" onclick="if(window.K&&window.K.drawer)K.drawer.close();INFRA.showInGraph(${J(node.id)}, ${J(node.name)})" style="display:inline-flex;align-items:center;gap:4px">
              <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="3"/><circle cx="5" cy="12" r="2"/><circle cx="19" cy="12" r="2"/><path d="M7 12h3M14 12h3"/></svg>
              Show in graph
            </button>
          </div>

          <!-- Runs On Chain -->
          ${chain.length ? `
            <div class="infra-drawer-chain">
              <span class="infra-drawer-chain-lbl">Runs on:</span>
              ${chain.map((c, i) => `
                <button type="button" class="infra-drawer-chain-btn" onclick="K.drawer.openId(${J('infra:' + c.id)})">
                  ${E(c.name)} <span style="opacity:0.6;font-size:10px">(${E(c.kind)})</span>
                </button>
                ${i < chain.length - 1 ? '<span style="color:var(--fg-subtle)">›</span>' : ""}
              `).join("")}
            </div>
          ` : ""}

          <!-- Projects Section -->
          ${projects.length ? `
            <div class="infra-drawer-section">
              <span class="infra-drawer-section-title">Projects (${projects.length})</span>
              <div class="infra-drawer-chips">
                ${projects.map(p => `
                  <a href="#Projects?p=${encodeURIComponent(p.id)}" class="infra-project-chip" onclick="if(window.K&&window.K.drawer)K.drawer.close();if(typeof go==='function')go('Projects');">
                    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>
                    <span style="font-weight:500">${E(p.title || p.id)}</span>
                    ${p.notes ? `<span class="badge sm" style="font-size:10px">${p.notes} ${p.notes === 1 ? 'note' : 'notes'}</span>` : ''}
                  </a>
                `).join("")}
              </div>
            </div>
          ` : ""}

          <!-- Sibling Nodes (Also on host) -->
          ${siblings.length ? `
            <div class="infra-drawer-section">
              <span class="infra-drawer-section-title">Also on ${E(hostName)} (${siblings.length})</span>
              <div class="infra-drawer-chips">
                ${siblings.map(s => `
                  <button type="button" class="infra-sibling-chip" onclick="K.drawer.openId(${J('infra:' + s.id)})">
                    ${getKindIcon(s.kind)}
                    <span style="font-weight:500">${E(s.name)}</span>
                    <span class="badge sm" style="font-size:10px">${E(s.kind)}</span>
                  </button>
                `).join("")}
              </div>
            </div>
          ` : ""}

          <!-- Attributes Key-Value List -->
          ${Object.keys(cleanAttrs).length ? `
            <div class="infra-drawer-section">
              <span class="infra-drawer-section-title">Attributes</span>
              ${window.K && window.K.kv ? window.K.kv(cleanAttrs) : `
                <div class="kv">
                  ${Object.entries(cleanAttrs).map(([k, v]) => `
                    <span class="kv-pill"><span class="kv-k">${E(k)}</span><span class="kv-v">${E(typeof v === 'object' ? JSON.stringify(v) : v)}</span></span>
                  `).join("")}
                </div>
              `}
            </div>
          ` : ""}

          <!-- Links Grouped by Relation -->
          ${Object.keys(relGroups).length ? `
            <div class="infra-drawer-section">
              <span class="infra-drawer-section-title">Connections</span>
              ${Object.entries(relGroups).map(([groupName, links]) => `
                <div class="infra-rel-group">
                  <span class="infra-rel-title">${E(groupName)}</span>
                  ${links.map(l => {
                    const isCred = l.kind === "credential";
                    return `<div class="infra-drawer-link-row" id="edge-row-${E(l.edge)}">
                      <div class="infra-conn-info" onclick="if(!event.target.closest('a,button')){K.drawer.openId(${J('infra:' + l.id)})}">
                        ${getKindIcon(l.kind)}
                        ${isCred ? `
                          <span><b>${E(l.name)}</b> (${E(l.attrs && l.attrs.service ? l.attrs.service : 'vault')})</span>
                          <span class="mut sm">·</span>
                          <a href="#Secrets" class="mut sm" onclick="go('Secrets');return false" style="text-decoration:underline">Secrets page</a>
                        ` : `
                          <button type="button" class="btn sm" onclick="K.drawer.openId(${J('infra:' + l.id)})">
                            ${E(l.name)} <span class="badge sm">${E(l.kind)}</span>
                          </button>
                        `}
                        ${l.stale ? `<span class="badge sm warn">stale</span>` : ""}
                        ${l.verdict === "yes" ? `<span class="badge sm ok" title="Confirmed link">✓ confirmed</span>` : ""}
                      </div>
                      <div class="infra-verdict-actions">
                        <button type="button" class="infra-verdict-btn ok" title="Confirm link" onclick="INFRA.setVerdict(${J(l.edge)}, true, ${J(nid)})">✓</button>
                        <button type="button" class="infra-verdict-btn bad" title="Reject link" onclick="INFRA.setVerdict(${J(l.edge)}, false, ${J(nid)})">✕</button>
                      </div>
                    </div>`;
                  }).join("")}
                </div>
              `).join("")}
            </div>
          ` : ""}

          <!-- Evidence Notes List -->
          ${notes.length ? `
            <div class="infra-drawer-section">
              <span class="infra-drawer-section-title">Evidence from notes (${notes.length})</span>
              <div class="infra-evidence-list">
                ${notes.map(n => `
                  <div class="infra-evidence-row" onclick="K.drawer.openId(${J('mem:' + n.id)})">
                    <div class="infra-evidence-head">
                      <span>Note ${E(n.id)}</span>
                      <span>${E(fmtAgo(n.at))}</span>
                    </div>
                    <div class="infra-evidence-snippet">${E(n.text)}</div>
                  </div>
                `).join("")}
              </div>
            </div>
          ` : ""}
        </div>`;
      },
    });
  };

  INFRA.setVerdict = function (edgeId, ok, parentNid) {
    const row = document.getElementById("edge-row-" + edgeId);
    const prevDisplay = row ? row.style.display : "";

    if (window.K && window.K.optimistic) {
      window.K.optimistic({
        label: ok ? "Link confirmed" : "Link rejected",
        apply: () => {
          if (row && !ok) row.style.display = "none";
        },
        revert: () => {
          if (row) row.style.display = prevDisplay;
        },
        commit: async () => {
          const r = await postRaw("infra/verdict", { edge: edgeId, ok });
          if (r && r.error) {
            if (window.K && window.K.toast) window.K.toast(r.error, { kind: "err" });
            return r;
          }
          if (parentNid) {
            setTimeout(() => INFRA.openNodeDrawer(parentNid), 300);
          }
          return r;
        },
      });
    } else {
      postRaw("infra/verdict", { edge: edgeId, ok }).then(r => {
        if (r && r.error) {
          if (window.K && window.K.toast) window.K.toast(r.error, { kind: "err" });
        } else {
          if (window.K && window.K.toast) window.K.toast(ok ? "Link confirmed" : "Link rejected", { kind: "ok" });
          if (parentNid) INFRA.openNodeDrawer(parentNid);
        }
      });
    }
  };

  /* ---------- Guest Card Renderer ---------- */
  function renderGuestCard(g, activeSys) {
    const gAttrs = g.attrs || {};
    const gServices = g.children || [];
    const gMemOnly = !(g.sources || []).some(s => s.startsWith("discovery"));
    const gHighlighted = activeSys && gServices.some(s => s.name.toLowerCase().includes(activeSys.toLowerCase()));
    const gDimmed = activeSys && !gHighlighted;
    const isStopped = gAttrs.status && String(gAttrs.status).toLowerCase() === "stopped";
    const specsText = fmtGuestSpecs(gAttrs);

    const visibleServices = gServices.slice(0, 8);
    const hiddenServices = gServices.slice(8);

    return `<div class="infra-guest-card ${g.stale ? 'stale' : ''} ${isStopped ? 'stopped' : ''} ${gHighlighted ? 'infra-highlighted' : ''} ${gDimmed ? 'infra-dimmed' : ''}" data-id="${E(g.id)}" data-search="${E(g.name + ' ' + (gAttrs.ip || '') + ' ' + (gAttrs.vmid || '') + ' ' + (specsText || ''))}">
      <div class="infra-guest-head">
        <div class="infra-guest-title-wrap" onclick="INFRA.zoom(${J(g.id)})">
          <span class="status-dot ${getStatusDotClass(g)}"></span>
          <span class="infra-guest-title">${E(g.name)}</span>
          ${specsText ? `<span class="infra-guest-specs mut sm">${E(specsText)}</span>` : ""}
          ${gMemOnly ? `<span class="badge sm info" title="Discovered from memory notes">from memory</span>` : ""}
          ${g.stale ? `<span class="badge sm warn">stale</span>` : ""}
        </div>
        <div class="infra-guest-actions">
          <button type="button" class="infra-guest-btn" onclick="INFRA.zoom(${J(g.id)})">Zoom</button>
          <button type="button" class="infra-guest-btn" onclick="K.drawer.openId(${J('infra:' + g.id)})">Inspect</button>
        </div>
      </div>

      ${g.stale && g.last_seen ? `
        <div class="mut sm" style="font-size:11px">Not seen since ${E(fmtAgo(g.last_seen))}</div>
      ` : ""}

      <!-- Services inside Guest -->
      ${gServices.length ? `
        <div class="infra-services-list">
          ${visibleServices.map(svc => renderServiceChipRow(svc, activeSys)).join("")}
          ${hiddenServices.length ? `
            <div class="infra-services-more" style="display:none" data-count="${hiddenServices.length}">
              ${hiddenServices.map(svc => renderServiceChipRow(svc, activeSys)).join("")}
            </div>
            <button type="button" class="infra-more-btn" onclick="INFRA.toggleMore(this, event)">+${hiddenServices.length} more</button>
          ` : ""}
        </div>
      ` : `<div class="mut sm" style="padding:4px 0">No services running</div>`}
    </div>`;
  }

  function renderServiceChipRow(svc, activeSys, gEdges, nodeMap) {
    const sAttrs = svc.attrs || {};
    const sHighlighted = activeSys && svc.name.toLowerCase().includes(activeSys.toLowerCase());
    const sMemOnly = !(svc.sources || []).some(s => s.startsWith("discovery"));
    const ports = getServicePorts(svc, gEdges, nodeMap);

    return `<div class="infra-svc-chip-row ${sHighlighted ? 'infra-highlighted' : ''}" data-id="${E(svc.id)}" data-search="${E(svc.name + ' ' + (ports.join(' ') || '') + ' ' + (sAttrs.image || ''))}" onclick="K.drawer.openId(${J('infra:' + svc.id)})">
      <div class="infra-svc-name">
        ${getKindIcon(svc.kind)}
        <span>${E(svc.name)}</span>
        ${renderPortChips(ports)}
        ${sMemOnly ? `<span class="badge sm info" style="font-size:9.5px;padding:0 4px">memory</span>` : ""}
        ${svc.stale ? `<span class="badge sm warn" style="font-size:9.5px;padding:0 4px">stale</span>` : ""}
      </div>
    </div>`;
  }

  /* ==========================================================================
     PAGE: Infrastructure (V["Infrastructure"])
     ========================================================================== */
  V["Infrastructure"] = async function () {
    const rawHash = location.hash.slice(1);
    const qIndex = rawHash.indexOf("?");
    const qs = new URLSearchParams(qIndex >= 0 ? rawHash.slice(qIndex + 1) : "");

    S.view = qs.get("view") || "overview";
    S.q = qs.get("q") || "";
    S.zoom = qs.get("zoom") || "";
    S.sys = qs.get("sys") || "";
    S.root = qs.get("root") || "";
    S.depth = qs.get("depth") || "2";

    let overview;
    try {
      overview = await api("infra_overview");
    } catch (e) {
      return `<div class="infra-page">
        <div class="empty">
          <b>Could not load infrastructure</b>
          <div class="hint">${E(e && e.message ? e.message : e)}</div>
          <div class="empty-actions"><button class="pri" onclick="render()">Try again</button></div>
        </div>
      </div>`;
    }

    overview = overview || {};
    const counts = overview.counts || {};
    const systems = overview.systems || [];
    const tree = overview.tree || [];
    const sources = overview.sources || [];
    const job = overview.job;

    // Check if background extraction is in progress
    if (job && job.status === "running") {
      if (!S.jobPollTimer) {
        S.jobPollTimer = setInterval(INFRA.pollJob, 3000);
      }
    } else if (S.jobPollTimer) {
      clearInterval(S.jobPollTimer);
      S.jobPollTimer = null;
    }

    // Build freshness subtitle
    let freshnessHtml = "";
    if (sources.length) {
      const srcItems = sources.map(s => `${humanizeSource(s.source)} ${fmtAgo(s.at)}`);
      freshnessHtml = `<span class="dot"></span><span>Discovered from ${E(srcItems.join(" · "))}</span>`;
    }
    const memFacts = countMemoryFacts(overview);
    if (memFacts > 0) {
      freshnessHtml += `${sources.length ? ' <span class="mut">·</span> ' : '<span class="dot"></span>'}<span>${memFacts} facts from memory</span>`;
    }
    if (!freshnessHtml && overview.watermark) {
      freshnessHtml = `<span class="dot"></span><span>Knowledge synced ${E(fmtAgo(overview.watermark))}</span>`;
    }
    if (!freshnessHtml) {
      freshnessHtml = '<span>No sources connected yet</span>';
    }

    const hasAnyNodes = tree.length > 0 || Object.keys(counts).length > 0;

    let out = `<div class="infra-page">
      <!-- Head Section -->
      <div class="infra-head">
        <div class="infra-head-top">
          <div class="infra-head-titles">
            <h2 class="infra-head-title">Infrastructure</h2>
            <div class="infra-freshness">${freshnessHtml}</div>
          </div>
          <div class="infra-head-controls">
            ${hasAnyNodes ? `
              <div class="infra-search-box">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
                <input type="search" id="infra-search-input" class="infra-search-input" value="${E(S.q)}" placeholder="Search name, IP, vmid, URL..." autocomplete="off" oninput="INFRA.onSearch(this.value)">
              </div>
            ` : ""}
            <div class="seg" role="tablist">
              <button type="button" class="seg-btn ${S.view === "overview" ? "on" : ""}" role="tab" aria-selected="${S.view === "overview"}" onclick="INFRA.setView('overview')">Overview</button>
              <button type="button" class="seg-btn ${S.view === "map" ? "on" : ""}" role="tab" aria-selected="${S.view === "map"}" onclick="INFRA.setView('map')">Map</button>
              <button type="button" class="seg-btn ${S.view === "graph" ? "on" : ""}" role="tab" aria-selected="${S.view === "graph"}" onclick="INFRA.setView('graph')">Graph</button>
              <button type="button" class="seg-btn ${S.view === "list" ? "on" : ""}" role="tab" aria-selected="${S.view === "list"}" onclick="INFRA.setView('list')">List</button>
              <button type="button" class="seg-btn ${S.view === "ask" ? "on" : ""}" role="tab" aria-selected="${S.view === "ask"}" onclick="INFRA.setView('ask')">Ask</button>
            </div>
          </div>
        </div>

        <!-- Background Job Banner -->
        ${job && job.status === "running" ? `
          <div class="infra-job-banner" id="infra-job-banner">
            <div class="infra-job-top">
              <span>Building infrastructure map from memory...</span>
              <span id="infra-job-lbl">Batch ${job.done} of ${job.total} · $${(job.cost || 0).toFixed(2)}</span>
            </div>
            <div class="infra-job-progress">
              <div class="infra-job-bar" id="infra-job-bar" style="width:${job.total > 0 ? Math.round((job.done / job.total) * 100) : 0}%"></div>
            </div>
          </div>
        ` : ""}
      </div>`;

    // View Content
    if (!hasAnyNodes && S.view !== "ask") {
      out += renderEmptyState();
    } else if (S.view === "overview") {
      out += await renderOverviewView(overview);
    } else if (S.view === "map") {
      out += await renderMapView(overview, S.zoom, S.sys);
    } else if (S.view === "graph") {
      out += renderGraphView(overview, S.root, S.depth);
    } else if (S.view === "list") {
      out += await renderListView(S.q);
    } else if (S.view === "ask") {
      out += renderAskView(S.askQ, S.askText);
    }

    out += `</div>`;
    return out;
  };

  /* ---------- Empty State ---------- */
  function renderEmptyState() {
    return `<div class="infra-empty-wrap">
      <div class="infra-empty-hero">
        <div class="infra-empty-hero-icon">
          <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h10M7 12h10M7 16h4"/></svg>
        </div>
        <h3 class="infra-empty-hero-title">Infrastructure map is empty</h3>
        <p class="infra-empty-hero-sub">The knowledge base connects hosts, containers, services, storage and vault credentials so agents and humans share the full picture.</p>
      </div>

      <div class="infra-empty-cards">
        <div class="infra-empty-card">
          <div class="infra-empty-card-head">
            <div class="infra-empty-card-icon">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
            </div>
            <span class="infra-empty-card-title">Build from memory</span>
          </div>
          <p class="infra-empty-card-desc">Scan existing notes, decisions and handoffs with AI to construct topology, container ports and relations.</p>
          <div style="margin-top:auto">
            <button type="button" class="pri" style="width:100%" onclick="INFRA.buildFromMemory()">Build from memory</button>
          </div>
        </div>

        <div class="infra-empty-card">
          <div class="infra-empty-card-head">
            <div class="infra-empty-card-icon">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h10M7 12h10M7 16h4"/></svg>
            </div>
            <span class="infra-empty-card-title">Discover live</span>
          </div>
          <p class="infra-empty-card-desc">Run the collector on a machine that reaches your hypervisors to inspect Proxmox, Docker and systemd.</p>
          <div class="infra-code-box">
            <code>commons infra collect --submit</code>
            <button type="button" class="btn sm" onclick="K.copy('commons infra collect --submit')">Copy</button>
          </div>
        </div>
      </div>
    </div>`;
  }

  /* ---------- Overview View (Charts & KPIs) ---------- */
  async function renderOverviewView(overview) {
    let stats;
    try {
      stats = await api("infra_stats");
    } catch (e) {
      return `<div class="empty"><div class="mut">Could not load infrastructure stats: ${E(e && e.message ? e.message : e)}</div></div>`;
    }

    stats = stats || {};
    const machines = stats.machines || [];
    const storage = stats.storage || [];
    const counts = stats.counts || {};
    const staleCount = stats.stale || 0;

    // Tally guest running vs stopped
    let runningGuests = 0;
    let stoppedGuests = 0;
    machines.forEach(m => {
      (m.guests || []).forEach(g => {
        if (g.status && String(g.status).toLowerCase() === "stopped") {
          stoppedGuests++;
        } else {
          runningGuests++;
        }
      });
    });

    const totalGuests = counts.guest || (runningGuests + stoppedGuests);
    const totalServices = counts.service || 0;
    const totalEndpoints = (counts.endpoint || 0) + (counts.tunnel || 0);
    const totalStoragePools = storage.length || counts.storage || 0;
    const totalMachines = counts.machine || machines.length;

    // Total storage capacity
    const totalStorageBytes = storage.reduce((acc, s) => acc + (s.total || 0), 0);

    // Distribution bar strip
    const totalNodes = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
    const kindOrder = ["machine", "guest", "service", "system", "endpoint", "tunnel", "storage", "credential"];
    Object.keys(counts).forEach(k => {
      if (!kindOrder.includes(k)) kindOrder.push(k);
    });
    const stripKinds = kindOrder.filter(k => (counts[k] || 0) > 0);

    let out = `<div class="infra-overview-view">
      <!-- KPI Tiles -->
      <div class="infra-kpi-grid">
        <div class="infra-kpi-card" onclick="INFRA.setView('map')">
          <div class="infra-kpi-head">
            <span class="infra-kpi-lbl">Machines</span>
            ${getKindIcon('machine')}
          </div>
          <div class="infra-kpi-val">${totalMachines}</div>
          <div class="infra-kpi-sub mut sm">${totalMachines === 1 ? '1 physical or virtual host' : totalMachines + ' physical or virtual hosts'}</div>
        </div>

        <div class="infra-kpi-card" onclick="INFRA.setView('map')">
          <div class="infra-kpi-head">
            <span class="infra-kpi-lbl">Guests</span>
            ${getKindIcon('guest')}
          </div>
          <div class="infra-kpi-val">${totalGuests}</div>
          <div class="infra-kpi-sub mut sm">
            <span class="infra-pill-live">${runningGuests} running</span>
            <span class="infra-crumb-sep">·</span>
            <span class="infra-pill-stopped">${stoppedGuests} stopped</span>
          </div>
        </div>

        <div class="infra-kpi-card" onclick="INFRA.setView('list')">
          <div class="infra-kpi-head">
            <span class="infra-kpi-lbl">Services</span>
            ${getKindIcon('service')}
          </div>
          <div class="infra-kpi-val">${totalServices}</div>
          <div class="infra-kpi-sub mut sm">Across all machines and guests</div>
        </div>

        <div class="infra-kpi-card" onclick="INFRA.setView('list')">
          <div class="infra-kpi-head">
            <span class="infra-kpi-lbl">Endpoints</span>
            ${getKindIcon('endpoint')}
          </div>
          <div class="infra-kpi-val">${totalEndpoints}</div>
          <div class="infra-kpi-sub mut sm">${counts.tunnel ? counts.tunnel + ' tunnels · ' : ''}${counts.endpoint || 0} reachable URLs</div>
        </div>

        <div class="infra-kpi-card">
          <div class="infra-kpi-head">
            <span class="infra-kpi-lbl">Storage pools</span>
            ${getKindIcon('storage')}
          </div>
          <div class="infra-kpi-val">${totalStoragePools}</div>
          <div class="infra-kpi-sub mut sm">${fmtBytes(totalStorageBytes)} total capacity</div>
        </div>

        <div class="infra-kpi-card ${staleCount > 0 ? 'warn' : ''}">
          <div class="infra-kpi-head">
            <span class="infra-kpi-lbl">Stale items</span>
            <span class="status-dot ${staleCount > 0 ? 'busy' : 'live'}"></span>
          </div>
          <div class="infra-kpi-val ${staleCount > 0 ? 'infra-warn-text' : ''}">${staleCount}</div>
          <div class="infra-kpi-sub mut sm">${staleCount > 0 ? 'Not seen in recent collector runs' : 'All items recently verified'}</div>
        </div>
      </div>

      <!-- Counts by Kind Compact Bar Strip -->
      <div class="card infra-strip-card">
        <div class="infra-strip-head">
          <span class="infra-section-title">Counts by kind (${totalNodes} nodes)</span>
          <button type="button" class="btn sm" onclick="INFRA.setView('list')">View list</button>
        </div>
        <div class="infra-bar-strip">
          ${stripKinds.map(k => {
            const c = counts[k] || 0;
            const pct = Math.max(0.5, ((c / totalNodes) * 100)).toFixed(1);
            const color = KIND_COLORS[k] || '#64748b';
            return `<div class="infra-strip-seg" style="width:${pct}%;background:${color}" title="${E(k)}: ${c} (${pct}%)"></div>`;
          }).join("")}
        </div>
        <div class="infra-strip-legend">
          ${stripKinds.map(k => {
            const c = counts[k] || 0;
            const pct = Math.round((c / totalNodes) * 100);
            const color = KIND_COLORS[k] || '#64748b';
            return `<div class="infra-strip-chip">
              <span class="infra-legend-dot" style="background:${color}"></span>
              <span class="infra-strip-kind">${E(k)}</span>
              <span class="infra-strip-num infra-num">${c}</span>
              <span class="infra-strip-pct mut sm">(${pct}%)</span>
            </div>`;
          }).join("")}
        </div>
      </div>

      <!-- Capacity Cards per Machine -->
      <div class="infra-machines-section">
        <div class="infra-section-header">
          <span class="infra-section-title">Machine capacity & guest allocations</span>
        </div>

        <div class="infra-machines-capacity-grid">
          ${machines.map(m => renderMachineCapacityCard(m)).join("")}
        </div>
      </div>

      <!-- Storage Pools Section -->
      ${storage.length ? `
        <div class="card infra-storage-card">
          <div class="infra-storage-head">
            <div>
              <span class="infra-section-title">Storage pools (${storage.length})</span>
              <div class="mut sm" style="font-size:12px">Used vs total disk capacity across pools</div>
            </div>
            <span class="badge sm">${fmtBytes(totalStorageBytes)} total</span>
          </div>

          <div class="infra-storage-list">
            ${storage.map(p => {
              const usedStr = fmtBytes(p.used);
              const totalStr = fmtBytes(p.total);
              const pct = p.total > 0 ? Math.min(100, Math.round(((p.used || 0) / p.total) * 100)) : 0;
              const isHigh = pct >= 85;
              return `<div class="infra-storage-row" onclick="K.drawer.openId(${J('infra:' + p.id)})">
                <div class="infra-storage-row-top">
                  <div class="infra-storage-name">
                    ${getKindIcon('storage')}
                    <span style="font-weight:600">${E(p.name)}</span>
                    ${p.stale ? `<span class="badge sm warn">stale</span>` : ""}
                  </div>
                  <div class="infra-storage-vals">
                    <span class="infra-num">${E(usedStr)}</span>
                    <span class="mut sm">of</span>
                    <span class="infra-num">${E(totalStr)}</span>
                    <span class="badge sm ${isHigh ? 'warn' : ''} infra-num">${pct}%</span>
                  </div>
                </div>
                <div class="infra-bar-track">
                  <div class="infra-bar-fill ${isHigh ? 'warn' : 'storage'}" style="width:${pct}%"></div>
                </div>
              </div>`;
            }).join("")}
          </div>
        </div>
      ` : ""}
    </div>`;

    return out;
  }

  /* ---------- Machine Capacity Card ---------- */
  function renderMachineCapacityCard(m) {
    const guests = m.guests || [];
    const guestsByMem = guests.slice().sort((a, b) => (b.memory_mb || 0) - (a.memory_mb || 0));
    const guestsByCores = guests.slice().sort((a, b) => (b.cores || 0) - (a.cores || 0));
    const guestsBySvcs = guests.slice().sort((a, b) => (b.services || 0) - (a.services || 0) || (b.ports || 0) - (a.ports || 0));

    const maxGuestMem = Math.max(...guests.map(g => g.memory_mb || 0), 1);
    const maxGuestCores = Math.max(...guests.map(g => g.cores || 0), 1);
    const maxGuestSvcs = Math.max(...guests.map(g => g.services || 0), 1);

    // Host memory metrics
    const hasHostMem = m.memory_total && m.memory_total > 0;
    const memUsedStr = fmtBytes(m.memory_used);
    const memTotalStr = fmtBytes(m.memory_total);
    const hostMemPct = hasHostMem ? Math.min(100, Math.round(((m.memory_used || 0) / m.memory_total) * 100)) : 0;

    // Total guest allocated memory and cores
    const totalAllocMemMb = guests.reduce((sum, g) => sum + (g.memory_mb || 0), 0);
    const totalAllocCores = guests.reduce((sum, g) => sum + (g.cores || 0), 0);

    return `<div class="card infra-machine-cap-card" data-id="${E(m.id)}">
      <!-- Machine Header -->
      <div class="infra-machine-cap-head">
        <div class="infra-machine-cap-info">
          <div class="infra-machine-cap-title" onclick="K.drawer.openId(${J('infra:' + m.id)})">
            ${getKindIcon('machine')}
            <span>${E(m.name)}</span>
          </div>
          <span class="badge sm">machine</span>
          ${m.cpu_count ? `<span class="badge sm">${m.cpu_count} CPU threads</span>` : ""}
          ${m.host_services ? `<span class="badge sm ok">${m.host_services} host services</span>` : ""}
          <span class="mut sm">${guests.length} ${guests.length === 1 ? 'guest' : 'guests'}</span>
        </div>
        <div class="infra-machine-cap-actions">
          <button type="button" class="btn sm" onclick="INFRA.zoom(${J(m.id)});INFRA.setView('map')">Zoom in map</button>
          <button type="button" class="btn sm" onclick="K.drawer.openId(${J('infra:' + m.id)})">Inspect</button>
        </div>
      </div>

      <!-- Capacity Stats Bar (Memory Used vs Total & CPU Threads) -->
      <div class="infra-machine-capacity-banner">
        <div class="infra-cap-stat-item">
          <div class="infra-cap-stat-label">
            <span>Memory used vs total</span>
            <span class="infra-num">${hasHostMem ? `${memUsedStr} of ${memTotalStr} (${hostMemPct}%)` : `${fmtGuestMem(totalAllocMemMb)} allocated`}</span>
          </div>
          <div class="infra-bar-track">
            <div class="infra-bar-fill" style="width:${hasHostMem ? hostMemPct : 100}%"></div>
          </div>
        </div>

        <div class="infra-cap-stat-item">
          <div class="infra-cap-stat-label">
            <span>CPU threads & allocation</span>
            <span class="infra-num">${m.cpu_count ? `${m.cpu_count} threads · ${totalAllocCores} cores allocated` : `${totalAllocCores} cores allocated`}</span>
          </div>
          <div class="infra-bar-track">
            <div class="infra-bar-fill cores" style="width:${m.cpu_count ? Math.min(100, Math.round((totalAllocCores / m.cpu_count) * 100)) : 100}%"></div>
          </div>
        </div>
      </div>

      <!-- Guest Allocation Charts: 3 Columns -->
      ${guests.length ? `
        <div class="infra-machine-charts-grid">
          <!-- Col 1: Allocated Memory -->
          <div class="infra-chart-col">
            <div class="infra-chart-title">
              <span>Allocated memory</span>
              <span class="mut sm infra-num">${fmtGuestMem(totalAllocMemMb)}</span>
            </div>
            <div class="infra-chart-list">
              ${guestsByMem.map(g => {
                const memVal = g.memory_mb || 0;
                const pct = Math.max(4, Math.round((memVal / maxGuestMem) * 100));
                const isStopped = g.status && String(g.status).toLowerCase() === "stopped";
                const barClass = g.stale ? "stale" : isStopped ? "stopped" : "guest";
                return `<div class="infra-bar-row ${isStopped ? 'stopped' : ''} ${g.stale ? 'stale' : ''}" onclick="K.drawer.openId(${J('infra:' + g.id)})" title="${E(g.name)}: ${fmtGuestMem(memVal)}">
                  <div class="infra-bar-row-label">
                    <span class="infra-bar-row-name">${E(g.name)}</span>
                    <span class="infra-bar-row-val infra-num">${fmtGuestMem(memVal)}</span>
                  </div>
                  <div class="infra-bar-track">
                    <div class="infra-bar-fill ${barClass}" style="width:${pct}%"></div>
                  </div>
                </div>`;
              }).join("")}
            </div>
          </div>

          <!-- Col 2: Allocated Cores -->
          <div class="infra-chart-col">
            <div class="infra-chart-title">
              <span>Allocated cores</span>
              <span class="mut sm infra-num">${totalAllocCores} cores</span>
            </div>
            <div class="infra-chart-list">
              ${guestsByCores.map(g => {
                const coresVal = g.cores || 0;
                const pct = Math.max(4, Math.round((coresVal / maxGuestCores) * 100));
                const isStopped = g.status && String(g.status).toLowerCase() === "stopped";
                const barClass = g.stale ? "stale" : isStopped ? "stopped" : "cores";
                return `<div class="infra-bar-row ${isStopped ? 'stopped' : ''} ${g.stale ? 'stale' : ''}" onclick="K.drawer.openId(${J('infra:' + g.id)})" title="${E(g.name)}: ${coresVal} cores">
                  <div class="infra-bar-row-label">
                    <span class="infra-bar-row-name">${E(g.name)}</span>
                    <span class="infra-bar-row-val infra-num">${coresVal} ${coresVal === 1 ? 'core' : 'cores'}</span>
                  </div>
                  <div class="infra-bar-track">
                    <div class="infra-bar-fill ${barClass}" style="width:${pct}%"></div>
                  </div>
                </div>`;
              }).join("")}
            </div>
          </div>

          <!-- Col 3: Services per Guest -->
          <div class="infra-chart-col">
            <div class="infra-chart-title">
              <span>Services per guest</span>
              <span class="mut sm infra-num">${guests.reduce((sum, g) => sum + (g.services || 0), 0)} svc</span>
            </div>
            <div class="infra-chart-list">
              ${guestsBySvcs.map(g => {
                const svcsVal = g.services || 0;
                const portsVal = g.ports || 0;
                const pct = Math.max(4, Math.round((svcsVal / maxGuestSvcs) * 100));
                const isStopped = g.status && String(g.status).toLowerCase() === "stopped";
                const barClass = g.stale ? "stale" : isStopped ? "stopped" : "service";
                return `<div class="infra-bar-row ${isStopped ? 'stopped' : ''} ${g.stale ? 'stale' : ''}" onclick="K.drawer.openId(${J('infra:' + g.id)})" title="${E(g.name)}: ${svcsVal} services, ${portsVal} ports">
                  <div class="infra-bar-row-label">
                    <span class="infra-bar-row-name">${E(g.name)}</span>
                    <span class="infra-bar-row-val infra-num">${svcsVal} svc · ${portsVal} ${portsVal === 1 ? 'port' : 'ports'}</span>
                  </div>
                  <div class="infra-bar-track">
                    <div class="infra-bar-fill ${barClass}" style="width:${pct}%"></div>
                  </div>
                </div>`;
              }).join("")}
            </div>
          </div>
        </div>
      ` : `<div class="mut sm" style="padding:16px 0">No guests running on this machine</div>`}
    </div>`;
  }

  /* ---------- Graph View ---------- */
  function renderGraphView(overview, root, depth) {
    depth = String(depth || "2");
    const src = root
      ? "/graph.html?source=infra&root=" + encodeURIComponent(root) + "&depth=" + encodeURIComponent(depth)
      : "/graph.html?source=infra";

    const tree = overview.tree || [];
    const systems = overview.systems || [];

    const machines = [];
    const guests = [];
    tree.forEach(m => {
      if (m.kind === "machine") machines.push(m);
      (m.children || []).forEach(c => {
        if (c.kind === "guest") guests.push(c);
      });
    });

    const isKnown = !root || machines.some(m => m.id === root) || guests.some(g => g.id === root) || systems.some(s => s.id === root);

    const depths = ["1", "2", "3"].map(d =>
      `<button type="button" class="seg-btn ${d === depth ? "on" : ""}" aria-pressed="${d === depth}" onclick="INFRA.setGraphDepth(${J(d)})">${d}</button>`
    ).join("");

    return `<div class="infra-graph-view">
      <div class="card infra-graph-controls-card">
        <div class="infra-graph-controls">
          <div class="infra-graph-controls-left">
            <label class="infra-graph-label">
              <span>Scope</span>
              <select class="infra-graph-select" aria-label="Graph scope" onchange="INFRA.setGraphScope(this.value)">
                <option value="">Everything</option>
                ${machines.length ? `
                  <optgroup label="Machines">
                    ${machines.map(m => `<option value="${E(m.id)}"${m.id === root ? " selected" : ""}>${E(m.name)}</option>`).join("")}
                  </optgroup>
                ` : ""}
                ${guests.length ? `
                  <optgroup label="Guests & Containers">
                    ${guests.map(g => `<option value="${E(g.id)}"${g.id === root ? " selected" : ""}>${E(g.name)}</option>`).join("")}
                  </optgroup>
                ` : ""}
                ${systems.length ? `
                  <optgroup label="Systems">
                    ${systems.map(s => `<option value="${E(s.id)}"${s.id === root ? " selected" : ""}>${E(s.name)}</option>`).join("")}
                  </optgroup>
                ` : ""}
                ${!isKnown ? `<option value="${E(root)}" selected>Node ${E(S.rootName || root)}</option>` : ""}
              </select>
            </label>

            ${root ? `
              <div class="infra-graph-depth-wrap">
                <span class="infra-filter-label">Depth</span>
                <div class="seg" role="group" aria-label="Depth">
                  ${depths}
                </div>
              </div>
            ` : ""}

            <a href="${src}" target="_blank" rel="noopener" class="btn sm infra-graph-fullscreen-btn">Full screen</a>
          </div>

          <div class="infra-graph-legend">
            <span class="infra-legend-item"><span class="infra-legend-dot" style="background:#f97316"></span>machine</span>
            <span class="infra-legend-item"><span class="infra-legend-dot" style="background:#3b82f6"></span>guest</span>
            <span class="infra-legend-item"><span class="infra-legend-dot" style="background:#22c55e"></span>service</span>
            <span class="infra-legend-item"><span class="infra-legend-dot" style="background:#eab308"></span>system</span>
            <span class="infra-legend-item"><span class="infra-legend-dot" style="background:#a855f7"></span>endpoint</span>
            <span class="infra-legend-item"><span class="infra-legend-dot" style="background:#ec4899"></span>tunnel</span>
            <span class="infra-legend-item"><span class="infra-legend-dot" style="background:#14b8a6"></span>storage</span>
            <span class="infra-legend-item"><span class="infra-legend-dot" style="background:#ef4444"></span>credential</span>
          </div>
        </div>
      </div>

      <div class="infra-graph-frame-wrap">
        <iframe title="Infrastructure Graph" class="infra-graph-iframe" src="${src}"></iframe>
      </div>
    </div>`;
  }

  /* ---------- Map View ---------- */
  async function renderMapView(overview, zoomId, activeSys) {
    const systems = overview.systems || [];
    const tree = overview.tree || [];

    // If zoomed into a specific node (machine or guest), render zoomed view
    if (zoomId) {
      return await renderZoomedView(zoomId, tree);
    }

    // Top Controls Bar: Systems Filter & Breadcrumb
    let out = `<div class="infra-map-bar">
      <!-- Systems Filter -->
      <div class="infra-sys-filter-wrap">
        <span class="infra-sys-label">Filter by system:</span>
        <button type="button" class="chip ${!activeSys ? "on" : ""}" aria-pressed="${!activeSys}" onclick="INFRA.setSystemFilter('')">All systems</button>
        ${systems.map(s => `
          <button type="button" class="chip ${activeSys === s.name ? "on" : ""}" aria-pressed="${activeSys === s.name}" onclick="INFRA.setSystemFilter(${J(s.name)})">
            ${E(s.name)}
          </button>
        `).join("")}
      </div>

      <!-- Breadcrumbs -->
      <div class="infra-crumbs">
        <span class="infra-crumb cur">All infrastructure</span>
      </div>
    </div>`;

    // Canvas of Machines
    out += `<div class="infra-map-canvas">`;

    tree.forEach(m => {
      const allChildren = m.children || [];
      const guests = allChildren.filter(c => c.kind === "guest");
      const hostServices = allChildren.filter(c => c.kind === "service");

      const mAttrs = m.attrs || {};
      const mSpecs = [
        mAttrs.ip ? mAttrs.ip : null,
        mAttrs.cpus ? mAttrs.cpus + " CPUs" : null,
        mAttrs.ram_gb ? mAttrs.ram_gb + "GB RAM" : null,
        mAttrs.os ? mAttrs.os : null,
      ].filter(Boolean).join(" · ");

      const isMemOnly = !(m.sources || []).some(s => s.startsWith("discovery"));
      const isDimmed = activeSys && !(m.name.toLowerCase().includes(activeSys.toLowerCase()));

      out += `<div class="infra-machine-card ${m.stale ? 'stale' : ''} ${isDimmed ? 'infra-dimmed' : ''}" data-id="${E(m.id)}" data-search="${E(m.name + ' ' + (mAttrs.ip || '') + ' ' + m.kind)}">
        <!-- Machine Header -->
        <div class="infra-machine-head">
          <div class="infra-machine-info">
            <div class="infra-machine-title" onclick="INFRA.zoom(${J(m.id)})">
              ${getKindIcon(m.kind)}
              <span>${E(m.name)}</span>
            </div>
            <span class="badge sm">${E(m.kind)}</span>
            ${mAttrs.ip ? `<span class="badge sm">${E(mAttrs.ip)}</span>` : ""}
            ${mSpecs ? `<span class="mut sm">${E(mSpecs)}</span>` : ""}
            ${isMemOnly ? `<span class="badge sm info" title="Discovered from memory notes">from memory</span>` : ""}
            ${m.stale ? `<span class="badge sm warn">stale · not seen since ${E(fmtAgo(m.last_seen))}</span>` : ""}
          </div>
          <div class="infra-machine-actions">
            <button type="button" class="btn sm" onclick="INFRA.zoom(${J(m.id)})">Zoom in</button>
            <button type="button" class="btn sm" onclick="K.drawer.openId(${J('infra:' + m.id)})">Inspect</button>
          </div>
        </div>

        <!-- Host Services Section -->
        ${hostServices.length ? `
          <div class="infra-host-services-section">
            <div class="infra-section-title">On the host (${hostServices.length})</div>
            <div class="infra-host-chips">
              ${hostServices.map(svc => {
                const sAttrs = svc.attrs || {};
                const sHighlighted = activeSys && svc.name.toLowerCase().includes(activeSys.toLowerCase());
                const sMemOnly = !(svc.sources || []).some(s => s.startsWith("discovery"));
                const ports = getServicePorts(svc);
                return `<div class="infra-host-chip ${sHighlighted ? 'infra-highlighted' : ''}" data-id="${E(svc.id)}" data-search="${E(svc.name + ' ' + svc.kind + ' ' + (ports.join(' ') || ''))}" onclick="K.drawer.openId(${J('infra:' + svc.id)})">
                  ${getKindIcon(svc.kind)}
                  <span class="infra-host-chip-name">${E(svc.name)}</span>
                  ${renderPortChips(ports)}
                  ${sMemOnly ? `<span class="badge sm info" style="font-size:9.5px;padding:0 4px">memory</span>` : ""}
                  ${svc.stale ? `<span class="badge sm warn" style="font-size:9.5px;padding:0 4px">stale</span>` : ""}
                </div>`;
              }).join("")}
            </div>
          </div>
        ` : ""}

        <!-- Guests Section -->
        ${guests.length ? `
          <div class="infra-guests-section">
            <div class="infra-section-title">Guests & Containers (${guests.length})</div>
            <div class="infra-guest-grid">
              ${guests.map(g => renderGuestCard(g, activeSys)).join("")}
            </div>
          </div>
        ` : `<div class="mut sm">No guests running on this machine</div>`}
      </div>`;
    });

    out += `</div>`;
    return out;
  }

  /* ---------- Zoomed-In View (Machine or Guest) ---------- */
  async function renderZoomedView(nodeId, tree) {
    let node;
    let graphData;
    try {
      [node, graphData] = await Promise.all([
        api("infra_node?id=" + encodeURIComponent(nodeId)),
        api("infra_graph?root=" + encodeURIComponent(nodeId) + "&depth=2"),
      ]);
    } catch (e) {
      try {
        node = await api("infra_node?id=" + encodeURIComponent(nodeId));
      } catch (err) {
        return `<div class="empty"><div class="mut">Could not load node: ${E(err && err.message ? err.message : err)}</div></div>`;
      }
    }
    if (!node || !node.id) {
      return '<div class="empty"><div class="mut">Node not found.</div></div>';
    }

    const chain = node.chain || [];
    const outLinks = node.out || [];
    const inLinks = node.in || [];
    const attrs = node.attrs || {};

    const gNodes = (graphData && graphData.nodes) || [];
    const gEdges = (graphData && graphData.edges) || [];
    const nodeMap = new Map(gNodes.map(n => [n.id, n]));

    // Breadcrumb navigation
    let crumbsHtml = `<button type="button" class="infra-crumb" onclick="INFRA.zoom('')">All infrastructure</button>`;
    chain.slice().reverse().forEach(c => {
      crumbsHtml += `<span class="infra-crumb-sep">›</span>
        <button type="button" class="infra-crumb" onclick="INFRA.zoom(${J(c.id)})">${E(c.name)}</button>`;
    });
    crumbsHtml += `<span class="infra-crumb-sep">›</span><span class="infra-crumb cur">${E(node.name)}</span>`;

    // Machine view vs Guest/Other view
    if (node.kind === "machine") {
      const machineInTree = (tree || []).find(t => t.id === nodeId);
      const allChildren = (machineInTree && machineInTree.children) ? machineInTree.children : inLinks.filter(l => l.rel === "runs_on");
      const guests = allChildren.filter(c => c.kind === "guest");
      const hostServices = allChildren.filter(c => c.kind === "service");

      const specsText = fmtGuestSpecs(attrs);

      let out = `<div class="infra-zoomed-view">
        <!-- Breadcrumb Bar -->
        <div class="infra-map-bar">
          <div class="infra-crumbs">${crumbsHtml}</div>
          <div class="infra-head-controls">
            <button type="button" class="btn sm" onclick="INFRA.zoom('')">Zoom out to all</button>
            <button type="button" class="btn sm" onclick="K.drawer.openId(${J('infra:' + node.id)})">Inspect drawer</button>
          </div>
        </div>

        <!-- Node Summary Card -->
        <div class="infra-zoomed-header-card">
          <div class="infra-machine-info">
            <span style="display:flex;align-items:center;gap:6px;font-size:var(--text-xl);font-weight:600">
              ${getKindIcon(node.kind)} ${E(node.name)}
            </span>
            <span class="badge sm">${E(node.kind)}</span>
            ${attrs.ip ? `<span class="badge sm">${E(attrs.ip)}</span>` : ""}
            ${specsText ? `<span class="mut sm">${E(specsText)}</span>` : ""}
            ${attrs.os ? `<span class="mut sm">${E(attrs.os)}</span>` : ""}
            ${node.stale ? `<span class="badge sm warn">stale · not seen since ${E(fmtAgo(node.last_seen))}</span>` : ""}
            ${node.last_seen && !node.stale ? `<span class="mut sm">seen ${E(fmtAgo(node.last_seen))}</span>` : ""}
          </div>
          ${node.note ? `<div class="mut sm">${E(node.note)}</div>` : ""}
        </div>

        <!-- Host Services Section -->
        ${hostServices.length ? `
          <div class="infra-host-services-section">
            <div class="infra-section-title">On the host (${hostServices.length})</div>
            <div class="infra-host-chips">
              ${hostServices.map(svc => {
                const sAttrs = svc.attrs || {};
                const sMemOnly = !(svc.sources || []).some(s => s.startsWith("discovery"));
                const ports = getServicePorts(svc, gEdges, nodeMap);
                return `<div class="infra-host-chip" data-id="${E(svc.id)}" onclick="K.drawer.openId(${J('infra:' + svc.id)})">
                  ${getKindIcon(svc.kind)}
                  <span class="infra-host-chip-name">${E(svc.name)}</span>
                  ${renderPortChips(ports)}
                  ${sMemOnly ? `<span class="badge sm info" style="font-size:9.5px;padding:0 4px">memory</span>` : ""}
                  ${svc.stale ? `<span class="badge sm warn" style="font-size:9.5px;padding:0 4px">stale</span>` : ""}
                </div>`;
              }).join("")}
            </div>
          </div>
        ` : ""}

        <!-- Guests Section -->
        ${guests.length ? `
          <div class="infra-guests-section">
            <div class="infra-section-title">Guests & Containers (${guests.length})</div>
            <div class="infra-guest-grid">
              ${guests.map(g => renderGuestCard(g, "")).join("")}
            </div>
          </div>
        ` : `<div class="mut sm">No guests running on this machine</div>`}
      </div>`;

      return out;
    }

    // Children (runtime services running inside this guest)
    const childIds = new Set(inLinks.filter(l => l.rel === "runs_on").map(l => l.id));
    const childNodes = inLinks.filter(l => l.rel === "runs_on");

    // Outbound mounts to storage
    const outboundMounts = outLinks.filter(l => l.rel === "mounts" || (l.rel === "stores_in" && l.kind === "storage"));

    // Inbound container binds (containers that bind paths from this guest)
    const containerBinds = inLinks.filter(l => l.rel === "mounts");

    // Gather connections (talks_to, exposed_via, uses_credential, part_of, reachable_at)
    const connMap = new Map();
    outLinks.filter(l => l.rel !== "runs_on" && l.rel !== "mounts" && l.rel !== "stores_in").forEach(l => {
      connMap.set(l.edge || (l.id + l.rel), { ...l, sourceName: node.name });
    });
    inLinks.filter(l => l.rel !== "runs_on" && l.rel !== "mounts").forEach(l => {
      connMap.set(l.edge || (l.id + l.rel), {
        ...l,
        label: l.rel === "talks_to" ? "used by" : l.rel === "uses_credential" ? "credential for" : "linked from",
        sourceName: node.name,
      });
    });

    // Child service connections
    gEdges.forEach(e => {
      if (childIds.has(e.src) && e.rel !== "runs_on" && e.rel !== "mounts") {
        const srcNode = nodeMap.get(e.src);
        const dstNode = nodeMap.get(e.dst);
        if (srcNode && dstNode) {
          const key = e.id || (e.src + e.rel + e.dst);
          if (!connMap.has(key)) {
            let label = e.rel;
            if (e.rel === "talks_to") label = "talks to";
            else if (e.rel === "exposed_via") label = "exposed via";
            else if (e.rel === "uses_credential") label = "uses credential";
            else if (e.rel === "part_of") label = "part of";
            else if (e.rel === "reachable_at") label = "reachable at";
            connMap.set(key, {
              id: dstNode.id,
              name: dstNode.name,
              kind: dstNode.kind,
              rel: e.rel,
              label,
              sourceName: srcNode.name,
              stale: e.stale,
            });
          }
        }
      }
    });

    const allConns = Array.from(connMap.values());
    const specsText = fmtGuestSpecs(attrs);

    let out = `<div class="infra-zoomed-view">
      <!-- Breadcrumb Bar -->
      <div class="infra-map-bar">
        <div class="infra-crumbs">${crumbsHtml}</div>
        <div class="infra-head-controls">
          <button type="button" class="btn sm" onclick="INFRA.zoom('')">Zoom out to all</button>
          <button type="button" class="btn sm" onclick="K.drawer.openId(${J('infra:' + node.id)})">Inspect drawer</button>
        </div>
      </div>

      <!-- Node Summary Card -->
      <div class="infra-zoomed-header-card">
        <div class="infra-machine-info">
          <span style="display:flex;align-items:center;gap:6px;font-size:var(--text-xl);font-weight:600">
            ${getKindIcon(node.kind)} ${E(node.name)}
          </span>
          <span class="badge sm">${E(node.kind)}</span>
          ${specsText ? `<span class="infra-guest-specs mut sm">${E(specsText)}</span>` : ""}
          ${attrs.os ? `<span class="mut sm">${E(attrs.os)}</span>` : ""}
          ${node.stale ? `<span class="badge sm warn">stale · not seen since ${E(fmtAgo(node.last_seen))}</span>` : ""}
          ${node.last_seen && !node.stale ? `<span class="mut sm">seen ${E(fmtAgo(node.last_seen))}</span>` : ""}
        </div>
        ${node.note ? `<div class="mut sm">${E(node.note)}</div>` : ""}
      </div>

      <!-- 2-Column Split: Services/Disks & Connections -->
      <div class="infra-zoomed-grid">
        <!-- Left: Services or Children -->
        <div class="infra-zoomed-panel">
          <span class="infra-section-title">Runtime & Services (${childNodes.length})</span>
          ${childNodes.length ? `
            <div class="infra-services-list">
              ${childNodes.map(child => {
                const cNode = nodeMap.get(child.id) || child;
                const cAttrs = cNode.attrs || {};
                const ports = getServicePorts(cNode, gEdges, nodeMap);
                return `<div class="infra-svc-chip-row" onclick="K.drawer.openId(${J('infra:' + child.id)})">
                  <div class="infra-svc-name">
                    ${getKindIcon(child.kind)}
                    <span>${E(child.name)}</span>
                    <span class="badge sm">${E(child.kind)}</span>
                    ${renderPortChips(ports)}
                    ${cAttrs.image ? `<span class="infra-svc-image" title="${E(cAttrs.image)}">${E(truncateMiddle(cAttrs.image, 32))}</span>` : ""}
                  </div>
                  <div style="display:flex;gap:4px;align-items:center;flex-shrink:0">
                    <button type="button" class="btn sm" onclick="event.stopPropagation();INFRA.zoom(${J(child.id)})">Zoom</button>
                    <button type="button" class="btn sm" onclick="event.stopPropagation();K.drawer.openId(${J('infra:' + child.id)})">Inspect</button>
                  </div>
                </div>`;
              }).join("")}
            </div>
          ` : `<div class="mut sm">No runtime children running inside this node</div>`}

          <!-- Outbound Storage Mounts -->
          ${outboundMounts.length ? `
            <div class="infra-zoomed-section" style="margin-top:var(--space-3)">
              <span class="infra-section-title">Mounts (${outboundMounts.length})</span>
              <div class="infra-binds-list">
                ${outboundMounts.map(m => {
                  const binds = (m.attrs && Array.isArray(m.attrs.binds)) ? m.attrs.binds : [];
                  return `<div class="infra-bind-group">
                    <div class="infra-bind-head" onclick="K.drawer.openId(${J('infra:' + m.id)})">
                      ${getKindIcon(m.kind || 'storage')}
                      <span style="font-weight:600">${E(m.name)}</span>
                      <span class="badge sm">${E(m.kind || 'storage')}</span>
                      ${m.attrs && m.attrs.volume ? `<span class="mut sm">${E([m.attrs.volume, m.attrs.size].filter(Boolean).join(' · '))}</span>` : ''}
                    </div>
                    ${binds.length ? `
                      <div class="infra-bind-items">
                        ${binds.map(b => {
                          const srcPath = b.host_path || b.guest_path || "";
                          const dstPath = b.guest_path || b.container_path || "";
                          return `<div class="infra-bind-item">
                            <code class="infra-bind-path">${E(srcPath)}${dstPath && dstPath !== srcPath ? ' → ' + E(dstPath) : ''}</code>
                            ${b.read_only ? `<span class="badge sm">read-only</span>` : ""}
                          </div>`;
                        }).join("")}
                      </div>
                    ` : ""}
                  </div>`;
                }).join("")}
              </div>
            </div>
          ` : ""}

          <!-- Inbound Container Binds -->
          ${containerBinds.length ? `
            <div class="infra-zoomed-section" style="margin-top:var(--space-3)">
              <span class="infra-section-title">Container binds (${containerBinds.length})</span>
              <div class="infra-binds-list">
                ${containerBinds.map(cb => {
                  const binds = (cb.attrs && Array.isArray(cb.attrs.binds)) ? cb.attrs.binds : [];
                  return `<div class="infra-bind-group">
                    <div class="infra-bind-head" onclick="K.drawer.openId(${J('infra:' + cb.id)})">
                      ${getKindIcon(cb.kind)}
                      <span style="font-weight:600">${E(cb.name)}</span>
                      <span class="badge sm">${E(cb.kind)}</span>
                    </div>
                    ${binds.length ? `
                      <div class="infra-bind-items">
                        ${binds.map(b => {
                          const srcPath = b.host_path || b.guest_path || "";
                          const dstPath = b.container_path || "";
                          return `<div class="infra-bind-item">
                            <code class="infra-bind-path">${E(srcPath)} → ${E(dstPath)}</code>
                            ${b.read_only ? `<span class="badge sm">read-only</span>` : ""}
                          </div>`;
                        }).join("")}
                      </div>
                    ` : ""}
                  </div>`;
                }).join("")}
              </div>
            </div>
          ` : ""}
        </div>

        <!-- Right: Connections & Links -->
        <div class="infra-zoomed-panel">
          <span class="infra-section-title">Connections & Dependencies (${allConns.length})</span>
          ${allConns.length ? `
            <div class="infra-rel-group">
              ${allConns.map(l => {
                const isCred = l.kind === "credential";
                return `<div class="infra-conn-row" onclick="if(!event.target.closest('a,button')){K.drawer.openId(${J('infra:' + l.id)})}">
                  <div class="infra-conn-info">
                    ${getKindIcon(l.kind)}
                    <div>
                      <div style="font-size:var(--text-sm)">
                        <span class="mut sm">${E(l.sourceName ? l.sourceName + ' ' + (l.label || l.rel) : (l.label || l.rel))}:</span>
                        <b>${E(l.name)}</b>
                      </div>
                      ${isCred ? `
                        <div class="mut sm" style="font-size:11px">
                          Vault credential · <a href="#Secrets" onclick="go('Secrets');return false" style="text-decoration:underline">Secrets page</a>
                        </div>
                      ` : ""}
                    </div>
                  </div>
                  <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
                    <span class="badge sm">${E(l.kind)}</span>
                    ${!isCred ? `<button type="button" class="btn sm" onclick="event.stopPropagation();K.drawer.openId(${J('infra:' + l.id)})">Inspect</button>` : ""}
                  </div>
                </div>`;
              }).join("")}
            </div>
          ` : `<div class="mut sm">No external connections recorded</div>`}
        </div>
      </div>
    </div>`;

    return out;
  }

  /* ---------- List View ---------- */
  async function renderListView(query) {
    let nodes;
    try {
      nodes = await api("infra_nodes?q=" + encodeURIComponent(query || ""));
    } catch (e) {
      return `<div class="empty"><div class="mut">Could not load nodes: ${E(e && e.message ? e.message : e)}</div></div>`;
    }

    nodes = nodes || [];
    if (!nodes.length) {
      return `<div class="empty"><b>No nodes match your search</b></div>`;
    }

    // Group by kind in layer order
    const layerKinds = [
      { key: "machine", title: "Machines" },
      { key: "guest", title: "Guests & Containers" },
      { key: "service", title: "Services" },
      { key: "endpoint", title: "Endpoints & Tunnels" },
      { key: "storage", title: "Storage & Databases" },
      { key: "credential", title: "Credentials" },
      { key: "system", title: "Systems" },
      { key: "repo", title: "Repositories & Directories" },
      { key: "place", title: "Places & Networks" },
    ];

    const grouped = {};
    nodes.forEach(n => {
      let g = n.kind;
      if (g === "tunnel") g = "endpoint";
      if (g === "database") g = "storage";
      if (g === "directory") g = "repo";
      if (g === "network") g = "place";
      grouped[g] = grouped[g] || [];
      grouped[g].push(n);
    });

    let out = `<div class="infra-list-wrap">`;

    layerKinds.forEach(k => {
      const list = grouped[k.key] || [];
      if (!list.length) return;

      out += `<div class="infra-kind-group">
        <div class="infra-kind-head">
          <div class="infra-kind-title-wrap">
            <span class="infra-kind-title">${E(k.title)}</span>
            <span class="badge sm">${list.length}</span>
          </div>
        </div>
        <div class="infra-table-wrap">
          <table class="infra-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Kind</th>
                <th>Attributes</th>
                <th>Sources</th>
                <th>Last seen</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              ${list.map(n => {
                const attrs = n.attrs || {};
                const attrPills = [
                  attrs.ip ? attrs.ip : (Array.isArray(attrs.ips) && attrs.ips.length ? attrs.ips[0] : null),
                  attrs.vmid ? "vmid:" + attrs.vmid : null,
                  attrs.port ? "port:" + attrs.port : null,
                  attrs.status ? attrs.status : null,
                ].filter(Boolean).join(" · ");

                const sources = Array.isArray(n.sources) ? n.sources : [];
                const searchKw = [n.name, n.kind, n.note, JSON.stringify(attrs)].join(" ").toLowerCase();

                return `<tr class="${n.stale ? 'stale' : ''}" data-search="${E(searchKw)}" onclick="K.drawer.openId(${J('infra:' + n.id)})">
                  <td>
                    <div style="display:flex;align-items:center;gap:6px;">
                      ${getKindIcon(n.kind)}
                      <span style="font-weight:500">${E(n.name)}</span>
                      ${n.note ? `<span class="mut sm">· ${E(n.note.slice(0, 60))}</span>` : ""}
                    </div>
                  </td>
                  <td><span class="badge sm">${E(n.kind)}</span></td>
                  <td><span class="mut sm">${E(attrPills)}</span></td>
                  <td>
                    <div style="display:flex;gap:4px;flex-wrap:wrap">
                      ${sources.map(s => {
                        const isDisc = s.startsWith("discovery");
                        const isVault = s.startsWith("vault");
                        const cls = isDisc ? "ok" : isVault ? "accent" : "info";
                        return `<span class="badge sm ${cls}">${E(s)}</span>`;
                      }).join("")}
                    </div>
                  </td>
                  <td>${E(fmtAgo(n.last_seen))}</td>
                  <td>
                    <div style="display:flex;align-items:center;gap:6px">
                      <span class="status-dot ${getStatusDotClass(n)}"></span>
                      <span>${n.stale ? 'Stale' : (n.attrs && n.attrs.status ? (n.attrs.status.charAt(0).toUpperCase() + n.attrs.status.slice(1)) : 'Active')}</span>
                    </div>
                  </td>
                </tr>`;
              }).join("")}
            </tbody>
          </table>
        </div>
      </div>`;
    });

    out += `</div>`;
    return out;
  }

  /* ---------- Ask View ---------- */
  function renderAskView(q, text) {
    return `<div class="infra-ask-wrap">
      <div class="infra-head-titles">
        <h3 style="margin:0;font-size:var(--text-lg);font-weight:600">Query infrastructure</h3>
        <span class="mut sm">Inspect exactly what AI agents receive when calling infra()</span>
      </div>

      <div class="infra-ask-input-row">
        <input type="search" id="infra-ask-input" class="infra-ask-input" value="${E(q || "")}" placeholder="Query machine, service, container, IP or system..." onkeydown="if(event.key==='Enter')INFRA.ask(this.value)">
        <button type="button" class="pri" onclick="INFRA.ask(document.getElementById('infra-ask-input').value)">Query</button>
      </div>

      <div class="infra-ask-quick-row">
        <span class="mut sm">Suggestions:</span>
        <button type="button" class="chip sm" onclick="INFRA.ask('')">Full overview</button>
        <button type="button" class="chip sm" onclick="INFRA.ask('hv01')">hv01</button>
        <button type="button" class="chip sm" onclick="INFRA.ask('media')">media (LXC 201)</button>
        <button type="button" class="chip sm" onclick="INFRA.ask('apps')">apps (LXC 202)</button>
        <button type="button" class="chip sm" onclick="INFRA.ask('Atlas')">Atlas</button>
        <button type="button" class="chip sm" onclick="INFRA.ask('nas01')">nas01</button>
      </div>

      <div class="infra-ask-result-card">
        <div class="infra-ask-result-head">
          <span style="font-weight:600;font-size:var(--text-sm)">Agent response</span>
          <span class="badge sm info">Agents get this from infra()</span>
        </div>
        <div id="infra-ask-result">
          ${text ? `
            <pre class="infra-ask-pre">${E(text)}</pre>
          ` : `
            <div class="mut sm" style="padding:16px 0">Run a query or click a suggestion above to inspect agent text.</div>
          `}
        </div>
        ${text ? `
          <div style="display:flex;justify-content:flex-end">
            <button type="button" class="btn sm" onclick="K.copy(S.askText)">Copy text</button>
          </div>
        ` : ""}
      </div>
    </div>`;
  }

})();
