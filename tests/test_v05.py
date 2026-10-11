"""v0.5: Postgres, encryption, auth/ACL/rate limits, trust, escalation, catch-up, retention, background thread,
secrets backends, A2A server, tree-sitter + resolved calls, semantic clustering, token counting, dashboard SSE/TLS,
routing A/B validation, MCP hub pooling + per-agent creds, research corroboration, OTel, dedupe, export masking."""
import asyncio
import http.server
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from commons_memory.store import CommonsError, Store
from conftest import PG, make_store


def serve(handler_cls):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ------------------------------------------------------------------ Postgres
@pytest.mark.skipif(not PG, reason="set COMMONS_TEST_PG=postgresql://... to run against Postgres")
def test_postgres_pgvector_and_migration(tmp_path):
    s = make_store(tmp_path)
    assert s.dialect == "postgres" and s._pgvec
    a = s.add_item("memory", "Payments use Stripe webhooks with idempotency keys", project="shop")
    s.add_item("memory", "The office plant needs water on Fridays")
    sem = s.search("stripe idempotent webhook handling", mode="semantic")
    assert sem["results"][0]["id"] == a["id"]
    assert s.search("Stripe", mode="keyword")["results"][0]["id"] == a["id"]
    # sqlite -> postgres migration
    lite = Store(str(tmp_path / "src.db"), embedder="hash")
    m = lite.add_item("decision", "Use Postgres for scale", project="infra")
    from commons_memory.pgcompat import migrate_sqlite_to_pg
    dsn = f"{PG}{'&' if '?' in PG else '?'}schema=mig_{os.getpid()}_{int(time.time())}"
    counts = migrate_sqlite_to_pg(str(tmp_path / "src.db"), dsn)
    assert counts["items"] >= 1
    pg = Store(dsn, embedder="hash")
    assert pg.expand(m["id"])["content"].startswith("Use Postgres")
    assert pg.search("Postgres scale")["results"][0]["id"] == m["id"]


def test_pg_translator_offline():
    from commons_memory.pgcompat import _ddl, _placeholders
    assert _placeholders("SELECT * FROM t WHERE a=? AND b IS ? AND c LIKE '%x?%'") == \
        "SELECT * FROM t WHERE a=%s AND b IS NOT DISTINCT FROM %s AND c ILIKE '%%x?%%'"
    ddl = _ddl("CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(item_id UNINDEXED, title, content, tokenize='porter')")
    assert "tsvector" in ddl[0] and "gin(tsv)" in ddl[1]
    assert "BIGSERIAL PRIMARY KEY" in _ddl("CREATE TABLE a(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, v BLOB)")[0]


