import json
import random
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from commons_memory.breakdown import breakdown
from commons_memory.drive import Drive, FTPBlobs, LocalBlobs, S3Blobs, WebDAVBlobs, check_host_path
from commons_memory.orchestrator import Orchestrator
from commons_memory.scheduler import Scheduler
from commons_memory.store import CommonsError, Store


@pytest.fixture
def drive(store, tmp_path):
    return Drive(store, LocalBlobs(str(tmp_path / "blobs")))


def test_drive_versions_diff_restore_search_links(store, drive):
    a = store.ensure_agent("claude")
    r = drive.write("/docs/plan.md", "# Plan\nuse postgres\n", project="shop", agent_id=a, tags=["plan"])
    assert r["version"] == 1
    drive.edit("/docs/plan.md", "postgres", "sqlite first, postgres later", project="shop", agent_id=a)
    assert drive.stat("/docs/plan.md", "shop")["version"] == 2
    assert drive.write("/docs/plan.md", "# Plan\nuse sqlite first, postgres later\n", project="shop")["unchanged"]
    d = drive.diff("/docs/plan.md", 1, 2, "shop")
    assert "-use postgres" in d and "+use sqlite first" in d
    assert [h["version"] for h in drive.history("/docs/plan.md", "shop")] == [2, 1]
    drive.restore("/docs/plan.md", 1, "shop", a)
    assert drive.read("/docs/plan.md", "shop")["text"].endswith("use postgres\n")
    drive.delete("/docs/plan.md", "shop", a)
    assert drive.list("/", "shop")["files"] == []
    drive.restore("/docs/plan.md", project="shop")
    assert drive.read("/docs/plan.md", "shop")["version"] == 3
    hits = drive.search("postgres", "shop")
    assert hits["results"] and hits["results"][0]["path"] == "/docs/plan.md"
    # projects are isolated
    assert drive.list("/", "other")["files"] == []
    with pytest.raises(CommonsError):
        drive.read("/docs/plan.md", "other")
    # artifact links
    f = drive.write("/out/report.md", "done", project="shop", agent_id=a, link="sub_123")
    assert drive.artifacts("sub_123")[0]["id"] == f["id"]
    drive.move("/out/report.md", "/out/final.md", "shop")
    assert drive.stat("/out/final.md", "shop")["version"] == 1
    nd = drive.list("/", "shop", recursive=False)
    assert set(nd["dirs"]) == {"/docs", "/out"}


def test_drive_concurrency_locks_secrets_quota_budgeted_read(store, drive, monkeypatch):
    a, b = store.ensure_agent("a"), store.ensure_agent("b")
    drive.write("/f.txt", "v1", project="p", agent_id=a)
    with pytest.raises(CommonsError, match="version conflict"):
        drive.write("/f.txt", "x", project="p", agent_id=b, expected_version=0)
    drive.write("/f.txt", "v2", project="p", agent_id=b, expected_version=1)
    drive.lock("/f.txt", a, "p")
    with pytest.raises(CommonsError, match="locked"):
        drive.write("/f.txt", "v3", project="p", agent_id=b)
    with pytest.raises(CommonsError, match="locked"):
        drive.lock("/f.txt", b, "p")
    drive.lock("/f.txt", a, "p", release=True)
    drive.write("/f.txt", "v3", project="p", agent_id=b)
    r = drive.write("/cfg.env", "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD\n", project="p")
    assert r.get("redacted") and "sk-abc" not in drive.read("/cfg.env", "p")["text"]
    with pytest.raises(CommonsError, match="secret"):
        drive.write("/x.txt", "ghp_" + "a1B2" * 9, project="p", on_secret="reject")
    big = "".join(f"line {i} " + "x" * 50 + "\n" for i in range(500))
    drive.write("/big.txt", big, project="p")
    r1 = drive.read("/big.txt", "p", max_tokens=200)
    assert r1["truncated"] and len(r1["text"]) <= 800
    r2 = drive.read("/big.txt", "p", max_tokens=200, offset_line=r1["next_offset_line"])
    assert r2["text"].startswith(f"line {r1['next_offset_line']} ")
    # binary
    drive.write("/img.png", b"\x89PNG\r\n\x1a\n\x00\xff", project="p")
    assert drive.read("/img.png", "p")["binary"]
    monkeypatch.setenv("COMMONS_DRIVE_QUOTA_MB", "0.02")
    with pytest.raises(CommonsError, match="quota"):
        drive.write("/big2.txt", big, project="p")


