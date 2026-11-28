"""bw:// references, Bitwarden items linked into the vault, and `commons vault export --to bitwarden`, against a fake
`bw` executable (a Python script keeping items and folders in a JSON file and logging every argv)."""
import base64
import json
import os
import stat
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from commons_memory import bitwarden as BW
from commons_memory import vault as V
from commons_memory.secrets import Broker
from commons_memory.store import CommonsError

SESSION = "sess-KEY-0a1b2c3d4e5f"
PASSWORD = "master-pw-never-in-argv"
PW = "pw-9f3a71c2-secret"
ITM1, ITM2, ITM3, ITM404 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"
FOLDER = "fld-commons"
OTHER = "fld-other"

FAKE = textwrap.dedent('''\
    #!%(py)s
    import base64, json, os, re, sys, time, uuid
    st_path, log = os.environ["FAKE_BW_STATE"], os.environ["FAKE_BW_LOG"]
    argv = [a for a in sys.argv[1:] if a != "--nointeraction"]
    open(log, "a").write(json.dumps(sys.argv[1:]) + "\\n")
    st = json.load(open(st_path))

    def save():
        json.dump(st, open(st_path, "w"))

    def fail(msg, code=1):
        sys.stderr.write(msg + "\\n")
        sys.exit(code)
    cmd = argv[0]
    st.setdefault("envs", []).append({k: v for k, v in os.environ.items() if k.startswith("BW_") and k != "BW_SESSION"})
    if cmd == "unlock":
        time.sleep(st.get("unlock_delay", 0))
        pw = open(argv[argv.index("--passwordfile") + 1]).read().strip()
        if pw != st["password"]:
            fail("Invalid master password.")
        st["unlocks"] = st.get("unlocks", 0) + 1
        if not st.get("bad_unlock"):
            st["session"] = st["session_next"]
        save()
        print(st["session_next"], end="")
        sys.exit(0)
    if st.get("logged_out"):
        fail("You are not logged in.")
    if os.environ.get("BW_SESSION") != st["session"]:
        fail("Vault is locked.")
    st.setdefault("seen_sessions", []).append(os.environ["BW_SESSION"])
    if st.get("explode") == cmd:
        fail("boom " + st["leak"])
    stdin = lambda: json.loads(base64.b64decode(sys.stdin.read()))
    if cmd == "sync":
        if st.get("sync_fail"):
            fail("sync exploded")
    elif cmd == "list" and argv[1] == "folders":
        print(json.dumps([f for f in st["folders"] if argv[argv.index("--search") + 1] in f["name"]]))
    elif cmd == "list" and argv[1] == "items":
        fid = argv[argv.index("--folderid") + 1]
        print(st["list_items_raw"] if "list_items_raw" in st else json.dumps([i for i in st["items"] if i.get("folderId") == fid]))
    elif cmd == "get" and argv[1] == "item":
        arg = argv[2]
        if re.fullmatch(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", arg):
            m = [i for i in st["items"] if i["id"].lower() == arg.lower()]
        else:  # the real CLI takes anything else as a name search over ALL folders
            m = [i for i in st["items"] if arg.lower() in i["name"].lower()]
        if len(m) > 1:
            fail("More than one result was found. Try getting a specific object by `id` instead.")
        if not m:
            fail("Not found.")
        print(json.dumps(m[0] | ({"id": "99999999-9999-4999-8999-999999999999"} if st.get("wrong_id") else {})))
    elif cmd == "get" and argv[1] == "totp":
        if st.get("no_premium"):
            fail("Premium status is required to use this feature.")
        print("123456")
    elif cmd == "create" and argv[1] == "folder":
        f = stdin()
        st["folder_creates"] = st.get("folder_creates", 0) + 1
        if st.get("bad_folder_create"):
            print("this is not json")
            save()
            sys.exit(0)
        f["id"] = "fld-%%d" %% (len(st["folders"]) + 1)
        st["folders"].append(f)
        print(json.dumps(f))
    elif cmd == "create" and argv[1] == "item":
        i = stdin()
        if i["name"] in st.get("fail_names", []):
            fail("write refused " + i["login"]["password"])
        i["id"] = str(uuid.uuid4())
        st["items"].append(i)
        st["writes"] = st.get("writes", 0) + 1
        print(json.dumps(i))
    elif cmd == "edit" and argv[1] == "item":
        i = stdin()
        if i["name"] in st.get("fail_names", []):
            fail("write refused " + i["login"]["password"])
        st["items"] = [i if x["id"] == argv[2] else x for x in st["items"]]
        st["writes"] = st.get("writes", 0) + 1
        print(json.dumps(i))
    else:
        fail("unknown command")
    save()
''')


