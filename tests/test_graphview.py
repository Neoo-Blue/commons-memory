import re
import time
from commons_memory.graphview import analyze, render, shortest_path


def make_two_cliques():
    nodes = {f"c1_{i}": {"type": "concept", "name": f"c1_{i}"} for i in range(6)}
    nodes.update({f"c2_{i}": {"type": "concept", "name": f"c2_{i}"} for i in range(6)})
    edges = []
    for i in range(6):
        for j in range(i + 1, 6):
            edges.append({"src": f"c1_{i}", "dst": f"c1_{j}", "type": "link"})
            edges.append({"src": f"c2_{i}", "dst": f"c2_{j}", "type": "link"})
    edges.append({"src": "c1_0", "dst": "c2_0", "type": "bridge"})
    return {"nodes": nodes, "edges": edges}


def test_analyze_two_cliques_communities():
    g = make_two_cliques()
    res = analyze(g)
    assert len(res["communities"]) == 2
    comms_a = {res["nodes"][f"c1_{i}"]["community"] for i in range(6)}
    comms_b = {res["nodes"][f"c2_{i}"]["community"] for i in range(6)}
    assert len(comms_a) == 1
    assert len(comms_b) == 1
    assert comms_a != comms_b
    assert {c["id"] for c in res["communities"]} == comms_a | comms_b


def test_pagerank_sums_to_one():
    g = make_two_cliques()
    res = analyze(g)
    total = sum(n["rank"] for n in res["nodes"].values())
    assert abs(total - 1.0) <= 0.01

    single = analyze({"nodes": {"n1": {"type": "file", "name": "single.py"}}, "edges": []})
    assert abs(sum(n["rank"] for n in single["nodes"].values()) - 1.0) <= 0.01


def test_shortest_path_on_chain():
    nodes = {x: {"type": "step", "name": x} for x in ["A", "B", "C", "D"]}
    edges = [
        {"src": "A", "dst": "B", "type": "next"},
        {"src": "B", "dst": "C", "type": "next"},
        {"src": "C", "dst": "D", "type": "next"},
    ]
    g = {"nodes": nodes, "edges": edges}
    assert shortest_path(g, "A", "D") == ["A", "B", "C", "D"]
    assert shortest_path(g, "D", "A") == ["D", "C", "B", "A"]
    assert shortest_path(g, "B", "C") == ["B", "C"]
    assert shortest_path(g, "A", "A") == ["A"]
    assert shortest_path(g, "A", "Z") == []


def test_render_no_external_urls_and_escapes():
    xss_name = "</script><script>alert('xss')</script>"
    g = {"nodes": {"x1": {"type": "file", "name": xss_name}}, "edges": []}
    html_out = render(g, title="Commons Test")

    # Embeds the node name with escaped script tag
    assert "<\\/script>" in html_out
    assert "</script><script>alert" not in html_out

    # No external http:// or https:// except SVG xmlns
    urls = re.findall(r"https?://[^\s\"'<>]+", html_out)
    assert urls == ["http://www.w3.org/2000/svg"]


def test_analyze_performance_5k_nodes():
    nodes = {f"n_{i}": {"type": "entity", "name": f"Node {i}"} for i in range(5000)}
    edges = []
    for i in range(4999):
        edges.append({"src": f"n_{i}", "dst": f"n_{i+1}", "type": "line"})
    for i in range(15001):
        edges.append({"src": f"n_{i % 5000}", "dst": f"n_{(i * 7) % 5000}", "type": "rand"})
    g = {"nodes": nodes, "edges": edges}

    t0 = time.time()
    res = analyze(g)
    duration = time.time() - t0

    assert duration < 2.0
    assert len(res["nodes"]) == 5000
    assert abs(sum(n["rank"] for n in res["nodes"].values()) - 1.0) <= 0.01


def test_empty_graph():
    g = {"nodes": {}, "edges": []}
    res = analyze(g)
    assert res["communities"] == []
    html_out = render(g)
    assert "No nodes in graph" in html_out


def test_overview_and_community_views():
    from commons_memory import graphview as gv
    nodes, edges = {}, []
    for c in range(6):  # 6 dense groups of 80 nodes, each with its own file node, sparsely linked to the next group
        nodes[f"f{c}"] = {"type": "file", "name": f"src/pkg/mod{c}.py"}
        for i in range(80):
            nodes[f"n{c}_{i}"] = {"type": "function", "name": f"mod{c}.fn{i}"}
            edges.append({"src": f"f{c}", "dst": f"n{c}_{i}", "type": "defines"})
            edges.append({"src": f"n{c}_{i}", "dst": f"n{c}_{(i + 1) % 80}", "type": "calls"})
        edges.append({"src": f"n{c}_0", "dst": f"n{(c + 1) % 6}_0", "type": "calls"})
    nodes["m_json"] = {"type": "module", "name": "json"}  # external import hub: excluded from the map
    edges += [{"src": f"f{c}", "dst": "m_json", "type": "imports"} for c in range(6)]
    ov = gv.overview({"nodes": nodes, "edges": edges}, max_nodes=100)
    assert ov["scope"] == "overview" and ov["total_nodes"] == len(nodes) - 1
    assert all(d["type"] == "cluster" for d in ov["nodes"].values()) and len(ov["nodes"]) >= 6
    assert any("mod" in d["name"] and ".py" in d["name"] for d in ov["nodes"].values())  # labelled by file
    some = next(iter(ov["nodes"].values()))["cluster"]
    sub = gv.community({"nodes": nodes, "edges": edges}, str(some))
    inside = [n for n, d in sub["nodes"].items() if not d.get("outside")]
    assert len(inside) >= 40 and "m_json" not in sub["nodes"]
    small = gv.overview({"nodes": dict(list(nodes.items())[:30]), "edges": []}, max_nodes=100)
    assert "scope" not in small or small.get("scope") != "overview"  # small graphs are shown as-is
    assert "commons:open" in gv.render(ov) and "community" in gv.render(ov)


def test_source_badges_and_architecture_delta(tmp_path):
    import subprocess
    from commons_memory.codeindex import index_repo
    from commons_memory.diagrams import delta_graph, make
    from commons_memory.store import Store
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "a.py").write_text("import b\n\ndef run():\n    return b.helper()\n")
    (repo / "b.py").write_text("def helper():\n    return 1\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=False)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], check=False)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "1"], check=False)
    s = Store(str(tmp_path / "d.db"), embedder="hash")
    index_repo(s, str(repo), "app")
    g = s.export_graph("app", include_items=False)
    fn = next(d for d in g["nodes"].values() if d["type"] == "function" and d["name"].endswith("run"))
    assert fn["path"] == "a.py" and fn["line"] == 3
    (repo / "c.py").write_text("import a\n\ndef main():\n    return a.run()\n")
    index_repo(s, str(repo), "app")
    d = delta_graph(s, "app")
    assert d["summary"]["files_added"] == 1 and "c.py" in d["nodes"] and d["nodes"]["c.py"]["type"] == "added file"
    assert any(e["type"] == "added dependency" for e in d["edges"])
    assert "Architecture delta" in make(s, "delta", "app")