def test_drive_concurrent_writers_no_lost_update(store, drive):
    drive.write("/counter.txt", "0", project="p")
    errs, ok = [], []

    def worker(i):
        st = Store(store.path) if hasattr(store, "path") and store.path else store
        d = Drive(st, drive.b)
        for _ in range(10):
            try:
                cur = d.read("/counter.txt", "p")
                d.write("/counter.txt", str(int(cur["text"]) + 1), project="p", expected_version=cur["version"])
                ok.append(i)
            except CommonsError as e:
                errs.append(str(e))
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert int(drive.read("/counter.txt", "p")["text"]) == len(ok)  # every successful write counted, none lost
    assert all("conflict" in e for e in errs)


def test_drive_acl(store, drive):
    from commons_memory.auth import ACL, PRINCIPAL
    a, b = store.ensure_agent("owner"), store.ensure_agent("stranger")
    drive.write("/s.md", "secret plan", project="priv", agent_id=a)
    ACL(store).grant("priv", "owner", "admin")
    tok = PRINCIPAL.set(b)
    try:
        with pytest.raises(CommonsError):
            drive.read("/s.md", "priv")
        with pytest.raises(CommonsError):
            drive.write("/t.md", "x", project="priv", agent_id=b)
    finally:
        PRINCIPAL.reset(tok)


def test_host_mounts(store, drive, tmp_path, monkeypatch):
    root = tmp_path / "host"
    (root / "ro").mkdir(parents=True)
    (root / "ro" / "readme.txt").write_text("hello host")
    (root / "rw").mkdir()
    with pytest.raises(CommonsError, match="COMMONS_HOST_ROOTS"):
        drive.mount_host("ro", str(root / "ro"))
    monkeypatch.setenv("COMMONS_HOST_ROOTS", str(root))
    with pytest.raises(CommonsError, match="outside"):
        check_host_path("/etc")
    drive.mount_host("ro", str(root / "ro"))
    drive.mount_host("rw", str(root / "rw"), read_only=False)
    assert drive.read("/mnt/ro/readme.txt")["text"] == "hello host"
    with pytest.raises(CommonsError, match="read-only"):
        drive.write("/mnt/ro/new.txt", "x")
    with pytest.raises(CommonsError):
        drive.read("/mnt/ro/../../../etc/passwd")
    drive.write("/mnt/rw/sub/out.txt", "written")
    assert (root / "rw" / "sub" / "out.txt").read_text() == "written"
    assert "/mnt/ro" in drive.list("/")["mounts"]
    (root / "ro" / "evil").symlink_to("/etc")
    with pytest.raises(CommonsError, match="outside"):
        drive.read("/mnt/ro/evil/hostname")


class _ObjStore(BaseHTTPRequestHandler):
    objs: dict = {}
    auth: list = []

    def log_message(self, *a):
        pass

    def _key(self):
        return urllib.parse.unquote(self.path)

    def do_PUT(self):
        self.auth.append(self.headers.get("Authorization", ""))
        self.objs[self._key()] = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(201)
        self.end_headers()

    def do_MKCOL(self):
        self.send_response(201)
        self.end_headers()

    def do_GET(self):
        v = self.objs.get(self._key())
        self.send_response(200 if v is not None else 404)
        self.end_headers()
        self.wfile.write(v or b"")

    def do_HEAD(self):
        self.send_response(200 if self._key() in self.objs else 404)
        self.end_headers()

    def do_DELETE(self):
        self.objs.pop(self._key(), None)
        self.send_response(204)
        self.end_headers()


@pytest.fixture
def objserver():
    _ObjStore.objs, _ObjStore.auth = {}, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ObjStore)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_s3_backend_mock(store, objserver):
    d = Drive(store, S3Blobs(objserver, "bucket", "us-east-1", "AKID", "SECRET"))
    d.write("/a.md", "hello s3", project="p")
    d.write("/a.md", "hello again", project="p")
    assert d.read("/a.md", "p")["text"] == "hello again"
    assert d.read("/a.md", "p", version=1)["text"] == "hello s3"
    assert all(k.startswith("/bucket/commons-drive/") for k in _ObjStore.objs)
    assert _ObjStore.auth[0].startswith("AWS4-HMAC-SHA256 Credential=AKID/") and "Signature=" in _ObjStore.auth[0]


def test_webdav_backend_mock(store, objserver):
    d = Drive(store, WebDAVBlobs(objserver + "/remote.php/dav/files/me/commons", "me", "pw"))
    d.write("/n.md", "nextcloud", project="p")
    assert d.read("/n.md", "p")["text"] == "nextcloud"
    assert _ObjStore.auth[0].startswith("Basic ")


