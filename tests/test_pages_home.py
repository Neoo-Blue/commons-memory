"""The Home workspace page script (web/pages/home.js): it parses inside the assembled dashboard page, owns Overview
and Board, keeps the copy rules, and only calls endpoints the hub serves."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from commons_memory import dashboard

WEB = Path(dashboard.__file__).with_name("web")
JS = (WEB / "pages" / "home.js").read_text()
CSS = (WEB / "pages" / "home.css").read_text()
SRC = Path(dashboard.__file__).read_text()


def test_home_files_are_in_the_page():
    assert "pages/home.js" in dashboard.JS_FILES and "pages/home.css" in dashboard.CSS_FILES
    assert dashboard.JS_FILES.index("kit.js") < dashboard.JS_FILES.index("pages/home.js")


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_assembled_script_parses(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", dashboard.PAGE, re.S)
    assert scripts and 'V["Overview"]' in scripts[-1]
    f = tmp_path / "page.js"
    f.write_text(scripts[-1])
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_owns_overview_and_board():
    for page in ("Overview", "Board"):
        assert re.search(r'V\["%s"\]\s*=\s*async function' % page, JS), page
    for prefix in ("agent:", "job:", "board:"):
        assert f'K.drawer.on("{prefix}"' in JS


def test_copy_rules():
    for name, text in (("home.js", JS), ("home.css", CSS)):
        assert "\u2014" not in text, f"em dash in {name}"


def test_every_endpoint_exists():
    gets = set(re.findall(r'api\(\s*["\'`]([a-z_]+)', JS))
    assert {"overview", "board", "job_detail", "agent_detail"} <= gets
    for name in gets:
        assert callable(getattr(dashboard.API, name, None)), f"GET /api/{name} is not served"
    posts = set(re.findall(r'(?:postRaw|ownerPost)\(\s*["\'`]([a-z_/]+)["\'`]', JS))
    assert {"board/answer", "board/update", "board/add", "subtask/retry", "subtask/reassign", "job/retry"} <= posts
    for path in posts:
        assert f'"/api/{path}"' in SRC, f"POST /api/{path} is not served"
