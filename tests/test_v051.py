"""Regression tests for bugs found in the v0.5.0 live two-agent test (fixed in 0.5.1)."""
import sys
import inspect

from commons_memory import cli
from commons_memory.store import Store


def test_remote_worker_sends_bearer_header(monkeypatch):
    """McpClient must authenticate the HTTP request itself (auth middleware runs before tools)."""
    from commons_memory import worker
    src = inspect.getsource(worker.McpClient._call)
    assert "Authorization" in src and "Bearer" in src


def test_serve_dashboard_follows_host(monkeypatch):
    seen = {}
    import commons_memory.dashboard as d
    import commons_memory.server as srv
    monkeypatch.setattr(d, "serve_dashboard", lambda store, host="127.0.0.1", **kw: seen.setdefault("host", host))
    monkeypatch.setattr(srv, "run", lambda *a, **k: None)
    cli.main(["serve", "--transport", "http", "--host", "10.1.2.3", "--dashboard"])
    assert seen["host"] == "10.1.2.3"
    seen.clear()
    cli.main(["serve", "--transport", "http", "--host", "10.1.2.3", "--dashboard", "--dashboard-host", "127.0.0.1"])
    assert seen["host"] == "127.0.0.1"


def test_secret_exec_masks_value(capfd):
    rc = cli.run_masked([sys.executable, "-c", "import os,sys;print('v='+os.environ['T']);print('e='+os.environ['T'],file=sys.stderr)"],
                        {"T": "s3cr3t-value-xyz", "PATH": "/usr/bin:/bin"}, "s3cr3t-value-xyz")
    out, err = capfd.readouterr()
    assert rc == 0 and "s3cr3t" not in out + err and "v=***" in out and "e=***" in err


def test_cli_commons_error_is_clean(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("COMMONS_DB", str(tmp_path / "c.db"))
    rc = cli.main(["--db", str(tmp_path / "c.db"), "secret-exec", "lea_missing", "--as", "X", "--", "true"])
    assert rc == 1 and "commons: error:" in capsys.readouterr().err


def test_kb_unknown_bucket_not_materialized(tmp_path):
    from commons_memory.kb import KB
    s = Store(str(tmp_path / "k.db"))
    KB(s)._ensure("no-such-project?")
    assert not s.q("SELECT 1 FROM kb_pages WHERE bucket=?", ("no-such-project?",))


def test_dashboard_org_chart_initializes_selection():
    from commons_memory.dashboard import PAGE
    assert 'window.oc_sel=window.oc_sel||""' in PAGE


def test_skill_errors_explain_next_step(tmp_path):
    import pytest
    from commons_memory.skillpool import SkillPool
    from commons_memory.store import CommonsError
    s = Store(str(tmp_path / "s.db"))
    a = None
    sp = SkillPool(s)
    sp.publish("xskill", "# x", agent_id=a)
    with pytest.raises(CommonsError, match="skill_review"):
        sp.get("xskill")
    with pytest.raises(CommonsError, match="pending"):
        sp.rollback("xskill", 1)


def test_acl_bootstrap_by_sole_author(store, monkeypatch):
    import asyncio
    import pytest
    from commons_memory.auth import PRINCIPAL
    from commons_memory.server import build_server
    from commons_memory.store import CommonsError
    monkeypatch.delenv("COMMONS_ADMIN_AGENTS", raising=False)
    ka = store.register_agent("alice")["api_key"]
    kb = store.register_agent("bob")["api_key"]
    a, b = store.ensure_agent("alice"), store.ensure_agent("bob")
    srv = build_server(store, require_keys=True)

    def grant(key, who, project):
        tok = PRINCIPAL.set(who)
        try:
            return asyncio.run(srv.call_tool("acl_grant", {"project": project, "grantee": "alice", "role": "admin",
                                                           "api_key": key}))
        finally:
            PRINCIPAL.reset(tok)
    store.add_item("memory", "alice-only plan", project="priv", agent_id=a)
    with pytest.raises(Exception, match="sole author"):
        grant(kb, b, "priv")          # bob did not write it
    grant(ka, a, "priv")              # sole author bootstraps the ACL
    store.add_item("memory", "shared", project="mixed", agent_id=a)
    store.add_item("memory", "shared2", project="mixed", agent_id=b)
    with pytest.raises(Exception, match="admin rights"):
        grant(ka, a, "mixed")         # multi-author project still needs an admin


def test_header_only_auth_updates_last_seen(store):
    import asyncio
    from commons_memory.auth import PRINCIPAL
    from commons_memory.server import build_server
    store.register_agent("hdr")
    aid = store.ensure_agent("hdr")
    store.x("UPDATE agents SET last_seen=0 WHERE id=?", (aid,))
    srv = build_server(store, require_keys=True)
    tok = PRINCIPAL.set(aid)
    try:
        asyncio.run(srv.call_tool("remember", {"content": "header-only call"}))
    finally:
        PRINCIPAL.reset(tok)
    assert store.q("SELECT last_seen FROM agents WHERE id=?", (aid,))[0]["last_seen"] > 0