class Fake:
    def __init__(self, tmp, monkeypatch):
        self.bin, self.state, self.log = tmp / "bw", tmp / "state.json", tmp / "argv.log"
        self.bin.write_text(FAKE % {"py": sys.executable})
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR)
        self.log.write_text("")
        self.pwfile = tmp / "pw"
        self.pwfile.write_text(PASSWORD + "\n")
        self.save({"session": SESSION, "session_next": SESSION, "password": PASSWORD, "leak": "LEAKED-" + PW,
                   "folders": [{"id": FOLDER, "name": "Commons"}, {"id": OTHER, "name": "Private"}], "items": [
                       {"id": ITM1, "name": "svc", "folderId": FOLDER, "notes": "the notes",
                        "login": {"username": "ann", "password": PW, "uris": [{"uri": "https://svc.example/"}]},
                        "fields": [{"name": "api token", "value": "tok-custom-77", "type": 1}]},
                       {"id": ITM2, "name": "bank", "folderId": OTHER, "login": {"password": "bank-pw-1234"}, "fields": []},
                       {"id": ITM3, "name": "loose", "folderId": None, "login": {"password": "loose-pw-1"}, "fields": []}]})
        for k, v in (("COMMONS_BW_BIN", str(self.bin)), ("FAKE_BW_STATE", str(self.state)), ("FAKE_BW_LOG", str(self.log)),
                     ("BW_SESSION", SESSION), ("COMMONS_BW_FOLDER", "Commons"), ("COMMONS_BW_WRITE_PAUSE_S", "0")):
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("COMMONS_BW_PASSWORD_FILE", raising=False)
        monkeypatch.delenv("COMMONS_BW_SYNC_S", raising=False)
        BW.reset()

    def load(self):
        return json.loads(self.state.read_text())

    def save(self, st):
        self.state.write_text(json.dumps(st))

    def edit(self, **kw):
        st = self.load()
        st.update(kw)
        self.save(st)

    def argv(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def count(self, *head):
        return sum(1 for a in self.argv() if a[:len(head)] == list(head))


@pytest.fixture()
def bw(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    f = Fake(tmp_path, monkeypatch)
    yield f
    BW.reset()


def test_every_field_kind_resolves(bw):
    assert BW.resolve(f"bw://{ITM1}/password") == PW
    assert BW.resolve(f"bw://{ITM1}") == PW                                  # password is the default
    assert BW.resolve(f"bw://{ITM1}/username") == "ann"
    assert BW.resolve(f"bw://{ITM1}/notes") == "the notes"
    assert BW.resolve(f"bw://{ITM1}/uri") == "https://svc.example/"
    assert BW.resolve(f"bw://{ITM1}/totp") == "123456"
    assert BW.resolve(f"bw://{ITM1}/api%20token") == "tok-custom-77"         # a custom field, percent-encoded name
    with pytest.raises(CommonsError, match="no field"):
        BW.resolve(f"bw://{ITM1}/nonexistent")
    with pytest.raises(CommonsError, match="Bitwarden ref"):
        BW.resolve("bw://")


def test_bitwarden_is_not_a_broker_scheme(bw, store):
    """An agent can register any reference and lease it: no scheme may reach Bitwarden through the broker."""
    from commons_memory.secrets import REF_RE
    ref = f"bw://{ITM1}/password"
    assert not REF_RE.match(ref)
    br = Broker(store)
    assert "bw" not in br.backends
    a = store.ensure_agent("muse")
    with pytest.raises(CommonsError, match="ref must look like"):
        br.register(ref, "svc key", agent_id=a)
    store.x("INSERT INTO secret_refs VALUES(?,?,?,?,?,?,?)", (ref, "forced in", None, "[]", 300, a, 0))  # even a row put in by hand
    lease = br.request(ref, a)["lease_id"]
    with pytest.raises(CommonsError, match="no backend for bw://"):
        br.resolve(lease, a)
    with pytest.raises(CommonsError, match="no backend for bw://"):
        br.resolve_internal(ref, "test")
    assert bw.argv() == []                                                  # bw was never run


def test_scope_fails_closed(bw, monkeypatch):
    for ref in (f"bw://{ITM2}/password", f"bw://{ITM3}/password"):             # another folder; no folder at all
        with pytest.raises(CommonsError, match="not found, or outside") as e:
            BW.resolve(ref)
        assert "bank-pw" not in str(e.value) and "loose-pw" not in str(e.value)
    with pytest.raises(CommonsError) as missing:                            # same words for an id that does not exist
        BW.resolve(f"bw://{ITM404}/password")
    with pytest.raises(CommonsError) as outside:
        BW.resolve(f"bw://{ITM2}/password")
    assert str(missing.value) == str(outside.value)
    monkeypatch.setenv("COMMONS_BW_FOLDER", "No such folder")
    with pytest.raises(CommonsError, match="not found, or outside"):
        BW.resolve(f"bw://{ITM1}/password")


def test_unset_folder_refuses_everything_and_runs_nothing(bw, monkeypatch):
    monkeypatch.delenv("COMMONS_BW_FOLDER")
    with pytest.raises(CommonsError, match="COMMONS_BW_FOLDER is not set"):
        BW.resolve(f"bw://{ITM1}/password")
    assert bw.argv() == []


def test_star_allows_the_whole_vault(bw, monkeypatch):
    monkeypatch.setenv("COMMONS_BW_FOLDER", "*")
    assert BW.resolve(f"bw://{ITM2}/password") == "bank-pw-1234"
    assert BW.resolve(f"bw://{ITM3}/password") == "loose-pw-1"


def test_missing_binary(bw, monkeypatch):
    monkeypatch.setenv("COMMONS_BW_BIN", str(bw.bin) + "-nope")
    with pytest.raises(CommonsError, match="not installed"):
        BW.resolve(f"bw://{ITM1}/password")


def test_no_session_and_no_password_file(bw, monkeypatch):
    monkeypatch.delenv("BW_SESSION")
    with pytest.raises(CommonsError, match="BW_SESSION.*COMMONS_BW_PASSWORD_FILE"):
        BW.resolve(f"bw://{ITM1}/password")


def test_locked_and_failing_cli_leak_nothing(bw, monkeypatch):
    monkeypatch.setenv("BW_SESSION", "stale-session")
    with pytest.raises(CommonsError, match="locked") as e:
        BW.resolve(f"bw://{ITM1}/password")
    assert "stale-session" not in str(e.value)
    monkeypatch.setenv("BW_SESSION", SESSION)
    BW.reset()                                                              # the failed sync above opened a no-retry window
    bw.edit(explode="get")                                                 # stderr carries a secret: it must not surface
    with pytest.raises(CommonsError) as e:
        BW.resolve(f"bw://{ITM1}/password")
    assert PW not in str(e.value) and "LEAKED" not in str(e.value) and "failed (exit 1)" in str(e.value)


def test_password_file_unlocks_caches_and_relocks_once(bw, monkeypatch):
    monkeypatch.delenv("BW_SESSION")
    monkeypatch.setenv("COMMONS_BW_PASSWORD_FILE", str(bw.pwfile))
    assert BW.resolve(f"bw://{ITM1}/password") == PW
    assert BW.resolve(f"bw://{ITM1}/username") == "ann"
    assert bw.count("unlock") == 1                                         # cached in memory
    bw.edit(session="rotated", session_next="rotated")                     # the vault locked itself since
    assert BW.resolve(f"bw://{ITM1}/password") == PW
    assert bw.count("unlock") == 2                                         # unlocked again, once
    assert bw.load()["seen_sessions"][-1] == "rotated"
    bw.edit(session="rotated-again", session_next="other", bad_unlock=True)                 # can never match: no endless retry
    with pytest.raises(CommonsError, match="locked"):
        BW.resolve(f"bw://{ITM1}/password")
    assert bw.count("unlock") == 3


def test_wrong_master_password_is_a_plain_error(bw, monkeypatch):
    monkeypatch.delenv("BW_SESSION")
    monkeypatch.setenv("COMMONS_BW_PASSWORD_FILE", str(bw.pwfile))
    bw.edit(password="another")
    with pytest.raises(CommonsError, match="unlock failed") as e:
        BW.resolve(f"bw://{ITM1}/password")
    assert PASSWORD not in str(e.value) and "Invalid" not in str(e.value)


def test_session_and_values_never_on_argv(bw, monkeypatch):
    BW.resolve(f"bw://{ITM1}/password")
    monkeypatch.delenv("BW_SESSION")
    monkeypatch.setenv("COMMONS_BW_PASSWORD_FILE", str(bw.pwfile))
    BW.reset()
    BW.resolve(f"bw://{ITM1}/totp")
    log = bw.log.read_text()
    assert SESSION not in log and PW not in log and PASSWORD not in log
    assert "--nointeraction" in log and all(a[-1] == "--nointeraction" for a in bw.argv())
    assert SESSION in bw.load()["seen_sessions"]                           # it reached the child through the environment


def test_sync_is_throttled(bw, monkeypatch):
    for _ in range(3):
        BW.resolve(f"bw://{ITM1}/password")
    assert bw.count("sync") == 1                                           # default 300 s
    monkeypatch.setenv("COMMONS_BW_SYNC_S", "0")
    BW.reset()
    bw.log.write_text("")
    BW.resolve(f"bw://{ITM1}/password")
    assert bw.count("sync") == 0                                           # 0 = never
    monkeypatch.setenv("COMMONS_BW_SYNC_S", "0.0001")
    BW.reset()
    BW.resolve(f"bw://{ITM1}/password")
    BW.resolve(f"bw://{ITM1}/password")
    assert bw.count("sync") == 2


# ---------------------------------------------------------------------------------------------- linked credentials
@pytest.fixture()
def svc():
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("X-Svc-Token"))
            body = json.dumps({"echo": self.headers.get("X-Svc-Token")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def _link(store, base, ref=f"bw://{ITM1}/password", name="svc"):
    return V.link(store, name, ref, "svc", [base], {"header": "X-Svc-Token"})


def test_a_linked_row_resolves_and_works_through_use_credential(bw, store, svc, monkeypatch):
    base, seen = svc
    from commons_memory import typesafe
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    monkeypatch.setattr(typesafe, "noul", lambda state, qs, timeout=20, key=None: {k: 0.95 for k in qs})
    _link(store, base)
    assert store.q("SELECT secret, ref FROM vault_creds")[0]["secret"] is None
    assert V._value(store, "svc") == PW
    r = V.use(store, store.ensure_agent("muse"), "svc", "GET", base + "/thing", "the user asked for the thing on svc")
    assert r["verdict"] == "allowed" and r["status"] == 200
    assert seen == [PW] and PW not in json.dumps(r)                        # sent to the service, scrubbed from the answer
    row = [x for x in V.listing(store) if x["credential"] == "svc"][0]
    assert row["source"] == "bitwarden" and row["stored"] is False
    assert PW not in json.dumps(V.listing(store)) and ITM1 not in json.dumps(V.listing(store))
    audit = json.dumps([dict(x) for x in store.q("SELECT * FROM audit")])
    assert PW not in audit and "vault.link" in audit


def test_link_checks_the_item_is_readable_now(bw, store, svc, monkeypatch):
    base, _ = svc
    with pytest.raises(CommonsError, match="not found, or outside"):        # another folder: linking does not bypass the scope
        _link(store, base, f"bw://{ITM2}/password", "bank")
    with pytest.raises(CommonsError, match="not found, or outside"):
        _link(store, base, f"bw://{ITM404}/password", "gone")
    with pytest.raises(CommonsError, match="no field"):
        _link(store, base, f"bw://{ITM1}/nonexistent", "nofield")
    with pytest.raises(CommonsError, match="Bitwarden ref"):
        _link(store, base, "https://x", "bad")
    with pytest.raises(CommonsError, match="Bitwarden ref"):                 # a name is a search in bw, never an id
        _link(store, base, "bw://svc/password", "bad")
    with pytest.raises(CommonsError, match="base URL"):
        V.link(store, "nohost", f"bw://{ITM1}/password", "svc", [], {"bearer": True})
    assert not store.q("SELECT 1 FROM vault_creds")
    monkeypatch.delenv("COMMONS_BW_FOLDER")
    with pytest.raises(CommonsError, match="COMMONS_BW_FOLDER is not set"):
        _link(store, base)
    monkeypatch.setenv("COMMONS_BW_FOLDER", "Commons")
    monkeypatch.setenv("BW_SESSION", "stale")
    with pytest.raises(CommonsError, match="locked"):
        _link(store, base)
    assert not store.q("SELECT 1 FROM vault_creds")


def test_relinking_to_another_item_drops_grants_and_the_audit_has_no_item_id(bw, store, svc):
    base, _ = svc
    other = "55555555-5555-4555-8555-555555555555"
    bw.edit(items=bw.load()["items"] + [{"id": other, "name": "second", "folderId": FOLDER, "login": {"password": "second-pw"},
                                         "fields": []}])
    _link(store, base)
    store.x("INSERT INTO vault_grants(id,agent_id,cred,scope,method,url,decision_id,expires_at) VALUES('g1','a','svc','read','GET','u','d',9e12)")
    _link(store, base)                                                       # the same item again keeps what was approved
    assert store.q("SELECT 1 FROM vault_grants")
    _link(store, base, f"bw://{other}/password")
    assert not store.q("SELECT 1 FROM vault_grants WHERE cred='svc'")
    assert V._value(store, "svc") == "second-pw"
    audit = json.dumps([dict(x) for x in store.q("SELECT * FROM audit WHERE action='vault.link'")])
    assert ITM1 not in audit and other not in audit and "field" in audit


def test_agents_cannot_set_or_overwrite_a_ref(bw, store, svc, monkeypatch):
    monkeypatch.setenv("COMMONS_TOOLS", "all")
    base, _ = svc
    _link(store, base)
    a = store.ensure_agent("muse")
    with pytest.raises(CommonsError, match="already in the vault"):
        V.deposit(store, a, "svc", "hunter2-new-value", "svc", [base], purpose="overwrite attempt!!", scope="all")
    with pytest.raises(CommonsError, match="already in the vault"):
        V.generate(store, a, "svc", "svc", base, purpose="take over the name")
    with pytest.raises(CommonsError, match="only the agent that deposited"):
        V.set_hosts(store, a, "svc", ["http://evil.example"])
    with pytest.raises(CommonsError, match="only the agent that deposited"):
        V.set_headers(store, a, "svc", ["X-A: b"])
    with pytest.raises(CommonsError, match="linked to Bitwarden"):          # the owner path (CLI set, dashboard) refuses too
        V.add(store, "svc", "a-value", "svc", [base], {"bearer": True})
    r = store.q("SELECT secret, ref, deposited_by FROM vault_creds")[0]
    assert r["secret"] is None and r["ref"] == f"bw://{ITM1}/password" and r["deposited_by"] is None
    # a new name deposited by an agent never gets a ref, and no agent tool takes one
    V.deposit(store, a, "mine", "agent-value-12345", "svc", [base], purpose="an agent's own key", scope="read-only")
    assert store.q("SELECT ref FROM vault_creds WHERE name='mine'")[0]["ref"] is None
    from commons_memory.server import build_server
    tools = {t.name: t for t in build_server(store)._tool_manager.list_tools()}
    for name in ("vault_deposit", "vault_generate", "vault_note", "vault_hosts", "vault_headers", "vault_reveal"):
        assert "ref" not in tools[name].parameters["properties"]
    assert not [n for n in tools if "link" in n and "vault" in n]


def test_a_dashboard_write_cannot_touch_a_linked_row(bw, store, svc):
    base, _ = svc
    _link(store, base)
    with pytest.raises(CommonsError, match="linked to Bitwarden"):          # /api/vault/set goes through V.add
        V.add(store, "svc", "x-value", "svc", [base], {"bearer": True})
    assert store.q("SELECT ref FROM vault_creds")[0]["ref"]


def test_reveal_refuses_a_linked_row(bw, store, svc):
    base, _ = svc
    _link(store, base)
    store.x("UPDATE vault_creds SET kind='login' WHERE name='svc'")         # even if it were marked a login
    with pytest.raises(CommonsError, match="never revealed"):
        V.reveal(store, store.ensure_agent("muse"), "svc", "I need to sign in to the service again")


def test_the_broker_never_leases_a_linked_row(bw, store, svc):
    base, _ = svc
    _link(store, base)
    a = store.ensure_agent("muse")
    br = Broker(store)
    br.register("commons://svc", "x", agent_id=a)
    lease = br.request("commons://svc", a)["lease_id"]
    with pytest.raises(CommonsError, match="through use_credential only"):
        br.resolve(lease, a)


def test_the_cli_links_and_lists(bw, tmp_path, svc, capsys):
    from commons_memory.cli import main
    base, _ = svc
    db = str(tmp_path / "cli.db")
    assert main(["--db", db, "vault", "link", "svc", "--ref", f"bw://{ITM1}/password", "--base-url", base,
                 "--auth", "header:X-Svc-Token"]) == 0
    assert main(["--db", db, "vault", "list"]) == 0
    out = capsys.readouterr().out
    assert '"source": "bitwarden"' in out and PW not in out
    assert main(["--db", db, "vault", "link", "svc2", "--base-url", base]) == 2      # no --ref


# ---------------------------------------------------------------------------------------------- export
def _store_creds(store, base="http://svc.example"):
    V.add(store, "alpha", "alpha-secret-AAAA1111", "svc", [base], {"bearer": True, "extra_headers": {"X-Csrf": "hdr-secret-ZZZ"}},
          "alpha service key")
    V.add(store, "beta", "beta-secret-BBBB2222", "svc", [base], {"basic": "bob"})
    store.x("UPDATE vault_creds SET username='bob', kind='login', scope='read-only', purpose='beta login' WHERE name='beta'")


def _names(r, k):
    return sorted(r[k])


def test_export_creates_then_is_idempotent_then_updates(bw, store):
    _store_creds(store)
    r = BW.export(store, "Commons backup", pause=0)
    assert _names(r, "created") == ["alpha", "beta"] and r["failed"] == [] and r["counts"]["created"] == 2
    st = bw.load()
    fid = [f["id"] for f in st["folders"] if f["name"] == "Commons backup"][0]
    items = {i["name"]: i for i in st["items"] if i.get("folderId") == fid}
    a = items["alpha"]
    assert a["type"] == 1 and a["login"]["password"] == "alpha-secret-AAAA1111"
    assert a["login"]["uris"] == [{"match": None, "uri": "http://svc.example/"}]
    fields = {f["name"]: f for f in a["fields"]}
    assert fields["commons_name"]["value"] == "alpha" and fields["commons_name"]["type"] == 0
    assert "hdr-secret-ZZZ" not in fields["commons_auth"]["value"]                  # header values are not in text
    assert fields["commons_extra_headers"]["type"] == 1 and "hdr-secret-ZZZ" in fields["commons_extra_headers"]["value"]
    assert json.loads(fields["commons_auth"]["value"]) == {"bearer": True}
    assert items["beta"]["login"]["username"] == "bob" and items["beta"]["notes"] == "beta login"
    assert {"commons_kind", "commons_scope"} <= {f["name"] for f in items["beta"]["fields"]}
    again = BW.export(store, "Commons backup", pause=0)
    assert _names(again, "unchanged") == ["alpha", "beta"] and again["counts"]["created"] == 0
    assert bw.load()["writes"] == 2
    V.add(store, "alpha", "alpha-rotated-CCCC3333", "svc", ["http://svc.example"], {"bearer": True})
    upd = BW.export(store, "Commons backup", pause=0)
    assert upd["updated"] == ["alpha"] and upd["unchanged"] == ["beta"]
    st = bw.load()
    a2 = [i for i in st["items"] if i["name"] == "alpha"]
    assert len(a2) == 1 and a2[0]["id"] == a["id"] and a2[0]["folderId"] == fid       # same item, edited in place
    assert a2[0]["login"]["password"] == "alpha-rotated-CCCC3333"
    assert "commons_extra_headers" not in {f["name"] for f in a2[0]["fields"]}


def test_export_keeps_what_the_owner_added_and_never_touches_other_items(bw, store):
    _store_creds(store)
    BW.export(store, "Commons backup", pause=0)
    st = bw.load()
    fid = [f["id"] for f in st["folders"] if f["name"] == "Commons backup"][0]
    for i in st["items"]:
        if i["name"] == "alpha":
            i["fields"].append({"name": "my own note", "value": "keep me", "type": 0})
            i["login"]["totp"] = "otpauth://keep"
    st["items"] += [{"id": "dup-1", "name": "alpha", "folderId": fid, "login": {"password": "someone-elses"}, "fields": []},
                    {"id": "dup-2", "name": "beta", "folderId": fid, "login": {"password": "x"}, "fields": []}]
    bw.save(st)
    assert BW.export(store, "Commons backup", pause=0)["unchanged"] == ["alpha", "beta"]
    V.add(store, "alpha", "alpha-rotated-CCCC3333", "svc", ["http://svc.example"], {"bearer": True})
    assert BW.export(store, "Commons backup", pause=0)["updated"] == ["alpha"]
    after = {i["id"]: i for i in bw.load()["items"]}
    assert after["dup-1"]["login"]["password"] == "someone-elses" and after["dup-2"]["login"]["password"] == "x"
    mine = [i for i in after.values() if i["name"] == "alpha" and any(f["name"] == "commons_name" for f in i["fields"])][0]
    assert {"name": "my own note", "value": "keep me", "type": 0} in mine["fields"] and mine["login"]["totp"] == "otpauth://keep"
    assert len(after) == len(bw.load()["items"])                                      # nothing was deleted


def test_export_skips_linked_and_oversize(bw, store, svc):
    base, _ = svc
    _store_creds(store)
    _link(store, base)
    V.add(store, "huge", "x" * 3001, "svc", [base], {"bearer": True})
    V.add(store, "edge", "y" * 3000, "svc", [base], {"bearer": True})
    r = BW.export(store, "Commons backup", pause=0)
    assert r["skipped_linked"] == ["svc"] and r["skipped_too_large"] == ["huge"]
    assert "huge" not in r["created"] and "edge" in r["created"]
    assert not [i for i in bw.load()["items"] if i["name"] == "huge" or (i["name"] == "svc" and i["id"] != ITM1)]


def test_one_failed_write_does_not_stop_the_run(bw, store):
    _store_creds(store)
    bw.edit(fail_names=["alpha"])
    r = BW.export(store, "Commons backup", pause=0)
    assert r["failed"] == ["alpha"] and r["created"] == ["beta"]
    assert "alpha-secret-AAAA1111" not in json.dumps(r)                               # bw's stderr carried it: not repeated
    assert r["errors"]["alpha"] == "bw create failed (exit 1)"


def test_dry_run_writes_nothing(bw, store):
    _store_creds(store)
    r = BW.export(store, "Commons backup", dry_run=True, pause=0)
    st = bw.load()
    assert _names(r, "created") == ["alpha", "beta"] and r["dry_run"] is True
    assert not st.get("writes") and [f["name"] for f in st["folders"]] == ["Commons", "Private"]   # no folder either
    assert bw.count("create") == 0 and bw.count("edit") == 0
    assert not store.q("SELECT 1 FROM audit WHERE action='vault.export'")
    BW.export(store, "Commons backup", pause=0)
    V.add(store, "alpha", "alpha-rotated-CCCC3333", "svc", ["http://svc.example"], {"bearer": True})
    before = bw.load()["items"]
    r = BW.export(store, "Commons backup", dry_run=True, pause=0)
    assert r["updated"] == ["alpha"] and bw.load()["items"] == before


def test_summary_audit_and_argv_hold_no_secret_value(bw, store):
    _store_creds(store)
    r = BW.export(store, "Commons backup", pause=0)
    blob = json.dumps(r) + json.dumps([dict(x) for x in store.q("SELECT * FROM audit")]) + bw.log.read_text()
    for v in ("alpha-secret-AAAA1111", "beta-secret-BBBB2222", "hdr-secret-ZZZ", SESSION):
        assert v not in blob
    row = [dict(x) for x in store.q("SELECT * FROM audit WHERE action='vault.export'")][0]
    d = json.loads(row["detail"])
    assert row["target"] == "Commons backup" and d["created"] == 2 and d["folder"] == "Commons backup"
    for a in bw.argv():                                                              # item JSON goes on stdin, never argv
        assert not any(len(x) > 60 for x in a)


def test_export_keeps_backups_apart_from_linked_items(bw, store, monkeypatch):
    _store_creds(store)
    with pytest.raises(CommonsError, match="keep the backup and the linked items apart"):
        BW.export(store, "Commons", pause=0)
    monkeypatch.setenv("COMMONS_BW_FOLDER", "*")
    with pytest.raises(CommonsError, match="apart"):
        BW.export(store, "Commons backup", pause=0)
    assert bw.argv() == []


def test_the_export_cli_exit_codes(bw, tmp_path, capsys, monkeypatch):
    from commons_memory.cli import main
    from commons_memory.store import Store
    db = str(tmp_path / "cli.db")
    s = Store(db)
    _store_creds(s)
    s.db.close()
    assert main(["--db", db, "vault", "export", "--to", "bitwarden", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["created"] == 2
    bw.edit(fail_names=["beta"])
    assert main(["--db", db, "vault", "export", "--to", "bitwarden"]) == 1             # a failure: non-zero
    out = json.loads(capsys.readouterr().out)
    assert out["failed"] == ["beta"] and out["created"] == ["alpha"]
    assert main(["--db", db, "vault", "export"]) == 2                                  # --to is required


# ---------------------------------------------------------------------------------------------- review round 1
def test_a_name_is_never_an_id(bw):
    for ref in ("bw://svc/password", "bw://-h/password", f"bw://{ITM1[:-1]}/password"):
        with pytest.raises(CommonsError, match="Bitwarden ref"):
            BW.resolve(ref)
    assert bw.argv() == []                                                  # never a name search in bw
    assert BW.resolve(f"bw://{ITM1.upper()}/password") == PW                # the id is case-insensitive
    bw.edit(wrong_id=True)                                                  # bw answered with another item
    with pytest.raises(CommonsError, match="not found, or outside"):
        BW.resolve(f"bw://{ITM1}/password")


def test_the_fake_searches_by_name_like_the_real_cli(bw, monkeypatch):
    import subprocess
    env = os.environ | {"BW_SESSION": SESSION}
    one = subprocess.run([str(bw.bin), "get", "item", "BANK"], env=env, capture_output=True, text=True)
    assert one.returncode == 0 and json.loads(one.stdout)["id"] == ITM2     # a name finds an item in ANY folder
    st = bw.load()
    st["items"].append({"id": "66666666-6666-4666-8666-666666666666", "name": "bank two", "folderId": None, "fields": []})
    bw.save(st)
    many = subprocess.run([str(bw.bin), "get", "item", "bank"], env=env, capture_output=True, text=True)
    assert many.returncode == 1 and "More than one result" in many.stderr
    none = subprocess.run([str(bw.bin), "get", "item", "zzz"], env=env, capture_output=True, text=True)
    assert none.returncode == 1 and "Not found." in none.stderr


def test_ambient_bw_variables_are_not_passed_on(bw, monkeypatch):
    for k in ("BW_RESPONSE", "BW_QUIET", "BW_CLEANEXIT", "BW_RAW", "BW_PRETTY"):
        monkeypatch.setenv(k, "true")
    monkeypatch.setenv("BW_NOINTERACTION", "false")
    assert BW.resolve(f"bw://{ITM1}/password") == PW
    seen = bw.load()["envs"]
    assert seen and all(e == {"BW_NOINTERACTION": "true"} for e in seen)


def test_not_logged_in_and_premium_have_their_own_messages(bw):
    bw.edit(no_premium=True)
    with pytest.raises(CommonsError, match="Premium account") as e:
        BW.resolve(f"bw://{ITM1}/totp")
    assert "Premium status" not in str(e.value)
    bw.edit(logged_out=True)
    BW.reset()
    with pytest.raises(CommonsError, match="not logged in.*bw login"):
        BW.resolve(f"bw://{ITM1}/password")


def test_export_skips_what_bitwarden_would_refuse(bw, store):
    V.add(store, "wordy", "pw-1", "svc", ["http://svc.example"], {"bearer": True}, "d" * 10)
    store.x("UPDATE vault_creds SET purpose=? WHERE name='wordy'", ("n" * 7001,))
    V.add(store, "longuri", "pw-2", "svc", ["http://svc.example/" + "u" * 7000], {"bearer": True})
    V.add(store, "whoami", "pw-3", "svc", ["http://svc.example"], {"bearer": True})
    store.x("UPDATE vault_creds SET username=? WHERE name='whoami'", ("w" * 701,))
    V.add(store, "fine", "pw-4", "svc", ["http://svc.example"], {"bearer": True})
    store.x("UPDATE vault_creds SET username=?, purpose=? WHERE name='fine'", ("w" * 700, "n" * 7000))
    r = BW.export(store, "Commons backup", pause=0)
    assert r["skipped_too_large"] == ["longuri", "whoami", "wordy"] and r["created"] == ["fine"]


# ---- sync failures
def test_a_failed_first_sync_fails_fast_and_is_not_retried_for_a_minute(bw, monkeypatch):
    bw.edit(sync_fail=True)
    with pytest.raises(CommonsError, match="bw sync failed"):
        BW.resolve(f"bw://{ITM1}/password")
    with pytest.raises(CommonsError, match="try again shortly"):
        BW.resolve(f"bw://{ITM1}/password")
    assert bw.count("sync") == 1                                            # the second read did not try again
    assert bw.count("get") == 0
    monkeypatch.setattr(BW, "SYNC_RETRY_S", 0)
    bw.edit(sync_fail=False)
    assert BW.resolve(f"bw://{ITM1}/password") == PW                        # after the window it syncs again
    assert bw.count("sync") == 2


def test_a_failed_later_sync_serves_the_local_copy_for_a_minute(bw, store, monkeypatch):
    monkeypatch.setenv("COMMONS_BW_SYNC_S", "0.0001")
    assert BW.resolve(f"bw://{ITM1}/password") == PW
    bw.edit(sync_fail=True)
    assert BW.resolve(f"bw://{ITM1}/password") == PW                        # stale but served
    assert BW.resolve(f"bw://{ITM1}/username") == "ann"
    assert bw.count("sync") == 2                                            # one good, one failed, none after
    monkeypatch.setattr(BW, "SYNC_RETRY_S", 0)
    assert BW.resolve(f"bw://{ITM1}/password") == PW
    assert bw.count("sync") == 3
    with pytest.raises(CommonsError, match="bw sync failed"):               # export never works from a stale copy
        BW.export(store, "Commons backup")


def test_a_recreated_folder_is_picked_up_after_the_next_sync(bw, monkeypatch):
    assert BW.resolve(f"bw://{ITM1}/password") == PW
    st = bw.load()
    st["folders"][0]["id"] = "fld-reborn"
    st["items"][0]["folderId"] = "fld-reborn"
    bw.save(st)
    with pytest.raises(CommonsError, match="not found, or outside"):        # no sync yet: the cached id is stale, so closed
        BW.resolve(f"bw://{ITM1}/password")
    monkeypatch.setenv("COMMONS_BW_SYNC_S", "0.0001")
    assert BW.resolve(f"bw://{ITM1}/password") == PW                        # a sync ran: the folder is asked for again


# ---- export odds and ends
def test_an_existing_item_that_is_not_a_login_is_left_alone(bw, store):
    _store_creds(store)
    BW.export(store, "Commons backup", pause=0)
    st = bw.load()
    for i in st["items"]:
        if i["name"] == "alpha":
            i["type"] = 2
    bw.save(st)
    V.add(store, "alpha", "alpha-rotated-CCCC3333", "svc", ["http://svc.example"], {"bearer": True})
    writes = bw.load()["writes"]
    r = BW.export(store, "Commons backup", pause=0)
    assert r["failed"] == ["alpha"] and "not a login" in r["errors"]["alpha"] and r["unchanged"] == ["beta"]
    assert bw.load()["writes"] == writes


def test_an_odd_answer_for_one_credential_does_not_abort_the_run(bw, store, monkeypatch):
    _store_creds(store)
    real = BW._login_item

    def odd(cred, value):
        if cred["name"] == "alpha":
            raise ValueError("secret-detail " + value)
        return real(cred, value)
    monkeypatch.setattr(BW, "_login_item", odd)
    r = BW.export(store, "Commons backup", pause=0)
    assert r["failed"] == ["alpha"] and r["created"] == ["beta"]
    assert r["errors"]["alpha"] == "unexpected error (ValueError)" and "alpha-secret" not in json.dumps(r)


def test_a_folder_that_cannot_be_created_is_tried_once(bw, store):
    _store_creds(store)
    bw.edit(bad_folder_create=True)
    r = BW.export(store, "Commons backup", pause=0)
    assert r["failed"] == ["alpha", "beta"] and r["created"] == []
    assert bw.load()["folder_creates"] == 1


def test_a_list_that_is_not_a_list_stops_the_export_cleanly(bw, store):
    _store_creds(store)
    BW.export(store, "Commons backup", pause=0)
    bw.edit(list_items_raw='{"oops": 1}')
    with pytest.raises(CommonsError, match="not a list"):
        BW.export(store, "Commons backup", pause=0)


def test_two_folders_with_the_same_name_are_refused(bw):
    st = bw.load()
    st["folders"].append({"id": "fld-twin", "name": "Commons"})
    bw.save(st)
    with pytest.raises(CommonsError, match="two Bitwarden folders"):
        BW.resolve(f"bw://{ITM1}/password")


def test_concurrent_reads_unlock_once(bw, monkeypatch):
    monkeypatch.delenv("BW_SESSION")
    monkeypatch.setenv("COMMONS_BW_PASSWORD_FILE", str(bw.pwfile))
    bw.edit(unlock_delay=0.4)
    got, errs = [], []

    def go():
        try:
            got.append(BW.resolve(f"bw://{ITM1}/password"))
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert got == [PW] * 4 and not errs and bw.count("unlock") == 1


# ---------------------------------------------------------------------------------------------- review round 3
def test_a_relink_voids_what_was_waiting_for_the_owner(bw, store, svc, monkeypatch):
    base, seen = svc
    from commons_memory import board as B
    from commons_memory import typesafe
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    monkeypatch.setattr(typesafe, "noul", lambda state, qs, timeout=20, key=None: {k: 0.5 for k in qs})  # unclear: ask the owner
    other = "55555555-5555-4555-8555-555555555555"
    bw.edit(items=bw.load()["items"] + [{"id": other, "name": "second", "folderId": FOLDER, "login": {"password": "second-pw"},
                                         "fields": []}])
    _link(store, base)
    a = store.ensure_agent("muse")
    w = V.use(store, a, "svc", "GET", base + "/thing", "the user asked for the thing on svc")
    assert w["verdict"] == "waiting_for_user"
    _link(store, base, f"bw://{other}/password")                              # re-linked to another item
    d = store.q("SELECT verdict, pending FROM vault_decisions WHERE id=?", (w["decision_id"],))[0]
    assert d["verdict"] == "superseded" and d["pending"] is None
    assert B.get(store, w["question_id"])["status"] == "dropped"
    B.answer(store, w["question_id"], "yes")                                  # a late yes does nothing
    assert seen == [] and not store.q("SELECT 1 FROM vault_grants")
    assert store.q("SELECT verdict FROM vault_decisions WHERE id=?", (w["decision_id"],))[0]["verdict"] == "superseded"
    again = V.use(store, a, "svc", "GET", base + "/thing", "the user asked for the thing on svc")
    assert again["verdict"] == "waiting_for_user" and again["question_id"] != w["question_id"]   # a fresh question
    assert [x["verdict"] for x in V.decisions(store)]                          # the listing copes with the new verdict


def test_a_relink_with_other_hosts_or_auth_also_voids_it(bw, store, svc):
    base, _ = svc
    _link(store, base)
    for changed in ({"base": base + "/v2"}, {"auth": {"bearer": True}}):
        store.x("INSERT INTO vault_decisions(id,agent_id,cred,method,url,justification,scope,verdict,scores,reason,ts,pending) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("vdc_x" + str(len(changed["base"]) if "base" in changed else 0), "a", "svc", "GET", "u", "j",
                                                   "read", "ask_user", "{}", "r", 1.0, "{}"))
        V.link(store, "svc", f"bw://{ITM1}/password", "svc", [changed.get("base", base)], changed.get("auth", {"header": "X-Svc-Token"}))
        assert not store.q("SELECT 1 FROM vault_decisions WHERE verdict='ask_user'")
    store.x("INSERT INTO vault_decisions(id,agent_id,cred,method,url,justification,scope,verdict,scores,reason,ts,pending) "
            "VALUES('vdc_keep','a','svc','GET','u','j','read','ask_user','{}','r',1.0,'{}')")
    V.link(store, "svc", f"bw://{ITM1}/password", "svc", [base], {"bearer": True})   # nothing changed: it stays waiting
    assert store.q("SELECT 1 FROM vault_decisions WHERE id='vdc_keep' AND verdict='ask_user'")


def test_the_local_copy_is_not_served_forever_when_syncs_keep_failing(bw, monkeypatch):
    monkeypatch.setenv("COMMONS_BW_SYNC_S", "0.0001")
    assert BW.resolve(f"bw://{ITM1}/password") == PW
    bw.edit(sync_fail=True)
    assert BW.resolve(f"bw://{ITM1}/password") == PW                          # a short while on the local copy
    monkeypatch.setattr(BW, "MAX_STALE_S", -1)                                # the last good sync is now "over an hour" old
    with pytest.raises(CommonsError, match="not synced for over an hour"):
        BW.resolve(f"bw://{ITM1}/password")                                    # inside the no-retry window
    monkeypatch.setattr(BW, "SYNC_RETRY_S", 0)
    with pytest.raises(CommonsError, match="not synced for over an hour"):
        BW.resolve(f"bw://{ITM1}/password")                                    # and when the sync is tried again
    monkeypatch.setenv("COMMONS_BW_SYNC_S", "0")                               # never syncing is the owner's choice: no cap
    assert BW.resolve(f"bw://{ITM1}/password") == PW
