"""The infrastructure map: every place, machine, guest, service, endpoint, store, repo and credential the team knows
about, in layers, with how they connect, for people (Infrastructure page, project System tab) and agents (infra()).

Three sources feed one graph and each fact keeps where it came from:
- discovery snapshots (`commons infra collect` on a machine that can reach the hosts; exact and timestamped),
- memories (a small model extracts what notes state as current fact; every link cites its notes),
- the vault (a credential attaches to the services its base URLs or name point at; names only, never values).
Discovery outranks memory for the same thing; whatever a source stops reporting is marked stale, never silently kept.
Identity is the hard part: the same LXC is "LXC 106", "CT 106", "arrstack" and 192.168.50.205 depending on who wrote it,
so every node carries keys (vmid, IP, URL, normalised name) and an incoming fact joins the node that shares a key.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict

from .ids import iso, new_id, now
from .store import CommonsError, Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS infra_nodes(id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, note TEXT, attrs TEXT,
  sources TEXT, evidence TEXT, first_seen REAL, last_seen REAL, stale INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS infra_edges(id TEXT PRIMARY KEY, src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL,
  attrs TEXT, sources TEXT, evidence TEXT, first_seen REAL, last_seen REAL, stale INTEGER DEFAULT 0,
  verdict TEXT, UNIQUE(src, rel, dst));
CREATE TABLE IF NOT EXISTS infra_keys(key TEXT PRIMARY KEY, node TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS infra_edges_dst ON infra_edges(dst);
"""
# layer order is the map's vertical order: where things live at the top, what they hold at the bottom
KINDS = {"place": 0, "network": 0, "machine": 1, "guest": 2, "service": 3, "system": 3, "endpoint": 4, "tunnel": 4,
         "storage": 5, "database": 5, "repo": 6, "directory": 6, "credential": 7}
RELS = ("part_of", "runs_on", "exposed_via", "reachable_at", "stores_in", "mounts", "talks_to", "deploys_from", "dev_at",
        "uses_credential", "backs_up_to", "member_of")
LABEL = {"part_of": "part of", "runs_on": "runs on", "exposed_via": "exposed via", "reachable_at": "reachable at",
         "stores_in": "stores data in", "mounts": "mounts", "talks_to": "talks to", "deploys_from": "deploys from",
         "dev_at": "developed at", "uses_credential": "uses credential", "backs_up_to": "backs up to", "member_of": "member of"}
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
DOCKER_GW = re.compile(r"^172\.(1[6-9]|2\d|3[01])\.0\.1$")  # every docker host has one: never an identity
VMID = re.compile(r"\b(?:lxc|ct|vm|container)\s*#?\s*(\d{2,5})\b", re.I)


def ensure(s: Store) -> None:
    if not getattr(s, "_infra_ok", False):
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                s.x(stmt)
        s._infra_ok = True


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _keys(kind: str, name: str, attrs: dict, extra: list[str] = ()) -> list[str]:
    """Identity keys, strongest first. A service's name is only unique on its host, so it gets no bare-name key."""
    ks = list(extra)
    # the primary IP and the name only: every docker host also has 172.17.0.1, so a full address list would merge
    # unrelated guests into one
    text = f"{name} {attrs.get('ip') or ''}"
    if kind in ("machine", "guest"):
        if attrs.get("vmid") or (kind == "guest" and VMID.search(name or "")):
            ks.append(f"vmid:{attrs.get('vmid') or VMID.search(name).group(1)}")
        ks += [f"ip:{a}" for a in dict.fromkeys(IP.findall(text)) if not a.startswith(("127.", "0.")) and not DOCKER_GW.match(a)]
    if kind == "endpoint":
        ks.append("url:" + (name or "").lower().rstrip("/"))
    if kind in ("storage", "directory"):
        ks.append(f"path:{attrs.get('host', '')}:{(name or '').rstrip('/')}")
    if kind == "credential":
        ks.append("cred:" + _norm(name.removeprefix("credential ")))
    if kind not in ("service", "storage", "directory", "endpoint"):
        ks.append(f"{kind}:{_norm(name)}")
    return [k for k in dict.fromkeys(ks) if k and not k.endswith(":")]


WEAK = ("svcname:",) + tuple(f"{k}:" for k in KINDS)  # a bare name: two guests can both be called "docker"


def _find(s: Store, keys: list[str]) -> str | None:
    """Match on strong keys (snapshot key, vmid, IP, URL, path, service@host, credential) when there are any; a bare
    name only identifies something that has nothing stronger."""
    strong = [k for k in keys if not k.startswith(WEAK)]
    for k in strong or keys:
        r = s.q("SELECT node FROM infra_keys WHERE key=?", (k,))
        if r and s.q("SELECT 1 FROM infra_nodes WHERE id=?", (r[0]["node"],)):
            return r[0]["node"]
    return None


def _merge_list(old: str | None, new: list) -> str:
    cur = json.loads(old or "[]")
    return json.dumps(list(dict.fromkeys(cur + [x for x in new if x])))


