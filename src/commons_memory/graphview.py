"""Graph view analysis and interactive HTML explorer."""
from __future__ import annotations

import collections
import html
import json
import re


def analyze(g: dict) -> dict:
    raw_nodes = g.get("nodes") or {}
    edges = g.get("edges") or []
    if not raw_nodes:
        out = dict(g)
        out["nodes"] = {}
        out["edges"] = list(edges)
        out["communities"] = []
        return out

    sorted_nodes = sorted(raw_nodes.keys())
    nodes = {nid: dict(data) for nid, data in raw_nodes.items()}

    adj_set = {u: set() for u in sorted_nodes}
    out_deg = {u: 0 for u in sorted_nodes}
    in_nbrs = {u: [] for u in sorted_nodes}
    deg = {u: 0 for u in sorted_nodes}

    for e in edges:
        s, d = e.get("src"), e.get("dst")
        if s in adj_set and d in adj_set:
            adj_set[s].add(d)
            adj_set[d].add(s)
            out_deg[s] += 1
            in_nbrs[d].append(s)
            deg[s] += 1
            if s != d:
                deg[d] += 1

    adj_sorted = {u: sorted(adj_set[u]) for u in sorted_nodes}

    # Deterministic Louvain community detection on undirected graph
    adj_weights = {u: {} for u in sorted_nodes}
    m = 0
    for e in edges:
        s, d = e.get("src"), e.get("dst")
        if s in adj_set and d in adj_set:
            adj_weights[s][d] = adj_weights[s].get(d, 0) + 1
            if s != d:
                adj_weights[d][s] = adj_weights[d].get(s, 0) + 1
            m += 1

    comm = {u: u for u in sorted_nodes}
    if m > 0:
        tot_deg = {u: deg[u] for u in sorted_nodes}
        two_m = 2.0 * m
        for _ in range(15):
            improved = False
            for u in sorted_nodes:
                c_u = comm[u]
                k_u = deg[u]
                if k_u == 0:
                    continue
                tot_deg[c_u] -= k_u

                comm_weights = {}
                for v, w in adj_weights[u].items():
                    cv = comm[v]
                    comm_weights[cv] = comm_weights.get(cv, 0) + w

                best_c = c_u
                best_gain = 0.0
                baseline_gain = comm_weights.get(c_u, 0) - (tot_deg[c_u] * k_u) / two_m

                for c in sorted(comm_weights.keys()):
                    gain = comm_weights[c] - (tot_deg[c] * k_u) / two_m
                    diff = gain - baseline_gain
                    if diff > 1e-6 and diff > best_gain:
                        best_gain = diff
                        best_c = c

                comm[u] = best_c
                tot_deg[best_c] += k_u
                if best_c != c_u:
                    improved = True
            if not improved:
                break

    comm_sizes = collections.Counter(comm.values())
    unique_labels = sorted(set(comm.values()), key=lambda l: (-comm_sizes[l], l))
    label_to_id = {lbl: i for i, lbl in enumerate(unique_labels)}
    node_comm = {u: label_to_id[comm[u]] for u in sorted_nodes}

    # PageRank (30 power iterations, damping 0.85, sums to ~1)
    n = len(sorted_nodes)
    rank = {u: 1.0 / n for u in sorted_nodes}
    d = 0.85
    base_const = (1.0 - d) / n
    for _ in range(30):
        dangling_sum = sum(rank[u] for u in sorted_nodes if out_deg[u] == 0)
        base = base_const + (d * dangling_sum) / n
        new_rank = {}
        for u in sorted_nodes:
            s = sum(rank[v] / out_deg[v] for v in in_nbrs[u])
            new_rank[u] = base + d * s
        rank = new_rank

    # Communities metadata
    comm_nodes_map = collections.defaultdict(list)
    for u in sorted_nodes:
        comm_nodes_map[node_comm[u]].append(u)

    communities = []
    for cid, c_nodes in sorted(comm_nodes_map.items()):
        size = len(c_nodes)
        top_u = min(c_nodes, key=lambda u: (-rank[u], u))
        top_name = nodes[top_u].get("name") or top_u
        type_counts = collections.Counter(nodes[u].get("type", "unknown") for u in c_nodes)
        dom_type = min(type_counts.keys(), key=lambda t: (-type_counts[t], t))
        comm_label = f"{top_name} ({dom_type})"
        communities.append({"id": cid, "size": size, "label": comm_label})

    for u in sorted_nodes:
        nodes[u]["community"] = node_comm[u]
        nodes[u]["degree"] = deg[u]
        nodes[u]["rank"] = rank[u]

    out = dict(g)
    out["nodes"] = nodes
    out["edges"] = list(edges)
    out["communities"] = communities
    return out


def shortest_path(g: dict, a: str, b: str) -> list[str]:
    nodes = g.get("nodes") or {}
    if a not in nodes or b not in nodes:
        return []
    if a == b:
        return [a]
    adj = collections.defaultdict(list)
    for e in g.get("edges") or []:
        s, d = e.get("src"), e.get("dst")
        if s in nodes and d in nodes:
            adj[s].append(d)
            adj[d].append(s)
    for u in adj:
        adj[u].sort()
    q = collections.deque([a])
    visited = {a: None}
    while q:
        curr = q.popleft()
        if curr == b:
            path = []
            node = b
            while node is not None:
                path.append(node)
                node = visited[node]
            return path[::-1]
        for nxt in adj[curr]:
            if nxt not in visited:
                visited[nxt] = curr
                q.append(nxt)
    return []


VIEWER_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #0f1117;
  --panel-bg: rgba(23, 27, 38, 0.94);
  --border: #282e3f;
  --text: #e2e8f0;
  --text-muted: #94a3b8;
  --accent: #8b5cf6;
  --accent-hover: #7c3aed;
  --card-bg: #1a1e2b;
  --card-border: #2e3549;
  --danger: #ef4444;
  --success: #10b981;
  --edge: #64748b;
  --edge-highlight: #c7d2fe;
  --label-bg: rgba(23, 27, 38, 0.88);
  --label-border: rgba(51, 65, 85, 0.85);
  --label-text: #f8fafc;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f8fafc;
    --panel-bg: rgba(255, 255, 255, 0.94);
    --border: #e2e8f0;
    --text: #0f172a;
    --text-muted: #64748b;
    --accent: #7c3aed;
    --accent-hover: #6d28d9;
    --card-bg: #f1f5f9;
    --card-border: #cbd5e1;
    --danger: #dc2626;
    --success: #059669;
    --edge: #475569;
    --edge-highlight: #312e81;
    --label-bg: rgba(255, 255, 255, 0.95);
    --label-border: rgba(148, 163, 184, 0.9);
    --label-text: #0f172a;
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 0; background: var(--bg); color: var(--text); font-family: var(--font); font-size: 13px; overflow: hidden; height: 100vh; width: 100vw; }
button { font-family: inherit; font-size: 12px; cursor: pointer; border: 1px solid var(--border); background: var(--card-bg); color: var(--text); border-radius: 6px; padding: 5px 10px; display: inline-flex; align-items: center; justify-content: center; gap: 5px; transition: background 0.15s, border-color 0.15s; }
button:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
button.small { padding: 2px 7px; font-size: 11px; }
input, select { font-family: inherit; font-size: 12px; background: var(--card-bg); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; outline: none; }
input:focus, select:focus { border-color: var(--accent); }

#topbar { position: fixed; top: 0; left: 0; right: 0; height: 46px; background: var(--panel-bg); backdrop-filter: blur(8px); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 12px; z-index: 100; gap: 10px; }
#topbar-left { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
#title-badge { font-weight: 700; font-size: 14px; display: flex; align-items: center; gap: 6px; }
#graph-stats { color: var(--text-muted); font-size: 11px; }

#search-wrap { position: relative; width: 280px; }
#search-input { width: 100%; padding-left: 28px; }
#search-icon { position: absolute; left: 8px; top: 50%; transform: translateY(-50%); color: var(--text-muted); pointer-events: none; }
#search-results { position: absolute; top: calc(100% + 4px); left: 0; right: 0; background: var(--panel-bg); border: 1px solid var(--border); border-radius: 6px; max-height: 280px; overflow-y: auto; display: none; z-index: 200; box-shadow: 0 8px 24px rgba(0,0,0,0.3); }
.search-item { padding: 6px 10px; cursor: pointer; display: flex; align-items: center; justify-content: space-between; gap: 8px; border-bottom: 1px solid var(--border); }
.search-item:last-child { border-bottom: none; }
.search-item:hover, .search-item.sel { background: var(--card-bg); }
.search-item-name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-item-meta { font-size: 11px; color: var(--text-muted); flex-shrink: 0; }

#topbar-right { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
#topbar-tools { display: flex; align-items: center; gap: 6px; }
#btn-search-toggle { display: none; }
#btn-overflow { display: none; }
#btn-close-search { display: none; }
.tool-text { display: none; }
.tool-icon { display: inline; }

#canvas-container { position: absolute; top: 46px; left: 0; right: 0; bottom: 0; overflow: hidden; }
#main-canvas { display: block; width: 100%; height: 100%; cursor: grab; }
#main-canvas.grabbing { cursor: grabbing; }

#sidebar-toggle { position: fixed; top: 54px; left: 10px; z-index: 90; }
#sidebar { position: fixed; top: 46px; left: 0; bottom: 0; width: 280px; background: var(--panel-bg); backdrop-filter: blur(8px); border-right: 1px solid var(--border); z-index: 80; display: flex; flex-direction: column; transform: translateX(0); transition: transform 0.25s ease; }
#sidebar.collapsed { transform: translateX(-100%); }
#sidebar-content { padding: 12px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 14px; }
.sidebar-section-title { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.5px; margin-bottom: 6px; display: flex; align-items: center; justify-content: space-between; }
.chips-wrap { display: flex; flex-wrap: wrap; gap: 5px; }
.chip { padding: 3px 8px; border-radius: 12px; font-size: 11px; cursor: pointer; border: 1px solid var(--border); background: var(--card-bg); display: inline-flex; align-items: center; gap: 5px; user-select: none; transition: opacity 0.15s, border-color 0.15s; }
.chip.dim { opacity: 0.35; border-color: transparent; }
.chip-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.chip-count { color: var(--text-muted); font-size: 10px; margin-left: 2px; }

#detail-panel { position: fixed; top: 46px; right: 0; bottom: 0; width: 330px; background: var(--panel-bg); backdrop-filter: blur(8px); border-left: 1px solid var(--border); z-index: 80; display: flex; flex-direction: column; transform: translateX(100%); transition: transform 0.25s ease; box-shadow: -4px 0 20px rgba(0,0,0,0.15); }
#detail-panel.open { transform: translateX(0); }
#detail-header { padding: 12px; border-bottom: 1px solid var(--border); display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
#detail-title { font-size: 14px; font-weight: 700; word-break: break-word; line-height: 1.3; }
#detail-content { padding: 12px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }
.meta-row { display: flex; align-items: center; justify-content: space-between; gap: 6px; font-size: 12px; }
.meta-label { color: var(--text-muted); font-size: 11px; }
.detail-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.detail-actions button { width: 100%; }
.nbr-group { margin-top: 8px; }
.nbr-group-title { font-size: 11px; font-weight: 600; color: var(--text-muted); margin-bottom: 4px; text-transform: capitalize; }
.nbr-item { padding: 4px 6px; border-radius: 4px; background: var(--card-bg); margin-bottom: 3px; font-size: 11px; cursor: pointer; display: flex; align-items: center; justify-content: space-between; gap: 4px; border: 1px solid var(--border); }
.nbr-item:hover { border-color: var(--accent); color: var(--accent); }
.nbr-item-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.nbr-expander { margin-top: 4px; width: 100%; text-align: center; }

