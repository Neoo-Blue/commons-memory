import pytest

from commons_memory.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"), embedder="hash")
