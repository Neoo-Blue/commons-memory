"""The Agents workspace page module (web/pages/agents.js): it parses inside the assembled page script, reassigns every
page it owns, calls only endpoints the dashboard serves, and keeps em dashes out of the copy."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from commons_memory import dashboard

SRC = Path(dashboard.__file__).parent
JS = (SRC / "web" / "pages" / "agents.js").read_text()
CSS = (SRC / "web" / "pages" / "agents.css").read_text()
PAGES = ["Agents", "Presence", "Capacity", "Usage", "Models", "Connect", "Skills", "Discover", "MCP hub"]


def test_assembled_page_script_parses(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    assert "pages/agents.js" in dashboard.JS_FILES and "pages/agents.css" in dashboard.CSS_FILES
    script = re.search(r"<script>(.*)</script>", dashboard.PAGE, re.S).group(1)
    assert "V.Agents = async function" in script
    f = tmp_path / "page.js"
    f.write_text(script)
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_reassigns_every_owned_page():
    for p in PAGES:
        assert re.search(r'V(\.%s|\["%s"\]) = async function' % (re.escape(p), re.escape(p)), JS), p
    for prefix in ("agent:", "skill:", "repo:", "mcp:", "mcpadd:"):  # deep-linkable drawers
        assert f'K.drawer.on("{prefix}"' in JS, prefix


def test_no_em_dash():
    assert "—" not in JS and "—" not in CSS


def test_every_endpoint_exists():
    gets = set(re.findall(r'\bapi\(\s*[`"\']([a-z_]+)', JS))
    assert {"agents", "agent_detail", "presence_view", "capacity", "usage", "usage_series", "models_page", "connect_status",
            "skills", "skill_history", "discover", "mcp"} <= gets
    for name in gets:
        assert hasattr(dashboard.API, name), f"GET /api/{name} has no API method"
    routes = "".join((SRC / f).read_text() for f in ("dashboard.py", "dashboard_v7.py", "dashboard_v81.py"))
    posts = set(re.findall(r'\b(?:postRaw|act)\(\s*"([a-z_]+/[a-z_]+)"', JS))
    assert {"agent/update", "msg/send", "setup/provider_test", "setup/setting", "mcp/approve", "skill/review"} <= posts
    for path in posts:
        assert f'"/api/{path}"' in routes, f"POST /api/{path} has no route"


def test_data_is_escaped_in_hot_spots():
    # names and free text reach the markup only through E(); spot-check the riskiest templates
    for snippet in ("${E(a.name)}", "${E(m.from || \"Commons\")}", "${E(String(txt).slice(0, 600))}", "${E(x.description || \"\")}"):
        assert snippet in JS, snippet
