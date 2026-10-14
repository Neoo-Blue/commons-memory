"""Infrastructure map: snapshots, memory extraction joining discovered nodes, vault links, staleness, the agent text."""
import base64
import json
import os

from commons_memory import infra as I
from commons_memory.ids import now
from conftest import make_store


def _snap(at=None, with_radarr=True):
    ents = [{"key": "pve", "kind": "machine", "name": "pve", "attrs": {"ip": "192.168.50.202", "cpus": 16}},
            {"key": "pve/lxc/106", "kind": "guest", "name": "arrstack", "attrs": {"vmid": 106, "ip": "192.168.50.205", "status": "running"}},
            {"key": "pve/lxc/100", "kind": "guest", "name": "docker", "attrs": {"vmid": 100, "ip": "192.168.50.203"}},
            {"key": "pve/mount/mnt/library", "kind": "storage", "name": "/mnt/library", "attrs": {"fs": "nfs4", "source": "192.168.50.111:/volume1/library"}},
            {"key": "pve/lxc/100/docker/plex", "kind": "service", "name": "plex", "attrs": {"image": "plexinc/pms-docker"}},
            {"key": "url/http://192.168.50.203:32400", "kind": "endpoint", "name": "http://192.168.50.203:32400"}]
    rels = [{"src": "pve/lxc/106", "rel": "runs_on", "dst": "pve"}, {"src": "pve/lxc/100", "rel": "runs_on", "dst": "pve"},
            {"src": "pve/lxc/106", "rel": "mounts", "dst": "pve/mount/mnt/library", "attrs": {"guest_path": "/mnt/library"}},
            {"src": "pve/lxc/100/docker/plex", "rel": "runs_on", "dst": "pve/lxc/100"},
            {"src": "pve/lxc/100/docker/plex", "rel": "reachable_at", "dst": "url/http://192.168.50.203:32400"}]
    if with_radarr:
        ents += [{"key": "pve/lxc/106/docker/radarr", "kind": "service", "name": "radarr", "attrs": {"image": "linuxserver/radarr"}},
                 {"key": "url/http://192.168.50.205:7878", "kind": "endpoint", "name": "http://192.168.50.205:7878"}]
        rels += [{"src": "pve/lxc/106/docker/radarr", "rel": "runs_on", "dst": "pve/lxc/106"},
                 {"src": "pve/lxc/106/docker/radarr", "rel": "reachable_at", "dst": "url/http://192.168.50.205:7878"}]
    return {"collector": "proxmox", "source": "pve", "at": at or now(), "entities": ents, "relations": rels}


def test_snapshot_builds_layers_and_marks_what_disappears_stale(tmp_path):
    s = make_store(tmp_path, "i1.db")
    r = I.ingest_snapshot(s, _snap())
    assert r["nodes"] == 8 and r["edges"] == 7
    o = I.overview(s)
    pve = o["tree"][0]
    assert pve["name"] == "pve" and {c["name"] for c in pve["children"]} == {"arrstack (LXC 106)", "docker (LXC 100)"}
    arr = [c for c in pve["children"] if c["name"] == "arrstack (LXC 106)"][0]
    assert [c["name"] for c in arr["children"]] == ["radarr"]
    I.ingest_snapshot(s, _snap(at=now() + 60))                          # same snapshot again: no duplicates
    assert len(I.nodes(s)) == 8
    I.ingest_snapshot(s, _snap(at=now() + 120, with_radarr=False))     # radarr went away
    assert {n["name"] for n in I.nodes(s) if n["stale"]} == {"radarr", "http://192.168.50.205:7878"}