def upsert_node(s: Store, kind: str, name: str, *, attrs: dict | None = None, note: str = "", source: str,
                evidence: list[str] = (), keys: list[str] = (), at: float | None = None, host: str | None = None,
                find: list[str] | None = None) -> str:
    """Join the node that shares a key, or make one. Discovery names win over memory names; attrs merge (newer wins).
    find: match only on these keys (a snapshot's guest matches by its own host's vmid, never another host's LXC 100);
    every key is still registered for lookups by name, vmid or IP."""
    if kind not in KINDS:
        raise CommonsError(f"unknown kind {kind!r}")
    ensure(s)
    attrs, at = dict(attrs or {}), at or now()
    ks = _keys(kind, name, attrs, list(keys) + ([f"svc:{_norm(name)}@{host}"] if kind == "service" and host else []))
    nid = _find(s, find if find is not None else ks)
    with s.tx():
        if nid:
            r = s.q("SELECT * FROM infra_nodes WHERE id=?", (nid,))[0]
            merged = json.loads(r["attrs"] or "{}") | attrs
            disc = source.startswith(("discovery", "vault"))
            s.x("UPDATE infra_nodes SET name=?, note=?, attrs=?, sources=?, evidence=?, last_seen=?, stale=? WHERE id=?",
                (name if disc or not json.loads(r["sources"] or "[]") else r["name"], note or r["note"],
                 json.dumps(merged), _merge_list(r["sources"], [source]), _merge_list(r["evidence"], list(evidence)),
                 max(at, r["last_seen"] or 0), 0 if source.startswith("discovery") else r["stale"], nid))
        else:
            nid = new_id("inf")
            s.x("INSERT INTO infra_nodes(id,kind,name,note,attrs,sources,evidence,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                (nid, kind, name[:200], note[:500], json.dumps(attrs), json.dumps([source]), json.dumps(list(evidence)), at, at))
        for k in ks:
            s.x("INSERT OR IGNORE INTO infra_keys VALUES(?,?)", (k, nid))
    return nid


def upsert_edge(s: Store, src: str, rel: str, dst: str, *, source: str, evidence: list[str] = (), attrs: dict | None = None,
                at: float | None = None) -> str | None:
    if rel not in RELS or src == dst:
        return None
    at, attrs = at or now(), dict(attrs or {})
    bind = {k: attrs.pop(k) for k in ("host_path", "guest_path", "container_path", "read_only") if k in attrs}
    if bind:  # one container can bind several paths from the same guest: keep them all on the one link
        attrs["binds"] = [bind]
    r = s.q("SELECT * FROM infra_edges WHERE src=? AND rel=? AND dst=?", (src, rel, dst))
    if r:
        r = r[0]
        old = json.loads(r["attrs"] or "{}")
        if bind:
            attrs["binds"] = [b for b in old.get("binds", []) if b != bind] + [bind]
        s.x("UPDATE infra_edges SET attrs=?, sources=?, evidence=?, last_seen=?, stale=0 WHERE id=?",
            (json.dumps(old | attrs), _merge_list(r["sources"], [source]),
             _merge_list(r["evidence"], list(evidence)), max(at, r["last_seen"] or 0), r["id"]))
        return r["id"]
    eid = new_id("ine")
    s.x("INSERT INTO infra_edges(id,src,rel,dst,attrs,sources,evidence,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
        (eid, src, rel, dst, json.dumps(attrs or {}), json.dumps([source]), json.dumps(list(evidence)), at, at))
    return eid


# ------------------------------------------------------------------------------------------------ discovery
def ingest_snapshot(s: Store, snap: dict, by: str | None = None) -> dict:
    """Merge one collector snapshot ({collector, source, at, entities, relations}) and mark what that source no longer
    reports as stale (a guest that was deleted, a container that went away)."""
    ensure(s)
    src = f"discovery:{snap.get('collector', '?')}:{snap.get('source', '?')}"
    at = float(snap.get("at") or now())
    if by:  # the first agent to report a source owns it: another key can't overwrite or stale out someone's map
        own = s.q("SELECT value FROM meta WHERE key=?", (f"infra:owner:{src}",))
        if own and own[0]["value"] != by:
            raise PermissionError(f"{src} is reported by another agent's collector")
        s.x("INSERT OR IGNORE INTO meta VALUES(?, ?)", (f"infra:owner:{src}", by))
    prev = s.q("SELECT value FROM meta WHERE key=?", (f"infra:last:{src}",))
    newest = not prev or at >= json.loads(prev[0]["value"]).get("at", 0)
    ids: dict[str, str] = {}
    hosts: dict[str, str] = {r["src"]: r["dst"] for r in snap.get("relations", []) if r.get("rel") == "runs_on"}
    kinds = {e["key"]: e["kind"] for e in snap.get("entities", [])}
    for e in sorted(snap.get("entities", []), key=lambda e: KINDS.get(e.get("kind"), 9)):  # hosts before what runs on them
        kind = {"network": "network", "tunnel": "tunnel"}.get(e["kind"], e["kind"])
        if kind not in KINDS:
            continue
        host = ids.get(hosts.get(e["key"], "")) if kind == "service" else None
        attrs = dict(e.get("attrs") or {})
        if kind in ("machine", "guest") and not attrs.get("ip"):  # a primary LAN address, for lookups and the map
            lan = [x for x in attrs.get("ips") or [] if IP.fullmatch(str(x)) and not DOCKER_GW.match(x)
                   and not re.match(r"172\.(1[6-9]|2\d|3[01])\.|127\.", x)]
            if lan:
                attrs["ip"] = lan[0]
        if kind in ("storage", "directory") and hosts.get(e["key"]):
            attrs.setdefault("host", ids.get(hosts[e["key"]], ""))
        name = e.get("name") or e["key"]
        if kind == "guest" and attrs.get("vmid") and str(attrs["vmid"]) not in name:  # two guests can share a name
            name = f"{name} ({'VM' if attrs.get('type') in ('qemu', 'vm') else 'LXC'} {attrs['vmid']})"
        dkey = f"dkey:{snap.get('source')}:{e['key']}"
        machine = ids.get(hosts.get(e["key"], "")) if kind == "guest" else None
        scoped = [f"vmid@{machine}:{attrs['vmid']}"] if machine and attrs.get("vmid") else []
        strong = [k for k in _keys(kind, name, attrs) if k.startswith(("ip:", "url:", "path:"))]
        ids[e["key"]] = upsert_node(s, kind, name, attrs=attrs, source=src, at=at, host=host, keys=[dkey, *scoped],
                                    find=[dkey, *scoped, *strong, *([f"svc:{_norm(name)}@{host}"] if host else [])])
    edges = 0
    for r in snap.get("relations", []):
        a, b = ids.get(r.get("src")), ids.get(r.get("dst"))
        if a and b and upsert_edge(s, a, r.get("rel"), b, source=src, attrs=r.get("attrs"), at=at):
            edges += 1
    seen = set(ids.values())
    stale = 0
    if newest:  # a replayed older snapshot must not stale out what a newer one found
        for n in s.q("SELECT id FROM infra_nodes WHERE sources LIKE ? AND stale=0", (f'%"{src}"%',)):
            if n["id"] not in seen:  # this collector reported it before and no longer does, whatever notes say
                s.x("UPDATE infra_nodes SET stale=1 WHERE id=?", (n["id"],))
                stale += 1
        s.x("UPDATE infra_edges SET stale=1 WHERE sources LIKE ? AND last_seen<?", (f'%"{src}"%', at))
    _reconcile(s)
    if newest:
        s.x("INSERT OR REPLACE INTO meta VALUES(?, ?)", (f"infra:last:{src}", json.dumps({"at": at, "nodes": len(ids), "kinds": len(set(kinds.values()))})))
    s.audit(by, "infra.snapshot", src, {"nodes": len(ids), "edges": edges, "stale": stale})  # who sent it, for tracing
    return {"source": src, "nodes": len(ids), "edges": edges, "stale": stale}


# ------------------------------------------------------------------------------------------------ memory extraction
NOTE_HINT = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\blxc\b|\bct ?\d|\bdocker\b|\bcontainer\b|\bport\b|\bdomain\b|"
                       r"\btunnel\b|\bmount|\bnfs\b|/mnt/|/volume\d|~/|\bdeploy|\bssh\b|systemd|postgres|redis|\bbucket\b|"
                       r"worker|wrangler|cloudflare|tailscale|nginx|caddy|proxmox|\bpve\b|synology|\bnas\b|\bvm\b|\.com\b|"
                       r"\.ai\b|\.wtf\b|https?://", re.I)
