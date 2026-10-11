import os
import uuid

import pytest

from commons_memory.store import Store

PG = os.environ.get("COMMONS_TEST_PG")  # e.g. postgresql://commons@127.0.0.1:54329/commons -> whole suite on Postgres


def make_store(tmp_path, name="t.db", embedder="hash"):
    if PG:
        sep = "&" if "?" in PG else "?"
        return Store(f"{PG}{sep}schema=t_{uuid.uuid4().hex[:12]}", embedder=embedder)
    return Store(str(tmp_path / name), embedder=embedder)


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)
