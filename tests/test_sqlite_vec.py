"""Tests for sqlite-vec extension vector search path."""
import os
import random
import sqlite3
import pytest

from commons_memory.store import Store


def _sqlite_vec_usable() -> bool:
    try:
        import sqlite_vec
        con = sqlite3.connect(":memory:")
        if not hasattr(con, "enable_load_extension"):
            con.close()
            return False
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        con.execute("SELECT vec_version()")
        con.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _sqlite_vec_usable(),
    reason="sqlite-vec or enable_load_extension unavailable",
)


def _assert_same_top_ids(vec_results, np_results, tol: float = 1e-6):
    """Assert results have the same top-10 ids, allowing ties within floating tolerance."""
    assert len(vec_results) == len(np_results)
    vec_ids = [r[1] if isinstance(r, tuple) else r["id"] for r in vec_results]
    np_ids = [r[1] if isinstance(r, tuple) else r["id"] for r in np_results]
    if vec_ids == np_ids:
        return
    vec_map = {r[1] if isinstance(r, tuple) else r["id"]: (r[0] if isinstance(r, tuple) else r.get("score", 0.0)) for r in vec_results}
    np_map = {r[1] if isinstance(r, tuple) else r["id"]: (r[0] if isinstance(r, tuple) else r.get("score", 0.0)) for r in np_results}
    assert set(vec_ids) == set(np_ids), f"ID sets differ: vec={vec_ids} vs np={np_ids}"
    for iid in vec_ids:
        assert abs(vec_map[iid] - np_map[iid]) <= tol


@pytest.fixture
def store_db_path(tmp_path):
    """Seed a SQLite database with ~300 items across projects and tags."""
    path = str(tmp_path / "vec_bench.db")
    seed_store = Store(path, embedder="hash")
    rng = random.Random(42)
    words = (
        "database postgres index cache latency cluster backup restore token auth key "
        "graph memory agent review queue schedule budget invoice tenant region gateway "
        "proxy tunnel vault secret dashboard metric alert incident rollback migration "
        "schema replica shard lease worker python rust golang javascript typescript"
    ).split()
    with seed_store.tx():
        for i in range(300):
            w = rng.sample(words, rng.randint(5, 12))
            seed_store.add_item(
                "memory",
                f"note {i}: " + " ".join(w),
                project=f"p{i % 5}",
                tags=[f"tag_{i % 4}"],
            )
    return path


def test_sqlite_vec_matches_numpy_queries(store_db_path, monkeypatch):
    """Both search paths return the same top-10 results and sqlite-vec never builds _vm."""
    monkeypatch.setenv("COMMONS_SQLITE_VEC", "1")
    s_vec = Store(store_db_path, embedder="hash")
    assert s_vec._sqlite_vec is True

    monkeypatch.delenv("COMMONS_SQLITE_VEC", raising=False)
    s_np = Store(store_db_path, embedder="hash")
    assert s_np._sqlite_vec is False

    test_queries = [
        ("database query latency cluster", {}),
        ("vault secret token auth", {}),
        ("queue worker schedule", {}),
        ("backup restore replica", {"project": "p1"}),
        ("metric alert dashboard incident", {"project": "p2"}),
        ("cache proxy gateway tunnel", {"tags": ["tag_0"]}),
        ("tenant budget invoice", {"tags": ["tag_3"]}),
        ("postgres schema migration", {"mode": "semantic"}),
        ("python rust typescript", {"mode": "semantic", "project": "p0"}),
    ]

    for query, kwargs in test_queries:
        res_vec = s_vec.search(query, limit=10, **kwargs)["results"]
        res_np = s_np.search(query, limit=10, **kwargs)["results"]
        _assert_same_top_ids(res_vec, res_np)

    assert getattr(s_vec, "_vm", None) is None
    assert getattr(s_np, "_vm", None) is not None


def test_sqlite_vec_direct_vec_search(store_db_path, monkeypatch):
    """Direct _vec_search returns matching top-k (sim, item_id) without building _vm."""
    monkeypatch.setenv("COMMONS_SQLITE_VEC", "1")
    s_vec = Store(store_db_path, embedder="hash")

    monkeypatch.delenv("COMMONS_SQLITE_VEC", raising=False)
    s_np = Store(store_db_path, embedder="hash")

    qv = s_vec.embedder.embed_query("cluster latency performance")

    out_vec = s_vec._vec_search(qv, "i.superseded_by IS NULL", [], restrictive=False, k=10)
    out_np = s_np._vec_search(qv, "i.superseded_by IS NULL", [], restrictive=False, k=10)
    _assert_same_top_ids(out_vec, out_np)

    pid = s_vec.q("SELECT id FROM projects WHERE name=?", ("p2",))[0]["id"]
    out_vec_res = s_vec._vec_search(qv, "i.superseded_by IS NULL AND i.project_id=?", [pid], restrictive=True, k=10)
    out_np_res = s_np._vec_search(qv, "i.superseded_by IS NULL AND i.project_id=?", [pid], restrictive=True, k=10)
    _assert_same_top_ids(out_vec_res, out_np_res)

    assert getattr(s_vec, "_vm", None) is None
    assert getattr(s_np, "_vm", None) is not None