EXTRACT_SYS = """You build a map of one person's software systems and infrastructure from their notes. Extract only what the
notes state as current fact: what each service runs on (host, LXC, VM, container, Cloudflare Worker), how it is reached
(domain, tunnel, Access, port), where its data lives (paths, mounts, NFS shares, databases, buckets), what it talks to,
where its code and dev environment are, and which named credential it uses (a name only, never a secret value).
Kinds: place (a site or cloud account), network, machine (a physical host or cloud VM), guest (an LXC or VM; name it like
"LXC 106 (192.168.50.205)"), service, system (a product made of services, e.g. "Plex" or "Poise"), endpoint (a URL or
domain), tunnel, storage, database, repo, directory, credential. Use specific, stable names as written. Each relation
cites the ids of the notes that state it. Skip chat chatter, plans, test reports and anything a later note says changed;
when notes disagree, the newest wins. Link each service to its system with part_of."""
EXTRACT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["entities", "relations"], "properties": {
    "entities": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "kind", "note"],
                 "properties": {"name": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
                                "note": {"type": "string"}}}},
    "relations": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                  "required": ["src", "rel", "dst", "evidence"],
                  "properties": {"src": {"type": "string"}, "rel": {"type": "string", "enum": list(RELS)},
                                 "dst": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}}}}}}


STRONG = re.compile(r"\blxc\b|\bct ?\d{2,4}\b|proxmox|\bpve\b|synology|\bnas\b|cloudflare|tunnel|tailscale|wrangler|"
                    r"\bvps\b|\bdroplet\b|vercel|systemd|docker compose|deploy\.sh|/mnt/|/volume\d", re.I)


def _anchors(s: Store) -> re.Pattern | None:
    """Once discovery has run, a note is worth reading when it names something discovered (an IP, a guest, a service, a
    tunnel hostname) or clearly talks about infrastructure; the rest of the hint matches were mostly noise."""
    names = set()
    for r in s.q("SELECT kind, name, attrs FROM infra_nodes WHERE hidden=0 AND sources LIKE '%discovery%'"):
        a = json.loads(r["attrs"] or "{}")
        if a.get("ip"):
            names.add(re.escape(a["ip"]))
        base = re.sub(r"\s*\((?:LXC|VM) \d+\)$", "", r["name"])
        if r["kind"] in ("guest", "service", "machine") and len(base) >= 4 and not base.isdigit():
            names.add(r"\b" + re.escape(base.lower()) + r"\b")
        if r["kind"] in ("endpoint", "tunnel") and not IP.search(r["name"]) and "." in r["name"]:
            names.add(re.escape(re.sub(r"^https?://", "", r["name"]).split("/")[0].lower()))
    return re.compile("|".join(sorted(names, key=len, reverse=True)), re.I) if names else None


def candidates(s: Store, since: float = 0, limit: int | None = None) -> list[dict]:
    """Notes worth reading for the map: facts, decisions, documents and memories that mention hosts, ports, paths, domains
    or the tools that run them (and, once discovery has run, name something it found), newest first, one copy each."""
    out, seen = [], set()
    anchors = _anchors(s)
    for r in s.q("SELECT i.id, i.kind, i.created_at, p.name project, substr(COALESCE(i.content, i.summary), 1, 900) c FROM items i "
                 "LEFT JOIN projects p ON p.id=i.project_id WHERE i.superseded_by IS NULL AND i.created_at>? AND "
                 "i.kind IN ('memory','decision','fact','document','handoff','note') ORDER BY i.created_at DESC", (since,)):
        c = r["c"] or ""
        k = _norm(c[:160])
        if len(c) < 40 or k in seen or not NOTE_HINT.search(c):
            continue
        if anchors and not (anchors.search(c) or STRONG.search(c)):
            continue
        seen.add(k)
        out.append(dict(r))
        if limit and len(out) >= limit:
            break
    return out


def _batches(notes: list[dict], chars: int = 22000) -> list[list[dict]]:
    """Group by project so one call sees a system's notes together, then cut to the size budget."""
    by: dict[str, list[dict]] = defaultdict(list)
    for n in notes:
        by[n["project"] or ""].append(n)
    out = []
    for group in sorted(by.values(), key=len, reverse=True):
        cur, size = [], 0
        for n in group:
            if cur and size + len(n["c"]) > chars:
                out.append(cur)
                cur, size = [], 0
            cur.append(n)
            size += len(n["c"]) + 40
        if cur:
            out.append(cur)
    return out


def _kind_of(kind: str, name: str) -> str:
    if kind == "machine" and VMID.search(name or ""):
        return "guest"
    return kind


def ingest_extraction(s: Store, out: dict, notes: list[dict]) -> dict:
    """Merge one model answer. Names resolve through identity keys, so "LXC 106 (192.168.50.205)" from a note joins the
    discovered arrstack guest at that IP; evidence ids that aren't in this batch are dropped."""
    ensure(s)
    valid = {n["id"] for n in notes}
    when = {n["id"]: n["created_at"] for n in notes}
    ids, kinds = {}, {}
    for e in out.get("entities", []):
        kinds[e["name"]] = _kind_of(e["kind"], e["name"])
    hosts = {r["src"]: r["dst"] for r in out.get("relations", []) if r.get("rel") == "runs_on"}
    for name, kind in sorted(kinds.items(), key=lambda kv: KINDS.get(kv[1], 9)):
        note = next((e.get("note", "") for e in out["entities"] if e["name"] == name), "")
        host = ids.get(hosts.get(name, "")) if kind == "service" else None
        keys = _service_keys(s, name, host) if kind == "service" else _host_alias(s, kind, name) if kind in ("machine", "guest") else []
        ids[name] = upsert_node(s, kind, name, note=note, source="memory", host=host, keys=keys)
    n_e = 0
    for r in out.get("relations", []):
        a, b = ids.get(r["src"]), ids.get(r["dst"])
        ev = [x for x in r.get("evidence", []) if x in valid]
        if a and b and ev and upsert_edge(s, a, r["rel"], b, source="memory", evidence=ev, at=max(when[x] for x in ev)):
            n_e += 1
            for nid in (a, b):
                s.x("UPDATE infra_nodes SET evidence=? WHERE id=?",
                    (_merge_list(s.q("SELECT evidence FROM infra_nodes WHERE id=?", (nid,))[0]["evidence"], ev), nid))
    return {"nodes": len(ids), "edges": n_e}


