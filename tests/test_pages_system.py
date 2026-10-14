"""Tests for System workspace pages: Setup, Secrets, Recurring, and System."""
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from commons_memory import dashboard


def test_node_check_assembled_script():
    """Verify assembled script from dashboard.PAGE passes node --check with zero syntax errors."""
    page = dashboard.PAGE
    match = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    assert match is not None, "Script tag missing in assembled dashboard page"
    script = match.group(1)
    assert len(script) > 1000

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
        f_path = f.name

    try:
        res = subprocess.run(["node", "--check", f_path], capture_output=True, text=True)
        assert res.returncode == 0, f"node --check failed:\n{res.stderr}"
    finally:
        Path(f_path).unlink(missing_ok=True)


def test_pages_reassigned():
    """Assert system.js reassigns all four pages it owns: Setup, Secrets, Recurring, System."""
    system_js = (dashboard.WEB / "pages" / "system.js").read_text()
    for page_name in ["Setup", "Secrets", "Recurring", "System"]:
        assert f'V["{page_name}"]' in system_js or f"V['{page_name}']" in system_js, (
            f"Page {page_name} is not reassigned in system.js"
        )


def test_no_em_dashes():
    """Assert no em dashes are present anywhere in system.js or system.css."""
    system_js = (dashboard.WEB / "pages" / "system.js").read_text()
    system_css = (dashboard.WEB / "pages" / "system.css").read_text()
    assert "—" not in system_js, "Em dash found in system.js"
    assert "\u2014" not in system_js, "Unicode em dash found in system.js"
    assert "—" not in system_css, "Em dash found in system.css"
    assert "\u2014" not in system_css, "Unicode em dash found in system.css"


def test_api_calls_exist_on_dashboard_api():
    """Assert every api('...') call in system.js exists as a method on dashboard.API or a route in dashboard.py."""
    system_js = (dashboard.WEB / "pages" / "system.js").read_text()
    calls = re.findall(r'api\(["\']([^"\'\?]+)', system_js)
    assert len(calls) > 0, "No api() calls found in system.js"

    api_class = dashboard.API
    dashboard_py = Path(dashboard.__file__).read_text()

    for endpoint in set(calls):
        endpoint_clean = endpoint.strip()
        method_exists = hasattr(api_class, endpoint_clean)
        route_exists = f'"{endpoint_clean}"' in dashboard_py or f"'{endpoint_clean}'" in dashboard_py or f"/api/{endpoint_clean}" in dashboard_py
        assert method_exists or route_exists, f"API endpoint '{endpoint_clean}' not found on dashboard.API or dashboard.py"
