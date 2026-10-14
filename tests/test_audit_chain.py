"""Live audit hash chain: built at insert, checked by verify_live, kept across prune."""
import json
import threading

from commons_memory.auditexport import export, verify, verify_live
from commons_memory.retention import prune
from commons_memory.store import Store
from conftest import make_store

DAY = 86400.0


def test_chain_builds_and_export_still_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_AUDIT_KEY_FILE", str(tmp_path / "audit.pem"))
    s = make_store(tmp_path, "chain.db")
    s.audit("ag", "one", "t", {"n": 1})
    s.audit(None, "two", "", None)
    s.audit("ag", "three", "t3", {"n": 3})
    live = verify_live(s)
    rows = s.q("SELECT prev, hash FROM audit ORDER BY id")
    assert live["ok"] is True and live["rows"] == 3 and live["bad_id"] is None and live["reason"] == ""
    assert rows[0]["prev"] == ""
    assert rows[1]["prev"] == rows[0]["hash"]
    assert rows[2]["prev"] == rows[1]["hash"]
    assert live["head"] == rows[2]["hash"]
    assert verify(export(s))["ok"] is True


def test_edit_detail_breaks_chain(tmp_path):
    s = make_store(tmp_path, "edit.db")
    for i in range(5):
        s.audit("a", "ev", str(i), {"i": i})
    ids = [r["id"] for r in s.q("SELECT id FROM audit ORDER BY id")]
    s.x("UPDATE audit SET detail=? WHERE id=?", ("tampered", ids[1]))
    bad = verify_live(s)
    assert bad["ok"] is False and bad["bad_id"] == ids[1]
    assert bad["reason"]


def test_delete_middle_row_breaks_chain(tmp_path):
    s = make_store(tmp_path, "del.db")
    for i in range(5):
        s.audit("a", "ev", str(i))
    ids = [r["id"] for r in s.q("SELECT id FROM audit ORDER BY id")]
    s.x("DELETE FROM audit WHERE id=?", (ids[2],))
    bad = verify_live(s)
    assert bad["ok"] is False and bad["bad_id"] == ids[3]
    assert bad["reason"]


def test_prune_keeps_chain(tmp_path, monkeypatch):
    s = make_store(tmp_path, "prune.db")
    base = 1_700_000_000.0
    clock = {"t": base}
    monkeypatch.setattr("commons_memory.store.now", lambda: clock["t"])
    for i in range(4):
        clock["t"] = base + i
        s.audit("a", "old", str(i), {"i": i})
    for i in range(3):
        clock["t"] = base + 30 * DAY + i
        s.audit("a", "new", str(i), {"i": i})
    monkeypatch.setattr("commons_memory.retention.now", lambda: base + 30 * DAY + 100)
    out = prune(s, audit_days=7)
    assert out["audit_trimmed"] == 4
    live = verify_live(s)
    assert live["ok"] is True and live["rows"] == 4, live  # 3 kept, plus the prune audit row
    anchor = s.q("SELECT value FROM meta WHERE key='audit_anchor'")[0]["value"]
    first = s.q("SELECT prev FROM audit ORDER BY id LIMIT 1")[0]["prev"]
    assert anchor and first == anchor


def test_prune_takes_only_the_oldest_unbroken_run_and_never_rewrites_kept_rows(tmp_path, monkeypatch):
    s = make_store(tmp_path, "hole.db")
    s.x("DELETE FROM audit")
    base = 1_700_000_000.0
    clock = {"t": base}
    monkeypatch.setattr("commons_memory.store.now", lambda: clock["t"])
    for ts, target in ((base, "old1"), (base + 30 * DAY, "new"), (base + 2, "old-but-after-new"), (base + 30 * DAY + 1, "tail")):
        clock["t"] = ts
        s.audit("a", "x", target)
    kept_before = {r["id"]: r["hash"] for r in s.q("SELECT id, hash FROM audit")}
    monkeypatch.setattr("commons_memory.retention.now", lambda: base + 30 * DAY + 10)
    assert prune(s, audit_days=7)["audit_trimmed"] == 1                      # the skewed row waits; no hole is cut
    assert [r["target"] for r in s.q("SELECT target FROM audit ORDER BY id")][:3] == ["new", "old-but-after-new", "tail"]
    assert all(kept_before[r["id"]] == r["hash"] for r in s.q("SELECT id, hash FROM audit") if r["id"] in kept_before)
    assert verify_live(s)["ok"]


def test_legacy_rows_are_chained_on_open_but_a_blanked_hash_is_not_resigned(tmp_path):
    s = make_store(tmp_path, "legacy.db")
    for i in range(3):
        s.audit("a", "real", str(i), {"i": i})
    s.x("UPDATE audit SET prev=NULL, hash=NULL")                              # a log from before the chain existed
    live = verify_live(Store(s.path, embedder="hash"))
    assert live["ok"] is True and live["rows"] >= 3, live
    victim = s.q("SELECT id FROM audit ORDER BY id LIMIT 1")[0]["id"]
    s.x("UPDATE audit SET detail='forged', hash=NULL WHERE id=?", (victim,))  # edit a row, then blank its hash
    live = verify_live(Store(s.path, embedder="hash"))
    assert not live["ok"] and live["bad_id"] == victim


def test_concurrent_audits_one_chain(tmp_path):
    s = make_store(tmp_path, "thr.db")
    errors = []

    def run(i):
        try:
            for n in range(50):
                s.audit(f"a{i}", "tick", f"{i}-{n}", {"n": n})
        except Exception as e:  # noqa: BLE001 - asserted below
            errors.append((i, repr(e)))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not [t.name for t in threads if t.is_alive()]
    assert not errors
    live = verify_live(s)
    assert live["ok"] is True and live["rows"] == 400, live


def test_cli_audit_chain_exit_code(tmp_path, capsys):
    s = make_store(tmp_path, "cli.db")
    s.audit("a", "ev", "t", {"n": 1})
    from commons_memory.cli import main
    assert main(["--db", s.path, "audit-chain"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    row_id = s.q("SELECT id FROM audit ORDER BY id")[0]["id"]
    s.x("UPDATE audit SET detail=? WHERE id=?", ("tampered", row_id))
    assert main(["--db", s.path, "audit-chain"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["ok"] is False and body["bad_id"] == row_id
