import os
import subprocess
import threading
from pathlib import Path

import pytest

from commons_memory.dashboard import _private_host, make_server
from commons_memory.store import CommonsError, Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("host,ok", [("127.0.0.1", True), ("localhost", True), ("::1", True), ("10.1.2.3", True),
                                     ("192.168.1.5", True), ("172.20.0.2", True), ("100.125.2.90", True),
                                     ("fd7a:115c:a1e0::1", True), ("169.254.1.1", True), ("0.0.0.0", False),
                                     ("::", False), ("8.8.8.8", False), ("100.200.1.1", False), ("example.com", False)])
def test_private_host(host, ok):
    assert _private_host(host) is ok


def test_dashboard_token_policy(store, monkeypatch):
    monkeypatch.delenv("COMMONS_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("COMMONS_DASHBOARD_REQUIRE_TOKEN", raising=False)
    make_server(store, "127.0.0.1", 0).server_close()  # private: no token needed
    with pytest.raises(CommonsError, match="public/wildcard"):
        make_server(store, "0.0.0.0", 0)
    make_server(store, "0.0.0.0", 0, token="t").server_close()
    monkeypatch.setenv("COMMONS_DASHBOARD_REQUIRE_TOKEN", "1")
    with pytest.raises(CommonsError, match="REQUIRE_TOKEN"):
        make_server(store, "127.0.0.1", 0)


def test_ensure_agent_race_on_fresh_db(tmp_path):
    db = str(tmp_path / "race.db")
    Store(db)
    errs, ids = [], []

    def go():
        try:
            ids.append(Store(db).ensure_agent("librarian"))
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(set(ids)) == 1


def test_entrypoint_reads_file_secrets_and_builds_serve_args(tmp_path):
    sec = tmp_path / "tok"
    sec.write_text("from-secret")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "commons").write_text('#!/bin/sh\necho "TOKEN=$COMMONS_DASHBOARD_TOKEN ARGS=$*"\n')
    (fake / "commons").chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "COMMONS_DASHBOARD_TOKEN_FILE": str(sec), "COMMONS_DB": "/x.db"}
    out = subprocess.run(["sh", str(ROOT / "docker" / "entrypoint.sh"), "serve"], env=env, capture_output=True, text=True).stdout
    assert "TOKEN=from-secret" in out and "--host 0.0.0.0 --port 8765 --dashboard" in out
    out = subprocess.run(["sh", str(ROOT / "docker" / "entrypoint.sh"), "stats"], env=env, capture_output=True, text=True).stdout
    assert out.strip().endswith("ARGS=stats")


def test_compose_and_dockerfile_shape():
    yaml = pytest.importorskip("yaml")
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    s = c["services"]
    assert {"commons", "postgres", "tailscale", "cloudflared", "ngrok"} <= set(s)
    assert all(p.startswith("${COMMONS_BIND:-127.0.0.1}") for p in s["commons"]["ports"])
    assert s["postgres"]["profiles"] == ["postgres"] and "dashboard_token" in c["secrets"]
    d = (ROOT / "Dockerfile").read_text()
    for needle in ("USER commons", "tini", "HEALTHCHECK", 'VOLUME ["/data"]', "EXPOSE 8765 8766", "AS build"):
        assert needle in d