#minimap-wrap { position: fixed; bottom: 12px; right: 12px; width: 160px; height: 110px; background: var(--panel-bg); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; z-index: 70; box-shadow: 0 4px 12px rgba(0,0,0,0.25); }
#minimap-canvas { width: 100%; height: 100%; cursor: crosshair; }

#empty-message { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; color: var(--text-muted); font-size: 15px; pointer-events: none; display: none; }

#toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%) translateY(100px); background: var(--card-bg); border: 1px solid var(--border); color: var(--text); padding: 8px 16px; border-radius: 20px; font-size: 12px; z-index: 300; transition: transform 0.25s ease, opacity 0.25s ease; opacity: 0; pointer-events: none; box-shadow: 0 4px 16px rgba(0,0,0,0.3); }
#toast.show { transform: translateX(-50%) translateY(0); opacity: 1; }

#modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.55); z-index: 250; display: none; align-items: center; justify-content: center; backdrop-filter: blur(2px); }
#help-modal { background: var(--panel-bg); border: 1px solid var(--border); border-radius: 8px; width: 440px; max-width: 90vw; padding: 18px; box-shadow: 0 12px 36px rgba(0,0,0,0.4); }
#help-modal h3 { margin: 0 0 12px 0; font-size: 15px; display: flex; justify-content: space-between; align-items: center; }
.shortcut-row { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.shortcut-key { background: var(--card-bg); border: 1px solid var(--border); padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 11px; font-weight: 700; }

body.presentation #topbar, body.presentation #sidebar, body.presentation #detail-panel, body.presentation #minimap-wrap, body.presentation #sidebar-toggle { display: none !important; }
body.presentation #canvas-container { top: 0 !important; }

@media (max-width: 700px) {
  #topbar { padding: 0 8px; }
  #topbar-left #graph-stats { display: none; }
  #title-badge { font-size: 13px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #btn-search-toggle { display: inline-flex; }
  #btn-overflow { display: inline-flex; }

  #search-wrap { display: none; }
  #search-wrap.expanded {
    display: flex;
    position: absolute;
    left: 6px;
    right: 6px;
    top: 4px;
    bottom: 4px;
    width: auto;
    z-index: 220;
    background: var(--panel-bg);
  }
  #search-wrap.expanded #search-input {
    width: 100%;
    height: 100%;
    padding-left: 28px;
    padding-right: 32px;
  }
  #search-wrap.expanded #btn-close-search {
    display: inline-flex;
    position: absolute;
    right: 6px;
    top: 50%;
    transform: translateY(-50%);
    background: transparent;
    border: none;
    font-size: 16px;
    color: var(--text-muted);
    cursor: pointer;
    padding: 2px 6px;
  }
  #search-wrap.expanded #search-results {
    top: calc(100% + 4px);
    width: 100%;
  }

  #topbar-tools {
    display: none;
    position: absolute;
    top: 48px;
    right: 8px;
    background: var(--panel-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px;
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
    min-width: 170px;
    z-index: 200;
    box-shadow: 0 8px 24px rgba(0,0,0,0.35);
  }
  #topbar-tools.open { display: flex; }
  #topbar-tools select, #topbar-tools button {
    width: 100%;
    text-align: left;
    justify-content: flex-start;
  }
  .tool-text { display: inline; }
  .tool-icon { display: none; }

  #sidebar { width: 100%; top: auto; bottom: 0; height: 50vh; border-right: none; border-top: 1px solid var(--border); transform: translateY(100%); }
  #sidebar.collapsed { transform: translateY(100%); }
  #sidebar:not(.collapsed) { transform: translateY(0); }
  #detail-panel { width: 100%; top: auto; bottom: 0; height: 55vh; border-left: none; border-top: 1px solid var(--border); transform: translateY(100%); }
  #detail-panel.open { transform: translateY(0); }
  #minimap-wrap { display: none; }
}
</style>
</head>
<body>
<div id="topbar">
  <div id="topbar-left">
    <button id="sidebar-toggle" aria-label="Toggle filters sidebar" title="Toggle filters sidebar">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="18" x2="21" y2="18"></line></svg>
    </button>
    <div id="title-badge">__TITLE__</div>
    <div id="graph-stats"></div>
  </div>
  <div id="search-wrap">
    <span id="search-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg></span>
    <input id="search-input" placeholder="Search nodes... (/)" aria-label="Search nodes by name or ID" autocomplete="off">
    <button id="btn-close-search" aria-label="Close search" title="Close search">&times;</button>
    <div id="search-results"></div>
  </div>
  <div id="topbar-right">
    <button id="btn-search-toggle" aria-label="Open search" title="Search (/)">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
    </button>
    <button id="btn-reset" aria-label="Reset camera" title="Reset camera (R)">Fit</button>
    <button id="btn-overflow" aria-label="Menu" title="Menu">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="2"></circle><circle cx="12" cy="12" r="2"></circle><circle cx="12" cy="19" r="2"></circle></svg>
    </button>
    <div id="topbar-tools">
      <select id="layout-select" aria-label="Select layout mode">
        <option value="force">Force (L)</option>
        <option value="radial">Radial (L)</option>
        <option value="layered">Layered (L)</option>
      </select>
      <button id="btn-color" aria-label="Toggle color mode" title="Color by type or community (C)">Color: Type</button>
      <select id="export-select" aria-label="Export graph">
        <option value="">Export</option>
        <option value="png">PNG (2x)</option>
        <option value="svg">SVG</option>
        <option value="json">JSON</option>
      </select>
      <button id="btn-present" aria-label="Toggle presentation mode" title="Presentation mode (P)">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"></path></svg>
        <span class="tool-text">Present</span>
      </button>
      <button id="btn-help" aria-label="Keyboard shortcuts" title="Help (?)">
        <span class="tool-text">Shortcuts</span><span class="tool-icon">?</span>
      </button>
    </div>
  </div>
</div>

<div id="sidebar" class="collapsed">
  <div id="sidebar-content">
    <div>
      <div class="sidebar-section-title">Saved Views</div>
      <div style="display: flex; gap: 6px; margin-bottom: 6px;">
        <select id="saved-views-select" style="flex: 1;" aria-label="Saved views">
          <option value="">Select view...</option>
        </select>
        <button id="btn-save-view" class="small" aria-label="Save current view">Save</button>
        <button id="btn-del-view" class="small" aria-label="Delete selected view" style="display: none;">Del</button>
      </div>
    </div>
    <div>
      <div class="sidebar-section-title">
        <span>Node Types</span>
        <button id="btn-all-types" class="small" aria-label="Toggle all types">All</button>
      </div>
      <div id="type-chips" class="chips-wrap"></div>
    </div>
    <div>
      <div class="sidebar-section-title">
        <span>Communities</span>
        <button id="btn-all-comms" class="small" aria-label="Toggle all communities">All</button>
      </div>
      <div id="comm-chips" class="chips-wrap"></div>
    </div>
    <div>
      <div class="sidebar-section-title">
        <span>Edge Types</span>
        <button id="btn-all-edges" class="small" aria-label="Toggle all edge types">All</button>
      </div>
      <div id="edge-chips" class="chips-wrap"></div>
    </div>
  </div>
</div>

<div id="detail-panel">
  <div id="detail-header">
    <div>
      <div id="detail-title">Node Details</div>
      <div id="detail-subtitle" style="margin-top: 3px; font-size: 11px; color: var(--text-muted);"></div>
    </div>
    <button id="btn-close-detail" aria-label="Close detail panel" style="padding: 3px 6px;">&times;</button>
  </div>
  <div id="detail-content">
    <div class="meta-row">
      <span class="meta-label">ID</span>
      <div style="display: flex; align-items: center; gap: 4px;">
        <code id="detail-id" style="font-size: 11px; background: var(--card-bg); padding: 2px 5px; border-radius: 4px;"></code>
        <button id="btn-copy-id" class="small" aria-label="Copy node ID">Copy</button>
      </div>
    </div>
    <div class="meta-row">
      <span class="meta-label">Degree</span>
      <span id="detail-degree"></span>
    </div>
    <div class="meta-row">
      <span class="meta-label">PageRank</span>
      <span id="detail-rank"></span>
    </div>
    <div class="meta-row">
      <span class="meta-label">Community</span>
      <span id="detail-comm"></span>
    </div>
    <div class="detail-actions">
      <button id="btn-focus-node" aria-label="Focus neighborhood">Focus (F)</button>
      <button id="btn-path-mode" aria-label="Find path to another node">Path to...</button>
      <button id="btn-trace-up" aria-label="Trace upstream reach">Upstream</button>
      <button id="btn-trace-down" aria-label="Trace downstream reach">Downstream</button>
      <button id="btn-open-commons" style="grid-column: span 2;" aria-label="Open in Commons">Open in Commons</button>
    </div>
    <div id="path-box" style="display: none; padding: 8px; background: var(--card-bg); border: 1px solid var(--border); border-radius: 6px;">
      <div style="font-weight: 600; font-size: 11px; margin-bottom: 4px; display: flex; justify-content: space-between;">
        <span>Path Traversal</span>
        <button id="btn-clear-path" class="small" aria-label="Clear active path">&times;</button>
      </div>
      <div id="path-steps" style="font-size: 11px; max-height: 120px; overflow-y: auto;"></div>
    </div>
    <div>
      <div class="sidebar-section-title">Neighbours</div>
      <div id="detail-nbrs"></div>
    </div>
  </div>
</div>

<div id="minimap-wrap">
  <canvas id="minimap-canvas"></canvas>
</div>

<div id="canvas-container">
  <canvas id="main-canvas"></canvas>
  <div id="empty-message">No nodes in graph</div>
</div>

<div id="toast"></div>

<div id="modal-backdrop">
  <div id="help-modal">
    <h3>
      <span>Keyboard Shortcuts</span>
      <button id="btn-close-help" class="small" aria-label="Close help">&times;</button>
    </h3>
    <div class="shortcut-row"><span>Focus search</span><span class="shortcut-key">/</span></div>
    <div class="shortcut-row"><span>Toggle focus neighborhood (1-hop / 2-hop / off)</span><span class="shortcut-key">f</span></div>
    <div class="shortcut-row"><span>Cycle layout mode (Force / Radial / Layered)</span><span class="shortcut-key">l</span></div>
    <div class="shortcut-row"><span>Toggle color mode (Type / Community)</span><span class="shortcut-key">c</span></div>
    <div class="shortcut-row"><span>Reset camera to fit graph</span><span class="shortcut-key">r</span></div>
    <div class="shortcut-row"><span>Toggle presentation mode</span><span class="shortcut-key">p</span></div>
    <div class="shortcut-row"><span>Clear selection / path / focus</span><span class="shortcut-key">Esc</span></div>
    <div class="shortcut-row"><span>Show help overlay</span><span class="shortcut-key">?</span></div>
  </div>