def test_memory_facts_join_discovered_nodes_by_vmid_ip_and_hostname(tmp_path):
    s = make_store(tmp_path, "i2.db")
    I.ingest_snapshot(s, _snap())
    m1 = s.add_item("memory", "Radarr runs in LXC 106 at http://192.168.50.205:7878, part of the Plex stack")["id"]
    m2 = s.add_item("memory", "plex-guard is a systemd timer on the Proxmox PVE host")["id"]
    notes = [dict(id=m1, created_at=now(), project=None, c=""), dict(id=m2, created_at=now(), project=None, c="")]
    out = {"entities": [{"name": "Radarr", "kind": "service", "note": "movies"},
                        {"name": "LXC 106 (192.168.50.205)", "kind": "guest", "note": "arr stack"},
                        {"name": "Plex", "kind": "system", "note": "media"},
                        {"name": "plex-guard", "kind": "service", "note": "kills 4K transcodes"},
                        {"name": "Proxmox PVE host", "kind": "machine", "note": "hypervisor"}],
           "relations": [{"src": "Radarr", "rel": "runs_on", "dst": "LXC 106 (192.168.50.205)", "evidence": [m1]},
                         {"src": "Radarr", "rel": "part_of", "dst": "Plex", "evidence": [m1]},
                         {"src": "plex-guard", "rel": "runs_on", "dst": "Proxmox PVE host", "evidence": [m2]},
                         {"src": "Radarr", "rel": "talks_to", "dst": "Plex", "evidence": ["not-in-batch"]}]}
    I.ingest_extraction(s, out, notes)
    names = {n["name"] for n in I.nodes(s)}
    assert "LXC 106 (192.168.50.205)" not in names and "Proxmox PVE host" not in names   # joined arrstack and pve
    assert sum(1 for n in I.nodes(s) if n["name"].lower() == "radarr") == 1               # joined the container
    radarr = I.node(s, I.resolve(s, "radarr"))
    assert [c["name"] for c in radarr["chain"]] == ["arrstack (LXC 106)", "pve"]
    assert {x["name"] for x in radarr["out"] if x["rel"] == "part_of"} == {"Plex"}
    assert not [x for x in radarr["out"] if x["rel"] == "talks_to"]                       # uncited links are dropped
    assert radarr["notes"] and radarr["notes"][0]["id"] == m1
    guard = I.node(s, I.resolve(s, "plex-guard"))
    assert [c["name"] for c in guard["chain"]] == ["pve"]
    assert I.resolve(s, "LXC 106") == I.resolve(s, "192.168.50.205") == I.resolve(s, "arrstack")


def test_vault_credentials_attach_by_url_or_name_and_never_carry_values(tmp_path, monkeypatch):
    from commons_memory import vault as V
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    s = make_store(tmp_path, "i3.db")
    I.ingest_snapshot(s, _snap())
    V.add(s, "radarr", "secret-radarr-key-123", "radarr", ["http://192.168.50.205:7878"], {"header": "X-Api-Key"})
    V.add(s, "plex", "secret-plex-token-456", "plex", ["http://192.168.50.203:32400"], {"query": "X-Plex-Token"})
    assert I.link_vault(s) == 2
    text = I.describe(s, "radarr")
    assert "Uses credential: radarr" in text and "secret-radarr" not in text
    assert "secret" not in str(I.graph(s))


def test_describe_overview_and_one_node_for_agents(tmp_path):
    s = make_store(tmp_path, "i4.db")
    assert "empty" in I.describe(s)
    I.ingest_snapshot(s, _snap())
    over = I.describe(s)
    assert "- pve (machine)" in over and "  - arrstack (LXC 106) (guest)" in over and "    - radarr (service)" in over
    one = I.describe(s, "LXC 106")
    assert one.startswith("arrstack (LXC 106) (guest)") and "Runs on: pve" in one and "Hosts: radarr" in one
    assert "Mounts: /mnt/library" in one
    assert "Nothing in the infrastructure map" in I.describe(s, "no-such-thing")


def test_owner_corrections_reject_links_merge_and_hide(tmp_path):
    s = make_store(tmp_path, "i5.db")
    I.ingest_snapshot(s, _snap())
    radarr = I.node(s, I.resolve(s, "radarr"))
    edge = [x for x in radarr["out"] if x["rel"] == "reachable_at"][0]["edge"]
    I.verdict(s, edge, False)
    assert not [x for x in I.node(s, radarr["id"])["out"] if x["rel"] == "reachable_at"]
    twin = I.upsert_node(s, "service", "Radarr movies", source="memory")
    I.merge_nodes(s, radarr["id"], twin)
    assert I.resolve(s, "Radarr movies") is None or I.resolve(s, "Radarr movies") == radarr["id"]
    I.hide(s, radarr["id"])
    assert "radarr" not in {n["name"] for n in I.nodes(s)}


