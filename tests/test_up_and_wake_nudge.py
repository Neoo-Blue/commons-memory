"""`commons up` (the installer's last step) and the pickup line that lets an unwakeable agent fix itself."""
from commons_memory import setup as SU
from commons_memory.join import wake_nudge
from commons_memory.store import Store


def test_up_starts_the_hub_and_connects_every_cli_found(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_HOME", str(tmp_path))
    units, ran = [], []
    monkeypatch.setattr("commons_memory.cli_v7._install_unit", lambda kind, name, unit, **k: units.append((kind, unit)) or 0)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: type("R", (), {"read": lambda self: b"ok"})())
    monkeypatch.setattr(SU.shutil, "which", lambda b: f"/bin/{b}" if b in ("claude", "codex", "commons") else None)
    monkeypatch.setattr(SU, "registered", lambda c, url: False)
    monkeypatch.setattr(SU, "elsewhere", lambda c, url: False)
    monkeypatch.setattr(SU, "run", lambda c, n, url, skip: ran.append((c, n, url)) or 0)
    db = str(tmp_path / "c.db")
    assert SU.up(db, 8765, 8766) == 0
    assert "--dashboard" in units[0][1] and "COMMONS_BACKGROUND" in units[0][1]
    assert [c for c, _, _ in ran] == ["claude-code", "codex"] and ran[0][2] == "http://127.0.0.1:8765"
    for _, n, _ in ran:  # each got its own key, saved privately, and is a real agent
        f = tmp_path / "agents" / f"{n}.key"
        assert f.read_text().strip() and oct(f.stat().st_mode)[-3:] == "600" and Store(db).get_agent(n)
    ran.clear()
    assert SU.up(db, 8765, 8766) == 0 and len(ran) == 2  # a second run reuses the keys instead of failing on the names


def test_an_agent_commons_cannot_wake_is_told_once_a_day_how_to_fix_it(tmp_path):
    s = Store(str(tmp_path / "c.db"))
    aid = s.register_agent("muse")["id"]
    first = wake_nudge(s, aid)
    assert "CAN'T WAKE YOU" in first and "--agent muse" in first and "--wake-cmd" in first
    assert wake_nudge(s, aid) == ""  # not again today
    assert wake_nudge(s, aid, every=0)  # and again once the day has passed, while it still can't be woken
    s.x("INSERT INTO wake_channels(id,agent_id,kind,config,secret,enabled,created_at,failures) VALUES('w1',?,'command','{}','',1,0,0)", (aid,))
    assert wake_nudge(s, aid, every=0) == ""


def test_a_bare_doctor_checks_every_ai_setup_connected_here(tmp_path, monkeypatch, capsys):
    from commons_memory import setup
    monkeypatch.setenv("COMMONS_HOME", str(tmp_path))
    assert setup.doctor_all() == 1  # nothing connected yet: says what to run
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "codex-box.json").write_text('{"client": "codex", "url": "http://127.0.0.1:8765"}')
    seen = []
    monkeypatch.setattr(setup, "doctor", lambda c, a, u, r=False: seen.append((c, a, u, r)) or 0)
    assert setup.doctor_all(True) == 0
    assert seen == [("codex", "codex-box", "http://127.0.0.1:8765", True)]


def test_up_leaves_a_client_on_another_hub_alone(tmp_path, monkeypatch):
    from commons_memory import setup as S
    monkeypatch.setattr(S.Path, "home", lambda: tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('[mcp_servers.commons]\nurl = "http://10.0.0.5:8770/mcp"\n')
    assert S.elsewhere("codex", "http://127.0.0.1:8765") and not S.registered("codex", "http://127.0.0.1:8765")
    assert S.registered("codex", "http://10.0.0.5:8770/mcp") and not S.elsewhere("codex", "http://10.0.0.5:8770")
    assert not S.elsewhere("claude-code", "http://127.0.0.1:8765")  # no config at all: free to connect
