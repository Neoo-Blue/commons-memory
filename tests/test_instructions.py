from commons_memory import instructions as I


def test_master_instructions_install_update_and_uninstall(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".grok").mkdir()
    mine = "# My own rules\nKeep answers short.\n"
    (tmp_path / ".claude" / "CLAUDE.md").write_text(mine)
    res = I.install("all", url="http://hub:8770/mcp")
    by = {(r["ai"], r.get("file", "").split("/")[-1]): r["status"] for r in res}
    assert by[("claude-code", "CLAUDE.md")] == "installed" and by[("grok", "AGENTS.md")] == "installed"
    assert by[("codex", "")].startswith("skipped")                      # not installed on this machine
    claude = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert claude.startswith(mine.strip()) and "start_work" in claude and "http://hub:8770/mcp" in claude
    skill = (tmp_path / ".claude" / "skills" / "commons" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: commons\ndescription: ") and "use_credential" in skill
    assert (tmp_path / ".claude" / "CLAUDE.md.bak-commons").read_text() == mine
    assert {r["status"] for r in I.install("claude-code", url="http://hub:8770/mcp") if r["file"].endswith("CLAUDE.md")} == {"unchanged"}
    I.install("claude-code", url="http://other:8770/mcp")               # updated in place, not appended twice
    claude = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert claude.count(I.BEGIN) == 1 and "other:8770" in claude and "hub:8770" not in claude
    I.install("all", uninstall=True)
    assert (tmp_path / ".claude" / "CLAUDE.md").read_text().strip() == mine.strip()
    assert not (tmp_path / ".claude" / "skills" / "commons" / "SKILL.md").exists()


def test_prompt_md_is_generated_from_the_master_instructions():
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(root / "scripts" / "gen_prompt.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
