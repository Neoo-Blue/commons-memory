"""Tests for the Infrastructure workspace page (infra.js, infra.css)."""
from __future__ import annotations

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
