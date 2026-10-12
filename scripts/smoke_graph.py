"""Smoke test for Commons Graph explorer using Playwright and Chromium."""
import os
import random
import tempfile
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

from commons_memory.graphview import analyze, render
from commons_memory.store import Store
from commons_memory.codeindex import index_repo


def generate_synthetic_graph(node_count=1500, edge_count=4000):
    types = ["file", "function", "class", "memory", "decision", "agent"]
    prefixes = {
        "file": "fil", "function": "fun", "class": "cls",
        "memory": "mem", "decision": "dec", "agent": "agt"
    }
    edge_types = ["calls", "defines", "imports", "relates_to", "decided_by", "modified_in"]

    random.seed(42)
    nodes = {}
    node_ids = []

    for i in range(node_count):
        t = types[i % len(types)]
        nid = f"{prefixes[t]}_{i:04d}"
        if i == 42:
            name = "user_auth_service"
        elif i == 100:
            name = "database_connection_pool"
        else:
            name = f"{t}_{i:04d}_module"
        nodes[nid] = {"type": t, "name": name}
        node_ids.append(nid)

    edges = []
    # Create spanning tree to ensure connectivity
    for i in range(1, node_count):
        parent = node_ids[random.randint(0, i - 1)]
        edges.append({"src": parent, "dst": node_ids[i], "type": random.choice(edge_types)})

    # Add random remaining edges
    remaining = edge_count - len(edges)
    for _ in range(remaining):
        u = random.choice(node_ids)
        v = random.choice(node_ids)
        if u != v:
            edges.append({"src": u, "dst": v, "type": random.choice(edge_types)})

    return {"nodes": nodes, "edges": edges, "root": "fil_0000"}


def build_real_graph(src_dir: Path | str) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tmpfile = tf.name

    try:
        s = Store(tmpfile, embedder="hash")
        index_repo(s, str(src_dir), "commons")
        g0 = s.export_graph("commons")
        entity_ids = list((g0.get("nodes") or {}).keys())

        # Add ~40 memories/decisions with related_ids
        for i in range(40):
            rel = []
            if entity_ids:
                rel = [entity_ids[i % len(entity_ids)], entity_ids[(i * 7) % len(entity_ids)]]
            s.add_item(
                "decision",
                f"Decision {i + 1}: Architectural choice for subsystem {i + 1}",
                project="commons",
                related_ids=rel,
            )
        return s.export_graph("commons")
    finally:
        if os.path.exists(tmpfile):
            try:
                os.unlink(tmpfile)
            except OSError:
                pass


