from conftest import make_store

from commons_memory import typesafe
from commons_memory.librarian import Librarian


def test_librarian_flags_free_text_contradictions_once(tmp_path, monkeypatch):
    s = make_store(tmp_path)
    a = s.add_item("memory", "Production deploys run every Friday afternoon.", project="ops")["id"]
    s.add_item("memory", "The staging database lives in us-east-1.", project="ops")
    b = s.add_item("memory", "We never deploy to production on Fridays.", project="ops")["id"]
    calls = []

    def fake(state, questions, timeout=20):
        calls.append(state)
        return {"c": 0.97 if "Friday" in state and "never" in state else 0.05}
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(typesafe, "noul", fake)
    rep = Librarian(s).run(full=True)
    assert b in rep["contradictions"] or a in rep["contradictions"]
    open_ = s.list_conflicts()
    assert len(open_) == 1 and {open_[0]["a"], open_[0]["b"]} == {a, b}
    assert s.q("SELECT conflict FROM items WHERE id=?", (a,))[0]["conflict"] == 1
    Librarian(s).run(full=True)  # a second pass must not re-flag a known pair
    assert len(s.list_conflicts()) == 1
    monkeypatch.delenv("TYPESAFE_API_KEY")
    n = len(calls)
    Librarian(s).run(full=True)  # no key: no calls
    assert len(calls) == n
