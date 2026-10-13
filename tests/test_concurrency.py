"""Multi-threaded stress and chaos tests for claims, completion, leases, inbox, memory, and presence.

The MCP server builds one Store and runs sync tools on worker threads (server.offload_sync_tools), so those
races share a connection and the store RLock. A worker process opens its own Store on the same file
(worker.LocalClient). Memory writers do that too: that is the path that raises "database is locked" when
SQLite cannot get the WAL write lock. Orchestrator and inbox tests share one Store, matching server threads.
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from commons_memory.auth import ACL, PRINCIPAL
from commons_memory.ids import estimate_tokens
from commons_memory.messaging import Messaging, heartbeat
from commons_memory.orchestrator import Orchestrator
from commons_memory.store import CommonsError, Store
from conftest import make_store


def _join(threads, timeout):
    for t in threads:
        t.join(timeout)
    alive = [t.name for t in threads if t.is_alive()]
    assert not alive, f"threads still running: {alive}"


def _raise_errors(errors):
    assert not errors, "worker exceptions:\n" + "\n".join(f"{n}: {type(e).__name__}: {e}" for n, e in errors)


def _sub(store, job_id, key):
    return store.q("SELECT * FROM subtasks WHERE job_id=? AND key=?", (job_id, key))[0]


# ---------------------------------------------------------------- claims
def test_double_claim_exactly_one_agent_per_subtask(tmp_path):
    """20 threads race claim() on 10 independent subtasks. Each subtask is claimed once, by one agent."""
    store = make_store(tmp_path, "claim.db")
    o = Orchestrator(store)
    # No profiles: allocate leaves assigned_agent NULL, so every agent is eligible and they actually collide.
    agents = [store.ensure_agent(f"claimer-{i}") for i in range(20)]
    dag = [{"key": f"t{i:02d}", "type": "general", "title": f"piece {i}", "body": f"do piece {i}", "deps": []}
           for i in range(10)]
    job = o.submit("race ten pieces", dag=dag, pattern="parallel")
    jid = job["job_id"]
    won, errors = [], []
    lock, stop = threading.Lock(), threading.Event()

    def worker(aid):
        try:
            while not stop.is_set():
                packet = o.claim(aid)
                if packet:
                    with lock:
                        won.append((packet["subtask_id"], aid, packet["attempt"]))
                else:
                    time.sleep(0.001)
        except Exception as e:  # noqa: BLE001 - surfaced below
            errors.append((aid, e))

    threads = [threading.Thread(target=worker, args=(a,), name=f"claim-{i}") for i, a in enumerate(agents)]
    for t in threads:
        t.start()
    deadline = time.time() + 20
    while time.time() < deadline:
        n = store.q("SELECT COUNT(*) c FROM subtasks WHERE job_id=? AND status='claimed'", (jid,))[0]["c"]
        if n == 10:
            break
        time.sleep(0.02)
    stop.set()
    _join(threads, 10)
    _raise_errors(errors)

    rows = store.q("SELECT id, status, claimed_by, attempts FROM subtasks WHERE job_id=?", (jid,))
    assert len(rows) == 10
    assert {r["status"] for r in rows} == {"claimed"}
    assert len({r["claimed_by"] for r in rows}) >= 1
    by_id = {sid: aid for sid, aid, _ in won}
    assert len(won) == 10 and len(by_id) == 10
    assert all(attempt == 1 for _, _, attempt in won)
    for r in rows:
        assert r["attempts"] == 1
        assert by_id[r["id"]] == r["claimed_by"]
    # A second wave must not steal a live claim.
    again = []
    for a in agents:
        packet = o.claim(a)
        if packet:
            again.append(packet["subtask_id"])
    assert again == []


# ---------------------------------------------------------------- complete
def test_concurrent_complete_one_winner_and_dependant_once(tmp_path):
    """Owner completes the same subtask twice, alongside a non-owner. One success; the dependant wakes once."""
    store = make_store(tmp_path, "complete.db")
    o = Orchestrator(store)
    owner, stranger, dep_agent = (store.ensure_agent(n) for n in ("owner", "stranger", "dep"))
    for aid in (owner, stranger, dep_agent):
        o.profiles.set(aid, expertise={"general": 1.0}, max_concurrent=8)
    job = o.submit("chain", dag=[
        {"key": "root", "type": "general", "title": "root task", "body": "do the root work", "deps": []},
        {"key": "next", "type": "general", "title": "next task", "body": "do the next work", "deps": ["root"]},
    ], pattern="parallel")
    jid = job["job_id"]
    root_id, next_id = _sub(store, jid, "root")["id"], _sub(store, jid, "next")["id"]
    # Pin assignees so the race is on these agents, not on whoever allocate picked.
    store.x("UPDATE subtasks SET assigned_agent=? WHERE id=?", (owner, root_id))
    store.x("UPDATE subtasks SET assigned_agent=? WHERE id=?", (dep_agent, next_id))
    assert o.claim(owner, root_id, lease_seconds=60)

    claims, results, errors = [], [], []
    claim_stop = threading.Event()

    def claim_next(aid, name):
        try:
            while not claim_stop.is_set():
                packet = o.claim(aid, next_id)
                if packet:
                    claims.append((name, packet["subtask_id"], packet["attempt"]))
                    return
                time.sleep(0.002)
        except Exception as e:  # noqa: BLE001
            errors.append((name, e))

    def finish(aid, label, text):
        try:
            out = o.complete(root_id, aid, text)
            results.append(("ok", label, out))
        except CommonsError as e:
            results.append(("err", label, str(e)))
        except Exception as e:  # noqa: BLE001
            errors.append((label, e))

    claimers = [threading.Thread(target=claim_next, args=args, name=f"dep-{args[1]}")
                for args in ((dep_agent, "dep-a"), (dep_agent, "dep-b"), (stranger, "stranger-claim"),
                             (owner, "owner-claim"))]
    for t in claimers:
        t.start()
    # Release the three completers together so the loser overlaps the winner.
    gate = threading.Barrier(3)

    def gated(aid, label, text):
        gate.wait(5)
        finish(aid, label, text)

    finishers = [threading.Thread(target=gated, args=args, name=args[1]) for args in (
        (owner, "owner-a", "owner result alpha"),
        (owner, "owner-b", "owner result beta"),
        (stranger, "stranger", "stranger result"),
    )]
    for t in finishers:
        t.start()
    _join(finishers, 15)
    deadline = time.time() + 5  # the dependant is ready now; give the pollers a moment to take it before stopping them
    while not claims and time.time() < deadline:
        time.sleep(0.01)
    claim_stop.set()
    _join(claimers, 5)
    _raise_errors(errors)

    oks = [r for r in results if r[0] == "ok"]
    errs = [r for r in results if r[0] == "err"]
    assert len(results) == 3
    assert len(oks) == 1 and oks[0][1].startswith("owner"), results
    assert len(errs) == 2
    assert any(r[1] == "stranger" for r in errs)

    root = store.q("SELECT status, claimed_by, attempts, result FROM subtasks WHERE id=?", (root_id,))[0]
    assert root["status"] == "done" and root["claimed_by"] == owner and root["attempts"] == 1
    assert root["result"] in ("owner result alpha", "owner result beta")
    used = json.loads(store.q("SELECT used FROM jobs WHERE id=?", (jid,))[0]["used"])
    assert used["tokens"] == estimate_tokens(root["result"])
    assert store.q("SELECT COUNT(*) c FROM usage WHERE subtask_id=?", (root_id,))[0]["c"] == 1
    assert store.q("SELECT COUNT(*) c FROM relations WHERE src=? AND type='produced'", (root_id,))[0]["c"] == 1
    assert store.q("SELECT status FROM jobs WHERE id=?", (jid,))[0]["status"] == "running"

    mails = [json.loads(m["meta"] or "{}") for m in store.q("SELECT meta FROM messages")]
    assert sum(1 for m in mails if m.get("subtask") == next_id) == 1
    assert len(claims) == 1 and claims[0][1] == next_id and claims[0][2] == 1
    nxt = store.q("SELECT status, claimed_by, attempts FROM subtasks WHERE id=?", (next_id,))[0]
    assert nxt["status"] == "claimed" and nxt["claimed_by"] == dep_agent and nxt["attempts"] == 1
    assert o.claim(dep_agent, next_id) is None
    assert o.claim(stranger, next_id) is None


def test_concurrent_complete_separate_connections_keep_job_state(tmp_path):
    """Same race as above, but each completer opens its own Store, the way two workers share a WAL file.

    One completion must win, the dependant must wake once, and job accounting must describe that one result.
    """
    store = make_store(tmp_path, "complete-conns.db")
    o = Orchestrator(store)
    owner, stranger, dep_agent = (store.ensure_agent(n) for n in ("owner", "stranger", "dep"))
    for aid in (owner, stranger, dep_agent):
        o.profiles.set(aid, expertise={"general": 1.0}, max_concurrent=8)
    job = o.submit("chain", dag=[
        {"key": "root", "type": "general", "title": "root task", "body": "do the root work", "deps": []},
        {"key": "next", "type": "general", "title": "next task", "body": "do the next work", "deps": ["root"]},
    ], pattern="parallel")
    jid = job["job_id"]
    root_id, next_id = _sub(store, jid, "root")["id"], _sub(store, jid, "next")["id"]
    store.x("UPDATE subtasks SET assigned_agent=? WHERE id=?", (owner, root_id))
    store.x("UPDATE subtasks SET assigned_agent=? WHERE id=?", (dep_agent, next_id))
    assert o.claim(owner, root_id, lease_seconds=60)

    # Very different sizes so a last-write-wins clobber of jobs.used is visible against the stored result.
    texts = {"owner-a": "A", "owner-b": "B" * 400, "stranger": "nope"}
    conns = {label: Store(store.path, embedder="hash") for label in texts}
    results, errors, claims = [], [], []
    gate = threading.Barrier(3)
    claim_stop = threading.Event()

    def claim_next():
        try:
            local = Orchestrator(Store(store.path, embedder="hash"))
            while not claim_stop.is_set():
                packet = local.claim(dep_agent, next_id)
                if packet:
                    claims.append(packet["subtask_id"])
                    return
                time.sleep(0.002)
        except Exception as e:  # noqa: BLE001
            errors.append(("dep", e))

    def finish(label, aid):
        gate.wait(5)
        try:
            Orchestrator(conns[label]).complete(root_id, aid, texts[label])
            results.append(("ok", label))
        except CommonsError as e:
            results.append(("err", label, str(e)))
        except Exception as e:  # noqa: BLE001
            errors.append((label, e))

    claimer = threading.Thread(target=claim_next, name="dep-claim")
    claimer.start()
    finishers = [threading.Thread(target=finish, args=args, name=args[0]) for args in (
        ("owner-a", owner), ("owner-b", owner), ("stranger", stranger))]
    for t in finishers:
        t.start()
    _join(finishers, 15)
    deadline = time.time() + 5  # the dependant is ready now; give the pollers a moment to take it before stopping them
    while not claims and time.time() < deadline:
        time.sleep(0.01)
    claim_stop.set()
    _join([claimer], 5)
    _raise_errors(errors)

    oks = [r for r in results if r[0] == "ok"]
    assert len(oks) == 1 and oks[0][1].startswith("owner")
    assert sum(1 for r in results if r[0] == "err") == 2
    root = store.q("SELECT status, claimed_by, attempts, result FROM subtasks WHERE id=?", (root_id,))[0]
    assert root["status"] == "done" and root["claimed_by"] == owner and root["attempts"] == 1
    assert root["result"] == texts[oks[0][1]]
    used = json.loads(store.q("SELECT used FROM jobs WHERE id=?", (jid,))[0]["used"])
    assert used["tokens"] == estimate_tokens(root["result"])
    assert store.q("SELECT COUNT(*) c FROM usage WHERE subtask_id=?", (root_id,))[0]["c"] == 1
    assert store.q("SELECT COUNT(*) c FROM relations WHERE src=? AND type='produced'", (root_id,))[0]["c"] == 1
    mails = [json.loads(m["meta"] or "{}") for m in store.q("SELECT meta FROM messages")]
    # the polling dependant may claim before the wake mail goes out (then none is needed): never mailed twice
    assert sum(1 for m in mails if m.get("subtask") == next_id) <= 1
    assert claims == [next_id]
    for s in conns.values():
        s.db.close()


# ---------------------------------------------------------------- leases
def test_lease_expiry_rejects_stale_owner(tmp_path):
    """A 1s lease lapses, reap() frees it, another agent claims it, and the old owner's heartbeat/complete lose."""
    store = make_store(tmp_path, "lease.db")
    o = Orchestrator(store)
    owner, other = store.ensure_agent("lease-owner"), store.ensure_agent("lease-other")
    job = o.submit("leased", dag=[{"key": "only", "type": "general", "title": "leased work", "body": "do it", "deps": []}],
                   pattern="parallel")
    sid = _sub(store, job["job_id"], "only")["id"]
    packet = o.claim(owner, sid, lease_seconds=1)
    assert packet and packet["subtask_id"] == sid

    deadline = time.time() + 4
    while time.time() < deadline:
        exp = store.q("SELECT lease_expires FROM subtasks WHERE id=?", (sid,))[0]["lease_expires"]
        if exp < time.time():
            break
        time.sleep(0.05)
    else:
        pytest.fail("lease did not lapse")

    reaped = o.reap()
    assert sid in reaped
    freed = store.q("SELECT status, claimed_by, excluded_agents FROM subtasks WHERE id=?", (sid,))[0]
    assert freed["status"] == "pending" and freed["claimed_by"] is None
    assert owner in json.loads(freed["excluded_agents"] or "[]")

    errors, claimed = [], []
    gate = threading.Barrier(3)

    def stale_heartbeat():
        gate.wait(5)
        try:
            heartbeat_result = o.heartbeat(sid, owner, lease_seconds=30)
            errors.append(("stale-heartbeat", f"accepted {heartbeat_result}"))
        except CommonsError:
            return
        except Exception as e:  # noqa: BLE001
            errors.append(("stale-heartbeat", e))

    def stale_complete():
        gate.wait(5)
        try:
            o.complete(sid, owner, "late result from the expired owner")
            errors.append(("stale-complete", "accepted"))
        except CommonsError:
            return
        except Exception as e:  # noqa: BLE001
            errors.append(("stale-complete", e))

    def fresh_claim():
        gate.wait(5)
        try:
            packet = o.claim(other, sid, lease_seconds=60)
            if packet:
                claimed.append(packet["subtask_id"])
            else:
                errors.append(("fresh-claim", "returned None"))
        except Exception as e:  # noqa: BLE001
            errors.append(("fresh-claim", e))

    threads = [threading.Thread(target=fn, name=fn.__name__) for fn in (stale_heartbeat, stale_complete, fresh_claim)]
    for t in threads:
        t.start()
    _join(threads, 10)
    assert not errors, errors
    assert claimed == [sid]
    row = store.q("SELECT status, claimed_by, result FROM subtasks WHERE id=?", (sid,))[0]
    assert row["status"] == "claimed" and row["claimed_by"] == other
    assert "late result" not in (row["result"] or "")
    # The stale owner is still rejected after the new claim sticks.
    with pytest.raises(CommonsError):
        o.heartbeat(sid, owner)
    with pytest.raises(CommonsError):
        o.complete(sid, owner, "later still")