# ------------------------------------------------------------------ encryption at rest
def test_sqlcipher_encryption_at_rest(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    monkeypatch.setenv("COMMONS_DB_KEY", "correct horse battery staple")
    p = str(tmp_path / "enc.db")
    s = Store(p, embedder="hash")
    s.add_item("memory", "top secret plan: launch on Monday")
    s.db.close()
    raw = Path(p).read_bytes()
    assert b"launch on Monday" not in raw and not raw.startswith(b"SQLite format 3")
    assert Store(p, embedder="hash").search("launch")["results"]
    monkeypatch.setenv("COMMONS_DB_KEY", "wrong")
    with pytest.raises(CommonsError):
        Store(p, embedder="hash")


def test_encrypted_backup_and_dump_roundtrip(store, tmp_path, monkeypatch):
    from commons_memory.retention import backup, decrypt_file, dump, load, _key
    store.add_item("memory", "remember the alamo", project="hist")
    monkeypatch.setenv("COMMONS_BACKUP_KEY", "k3y")
    b = backup(store, str(tmp_path / "bk"), keep=2)
    assert b["encrypted"] and b"alamo" not in Path(b["path"]).read_bytes()
    if store.dialect == "sqlite":
        decrypt_file(b["path"], str(tmp_path / "plain.db"), _key())
        assert Store(str(tmp_path / "plain.db"), embedder="hash").search("alamo")["results"]
    d = dump(store, str(tmp_path / "d.jsonl.gz"))
    other = make_store(tmp_path, "other.db")
    c = load(other, d["path"])
    assert c["items"] >= 1 and other.search("alamo")["results"]
    assert load(other, d["path"])["items"] >= 1 and len(other.search("alamo")["results"]) == 1  # idempotent


# ------------------------------------------------------------------ HTTP auth, OAuth, rate limit, ACL
def _asgi(mw, path="/mcp", headers=None):
    sent = []

    async def recv():
        return {"type": "http.request", "body": b""}

    async def send(m):
        sent.append(m)
    scope = {"type": "http", "path": path, "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
             "client": ("127.0.0.1", 5555)}
    asyncio.run(mw(scope, recv, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict((k.decode(), v.decode()) for k, v in start["headers"]), body


def test_http_auth_reads_oauth_and_rate_limit(store):
    from commons_memory.auth import PRINCIPAL, AuthMiddleware, OAuthVerifier, RateLimiter
    seen = {}

    async def app(scope, receive, send):
        seen["p"] = PRINCIPAL.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    class IdP(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):  # noqa: N802 - RFC 7662 introspection
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            active = "token=good-oauth" in body
            out = json.dumps({"active": active, "sub": "alice", "iss": "https://idp.test", "exp": time.time() + 60}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(out)
    srv, url = serve(IdP)
    key = store.register_agent("reader")["api_key"]
    mw = AuthMiddleware(app, store, resource_url="http://127.0.0.1:8765", allow_anon_reads=False,
                        limiter=RateLimiter(per_min=3), oauth=OAuthVerifier(store, issuer="https://idp.test", introspect_url=url + "/introspect"))
    st, h, _ = _asgi(mw)
    assert st == 401 and "resource_metadata" in h["www-authenticate"]
    assert _asgi(mw, "/.well-known/oauth-protected-resource")[0] == 200
    assert _asgi(mw, headers={"Authorization": f"Bearer {key}"})[0] == 200 and seen["p"] == store.get_agent("reader")["id"]
    assert _asgi(mw, headers={"Authorization": "Bearer good-oauth"})[0] == 200
    assert store.get_agent(seen["p"])["name"] == "oauth:alice"
    assert _asgi(mw, headers={"Authorization": "Bearer bad"})[0] == 401
    codes = [_asgi(mw, headers={"Authorization": f"Bearer {key}"})[0] for _ in range(3)]
    assert codes[-1] == 429 and codes.count(200) == 2  # 3/min per principal
    srv.shutdown()


def test_project_acls_on_reads_and_writes(store):
    from commons_memory.auth import ACL, PRINCIPAL
    a, b = store.ensure_agent("alice"), store.ensure_agent("bob")
    secret = store.add_item("memory", "acquisition target is Initech", project="mna")
    store.add_item("memory", "public roadmap mentions Initech partnership")
    ACL(store).grant("mna", "alice", "write")
    tok = PRINCIPAL.set(b)
    try:
        ids = [r["id"] for r in store.search("Initech")["results"]]
        assert secret["id"] not in ids and ids
        with pytest.raises(CommonsError):
            store.expand(secret["id"])
        with pytest.raises(CommonsError):
            store.add_item("memory", "sneaky", project="mna")
    finally:
        PRINCIPAL.reset(tok)
    tok = PRINCIPAL.set(a)
    try:
        assert secret["id"] in [r["id"] for r in store.search("Initech")["results"]]
        store.add_item("memory", "alice note", project="mna")
    finally:
        PRINCIPAL.reset(tok)
    assert store.search("acquisition")["results"]  # local owner (no principal) sees everything


# ------------------------------------------------------------------ trust
def test_trust_weights_search_and_kb(store, monkeypatch):
    from commons_memory.kb import KB
    from commons_memory.trust import Trust
    good, bad = store.ensure_agent("good"), store.ensure_agent("bad")
    g = store.add_item("memory", "Deploys happen through the blue pipeline every Tuesday", agent_id=good, project="ops")
    b_ = store.add_item("memory", "Deploys happen through the red pipeline every Tuesday", agent_id=bad, project="ops")
    t = Trust(store)
    t.set_manual("bad", 0.1)
    t.set_manual("good", 0.9)
    res = store.search("deploys pipeline tuesday")
    assert res["results"][0]["id"] == g["id"] and "low-trust" in res["text"]
    KB(store).build("ops")
    page = KB(store).get("ops", "overview")["text"]
    assert b_["id"] not in page  # below COMMONS_KB_MIN_TRUST
    assert t.table()[0]["agent"] == "good"


# ------------------------------------------------------------------ escalation
def test_unassignable_escalates_reassign_llm_notify(tmp_path):
    from commons_memory.escalation import Escalator, notifications
    from commons_memory.orchestrator import Orchestrator
    from commons_memory.providers import ProviderLayer
    s = make_store(tmp_path, "esc.db")
    o = Orchestrator(s)
    dag = [{"key": "a", "type": "research", "title": "Find X", "body": "find it"}]
    helper = s.ensure_agent("helper")  # active but no profile -> unassignable by profile
    j = o.submit("find x", dag=dag)
    r = Escalator(s, after=999).run()
    assert r and r[0]["action"] == "reassigned" and r[0]["to"] == "helper"
    assert s.inbox(helper)  # told about it
    # nobody active -> distributor model executes it
    s2 = make_store(tmp_path, "esc2.db")
    pl = ProviderLayer(s2)
    pl.add_provider("mock", "mock", config={"models": [{"model": "m", "quality": 1}]})
    pl.refresh_models()
    pl.set_role("distributor", "mock/m")
    o2 = Orchestrator(s2)
    j2 = o2.submit("find y", dag=dag)
    r2 = Escalator(s2, after=999).run()
    assert r2[0]["action"] == "llm" and o2.status(j2["job_id"])["subtasks"][0]["status"] == "done"
    # no agents, no model -> user notification, not repeated
    s3 = make_store(tmp_path, "esc3.db")
    Orchestrator(s3).submit("find z", dag=dag)
    assert Escalator(s3, after=999).run()[0]["action"] == "notified"
    assert not Escalator(s3, after=999).run()
    assert notifications(s3, unread_only=True)[0]["title"].startswith("Subtask needs")
    assert j["job_id"]


# ------------------------------------------------------------------ recurring catch-up
@pytest.mark.parametrize("policy,expected", [("skip", 0), ("once", 1), ("all", 3)])
def test_recurring_catchup_policies(tmp_path, policy, expected):
    from commons_memory.orchestrator import Orchestrator
    s = make_store(tmp_path, f"rc-{policy}.db")
    o = Orchestrator(s)
    o.schedule("hourly", "0 * * * *", "check things", catchup=policy, max_catchup=3)
    s.x("UPDATE recurring SET next_run=?", (time.time() - 5 * 3600 - 60,))  # scheduler was down ~5h
    r = o.run_due()[0]
    assert r["missed"] >= 4 and len(r["jobs"]) == expected and r["policy"] == policy
    assert r["next_run"] and s.q("SELECT next_run FROM recurring")[0]["next_run"] > time.time()


# ------------------------------------------------------------------ retention
def test_prune_rolls_up_usage_and_gcs_context(store):
    from commons_memory.contextpacks import ContextStore
    from commons_memory.retention import prune
    from commons_memory.usage import Usage
    u = Usage(store)
    a = store.ensure_agent("w")
    for _ in range(3):
        u.report(a, tokens_in=100, tokens_out=50, cost=0.5)
    store.x("UPDATE usage SET ts=ts-200*86400")
    before = u.summary("agent")[0]
    cs = ContextStore(store)
    old = cs.save("p", "old gist", sections={"big": "x" * 500})
    cs.save("p", "new gist", sections={"small": "y"})
    store.x("UPDATE context_packs SET created_at=created_at-90*86400 WHERE id=?", (old["pack_id"],))
    r = prune(store)
    assert r["usage_rolled_up"] == 3 and r["context_packs_deleted"] == 1 and r["context_blobs_gc"] >= 1
    after = u.summary("agent")[0]
    assert after["cost"] == before["cost"] == 1.5 and after["calls"] == 3
    assert not store.q("SELECT 1 FROM usage") and not store.q("SELECT 1 FROM context_blobs WHERE content=?", ("x" * 500,))


# ------------------------------------------------------------------ background thread
def test_background_thread_rebuilds_kb_without_scheduler(store, monkeypatch):
    from commons_memory.background import start_background, stop_background
    from commons_memory.kb import KB
    monkeypatch.setenv("COMMONS_KB_DEBOUNCE", "0")
    monkeypatch.setenv("COMMONS_BG_POLL", "0.2")
    monkeypatch.setenv("COMMONS_EVERY_KB", "0.1")
    store.add_item("memory", "Background rebuild keeps the knowledge base fresh", project="bg")
    t = start_background(store)
    try:
        for _ in range(50):
            if not store.q("SELECT 1 FROM kb_state WHERE dirty=1"):
                break
            time.sleep(0.1)
        assert not store.q("SELECT 1 FROM kb_state WHERE dirty=1")
        assert "Background rebuild" in KB(store).get("bg")["text"]
    finally:
        stop_background(store)
    assert not t.is_alive()


# ------------------------------------------------------------------ secrets
def test_vault_kv_dynamic_and_infisical(store, monkeypatch):
    from commons_memory.redact import redact
    from commons_memory.secrets import Broker
    calls = []

    class Vault(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _j(self, o):
            b = json.dumps(o).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)

        def do_GET(self):  # noqa: N802
            calls.append(("GET", self.path, self.headers.get("X-Vault-Token") or self.headers.get("Authorization")))
            if self.path == "/v1/secret/data/app":
                return self._j({"data": {"data": {"password": "kv-pw"}}})
            if self.path == "/v1/database/creds/ro":
                n = sum(1 for c in calls if c[1] == self.path)
                return self._j({"lease_id": f"database/creds/ro/L{n}", "lease_duration": 60,
                                "data": {"username": f"v-user-{n}", "password": "dyn-pw"}})
            if self.path.startswith("/api/v4/secrets/DB_PASS"):
                assert "projectId=proj1" in self.path and "environment=dev" in self.path and "secretPath=%2Fapi" in self.path
                return self._j({"secret": {"secretValue": "inf-pw"}})
            self.send_response(404); self.end_headers()

        def do_PUT(self):  # noqa: N802
            calls.append(("PUT", self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self._j({})

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body == {"clientId": "cid", "clientSecret": "csec"}
            self._j({"accessToken": "inf-token", "expiresIn": 600})
    srv, url = serve(Vault)
    monkeypatch.setenv("VAULT_ADDR", url)
    monkeypatch.setenv("VAULT_TOKEN", "root")
    monkeypatch.setenv("INFISICAL_HOST", url)
    monkeypatch.setenv("INFISICAL_CLIENT_ID", "cid")
    monkeypatch.setenv("INFISICAL_CLIENT_SECRET", "csec")
    b, a = Broker(store), store.ensure_agent("deployer")
    for ref in ("vault://secret/app#password", "vault-dyn://database/creds/ro#username", "infisical://proj1/dev/api/DB_PASS"):
        b.register(ref)
    assert b.resolve(b.request("vault://secret/app#password", a)["lease_id"], a) == "kv-pw"
    l1 = b.request("vault-dyn://database/creds/ro#username", a)["lease_id"]
    l2 = b.request("vault-dyn://database/creds/ro#username", a)["lease_id"]
    u1, u2 = b.resolve(l1, a), b.resolve(l2, a)
    assert u1 != u2 and u1.startswith("v-user-")  # a fresh credential per lease
    b.revoke(l1)
    assert ("PUT", "/v1/sys/leases/revoke", {"lease_id": "database/creds/ro/L1"}) in calls
    store.x("UPDATE leases SET expires_at=0 WHERE id=?", (l2,))
    assert b.reap_expired() == 1 and any(c[2] == {"lease_id": "database/creds/ro/L2"} for c in calls if c[0] == "PUT")
    assert b.resolve(b.request("infisical://proj1/dev/api/DB_PASS", a)["lease_id"], a) == "inf-pw"
    srv.shutdown()
    # entropy-based detection of novel secret formats, without eating hashes/paths/ids
    novel = "token for acme: Zx9qL2mR7vT4kW8pN3sB6yH1cF5gJ0dXe"
    clean, kinds = redact(novel)
    assert "high_entropy" in kinds and "Zx9qL2mR7vT4" not in clean
    safe = "commit 3f2a9c1b7e4d6a8f0b2c4e6a8d0f1b3c5e7a9d2f path /home/User/Projects/MyRepo2024/src/main.py id mem_01a12d4b88e4d8396cdda1"
    assert redact(safe)[0] == safe


# ------------------------------------------------------------------ A2A server
def test_a2a_server_send_get_stream_cancel(store):
    from starlette.testclient import TestClient
    from commons_memory.a2a_server import build_app
    from commons_memory.orchestrator import Orchestrator
    o = Orchestrator(store)
    w, w2 = store.ensure_agent("worker"), store.ensure_agent("reviewer")
    for x in (w, w2):
        o.profiles.set(x, expertise={"general": 0.9, "research": 0.9, "code": 0.9, "test": 0.9, "plan": 0.9})
    c = TestClient(build_app(store, "http://testserver", poll=0.05))
    card = c.get("/.well-known/agent-card.json").json()
    assert card["capabilities"]["streaming"] and card["url"].endswith("/a2a")
    msg = {"role": "user", "parts": [{"kind": "text", "text": "Summarize the release notes"}], "messageId": "m1",
           "metadata": {"pattern": "parallel"}}
    t = c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": msg}}).json()["result"]
    assert t["kind"] == "task" and t["status"]["state"] in ("submitted", "working")
    got = c.post("/a2a", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": t["id"]}}).json()
    assert got["result"]["metadata"]["commons_job_id"]
    canc = c.post("/a2a", json={"jsonrpc": "2.0", "id": 3, "method": "tasks/cancel", "params": {"id": t["id"]}}).json()
    assert canc["result"]["status"]["state"] == "canceled"
    assert c.post("/a2a", json={"jsonrpc": "2.0", "id": 4, "method": "tasks/get", "params": {"id": "nope"}}).json()["error"]
    # streaming: a worker thread completes the job while the client listens
    msg2 = dict(msg, messageId="m2", metadata={})
    events, stop = [], threading.Event()

    def work():
        while not stop.is_set():
            for x in (w, w2):
                pk = o.claim(x)
                if pk:
                    o.complete(pk.get("subtask_id") or pk["id"], x, "VERDICT: approve\nAll good.")
            time.sleep(0.05)
    th = threading.Thread(target=work, daemon=True)
    th.start()
    try:
        with c.stream("POST", "/a2a", json={"jsonrpc": "2.0", "id": 5, "method": "message/stream", "params": {"message": msg2}}) as r:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:])["result"])
    finally:
        stop.set()
    kinds = [e.get("kind") for e in events]
    assert kinds[0] == "task" and "artifact-update" in kinds and events[-1]["status"]["state"] == "completed"


# ------------------------------------------------------------------ code: tree-sitter + resolved calls
def _repo(tmp_path):
    r = tmp_path / "repo"
    (r / "web").mkdir(parents=True)
    (r / "pkg").mkdir()
    (r / "web/util.ts").write_text("export function format(x: number): string { return String(x) }\n"
                                   "export default function helper() { return 1 }\n")
    (r / "web/other.ts").write_text("export function format(s: string) { return s.trim() }\n")
    (r / "web/app.ts").write_text("import helper, { format as fmt } from './util';\nimport * as o from './other';\n"
                                  "export class Cart extends Base {\n  total(): number { this.sum(); return fmt(helper()) }\n"
                                  "  sum() { return o.format('x') }\n}\nconst run = async () => { new Cart().total() }\n")
    (r / "pkg/a.py").write_text("def load():\n    return 1\n")
    (r / "pkg/b.py").write_text("def load():\n    return 2\n")
    (r / "pkg/main.py").write_text("from pkg.a import load as load_a\nimport pkg.b as bee\n\nclass Svc:\n"
                                   "    def go(self):\n        self.step()\n        return load_a() + bee.load()\n\n"
                                   "    def step(self):\n        pass\n")
    return r


def _calls(store, caller):
    return {(r["name"], r["path"], json.loads(r["meta"])["resolved"]) for r in store.q(
        "SELECT e2.name, e2.path, r.meta FROM relations r JOIN entities e1 ON e1.id=r.src JOIN entities e2 ON e2.id=r.dst "
        "WHERE r.type='calls' AND e1.name=?", (caller,))}


@pytest.mark.parametrize("ts", ["1", "0"])
def test_code_index_tree_sitter_and_resolved_calls(tmp_path, monkeypatch, ts):
    from commons_memory.codeindex import index_repo
    monkeypatch.setenv("COMMONS_TREE_SITTER", ts)
    if ts == "1":
        pytest.importorskip("tree_sitter_typescript")
    s = make_store(tmp_path, f"code{ts}.db")
    st = index_repo(s, str(_repo(tmp_path)), "demo")
    assert st["parser_js"] == ("tree-sitter" if ts == "1" else "regex")
    py = _calls(s, "Svc.go")
    assert ("load", "pkg/a.py", "import") in py and ("load", "pkg/b.py", "import") in py and ("Svc.step", "pkg/main.py", "self") in py
    if ts == "1":
        js = _calls(s, "Cart.total")
        assert ("format", "web/util.ts", "import") in js and ("helper", "web/util.ts", "import") in js
        assert ("Cart.sum", "web/app.ts", "self") in js
        assert ("format", "web/other.ts", "import") in _calls(s, "Cart.sum")
        assert ("Cart", "web/app.ts", "local") in _calls(s, "run")


# ------------------------------------------------------------------ semantic clustering, tokens
def test_semantic_topic_clustering(store, monkeypatch):
    from commons_memory.kb import KB
    monkeypatch.setenv("COMMONS_KB_CLUSTER", "semantic")
    for t in ["kubernetes pod crashloop restart policy", "kubernetes pod memory limits restart",
              "kubernetes deployment pod rollout restart", "sourdough bread starter hydration flour",
              "sourdough bread proofing flour oven", "sourdough starter flour feeding bread"]:
        store.add_item("memory", t)
    topics = KB(store).misc_topics()
    groups = [set(v) for k, v in topics.items() if k != "general"]
    assert len(groups) >= 2 and all(len(g) >= 2 for g in groups)
    names = " ".join(topics)
    assert ("kubernetes" in names or "pod" in names or "restart" in names) and ("sourdough" in names or "bread" in names or "flour" in names)


def test_token_counters(store):
    from commons_memory.contextpacks import count_tokens
    from commons_memory.providers import ProviderLayer
    pytest.importorskip("tiktoken")
    import tiktoken
    t = "Commons counts tokens exactly for OpenAI models."
    try:
        exact = len(tiktoken.get_encoding("o200k_base").encode(t))
    except Exception:  # noqa: BLE001 - encoding download unavailable offline
        pytest.skip("tiktoken encodings unavailable")
    assert count_tokens(t, "openai/gpt-4o") == exact
    pl = ProviderLayer(store)
    pl.add_provider("mock", "mock", config={"models": [{"model": "claude-x"}]})
    assert count_tokens("one two three four", "mock/claude-x", store, exact=True) == 4  # provider API path (mocked)
    assert store.q("SELECT COUNT(*) n FROM token_count_cache")[0]["n"] == 1


# ------------------------------------------------------------------ dashboard: SSE, TLS, users
def test_dashboard_sse_tls_and_viewer(store, tmp_path):
    from commons_memory.dashboard import add_dashboard_user, make_server, self_signed_cert
    cert, key = self_signed_cert(str(tmp_path / "tls"))
    viewer = add_dashboard_user(store, "pat", "viewer")["token"]
    srv = make_server(store, "127.0.0.1", 0, token="admintok", tls_cert=cert, tls_key=key)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"https://127.0.0.1:{srv.server_address[1]}"
    ctx = ssl.create_default_context(cafile=cert)
    ctx.check_hostname = False
    ok = urllib.request.urlopen(urllib.request.Request(base + "/api/system", headers={"Authorization": f"Bearer {viewer}"}), context=ctx)
    assert json.loads(ok.read())["dialect"] == store.dialect
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(base + "/api/notifications/read", data=b"{}", method="POST",
                                                      headers={"Authorization": f"Bearer {viewer}", "X-Commons": "1"}), context=ctx)
    assert e.value.code == 403
    os.environ["COMMONS_SSE_MAX_SECONDS"] = "3"
    try:
        r = urllib.request.urlopen(urllib.request.Request(base + "/events", headers={"Authorization": "Bearer admintok"}),
                                   context=ctx, timeout=10)
        first = r.readline().decode()
        assert first.startswith("event: change")
        store.add_item("memory", "something changed")
        lines = [r.readline().decode() for _ in range(6)]
        assert any(x.startswith("event: change") for x in lines)
    finally:
        os.environ.pop("COMMONS_SSE_MAX_SECONDS", None)
        srv.shutdown()


# ------------------------------------------------------------------ routing A/B validation
def test_routing_ab_validation_proposes_revert(store):
    from commons_memory.providers import ProviderLayer
    pl = ProviderLayer(store)
    pl._apply("code", None, "old/model", "baseline", "manual")
    for _ in range(10):
        pl.record_score("old/model", "code", 0.9)
    pl._apply("code", None, "new/model", "probe looked better", "proposal")
    for _ in range(5):
        pl.record_score("new/model", "code", 0.3)
    v = pl.validate_routing(min_samples=5)
    assert v[0]["verdict"] == "revert_proposed"
    assert pl.proposals()[0]["to_model"] == "old/model"


# ------------------------------------------------------------------ MCP hub: pooled sessions + per-agent creds
def test_mcp_pool_reuse_and_agent_credentials(store, monkeypatch):
    from commons_memory.mcphub import POOL, Hub
    up = str(Path(__file__).parent / "mock_upstream_mcp.py")
    monkeypatch.setenv("UPSTREAM_TOKEN", "tok_live_123")
    monkeypatch.setenv("ALICE_TOKEN", "tok_alice_456")
    h = Hub(store)
    h.add("up2", command=sys.executable, args=[up], env_refs={"UPSTREAM_TOKEN": "env://UPSTREAM_TOKEN"})
    a, b = store.ensure_agent("alice"), store.ensure_agent("bob")
    before = len(POOL.sessions)
    h.call("up2__create_note", {"text": "1"}, b)
    h.call("up2__create_note", {"text": "2"}, b)
    assert len(POOL.sessions) == before + 1  # second call reused the warm session
    from commons_memory.secrets import Broker
    Broker(store).register("env://ALICE_TOKEN")
    h.set_agent_credentials("up2", "alice", env_refs={"UPSTREAM_TOKEN": "env://ALICE_TOKEN"}, require=True)
    assert h.call("up2__create_note", {"text": "3"}, a)["status"] == "ok"
    assert len(POOL.sessions) == before + 2  # alice has her own upstream identity
    with pytest.raises(CommonsError):
        h.call("up2__create_note", {"text": "4"}, b)  # bob has no own credentials and they are required
    with pytest.raises(CommonsError):
        h.set_agent_credentials("up2", "bob", env_refs={"UPSTREAM_TOKEN": "raw-secret-value"})
    POOL.close_server("up2")


# ------------------------------------------------------------------ misc
def test_research_corroboration_dedupe_export_mask_and_otel(store, monkeypatch):
    from commons_memory.graphexport import export
    from commons_memory.orchestrator import Orchestrator
    from commons_memory.research import corroboration
    srcs = [{"url": "https://a.example.com/x", "excerpt": "The Falcon library released version 4 with async support."},
            {"url": "https://news.other.org/y", "excerpt": "Falcon released version 4, adding async support."},
            {"url": "https://blog.third.net/z", "excerpt": "Unrelated post about gardening."}]
    assert corroboration("Falcon released version 4 with async support", srcs[0]["url"], srcs) == 2
    assert corroboration("Gardening tips for spring tulips", srcs[2]["url"], srcs) == 1
    o = Orchestrator(store)
    j1 = o.submit("Write the quarterly security report for the payments service")
    j2 = o.submit("Write the quarterly security report for the payments service", dedupe=True)
    assert j2["duplicate_of"] == j1["job_id"]
    l1 = o.add_leftover("update the README install section with uv instructions")
    l2 = o.add_leftover("Update README install section with the uv instructions")
    assert l2.get("duplicate") and l2["id"] == l1["id"]
    from commons_memory.secrets import Broker
    Broker(store).register("vault://secret/payments-prod-db#password", project="p")
    g = store.export_graph(None, True)
    out = export(g, "json")
    assert "payments-prod-db" not in out and "secret_ref:" in out
    monkeypatch.setenv("COMMONS_OTEL", "1")
    pytest.importorskip("opentelemetry.sdk")
    import commons_memory.telemetry as tel
    tel._tracer = None
    from commons_memory.providers import ProviderLayer
    pl = ProviderLayer(store)
    pl.add_provider("mock", "mock", config={"models": [{"model": "m"}]})
    pl.refresh_models()
    pl.chat("mock/m", "hello")
    assert any(sp.name == "llm.chat" for sp in tel.memory_spans())
