import base64
import os

from commons_memory import onboarding as O
from commons_memory import settings


def test_onboarding_keys_judges_and_checklist(store, monkeypatch):
    monkeypatch.setenv("COMMONS_VAULT_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    st = O.status(store)
    step = {x["step"]: x["done"] for x in st["steps"]}
    assert step["Vault ready (keys stored encrypted)"] and not step["Decision agent can answer"]
    O.set_provider(store, "anthropic", "sk-ant-test-123456")
    O.set_provider(store, "ollama", base_url="http://127.0.0.1:11434")
    row = store.q("SELECT key_ref FROM providers WHERE name='anthropic'")[0]
    assert row["key_ref"] == "commons://anthropic_api"                    # the provider reads its key from the vault
    from commons_memory.secrets import Broker
    assert Broker(store).resolve_internal(row["key_ref"], "test") == "sk-ant-test-123456"
    st = O.status(store)
    ready = {j["entry"]: j["ready"] for j in st["judges"]}
    assert ready["anthropic/claude-haiku-5-5"] and not ready["typesafe"] and ready["ask"]
    assert {x["step"]: x["done"] for x in st["steps"]}["Decision agent can answer"]
    assert all(c["credential"] != "anthropic_api" for c in st["credentials"])  # provider keys aren't "your services"
    settings.put(store, "capture_verbatim", False)
    assert not settings.get(store, "capture_verbatim")
    assert {x["step"]: x["done"] for x in O.status(store)["steps"]}["Features reviewed"]
    O.remove_provider(store, "anthropic")
    assert not store.q("SELECT 1 FROM providers WHERE name='anthropic'")