def _service_keys(s: Store, name: str, host: str | None) -> list[str]:
    """A note's "Radarr" joins the discovered radarr container when exactly one service by that name exists (or the one
    on the stated host); otherwise it stays its own node until a snapshot or the owner links it."""
    if host:
        return [f"svc:{_norm(name)}@{host}"]
    hits = s.q("SELECT key, node FROM infra_keys WHERE key LIKE ?", (f"svc:{_norm(name)}@%",))
    return [hits[0]["key"]] if len({r["node"] for r in hits}) == 1 else [f"svcname:{_norm(name)}"]


def _host_alias(s: Store, kind: str, name: str) -> list[str]:
    """A note says "Proxmox PVE host" or "the NAS (aerkin)"; a snapshot calls it "pve". When exactly one discovered
    machine or guest of that kind has a name that appears as a whole word in the note's name, use its key."""
    words = set(re.findall(r"[a-z0-9][a-z0-9-]*", (name or "").lower()))
    hits = [r["id"] for r in s.q("SELECT id, name, sources FROM infra_nodes WHERE kind=? AND sources LIKE '%discovery%'", (kind,))
            if r["name"].lower() in words]
    return [f"{kind}:{_norm(s.q('SELECT name FROM infra_nodes WHERE id=?', (hits[0],))[0]['name'])}"] if len(hits) == 1 else []


def merge_nodes(s: Store, keep: str, drop: str) -> dict:
    """Two nodes are the same thing: move every link, key, source and note of drop onto keep."""
    ensure(s)
    if keep == drop:
        return {"ok": True}
    a = s.q("SELECT * FROM infra_nodes WHERE id=?", (keep,))
    b = s.q("SELECT * FROM infra_nodes WHERE id=?", (drop,))
    if not a or not b:
        raise CommonsError("unknown infrastructure node")
    a, b = a[0], b[0]
    with s.tx():
        for e in s.q("SELECT * FROM infra_edges WHERE src=? OR dst=?", (drop, drop)):
            src, dst = (keep if e["src"] == drop else e["src"]), (keep if e["dst"] == drop else e["dst"])
            ex = s.q("SELECT * FROM infra_edges WHERE src=? AND rel=? AND dst=? AND id<>?", (src, e["rel"], dst, e["id"]))
            if src == dst:
                s.x("DELETE FROM infra_edges WHERE id=?", (e["id"],))
            elif ex:  # the kept node already has this link: fold the two, keeping every bind and a rejection
                x, ea, xa = ex[0], json.loads(e["attrs"] or "{}"), json.loads(ex[0]["attrs"] or "{}")
                binds = xa.get("binds", []) + [b for b in ea.get("binds", []) if b not in xa.get("binds", [])]
                verdict = "no" if "no" in (x["verdict"], e["verdict"]) else (x["verdict"] or e["verdict"])
                s.x("UPDATE infra_edges SET attrs=?, sources=?, evidence=?, verdict=?, last_seen=? WHERE id=?",
                    (json.dumps(ea | xa | ({"binds": binds} if binds else {})), _merge_list(x["sources"], json.loads(e["sources"] or "[]")),
                     _merge_list(x["evidence"], json.loads(e["evidence"] or "[]")), verdict, max(x["last_seen"] or 0, e["last_seen"] or 0), x["id"]))
                s.x("DELETE FROM infra_edges WHERE id=?", (e["id"],))
            else:
                s.x("UPDATE infra_edges SET src=?, dst=? WHERE id=?", (src, dst, e["id"]))
        for r in s.q("SELECT key FROM infra_keys WHERE key LIKE ?", (f"%@{drop}",)):  # services named by their host
            s.x("INSERT OR IGNORE INTO infra_keys VALUES(?,?)", (r["key"][: -len(drop)] + keep,
                s.q("SELECT node FROM infra_keys WHERE key=?", (r["key"],))[0]["node"]))
            s.x("DELETE FROM infra_keys WHERE key=?", (r["key"],))
        s.x("UPDATE infra_keys SET node=? WHERE node=?", (keep, drop))
        s.x("UPDATE infra_nodes SET attrs=?, sources=?, evidence=?, note=? WHERE id=?",
            (json.dumps(json.loads(b["attrs"] or "{}") | json.loads(a["attrs"] or "{}")),
             _merge_list(a["sources"], json.loads(b["sources"] or "[]")), _merge_list(a["evidence"], json.loads(b["evidence"] or "[]")),
             a["note"] or b["note"], keep))
        s.x("DELETE FROM infra_nodes WHERE id=?", (drop,))
    s.audit(None, "infra.merge", keep, {"dropped": drop, "name": b["name"]})
    return {"ok": True, "id": keep}


def _reconcile(s: Store) -> int:
    """After a snapshot: a note-only service ("svcname:radarr") folds into the one discovered service of that name."""
    n = 0
    for r in s.q("SELECT key, node FROM infra_keys WHERE key LIKE 'svcname:%'"):
        hits = {x["node"] for x in s.q("SELECT node FROM infra_keys WHERE key LIKE ?", (f"svc:{r['key'][8:]}@%",))}
        if len(hits) == 1 and r["node"] not in hits and s.q("SELECT 1 FROM infra_nodes WHERE id=?", (r["node"],)):
            merge_nodes(s, next(iter(hits)), r["node"])
            n += 1
    return n


def known_names(s: Store, limit: int = 400) -> str:
    """What live discovery already found, so a note's "Plex Media Server" comes back as the discovered "plex"."""
    rows = s.q("SELECT n.name, n.kind, n.attrs, (SELECT h.name FROM infra_edges e JOIN infra_nodes h ON h.id=e.dst "
               "WHERE e.src=n.id AND e.rel='runs_on' LIMIT 1) host FROM infra_nodes n WHERE n.hidden=0 AND n.stale=0 AND "
               "n.sources LIKE '%discovery%' AND n.kind IN ('machine','guest','service') ORDER BY n.kind, n.name LIMIT ?", (limit,))
    if not rows:
        return ""
    def one(r):
        a = json.loads(r["attrs"] or "{}")
        extra = ", ".join(str(x) for x in (f"vmid {a['vmid']}" if a.get("vmid") else None, a.get("ip")) if x)
        return f"- {r['name']} ({r['kind']}{', ' + extra if extra else ''}{', on ' + r['host'] if r['host'] else ''})"
    return ("Already discovered live (use these exact names when a note refers to one of them, e.g. a note's "
            "'Plex Media Server' running in Docker on LXC 100 is the service 'plex'):\n" + "\n".join(one(r) for r in rows) + "\n\n")


