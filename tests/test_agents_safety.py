import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from commons_memory import a2a
from commons_memory.redact import redact, scan
from commons_memory.secrets import Broker
from commons_memory.store import CommonsError

# fake secrets are assembled at runtime so no secret-looking literal lives in the repo
FAKE_GH = "gh" + "p_" + "A1b2C3d4" * 5
FAKE_AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
FAKE_PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIabc\n-----END " + "RSA PRIVATE KEY-----"


def test_redaction_scanner():
    text = f"token {FAKE_GH} aws {FAKE_AWS}\n{FAKE_PEM}\npassword=hunter22xyz db=postgres://u:pw1234@h/db"
    clean, kinds = redact(text)
    assert FAKE_GH not in clean and FAKE_AWS not in clean and "MIIabc" not in clean
    assert "hunter22xyz" not in clean and "pw1234" not in clean
    assert {"github_token", "aws_access_key", "private_key", "assignment", "url_credentials"} <= set(kinds)
    assert scan("api_key=env://OPENAI_KEY") == []  # references are allowed


def test_writes_are_redacted_everywhere(store):
    a = store.ensure_agent("x")
    m = store.add_item("memory", f"deploy with {FAKE_GH}", title=f"key {FAKE_AWS}", agent_id=a)
    assert set(m["redacted"]) >= {"github_token", "aws_access_key"}
    item = store.expand(m["id"])
    assert FAKE_GH not in json.dumps(item) and FAKE_AWS not in json.dumps(item)
    assert store.search(FAKE_GH)["results"] == [] or FAKE_GH not in store.search(FAKE_GH)["text"]
    s = store.session_start(f"goal {FAKE_GH}", a)
    store.checkpoint(s["session_id"], "x", {"tok": FAKE_GH}, a)
    t = store.post_task(a, "y", f"use {FAKE_AWS}")
    dump = json.dumps([dict(r) for r in store.q("SELECT * FROM sessions")] +
                      [dict(r) for r in store.q("SELECT * FROM inbox")] +
                      [dict(r) for r in store.q("SELECT title,summary,content,meta FROM items")])
    assert FAKE_GH not in dump and FAKE_AWS not in dump and t["redacted"]


def test_agent_keys_and_provenance(store):
    reg = store.register_agent("claude", "anthropic", "opus")
    assert reg["api_key"].startswith("cmk_")
    assert store.authenticate(reg["api_key"])["id"] == reg["id"]
    assert store.authenticate("cmk_wrong") is None
    assert reg["api_key"] not in json.dumps([dict(r) for r in store.q("SELECT * FROM agents")])
    m = store.add_item("memory", "hello", agent_id=reg["id"], authenticated=True)
    assert store.expand(m["id"])["agent"] == "claude" and store.expand(m["id"])["authenticated"] == 1


def test_conflict_detection_and_resolution(store):
    a, b = store.ensure_agent("a"), store.ensure_agent("b")
    f1 = store.add_item("fact", "prod DB is postgres 15", subject="prod-db", predicate="version", value="15", agent_id=a)
    same = store.add_item("fact", "prod DB is postgres 15 (confirmed)", subject="prod-db", predicate="version", value="15", agent_id=b)
    assert "conflicts_with" not in same
    f2 = store.add_item("fact", "prod DB is postgres 16", subject="prod-db", predicate="version", value="16", agent_id=b)
    assert f1["id"] in f2["conflicts_with"]
    c = [c for c in store.list_conflicts() if c["b"] == f2["id"] and c["a"] == f1["id"]][0]
    assert "⚠conflict" in store.search("prod DB postgres")["text"]
    store.resolve_conflict(c["id"], f2["id"], b)
    ids = [r["id"] for r in store.search("prod DB postgres")["results"]]
    assert f1["id"] not in ids and f2["id"] in ids


def test_inbox_delegate_respond(store):
    a, b = store.ensure_agent("planner"), store.ensure_agent("coder")
    t = store.post_task(a, "coder", "write tests for auth", context_ids=[])
    broadcast = store.post_task(a, "*", "anyone: review PR")
    got = store.inbox(b, claim=True)
    assert {g["id"] for g in got} == {t["task_id"], broadcast["task_id"]}
    assert store.inbox(b) == []
    store.respond_task(t["task_id"], "added 12 tests", agent_id=b)
    assert store.get_task(t["task_id"])["result"] == "added 12 tests"


def test_secret_broker_leases(store, monkeypatch):
    monkeypatch.setenv("DEMO_TOKEN", "s3cr3t-value")
    a = store.ensure_agent("ops")
    other = store.ensure_agent("intruder")
    br = Broker(store)
    with pytest.raises(CommonsError):
        br.register("plaintext-secret")
    br.register("env://DEMO_TOKEN", "demo", allowed_agents=["ops"], max_ttl=60)
    with pytest.raises(CommonsError):
        br.request("env://DEMO_TOKEN", other)
    lease = br.request("env://DEMO_TOKEN", a, ttl=9999)
    assert "s3cr3t" not in json.dumps(lease)
    assert br.resolve(lease["lease_id"], a) == "s3cr3t-value"
    with pytest.raises(CommonsError):
        br.resolve(lease["lease_id"], other)
    br.revoke(lease["lease_id"])
    with pytest.raises(CommonsError):
        br.resolve(lease["lease_id"])
    expired = br.request("env://DEMO_TOKEN", a, ttl=1)
    store.x("UPDATE leases SET expires_at=0 WHERE id=?", (expired["lease_id"],))
    with pytest.raises(CommonsError):
        br.resolve(expired["lease_id"])
    log = br.audit_log()
    actions = {e["action"] for e in log}
    assert {"secret.register", "secret.lease", "secret.resolve", "secret.revoke", "secret.request.denied",
            "secret.resolve.denied"} <= actions
    assert "s3cr3t" not in json.dumps(log)


def test_a2a_client_stub():
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = json.dumps({"jsonrpc": "2.0", "id": got["body"]["id"],
                              "result": {"kind": "message", "parts": [{"kind": "text", "text": "pong"}]}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            out = json.dumps({"name": "echo", "url": "x"}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    assert a2a.fetch_agent_card(url)["name"] == "echo"
    r = a2a.send_message(url, f"ping {FAKE_GH}")
    srv.shutdown()
    assert r["result"]["parts"][0]["text"] == "pong"
    assert got["body"]["method"] == "message/send" and FAKE_GH not in json.dumps(got["body"])