def main():
    worktree_src = Path(__file__).resolve().parent.parent / "src"
    print(f"Building real graph from {worktree_src}...")
    t0 = time.time()
    g = build_real_graph(worktree_src)
    build_time = time.time() - t0
    print(f"Built real graph in {build_time:.2f}s: {len(g['nodes'])} nodes and {len(g['edges'])} edges.")

    html_content = render(g, title="Commons Code Graph")
    smoke_path = Path("/tmp/claude-graph-smoke.html")
    smoke_path.write_text(html_content, encoding="utf-8")
    print(f"Rendered viewer to {smoke_path} ({len(html_content)} bytes).")

    screenshot_dir = Path("docs/screenshots")
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    desktop_png = screenshot_dir / "graph-v2-desktop.png"
    mobile_png = screenshot_dir / "graph-v2-mobile.png"

    errors = []
    warnings = []

    with sync_playwright() as p:
        print("Launching Chromium via channel='chrome'...")
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        def handle_console(msg):
            if msg.type == "error":
                errors.append(msg.text)
            elif msg.type == "warning":
                warnings.append(msg.text)

        page.on("console", handle_console)
        page.on("pageerror", lambda err: errors.append(str(err)))

        print(f"Navigating to {smoke_path.as_uri()}...")
        page.emulate_media(color_scheme="dark")
        page.goto(smoke_path.as_uri())
        page.wait_for_timeout(1400)

        # 1. Desktop Screenshot (1440x900, dark theme)
        print("Capturing desktop screenshot (1440x900, dark theme)...")
        page.screenshot(path=str(desktop_png))
        assert desktop_png.exists() and desktop_png.stat().st_size > 0, "Desktop screenshot failed"
        print(f"Desktop screenshot saved to {desktop_png} ({desktop_png.stat().st_size} bytes)")

        # 2. Mobile Screenshot (390x844, light theme)
        print("Resizing to mobile (390x844, light theme) and capturing screenshot...")
        page.set_viewport_size({"width": 390, "height": 844})
        page.emulate_media(color_scheme="light")
        page.wait_for_timeout(700)
        page.screenshot(path=str(mobile_png))
        assert mobile_png.exists() and mobile_png.stat().st_size > 0, "Mobile screenshot failed"
        print(f"Mobile screenshot saved to {mobile_png} ({mobile_png.stat().st_size} bytes)")

        # 3. Mobile UI exercises (overflow menu and search expansion)
        print("Exercising mobile overflow menu and search...")
        btn_overflow = page.query_selector("#btn-overflow")
        assert btn_overflow and btn_overflow.is_visible(), "Expected overflow button to be visible on mobile"
        btn_overflow.click()
        page.wait_for_timeout(200)
        tools_open = page.eval_on_selector("#topbar-tools", "el => el.classList.contains('open')")
        assert tools_open, "Expected topbar-tools dropdown to open after clicking overflow"

        # Close overflow by clicking body
        page.mouse.click(10, 100)
        page.wait_for_timeout(200)
        tools_closed = page.eval_on_selector("#topbar-tools", "el => !el.classList.contains('open')")
        assert tools_closed, "Expected topbar-tools dropdown to close"

        btn_search_toggle = page.query_selector("#btn-search-toggle")
        assert btn_search_toggle and btn_search_toggle.is_visible(), "Expected search toggle button on mobile"
        btn_search_toggle.click()
        page.wait_for_timeout(200)
        search_expanded = page.eval_on_selector("#search-wrap", "el => el.classList.contains('expanded')")
        assert search_expanded, "Expected search-wrap to expand on mobile"

        btn_close_search = page.query_selector("#btn-close-search")
        btn_close_search.click()
        page.wait_for_timeout(200)
        search_collapsed = page.eval_on_selector("#search-wrap", "el => !el.classList.contains('expanded')")
        assert search_collapsed, "Expected search-wrap to collapse"

        # Restore desktop viewport and dark theme for keyboard exercises
        page.set_viewport_size({"width": 1440, "height": 900})
        page.emulate_media(color_scheme="dark")
        page.wait_for_timeout(400)

        # 4. Search exercise
        print("Exercising search box...")
        page.keyboard.press("/")
        page.wait_for_timeout(200)
        page.keyboard.type("graphview", delay=30)
        page.wait_for_timeout(400)
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)

        detail_open = page.eval_on_selector("#detail-panel", "el => el.classList.contains('open')")
        print(f"Detail panel open after search selection: {detail_open}")
        assert detail_open, "Expected detail panel to open after selecting search result"

        # Check neighbour groups and expander in detail panel
        nbr_groups = page.query_selector_all(".nbr-group")
        print(f"Neighbour groups found in detail panel: {len(nbr_groups)}")
        assert len(nbr_groups) > 0, "Expected at least one neighbour group"

        # 5. Focus exercise (f key)
        print("Exercising focus mode via keyboard 'f'...")
        btn_focus = page.query_selector("#btn-focus-node")
        text_before = btn_focus.inner_text()
        page.keyboard.press("f")
        page.wait_for_timeout(300)
        text_1hop = btn_focus.inner_text()
        print(f"Focus toggled from '{text_before}' to '{text_1hop}'")
        assert "1-Hop" in text_1hop, f"Expected 1-Hop focus, got {text_1hop}"

        page.keyboard.press("f")
        page.wait_for_timeout(300)
        text_2hop = btn_focus.inner_text()
        print(f"Focus toggled to '{text_2hop}'")
        assert "2-Hop" in text_2hop, f"Expected 2-Hop focus, got {text_2hop}"

        page.keyboard.press("f")
        page.wait_for_timeout(300)

        # 6. Path to... exercise
        print("Exercising path mode...")
        btn_path = page.query_selector("#btn-path-mode")
        btn_path.click()
        page.wait_for_timeout(200)
        page.keyboard.press("/")
        page.keyboard.type("Store.audit", delay=30)
        page.wait_for_timeout(400)
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        path_box_visible = page.eval_on_selector("#path-box", "el => el.style.display !== 'none'")
        print(f"Path results box visible: {path_box_visible}")
        assert path_box_visible, "Expected path results box to be visible"

        # 7. Layout cycling (l key)
        print("Exercising layout cycling via keyboard 'l'...")
        layout_val = page.eval_on_selector("#layout-select", "el => el.value")
        print(f"Initial layout: {layout_val}")

        page.keyboard.press("l")
        page.wait_for_timeout(400)
        layout_radial = page.eval_on_selector("#layout-select", "el => el.value")
        print(f"Layout after 1st press: {layout_radial}")
        assert layout_radial == "radial", f"Expected radial, got {layout_radial}"

        page.keyboard.press("l")
        page.wait_for_timeout(400)
        layout_layered = page.eval_on_selector("#layout-select", "el => el.value")
        print(f"Layout after 2nd press: {layout_layered}")
        assert layout_layered == "layered", f"Expected layered, got {layout_layered}"

        page.keyboard.press("l")
        page.wait_for_timeout(400)
        layout_force = page.eval_on_selector("#layout-select", "el => el.value")
        print(f"Layout after 3rd press: {layout_force}")
        assert layout_force == "force", f"Expected force, got {layout_force}"

        # 8. Color mode toggle (c key)
        print("Exercising color mode toggle via keyboard 'c'...")
        btn_color = page.query_selector("#btn-color")
        color_before = btn_color.inner_text()
        page.keyboard.press("c")
        page.wait_for_timeout(300)
        color_after = btn_color.inner_text()
        print(f"Color mode toggled from '{color_before}' to '{color_after}'")
        assert "Community" in color_after, f"Expected Community color mode, got {color_after}"

        # 9. Reset camera (r key)
        print("Exercising reset camera via keyboard 'r'...")
        page.keyboard.press("r")
        page.wait_for_timeout(300)

        # 10. Clear selection (Esc key)
        print("Exercising Esc key to clear...")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        detail_after_esc = page.eval_on_selector("#detail-panel", "el => el.classList.contains('open')")
        assert not detail_after_esc, "Detail panel should be closed after Escape"

        browser.close()

    print("\nConsole errors count:", len(errors))
    if errors:
        print("Errors encountered:", errors)
    assert len(errors) == 0, f"Encountered browser errors: {errors}"

    # 11. Synthetic graph performance check
    print("\nRunning synthetic graph performance check (1500 nodes, 4000 edges)...")
    t_synth_start = time.time()
    synth_g = generate_synthetic_graph(1500, 4000)
    synth_res = analyze(synth_g)
    synth_duration = time.time() - t_synth_start
    print(f"Synthetic analysis completed in {synth_duration:.3f}s (communities: {len(synth_res['communities'])}).")
    assert synth_duration < 2.0, f"Synthetic analysis took too long: {synth_duration:.3f}s"

    print("\nSmoke test completed successfully!")


if __name__ == "__main__":
    main()