# ---------------------------------------------------------------- inbox
def test_inbox_concurrent_recv_delivers_each_message_once(tmp_path):
    """5 senders write 200 messages while 10 threads recv(ack=True) the same inbox. No loss, no duplicates."""
    store = make_store(tmp_path, "inbox.db")
    inbox = store.ensure_agent("inbox")
    senders = [store.ensure_agent(f"sender-{i}") for i in range(5)]
    sent, got, errors = [], [], []
    sent_lock, got_lock = threading.Lock(), threading.Lock()
    senders_done = threading.Event()

    def send_worker(sender, start, stop):
        try:
            hub = Messaging(store)
            ids = []
            for n in range(start, stop):
                ids.append(hub.send(sender, inbox, f"payload {n} from {sender}", subject=f"sub-{n}")["id"])
            with sent_lock:
                sent.extend(ids)
        except Exception as e:  # noqa: BLE001
            errors.append((sender, e))

    def recv_worker(name):
        try:
            hub = Messaging(store)
            idle = 0
            while True:
                batch = hub.recv(inbox, ack=True, limit=25)
                if batch:
                    with got_lock:
                        got.extend(m["id"] for m in batch)
                    idle = 0
                    continue
                if senders_done.is_set():
                    idle += 1
                    if idle >= 3:
                        return
                time.sleep(0.005)
        except Exception as e:  # noqa: BLE001
            errors.append((name, e))

    receivers = [threading.Thread(target=recv_worker, args=(f"recv-{i}",), name=f"recv-{i}") for i in range(10)]
    for t in receivers:
        t.start()
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(send_worker, senders[i], i * 40, (i + 1) * 40) for i in range(5)]
        for f in futs:
            f.result(timeout=30)
    senders_done.set()
    _join(receivers, 20)
    _raise_errors(errors)

    assert len(sent) == 200
    assert sorted(got) == sorted(sent)
    assert len(set(got)) == 200
    left = store.q("SELECT COUNT(*) c FROM message_deliveries WHERE agent_id=? AND acked_at IS NULL", (inbox,))[0]["c"]
    assert left == 0


