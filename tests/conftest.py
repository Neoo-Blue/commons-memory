import os
import tempfile
import uuid

import pytest

from commons_memory.store import Store

os.environ.setdefault("COMMONS_EMBEDDER", "hash")  # deterministic tests; fastembed is exercised explicitly
os.environ["COMMONS_DRIVE_DIR"] = tempfile.mkdtemp(prefix="commons-test-drive-")  # never the real ~/.commons/drive
os.environ["COMMONS_VAULT_KEY_FILE"] = os.path.join(tempfile.mkdtemp(prefix="commons-test-vault-"), "vault.key")
PG = os.environ.get("COMMONS_TEST_PG")  # e.g. postgresql://commons@127.0.0.1:54329/commons -> whole suite on Postgres


def make_store(tmp_path, name="t.db", embedder="hash"):
    if PG:
        sep = "&" if "?" in PG else "?"
        return Store(f"{PG}{sep}schema=t_{uuid.uuid4().hex[:12]}", embedder=embedder)
    return Store(str(tmp_path / name), embedder=embedder)


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    yield s
    if PG:  # each test's schema goes when the test does (4,579 left behind once filled 12 GB)
        try:
            s.db.close()  # an open transaction on this connection would block the DROP
        except Exception:  # noqa: BLE001
            pass
        _drop_schemas(f"t\\_{s.path.rsplit('schema=t_', 1)[-1]}")


def _drop_schemas(like: str = "t\\_%") -> None:
    import psycopg
    with psycopg.connect(PG.split("?", 1)[0], autocommit=True) as c:
        for (n,) in c.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE %s", (like,)).fetchall():
            c.execute(f'DROP SCHEMA IF EXISTS "{n}" CASCADE')


def pytest_sessionfinish(session, exitstatus):
    if PG:  # schemas made by make_store() directly
        _drop_schemas()