def extract(s: Store, model: str | None = None, since: float | None = None, limit: int | None = None,
            dry_run: bool = False, log=lambda m: None) -> dict:
    """Read candidate notes in batches and merge what the model finds. dry_run: count notes and batches, estimate cost."""
    from .providers import ProviderLayer
    ensure(s)
    if since is None:
        r = s.q("SELECT value FROM meta WHERE key='infra:watermark'")
        since = float(r[0]["value"]) if r else 0.0
    notes = candidates(s, since, limit)
    batches = _batches(notes)
    est = round(len(batches) * 0.035, 2)  # measured $0.07 for a 22k-character batch with Haiku; the batch API halves it
    if dry_run or not notes:
        return {"notes": len(notes), "batches": len(batches), "estimate_usd": est, "since": iso(since) if since else None}
    pl = ProviderLayer(s)
    model = model or pl.role_model("librarian") or pl.role_model("kb_summarizer")
    if not model:
        raise CommonsError("no model for the map: set the librarian role or add a provider key on the Setup page")
    known = known_names(s)
    cost, merged, failed = 0.0, defaultdict(int), []
    prompts = ["Notes:\n" + "\n".join(f"[{n['id']}] {iso(n['created_at'])[:10]} ({n['project'] or 'no project'}): {n['c']}"
                                     for n in b) for b in batches]
    texts: list = []
    if pl._p(model.partition("/")[0])["kind"] == "anthropic":  # the batch API: half price, results usually within the hour
        bid = pl.batch_submit(model, [{"id": f"b{i}", "prompt": p, "system": EXTRACT_SYS, "cache_prefix": known,
                                       "max_tokens": 32000, "schema": EXTRACT_SCHEMA} for i, p in enumerate(prompts)])
        log(f"batch 0/{len(batches)}: submitted {bid}")
        while True:
            r = pl.batch_results(model, bid, role="librarian")
            if r["status"] == "ended":
                break
            done = sum((r.get("counts") or {}).get(k, 0) for k in ("succeeded", "errored", "expired", "canceled"))
            log(f"batch {done}/{len(batches)}: waiting, ${cost:.2f}")
            time.sleep(30)
        cost, texts = r.get("cost", 0.0) or 0.0, [r["results"].get(f"b{i}") for i in range(len(batches))]
    for i, b in enumerate(batches):
        if i >= len(texts):
            r = pl.chat(model, prompts[i], system=EXTRACT_SYS, max_tokens=32000, role="librarian", schema=EXTRACT_SCHEMA,
                        cache_prefix=known)
            cost += r.get("cost", 0) or 0
            texts.append(r.get("text"))
        try:
            got = ingest_extraction(s, json.loads(texts[i] or ""), b)
        except (ValueError, KeyError, TypeError) as e:
            log(f"batch {i + 1}: unreadable answer ({e})")
            failed += [n["created_at"] for n in b]
            continue
        for k, v in got.items():
            merged[k] += v
        log(f"batch {i + 1}/{len(batches)}: {got}, ${cost:.2f}")
    mark = min(failed) - 0.001 if failed else max(n["created_at"] for n in notes)  # failed notes are read again next run
    s.x("INSERT OR REPLACE INTO meta VALUES('infra:watermark', ?)", (str(max(mark, since)),))
    link_vault(s)
    s.audit(None, "infra.extract", model, {"notes": len(notes), "cost": round(cost, 4)} | dict(merged))
    return {"notes": len(notes), "batches": len(batches), "cost": round(cost, 4)} | dict(merged)


# ------------------------------------------------------------------------------------------------ vault
def link_vault(s: Store) -> int:
    """Each vault credential becomes a credential node (name and service only) linked to the services whose endpoints
    share a host:port with its base URLs, or whose name it carries."""
    ensure(s)
    try:
        from .vault import listing
        creds = listing(s)
    except Exception:  # noqa: BLE001 - no vault table yet
        return 0
    from urllib.parse import urlsplit
    def hp(u):
        try:
            x = urlsplit(u if "://" in u else "http://" + u)
            return (x.hostname or "").lower(), x.port
        except ValueError:
            return "", None
    n = 0
    eps = [(r["id"], hp(r["name"])) for r in s.q("SELECT id, name FROM infra_nodes WHERE kind='endpoint'")]
    for c in creds:
        cid = upsert_node(s, "credential", c["credential"], attrs={"service": c.get("service"), "vault": True},
                          note=c.get("description") or "", source="vault")
        bases = [hp(u) for u in c.get("base_urls") or []]
        targets = set()
        for eid, (eh, ep) in eps:  # same host and port; a base URL with no port means the default web ports
            if any(h and h == eh and (p == ep or (p is None and ep in (None, 80, 443))) for h, p in bases):
                targets |= {r["src"] for r in s.q("SELECT src FROM infra_edges WHERE dst=? AND rel='reachable_at'", (eid,))}
        nm = _norm(c["credential"])
        if len(nm) >= 4:
            targets |= {r["id"] for r in s.q("SELECT id, name FROM infra_nodes WHERE kind='service'") if _norm(r["name"]) == nm}
        for t in targets:
            if upsert_edge(s, t, "uses_credential", cid, source="vault"):
                n += 1
    return n


# ------------------------------------------------------------------------------------------------ reading
def _node(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "name": r["name"], "note": r["note"] or "", "attrs": json.loads(r["attrs"] or "{}"),
            "sources": json.loads(r["sources"] or "[]"), "evidence": len(json.loads(r["evidence"] or "[]")),
            "last_seen": iso(r["last_seen"]) if r["last_seen"] else None, "stale": bool(r["stale"])}


def nodes(s: Store, kind: str | None = None, q: str = "", include_stale: bool = True, limit: int = 500) -> list[dict]:
    ensure(s)
    sql, args = "SELECT * FROM infra_nodes WHERE hidden=0", []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if q:
        sql += " AND (lower(name) LIKE ? OR lower(note) LIKE ? OR lower(attrs) LIKE ?)"
        args += [f"%{q.lower()}%"] * 3
    if not include_stale:
        sql += " AND stale=0"
    return [_node(r) for r in s.q(sql + " ORDER BY kind, name LIMIT ?", args + [limit])]