def test_extract_dry_run_counts_candidate_notes_without_a_model(tmp_path):
    s = make_store(tmp_path, "i6.db")
    s.add_item("memory", "Poise runs on LXC 108 (192.168.50.213) behind the Cloudflare tunnel at phrase.cooli.ai")
    s.add_item("memory", "I like my coffee black and strong, nothing about servers here at all, just coffee")
    est = I.extract(s, dry_run=True)
    assert est["notes"] == 1 and est["batches"] == 1 and est["estimate_usd"] > 0


def test_snapshot_route_needs_an_agent_key_and_merges(tmp_path):
    from starlette.testclient import TestClient
    from commons_memory.agent_http import build_app
    from commons_memory.auth import AuthMiddleware
    s = make_store(tmp_path, "i7.db")
    key = s.register_agent("collector-mac")["api_key"]
    c = TestClient(AuthMiddleware(build_app(s), s, resource_url="http://testserver", allow_anon_reads=False))
    assert c.post("/agent/infra", json={"snapshots": [_snap()]}).status_code == 401
    r = c.post("/agent/infra", json={"snapshots": [_snap()]}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.json()["merged"][0]["nodes"] == 8


def test_dashboard_api_and_project_view(tmp_path):
    from commons_memory.dashboard import API, OWNER_ONLY
    s = make_store(tmp_path, "i8.db")
    I.ingest_snapshot(s, _snap())
    m = s.add_item("memory", "Radarr in LXC 106 serves the media project", project="media")["id"]
    I.ingest_extraction(s, {"entities": [{"name": "Radarr", "kind": "service", "note": ""}, {"name": "Plex", "kind": "system", "note": ""}],
                            "relations": [{"src": "Radarr", "rel": "part_of", "dst": "Plex", "evidence": [m]}]},
                        [dict(id=m, created_at=now(), project="media", c="")])
    api = API(s)
    assert api.infra_overview({})["tree"][0]["name"] == "pve"
    assert "arrstack" in api.infra_describe({"q": "192.168.50.205"})["text"]
    assert api.infra_graph({"root": "radarr", "depth": "1"})["nodes"]
    pv = api.infra_project({"id": "media"})
    assert pv["systems"][0]["name"] == "Plex" and "radarr" in {x["name"].lower() for x in pv["services"]}
    assert any("/api/infra/extract".startswith(p) for p in OWNER_ONLY)                     # it spends model credit
    assert api.infra_estimate({})["notes"] == 1


def test_review_fixes_identity_staleness_merge_vault_and_ownership(tmp_path, monkeypatch):
    import copy
    import pytest
    from commons_memory import vault as V
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    s = make_store(tmp_path, "i9.db")
    t0 = now()
    I.ingest_snapshot(s, _snap(at=t0), by="agent-a")
    # a URL resolves to the endpoint, not the guest that owns its IP
    assert I.node(s, I.resolve(s, "http://192.168.50.205:7878"))["kind"] == "endpoint"
    # another agent can't report (or stale out) the same source
    with pytest.raises(PermissionError):
        I.ingest_snapshot(s, _snap(at=t0 + 1, with_radarr=False), by="agent-b")
    # a second Proxmox host with its own LXC 106 stays a separate guest
    other = copy.deepcopy(_snap(at=t0))
    other["source"] = "pve2"
    for e in other["entities"]:
        e["attrs"] = {k: v for k, v in (e.get("attrs") or {}).items() if k not in ("ip",)}
        if e["kind"] == "machine":
            e["name"], e["attrs"]["ip"] = "pve2", "10.9.9.9"
    I.ingest_snapshot(s, other, by="agent-c")
    assert len([n for n in I.nodes(s, kind="guest") if n["attrs"].get("vmid") == 106]) == 2
    # radarr disappears from pve: stale; an old note can't revive it; replaying an older snapshot changes nothing
    I.ingest_snapshot(s, _snap(at=t0 + 60, with_radarr=False), by="agent-a")
    rid = [n for n in I.nodes(s) if n["name"] == "radarr" and n["stale"]][0]["id"]
    I.upsert_node(s, "service", "radarr", source="memory", keys=[f"svc:radarr@{I.node(s, rid)['chain'][0]['id']}"])
    assert I.node(s, rid)["stale"]
    I.ingest_snapshot(s, _snap(at=t0 - 600, with_radarr=False), by="agent-a")
    assert not [n for n in I.nodes(s) if n["name"] == "plex" and n["stale"]]
    # same-as keeps every bind and a rejection
    lxc = I.resolve(s, "arrstack")
    twin = I.upsert_node(s, "guest", "arr box", source="memory")
    store_id = [n["id"] for n in I.nodes(s, kind="storage")][0]
    I.upsert_edge(s, twin, "mounts", store_id, source="memory", attrs={"host_path": "/mnt/extra", "guest_path": "/extra"})
    e = [x for x in I.node(s, lxc)["out"] if x["rel"] == "mounts"][0]
    I.verdict(s, e["edge"], False)
    I.merge_nodes(s, lxc, twin)
    edges = s.q("SELECT attrs, verdict FROM infra_edges WHERE src=? AND rel='mounts'", (lxc,))
    assert len(edges) == 1 and edges[0]["verdict"] == "no" and len(json.loads(edges[0]["attrs"])["binds"]) == 2
    # vault: exact host and port, never a longer IP or another port on the host
    V.add(s, "short-ip", "x-secret-1", "svc", ["http://192.168.50.20:7878"], {"bearer": True})
    V.add(s, "plex-api", "x-secret-2", "plex", ["http://192.168.50.203:32400"], {"bearer": True})
    I.link_vault(s)
    creds = {x["name"] for x in I.node(s, I.resolve(s, "plex"))["out"] if x["rel"] == "uses_credential"}
    assert creds == {"plex-api"}


def test_extract_async_runs_once_and_failed_batches_are_read_again(tmp_path, monkeypatch):
    s = make_store(tmp_path, "i10.db")
    s.add_item("memory", "Poise runs on LXC 108 (192.168.50.213) behind the tunnel at phrase.cooli.ai, deployed by deploy.sh")
    gate = __import__("threading").Event()
    calls = []
    def fake_extract(store, model=None, since=None, limit=None, dry_run=False, log=lambda m: None):
        if dry_run:
            return {"notes": 1, "batches": 1, "estimate_usd": 0.07}
        calls.append(1)
        gate.wait(2)
        return {"notes": 1, "batches": 1, "cost": 0.0}
    monkeypatch.setattr(I, "extract", fake_extract)
    a = I.extract_async(s)
    b = I.extract_async(s)                                   # a second click while the first runs
    gate.set()
    __import__("time").sleep(0.3)
    assert b["status"] == "running" and len(calls) == 1      # the first call's dict is live state, done by now


def test_stats_related_and_the_graph_explorer_source(tmp_path):
    from commons_memory.dashboard import API
    s = make_store(tmp_path, "i11.db")
    I.ingest_snapshot(s, _snap())
    m = s.add_item("memory", "Radarr feeds the media library", project="media")["id"]
    I.ingest_extraction(s, {"entities": [{"name": "radarr", "kind": "service", "note": ""}, {"name": "Media", "kind": "system", "note": ""}],
                            "relations": [{"src": "radarr", "rel": "part_of", "dst": "Media", "evidence": [m]}]},
                        [dict(id=m, created_at=now(), project="media", c="")])
    st = API(s).infra_stats({})
    pve = st["machines"][0]
    assert pve["name"] == "pve" and {g["name"] for g in pve["guests"]} == {"arrstack (LXC 106)", "docker (LXC 100)"}
    assert [g for g in pve["guests"] if g["name"].startswith("arrstack")][0]["ports"] == 1
    r = I.node(s, I.resolve(s, "radarr"))
    assert r["projects"][0]["title"] == "media" and r["siblings"] == []
    assert I.node(s, I.resolve(s, "plex"))["siblings"] == []
    import commons_memory.graphview as gv
    html = gv.render({"nodes": {n["id"]: {"name": n["name"], "type": n["kind"]} for n in I.graph(s)["nodes"]},
                      "edges": [{"src": e["src"], "dst": e["dst"], "type": e["rel"]} for e in I.graph(s)["edges"]]}, "Infrastructure")
    assert "arrstack (LXC 106)" in html and "machine: '#f97316'" in html