# ---------------------------------------------------------------- memory
def test_memory_concurrent_remember_search_and_acl(tmp_path):
    """8 writers remember 500 items on their own connections; 8 readers search. Hidden items stay hidden."""
    store = make_store(tmp_path, "memory.db")
    alice, bob = store.ensure_agent("alice"), store.ensure_agent("bob")
    ACL(store).grant("vault", "alice", "write")
    store.project_id("plaza")
    # One connection per thread, same WAL file, opened before the storm so migrate does not race the writers.
    conns = [store] + [Store(store.path, embedder="hash") for _ in range(15)]
    n = 500
    hidden_idx = {i for i in range(n) if i % 10 == 0}
    written, errors = [], []
    lock = threading.Lock()

    def token(i):
        return f"qxw{i:04d}brk"

    def writer(wid, s):
        try:
            for i in range(wid, n, 8):
                tok = token(i)
                if i in hidden_idx:
                    principal = PRINCIPAL.set(alice)
                    try:
                        item = s.add_item("memory", f"{tok} vaultsecret ledger row {i}", project="vault",
                                          agent_id=alice, title=tok)
                    finally:
                        PRINCIPAL.reset(principal)
                    hidden = True
                else:
                    item = s.add_item("memory", f"{tok} plazanote public row {i}", project="plaza",
                                      agent_id=alice, title=tok)
                    hidden = False
                with lock:
                    written.append((tok, item["id"], hidden))
        except Exception as e:  # noqa: BLE001
            errors.append((f"writer-{wid}", e))

    def reader(name, s):
        principal = PRINCIPAL.set(bob)
        try:
            for _ in range(12):
                for query, mode in (("plazanote", "keyword"), ("vaultsecret", "hybrid"), ("qxw", "keyword")):
                    res = s.search(query, mode=mode, limit=20, max_tokens=600, rerank=False)
                    for hit in res["results"]:
                        if hit["project"] == "vault":
                            raise AssertionError(f"{name} saw vault item {hit['id']} via {query!r}")
        except Exception as e:  # noqa: BLE001
            errors.append((name, e))
        finally:
            PRINCIPAL.reset(principal)

    threads = []
    for i in range(8):
        threads.append(threading.Thread(target=writer, args=(i, conns[i]), name=f"writer-{i}"))
        threads.append(threading.Thread(target=reader, args=(f"reader-{i}", conns[8 + i]), name=f"reader-{i}"))
    for t in threads:
        t.start()
    _join(threads, 45)
    _raise_errors(errors)
    assert len(written) == n
    assert sum(1 for _, _, hidden in written if hidden) == len(hidden_idx)

    missing = []
    for tok, iid, _hidden in written:
        hits = [r["id"] for r in store.search(tok, mode="keyword", limit=3, rerank=False)["results"]]
        if iid not in hits:
            missing.append(tok)
    assert not missing, f"{len(missing)} written items not findable, e.g. {missing[:5]}"

    principal = PRINCIPAL.set(bob)
    try:
        leaked = []
        broad = store.search("vaultsecret", mode="keyword", limit=50, max_tokens=4000, rerank=False)
        leaked += [r["id"] for r in broad["results"] if r["project"] == "vault"]
        for tok, iid, hidden in written:
            if not hidden:
                continue
            hits = store.search(tok, mode="keyword", limit=5, rerank=False)["results"]
            if any(r["id"] == iid or r["project"] == "vault" for r in hits):
                leaked.append(iid)
        assert not leaked, f"bob saw {len(leaked)} vault items"
    finally:
        PRINCIPAL.reset(principal)
    for s in conns[1:]:
        s.db.close()


