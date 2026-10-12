"""Export the graph to Mermaid, GraphML, JSON and a self-contained static HTML viewer."""
from __future__ import annotations

import html
import json
import re
import os
from xml.sax.saxutils import escape


def to_mermaid(g: dict, max_nodes: int = 300) -> str:
    ids = list(g["nodes"])[:max_nodes]
    alias = {n: f"n{i}" for i, n in enumerate(ids)}
    lines = ["graph LR"]
    for n in ids:
        d = g["nodes"][n]
        label = f"{d['type']}: {d['name']}".replace('"', "'")[:60]
        lines.append(f'  {alias[n]}["{label}"]')
    for e in g["edges"]:
        if e["src"] in alias and e["dst"] in alias:
            lines.append(f"  {alias[e['src']]} -->|{e['type']}| {alias[e['dst']]}")
    return "\n".join(lines) + "\n"


def to_graphml(g: dict) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
           '<key id="type" for="node" attr.name="type" attr.type="string"/>',
           '<key id="name" for="node" attr.name="name" attr.type="string"/>',
           '<key id="rel" for="edge" attr.name="type" attr.type="string"/>',
           '<graph id="commons" edgedefault="directed">']
    for n, d in g["nodes"].items():
        out.append(f'<node id="{escape(n)}"><data key="type">{escape(d["type"])}</data>'
                   f'<data key="name">{escape(str(d["name"]))}</data></node>')
    for i, e in enumerate(g["edges"]):
        out.append(f'<edge id="e{i}" source="{escape(e["src"])}" target="{escape(e["dst"])}">'
                   f'<data key="rel">{escape(e["type"])}</data></edge>')
    out += ["</graph>", "</graphml>"]
    return "\n".join(out) + "\n"


from . import graphview

HTML = graphview.VIEWER_TEMPLATE


def to_html(g: dict) -> str:
    return graphview.render(g)


_REF = re.compile(r"^(env|vault|vault-dyn|op|infisical)://")


def mask_secret_refs(g: dict) -> dict:
    """Replace secret-reference names (which can themselves be sensitive) with stable hashes for sharing.
    Set COMMONS_EXPORT_SECRET_REFS=1 to keep them."""
    import hashlib
    if os.environ.get("COMMONS_EXPORT_SECRET_REFS") == "1":
        return g
    m = {k: "secret_ref:" + hashlib.sha256(k.encode()).hexdigest()[:10] for k in g.get("nodes", {}) if _REF.match(k)}
    if not m:
        return g
    nodes = {m.get(k, k): (dict(v, name=m[k]) if k in m else v) for k, v in g["nodes"].items()}
    edges = [dict(e, src=m.get(e["src"], e["src"]), dst=m.get(e["dst"], e["dst"])) for e in g.get("edges", [])]
    return {**g, "nodes": nodes, "edges": edges}


def export(g: dict, fmt: str) -> str:
    g = mask_secret_refs(g)
    fmt = fmt.lower()
    if fmt == "mermaid":
        return to_mermaid(g)
    if fmt == "graphml":
        return to_graphml(g)
    if fmt == "html":
        return to_html(g)
    if fmt == "json":
        return json.dumps(g, indent=1)
    raise ValueError("format must be mermaid|graphml|html|json")


__all__ = ["export", "to_mermaid", "to_graphml", "to_html", "html"]
