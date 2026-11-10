import pytest


def test_a_new_memory_is_appended_to_the_vector_cache_not_reloaded(store):
    np = pytest.importorskip("numpy")
    for i in range(5):
        store.add_item("memory", f"note number {i} about deploys")
    ids, _, mat = store._vec_matrix()
    assert len(ids) == 5 and mat.shape[0] == 5
    seen, real_q = [], store.q
    store.q = lambda sql, args=(): (seen.append(sql), real_q(sql, args))[1]
    store.add_item("memory", "a sixth note about backups")
    ids2, index2, mat2 = store._vec_matrix()
    store.q = real_q
    assert len(ids2) == 6 and index2[ids2[-1]] == 5
    assert not any(s.startswith("SELECT item_id, vec FROM embeddings WHERE model=?") for s in seen)  # no full read
    del store._vm
    ids3, _, mat3 = store._vec_matrix()  # a full reload gives the same matrix
    assert ids3 == ids2 and np.allclose(mat2, mat3)
    store.delete_item(ids2[0])  # a delete is not an append: it reloads
    ids4, index4, mat4 = store._vec_matrix()
    assert len(ids4) == 5 and mat4.shape[0] == 5 and ids2[0] not in index4
