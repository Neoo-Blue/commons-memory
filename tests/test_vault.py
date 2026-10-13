import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from commons_memory import vault as V
from commons_memory.store import CommonsError

SECRET = "plex-tok-9f3a71c2"


@pytest.fixture()
def svc(monkeypatch):
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.command, self.path, self.headers.get("X-Plex-Token")))
            body = json.dumps({"added": ["Corpse Bride"], "echo": self.headers.get("X-Plex-Token")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        do_DELETE = do_GET

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def _judge(monkeypatch, **scores):
    from commons_memory import typesafe
    monkeypatch.setattr(typesafe, "enabled", lambda: True)
    calls = []
    monkeypatch.setattr(typesafe, "noul", lambda state, qs, timeout=20: calls.append(qs) or {k: scores.get(k, 0) for k in qs})
    return calls


def test_values_are_encrypted_and_never_listed(store, svc):
    base, _ = svc
    V.add(store, "plex", SECRET, "plex", [base], {"header": "X-Plex-Token"})
    raw = store.q("SELECT * FROM vault_creds")[0]
    assert SECRET.encode() not in bytes(raw["secret"]) and SECRET not in json.dumps(V.listing(store))
    assert V._value(store, "plex") == SECRET
    os.environ["COMMONS_VAULT_KEY"] = base64.b64encode(os.urandom(32)).decode()   # another key can't read it
    with pytest.raises(CommonsError, match="can't be decrypted"):
        V._value(store, "plex")


def test_key_only_goes_to_its_own_service_and_reasons_are_required(store, svc):
    base, _ = svc
    V.add(store, "plex", SECRET, "plex", [base], {"header": "X-Plex-Token"})
    a = store.ensure_agent("muse")
    with pytest.raises(CommonsError, match="may only be sent to"):
        V.use(store, a, "plex", "GET", "http://evil.example/steal", "need the recently added list")
    with pytest.raises(CommonsError, match="may only be sent to"):
        V.use(store, a, "plex", "GET", base.replace("127.0.0.1", "user@127.0.0.1") + "/x", "need the recently added list")
    with pytest.raises(CommonsError, match="justify"):
        V.use(store, a, "plex", "GET", base + "/library/recentlyAdded", "pls")


def test_a_sound_read_is_allowed_scrubbed_and_granted(store, svc, monkeypatch):
    base, seen = svc
    calls = _judge(monkeypatch, legit=0.92)
    V.add(store, "plex", SECRET, "plex", [base], {"header": "X-Plex-Token"})
    a = store.ensure_agent("muse")
    r = V.use(store, a, "plex", "GET", base + "/library/recentlyAdded", "the user asked what was added to Plex today")
    assert r["verdict"] == "allowed" and r["status"] == 200 and "Corpse Bride" in r["body"]
    assert seen[-1][2] == SECRET and SECRET not in json.dumps(r)                 # sent to Plex, never shown
    V.use(store, a, "plex", "GET", base + "/library/sections", "listing the libraries for the same question")
    assert len(calls) == 1                                                        # second read rode the grant


def test_writes_and_bad_reasons(store, svc, monkeypatch):
    base, seen = svc
    V.add(store, "plex", SECRET, "plex", [base], {"header": "X-Plex-Token"})
    a = store.ensure_agent("grok")
    _judge(monkeypatch, legit=0.1)
    assert V.use(store, a, "plex", "GET", base + "/x", "just poking around the server")["verdict"] == "denied"
    _judge(monkeypatch, legit=0.9, destructive=0.8, user_backed=0.2)
    w = V.use(store, a, "plex", "DELETE", base + "/library/metadata/42", "remove a duplicate movie copy")
    assert w["verdict"] == "waiting_for_user" and not [x for x in seen if x[0] == "DELETE"]
    from commons_memory import board as B
    q = B.get(store, w["question_id"])
    assert q["status"] == "blocked" and "DELETE" in q["text"]
    B.answer(store, w["question_id"], "yes, that copy is a duplicate")             # the user approves on the board
    _judge(monkeypatch)                                                           # judge not consulted again
    ok = V.use(store, a, "plex", "DELETE", base + "/library/metadata/42", "remove a duplicate movie copy")
    assert ok["verdict"] == "allowed" and [x for x in seen if x[0] == "DELETE"]
    again = V.use(store, a, "plex", "DELETE", base + "/library/metadata/42", "remove a duplicate movie copy")
    assert again["verdict"] != "allowed"                                         # one approved write = one call


def test_no_judge_means_the_user_decides(store, svc, monkeypatch):
    base, _ = svc
    from commons_memory import typesafe
    monkeypatch.setattr(typesafe, "enabled", lambda: False)
    V.add(store, "plex", SECRET, "plex", [base], {"header": "X-Plex-Token"})
    r = V.use(store, store.ensure_agent("muse"), "plex", "GET", base + "/x", "the user asked what was added today")
    assert r["verdict"] == "waiting_for_user"


def test_locked_vault(store, monkeypatch):
    monkeypatch.delenv("COMMONS_VAULT_KEY", raising=False)
    with pytest.raises(CommonsError, match="locked"):
        V.add(store, "plex", SECRET, "plex", ["http://x/"], {"bearer": True})
