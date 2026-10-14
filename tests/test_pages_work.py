"""Tests for the Work workspace pages (work.js, work.css)."""
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
    "Workflow",
    "Orchestration",
    "Task breakdown",
    "Mission control",
    "Changes",
    "Sessions",
    "Drive",
    "Routing",
]


def test_work_js_and_css_exist():
    web_dir = Path(dashboard.__file__).with_name("web")
    work_js = web_dir / "pages" / "work.js"
    work_css = web_dir / "pages" / "work.css"
    assert work_js.exists(), "work.js must exist"
    assert work_css.exists(), "work.css must exist"


def test_no_em_dash_in_files():
    web_dir = Path(dashboard.__file__).with_name("web")
    work_js = web_dir / "pages" / "work.js"
    work_css = web_dir / "pages" / "work.css"
    for f in (work_js, work_css):
        content = f.read_text(encoding="utf-8")
        assert "\u2014" not in content, f"Found em dash (\\u2014) in {f.name}"
        assert "&mdash;" not in content, f"Found &mdash; in {f.name}"
        assert "&#8212;" not in content, f"Found &#8212; in {f.name}"


def test_work_js_reassigns_all_owned_pages():
    web_dir = Path(dashboard.__file__).with_name("web")
    content = (web_dir / "pages" / "work.js").read_text(encoding="utf-8")
    for page in PAGES_OWNED:
        pattern = rf'V\["{page}"\]\s*='
        assert re.search(pattern, content), f"work.js must reassign V[\"{page}\"]"


def test_node_check_assembled_page_script():
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is not installed")
    page_html = dashboard.PAGE
    m = re.search(r"<script>(.*?)</script>", page_html, re.DOTALL)
    assert m, "dashboard.PAGE must contain <script>...</script>"
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
    content = (web_dir / "pages" / "work.js").read_text(encoding="utf-8")
    # Matches api("endpoint...") or api(`endpoint...`)
    calls = re.findall(r'api\(["`]([^"`]+)["`]\)', content)
    assert calls, "Should find api(...) calls in work.js"

    # Known method endpoints on API or routes
    api_methods = {m for m in dir(dashboard.API) if not m.startswith("_")}
    from commons_memory import dashboard_v7, dashboard_v81
    # also v7 methods added by v7.install
    v7_methods = {"mission", "jobs_index", "breakdown_view", "changes_view", "routing_view", "presence_view", "drive_ls", "drive_file"}
    valid_endpoints = api_methods | v7_methods

    for call in calls:
        # Strip query string and expressions
        endpoint = call.split("?")[0].strip("/")
        # If call uses template interpolation like drive_ls?project=${...}
        endpoint = endpoint.split("$")[0]
        assert endpoint in valid_endpoints or ("/" + endpoint in valid_endpoints), (
            f"Endpoint '{endpoint}' from call '{call}' must exist on dashboard.API"
        )
