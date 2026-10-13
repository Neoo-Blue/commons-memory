"""Roadmap: live LSP lookups (definition, references, hover) through the language's own server."""
import shutil
import sys
import textwrap

import pytest

from commons_memory import lsp
from commons_memory.codeindex import index_repo

FAKE = textwrap.dedent('''
    import json, sys
    def read():
        size = 0
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                sys.exit(0)
            if line in (b"\\r\\n", b"\\n"):
                break
            if line.lower().startswith(b"content-length:"):
                size = int(line.split(b":")[1])
        return json.loads(sys.stdin.buffer.read(size))
    def send(msg):
        body = json.dumps(msg).encode()
        sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
        sys.stdout.buffer.flush()
    uri = None
    while True:
        m = read()
        meth, mid = m.get("method"), m.get("id")
        if meth == "textDocument/didOpen":
            uri = m["params"]["textDocument"]["uri"]
            send({"jsonrpc": "2.0", "id": 99, "method": "workspace/configuration", "params": {}})  # servers ask things
        elif meth == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}})
        elif meth == "textDocument/definition":
            send({"jsonrpc": "2.0", "id": mid, "result": {"uri": uri, "range": {"start": {"line": 0, "character": 4}}}})
        elif meth == "textDocument/references":
            send({"jsonrpc": "2.0", "id": mid, "result": [{"uri": uri, "range": {"start": {"line": n, "character": 0}}} for n in (0, 3)]})
        elif meth == "textDocument/hover":
            send({"jsonrpc": "2.0", "id": mid, "result": {"contents": {"kind": "plaintext", "value": "def add(a: int, b: int) -> int"}}})
        elif meth == "shutdown":
            send({"jsonrpc": "2.0", "id": mid, "result": None})
        elif meth == "exit":
            sys.exit(0)
''')


def test_lookups_speak_lsp_and_resolve_symbols_through_the_code_index(store, tmp_path, monkeypatch):
    (tmp_path / "fake.py").write_text(FAKE)
    monkeypatch.setenv("COMMONS_LSP_PYTHON", f"{sys.executable} {tmp_path / 'fake.py'}")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\ntotal = add(1, 2)\n")
    index_repo(store, str(repo), "calc")
    d = lsp.find(store, "add", "calc", "definition")
    assert d["at"] == "calc.py:1:5" and d["results"][0] == {"path": "calc.py", "line": 1, "col": 5, "text": "def add(a, b):"}
    assert [r["line"] for r in lsp.find(store, "add", "calc", "references")["results"]] == [1, 4]
    assert "-> int" in lsp.lookup(str(repo), "calc.py", 1, 5, "hover")["hover"]
    with pytest.raises(Exception, match="isn't in the code index"):
        lsp.find(store, "nope", "calc")


@pytest.mark.skipif(not shutil.which("clangd"), reason="clangd not installed")
def test_a_real_language_server_answers(tmp_path):
    (tmp_path / "calc.c").write_text("int add(int a, int b) { return a + b; }\n\nint twice(int x) { return add(x, x); }\n")
    assert lsp.lookup(str(tmp_path), "calc.c", 3, 27, "definition")["results"][0]["line"] == 1
    assert len(lsp.lookup(str(tmp_path), "calc.c", 1, 5, "references")["results"]) == 2
    assert "int add(int a, int b)" in lsp.lookup(str(tmp_path), "calc.c", 1, 5, "hover")["hover"]
