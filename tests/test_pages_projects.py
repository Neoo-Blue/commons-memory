"""Tests for the Projects workspace pages (projects.js, projects.css)."""
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


PAGES_OWNED = [
    "Projects",
    "Tasks",
]


def test_projects_js_and_css_exist():
    web_dir = Path(dashboard.__file__).with_name("web")
    proj_js = web_dir / "pages" / "projects.js"
    proj_css = web_dir / "pages" / "projects.css"
    assert proj_js.exists(), "projects.js must exist"
    assert proj_css.exists(), "projects.css must exist"


def test_no_em_dash_in_files():
    web_dir = Path(dashboard.__file__).with_name("web")
    proj_js = web_dir / "pages" / "projects.js"
    proj_css = web_dir / "pages" / "projects.css"
    for f in (proj_js, proj_css):
        content = f.read_text(encoding="utf-8")
        assert "\u2014" not in content, f"Found em dash (\\u2014) in {f.name}"
        assert "&mdash;" not in content, f"Found &mdash; in {f.name}"
        assert "&#8212;" not in content, f"Found &#8212; in {f.name}"


def test_projects_js_reassigns_all_owned_pages():
    web_dir = Path(dashboard.__file__).with_name("web")
    content = (web_dir / "pages" / "projects.js").read_text(encoding="utf-8")
    for page in PAGES_OWNED:
        pattern = rf'V\["{page}"\]\s*='
        assert re.search(pattern, content), f"projects.js must reassign V[\"{page}\"]"


def test_projects_in_t_and_shell():
    web_dir = Path(dashboard.__file__).with_name("web")
    legacy_content = (web_dir / "legacy.js").read_text(encoding="utf-8")
    shell_content = (web_dir / "shell.js").read_text(encoding="utf-8")

    assert '"Projects"' in legacy_content, "Projects must be in T in legacy.js"
    assert '"Tasks"' in legacy_content, "Tasks must be in T in legacy.js"
    assert '["Projects"' in shell_content, "Projects workspace must be in shell.js"


def test_node_check_assembled_page_script():
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is not installed")
    # Freshly assemble page with projects.js included
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
    content = (web_dir / "pages" / "projects.js").read_text(encoding="utf-8")
    calls = re.findall(r'api\(["`]([^"`]+)["`]\)', content)
    assert calls, "Should find api(...) calls in projects.js"

    api_methods = {m for m in dir(dashboard.API) if not m.startswith("_")}
    from commons_memory import dashboard_v7
    v7_methods = {"mission", "jobs_index", "breakdown_view", "changes_view", "routing_view", "presence_view", "drive_ls", "drive_file"}
    valid_endpoints = api_methods | v7_methods

    for call in calls:
        endpoint = call.split("?")[0].strip("/")
        assert endpoint in valid_endpoints, f"Endpoint api('{endpoint}') from call '{call}' not found in dashboard.API"