def overview(s: Store) -> dict:
    """Counts per kind, the systems, the top layer (places, machines) with what runs on each, and source freshness."""
    ensure(s)
    counts = {r["kind"]: r["n"] for r in s.q("SELECT kind, COUNT(*) n FROM infra_nodes WHERE hidden=0 GROUP BY kind")}
    runs = defaultdict(list)
    for r in s.q("SELECT e.src, e.dst FROM infra_edges e WHERE e.rel='runs_on' AND (e.verdict IS NULL OR e.verdict<>'no')"):
        runs[r["dst"]].append(r["src"])
    byid = {r["id"]: r for r in s.q("SELECT * FROM infra_nodes WHERE hidden=0")}
    def tree(nid, depth=0):
        kids = [k for k in runs.get(nid, []) if k in byid]
        return _node(byid[nid]) | {"children": [tree(k, depth + 1) for k in sorted(kids, key=lambda k: byid[k]["name"])] if depth < 3 else []}
    roots = [nid for nid, r in byid.items() if r["kind"] in ("place", "machine") and not s.q(
        "SELECT 1 FROM infra_edges WHERE src=? AND rel='runs_on'", (nid,))]
    sources = [{"source": r["key"][len("infra:last:"):]} | json.loads(r["value"]) for r in s.q(
        "SELECT key, value FROM meta WHERE key LIKE 'infra:last:%'")]
    for x in sources:
        x["at"] = iso(x["at"])
    return {"counts": counts, "systems": [_node(byid[i]) for i in byid if byid[i]["kind"] == "system"],
            "tree": [tree(r) for r in sorted(roots, key=lambda i: byid[i]["name"])], "sources": sources,
            "watermark": (lambda r: iso(float(r[0]["value"])) if r else None)(s.q("SELECT value FROM meta WHERE key='infra:watermark'"))}


def node(s: Store, nid: str) -> dict:
    """One node with every link in and out (grouped by relation), the evidence notes and the chain it runs on."""
    ensure(s)
    r = s.q("SELECT * FROM infra_nodes WHERE id=?", (nid,))
    if not r:
        hit = resolve(s, nid)
        if not hit:
            raise CommonsError("unknown infrastructure node")
        r = s.q("SELECT * FROM infra_nodes WHERE id=?", (hit,))
    me = r[0]
    names = {x["id"]: x for x in s.q("SELECT id, kind, name, stale FROM infra_nodes")}
    def link(e, other):
        o = names.get(other)
        return {"edge": e["id"], "rel": e["rel"], "label": LABEL[e["rel"]], "id": other, "name": o["name"] if o else "?",
                "kind": o["kind"] if o else "?", "sources": json.loads(e["sources"] or "[]"), "verdict": e["verdict"],
                "attrs": json.loads(e["attrs"] or "{}"), "stale": bool(e["stale"])}
    out = [link(e, e["dst"]) for e in s.q("SELECT * FROM infra_edges WHERE src=? AND (verdict IS NULL OR verdict<>'no')", (me["id"],))]
    inc = [link(e, e["src"]) for e in s.q("SELECT * FROM infra_edges WHERE dst=? AND (verdict IS NULL OR verdict<>'no')", (me["id"],))]
    chain, cur, seen = [], me["id"], set()
    while cur and cur not in seen:  # service -> container host -> LXC -> machine -> place
        seen.add(cur)
        up = s.q("SELECT dst FROM infra_edges WHERE src=? AND rel='runs_on' AND (verdict IS NULL OR verdict<>'no') LIMIT 1", (cur,))
        cur = up[0]["dst"] if up else None
        if cur and cur in names:
            chain.append({"id": cur, "name": names[cur]["name"], "kind": names[cur]["kind"]})
    ev = json.loads(me["evidence"] or "[]")[:20]
    notes = [{"id": x["id"], "at": iso(x["created_at"]), "text": (x["c"] or "")[:300]} for x in s.q(
        f"SELECT id, created_at, substr(COALESCE(content, summary),1,300) c FROM items WHERE id IN ({','.join('?' * len(ev))})", ev)] if ev else []
    return _node(me) | {"out": out, "in": inc, "chain": chain, "notes": notes} | related(s, me["id"])


def resolve(s: Store, query: str) -> str | None:
    """A name, alias, IP, vmid or URL to a node id. Things that run win over the credential of the same name."""
    ensure(s)
    q, n = (query or "").strip(), _norm(query)
    def key(k):
        r = s.q("SELECT node FROM infra_keys WHERE key=?", (k,))
        return r[0]["node"] if r else None
    for k in ["url:" + q.lower().rstrip("/")] + [f"vmid:{m.group(1)}" for m in [VMID.search(q)] if m] + [f"ip:{a}" for a in IP.findall(q)]:
        if key(k):
            return key(k)
    if key(f"system:{n}"):  # "Plex" means the system (its parts and where they run), not the container called plex
        return key(f"system:{n}")
    svc = {r["node"] for r in s.q("SELECT node FROM infra_keys WHERE key LIKE ?", (f"svc:{n}@%",))} if n else set()
    if len(svc) == 1:
        return svc.pop()
    for k in [f"svcname:{n}"] + [f"{kind}:{n}" for kind in KINDS if kind != "credential"]:
        if key(k):
            return key(k)
    r = s.q("SELECT id FROM infra_nodes WHERE hidden=0 AND kind<>'credential' AND lower(name)=? ORDER BY stale, last_seen DESC LIMIT 1",
            (q.lower(),)) or s.q("SELECT id FROM infra_nodes WHERE hidden=0 AND kind<>'credential' AND lower(name) LIKE ? "
                                 "ORDER BY stale, length(name) LIMIT 1", (f"%{q.lower()}%",))
    return r[0]["id"] if r else key(f"cred:{n}") or key(f"credential:{n}")


