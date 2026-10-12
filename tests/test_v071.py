"""v0.7.1: identity args are tolerated on every tool; env reference stays in sync with the code."""
import subprocess
import sys
from pathlib import Path

import pytest

from commons_memory.toolargs import normalize

ROOT = Path(__file__).resolve().parents[1]


def test_identity_args_ignored_on_tools_without_them():
    params = {"job_id": {"type": "string"}}
    assert normalize("job_status", params, [], {"job_id": "j", "api_key": "k", "agent": "a"}) == {"job_id": "j"}
    from commons_memory.store import CommonsError
    with pytest.raises(CommonsError, match="unknown argument"):
        normalize("job_status", params, [], {"bogus": 1})


def test_env_reference_is_current():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_env_reference.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_installers_and_prompt_present():
    sh = (ROOT / "install.sh").read_text()
    assert sh.startswith("#!/bin/sh") and "commons init" in sh and "uv tool install" in sh and "pipx install" in sh
    assert "irm" in (ROOT / "install.ps1").read_text()
    p = (ROOT / "PROMPT.md").read_text()
    for tool in ("pickup", "claim_subtask", "record_decision", "handoff", "report_usage"):
        assert tool in p
    assert subprocess.run(["sh", "-n", str(ROOT / "install.sh")]).returncode == 0
