"""Acceptance tests for the vault against a faithful fake of the Ether admin API (header_file bodies and fixed headers).

The fake enforces the real rules: X-Plex-Token auth, X-Ether-Request on every mutating call, application/json on
mutations except the plugin install, which needs application/octet-stream and a non-empty raw body. It runs on
127.0.0.1 only and echoes the token back in its responses so the tests can prove the vault scrubs it.
"""
import base64
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from commons_memory import board as B
from commons_memory import vault as V
from commons_memory.store import CommonsError

SECRET = "plex-owner-tok-7d41c9e0b2"
CSRF = {"error": {"code": "csrf", "message": "request rejected"}}
INSTALL = "/ether/api/plugins/install"


@pytest.fixture()
def ether(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setenv("COMMONS_VAULT_UPLOADS", str(up))
    seen = []  # every request that reached the server: (method, path, headers, raw body)

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _handle(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            seen.append((self.command, self.path, hdrs, raw))
            tok = hdrs.get("x-plex-token")
            if tok != SECRET:
                return self._send(401, {"error": {"code": "unauthorized", "message": "bad token", "echo": tok}})
            if self.command in ("POST", "PATCH", "DELETE"):
                ct = (hdrs.get("content-type") or "").split(";")[0].strip().lower()
                if hdrs.get("x-ether-request") != "1":
                    return self._send(403, CSRF)
                if self.path == INSTALL and self.command == "POST":
                    if ct != "application/octet-stream" or not raw:
                        return self._send(403, CSRF)
                    return self._send(200, {"ok": True, "sha256": hashlib.sha256(raw).hexdigest(), "length": len(raw),
                                            "echo_token": tok})
                if ct != "application/json":
                    return self._send(403, CSRF)
                return self._send(200, {"ok": True, "received": raw.decode("utf-8", "replace"), "echo_token": tok})
            if self.path == "/ether/api/plugins":
                return self._send(200, [{"id": "music", "token_echo": tok}])
            return self._send(404, {"error": {"code": "not_found", "message": "no"}})

        do_GET = do_POST = do_PATCH = do_DELETE = _handle

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen, up
    srv.shutdown()
    srv.server_close()


def _deposit(store, agent, risk="always", name="ether"):
    return V.deposit(store, agent, name, SECRET, "ether", [], auth="header:X-Plex-Token", risk=risk,
                     purpose="owner calls to the Ether console", scope="full owner")


def _hosted(store, agent, base, risk="always"):
    _deposit(store, agent, risk)
    V.set_hosts(store, agent, "ether", [base])


def _install(store, agent, base, name, why="the user asked to install the plugin build"):
    return V.use(store, agent, "ether", "POST", base + INSTALL, why, body_file=name)


def _package() -> bytes:
    return b"\x00\xff" * 64 + os.urandom(320 * 1024) + b"\x00\x00\xff\xff\x00"


# ------------------------------------------------------------------------------------------------ a
def test_a_csrf_403_before_set_headers_and_200_after(store, ether):
    base, seen, _ = ether
    a = store.ensure_agent("depositor")
    _hosted(store, a, base)
    why = "the user asked to change the plugin setting"
    r = V.use(store, a, "ether", "PATCH", base + "/ether/api/plugins/music", why, body='{"on": true}',
              content_type="application/json")
    assert r["verdict"] == "allowed" and r["status"] == 403 and "csrf" in r["body"]
    assert "x-ether-request" not in seen[-1][2]
    V.set_headers(store, a, "ether", ["X-Ether-Request: 1"])
    r = V.use(store, a, "ether", "PATCH", base + "/ether/api/plugins/music", why, body='{"on": true}',
              content_type="application/json")
    assert r["status"] == 200, r
    assert seen[-1][2]["x-ether-request"] == "1" and seen[-1][3] == b'{"on": true}'
    r = V.use(store, a, "ether", "GET", base + "/ether/api/plugins", "the user asked which plugins are installed")
    assert r["status"] == 200 and r["body"].lstrip().startswith("[")


# ------------------------------------------------------------------------------------------------ b, c
def test_b_binary_package_is_installed_byte_for_byte_and_c_secret_never_returned(store, ether):
    base, seen, up = ether
    a = store.ensure_agent("depositor")
    _hosted(store, a, base)
    V.set_headers(store, a, "ether", ["X-Ether-Request: 1"])
    blob = _package()
    assert len(blob) >= 300 * 1024 and b"\x00" in blob and b"\xff" in blob
    (up / "music-24.etherpkg").write_bytes(blob)
    outs = [V.use(store, a, "ether", "PATCH", base + "/ether/api/plugins/music", "the user asked to change the setting",
                  body="{}", content_type="application/json")]
    r = _install(store, a, base, "music-24.etherpkg")
    outs.append(r)
    outs.append(V.use(store, a, "ether", "GET", base + "/ether/api/plugins", "the user asked which plugins exist"))
    assert r["verdict"] == "allowed" and r["status"] == 200, r
    got = json.loads(r["body"])
    assert got["sha256"] == hashlib.sha256(blob).hexdigest() and got["length"] == len(blob)
    assert seen[-2][3] == blob or any(s[3] == blob for s in seen)
    # c: the fake echoed the token in every body; nothing returned to the agent may contain it
    assert all(SECRET in s[2].get("x-plex-token", "") for s in seen)
    for o in outs:
        assert SECRET not in json.dumps(o, default=str), o
    assert "[credential]" in r["body"]
    # the decision log and the question text must not hold it either
    for row in store.q("SELECT * FROM vault_decisions"):
        assert SECRET not in json.dumps({k: (v if not isinstance(v, bytes) else "") for k, v in dict(row).items()},
                                        default=str)


# ------------------------------------------------------------------------------------------------ d
def _refused_without_a_request(store, a, base, seen, name, problems):
    n = len(seen)
    box = {}

    def run():
        try:
            box["r"] = _install(store, a, base, name)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():
        problems.append(f"body_file={name!r} hung")
    elif not isinstance(box.get("e"), CommonsError):
        problems.append(f"body_file={name!r} was not refused with CommonsError: {box}")
    if len(seen) != n:
        problems.append(f"body_file={name!r} caused a request")


def test_d_hostile_body_file_values_are_refused_and_send_nothing(store, ether):
    base, seen, up = ether
    a = store.ensure_agent("depositor")
    _hosted(store, a, base)
    V.set_headers(store, a, "ether", ["X-Ether-Request: 1"])
    outside = up.parent / "outside-secret.bin"
    outside.write_bytes(b"hub secret bytes")
    os.symlink(outside, up / "link.etherpkg")
    os.mkdir(up / "adir")
    os.mkfifo(up / "pipe.etherpkg")
    (up / "ok.etherpkg").write_bytes(b"fine")
    problems = []
    for name in ("../outside-secret.bin", str(outside), "sub/ok.etherpkg", "./ok.etherpkg", "link.etherpkg", "adir",
                 "pipe.etherpkg", "missing.etherpkg", "..", ".", "ok.etherpkg\x00", "ok.etherpkg\n", ".hidden"):
        _refused_without_a_request(store, a, base, seen, name, problems)
    assert not problems, "\n".join(problems)
    assert not seen
    assert _install(store, a, base, "ok.etherpkg")["status"] == 200  # control: a good name still works
    assert len(seen) == 1


def test_d_empty_file_is_refused_or_reported(store, ether):
    """Judgement: an empty package can only be a mistake (the service refuses it) and an approval of 0 bytes is
    meaningless, so it should be refused before any request."""
    base, seen, up = ether
    a = store.ensure_agent("depositor")
    _hosted(store, a, base)
    V.set_headers(store, a, "ether", ["X-Ether-Request: 1"])
    (up / "empty.etherpkg").write_bytes(b"")
    try:
        r = _install(store, a, base, "empty.etherpkg")
    except CommonsError:
        r = None
    print("EMPTY FILE OUTCOME:", "refused" if r is None else r, "requests:", len(seen))
    assert r is None and not seen, f"empty file was sent: {r}, requests={len(seen)}"


# ------------------------------------------------------------------------------------------------ e
def test_e_file_install_waits_for_user_and_approval_covers_exact_bytes(store, ether, monkeypatch):
    base, seen, up = ether
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    a = store.ensure_agent("depositor")
    _hosted(store, a, base, risk="normal")
    V.set_headers(store, a, "ether", ["X-Ether-Request: 1"])
    good = _package()
    f = up / "plugin.etherpkg"
    f.write_bytes(good)
    w = _install(store, a, base, "plugin.etherpkg")
    assert w["verdict"] == "waiting_for_user" and not seen
    q = B.get(store, w["question_id"])
    assert "plugin.etherpkg" in q["text"] and hashlib.sha256(good).hexdigest() in q["text"]
    B.answer(store, w["question_id"], "yes")
    assert len(seen) == 1 and seen[0][3] == good and seen[0][2]["content-type"] == "application/octet-stream"
    # identical bytes again: no second request
    again = _install(store, a, base, "plugin.etherpkg")
    assert len(seen) == 1, f"a second request was sent: {again}"
    # swapped after asking: ask with bytes X, replace with Y, approve -> nothing sent
    f.write_bytes(os.urandom(5000) + b"\x00\xff")
    w2 = _install(store, a, base, "plugin.etherpkg")
    assert w2["verdict"] == "waiting_for_user" and len(seen) == 1
    f.write_bytes(os.urandom(5000) + b"\xff\x00")
    B.answer(store, w2["question_id"], "yes")
    assert len(seen) == 1, "swapped bytes were sent after approval of other bytes"
    # and asking again does not let it through unasked
    w3 = _install(store, a, base, "plugin.etherpkg")
    assert w3["verdict"] == "waiting_for_user" and len(seen) == 1


def test_e_approval_sends_exactly_once_even_if_answered_twice_or_denied(store, ether, monkeypatch):
    base, seen, up = ether
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    a = store.ensure_agent("depositor")
    _hosted(store, a, base, risk="normal")
    V.set_headers(store, a, "ether", ["X-Ether-Request: 1"])
    (up / "p.etherpkg").write_bytes(b"\x00one\xff")
    w = _install(store, a, base, "p.etherpkg")
    B.answer(store, w["question_id"], "no")
    assert not seen
    (up / "q.etherpkg").write_bytes(b"\x00two\xff")
    w = _install(store, a, base, "q.etherpkg")
    B.answer(store, w["question_id"], "yes")
    assert len(seen) == 1 and seen[0][3] == b"\x00two\xff"


# ------------------------------------------------------------------------------------------------ f
def test_f_set_headers_authorisation_and_validation(store, ether):
    base, seen, _ = ether
    a, other = store.ensure_agent("depositor"), store.ensure_agent("grok")
    _hosted(store, a, base)
    with pytest.raises(CommonsError):
        V.set_headers(store, other, "ether", ["X-Ether-Request: 1"])
    with pytest.raises(CommonsError):
        V.set_headers(store, a, "no-such-key", ["X-Ether-Request: 1"])
    for bad in (["X-A: 1\r\nX-B: 2"], ["X-A: 1\nX-B: 2"], ["Authorization: x"], ["authorization: x"],
                ["X-Plex-Token: other"], ["x-plex-token: other"], ["Host: evil.example"], ["Content-Length: 5"],
                ["X-Ok: 1", "Authorization: x"], ["no colon"], ["Bad Name: 1"]):
        with pytest.raises(CommonsError):
            V.set_headers(store, a, "ether", bad)
    # a refused call changed nothing, and the non-depositor changed nothing
    assert json.loads(store.q("SELECT auth FROM vault_creds WHERE name='ether'")[0]["auth"]).get("extra_headers") is None
    assert V.set_headers(store, a, "ether", ["X-Ether-Request: 1"])["extra_headers"] == {"X-Ether-Request": "1"}
    assert V._value(store, "ether") == SECRET
    with pytest.raises(CommonsError):
        V.set_headers(store, a, "ether", ["X-A: 1\r\nX-B: 2"])
    assert json.loads(store.q("SELECT auth FROM vault_creds WHERE name='ether'")[0]["auth"])["extra_headers"] == \
        {"X-Ether-Request": "1"}


def test_f_trailing_crlf_in_a_header_value_is_refused(store, ether):
    """The criterion says values containing CR/LF are refused; a trailing CRLF is not an injection, but it is still a
    value containing CRLF."""
    a = store.ensure_agent("depositor")
    _hosted(store, a, ether[0])
    with pytest.raises(CommonsError):
        V.set_headers(store, a, "ether", ["X-A: v\r\n"])


# ------------------------------------------------------------------------------------------------ g
def test_g_plain_json_body_is_unchanged_and_get_has_no_octet_stream(store, ether):
    base, seen, up = ether
    a = store.ensure_agent("depositor")
    _hosted(store, a, base)
    V.set_headers(store, a, "ether", ["X-Ether-Request: 1"])
    body = '{"name": "café ☃", "n": [1, 2, 3]}'
    r = V.use(store, a, "ether", "PATCH", base + "/ether/api/plugins/music", "the user asked to change the setting",
              body=body, content_type="application/json")
    assert r["status"] == 200
    m, p, h, raw = seen[-1]
    assert raw == body.encode() and h["content-type"] == "application/json"
    r = V.use(store, a, "ether", "GET", base + "/ether/api/plugins", "the user asked which plugins exist")
    assert r["status"] == 200
    m, p, h, raw = seen[-1]
    assert m == "GET" and raw == b"" and "octet-stream" not in h.get("content-type", "")
    # a text body without a content type must not become octet-stream either
    V.use(store, a, "ether", "POST", base + "/ether/api/plugins/music", "the user asked to change the setting", body="{}")
    assert "octet-stream" not in seen[-1][2].get("content-type", "")
