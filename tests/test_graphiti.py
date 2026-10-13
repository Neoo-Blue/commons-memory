"""Roadmap: Graphiti adapter (push memories as episodes, pull fact edges back), against a fake Graphiti server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from commons_memory import graphiti as G
from commons_memory import rowacl as R


@pytest.fixture()
def server():
    state = {"messages": [], "edges": {
        "e1": {"uuid": "e1", "name": "RUNS_ON", "fact": "Plex runs on LXC 100", "valid_at": "2026-10-01T00:00:00Z",
               "invalid_at": None, "expired_at": None, "episodes": ["ep1"]},
        "e2": {"uuid": "e2", "name": "RUNS_ON", "fact": "Plex runs on the old NAS", "valid_at": "2025-01-01T00:00:00Z",
               "invalid_at": "2026-10-01T00:00:00Z", "expired_at": None, "episodes": ["ep1"]}}}

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/messages":
                state["messages"] += [m | {"group_id": body["group_id"]} for m in body["messages"]]
                return self._send(202, {"message": "Messages added to processing queue", "success": True})
            self._send(404, {})

        def do_GET(self):
            if self.path.startswith("/episodes/commons"):
                return self._send(200, [{"uuid": "ep1", "group_id": "commons", "entity_edges": ["e1", "e2"]}])
            if self.path.startswith("/entity-edge/"):
                return self._send(200, state["edges"][self.path.rsplit("/", 1)[1]])
            self._send(404, {})

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", state
    srv.shutdown()


def test_push_sends_each_memory_once_and_never_a_restricted_one(store, server):
    url, state = server
    a = store.ensure_agent("claude")
    store.add_item("memory", "Plex runs on LXC 100 at 192.168.50.203", agent_id=a, project="homelab")
    store.add_item("memory", "where does Plex run?", tags=["conversation", "user"], agent_id=a)
    secret = store.add_item("memory", "the vault key sits on the NAS", agent_id=a)["id"]
    R.restrict(store, None, secret, ["claude"])
    out = G.push(store, url)
    assert out["sent"] == 2 and out["skipped_restricted"] == 1
    m = {x["content"]: x for x in state["messages"]}
    assert m["where does Plex run?"]["role_type"] == "user"
    assert m["Plex runs on LXC 100 at 192.168.50.203"]["role"] == "claude"
    assert "in homelab" in m["Plex runs on LXC 100 at 192.168.50.203"]["source_description"]
    assert G.push(store, url)["sent"] == 0  # the watermark keeps re-runs incremental
    store.add_item("decision", "move Plex transcodes to the GPU box", agent_id=a)
    assert G.push(store, url)["sent"] == 1 and len({x["uuid"] for x in state["messages"]}) == 3


def test_pull_brings_facts_back_once_with_their_validity(store, server):
    url, _ = server
    out = G.pull(store, url, project="homelab")
    assert (out["facts_added"], out["invalid"]) == (2, 1)
    facts = {r["content"]: r for r in store.q("SELECT content, tags, meta FROM items WHERE kind='fact'")}
    assert '"invalid"' in facts["Plex runs on the old NAS"]["tags"]
    assert json.loads(facts["Plex runs on LXC 100"]["meta"])["graphiti_uuid"] == "e1"
    assert G.pull(store, url)["facts_added"] == 0  # already imported
