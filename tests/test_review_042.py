"""Small checks for the v0.42.0 review fixes: one per piece of logic that could quietly break."""
import pytest

from commons_memory import vault as V
from commons_memory.auth import PRINCIPAL
from commons_memory.librarian import Librarian
from commons_memory.orchestrator import Orchestrator
from commons_memory.store import CommonsError


def test_a_heartbeat_never_shortens_a_lease(store):
    o = Orchestrator(store)
    a = store.ensure_agent("worker")
    job = o.submit("write the docs", pattern="single", agent_id=a)
    sid = store.q("SELECT id FROM subtasks WHERE job_id=?", (job["job_id"],))[0]["id"]
    store.x("UPDATE subtasks SET status='claimed', claimed_by=?, lease_expires=? WHERE id=?", (a, 9e12, sid))
    o.heartbeat(sid, a, lease_seconds=900)
    assert store.q("SELECT lease_expires FROM subtasks WHERE id=?", (sid,))[0]["lease_expires"] == 9e12


def test_a_librarian_proposal_is_decided_once(store):
    lib = Librarian(store)
    pid = lib._propose("flag_stale", "mem_x", {}, "old")
    lib.decide(pid, False)
    with pytest.raises(CommonsError):
        lib.decide(pid, True)


@pytest.mark.parametrize("spec,headers", [("basic:", None), ("nonsense:x", None), ("header:X\r\nEvil: 1", None),
                                          ("bearer", ["no colon"])])
def test_a_malformed_auth_spec_is_refused(spec, headers):
    with pytest.raises(CommonsError):
        V.parse_auth(spec, headers)


def test_only_a_party_to_a_task_can_answer_it(store):
    a, b, c = (store.ensure_agent(n) for n in ("ann", "bob", "cat"))
    tid = store.post_task(a, b, "please review")["task_id"]
    tok = PRINCIPAL.set(c)
    try:
        with pytest.raises(CommonsError):
            store.respond_task(tid, "done by a stranger", agent_id=c)
    finally:
        PRINCIPAL.reset(tok)
    assert store.respond_task(tid, "done", agent_id=b)["status"] == "done"
