"""Seed a demo hub for screenshots and UI work: made-up agents, jobs, a workflow, board items and memories.
Never point it at a real database.   python scripts/demo_seed.py /tmp/demo.db && commons dashboard --db /tmp/demo.db"""
import json
import random
import sys
import time

sys.path.insert(0, "src")
sys.path.insert(0, "tests")
from commons_memory import board as B  # noqa: E402
from commons_memory.orchestrator import Orchestrator  # noqa: E402
from commons_memory.store import Store  # noqa: E402
from commons_memory.usage import Usage  # noqa: E402


def seed(path: str) -> None:
    s = Store(path)
    o = Orchestrator(s)
    rnd = random.Random(7)
    team = {"claude-code": {"code": 0.9, "plan": 0.8, "review": 0.7}, "gemini": {"research": 0.9, "review": 0.8, "test": 0.7},
            "grok": {"debate": 0.9, "red_team": 0.9, "review": 0.8}, "codex": {"code": 0.85, "test": 0.8, "spec": 0.6},
            "cursor": {"code": 0.7, "review": 0.9, "judge": 0.8}}
    ids = {}
    for name, exp in team.items():
        ids[name] = s.ensure_agent(name)
        o.profiles.set(ids[name], expertise=exp, cost=3, speed=3, max_concurrent=3)
    for n, text in enumerate(["Staging runs on k3s in eu-west, three nodes", "Release notes go out every Friday at 15:00 UTC",
                              "The billing service owns invoice numbering; never generate ids client-side",
                              "Customer exports are capped at 50,000 rows per file",
                              "Design tokens live in packages/ui/tokens.json", "On-call rotates Monday 09:00 local"]):
        s.add_item("memory", text, agent_id=list(ids.values())[n % len(ids)], project=rnd.choice(["web", "billing", "infra"]))
    s.record_decision("Use Postgres row-level security for tenant isolation", "one policy per table beats app checks",
                      agent_id=ids["claude-code"], project="billing")
    for t in ["Add CSV export to the reports page", "Move invoices to the new numbering scheme",
              "Weekly dependency upgrade", "Investigate p95 latency on /search"]:
        B.add(s, "ask", t, project=rnd.choice(["web", "billing", "infra"]), source="user")
    B.add(s, "question", "Which date format should exports use: ISO 8601 or the account locale?", agent_id=ids["codex"])
    jid = o.submit("Add CSV export to the reports page", pattern="sdlc", project="web", agent_id=ids["claude-code"])["job_id"]
    spec = "Spec.\nAC1: an Export button downloads a CSV\nAC2: columns match the table\nAC3: unicode headers survive\n"
    answers = {"spec": spec, "plan": "PRD and build graph: backend (AC2, AC3), UI (AC1)",
               "code": "to_csv and the button\nTESTS: pytest -q -> 41 passed", "review": "Solid.\nVERDICT: approve",
               "test": "AC1: PASS test_button\nAC2: PASS test_columns\nAC3: FAIL headers mangled\nTESTS: pytest -> 1 failed\nVERDICT: reject"}
    for _ in range(5):
        for name, aid in ids.items():
            p = o.claim(aid)
            if p:
                o.complete(p["subtask_id"], aid, answers.get(p["type"], "done"))
        for q in s.q("SELECT id, text FROM board WHERE kind='question' AND status<>'done' AND text LIKE '[gate %'"):
            B.answer(s, q["id"], "yes", by="owner")
    o.submit("Review the auth middleware for session fixation", pattern="cross_review", project="web", agent_id=ids["cursor"])
    o.submit("Should we move search to a hosted vector database?", pattern="debate", project="infra", agent_id=ids["grok"])
    for name, aid in ids.items():
        for d in range(14):
            Usage(s).report(aid, tokens_in=rnd.randint(20_000, 90_000), tokens_out=rnd.randint(3_000, 20_000),
                            model={"claude-code": "anthropic/claude-sonnet-5-5", "gemini": "google/gemini-3-flash"}.get(name, ""),
                            cost=round(rnd.uniform(0.05, 0.9), 3), seconds=rnd.uniform(30, 600), note="demo")
            s.x("UPDATE usage SET ts=? WHERE id=(SELECT MAX(id) FROM usage)", (time.time() - d * 86400 - rnd.randint(0, 80000),))
    from commons_memory.messaging import heartbeat
    for name in ("claude-code", "gemini", "grok"):
        heartbeat(s, ids[name], status="online", client="demo")

    # Projects workspace demo seed: atlas, lumen, home, fix-typo
    now_t = time.time()
    for i, mem in enumerate([
        "Atlas API gateway v2 routing tables generated",
        "Postgres read replicas configured for Atlas API",
        "Rate limiter algorithm changed to token bucket",
        "Websocket connection pool tuned for dashboard events",
        "Atlas documentation generator updated to OpenAPI 3.1",
    ]):
        s.add_item("memory", mem, agent_id=ids["claude-code"], project="atlas-api", created_at=now_t - (5 - i) * 86400)
    s.add_item("memory", "Atlas web responsive navigation tests passing", agent_id=ids["gemini"], project="atlas-web", created_at=now_t - 2 * 86400)
    s.add_item("memory", "Atlas developer guides published to staging", agent_id=ids["codex"], project="atlas-docs", created_at=now_t - 86400)

    s.record_decision("Adopt OpenAPI 3.1 specification for Atlas API endpoints", "enables contract testing and automated SDK generation", agent_id=ids["claude-code"], project="atlas-api")
    s.record_decision("Use token bucket rate limiting across public Atlas routes", "smooths traffic bursts without dropping requests", agent_id=ids["grok"], project="atlas-api")
    s.add_item("handoff", "HANDOFF telemetry middleware completed, ready for load testing", agent_id=ids["claude-code"], project="atlas-api")

    s.session_for(ids["claude-code"], "atlas-api", "integrate telemetry exporter")
    s.session_for(ids["claude-code"], "atlas-web", "redesign query builder interface")
    s.session_for(ids["codex"], "atlas-docs", "generate reference API documentation")
    B.add(s, "ask", "Migrate Atlas authentication tokens to asymmetric keys", project="atlas-api", source="user")

    atlas_job = o.submit("Add telemetry middleware to Atlas API", pattern="parallel", project="atlas-api", title="Add telemetry middleware to Atlas API",
                         dag=[{"key": "telemetry", "type": "code", "title": "Implement OTLP tracer exporter", "body": "", "deps": []}])["job_id"]
    s.x("UPDATE subtasks SET status='done', claimed_by=?, result='TESTS: 18 passed', updated_at=? WHERE job_id=?",
        (ids["claude-code"], now_t, atlas_job))

    from commons_memory.workspace import record_changes
    subtask = dict(s.q("SELECT * FROM subtasks WHERE job_id=?", (atlas_job,))[0])
    diff_text = """diff --git a/atlas/telemetry.py b/atlas/telemetry.py
--- a/atlas/telemetry.py
+++ b/atlas/telemetry.py
@@ -10,4 +10,8 @@ def init_tracer():
-    return None
+    tracer = Tracer(service="atlas-api")
+    tracer.add_exporter(OTLPExporter())
+    return tracer
"""
    record_changes(s, ids["claude-code"], subtask=subtask, repo="github.com/atlas/api", branch="feat/telemetry",
                   commit="a1b2c3d4e5f6", diff=diff_text, stat="atlas/telemetry.py | 6 ++--")

    s.add_item("memory", "Lumen state persistence migrated to SQLite", agent_id=ids["cursor"], project="lumen-app")
    s.add_item("memory", "Desktop tray menu updated for macOS Ventura", agent_id=ids["codex"], project="lumen-desktop")

    for i in range(3):
        s.add_item("memory", f"Shell history and dotfile backup {i}", agent_id=ids["claude-code"], project="home")

    s.add_item("memory", "Fixed spelling of configuration in onboarding instructions", agent_id=ids["cursor"], project="fix-typo")

    # Infrastructure map demo seed: hv01 (Proxmox), nas01 (NAS), guests media and apps, stale guest
    from commons_memory import infra as I
    m_atlas = s.add_item("memory", "Atlas API gateway running in LXC 202 on hv01, connected to Postgres on port 5432 and exposed via Cloudflare tunnel at atlas.example.com", agent_id=ids["claude-code"], project="atlas-api", created_at=now_t - 3 * 86400)["id"]
    m_media = s.add_item("memory", "Media stack running in LXC 201 on hv01: mediaserver streams from nas01 /volume1/media, requests on port 5055", agent_id=ids["gemini"], project="web", created_at=now_t - 2 * 86400)["id"]
    m_stale = s.add_item("memory", "Legacy staging runner on LXC 209 decommissioned after CI migrated", agent_id=ids["codex"], project="infra", created_at=now_t - 7 * 86400)["id"]

    # Initial snapshot to establish previous state with stale guest
    I.ingest_snapshot(s, {
        "collector": "proxmox", "source": "pve", "at": now_t - 7200,
        "entities": [
            {"key": "hv01", "kind": "machine", "name": "hv01", "attrs": {"ip": "10.20.0.10", "cpu_count": 32, "memory_total": 68719476736, "memory_used": 30064771072, "os": "Proxmox VE 8.2"}},
            {"key": "hv01/lxc/209", "kind": "guest", "name": "staging-runner", "attrs": {"vmid": 209, "ip": "10.20.0.29", "status": "stopped", "cores": 2, "memory_mb": 4096}},
        ],
        "relations": [
            {"src": "hv01/lxc/209", "rel": "runs_on", "dst": "hv01"}
        ]
    })

    # Active snapshot representing current discovery
    snap_ents = [
        {"key": "hv01", "kind": "machine", "name": "hv01", "attrs": {"ip": "10.20.0.10", "cpu_count": 32, "memory_total": 68719476736, "memory_used": 30064771072, "os": "Proxmox VE 8.2"}},
        {"key": "nas01", "kind": "machine", "name": "nas01", "attrs": {"ip": "10.20.0.15", "os": "TrueNAS CORE", "storage": "16TB ZFS"}},
        {"key": "nas01/volume1/media", "kind": "storage", "name": "/volume1/media", "attrs": {"fs": "zfs", "source": "10.20.0.15:/volume1/media", "total": "12884901888", "used": "7730941132", "host": "nas01"}},
        {"key": "hv01/lxc/201", "kind": "guest", "name": "media", "attrs": {"vmid": 201, "ip": "10.20.0.21", "status": "running", "cores": 4, "memory_mb": 8192}},
        {"key": "hv01/lxc/202", "kind": "guest", "name": "apps", "attrs": {"vmid": 202, "ip": "10.20.0.22", "status": "running", "cores": 8, "memory_mb": 16384}},
        {"key": "hv01/lxc/201/mediaserver", "kind": "service", "name": "mediaserver", "attrs": {"image": "plexinc/pms-docker:latest", "status": "active", "version": "1.40.1"}},
        {"key": "hv01/lxc/201/requests", "kind": "service", "name": "requests", "attrs": {"image": "overseerr:latest", "status": "active"}},
        {"key": "hv01/lxc/202/atlas-api", "kind": "service", "name": "atlas-api", "attrs": {"runtime": "python-3.12", "port": 8080, "status": "active"}},
        {"key": "hv01/lxc/202/postgres", "kind": "service", "name": "postgres", "attrs": {"image": "postgres:16", "port": 5432, "status": "active"}},
        {"key": "url/http://10.20.0.21:32400", "kind": "endpoint", "name": "http://10.20.0.21:32400", "attrs": {"proto": "http", "port": 32400}},
        {"key": "url/http://10.20.0.21:5055", "kind": "endpoint", "name": "http://10.20.0.21:5055", "attrs": {"proto": "http", "port": 5055}},
        {"key": "url/http://10.20.0.22:8080", "kind": "endpoint", "name": "http://10.20.0.22:8080", "attrs": {"proto": "http", "port": 8080}},
        {"key": "url/http://10.20.0.22:5432", "kind": "endpoint", "name": "http://10.20.0.22:5432", "attrs": {"proto": "tcp", "port": 5432}},
        {"key": "tunnel/atlas", "kind": "tunnel", "name": "https://atlas.example.com", "attrs": {"provider": "cloudflare", "route": "http://10.20.0.22:8080"}},
    ]
    snap_rels = [
        {"src": "hv01/lxc/201", "rel": "runs_on", "dst": "hv01"},
        {"src": "hv01/lxc/202", "rel": "runs_on", "dst": "hv01"},
        {"src": "hv01/lxc/201", "rel": "mounts", "dst": "nas01/volume1/media", "attrs": {"guest_path": "/mnt/media", "host_path": "/volume1/media"}},
        {"src": "hv01/lxc/201/mediaserver", "rel": "runs_on", "dst": "hv01/lxc/201"},
        {"src": "hv01/lxc/201/requests", "rel": "runs_on", "dst": "hv01/lxc/201"},
        {"src": "hv01/lxc/202/atlas-api", "rel": "runs_on", "dst": "hv01/lxc/202"},
        {"src": "hv01/lxc/202/postgres", "rel": "runs_on", "dst": "hv01/lxc/202"},
        {"src": "hv01/lxc/201/mediaserver", "rel": "reachable_at", "dst": "url/http://10.20.0.21:32400"},
        {"src": "hv01/lxc/201/requests", "rel": "reachable_at", "dst": "url/http://10.20.0.21:5055"},
        {"src": "hv01/lxc/202/atlas-api", "rel": "reachable_at", "dst": "url/http://10.20.0.22:8080"},
        {"src": "hv01/lxc/202/postgres", "rel": "reachable_at", "dst": "url/http://10.20.0.22:5432"},
        {"src": "hv01/lxc/201/requests", "rel": "talks_to", "dst": "hv01/lxc/201/mediaserver"},
        {"src": "hv01/lxc/202/atlas-api", "rel": "talks_to", "dst": "hv01/lxc/202/postgres"},
        {"src": "hv01/lxc/202/atlas-api", "rel": "exposed_via", "dst": "tunnel/atlas"},
    ]
    I.ingest_snapshot(s, {"collector": "proxmox", "source": "pve", "at": now_t, "entities": snap_ents, "relations": snap_rels})

    # Memory-sourced parts with evidence
    atlas_api_id = I.resolve(s, "atlas-api")
    postgres_id = I.resolve(s, "postgres")
    mediaserver_id = I.resolve(s, "mediaserver")
    requests_id = I.resolve(s, "requests")

    sys_atlas = I.upsert_node(s, "system", "Atlas", note="Atlas API gateway and microservices platform", source="memory", evidence=[m_atlas], at=now_t)
    if atlas_api_id:
        I.upsert_edge(s, atlas_api_id, "part_of", sys_atlas, source="memory", evidence=[m_atlas], at=now_t)
        s.x("UPDATE infra_nodes SET evidence=? WHERE id=?", (json.dumps([m_atlas]), atlas_api_id))
    if postgres_id:
        I.upsert_edge(s, postgres_id, "part_of", sys_atlas, source="memory", evidence=[m_atlas], at=now_t)
        s.x("UPDATE infra_nodes SET evidence=? WHERE id=?", (json.dumps([m_atlas]), postgres_id))

    sys_media = I.upsert_node(s, "system", "Media", note="Home media streaming, transcoding and automated requests", source="memory", evidence=[m_media], at=now_t)
    if mediaserver_id:
        I.upsert_edge(s, mediaserver_id, "part_of", sys_media, source="memory", evidence=[m_media], at=now_t)
        s.x("UPDATE infra_nodes SET evidence=? WHERE id=?", (json.dumps([m_media]), mediaserver_id))
    if requests_id:
        I.upsert_edge(s, requests_id, "part_of", sys_media, source="memory", evidence=[m_media], at=now_t)
        s.x("UPDATE infra_nodes SET evidence=? WHERE id=?", (json.dumps([m_media]), requests_id))

    # Vault-like credential node atlas-db linked with uses_credential
    cred_id = I.upsert_node(s, "credential", "atlas-db", note="Postgres credentials for Atlas API", attrs={"service": "postgres", "vault": True}, source="vault", at=now_t)
    if atlas_api_id:
        I.upsert_edge(s, atlas_api_id, "uses_credential", cred_id, source="vault", at=now_t)

    # Attach evidence to stale runner
    stale_runner_id = I.resolve(s, "staging-runner")
    if stale_runner_id:
        s.x("UPDATE infra_nodes SET evidence=? WHERE id=?", (json.dumps([m_stale]), stale_runner_id))

    print(f"seeded {path}: {len(ids)} agents, job {jid}, atlas job {atlas_job}, infra map")


if __name__ == "__main__":
    seed(sys.argv[1])
