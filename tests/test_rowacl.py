"""Roadmap: row-level ACLs (one record restricted to named agents)."""
import pytest

from commons_memory import rowacl as R
from commons_memory.auth import PRINCIPAL
from commons_memory.board import add as board_add, view as board_view
from commons_memory.store import CommonsError


def _as(aid):
    return PRINCIPAL.set(aid)


def test_a_restricted_memory_is_invisible_to_everyone_but_the_listed_agents(store):
    a, b, c = (store.ensure_agent(n) for n in ("alice", "bob", "carol"))
    secret = store.add_item("memory", "the deploy password rotation happens on Fridays", agent_id=a)["id"]
    store.add_item("memory", "the deploy runs on Fridays too", agent_id=a)
    tok = _as(a)
    R.restrict(store, a, secret, ["bob"])
    PRINCIPAL.reset(tok)
    for who, sees in ((a, True), (b, True), (c, False)):
        tok = _as(who)
        hits = store.search("deploy Fridays")["text"]
        assert (secret in hits) is sees, who
        if not sees:
            with pytest.raises(CommonsError):
                store.expand(secret)
        PRINCIPAL.reset(tok)
    tok = _as(b)
    with pytest.raises(CommonsError, match="write"):
        store.delete_item(secret)  # read only
    with pytest.raises(CommonsError, match="author"):
        R.restrict(store, b, secret, ["carol"])
    PRINCIPAL.reset(tok)
    assert store.search("deploy Fridays")["text"].count("mem_") >= 2  # the local owner sees everything
    R.unrestrict(store, None, secret)
    tok = _as(c)
    assert secret in store.search("deploy Fridays")["text"]
    PRINCIPAL.reset(tok)


def test_restricted_board_items_and_kb_stay_private(store):
    a, c = store.ensure_agent("alice"), store.ensure_agent("carol")
    bid = board_add(store, "action", "rotate the vault key", project="p", agent_id=a)["id"]
    R.restrict(store, None, bid, ["alice"])
    tok = _as(c)
    assert bid not in board_view(store, "p", status="all")["text"]
    PRINCIPAL.reset(tok)
    assert bid in board_view(store, "p", status="all")["text"]
    mid = store.add_item("decision", "keep the vault key on the NAS only", project="p", agent_id=a)["id"]
    R.restrict(store, None, mid, ["alice"])
    from commons_memory.kb import KB
    assert mid not in [i["id"] for i in KB(store)._items("p")]


def test_signed_audit_export_detects_edits_drops_and_a_foreign_key(store, tmp_path, monkeypatch):
    import json
    from commons_memory import auditexport as AX
    monkeypatch.setenv("COMMONS_AUDIT_KEY_FILE", str(tmp_path / "k.pem"))
    a = store.ensure_agent("alice")
    for i in range(4):
        store.audit(a, "test.event", f"t{i}", {"n": i})
    text = AX.export(store)
    fp = AX.public_key()["fingerprint"]
    ok = AX.verify(text, fp)
    assert ok["ok"] and ok["rows"] >= 4 and ok["pinned"]
    lines = text.splitlines()
    edited = json.loads(lines[2]); edited["action"] = "nothing.to.see"
    assert not AX.verify("\n".join(lines[:2] + [json.dumps(edited)] + lines[3:]))["ok"]
    assert "chain broken" in AX.verify("\n".join(lines[:2] + lines[3:]))["error"]  # a dropped row
    monkeypatch.setenv("COMMONS_AUDIT_KEY_FILE", str(tmp_path / "other.pem"))
    forged = AX.export(store)  # re-signed with someone else's key
    assert AX.verify(forged)["ok"] and "not the pinned key" in AX.verify(forged, fp)["error"]
    assert oct((tmp_path / "k.pem").stat().st_mode)[-3:] == "600"
