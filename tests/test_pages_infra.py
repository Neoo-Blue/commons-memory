"""Tests for the Infrastructure workspace page (infra.js, infra.css)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import pytest

src_dir = str(Path(__file__).resolve().parent.parent / "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

import commons_memory
cm_dir = os.path.join(src_dir, "commons_memory")
if cm_dir not in commons_memory.__path__:
    commons_memory.__path__.insert(0, cm_dir)

from commons_memory import dashboard


def test_infra_js_and_css_exist():
    web_dir = Path(dashboard.__file__).with_name("web")
    infra_js = web_dir / "pages" / "infra.js"
    infra_css = web_dir / "pages" / "infra.css"
    assert infra_js.exists(), "infra.js must exist"
    assert infra_css.exists(), "infra.css must exist"


def test_no_em_dash_in_files():
    web_dir = Path(dashboard.__file__).with_name("web")
    infra_js = web_dir / "pages" / "infra.js"
    infra_css = web_dir / "pages" / "infra.css"
    projects_js = web_dir / "pages" / "projects.js"
    for f in (infra_js, infra_css, projects_js):
        content = f.read_text(encoding="utf-8")
        assert "\u2014" not in content, f"Found em dash (\\u2014) in {f.name}"
        assert "&mdash;" not in content, f"Found &mdash; in {f.name}"
        assert "&#8212;" not in content, f"Found &#8212; in {f.name}"


def test_no_E_inside_inline_handler_string():
    web_dir = Path(dashboard.__file__).with_name("web")
    infra_js = web_dir / "pages" / "infra.js"
    projects_js = web_dir / "pages" / "projects.js"
    for f in (infra_js, projects_js):
        content = f.read_text(encoding="utf-8")
        # Ensure pattern like onclick="...${E(...)}..." is not present
        bad = re.findall(r'on[a-z]+=["\'][^"\']*?\$\{E\(.*?\)[\'"]', content, re.IGNORECASE)
        assert not bad, f"Found E( inside inline handler string in {f.name}: {bad}"


def test_infra_js_reassigns_infrastructure_page():
    web_dir = Path(dashboard.__file__).with_name("web")
    content = (web_dir / "pages" / "infra.js").read_text(encoding="utf-8")
    assert re.search(r'V\["Infrastructure"\]\s*=', content), 'infra.js must reassign V["Infrastructure"]'


def test_infrastructure_in_t_and_shell():
    web_dir = Path(dashboard.__file__).with_name("web")
    legacy_content = (web_dir / "legacy.js").read_text(encoding="utf-8")
    shell_content = (web_dir / "shell.js").read_text(encoding="utf-8")

    assert '"Infrastructure"' in legacy_content, "Infrastructure must be in T in legacy.js"
    assert '"Infrastructure"' in shell_content, "Infrastructure must be in Knowledge workspace in shell.js"


def test_node_check_assembled_page_script():
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is not installed")
    # Freshly assemble page with infra.js included
    page_html = dashboard._page()
    m = re.search(r"<script>(.*?)</script>", page_html, re.DOTALL)
    assert m, "dashboard page must contain <script>...</script>"
    script_content = m.group(1)

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script_content)
        fname = f.name
    try:
        proc = subprocess.run([node_bin, "--check", fname], capture_output=True, text=True)
        assert proc.returncode == 0, f"node --check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    finally:
        if os.path.exists(fname):
            os.unlink(fname)


def test_api_calls_exist_on_dashboard_api():
    web_dir = Path(dashboard.__file__).with_name("web")
    content = (web_dir / "pages" / "infra.js").read_text(encoding="utf-8")
    calls = re.findall(r'api\(["`]([^"`]+)["`]\)', content)
    assert calls, "Should find api(...) calls in infra.js"

    api_methods = {m for m in dir(dashboard.API) if not m.startswith("_")}
    from commons_memory import dashboard_v7
    v7_methods = {"mission", "jobs_index", "breakdown_view", "changes_view", "routing_view", "presence_view", "drive_ls", "drive_file"}
    valid_endpoints = api_methods | v7_methods

    for call in calls:
        endpoint = call.split("?")[0].strip("/")
        assert endpoint in valid_endpoints, f"Endpoint api('{endpoint}') from call '{call}' not found in dashboard.API"


def test_host_level_services_on_the_host():
    """Verify 'On the host' section exists in infra.js and infra.css."""
    web_dir = Path(dashboard.__file__).with_name("web")
    js = (web_dir / "pages" / "infra.js").read_text(encoding="utf-8")
    css = (web_dir / "pages" / "infra.css").read_text(encoding="utf-8")

    assert "On the host" in js, "Must contain 'On the host' section title in infra.js"
    assert "infra-host-services-section" in js, "Must contain host services container in infra.js"
    assert "infra-host-chip" in js, "Must contain host chip in infra.js"
    assert ".infra-host-services-section" in css, "Must define host services section in infra.css"
    assert ".infra-host-chip" in css, "Must define host chip in infra.css"


def test_guest_card_grid_and_collapsible():
    """Verify guest card grid sizing and collapsible services toggle."""
    web_dir = Path(dashboard.__file__).with_name("web")
    js = (web_dir / "pages" / "infra.js").read_text(encoding="utf-8")
    css = (web_dir / "pages" / "infra.css").read_text(encoding="utf-8")

    assert "align-items: start" in css, "Grid cards must size to content using align-items: start"
    assert "infra-services-more" in js, "Must contain collapsed services container"
    assert "toggleMore" in js, "Must contain toggleMore function"
    assert ".infra-more-btn" in css, "Must define .infra-more-btn in infra.css"


def test_vmid_repeats_removed_and_specs_muted():
    """Verify VMID badge is removed and guest specs (IP, cores, RAM) are in small muted text."""
    web_dir = Path(dashboard.__file__).with_name("web")
    js = (web_dir / "pages" / "infra.js").read_text(encoding="utf-8")

    # Should not repeat VMID badge in guest card
    assert "VMID ${E(gAttrs.vmid)}" not in js, "Must not repeat VMID badge in guest card"
    assert "infra-guest-specs" in js, "Must format guest specs in small muted text"
    assert "fmtGuestSpecs" in js, "Must define fmtGuestSpecs helper"


def test_status_dots_logic():
    """Verify status dot is green only when running, grey for stopped, amber for stale."""
    web_dir = Path(dashboard.__file__).with_name("web")
    js = (web_dir / "pages" / "infra.js").read_text(encoding="utf-8")
    css = (web_dir / "pages" / "infra.css").read_text(encoding="utf-8")

    assert "getStatusDotClass" in js, "Must define getStatusDotClass helper"
    assert ".infra-guest-card.stopped" in css, "Must define stopped guest quieter styling in infra.css"


def test_freshness_humanization_and_memory_facts():
    """Verify freshness humanizer logic and facts from memory."""
    web_dir = Path(dashboard.__file__).with_name("web")
    js = (web_dir / "pages" / "infra.js").read_text(encoding="utf-8")

    assert "humanizeSource" in js, "Must define humanizeSource helper"
    assert "countMemoryFacts" in js, "Must define countMemoryFacts helper"
    assert "facts from memory" in js, "Must show facts from memory"


def test_zoomed_layout_no_scroll_and_split_mounts():
    """Verify zoomed view layout minmax(0,...) and Mounts vs Container binds split."""
    web_dir = Path(dashboard.__file__).with_name("web")
    js = (web_dir / "pages" / "infra.js").read_text(encoding="utf-8")
    css = (web_dir / "pages" / "infra.css").read_text(encoding="utf-8")

    assert "Container binds" in js, "Must contain 'Container binds' section"
    assert "Mounts" in js, "Must contain 'Mounts' section"
    assert "minmax(0, 1.2fr)" in css, "Must use minmax(0, ...) to prevent grid blowout"
    assert "truncateMiddle" in js, "Must define truncateMiddle helper"
    assert "infra-port-chip" in js, "Must render port chips"
    assert ".infra-port-chip" in css, "Must define port chip styling"


def test_node_eval_js_helpers():
    """Run node to test helper functions in isolation."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is not installed")

    web_dir = Path(dashboard.__file__).with_name("web")
    js = (web_dir / "pages" / "infra.js").read_text(encoding="utf-8")

    eval_script = f"""
{js}

// Test humanizeSource
console.log(JSON.stringify({{
  pve: humanizeSource("discovery:proxmox:pve"),
  tailscale: humanizeSource("discovery:tailscale:local"),
  cloudflared: humanizeSource("discovery:cloudflared:local"),
  docker: humanizeSource("discovery:docker:myhost"),
  trunc1: truncateMiddle("ghcr.io/sanket9225/easynews_as_indexer@sha256:3db52609c9f125d60eb76f2e862336a22aa2f1ea84b7078e266536efbe1a6998", 32),
  specs1: fmtGuestSpecs({{ ip: "192.168.50.205", cores: 20, memory_mb: 10240 }}),
  specs2: fmtGuestSpecs({{ cores: 2, memory_mb: 2048 }}),
  dot_running: getStatusDotClass({{ attrs: {{ status: "running" }} }}),
  dot_stopped: getStatusDotClass({{ attrs: {{ status: "stopped" }} }}),
  dot_stale: getStatusDotClass({{ stale: true, attrs: {{ status: "running" }} }}),
}}));
"""
    # Extract functions from the IIFE or redefine for the test if private
    # Let's extract the helper function definitions from infra.js
    helpers_src = re.findall(
        r"(function\s+(?:humanizeSource|truncateMiddle|fmtGuestSpecs|getStatusDotClass)\s*\(.*?\n  \})",
        js,
        re.DOTALL,
    )
    test_code = "\n".join(helpers_src) + f"""
console.log(JSON.stringify({{
  pve: humanizeSource("discovery:proxmox:pve"),
  tailscale: humanizeSource("discovery:tailscale:local"),
  cloudflared: humanizeSource("discovery:cloudflared:local"),
  docker: humanizeSource("discovery:docker:myhost"),
  trunc1: truncateMiddle("ghcr.io/sanket9225/easynews_as_indexer@sha256:3db52609c9f125d60eb76f2e862336a22aa2f1ea84b7078e266536efbe1a6998", 32),
  specs1: fmtGuestSpecs({{ ip: "192.168.50.205", cores: 20, memory_mb: 10240 }}),
  specs2: fmtGuestSpecs({{ cores: 2, memory_mb: 2048 }}),
  dot_running: getStatusDotClass({{ attrs: {{ status: "running" }} }}),
  dot_stopped: getStatusDotClass({{ attrs: {{ status: "stopped" }} }}),
  dot_stale: getStatusDotClass({{ stale: true, attrs: {{ status: "running" }} }}),
}}));
"""
    proc = subprocess.run([node_bin, "-e", test_code], capture_output=True, text=True)
    assert proc.returncode == 0, f"Node eval failed: {proc.stderr}"
    res = json.loads(proc.stdout)
    assert res["pve"] == "pve"
    assert res["tailscale"] == "tailscale"
    assert res["cloudflared"] == "cloudflared"
    assert res["docker"] == "myhost"
    assert "..." in res["trunc1"]
    assert len(res["trunc1"]) <= 33
    assert res["specs1"] == "192.168.50.205 · 20 cores · 10GB"
    assert res["specs2"] == "2 cores · 2GB"
    assert res["dot_running"] == "live"
    assert res["dot_stopped"] == "offline"
    assert res["dot_stale"] == "busy"