def describe(s: Store, query: str = "") -> str:
    """Plain text for an agent: one node's place in the stack and every link, or an overview of the whole map."""
    ensure(s)
    if not query:
        o = overview(s)
        if not o["counts"]:
            return ("The infrastructure map is empty. Run `commons infra collect --submit` on a machine that reaches your "
                    "hosts, or build it from memory on the dashboard (Knowledge > Infrastructure).")
        lines = ["Infrastructure map: " + ", ".join(f"{v} {k}" for k, v in sorted(o["counts"].items()))]
        def walk(n, d=0):
            lines.append("  " * d + f"- {n['name']} ({n['kind']}{', stale' if n['stale'] else ''})")
            for c in n["children"]:
                walk(c, d + 1)
        for t in o["tree"]:
            walk(t)
        if o["systems"]:
            lines.append("Systems: " + ", ".join(x["name"] for x in o["systems"]) + ". Ask infra('<name>') for one.")
        return "\n".join(lines)
    nid = resolve(s, query)
    if not nid:
        return f"Nothing in the infrastructure map matches {query!r}."
    n = node(s, nid)
    lines = [f"{n['name']} ({n['kind']})" + (f": {n['note']}" if n["note"] else "")]
    if n["chain"]:
        lines.append("Runs on: " + " > ".join(c["name"] for c in n["chain"]))
    a = n["attrs"]
    lan = [x for x in a.get("ips") or [] if IP.fullmatch(x) and not re.match(r"172\.(1[6-9]|2\d|3[01])\.", x)]
    shown = {"ip": a.get("ip") or ", ".join(lan) or None, "vmid": a.get("vmid"), "status": a.get("status"),
             "cores": a.get("cores"), "memory": f"{a['memory_mb']} MB" if a.get("memory_mb") else None,
             "os": a.get("os") or a.get("ostype"), "image": a.get("image"), "fs": a.get("fs"), "source": a.get("source"),
             "port": a.get("port"), "hostname": a.get("hostname")}
    det = "; ".join(f"{k} {v}" for k, v in shown.items() if v not in (None, "", [], {}))
    if det:
        lines.append("Details: " + det)
    def path(x, inbound=False):
        parts = []
        for b in x["attrs"].get("binds") or []:
            src, dst = b.get("host_path") or b.get("guest_path"), b.get("container_path") or b.get("guest_path")
            parts.append((f"{src} -> {dst}" if src and dst and src != dst else (src or "")) + (" read-only" if b.get("read_only") else ""))
        parts = [p for p in parts if p]
        if not parts:
            return x["name"]
        if not inbound and x["kind"] == "storage":
            return "; ".join(parts)  # the storage name is the host path already in the arrow
        return f"{x['name']} ({'; '.join(parts)})"
    by = defaultdict(list)
    for x in n["out"]:
        if x["rel"] == "runs_on":
            continue  # the chain above says it
        by[x["label"]].append((path(x) if x["rel"] == "mounts" else x["name"]) + (" (stale)" if x["stale"] else ""))
    for x in n["in"]:
        lab = {"runs_on": "hosts", "part_of": "has part", "mounts": "mounted by", "talks_to": "used by",
               "uses_credential": "credential for", "reachable_at": "endpoint of", "stores_in": "holds data of",
               "exposed_via": "exposes", "deploys_from": "deployed to"}.get(x["rel"], "linked from")
        if x["rel"] == "part_of":
            up = s.q("SELECT n.name FROM infra_edges e JOIN infra_nodes n ON n.id=e.dst WHERE e.src=? AND e.rel='runs_on' "
                     "AND (e.verdict IS NULL OR e.verdict<>'no') LIMIT 1", (x["id"],))
            by[lab].append(x["name"] + (f" (on {up[0]['name']})" if up else ""))
            continue
        by[lab].append(path(x, True) if x["rel"] == "mounts" else x["name"])
    for k, v in by.items():
        v = sorted(set(v))
        lines.append(f"{k[0].upper() + k[1:]}: " + ", ".join(v[:30]) + (f" and {len(v) - 30} more" if len(v) > 30 else ""))
    lines.append("Sources: " + ", ".join(n["sources"]) + (f"; {n['evidence']} notes" if n["evidence"] else "") +
                 (f"; last seen {n['last_seen']}" if n["last_seen"] else ""))
    return "\n".join(lines)


def graph(s: Store, root: str | None = None, depth: int = 2, limit: int = 400) -> dict:
    """Nodes and edges for the map: everything (capped), or the neighbourhood of one node."""
    ensure(s)
    if root:
        nid = resolve(s, root) if not s.q("SELECT 1 FROM infra_nodes WHERE id=?", (root,)) else root
        if not nid:
            raise CommonsError("unknown infrastructure node")
        keep, frontier = {nid}, {nid}
        for _ in range(max(1, min(depth, 4))):
            nxt = set()
            for f in frontier:
                for e in s.q("SELECT src, dst FROM infra_edges WHERE (src=? OR dst=?) AND (verdict IS NULL OR verdict<>'no')", (f, f)):
                    nxt |= {e["src"], e["dst"]}
            frontier = nxt - keep
            keep |= nxt
            if len(keep) > limit:
                break
        ns = [r for r in s.q("SELECT * FROM infra_nodes WHERE hidden=0") if r["id"] in keep]
    else:
        ns = s.q("SELECT * FROM infra_nodes WHERE hidden=0 ORDER BY stale, last_seen DESC LIMIT ?", (limit,))
    ids = {r["id"] for r in ns}
    es = [{"id": e["id"], "src": e["src"], "rel": e["rel"], "dst": e["dst"], "stale": bool(e["stale"]),
           "sources": json.loads(e["sources"] or "[]")}
          for e in s.q("SELECT * FROM infra_edges WHERE (verdict IS NULL OR verdict<>'no')") if e["src"] in ids and e["dst"] in ids]
    return {"nodes": [_node(r) | {"layer": KINDS.get(r["kind"], 9)} for r in ns], "edges": es}


# ------------------------------------------------------------------------------------------------ corrections
def verdict(s: Store, edge: str, ok: bool) -> dict:
    """The owner confirms or rejects one link; a rejected link stays recorded (so extraction won't re-add it) but hidden."""
    ensure(s)
    if not s.q("SELECT 1 FROM infra_edges WHERE id=?", (edge,)):
        raise CommonsError("unknown link")
    s.x("UPDATE infra_edges SET verdict=? WHERE id=?", ("yes" if ok else "no", edge))
    s.audit(None, "infra.verdict", edge, {"ok": ok})
    return {"ok": True}


def hide(s: Store, nid: str, hidden: bool = True) -> dict:
    ensure(s)
    s.x("UPDATE infra_nodes SET hidden=? WHERE id=?", (1 if hidden else 0, nid))
    s.audit(None, "infra.hide", nid, {"hidden": hidden})
    return {"ok": True}


def extract_async(s: Store, full: bool = False) -> dict:
    """Build from memory in the background (minutes, and it spends model credit): progress lands in meta infra:job."""
    import threading
    ensure(s)
    with s.tx():  # claim first, so a second click while the notes are being counted finds it running
        job = s.q("SELECT value FROM meta WHERE key='infra:job'")
        cur = json.loads(job[0]["value"]) if job else {}
        if cur.get("status") == "running" and now() - cur.get("at", 0) < 6 * 3600:
            return cur
        s.x("INSERT OR REPLACE INTO meta VALUES('infra:job', ?)", (json.dumps({"status": "running", "at": now(), "done": 0, "total": 0}),))
    est = extract(s, since=0.0 if full else None, dry_run=True)
    state = {"status": "running", "at": now(), "done": 0, "total": est["batches"], "notes": est["notes"],
             "estimate_usd": est["estimate_usd"], "cost": 0.0}
    def put(**kw):
        state.update(kw)
        s.x("INSERT OR REPLACE INTO meta VALUES('infra:job', ?)", (json.dumps(state),))
    put()
    def log(m):
        if m.startswith("batch "):
            n = m.split()[1].split("/")[0]
            cost = re.search(r"\$([\d.]+)$", m)
            put(done=int(n) if n.isdigit() else state["done"], cost=float(cost.group(1)) if cost else state["cost"])
    def run():
        try:
            r = extract(s, since=0.0 if full else None, log=log)
            put(status="done", done=state["total"], cost=r.get("cost", state["cost"]), result=r)
        except Exception as e:  # noqa: BLE001 - shown on the page, never a crash
            put(status="failed", error=str(e)[:300])
    threading.Thread(target=run, daemon=True, name="infra-extract").start()
    return state