</div>

<script>
(function() {
  const G = __DATA__;
  const rawNodes = G.nodes || {};
  const rawEdges = G.edges || [];
  const commList = G.communities || [];

  const TYPE_COLORS = {
    project: '#f6c344', file: '#5aa9e6', function: '#7bd389', method: '#a1e3a1',
    class: '#e07a5f', module: '#9a8c98', memory: '#c77dff', decision: '#ff6b6b',
    fact: '#ffd6a5', handoff: '#4cc9f0', checkpoint: '#90e0ef', agent: '#f72585',
    session: '#b5179e', concept: '#bdb2ff', task: '#ffadad', job: '#ff9f1c',
    subtask: '#2ec4b6', workflow: '#ffbf69', task_type: '#cbf3f0', schedule: '#e9c46a',
    leftover: '#f4a261', preference: '#90be6d', usage: '#577590', secret_ref: '#ef476f',
    lease: '#d62828', model: '#adb5bd', scope: '#6c757d', job_result: '#c77dff',
    entity: '#38bdf8'
  };

  const COMM_PALETTE = [
    '#6366f1', '#ec4899', '#14b8a6', '#f59e0b', '#8b5cf6',
    '#10b981', '#3b82f6', '#ef4444', '#06b6d4', '#f97316',
    '#a855f7', '#84cc16', '#e11d48', '#0ea5e9', '#d946ef',
    '#22c55e', '#eab308', '#64748b'
  ];

  function hashColor(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) hash = (hash * 31 + str.charCodeAt(i)) | 0;
    const h = Math.abs(hash) % 360;
    return 'hsl(' + h + ', 70%, 60%)';
  }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, function(c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }

  const commSizes = {};
  Object.values(rawNodes).forEach(function(d) {
    const c = d.community ?? 0;
    commSizes[c] = (commSizes[c] || 0) + 1;
  });
  const sortedCommIds = Object.keys(commSizes).sort(function(a, b) { return commSizes[b] - commSizes[a]; });
  const commIndex = {};
  sortedCommIds.forEach(function(cid, i) { commIndex[cid] = i; });
  const nodesInComm = {};

  const nodes = Object.entries(rawNodes).map(function(entry) {
    const id = entry[0], d = entry[1];
    const c = d.community ?? 0;
    const cIdx = commIndex[c] || 0;
    const inCommIdx = (nodesInComm[c] = (nodesInComm[c] || 0) + 1) - 1;

    const cAngle = cIdx * 2.39996;
    const cRadius = 180 + Math.sqrt(cIdx) * 75;
    const cx = Math.cos(cAngle) * cRadius;
    const cy = Math.sin(cAngle) * cRadius;

    const nAngle = inCommIdx * 2.39996;
    const nRadius = Math.sqrt(inCommIdx + 1) * 18;
    return {
      id: id,
      name: String(d.name ?? id),
      type: String(d.type ?? 'unknown'),
      degree: Number(d.degree ?? 0),
      rank: Number(d.rank ?? 0),
      community: Number(d.community ?? 0),
      cluster: d.cluster, size: Number(d.size ?? 0), outside: !!d.outside,
      x: cx + Math.cos(nAngle) * nRadius,
      y: cy + Math.sin(nAngle) * nRadius,
      vx: 0,
      vy: 0,
      targetX: null,
      targetY: null
    };
  });

  const nodeMap = Object.fromEntries(nodes.map(function(n) { return [n.id, n]; }));
  const edges = rawEdges.filter(function(e) { return nodeMap[e.src] && nodeMap[e.dst]; });

  const adj = {};
  const inEdges = {};
  const outEdges = {};
  nodes.forEach(function(n) {
    adj[n.id] = [];
    inEdges[n.id] = [];
    outEdges[n.id] = [];
  });
  edges.forEach(function(e) {
    adj[e.src].push({ target: e.dst, type: e.type, dir: 'out' });
    adj[e.dst].push({ target: e.src, type: e.type, dir: 'in' });
    outEdges[e.src].push(e);
    inEdges[e.dst].push(e);
  });

  const topRankNodes = new Set(nodes.slice().sort(function(a, b) { return b.rank - a.rank; }).slice(0, 30).map(function(n) { return n.id; }));
  const topCommNodes = new Set();
  const commBestNode = {};
  nodes.forEach(function(n) {
    const c = n.community;
    if (!commBestNode[c] || n.rank > commBestNode[c].rank) {
      commBestNode[c] = n;
    }
  });
  Object.values(commBestNode).forEach(function(n) {
    topCommNodes.add(n.id);
  });

  const mainCanvas = document.getElementById('main-canvas');
  const ctx = mainCanvas.getContext('2d');
  const miniCanvas = document.getElementById('minimap-canvas');
  const miniCtx = miniCanvas.getContext('2d');

  let ox = 0, oy = 0, sc = 1;
  let layoutMode = 'force';
  let colorMode = 'type';
  let selNode = null;
  let hoverNode = null;
  let focusHops = 0;
  let pathMode = false;
  let pathSource = null;
  let pathNodes = null;
  let traceNodes = null;

  const hiddenTypes = new Set();
  const hiddenComms = new Set();
  const hiddenEdgeTypes = new Set();

  let alpha = 1.0;
  let animFrame = null;
  let dragNode = null;
  let panStart = null;
  let pinchDist = null;
  let userInteractedCamera = false;
  let layoutSettled = false;

  function getThemeTokens() {
    const isLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
    if (isLight) {
      return {
        edge: '#475569',
        edgeAlpha: 0.65,
        edgeHighlight: '#312e81',
        edgeHighlightAlpha: 0.95,
        pathEdge: '#1d4ed8',
        labelBg: 'rgba(255, 255, 255, 0.95)',
        labelBorder: 'rgba(148, 163, 184, 0.9)',
        labelText: '#0f172a',
        canvasBg: '#f8fafc'
      };
    }
    return {
      edge: '#64748b',
      edgeAlpha: 0.4,
      edgeHighlight: '#c7d2fe',
      edgeHighlightAlpha: 0.9,
      pathEdge: '#38bdf8',
      labelBg: 'rgba(23, 27, 38, 0.92)',
      labelBorder: 'rgba(71, 85, 105, 0.9)',
      labelText: '#f8fafc',
      canvasBg: '#0f1117'
    };
  }

  function isNodeVisible(n) {
    if (hiddenTypes.has(n.type)) return false;
    if (hiddenComms.has(n.community)) return false;
    if (focusHops > 0 && selNode) {
      if (focusHops === 1) {
        if (n.id !== selNode.id && !adj[selNode.id].some(function(edge) { return edge.target === n.id; })) return false;
      } else if (focusHops === 2) {
        if (n.id !== selNode.id) {
          const direct = adj[selNode.id].map(function(edge) { return edge.target; });
          if (!direct.includes(n.id)) {
            let found2 = false;
            for (let i = 0; i < direct.length; i++) {
              if (adj[direct[i]].some(function(edge) { return edge.target === n.id; })) { found2 = true; break; }
            }
            if (!found2) return false;
          }
        }
      }
    }
    return true;
  }

  function isEdgeVisible(e) {
    if (hiddenEdgeTypes.has(e.type)) return false;
    const s = nodeMap[e.src], d = nodeMap[e.dst];
    if (!s || !d || !isNodeVisible(s) || !isNodeVisible(d)) return false;
    return true;
  }

  function getNodeColor(n) {
    if (colorMode === 'type') {
      return TYPE_COLORS[n.type] || hashColor(n.type);
    }
    return COMM_PALETTE[n.community % COMM_PALETTE.length];
  }

  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(function() { t.classList.remove('show'); }, 2000);
  }

  class Quad {
    constructor(x, y, w, h) {
      this.x = x; this.y = y; this.w = w; this.h = h;
      this.cx = 0; this.cy = 0; this.mass = 0;
      this.node = null;
      this.children = null;
    }
    insert(node) {
      if (this.mass === 0) {
        this.node = node;
        this.cx = node.x; this.cy = node.y; this.mass = 1;
        return;
      }
      if (!this.children) {
        this.subdivide();
        if (this.node) {
          this.insertChild(this.node);
          this.node = null;
        }
      }
      this.insertChild(node);
      this.cx = (this.cx * this.mass + node.x) / (this.mass + 1);
      this.cy = (this.cy * this.mass + node.y) / (this.mass + 1);
      this.mass++;
    }
    subdivide() {
      const hw = this.w / 2, hh = this.h / 2;
      this.children = [
        new Quad(this.x, this.y, hw, hh),
        new Quad(this.x + hw, this.y, hw, hh),
        new Quad(this.x, this.y + hh, hw, hh),
        new Quad(this.x + hw, this.y + hh, hw, hh)
      ];
    }
    insertChild(node) {
      const hw = this.w / 2, hh = this.h / 2;
      const idx = (node.x >= this.x + hw ? 1 : 0) + (node.y >= this.y + hh ? 2 : 0);
      this.children[idx].insert(node);
    }
    applyForce(node, theta, kRepel) {
      if (this.mass === 0 || this.node === node) return;
      const dx = this.cx - node.x, dy = this.cy - node.y;
      const d2 = dx * dx + dy * dy + 1;
      const d = Math.sqrt(d2);
      if (!this.children || (this.w / d) < theta) {
        const f = (kRepel * this.mass) / d2;
        node.vx -= (dx / d) * f;
        node.vy -= (dy / d) * f;
      } else {
        for (let i = 0; i < 4; i++) {
          this.children[i].applyForce(node, theta, kRepel);
        }
      }
    }
  }

  function computeRadialLayout() {
    let rootId = null;
    if (selNode && isNodeVisible(selNode)) rootId = selNode.id;
    else if (G.root && nodeMap[G.root] && isNodeVisible(nodeMap[G.root])) rootId = G.root;
    else {
      const vis = nodes.filter(isNodeVisible).sort(function(a, b) { return b.rank - a.rank; });
      if (vis.length) rootId = vis[0].id;
    }
    if (!rootId) return;

    const depth = {};
    depth[rootId] = 0;
    const queue = [rootId];
    while (queue.length > 0) {
      const curr = queue.shift();
      const d = depth[curr];
      const nbrs = adj[curr] || [];
      for (let i = 0; i < nbrs.length; i++) {
        const nextId = nbrs[i].target;
        if (depth[nextId] === undefined && isNodeVisible(nodeMap[nextId])) {
          depth[nextId] = d + 1;
          queue.push(nextId);
        }
      }
    }

    const groups = {};
    nodes.filter(isNodeVisible).forEach(function(n) {
      const d = depth[n.id] !== undefined ? depth[n.id] : 8;
      if (!groups[d]) groups[d] = [];
      groups[d].push(n);
    });

    Object.entries(groups).forEach(function(entry) {
      const d = Number(entry[0]), gNodes = entry[1];
      if (d === 0) {
        gNodes[0].targetX = 0;
        gNodes[0].targetY = 0;
      } else {
        const r = d * 140;
        const count = gNodes.length;
        gNodes.forEach(function(n, i) {
          const a = (i / count) * Math.PI * 2;
          n.targetX = Math.cos(a) * r;
          n.targetY = Math.sin(a) * r;
        });
      }
    });
  }

  function computeLayeredLayout() {
    const vis = nodes.filter(isNodeVisible);
    const visSet = new Set(vis.map(function(n) { return n.id; }));
    const layer = {};
    vis.forEach(function(n) { layer[n.id] = 0; });

    for (let round = 0; round < 15; round++) {
      let changed = false;
      edges.forEach(function(e) {
        if (visSet.has(e.src) && visSet.has(e.dst) && !hiddenEdgeTypes.has(e.type)) {
          if (layer[e.dst] < layer[e.src] + 1) {
            layer[e.dst] = layer[e.src] + 1;
            changed = true;
          }
        }
      });
      if (!changed) break;
    }

    const groups = {};
    let maxL = 0;
    vis.forEach(function(n) {
      const l = layer[n.id] || 0;
      if (l > maxL) maxL = l;
      if (!groups[l]) groups[l] = [];
      groups[l].push(n);
    });

    Object.entries(groups).forEach(function(entry) {
      const l = Number(entry[0]), gNodes = entry[1];
      const x = (l - maxL / 2) * 190;
      const count = gNodes.length;
      gNodes.forEach(function(n, i) {
        const y = (i - (count - 1) / 2) * 60;
        n.targetX = x;
        n.targetY = y;
      });
    });
  }

  function stepPhysics() {
    if (layoutMode === 'radial') {
      let moved = false;
      nodes.filter(isNodeVisible).forEach(function(n) {
        if (n.targetX !== null && n !== dragNode) {
          n.x += (n.targetX - n.x) * 0.14;
          n.y += (n.targetY - n.y) * 0.14;
          moved = true;
        }
      });
      return moved;
    }

    if (layoutMode === 'layered') {
      let moved = false;
      nodes.filter(isNodeVisible).forEach(function(n) {
        if (n.targetX !== null && n !== dragNode) {
          n.x += (n.targetX - n.x) * 0.14;
          n.y += (n.targetY - n.y) * 0.14;
          moved = true;
        }
      });
      return moved;
    }

    if (alpha < 0.02) {
      if (!layoutSettled) {
        layoutSettled = true;
        if (!userInteractedCamera) fitCamera();
      }
      return false;
    }

    const visNodes = nodes.filter(isNodeVisible);
    if (!visNodes.length) return false;

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    visNodes.forEach(function(n) {
      if (n.x < minX) minX = n.x;
      if (n.x > maxX) maxX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.y > maxY) maxY = n.y;
    });
    const size = Math.max(maxX - minX, maxY - minY, 100) + 200;
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    const quad = new Quad(cx - size / 2, cy - size / 2, size, size);
    visNodes.forEach(function(n) { quad.insert(n); });

    const sqrtN = Math.sqrt(visNodes.length);
    const kRepel = 240 + sqrtN * 15;

    visNodes.forEach(function(n) {
      if (n === dragNode) return;
      quad.applyForce(n, 0.8, kRepel);
    });

    const commCentroids = {};
    visNodes.forEach(function(n) {
      const c = n.community;
      if (!commCentroids[c]) commCentroids[c] = { x: 0, y: 0, count: 0 };
      commCentroids[c].x += n.x;
      commCentroids[c].y += n.y;
      commCentroids[c].count++;
    });
    const activeComms = [];
    const commRadius = {};
    Object.entries(commCentroids).forEach(function(entry) {
      const cid = Number(entry[0]);
      const c = entry[1];
      if (c.count > 0) {
        c.x /= c.count;
        c.y /= c.count;
        const r = Math.sqrt(c.count) * 14 + 20;
        commRadius[cid] = r;
        activeComms.push({ id: cid, x: c.x, y: c.y, count: c.count, r: r });
      }
    });

    edges.forEach(function(e) {
      if (!isEdgeVisible(e)) return;
      const u = nodeMap[e.src], v = nodeMap[e.dst];
      const dx = v.x - u.x, dy = v.y - u.y;
      const d = Math.sqrt(dx * dx + dy * dy) + 0.1;
      const sameComm = (u.community === v.community);
      const ru = commRadius[u.community] || 25, rv = commRadius[v.community] || 25;
      const restDist = sameComm ? (20 + sqrtN * 0.4) : (ru + rv + 45 + sqrtN * 1.8);
      const kSpring = sameComm ? 0.045 : 0.001;
      const f = (d - restDist) * kSpring;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      if (u !== dragNode) { u.vx += fx; u.vy += fy; }
      if (v !== dragNode) { v.vx -= fx; v.vy -= fy; }
    });

    const kCentroidPull = 0.05 * alpha;
    visNodes.forEach(function(n) {
      if (n === dragNode) return;
      const c = commCentroids[n.community];
      if (c && c.count > 1) {
        n.vx += (c.x - n.x) * kCentroidPull;
        n.vy += (c.y - n.y) * kCentroidPull;
      }
    });

    const commShift = {};
    activeComms.forEach(function(c) { commShift[c.id] = { fx: 0, fy: 0 }; });
    for (let i = 0; i < activeComms.length; i++) {
      const c1 = activeComms[i];
      for (let j = i + 1; j < activeComms.length; j++) {
        const c2 = activeComms[j];
        const dx = c2.x - c1.x, dy = c2.y - c1.y;
        const d = Math.sqrt(dx * dx + dy * dy) + 0.1;
        const targetD = c1.r + c2.r + 30 + sqrtN * 0.8;
        if (d < targetD) {
          const push = ((targetD - d) / targetD) * (3.5 + sqrtN * 0.05) * alpha;
          const fx = (dx / d) * push, fy = (dy / d) * push;
          commShift[c1.id].fx -= fx;
          commShift[c1.id].fy -= fy;
          commShift[c2.id].fx += fx;
          commShift[c2.id].fy += fy;
        } else {
          const rep = ((1800 + sqrtN * 40) * alpha) / (d * d);
          const fx = (dx / d) * rep, fy = (dy / d) * rep;
          commShift[c1.id].fx -= fx;
          commShift[c1.id].fy -= fy;
          commShift[c2.id].fx += fx;
          commShift[c2.id].fy += fy;
        }
      }
    }
    visNodes.forEach(function(n) {
      if (n === dragNode) return;
      const cs = commShift[n.community];
      if (cs) {
        n.vx += cs.fx;
        n.vy += cs.fy;
      }
    });

    visNodes.forEach(function(n) {
      if (n === dragNode) return;
      n.vx -= n.x * 0.0001;
      n.vy -= n.y * 0.0001;
      n.vx *= 0.74;
      n.vy *= 0.74;
      n.x += Math.max(-28, Math.min(28, n.vx * alpha));
      n.y += Math.max(-28, Math.min(28, n.vy * alpha));
    });

    alpha *= 0.965;
    return true;
  }

  function drawArrow(fromX, fromY, toX, toY, radius, color) {
    const dx = toX - fromX, dy = toY - fromY;
    const len = Math.sqrt(dx * dx + dy * dy);
    if (len < radius + 10) return;
    const ux = dx / len, uy = dy / len;
    const tipX = toX - ux * (radius + 2);
    const tipY = toY - uy * (radius + 2);
    const arrowLen = 8, arrowWidth = 5;
    const leftX = tipX - ux * arrowLen - uy * arrowWidth;
    const leftY = tipY - uy * arrowLen + ux * arrowWidth;
    const rightX = tipX - ux * arrowLen + uy * arrowWidth;
    const rightY = tipY - uy * arrowLen - ux * arrowWidth;

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(tipX, tipY);
    ctx.lineTo(leftX, leftY);
    ctx.lineTo(rightX, rightY);
    ctx.closePath();
    ctx.fill();
  }

  function draw() {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, mainCanvas.width, mainCanvas.height);

    if (nodes.length === 0) {
      document.getElementById('empty-message').style.display = 'block';
      return;
    }
    document.getElementById('empty-message').style.display = 'none';

    ctx.setTransform(sc, 0, 0, sc, mainCanvas.width / 2 + ox, mainCanvas.height / 2 + oy);

    const hasActiveFocus = selNode || hoverNode || pathNodes || traceNodes;
    const highlightIds = new Set();
    if (selNode) highlightIds.add(selNode.id);
    if (hoverNode) {
      highlightIds.add(hoverNode.id);
      (adj[hoverNode.id] || []).forEach(function(e) { highlightIds.add(e.target); });
    }
    if (pathNodes) pathNodes.forEach(function(id) { highlightIds.add(id); });
    if (traceNodes) traceNodes.forEach(function(id) { highlightIds.add(id); });

    // Draw community halos if coloring by community
    if (colorMode === 'community') {
      nodes.forEach(function(n) {
        if (!isNodeVisible(n)) return;
        const col = getNodeColor(n);
        ctx.fillStyle = col;
        ctx.globalAlpha = 0.08;
        ctx.beginPath();
        ctx.arc(n.x, n.y, 16 + Math.sqrt(n.degree) * 4, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.globalAlpha = 1.0;
    }

    // Edges
    const tokens = getThemeTokens();
    edges.forEach(function(e) {
      if (!isEdgeVisible(e)) return;
      const u = nodeMap[e.src], v = nodeMap[e.dst];
      const isPathEdge = pathNodes && pathNodes.includes(e.src) && pathNodes.includes(e.dst);
      const isConnected = highlightIds.has(e.src) && highlightIds.has(e.dst);

      let alphaEdge = tokens.edgeAlpha;
      let strokeCol = tokens.edge;
      let strokeWidth = 1.2;

      if (isPathEdge) {
        alphaEdge = 0.95;
        strokeCol = tokens.pathEdge;
        strokeWidth = 2.5;
      } else if (hasActiveFocus) {
        if (isConnected) {
          alphaEdge = tokens.edgeHighlightAlpha;
          strokeCol = tokens.edgeHighlight;
          strokeWidth = 1.8;
        } else {
          alphaEdge = 0.05;
        }
      }

      ctx.strokeStyle = strokeCol;
      ctx.globalAlpha = alphaEdge;
      ctx.lineWidth = strokeWidth;
      ctx.beginPath();
      ctx.moveTo(u.x, u.y);
      ctx.lineTo(v.x, v.y);
      ctx.stroke();

      if (sc >= 0.7 && alphaEdge > 0.1) {
        const vRadius = 4 + Math.min(10, Math.sqrt(v.degree) * 2);
        drawArrow(u.x, u.y, v.x, v.y, vRadius, strokeCol);
      }
    });

    ctx.globalAlpha = 1.0;

    // Nodes
    nodes.forEach(function(n) {
      if (!isNodeVisible(n)) return;
      const isHover = hoverNode && hoverNode.id === n.id;
      const isSel = selNode && selNode.id === n.id;
      const isHighlighted = highlightIds.has(n.id);
      const isPathNode = pathNodes && pathNodes.includes(n.id);

      let r = 4 + (n.size ? Math.min(26, 3 + Math.sqrt(n.size) * 2.2) : Math.min(10, Math.sqrt(n.degree) * 2) * (n.outside ? 0.6 : 1));
      if (isSel || isHover) r += 3;

      let nodeAlpha = 1.0;
      if (hasActiveFocus && !isHighlighted) nodeAlpha = 0.15;

      ctx.globalAlpha = nodeAlpha;
      ctx.fillStyle = getNodeColor(n);
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fill();

      if (isSel || isPathNode) {
        ctx.lineWidth = isSel ? 2.5 : 1.8;
        ctx.strokeStyle = '#ffffff';
        ctx.stroke();
      }
    });

    // Screen-space labels with collision grid and theme background pill
    ctx.setTransform(1, 0, 0, 1, 0, 0);

    const labelCandidates = [];
    nodes.forEach(function(n) {
      if (!isNodeVisible(n)) return;
      const isSel = selNode && selNode.id === n.id;
      const isHover = hoverNode && hoverNode.id === n.id;
      const isNbr = (hoverNode && adj[hoverNode.id].some(function(e) { return e.target === n.id; })) ||
                    (selNode && adj[selNode.id].some(function(e) { return e.target === n.id; }));
      const isTopRank = topRankNodes.has(n.id);
      const isPathNode = pathNodes && pathNodes.includes(n.id);

      let priority = 0;
      if (isSel || isHover) priority = 5;
      else if (isPathNode) priority = 4;
      else if (isNbr) priority = 3;
      else if (isTopRank) priority = 2;
      else if (sc > 1.2) priority = 1;

      if (priority > 0) labelCandidates.push({ node: n, priority: priority });
    });

    labelCandidates.sort(function(a, b) { return b.priority - a.priority; });

    const occCells = new Set();
    const cellW = 100, cellH = 24;

    ctx.font = '500 12px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';

    labelCandidates.forEach(function(item) {
      const n = item.node;
      const sx = n.x * sc + ox + mainCanvas.width / 2;
      const sy = n.y * sc + oy + mainCanvas.height / 2;
      if (sx < -80 || sx > mainCanvas.width + 80 || sy < -30 || sy > mainCanvas.height + 30) return;

      const gx = Math.floor(sx / cellW), gy = Math.floor(sy / cellH);
      const key = gx + ',' + gy;
      if (item.priority < 4 && (occCells.has(key) || occCells.has((gx + 1) + ',' + gy) || occCells.has((gx - 1) + ',' + gy))) return;

      occCells.add(key);
      occCells.add((gx + 1) + ',' + gy);

      let textAlpha = 1.0;
      if (hasActiveFocus && !highlightIds.has(n.id)) textAlpha = 0.2;
      ctx.globalAlpha = textAlpha;

      const nodeScreenR = (4 + (n.size ? Math.min(26, 3 + Math.sqrt(n.size) * 2.2) : Math.min(10, Math.sqrt(n.degree) * 2) * (n.outside ? 0.6 : 1))) * sc;
      let lx = sx + Math.max(nodeScreenR + 4, 10);
      const ly = sy;

      const textW = ctx.measureText(n.name).width;
      const pillH = 18;
      const pillW = textW + 8;
      let pillX = lx - 3;
      const pillY = ly - pillH / 2;

      if (pillX + pillW > mainCanvas.width - 6) {
        const leftLx = sx - Math.max(nodeScreenR + 4, 10) - textW;
        if (leftLx - 4 >= 6) {
          lx = leftLx;
          pillX = leftLx - 4;
        }
      }

      ctx.beginPath();
      ctx.moveTo(pillX + 3, pillY);
      ctx.arcTo(pillX + pillW, pillY, pillX + pillW, pillY + pillH, 3);
      ctx.arcTo(pillX + pillW, pillY + pillH, pillX, pillY + pillH, 3);
      ctx.arcTo(pillX, pillY + pillH, pillX, pillY, 3);
      ctx.arcTo(pillX, pillY, pillX + pillW, pillY, 3);
      ctx.closePath();
      ctx.fillStyle = tokens.labelBg;
      ctx.fill();
      ctx.strokeStyle = tokens.labelBorder;
      ctx.lineWidth = 1;
      ctx.stroke();

      ctx.fillStyle = tokens.labelText;
      ctx.fillText(n.name, lx + 1, ly);
    });

    ctx.globalAlpha = 1.0;
    drawMinimap();
  }

  function drawMinimap() {
    miniCtx.setTransform(1, 0, 0, 1, 0, 0);
    miniCtx.clearRect(0, 0, miniCanvas.width, miniCanvas.height);

    const vis = nodes.filter(isNodeVisible);
    if (!vis.length) return;

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    vis.forEach(function(n) {
      if (n.x < minX) minX = n.x;
      if (n.x > maxX) maxX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.y > maxY) maxY = n.y;
    });

    const pad = 120;
    minX -= pad; maxX += pad; minY -= pad; maxY += pad;
    const w = maxX - minX, h = maxY - minY;
    const ms = Math.min(miniCanvas.width / w, miniCanvas.height / h);

    const offX = (miniCanvas.width - w * ms) / 2 - minX * ms;
    const offY = (miniCanvas.height - h * ms) / 2 - minY * ms;

    vis.forEach(function(n) {
      const mx = n.x * ms + offX;
      const my = n.y * ms + offY;
      miniCtx.fillStyle = getNodeColor(n);
      miniCtx.fillRect(mx - 1, my - 1, 2, 2);
    });

    const vLeft = (-mainCanvas.width / 2 - ox) / sc;
    const vRight = (mainCanvas.width / 2 - ox) / sc;
    const vTop = (-mainCanvas.height / 2 - oy) / sc;
    const vBottom = (mainCanvas.height / 2 - oy) / sc;

    const rx = vLeft * ms + offX;
    const ry = vTop * ms + offY;
    const rw = (vRight - vLeft) * ms;
    const rh = (vBottom - vTop) * ms;

    miniCtx.strokeStyle = '#8b5cf6';
    miniCtx.lineWidth = 1.5;
    miniCtx.strokeRect(rx, ry, rw, rh);
    miniCtx.fillStyle = 'rgba(139, 92, 246, 0.12)';
    miniCtx.fillRect(rx, ry, rw, rh);
  }

  function renderLoop() {
    stepPhysics();
    draw();
    animFrame = requestAnimationFrame(renderLoop);
  }

  function reheat() {
    alpha = Math.max(alpha, 0.55);
    layoutSettled = false;
  }

  function fitCamera() {
    const vis = nodes.filter(isNodeVisible);
    if (!vis.length) { ox = 0; oy = 0; sc = 1; draw(); return; }
    // fit the 2nd-98th percentile box: a few far outliers must not shrink the dense core to a dot
    const xs = [], ys = [];
    vis.forEach(function(n) {
      xs.push((layoutMode !== 'force' && n.targetX !== null) ? n.targetX : n.x);
      ys.push((layoutMode !== 'force' && n.targetY !== null) ? n.targetY : n.y);
    });
    xs.sort(function(a, b) { return a - b; }); ys.sort(function(a, b) { return a - b; });
    const lo = vis.length > 40 ? Math.floor(vis.length * 0.02) : 0, hi = vis.length - 1 - lo;
    const minX = xs[lo], maxX = xs[hi], minY = ys[lo], maxY = ys[hi];
    const gw = Math.max(maxX - minX, 1);
    const gh = Math.max(maxY - minY, 1);
    const availW = mainCanvas.width * 0.84;
    const availH = mainCanvas.height * 0.84;
    sc = Math.min(availW / gw, availH / gh);
    sc = Math.max(0.04, Math.min(sc, 3.0));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    ox = -cx * sc;
    oy = -cy * sc;
    draw();
  }

  function resetCamera() {
    userInteractedCamera = false;
    fitCamera();
  }

  function resize() {
    mainCanvas.width = mainCanvas.clientWidth;
    mainCanvas.height = mainCanvas.clientHeight;
    miniCanvas.width = miniCanvas.clientWidth;
    miniCanvas.height = miniCanvas.clientHeight;
    if (!userInteractedCamera) {
      fitCamera();
    } else {
      draw();
    }
  }
  window.addEventListener('resize', resize);
  resize();

  function animateCameraTo(targetX, targetY, targetSc) {
    const startOx = ox, startOy = oy, startSc = sc;
    const destSc = targetSc || sc;
    const destOx = -targetX * destSc;
    const destOy = -targetY * destSc;
    const startTime = performance.now();
    const dur = 320;

    function stepAnim(now) {
      const p = Math.min(1, (now - startTime) / dur);
      const ease = p * (2 - p);
      ox = startOx + (destOx - startOx) * ease;
      oy = startOy + (destOy - startOy) * ease;
      sc = startSc + (destSc - startSc) * ease;
      draw();
      if (p < 1) requestAnimationFrame(stepAnim);
    }
    requestAnimationFrame(stepAnim);
  }

  function findNodeAt(clientX, clientY) {
    const rect = mainCanvas.getBoundingClientRect();
    const mx = clientX - rect.left;
    const my = clientY - rect.top;
    const wx = (mx - mainCanvas.width / 2 - ox) / sc;
    const wy = (my - mainCanvas.height / 2 - oy) / sc;

    let hit = null;
    let minDist = Infinity;
    nodes.forEach(function(n) {
      if (!isNodeVisible(n)) return;
      const dx = n.x - wx, dy = n.y - wy;
      const d = Math.sqrt(dx * dx + dy * dy);
      const r = (4 + (n.size ? Math.min(26, 3 + Math.sqrt(n.size) * 2.2) : Math.min(10, Math.sqrt(n.degree) * 2) * (n.outside ? 0.6 : 1)) + 6) / sc;
      if (d <= Math.max(14 / sc, r) && d < minDist) {
        minDist = d;
        hit = n;
      }
    });
    return hit;
  }

  function selectNode(n) {
    if (pathMode && pathSource && n && n.id !== pathSource.id) {
      runShortestPath(pathSource.id, n.id);
      pathMode = false;
      document.getElementById('btn-path-mode').classList.remove('active');
      return;
    }

    selNode = n;
    traceNodes = null;
    pathNodes = null;
    document.getElementById('path-box').style.display = 'none';

    if (!n) {
      document.getElementById('detail-panel').classList.remove('open');
      focusHops = 0;
      updateFocusBtn();
      draw();
      return;
    }

    document.getElementById('detail-title').textContent = n.name;
    document.getElementById('detail-subtitle').textContent = n.type + ' (community ' + n.community + ')';
    document.getElementById('detail-id').textContent = n.id;
    document.getElementById('detail-degree').textContent = n.degree;
    document.getElementById('detail-rank').textContent = (n.rank * 100).toFixed(2) + '%';

    const commMeta = commList.find(function(c) { return c.id === n.community; });
    document.getElementById('detail-comm').textContent = commMeta ? commMeta.label : 'Community ' + n.community;

    const nbrContainer = document.getElementById('detail-nbrs');
    nbrContainer.innerHTML = '';

    const groupsByType = {};
    edges.forEach(function(e) {
      if (e.src === n.id || e.dst === n.id) {
        const t = e.type || 'relates_to';
        if (!groupsByType[t]) groupsByType[t] = [];
        const isOut = (e.src === n.id);
        const otherId = isOut ? e.dst : e.src;
        const otherNode = nodeMap[otherId];
        if (otherNode) {
          groupsByType[t].push({
            dir: isOut ? 'out' : 'in',
            other: otherNode
          });
        }
      }
    });

    const edgeTypeKeys = Object.keys(groupsByType).sort();
    if (edgeTypeKeys.length === 0) {
      nbrContainer.innerHTML = '<div style="color: var(--text-muted); font-size: 11px;">No incident edges</div>';
    } else {
      edgeTypeKeys.forEach(function(type) {
        const items = groupsByType[type];
        items.sort(function(a, b) { return a.other.name.localeCompare(b.other.name); });

        const g = document.createElement('div');
        g.className = 'nbr-group';

        const titleEl = document.createElement('div');
        titleEl.className = 'nbr-group-title';
        titleEl.textContent = type + ' (' + items.length + ')';
        g.appendChild(titleEl);

        const listEl = document.createElement('div');
        listEl.className = 'nbr-list';

        function createItemEl(item) {
          const el = document.createElement('div');
          el.className = 'nbr-item';
          const arrow = item.dir === 'out' ? '&rarr;' : '&larr;';
          el.innerHTML = '<span class="nbr-item-name">' + arrow + ' ' + esc(item.other.name) + '</span>' +
                         '<span style="font-size: 10px; color: var(--text-muted); flex-shrink: 0;">' + esc(item.other.type) + '</span>';
          el.onclick = function() {
            selectNode(item.other);
            animateCameraTo(item.other.x, item.other.y);
          };
          return el;
        }

        const limit = 12;
        const visibleSlice = items.slice(0, limit);
        const hiddenSlice = items.slice(limit);

        visibleSlice.forEach(function(item) {
          listEl.appendChild(createItemEl(item));
        });

        if (hiddenSlice.length > 0) {
          const extraBox = document.createElement('div');
          extraBox.style.display = 'none';
          hiddenSlice.forEach(function(item) {
            extraBox.appendChild(createItemEl(item));
          });
          listEl.appendChild(extraBox);

          const moreBtn = document.createElement('button');
          moreBtn.className = 'small nbr-expander';
          moreBtn.textContent = '+' + hiddenSlice.length + ' more';
          let isOpen = false;
          moreBtn.onclick = function() {
            isOpen = !isOpen;
            extraBox.style.display = isOpen ? 'block' : 'none';
            moreBtn.textContent = isOpen ? 'Show less' : ('+' + hiddenSlice.length + ' more');
          };
          listEl.appendChild(moreBtn);
        }

        g.appendChild(listEl);
        nbrContainer.appendChild(g);
      });
    }

    document.getElementById('detail-panel').classList.add('open');
    updateFocusBtn();
    draw();
  }

  function runShortestPath(aId, bId) {
    const queue = [aId];
    const visited = {};
    visited[aId] = null;

    while (queue.length > 0) {
      const curr = queue.shift();
      if (curr === bId) break;
      const nbrs = adj[curr] || [];
      for (let i = 0; i < nbrs.length; i++) {
        const nextId = nbrs[i].target;
        if (visited[nextId] === undefined && isNodeVisible(nodeMap[nextId])) {
          visited[nextId] = curr;
          queue.push(nextId);
        }
      }
    }

    if (visited[bId] === undefined) {
      showToast('No path between nodes');
      return;
    }

    const path = [];
    let curr = bId;
    while (curr !== null) {
      path.push(curr);
      curr = visited[curr];
    }
    path.reverse();
    pathNodes = path;

    const pathBox = document.getElementById('path-box');
    const pathSteps = document.getElementById('path-steps');
    pathBox.style.display = 'block';
    pathSteps.innerHTML = '';

    path.forEach(function(nodeId, idx) {
      const n = nodeMap[nodeId];
      const stepEl = document.createElement('div');
      stepEl.style.padding = '2px 0';
      stepEl.style.cursor = 'pointer';
      stepEl.innerHTML = (idx + 1) + '. <b>' + esc(n.name) + '</b> <span style="color:var(--text-muted)">(' + esc(n.type) + ')</span>';
      stepEl.onclick = function() {
        selectNode(n);
        animateCameraTo(n.x, n.y);
      };
      pathSteps.appendChild(stepEl);
    });

    showToast('Path found: ' + path.length + ' nodes');
    draw();
  }

  function traceReach(dir) {
    if (!selNode) return;
    const visited = new Set();
    const queue = [selNode.id];
    visited.add(selNode.id);

    while (queue.length > 0) {
      const curr = queue.shift();
      const edgeList = dir === 'up' ? inEdges[curr] : outEdges[curr];
      (edgeList || []).forEach(function(e) {
        const nextId = dir === 'up' ? e.src : e.dst;
        if (!visited.has(nextId) && isNodeVisible(nodeMap[nextId])) {
          visited.add(nextId);
          queue.push(nextId);
        }
      });
    }

    traceNodes = Array.from(visited);
    showToast((dir === 'up' ? 'Upstream' : 'Downstream') + ' reach: ' + traceNodes.length + ' nodes');
    draw();
  }

  function updateFocusBtn() {
    const btn = document.getElementById('btn-focus-node');
    if (focusHops === 0) btn.textContent = 'Focus (F)';
    else if (focusHops === 1) btn.textContent = 'Focus: 1-Hop';
    else if (focusHops === 2) btn.textContent = 'Focus: 2-Hop';
    btn.classList.toggle('active', focusHops > 0);
  }

  function toggleFocus() {
    if (!selNode) return;
    focusHops = (focusHops + 1) % 3;
    updateFocusBtn();
    reheat();
    if (!userInteractedCamera) fitCamera();
    else draw();
  }

  function cycleLayout() {
    if (layoutMode === 'force') layoutMode = 'radial';
    else if (layoutMode === 'radial') layoutMode = 'layered';
    else layoutMode = 'force';
    document.getElementById('layout-select').value = layoutMode;
    applyLayout();
  }

  function applyLayout() {
    if (layoutMode === 'radial') computeRadialLayout();
    else if (layoutMode === 'layered') computeLayeredLayout();
    reheat();
    if (!userInteractedCamera) fitCamera();
    else draw();
  }

  function toggleColorMode() {
    colorMode = colorMode === 'type' ? 'community' : 'type';
    document.getElementById('btn-color').textContent = 'Color: ' + (colorMode === 'type' ? 'Type' : 'Community');
    updateChipsUI();
    draw();
  }

  // Sidebar chips initialization
  function initSidebar() {
    const typeCounts = {};
    const commCounts = {};
    const edgeCounts = {};

    nodes.forEach(function(n) {
      typeCounts[n.type] = (typeCounts[n.type] || 0) + 1;
      commCounts[n.community] = (commCounts[n.community] || 0) + 1;
    });
    edges.forEach(function(e) {
      edgeCounts[e.type] = (edgeCounts[e.type] || 0) + 1;
    });

    const typeChips = document.getElementById('type-chips');
    typeChips.innerHTML = '';
    Object.entries(typeCounts).sort(function(a, b) { return b[1] - a[1]; }).forEach(function(entry) {
      const type = entry[0], count = entry[1];
      const chip = document.createElement('div');
      chip.className = 'chip';
      chip.innerHTML = '<span class="chip-dot" style="background:' + (TYPE_COLORS[type] || hashColor(type)) + '"></span>' +
                       '<span>' + esc(type) + '</span><span class="chip-count">' + count + '</span>';
      chip.onclick = function() {
        if (hiddenTypes.has(type)) hiddenTypes.delete(type);
        else hiddenTypes.add(type);
        chip.classList.toggle('dim', hiddenTypes.has(type));
        reheat();
        draw();
      };
      typeChips.appendChild(chip);
    });

    const commChips = document.getElementById('comm-chips');
    commChips.innerHTML = '';
    commList.forEach(function(c) {
      const chip = document.createElement('div');
      chip.className = 'chip';
      const col = COMM_PALETTE[c.id % COMM_PALETTE.length];
      chip.innerHTML = '<span class="chip-dot" style="background:' + col + '"></span>' +
                       '<span>' + esc(c.label) + '</span><span class="chip-count">' + c.size + '</span>';
      chip.onclick = function() {
        if (hiddenComms.has(c.id)) hiddenComms.delete(c.id);
        else hiddenComms.add(c.id);
        chip.classList.toggle('dim', hiddenComms.has(c.id));
        reheat();
        draw();
      };
      commChips.appendChild(chip);
    });

    const edgeChips = document.getElementById('edge-chips');
    edgeChips.innerHTML = '';
    Object.entries(edgeCounts).sort(function(a, b) { return b[1] - a[1]; }).forEach(function(entry) {
      const type = entry[0], count = entry[1];
      const chip = document.createElement('div');
      chip.className = 'chip';
      chip.innerHTML = '<span>' + esc(type) + '</span><span class="chip-count">' + count + '</span>';
      chip.onclick = function() {
        if (hiddenEdgeTypes.has(type)) hiddenEdgeTypes.delete(type);
        else hiddenEdgeTypes.add(type);
        chip.classList.toggle('dim', hiddenEdgeTypes.has(type));
        reheat();
        draw();
      };
      edgeChips.appendChild(chip);
    });

    document.getElementById('graph-stats').textContent = nodes.length + ' nodes · ' + edges.length + ' edges · ' + commList.length + ' communities';
    loadSavedViewsDropdown();
  }

  function updateChipsUI() {
    const typeChips = document.getElementById('type-chips');
    const commChips = document.getElementById('comm-chips');
    if (colorMode === 'type') {
      typeChips.parentElement.style.order = 1;
      commChips.parentElement.style.order = 2;
    } else {
      typeChips.parentElement.style.order = 2;
      commChips.parentElement.style.order = 1;
    }
  }

  // Saved Views with safe localStorage
  function getSavedViews() {
    try {
      const item = localStorage.getItem('commons_graph_saved_views');
      return item ? JSON.parse(item) : {};
    } catch(e) { return {}; }
  }

  function saveViewToStorage(name, viewData) {
    try {
      const views = getSavedViews();
      views[name] = viewData;
      localStorage.setItem('commons_graph_saved_views', JSON.stringify(views));
      return true;
    } catch(e) { return false; }
  }

  function deleteViewFromStorage(name) {
    try {
      const views = getSavedViews();
      delete views[name];
      localStorage.setItem('commons_graph_saved_views', JSON.stringify(views));
      return true;
    } catch(e) { return false; }
  }

  function loadSavedViewsDropdown() {
    const sel = document.getElementById('saved-views-select');
    sel.innerHTML = '<option value="">Select view...</option>';
    const views = getSavedViews();
    Object.keys(views).forEach(function(name) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    });
  }

  document.getElementById('btn-save-view').onclick = function() {
    const name = window.prompt('Enter a name for this view:');
    if (!name || !name.trim()) return;
    const view = {
      name: name.trim(),
      hiddenTypes: Array.from(hiddenTypes),
      hiddenComms: Array.from(hiddenComms),
      hiddenEdgeTypes: Array.from(hiddenEdgeTypes),
      colorMode: colorMode,
      layoutMode: layoutMode,
      focusHops: focusHops,
      selNodeId: selNode ? selNode.id : null,
      camera: { ox: ox, oy: oy, sc: sc }
    };
    if (saveViewToStorage(name.trim(), view)) {
      loadSavedViewsDropdown();
      document.getElementById('saved-views-select').value = name.trim();
      document.getElementById('btn-del-view').style.display = 'inline-flex';
      showToast('Saved view: ' + name);
    } else {
      showToast('Could not save view (storage blocked)');
    }
  };

  document.getElementById('saved-views-select').onchange = function() {
    const name = this.value;
    const btnDel = document.getElementById('btn-del-view');
    if (!name) { btnDel.style.display = 'none'; return; }
    btnDel.style.display = 'inline-flex';
    const views = getSavedViews();
    const v = views[name];
    if (!v) return;

    hiddenTypes.clear();
    (v.hiddenTypes || []).forEach(function(t) { hiddenTypes.add(t); });
    hiddenComms.clear();
    (v.hiddenComms || []).forEach(function(c) { hiddenComms.add(c); });
    hiddenEdgeTypes.clear();
    (v.hiddenEdgeTypes || []).forEach(function(e) { hiddenEdgeTypes.add(e); });

    colorMode = v.colorMode || 'type';
    document.getElementById('btn-color').textContent = 'Color: ' + (colorMode === 'type' ? 'Type' : 'Community');

    layoutMode = v.layoutMode || 'force';
    document.getElementById('layout-select').value = layoutMode;

    if (v.camera) { ox = v.camera.ox; oy = v.camera.oy; sc = v.camera.sc; }
    focusHops = v.focusHops || 0;

    if (v.selNodeId && nodeMap[v.selNodeId]) selectNode(nodeMap[v.selNodeId]);
    else selectNode(null);

    // Update chip styling
    document.querySelectorAll('#type-chips .chip').forEach(function(el) {
      const type = el.children[1].textContent;
      el.classList.toggle('dim', hiddenTypes.has(type));
    });
    document.querySelectorAll('#edge-chips .chip').forEach(function(el) {
      const type = el.children[0].textContent;
      el.classList.toggle('dim', hiddenEdgeTypes.has(type));
    });

    applyLayout();
    showToast('Loaded view: ' + name);
  };

  document.getElementById('btn-del-view').onclick = function() {
    const sel = document.getElementById('saved-views-select');
    const name = sel.value;
    if (!name) return;
    deleteViewFromStorage(name);
    loadSavedViewsDropdown();
    this.style.display = 'none';
    showToast('Deleted view');
  };

  // Search Box Autocomplete
  const searchInput = document.getElementById('search-input');
  const searchResults = document.getElementById('search-results');

  searchInput.addEventListener('input', function(e) {
    const q = e.target.value.toLowerCase().trim();
    if (!q) { searchResults.style.display = 'none'; return; }
    const matches = nodes.filter(function(n) {
      return n.name.toLowerCase().includes(q) || n.id.toLowerCase().includes(q);
    });
    matches.sort(function(a, b) { return b.rank - a.rank; });
    const top8 = matches.slice(0, 8);
    if (!top8.length) {
      searchResults.innerHTML = '<div style="padding: 8px 10px; color: var(--text-muted);">No matches</div>';
      searchResults.style.display = 'block';
      return;
    }
    searchResults.innerHTML = '';
    top8.forEach(function(n) {
      const item = document.createElement('div');
      item.className = 'search-item';
      item.innerHTML = '<span class="search-item-name">' + esc(n.name) + '</span>' +
                       '<span class="search-item-meta">' + esc(n.type) + '</span>';
      item.onclick = function() {
        selectNode(n);
        animateCameraTo(n.x, n.y, Math.max(sc, 1.2));
        searchResults.style.display = 'none';
        searchInput.value = '';
        searchInput.blur();
        closeMobileSearch();
      };
      searchResults.appendChild(item);
    });
    searchResults.style.display = 'block';
  });

  searchInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      const first = searchResults.querySelector('.search-item');
      if (first) {
        first.click();
        searchInput.blur();
        closeMobileSearch();
      }
    } else if (e.key === 'Escape') {
      searchResults.style.display = 'none';
      searchInput.blur();
      closeMobileSearch();
    }
  });

  document.addEventListener('click', function(e) {
    if (!e.target.closest('#search-wrap') && !e.target.closest('#btn-search-toggle')) {
      searchResults.style.display = 'none';
    }
  });

  // Mobile Topbar Handlers
  const searchWrap = document.getElementById('search-wrap');
  const btnSearchToggle = document.getElementById('btn-search-toggle');
  const btnCloseSearch = document.getElementById('btn-close-search');
  const topbarTools = document.getElementById('topbar-tools');
  const btnOverflow = document.getElementById('btn-overflow');

  function openMobileSearch() {
    searchWrap.classList.add('expanded');
    searchInput.focus();
  }
  function closeMobileSearch() {
    searchWrap.classList.remove('expanded');
    searchResults.style.display = 'none';
  }
  if (btnSearchToggle) btnSearchToggle.onclick = openMobileSearch;
  if (btnCloseSearch) btnCloseSearch.onclick = closeMobileSearch;

  if (btnOverflow) {
    btnOverflow.onclick = function(e) {
      e.stopPropagation();
      topbarTools.classList.toggle('open');
    };
  }
  document.addEventListener('click', function(e) {
    if (topbarTools && !e.target.closest('#topbar-tools') && !e.target.closest('#btn-overflow')) {
      topbarTools.classList.remove('open');
    }
  });

  // Mouse / Canvas Events
  mainCanvas.addEventListener('mousedown', function(e) {
    if (e.button !== 0) return;
    const hit = findNodeAt(e.clientX, e.clientY);
    if (hit) {
      dragNode = hit;
      reheat();
    } else {
      panStart = { x: e.clientX - ox, y: e.clientY - oy };
      userInteractedCamera = true;
      mainCanvas.classList.add('grabbing');
    }
  });

  window.addEventListener('mousemove', function(e) {
    if (dragNode) {
      const rect = mainCanvas.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      dragNode.x = (mx - mainCanvas.width / 2 - ox) / sc;
      dragNode.y = (my - mainCanvas.height / 2 - oy) / sc;
      dragNode.vx = 0; dragNode.vy = 0;
      reheat();
      draw();
    } else if (panStart) {
      ox = e.clientX - panStart.x;
      oy = e.clientY - panStart.y;
      userInteractedCamera = true;
      draw();
    } else {
      const hit = findNodeAt(e.clientX, e.clientY);
      if (hit !== hoverNode) {
        hoverNode = hit;
        draw();
      }
    }
  });

  window.addEventListener('mouseup', function(e) {
    if (dragNode) { dragNode = null; }
    if (panStart) {
      panStart = null;
      mainCanvas.classList.remove('grabbing');
    }
  });

  mainCanvas.addEventListener('click', function(e) {
    const hit = findNodeAt(e.clientX, e.clientY);
    selectNode(hit);
  });

  mainCanvas.addEventListener('wheel', function(e) {
    e.preventDefault();
    userInteractedCamera = true;
    const rect = mainCanvas.getBoundingClientRect();
    const mx = e.clientX - rect.left - mainCanvas.width / 2;
    const my = e.clientY - rect.top - mainCanvas.height / 2;
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    const newSc = Math.max(0.04, Math.min(6.0, sc * factor));

    ox = mx - (mx - ox) * (newSc / sc);
    oy = my - (my - oy) * (newSc / sc);
    sc = newSc;
    draw();
  }, { passive: false });

  // Touch Events
  mainCanvas.addEventListener('touchstart', function(e) {
    if (e.touches.length === 1) {
      const touch = e.touches[0];
      const hit = findNodeAt(touch.clientX, touch.clientY);
      if (hit) {
        dragNode = hit;
        reheat();
      } else {
        panStart = { x: touch.clientX - ox, y: touch.clientY - oy };
        userInteractedCamera = true;
      }
    } else if (e.touches.length === 2) {
      dragNode = null;
      panStart = null;
      userInteractedCamera = true;
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      pinchDist = Math.sqrt(dx * dx + dy * dy);
    }
  }, { passive: true });

  mainCanvas.addEventListener('touchmove', function(e) {
    if (e.touches.length === 1) {
      const touch = e.touches[0];
      if (dragNode) {
        const rect = mainCanvas.getBoundingClientRect();
        const mx = touch.clientX - rect.left, my = touch.clientY - rect.top;
        dragNode.x = (mx - mainCanvas.width / 2 - ox) / sc;
        dragNode.y = (my - mainCanvas.height / 2 - oy) / sc;
        reheat();
        draw();
      } else if (panStart) {
        ox = touch.clientX - panStart.x;
        oy = touch.clientY - panStart.y;
        userInteractedCamera = true;
        draw();
      }
    } else if (e.touches.length === 2 && pinchDist) {
      userInteractedCamera = true;
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const factor = dist / pinchDist;
      sc = Math.max(0.04, Math.min(6.0, sc * factor));
      pinchDist = dist;
      draw();
    }
  }, { passive: true });

  mainCanvas.addEventListener('touchend', function(e) {
    dragNode = null;
    panStart = null;
    pinchDist = null;
  }, { passive: true });

  // Minimap Navigation
  let minimapDrag = false;
  function handleMinimapEvent(e) {
    userInteractedCamera = true;
    const rect = miniCanvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    const vis = nodes.filter(isNodeVisible);
    if (!vis.length) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    vis.forEach(function(n) {
      if (n.x < minX) minX = n.x;
      if (n.x > maxX) maxX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.y > maxY) maxY = n.y;
    });
    const pad = 120;
    minX -= pad; maxX += pad; minY -= pad; maxY += pad;
    const w = maxX - minX, h = maxY - minY;
    const ms = Math.min(miniCanvas.width / w, miniCanvas.height / h);
    const offX = (miniCanvas.width - w * ms) / 2 - minX * ms;
    const offY = (miniCanvas.height - h * ms) / 2 - minY * ms;

    const targetWx = (mx - offX) / ms;
    const targetWy = (my - offY) / ms;
    ox = -targetWx * sc;
    oy = -targetWy * sc;
    draw();
  }

  miniCanvas.addEventListener('mousedown', function(e) {
    minimapDrag = true;
    handleMinimapEvent(e);
  });
  window.addEventListener('mousemove', function(e) {
    if (minimapDrag) handleMinimapEvent(e);
  });
  window.addEventListener('mouseup', function() {
    minimapDrag = false;
  });

  // Keyboard Shortcuts
  window.addEventListener('keydown', function(e) {
    const inInput = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
    if (e.key === 'Escape') {
      if (topbarTools) topbarTools.classList.remove('open');
      closeMobileSearch();
      if (pathMode) {
        pathMode = false;
        document.getElementById('btn-path-mode').classList.remove('active');
      }
      selectNode(null);
      document.getElementById('modal-backdrop').style.display = 'none';
      if (document.body.classList.contains('presentation')) {
        document.body.classList.remove('presentation');
        resize();
      }
      return;
    }

    if (inInput) return;

    if (e.key === '/') {
      e.preventDefault();
      if (window.innerWidth <= 700) openMobileSearch();
      searchInput.focus();
    } else if (e.key === 'f') {
      e.preventDefault();
      toggleFocus();
    } else if (e.key === 'r') {
      e.preventDefault();
      resetCamera();
    } else if (e.key === 'l') {
      e.preventDefault();
      cycleLayout();
    } else if (e.key === 'c') {
      e.preventDefault();
      toggleColorMode();
    } else if (e.key === 'p') {
      e.preventDefault();
      document.body.classList.toggle('presentation');
      resize();
    } else if (e.key === '?') {
      e.preventDefault();
      document.getElementById('modal-backdrop').style.display = 'flex';
    }
  });

  // Buttons and Controls
  document.getElementById('sidebar-toggle').onclick = function() {
    document.getElementById('sidebar').classList.toggle('collapsed');
  };
  document.getElementById('layout-select').onchange = function() {
    layoutMode = this.value;
    applyLayout();
  };
  document.getElementById('btn-color').onclick = toggleColorMode;
  document.getElementById('btn-reset').onclick = resetCamera;
  document.getElementById('btn-present').onclick = function() {
    document.body.classList.toggle('presentation');
    resize();
  };
  document.getElementById('btn-help').onclick = function() {
    document.getElementById('modal-backdrop').style.display = 'flex';
  };
  document.getElementById('btn-close-help').onclick = function() {
    document.getElementById('modal-backdrop').style.display = 'none';
  };
  document.getElementById('modal-backdrop').onclick = function(e) {
    if (e.target === this) this.style.display = 'none';
  };
  document.getElementById('btn-close-detail').onclick = function() {
    selectNode(null);
  };
  document.getElementById('btn-focus-node').onclick = toggleFocus;
  document.getElementById('btn-copy-id').onclick = function() {
    if (!selNode) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(selNode.id).then(function() { showToast('Copied ID!'); });
    } else {
      showToast('Copied: ' + selNode.id);
    }
  };
  document.getElementById('btn-path-mode').onclick = function() {
    if (!selNode) return;
    pathMode = !pathMode;
    pathSource = selNode;
    this.classList.toggle('active', pathMode);
    if (pathMode) showToast('Click a target node to find path');
  };
  document.getElementById('btn-clear-path').onclick = function() {
    pathNodes = null;
    document.getElementById('path-box').style.display = 'none';
    draw();
  };
  document.getElementById('btn-trace-up').onclick = function() { traceReach('up'); };
  document.getElementById('btn-trace-down').onclick = function() { traceReach('down'); };
  function openCluster(n) {
    const u = new URL(window.location.href);
    u.searchParams.set('community', String(n.cluster));
    window.location.href = u.toString();
  }
  mainCanvas.addEventListener('dblclick', function() {
    if (selNode && selNode.type === 'cluster') openCluster(selNode);
  });
  document.getElementById('btn-open-commons').onclick = function() {
    if (!selNode) return;
    if (selNode.type === 'cluster') { openCluster(selNode); return; }
    window.parent.postMessage({ type: 'commons:open', id: selNode.id }, '*');
    showToast('Sent commons:open to parent');
  };

  document.getElementById('btn-all-types').onclick = function() {
    if (hiddenTypes.size > 0) hiddenTypes.clear();
    else nodes.forEach(function(n) { hiddenTypes.add(n.type); });
    document.querySelectorAll('#type-chips .chip').forEach(function(el) {
      el.classList.toggle('dim', hiddenTypes.size > 0);
    });
    reheat();
    draw();
  };

  document.getElementById('btn-all-comms').onclick = function() {
    if (hiddenComms.size > 0) hiddenComms.clear();
    else commList.forEach(function(c) { hiddenComms.add(c.id); });
    document.querySelectorAll('#comm-chips .chip').forEach(function(el) {
      el.classList.toggle('dim', hiddenComms.size > 0);
    });
    reheat();
    draw();
  };

  document.getElementById('btn-all-edges').onclick = function() {
    if (hiddenEdgeTypes.size > 0) hiddenEdgeTypes.clear();
    else edges.forEach(function(e) { hiddenEdgeTypes.add(e.type); });
    document.querySelectorAll('#edge-chips .chip').forEach(function(el) {
      el.classList.toggle('dim', hiddenEdgeTypes.size > 0);
    });
    reheat();
    draw();
  };

  // Export features
  document.getElementById('export-select').onchange = function() {
    const fmt = this.value;
    this.value = '';
    if (fmt === 'png') exportPNG();
    else if (fmt === 'svg') exportSVG();
    else if (fmt === 'json') exportJSON();
  };

  function exportPNG() {
    const tokens = getThemeTokens();
    const offCanvas = document.createElement('canvas');
    offCanvas.width = mainCanvas.width * 2;
    offCanvas.height = mainCanvas.height * 2;
    const octx = offCanvas.getContext('2d');
    octx.scale(2, 2);
    octx.fillStyle = tokens.canvasBg;
    octx.fillRect(0, 0, mainCanvas.width, mainCanvas.height);
    octx.drawImage(mainCanvas, 0, 0);

    const a = document.createElement('a');
    a.download = 'commons-graph.png';
    a.href = offCanvas.toDataURL('image/png');
    a.click();
    showToast('Exported PNG');
  }

  function exportSVG() {
    const tokens = getThemeTokens();
    const w = mainCanvas.width, h = mainCanvas.height;
    let svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">';
    svg += '<rect width="100%" height="100%" fill="' + tokens.canvasBg + '"/>';
    svg += '<g transform="translate(' + (w / 2 + ox) + ',' + (h / 2 + oy) + ') scale(' + sc + ')">';

    edges.forEach(function(e) {
      if (!isEdgeVisible(e)) return;
      const u = nodeMap[e.src], v = nodeMap[e.dst];
      svg += '<line x1="' + u.x + '" y1="' + u.y + '" x2="' + v.x + '" y2="' + v.y + '" stroke="' + tokens.edge + '" stroke-opacity="0.3" stroke-width="1"/>';
    });

    nodes.forEach(function(n) {
      if (!isNodeVisible(n)) return;
      const r = 4 + (n.size ? Math.min(26, 3 + Math.sqrt(n.size) * 2.2) : Math.min(10, Math.sqrt(n.degree) * 2) * (n.outside ? 0.6 : 1));
      const col = getNodeColor(n);
      svg += '<circle cx="' + n.x + '" cy="' + n.y + '" r="' + r + '" fill="' + col + '"/>';
      svg += '<text x="' + (n.x + r + 3) + '" y="' + n.y + '" fill="' + tokens.labelText + '" font-size="11" font-family="system-ui" dominant-baseline="middle">' + esc(n.name) + '</text>';
    });

    svg += '</g></svg>';
    const blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' });
    const a = document.createElement('a');
    a.download = 'commons-graph.svg';
    a.href = URL.createObjectURL(blob);
    a.click();
    showToast('Exported SVG');
  }

  function exportJSON() {
    const dataStr = 'data:text/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(G, null, 2));
    const a = document.createElement('a');
    a.download = 'commons-graph.json';
    a.href = dataStr;
    a.click();
    showToast('Exported JSON');
  }

  initSidebar();
  for (let i = 0; i < 45; i++) {
    stepPhysics();
  }
  fitCamera();
  renderLoop();
})();
</script>
</body>
</html>
"""


def overview(g: dict, max_nodes: int = 400, max_clusters: int = 60) -> dict:
    """Big graphs open as a cluster map: one node per community (top `max_clusters` by size, the rest folded into
    "other"), edges aggregated with counts. Open a cluster with community(g, id). Small graphs come back unchanged."""
    a = analyze(g)
    if len(a["nodes"]) <= max_nodes:
        return a
    comms = sorted(a["communities"], key=lambda c: -c["size"])
    keep = {c["id"] for c in comms[:max_clusters]}
    cid = {n: (d["community"] if d["community"] in keep else "other") for n, d in a["nodes"].items()}
    members = collections.defaultdict(list)
    for n, c in cid.items():
        members[c].append(n)
    nodes = {}
    for c in comms[:max_clusters]:
        top = sorted(members[c["id"]], key=lambda n: -a["nodes"][n]["rank"])[:2]
        names = " · ".join(str(a["nodes"][n].get("name"))[:28] for n in top)
        nodes[f"cluster:{c['id']}"] = {"type": "cluster", "name": f"{names} (+{c['size'] - len(top)})" if c["size"] > len(top)
                                       else names, "size": c["size"], "cluster": c["id"]}
    if members.get("other"):
        nodes["cluster:other"] = {"type": "cluster", "name": f"other small clusters ({len(members['other'])})",
                                  "size": len(members["other"]), "cluster": "other"}
    agg = collections.Counter()
    for e in a["edges"]:
        x, y = cid.get(e["src"]), cid.get(e["dst"])
        if x is not None and y is not None and x != y:
            agg[tuple(sorted((str(x), str(y))))] += 1
    edges = [{"src": f"cluster:{x}", "dst": f"cluster:{y}", "type": "links", "weight": n} for (x, y), n in agg.items()]
    return analyze({"nodes": nodes, "edges": edges, "scope": "overview", "total_nodes": len(a["nodes"]),
                    "root": g.get("root")})


def community(g: dict, cluster: str, limit: int = 600) -> dict:
    """One community from the cluster map, plus the nodes it links to directly (marked `outside`)."""
    a = analyze(g)
    if cluster == "other":
        big = {c["id"] for c in sorted(a["communities"], key=lambda c: -c["size"])[:60]}
        inside = {n for n, d in a["nodes"].items() if d["community"] not in big}
    else:
        inside = {n for n, d in a["nodes"].items() if str(d["community"]) == str(cluster)}
    near = {e["dst"] for e in a["edges"] if e["src"] in inside} | {e["src"] for e in a["edges"] if e["dst"] in inside}
    keep = list(inside) + sorted(near - inside, key=lambda n: -a["nodes"][n]["rank"])[: max(0, limit - len(inside))]
    ks = set(keep)
    nodes = {n: dict(a["nodes"][n], **({"outside": True} if n not in inside else {})) for n in keep}
    return analyze({"nodes": {n: {k: v for k, v in d.items() if k in ("type", "name", "outside")} for n, d in nodes.items()},
                    "edges": [e for e in a["edges"] if e["src"] in ks and e["dst"] in ks], "scope": f"cluster {cluster}"})


def render(g: dict, title: str = "Commons graph") -> str:
    analyzed = analyze(g) if "communities" not in g or not g.get("nodes") else g
    data = json.dumps(analyzed).replace("</", "<\\/")
    safe_title = html.escape(title)
    return VIEWER_TEMPLATE.replace("__TITLE__", safe_title).replace("__DATA__", data)


__all__ = ["analyze", "shortest_path", "overview", "community", "render"]
