"""The Knowledge workspace script stays valid and only calls real dashboard endpoints."""
import inspect
import re
import subprocess
from pathlib import Path

from commons_memory.dashboard import API, PAGE

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "src/commons_memory/web/pages/knowledge.js").read_text()
DASH = (ROOT / "src/commons_memory/dashboard.py").read_text()
PAGES = [
    "Memory", "Knowledge", "Context", "Graph", "Preferences",
    "Org chart", "Librarian", "Import", "Retro learning",
]


def test_pages_reassigned():
    for name in PAGES:
        assert re.search(r'V\["' + re.escape(name) + r'"\] = async function', JS), name


def test_no_em_or_en_dash():
    assert "\u2014" not in JS
    assert "\u2013" not in JS


def test_no_browser_prompts():
    assert "prompt(" not in JS
    assert "confirm(" not in JS


def test_memory_drawer_and_undo():
    assert 'K.drawer.on("mem:"' in JS
    assert "K.optimistic" in JS
    assert "K.menu" in JS


def test_api_calls_exist():
    methods = {name for name, _ in inspect.getmembers(API, predicate=inspect.isfunction)}
    calls = set(re.findall(r'api\(\s*"([^"?\\]+)', JS))
    calls |= {m.split("?")[0] for m in re.findall(r"api\(\s*`([^`]+)`", JS)}
    assert calls, "expected api() calls"
    missing = sorted(name for name in calls if name not in methods and f"/api/{name}" not in DASH)
    assert not missing, missing


def test_assembled_script_parses():
    match = re.search(r"<script>(.*)</script>\s*</body>", PAGE, re.S)
    assert match, "assembled page has no script"
    script = match.group(1)
    path = Path("/tmp/commons-knowledge-page.js")
    path.write_text(script)
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
