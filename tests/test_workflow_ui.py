"""Tests for the Workflow dashboard page (dashboard_v25.py)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import pytest

# Ensure local src takes precedence even if commons_memory is installed elsewhere
src_dir = str(Path(__file__).resolve().parent.parent / "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)
import commons_memory
cm_dir = os.path.join(src_dir, "commons_memory")
if cm_dir not in commons_memory.__path__:
    commons_memory.__path__.insert(0, cm_dir)

from commons_memory import dashboard_v25 as d25


def test_icon_is_non_empty_string():
    assert isinstance(d25.ICON, str)
    assert len(d25.ICON.strip()) > 0
    assert "<path" in d25.ICON or "<rect" in d25.ICON or "<circle" in d25.ICON
    # No em dashes in ICON
    assert "\u2014" not in d25.ICON
    assert "&mdash;" not in d25.ICON
    assert "&#8212;" not in d25.ICON


def test_js_no_em_dash():
    # Strict rule: no em dashes in any visible text / code
    assert "\u2014" not in d25.JS
    assert "&mdash;" not in d25.JS
    assert "&#8212;" not in d25.JS


def test_js_api_references():
    # Assert JS references both api("workflow and api("workflows")
    assert 'api("workflow' in d25.JS
    assert 'api("workflows")' in d25.JS or 'api("workflows' in d25.JS


def test_js_passes_node_check():
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is not installed")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(d25.JS)
        fname = f.name
    try:
        proc = subprocess.run([node_bin, "--check", fname], capture_output=True, text=True)
        assert proc.returncode == 0, f"node --check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    finally:
        if os.path.exists(fname):
            os.unlink(fname)


def test_tab_registration():
    # Adds "Workflow" to T and to the Work group in GROUPS if not present
    assert 'T.includes("Workflow")' in d25.JS or 'T.push("Workflow")' in d25.JS
    assert "GROUPS" in d25.JS
    assert '"Work"' in d25.JS


def test_v_workflow_defined():
    assert 'V["Workflow"]' in d25.JS or "V['Workflow']" in d25.JS


def test_empty_state_and_commands():
    # Must include short explanation and submit_task(task, pattern="sdlc") or /sdlc
    assert 'submit_task(task, pattern="sdlc")' in d25.JS
    assert "/sdlc" in d25.JS


def test_waiting_owner_and_board_link():
    # A waiting_owner gate links to the Board page with go('Board')
    assert "go('Board')" in d25.JS or 'go("Board")' in d25.JS
    assert "waiting_owner" in d25.JS


def test_drive_raw_artifact_link():
    # Clicking an artifact path opens /drive/raw?path=<path> in a new tab
    assert "/drive/raw?path=" in d25.JS


def test_job_picker_persistence():
    # Remember the selection in window.wf_job
    assert "window.wf_job" in d25.JS