# ---------------------------------------------------------------- presence
def test_presence_heartbeat_upsert_one_row_per_agent(tmp_path):
    """20 threads heartbeat the same agents at once. One presence row each, no UNIQUE failure."""
    store = make_store(tmp_path, "presence.db")
    agents = [store.ensure_agent(f"present-{i}") for i in range(20)]
    # Separate connections: the SELECT-then-INSERT in heartbeat is the documented race.
    conns = [store] + [Store(store.path, embedder="hash") for _ in range(19)]
    errors = []

    def worker(i):
        try:
            s = conns[i]
            for round_ in range(25):
                heartbeat(s, agents[i], status=f"round {round_}", client=f"thr-{i}")
                heartbeat(s, agents[(i + round_) % 20], status="shared", client=f"thr-{i}")
        except Exception as e:  # noqa: BLE001
            errors.append((f"thr-{i}", e))

    threads = [threading.Thread(target=worker, args=(i,), name=f"hb-{i}") for i in range(20)]
    for t in threads:
        t.start()
    _join(threads, 20)
    _raise_errors(errors)
    rows = store.q("SELECT agent_id FROM agent_presence")
    assert sorted(r["agent_id"] for r in rows) == sorted(agents)
    assert len(rows) == len(set(agents)) == 20
    for aid in agents:
        assert heartbeat(store, aid, status="settled", client="final")["id"] == aid
    for s in conns[1:]:
        s.db.close()


def test_one_background_loop_and_one_claim_per_database(tmp_path):
    """`serve --dashboard` opens two Store objects on one file: they must share one maintenance loop, one reindex lock,
    and a due job must be claimed once (two loops both embedding the same rows made a 12k backlog crawl)."""
    from commons_memory import background as bg
    a = make_store(tmp_path, "bg.db")
    b = Store(a.path, embedder="hash")
    assert a._reindexing is b._reindexing
    ta = bg.start_background(a, mode="light")
    try:
        assert bg.start_background(b, mode="light") is ta
        os.environ["COMMONS_EVERY_TIERS"] = "3600"
        a.x("DELETE FROM meta WHERE key='bg_last:tiers'")
        assert [bg._due(a, "tiers"), bg._due(b, "tiers")] == [True, False]
    finally:
        os.environ.pop("COMMONS_EVERY_TIERS", None)
        bg.stop_background(a)
