"""v0.24: approvals only the owner can give (PIN set with a host-only setup code), approving runs the request, and an
owner can approve everything at their own risk."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from commons_memory import approvals as AP
from commons_memory import board as B
from commons_memory import settings
from commons_memory import vault as V
from commons_memory.dashboard import make_server
from commons_memory.store import CommonsError

from test_vault import _judge, svc  # noqa: F401 - the fake service fixture and judge stub


def _ask(store, base, monkeypatch, method="POST", path="/items"):
    V.add(store, "svc", "tok-123456789", "svc", [base], {"bearer": True}, risk="high") \
        if not store.q("SELECT 1 FROM vault_creds WHERE name='svc'") else None
    _judge(monkeypatch, legit=0.9, destructive=0.9, user_backed=0.9)
    return V.use(store, store.ensure_agent("claude"), "svc", method, base + path, "create the item the user asked for",
                 body='{"name": "x"}', content_type="application/json")


def test_the_first_pin_needs_the_setup_code_from_the_hub(store):
    assert not AP.has_pin(store)
    with pytest.raises(CommonsError, match="setup code"):
        AP.set_pin(store, "123456", code="0000-0000")  # an agent guessing
    code = AP.setup_code(store)
    AP.set_pin(store, "482915", code=code)
    assert AP.has_pin(store) and AP.check_pin(store, "482915") and not AP.check_pin(store, "000000")
    with pytest.raises(CommonsError, match="current password"):
        AP.set_pin(store, "111111", code=code)  # the code is spent; changing needs the PIN
    AP.set_pin(store, "222222", by="host")  # on the hub itself
    assert AP.check_pin(store, "222222")


def test_wrong_pins_lock_approvals(store):
    AP.set_pin(store, "482915", by="host")
    for _ in range(AP.LOCK_AFTER):
        AP.check_pin(store, "000000")
    with pytest.raises(CommonsError, match="too many"):
        AP.check_pin(store, "482915")


def test_approving_runs_the_request_and_similar_and_always(store, svc, monkeypatch):  # noqa: F811
    base, seen = svc
    w = _ask(store, base, monkeypatch, "DELETE", "/a")
    assert w["verdict"] == "waiting_for_user" and w["approve_url"].endswith("/approve/" + w["question_id"])
    page = AP.render(store, w["question_id"], trusted=False)
    assert "DELETE" in page and "Choose your owner password" in page and "setup code" in page
    AP.set_pin(store, "482915", by="host")
    with pytest.raises(CommonsError, match="wrong password"):
        AP.answer(store, w["question_id"], "yes", pin="999999")
    out = AP.answer(store, w["question_id"], "all", pin="482915")
    assert out["result"]["status"] == 200 and [x for x in seen if x[0] == "DELETE"], out["result"]
    _judge(monkeypatch, legit=0.9, destructive=0.9, user_backed=0.9)
    assert V.use(store, store.ensure_agent("claude"), "svc", "DELETE", base + "/b",
                 "remove the second item the user asked for")["verdict"] == "allowed"  # "similar for 1 h"
    w2 = _ask(store, base, monkeypatch, "PUT", "/c")
    AP.answer(store, w2["question_id"], "always", session=AP.session_token(store))
    assert store.q("SELECT risk FROM vault_creds WHERE name='svc'")[0]["risk"] == "always"
    assert V.use(store, store.ensure_agent("claude"), "svc", "PATCH", base + "/d",
                 "edit the item the user asked about")["verdict"] == "allowed"


def test_approve_everything_is_the_owners_choice(store, svc, monkeypatch):  # noqa: F811
    base, _ = svc
    assert _ask(store, base, monkeypatch)["verdict"] == "waiting_for_user"
    settings.put(store, "auto_approve_all", True)
    assert _ask(store, base, monkeypatch, path="/other")["verdict"] == "allowed"
    from commons_memory.importers import import_items
    from commons_memory.librarian import Librarian
    assert Librarian(store).auto_approve()
    import_items(store, [{"src": "x:1", "kind": "memory", "content": "a fact worth keeping", "status": "review"}])
    assert store.q("SELECT 1 FROM items WHERE content='a fact worth keeping'")
    assert next(x for x in settings.listing(store) if x["key"] == "auto_approve_all")["risky"]


def test_the_dashboard_wont_take_a_key_approval_without_the_owner(store, svc, monkeypatch):  # noqa: F811
    base, _ = svc
    w = _ask(store, base, monkeypatch)
    q = B.add(store, "question", "which colour for the button?", agent_id=store.ensure_agent("claude"))
    srv = make_server(store, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body, cookie=""):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Commons": "1", "Content-Type": "application/json", "Cookie": cookie})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read()), r.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), ""
    try:
        code, body, _ = post("/api/board/answer", {"id": w["question_id"], "text": "yes"})  # an agent on the LAN
        assert code == 403 and body["approve_url"] == "/approve/" + w["question_id"]
        assert post("/api/board/answer", {"id": q["id"], "text": "blue"})[0] == 200  # ordinary questions still work
        with urllib.request.urlopen(url + "/approve/" + w["question_id"], timeout=8) as r:
            assert "Approve" in r.read().decode()
        AP.set_pin(store, "482915", by="host")
        assert post("/approve/" + w["question_id"], {"choice": "yes", "pin": "111111"})[0] == 403
        code, body, cookie = post("/approve/" + w["question_id"], {"choice": "yes", "pin": "482915"})
        assert code == 200 and body["status"] == "done" and "commons_approver=" in cookie and "HttpOnly" in cookie
    finally:
        srv.shutdown()



def test_a_second_answer_never_runs_the_request_twice(store, svc, monkeypatch):  # noqa: F811
    base, seen = svc
    w = _ask(store, base, monkeypatch, "DELETE", "/once")
    text = B.get(store, w["question_id"])["text"]
    V.answered(store, text, "yes")
    V.answered(store, text, "yes")  # a double click, or the phone and the dashboard both answering
    assert sum(1 for x in seen if x[:2] == ("DELETE", "/once")) == 1


def test_a_request_that_failed_on_approval_can_be_retried(store, svc, monkeypatch):  # noqa: F811
    dead = "http://127.0.0.1:9"  # nothing listens: the approved call can't reach the service
    V.add(store, "dead", "tok-123456789", "dead", [dead], {"bearer": True}, risk="high")
    _judge(monkeypatch, legit=0.9, destructive=0.9, user_backed=0.9)
    aid = store.ensure_agent("claude")
    args = ("dead", "POST", dead + "/x", "create the item the user asked for")
    w = V.use(store, aid, *args, body='{"a": 1}', content_type="application/json")
    AP.set_pin(store, "482915", by="host")
    assert not AP.answer(store, w["question_id"], "yes", pin="482915")["result"]["status"]
    assert V.use(store, aid, *args, body='{"a": 1}', content_type="application/json")["verdict"] == "allowed"


def test_waiting_requests_dedupe_by_body(store, svc, monkeypatch):  # noqa: F811
    base, _ = svc
    aid = store.ensure_agent("claude")
    first = _ask(store, base, monkeypatch)
    assert _ask(store, base, monkeypatch)["question_id"] == first["question_id"]  # same call asked twice: one question
    other = V.use(store, aid, "svc", "POST", base + "/items", "create the item the user asked for",
                  body='{"name": "something else"}', content_type="application/json")
    assert other["question_id"] != first["question_id"]  # approving one body must not approve another


def test_sessions_need_a_pin_and_end_when_it_changes(store):
    assert not AP.session_ok(store, AP.session_token(store))  # no PIN yet: nothing is trusted
    AP.set_pin(store, "482915", by="host")
    tok = AP.session_token(store)
    assert AP.session_ok(store, tok)
    AP.set_pin(store, "777111", by="host")
    assert not AP.session_ok(store, tok)  # a changed PIN logs every browser out


def test_lockouts_are_per_device(store):
    AP.set_pin(store, "482915", by="host")
    for _ in range(AP.LOCK_AFTER):
        AP.check_pin(store, "000000", client="10.0.0.9")
    with pytest.raises(CommonsError, match="too many"):
        AP.check_pin(store, "482915", client="10.0.0.9")
    assert AP.check_pin(store, "482915", client="10.0.0.5")  # an agent guessing can't lock the owner out


def test_owner_only_dashboard_changes_need_the_pin(store):
    srv = make_server(store, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body, cookie=""):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Commons": "1", "Content-Type": "application/json", "Cookie": cookie})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read()), r.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), ""
    try:
        code, body, _ = post("/api/setup/setting", {"key": "auto_approve_all", "value": True})
        assert code == 403 and body["approve_url"] == "/approve/pin"  # no PIN: nobody can flip approve-everything
        AP.set_pin(store, "482915", by="host")
        for path, b in (("/api/setup/setting", {"key": "auto_approve_all", "value": True}),
                        ("/api/setup/provider", {"kind": "anthropic", "key": "x", "base_url": "http://evil"}),
                        ("/api/vault/rm", {"name": "svc"}), ("/api/mcp/policy", {"effect": "allow"}),
                        ("/api/connect/key", {"name": "sneaky", "role": "admin"})):
            code, body, _ = post(path, b)
            assert code == 403 and body["need_pin"], path
        assert not settings.get(store, "auto_approve_all")
        assert post("/approve/unlock", {"pin": "111111"})[0] == 403
        code, _, cookie = post("/approve/unlock", {"pin": "482915"})
        assert code == 200 and "commons_approver=" in cookie
        cookie = cookie.split(";")[0]
        assert post("/api/setup/setting", {"key": "auto_approve_all", "value": True}, cookie)[0] == 200
        assert settings.get(store, "auto_approve_all")
    finally:
        srv.shutdown()


def test_agents_cant_repoint_providers_or_undo_a_stop(store, monkeypatch):
    import asyncio

    from commons_memory.server import build_server
    monkeypatch.delenv("COMMONS_ADMIN_AGENTS", raising=False)
    mcp = build_server(store)
    key = store.register_agent("sneaky")["api_key"]
    owner = store.register_agent("ops")["api_key"]

    def call(tool, args):
        return asyncio.run(mcp.call_tool(tool, args))
    for tool, args in (("provider_add", {"name": "anthropic", "kind": "anthropic", "base_url": "http://evil"}),
                       ("set_role_model", {"role": "librarian", "model": "evil/model"}),
                       ("kill_switch", {"paused": False})):
        with pytest.raises(Exception, match="admin rights"):
            call(tool, args | {"api_key": key})
    call("kill_switch", {"paused": True, "api_key": key})  # anyone can stop things
    call("secret_register", {"ref": "env://SOME_KEY", "allowed_agents": ["ops"], "api_key": owner})
    with pytest.raises(Exception, match="admin rights"):
        call("secret_register", {"ref": "env://SOME_KEY", "allowed_agents": ["sneaky"], "api_key": key})
    monkeypatch.setenv("COMMONS_ADMIN_AGENTS", "ops")
    call("kill_switch", {"paused": False, "api_key": owner})


def test_approved_reads_and_runs_in_flight_dont_loop_or_repeat(store, svc, monkeypatch):  # noqa: F811
    base, seen = svc
    aid = store.ensure_agent("claude")
    settings.put(store, "judge_chain", ["ask"])  # every request comes to the user, reads included
    w = _ask(store, base, monkeypatch, "GET", "/secret-list")
    AP.set_pin(store, "482915", by="host")
    AP.answer(store, w["question_id"], "yes", pin="482915")
    again = V.use(store, aid, "svc", "GET", base + "/secret-list", "create the item the user asked for",
                  body='{"name": "x"}', content_type="application/json")
    assert again["verdict"] == "allowed" and again["status"] == 200  # fetched fresh, not asked again
    w2 = _ask(store, base, monkeypatch, "DELETE", "/slow")
    did = store.q("SELECT id FROM vault_decisions WHERE question_id=?", (w2["question_id"],))[0]["id"]
    store.x("UPDATE vault_decisions SET verdict='user_allowed', ts=? WHERE id=?", (__import__("time").time(), did))
    assert _ask(store, base, monkeypatch, "DELETE", "/slow")["verdict"] == "running"  # approved, mid-call
    assert AP.details(store, w2["question_id"])["running"]
    assert not [x for x in seen if x[:2] == ("DELETE", "/slow")]


def test_an_approval_given_hours_later_still_counts(store, svc, monkeypatch):  # noqa: F811
    base, seen = svc
    w = _ask(store, base, monkeypatch, "DELETE", "/late")
    store.x("UPDATE vault_decisions SET ts=ts-7200 WHERE question_id=?", (w["question_id"],))  # asked 2 h ago
    AP.set_pin(store, "482915", by="host")
    AP.answer(store, w["question_id"], "yes", pin="482915")
    assert _ask(store, base, monkeypatch, "DELETE", "/late")["reason"] == "approved by the user and already run"
    assert sum(1 for x in seen if x[:2] == ("DELETE", "/late")) == 1


def test_approve_links_default_to_the_dashboard_port(store, monkeypatch):
    monkeypatch.delenv("COMMONS_DASHBOARD_PORT", raising=False)
    assert AP.url(store, "q1", "127.0.0.1:8765") == "http://127.0.0.1:8766/approve/q1"


def test_a_fresh_hub_is_claimed_by_its_first_visitor(store):
    AP.open_claim(store)  # Commons just started
    assert AP.claim_left(store) > 0
    AP.set_pin(store, "correct horse", client="192.168.50.20")  # no code: the owner opened the dashboard
    i = AP.info(store)
    assert i["set"] and i["from"] == "192.168.50.20" and i["claim_left_s"] == 0
    assert store.q("SELECT 1 FROM notifications WHERE title LIKE '%owner password%'")  # a claim never goes unnoticed
    AP.open_claim(store)  # a restart doesn't reopen a claimed hub
    with pytest.raises(CommonsError, match="current password"):
        AP.set_pin(store, "someone else")
    AP.reset(store)  # forgot it: reset on the hub, then claim again
    AP.set_pin(store, "new password")
    assert AP.check_pin(store, "new password")


def test_code_only_mode_and_closed_windows_need_the_setup_code(store, monkeypatch):
    monkeypatch.setattr(AP, "CLAIM_S", 0)  # COMMONS_CLAIM_MINUTES=0
    AP.open_claim(store)
    assert not AP.claim_left(store)
    with pytest.raises(CommonsError, match="setup code"):
        AP.set_pin(store, "123456")
    AP.set_pin(store, "123456", code=AP.setup_code(store))
    assert AP.has_pin(store)


def test_the_owner_can_lock_the_whole_dashboard(store):
    srv = make_server(store, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path, cookie=""):
        req = urllib.request.Request(url + path, headers={"Cookie": cookie})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def post(path, body, cookie=""):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Commons": "1", "Content-Type": "application/json", "Cookie": cookie})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read()), r.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), ""
    try:
        AP.open_claim(store)
        assert json.loads(get("/api/owner")[1])["claim_left_s"] > 0  # the first-run banner offers the claim
        code, _, cookie = post("/approve/pin", {"set_pin": "my hub password"})
        assert code == 200 and "commons_approver=" in cookie  # claiming signs this browser in
        cookie = cookie.split(";")[0]
        assert post("/api/setup/setting", {"key": "dashboard_lock", "value": True}, cookie)[0] == 200
        code, page = get("/")
        assert code == 200 and "Commons is locked" in page
        assert get("/api/overview")[0] == 401 and post("/api/board/add", {"text": "x"})[0] == 401
        code, page = get("/", cookie)
        assert code == 200 and "Commons is locked" not in page
        assert get("/api/overview", cookie)[0] == 200
        assert "Max-Age=0" in post("/approve/logout", {}, cookie)[2]
    finally:
        srv.shutdown()