def for_project(s: Store, project_ids: list[str]) -> dict:
    """The systems and services a project's notes talk about, most-cited first, with the agent text for the top one."""
    ensure(s)
    if not project_ids:
        return {"systems": [], "services": [], "text": ""}
    ph = ",".join("?" * len(project_ids))
    mine = {r["id"] for r in s.q(f"SELECT id FROM items WHERE project_id IN ({ph})", project_ids)}
    score = []
    for r in s.q("SELECT * FROM infra_nodes WHERE hidden=0 AND kind IN ('system','service','guest','machine','endpoint','storage','repo')"):
        hit = len(mine & set(json.loads(r["evidence"] or "[]")))
        if hit:
            score.append((hit, r))
    score.sort(key=lambda x: -x[0])
    systems = [_node(r) | {"cited": n} for n, r in score if r["kind"] == "system"][:6]
    others = [_node(r) | {"cited": n} for n, r in score if r["kind"] != "system"][:40]
    top = systems[0]["name"] if systems else (others[0]["name"] if others else "")
    return {"systems": systems, "services": others, "text": describe(s, top) if top else ""}


def _bytes(a: dict) -> tuple[float | None, float | None]:
    """(used, total) bytes from a storage's attrs: pvesm reports KiB, zpool human sizes like 472G."""
    def human(v):
        m = re.fullmatch(r"([\d.]+)\s*([KMGTP]?)i?B?", str(v or "").strip(), re.I)
        return float(m.group(1)) * 1024 ** "_KMGTP".index((m.group(2) or "_").upper()) if m else None
    if a.get("total") and a.get("used"):
        try:
            return float(a["used"]) * 1024, float(a["total"]) * 1024
        except (TypeError, ValueError):
            return None, None
    if a.get("size") and a.get("alloc"):
        return human(a["alloc"]), human(a["size"])
    return None, None


def stats(s: Store) -> dict:
    """What the charts draw: per machine its capacity and every guest's allocation, services and ports; storage use;
    counts by kind."""
    ensure(s)
    byid = {r["id"]: r for r in s.q("SELECT * FROM infra_nodes WHERE hidden=0")}
    runs = defaultdict(list)
    for e in s.q("SELECT src, dst FROM infra_edges WHERE rel='runs_on' AND (verdict IS NULL OR verdict<>'no')"):
        runs[e["dst"]].append(e["src"])
    ports = defaultdict(int)
    for e in s.q("SELECT src FROM infra_edges WHERE rel='reachable_at'"):
        ports[e["src"]] += 1
    machines = []
    for mid, m in byid.items():
        if m["kind"] != "machine":
            continue
        a = json.loads(m["attrs"] or "{}")
        guests = []
        for gid in runs.get(mid, []):
            g = byid.get(gid)
            if not g or g["kind"] != "guest":
                continue
            ga = json.loads(g["attrs"] or "{}")
            svcs = [x for x in runs.get(gid, []) if x in byid and byid[x]["kind"] == "service"]
            guests.append({"id": gid, "name": g["name"], "status": ga.get("status"), "stale": bool(g["stale"]),
                           "cores": ga.get("cores"), "memory_mb": ga.get("memory_mb"), "ip": ga.get("ip"),
                           "services": len(svcs), "ports": sum(ports[x] for x in svcs) + ports[gid]})
        host_svcs = [x for x in runs.get(mid, []) if x in byid and byid[x]["kind"] == "service"]
        machines.append({"id": mid, "name": m["name"], "cpu_count": a.get("cpu_count") or a.get("cores"),
                         "memory_total": a.get("memory_total"), "memory_used": a.get("memory_used"),
                         "host_services": len(host_svcs), "guests": sorted(guests, key=lambda g: -(g["memory_mb"] or 0))})
    storage = []
    for nid, n in byid.items():
        if n["kind"] == "storage":
            used, total = _bytes(json.loads(n["attrs"] or "{}"))
            if total:
                storage.append({"id": nid, "name": n["name"], "used": used, "total": total, "stale": bool(n["stale"])})
    counts = defaultdict(int)
    for n in byid.values():
        counts[n["kind"]] += 1
    return {"machines": sorted(machines, key=lambda m: -len(m["guests"])), "storage": sorted(storage, key=lambda x: -x["total"]),
            "counts": dict(counts), "stale": sum(1 for n in byid.values() if n["stale"])}


def related(s: Store, nid: str) -> dict:
    """Beyond direct links: the projects whose notes mention it and what else runs on the same host."""
    from .projects import _groups, _rows
    ensure(s)
    r = s.q("SELECT evidence FROM infra_nodes WHERE id=?", (nid,))
    ev = json.loads(r[0]["evidence"] or "[]") if r else []
    projects = []
    if ev:
        rows = _rows(s)
        root_of = {m: root for root, ids in _groups(rows).items() for m in ids}
        counts = defaultdict(int)
        for x in s.q(f"SELECT project_id FROM items WHERE id IN ({','.join('?' * len(ev))}) AND project_id IS NOT NULL", ev):
            counts[root_of.get(x["project_id"], x["project_id"])] += 1
        projects = [{"id": p, "title": rows[p]["title"] or rows[p]["name"], "notes": n}
                    for p, n in sorted(counts.items(), key=lambda kv: -kv[1]) if p in rows]
    host = s.q("SELECT dst FROM infra_edges WHERE src=? AND rel='runs_on' LIMIT 1", (nid,))
    siblings = []
    if host:
        siblings = [{"id": x["id"], "name": x["name"], "kind": x["kind"]} for x in s.q(
            "SELECT n.id, n.name, n.kind FROM infra_edges e JOIN infra_nodes n ON n.id=e.src WHERE e.dst=? AND e.rel='runs_on' "
            "AND n.id<>? AND n.hidden=0 ORDER BY n.name LIMIT 40", (host[0]["dst"], nid))]
    return {"projects": projects, "siblings": siblings}