def test_ftp_backend_fake(store, monkeypatch):
    import ftplib
    files = {}

    class FakeFTP:
        def __init__(self, host, timeout=0):
            pass

        def login(self, u, p):
            pass

        def mkd(self, d):
            pass

        def storbinary(self, cmd, f):
            files[cmd[5:]] = f.read()

        def retrbinary(self, cmd, cb):
            cb(files[cmd[5:]])

        def size(self, p):
            if p not in files:
                raise ftplib.error_perm("550")
            return len(files[p])

        def delete(self, p):
            files.pop(p)
    monkeypatch.setattr(ftplib, "FTP", FakeFTP)
    d = Drive(store, FTPBlobs("ftp.example", tls=False))
    d.write("/f.txt", "over ftp", project="p")
    assert d.read("/f.txt", "p")["text"] == "over ftp"


def test_optional_backends_explain_missing_deps():
    from commons_memory.drive import SFTPBlobs, SMBBlobs
    for cls, args in ((SFTPBlobs, ("h", "u")), (SMBBlobs, ("\\\\srv\\share",))):
        try:
            cls(*args)
        except CommonsError as e:
            assert "pip install" in str(e)
        except Exception:  # noqa: BLE001 - dependency installed but no server: fine
            pass


def test_git_sync(store, drive, tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("repo readme")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    r = drive.sync_git(str(repo), direction="pull", prefix="/repo", project="p")
    assert r["changed"] == 1 and drive.read("/repo/README.md", "p")["text"] == "repo readme"
    drive.write("/repo/NEW.md", "from drive", project="p")
    r = drive.sync_git(str(repo), direction="push", prefix="/repo", project="p", commit=False)
    assert (repo / "NEW.md").read_text() == "from drive"


# ------------------------------------------------------------------ scheduling
def _dag(n):
    return [{"key": "a", "type": "code", "title": f"part A {n}"}, {"key": "b", "type": "code", "title": f"part B {n}"},
            {"key": "c", "type": "summarize", "title": f"merge {n}", "deps": ["a", "b"]}]


def test_priorities_fair_share_limits_deps_preempt(store):
    o = Orchestrator(store)
    sch = Scheduler(store)
    a = store.ensure_agent("w1")
    o.profiles.set(a, expertise={"code": 0.8, "summarize": 0.8}, max_concurrent=10)
    j_low = o.submit("low", dag=_dag("low"), project="alpha")["job_id"]
    j_hi = o.submit("hi", dag=_dag("hi"), project="beta")["job_id"]
    sch.set_job(j_hi, priority=5)
    assert o.claim(a)["job_id"] == j_hi
    # fair share: beta has 1 in flight, equal priority projects -> alpha next once priorities are equal
    sch.set_job(j_hi, priority=0)
    assert o.claim(a)["job_id"] == j_low
    # project concurrency limit
    sch.configure("alpha", max_concurrent=1)
    assert o.claim(a)["job_id"] == j_hi
    assert o.claim(a) is None  # alpha full, beta's remaining subtask waits on deps
    # cross-project dependency
    j3 = o.submit("after", dag=_dag("after"), project="gamma")["job_id"]
    sch.set_job(j3, after=[j_low])
    with pytest.raises(CommonsError, match="cycle"):
        sch.set_job(j_low, after=[j3])
    assert o.claimable(a) == []
    # preemption
    r = sch.preempt(below=1, requeue=True)
    assert set(r["preempted"]) >= {j_low, j_hi} and r["requeued"] == 3
    assert o.claim(a) is None
    sch.resume()
    assert o.claim(a) is not None
    mc = sch.mission_control()
    assert {p["project"] for p in mc["projects"]} >= {"alpha", "beta", "gamma"}
    assert any(x["agent"] == "w1" and x["load"] >= 1 for x in mc["agents"])


def test_agent_capacity(store):
    o = Orchestrator(store)
    a = store.ensure_agent("solo")
    o.profiles.set(a, expertise={"code": 0.9}, max_concurrent=2)
    for i in range(3):
        o.submit(f"t{i}", dag=[{"key": "x", "type": "code", "title": f"t{i}"}], project="p")
    assert o.claim(a) and o.claim(a)
    assert o.claim(a) is None  # at capacity


def test_breakdown(store):
    o = Orchestrator(store)
    a = store.ensure_agent("w")
    o.profiles.set(a, expertise={"code": 0.9, "summarize": 0.9}, max_concurrent=5)
    jid = o.submit("build", dag=_dag("x"), project="p")["job_id"]
    p = o.claim(a)
    o.complete(p["subtask_id"], a, "did it", tokens=100, cost=0.01)
    b = breakdown(store, jid)
    assert len(b["nodes"]) == 3 and {(e["from"], e["to"]) for e in b["edges"]} == {("a", "c"), ("b", "c")}
    done = [n for n in b["nodes"] if n["status"] == "done"][0]
    assert done["agent"] == "w" and done["result"] == "did it" and done["timeline"]
    assert "rationale" in done and b["counts"]["done"] == 1


def test_stress_5_projects_concurrent_agents(tmp_path):
    db = str(tmp_path / "stress.db")
    s0 = Store(db, embedder="hash")
    o0 = Orchestrator(s0)
    names = [f"agent{i}" for i in range(6)]
    for n in names:
        o0.profiles.set(s0.ensure_agent(n), expertise={"code": 0.7 + random.random() / 5, "summarize": 0.7}, max_concurrent=3)
    jobs = []
    for p in range(5):
        Scheduler(s0).configure(f"proj{p}", priority=p % 2, max_concurrent=4)
        for j in range(3):
            jobs.append(o0.submit(f"job {p}.{j}", dag=_dag(f"{p}.{j}"), project=f"proj{p}")["job_id"])
    claims: dict[str, list] = {}
    lock = threading.Lock()
    errors = []
    stop = time.time() + 60

    def run(name):
        s = Store(db, embedder="hash")
        o = Orchestrator(s)
        aid = s.ensure_agent(name)
        while time.time() < stop:
            try:
                p = o.claim(aid)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
                continue
            if not p:
                if all(r["status"] == "done" for r in s.q("SELECT status FROM jobs")):
                    return
                time.sleep(0.01)
                continue
            with lock:
                claims.setdefault(p["subtask_id"], []).append(name)
            time.sleep(random.random() / 100)
            try:
                o.complete(p["subtask_id"], aid, f"{name} done")
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
    ts = [threading.Thread(target=run, args=(n,)) for n in names]
    [t.start() for t in ts]
    [t.join(70) for t in ts]
    assert not errors, errors[:3]
    st = {r["status"] for r in s0.q("SELECT status FROM jobs")}
    assert st == {"done"}, st
    assert len(claims) == 45 and all(len(v) == 1 for v in claims.values())  # no duplicate claims


def test_dashboard_v7_endpoints(store, tmp_path, monkeypatch):
    import urllib.request

    from commons_memory.dashboard import make_server
    monkeypatch.setenv("COMMONS_DRIVE_DIR", str(tmp_path / "dblobs"))
    o = Orchestrator(store)
    a = store.ensure_agent("w")
    o.profiles.set(a, expertise={"code": 0.9, "summarize": 0.9}, max_concurrent=5)
    jid = o.submit("dash job", dag=_dag("d"), project="dp")["job_id"]
    srv = make_server(store, "127.0.0.1", 0, "tok", None, None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(p):
        sep = "&" if "?" in p else "?"
        with urllib.request.urlopen(f"{base}{p}{sep}token=tok") as r:
            return r.read(), r.headers

    def post(p, b):
        req = urllib.request.Request(f"{base}/api/{p}?token=tok", data=json.dumps(b).encode(), method="POST",
                                     headers={"X-Commons": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    assert b"Mission control" in get("/")[0]
    assert json.loads(get("/api/mission")[0])["totals"]["running_jobs"] == 1
    assert len(json.loads(get(f"/api/breakdown_view?job={jid}")[0])["nodes"]) == 3
    assert "policy" in json.loads(get("/api/routing_view")[0])
    assert "board" in json.loads(get("/api/presence_view")[0])
    post("drive/upload", {"project": "dp", "path": "/notes/a.md", "text": "hello dash"})
    assert json.loads(get("/api/drive_file?project=dp&path=/notes/a.md")[0])["text"] == "hello dash"
    body, h = get("/drive/raw?project=dp&path=/notes/a.md")
    assert body == b"hello dash"
    post("drive/upload", {"project": "dp", "path": "/x.svg", "text": "<svg onload=alert(1)>"})
    assert get("/drive/raw?project=dp&path=/x.svg")[1]["Content-Type"] == "application/octet-stream"
    post("sched/project", {"project": "dp", "paused": True})
    assert json.loads(get("/api/mission")[0])["projects"][0]["paused"]
    srv.shutdown()
