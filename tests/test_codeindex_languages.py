"""Tree-sitter code index for Go, Rust, Java, Ruby, C, C++ and C#."""
import json
import textwrap

import pytest

from commons_memory.codeindex import index_repo

# One repo, seven languages: a function calling another, a container with a method, and an import.
# C has no methods; the struct is the container and the call is between two functions.
_FILES = {
    "demo.go": """\
        package demo

        import "fmt"

        type Server struct {
            Port int
        }

        func (s *Server) Listen() {
            helper()
            s.Stop()
        }

        func (s *Server) Stop() {}

        func helper() {
            fmt.Println("ok")
        }
    """,
    "demo.rs": """\
        use std::io::Read;

        struct Server {
            port: u16,
        }

        impl Server {
            fn listen(&self) {
                helper();
                self.ping();
            }
            fn ping(&self) {}
        }

        fn helper() {}
    """,
    "Demo.java": """\
        import java.util.List;

        class Server {
            void listen() {
                helper();
                this.ping();
                bag.get().pong();
            }
            void helper() {}
            void ping() {}
            void pong() {}
        }
    """,
    "demo.rb": """\
        require "json"

        class Server
          def listen
            helper()
            self.ping()
          end
          def ping
          end
        end

        def helper
        end
    """,
    "demo.c": """\
        #include <stdio.h>

        struct Server {
            int port;
        };

        void listen(void) {
            helper();
        }

        void helper(void) {}
    """,
    "demo.cpp": """\
        #include <vector>

        class Server {
        public:
            void listen() {
                helper();
                this->ping();
            }
            void ping() {}
        };

        void helper() {}
    """,
    "Demo.cs": """\
        using System.IO;

        class Server {
            void Listen() {
                Helper();
                this.Ping();
            }
            void Helper() {}
            void Ping() {}
        }
    """,
}

# rel, grammar, lang, symbols, module, calls (caller, callee, resolved)
_CASES = [
    ("demo.go", "tree_sitter_go", "go",
     {"Server": "class", "Server.Listen": "method", "Server.Stop": "method", "helper": "function"},
     "fmt", [("Server.Listen", "helper", "local"), ("Server.Listen", "Server.Stop", "unique")]),
    ("demo.rs", "tree_sitter_rust", "rust",
     {"Server": "class", "Server.listen": "method", "Server.ping": "method", "helper": "function"},
     "std::io::Read", [("Server.listen", "helper", "local"), ("Server.listen", "Server.ping", "self")]),
    ("Demo.java", "tree_sitter_java", "java",
     {"Server": "class", "Server.listen": "method", "Server.helper": "method", "Server.ping": "method",
      "Server.pong": "method"},
     "java.util.List", [("Server.listen", "Server.helper", "local"), ("Server.listen", "Server.ping", "self"),
                        ("Server.listen", "Server.pong", "unique")]),
    ("demo.rb", "tree_sitter_ruby", "ruby",
     {"Server": "class", "Server.listen": "method", "Server.ping": "method", "helper": "function"},
     "json", [("Server.listen", "helper", "local"), ("Server.listen", "Server.ping", "self")]),
    ("demo.c", "tree_sitter_c", "c",
     {"Server": "class", "listen": "function", "helper": "function"},
     "stdio.h", [("listen", "helper", "local")]),
    ("demo.cpp", "tree_sitter_cpp", "cpp",
     {"Server": "class", "Server.listen": "method", "Server.ping": "method", "helper": "function"},
     "vector", [("Server.listen", "helper", "local"), ("Server.listen", "Server.ping", "self")]),
    ("Demo.cs", "tree_sitter_c_sharp", "csharp",
     {"Server": "class", "Server.Listen": "method", "Server.Helper": "method", "Server.Ping": "method"},
     "System.IO", [("Server.Listen", "Server.Helper", "local"), ("Server.Listen", "Server.Ping", "self")]),
]


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for name, src in _FILES.items():
        (root / name).write_text(textwrap.dedent(src))
    return root


def _calls(store, caller, path):
    return {(r["name"], r["path"], json.loads(r["meta"])["resolved"]) for r in store.q(
        "SELECT e2.name AS name, e2.path AS path, r.meta AS meta FROM relations r "
        "JOIN entities e1 ON e1.id=r.src JOIN entities e2 ON e2.id=r.dst "
        "WHERE r.type='calls' AND e1.name=? AND e1.path=?", (caller, path))}


@pytest.mark.parametrize("rel,grammar,lang,symbols,module,calls", _CASES, ids=[c[2] for c in _CASES])
def test_codeindex_language(store, tmp_path, rel, grammar, lang, symbols, module, calls):
    pytest.importorskip(grammar)
    index_repo(store, str(_repo(tmp_path)), "poly")
    got = {r["name"]: r["type"] for r in store.q(
        "SELECT name, type FROM entities WHERE path=? AND type IN ('function','class','method')", (rel,))}
    assert got == symbols
    meta = json.loads(store.q("SELECT meta FROM entities WHERE type='file' AND path=?", (rel,))[0]["meta"])
    assert meta["lang"] == lang
    fid = store.q("SELECT id FROM entities WHERE type='file' AND path=?", (rel,))[0]["id"]
    mods = {r["name"] for r in store.q(
        "SELECT e.name AS name FROM relations r JOIN entities e ON e.id=r.dst "
        "WHERE r.src=? AND r.type='imports'", (fid,))}
    assert module in mods
    found = _calls(store, calls[0][0], rel)
    for caller, callee, how in calls:
        assert (callee, rel, how) in found


@pytest.mark.parametrize("name,grammar,lang,src", [
    ("a.h", "tree_sitter_c", "c", "int helper(void) { return 1; }\n"),
    ("b.cc", "tree_sitter_cpp", "cpp", "int helper() { return 1; }\n"),
    ("c.cxx", "tree_sitter_cpp", "cpp", "int helper() { return 1; }\n"),
    ("d.hpp", "tree_sitter_cpp", "cpp", "int helper() { return 1; }\n"),
    ("e.hh", "tree_sitter_cpp", "cpp", "int helper() { return 1; }\n"),
])
def test_codeindex_alias_extensions(store, tmp_path, name, grammar, lang, src):
    pytest.importorskip(grammar)
    root = tmp_path / "repo"
    root.mkdir()
    (root / name).write_text(src)
    index_repo(store, str(root), "poly")
    meta = json.loads(store.q("SELECT meta FROM entities WHERE type='file' AND path=?", (name,))[0]["meta"])
    assert meta["lang"] == lang
    got = {r["name"]: r["type"] for r in store.q(
        "SELECT name, type FROM entities WHERE path=? AND type IN ('function','class','method')", (name,))}
    assert got == {"helper": "function"}


def test_ruby_require_block_keeps_nested_method(store, tmp_path):
    pytest.importorskip("tree_sitter_ruby")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "demo.rb").write_text(textwrap.dedent("""\
        require "json" do
          def inner
            helper()
          end
        end

        def helper
        end
    """))
    index_repo(store, str(root), "poly")
    got = {r["name"]: r["type"] for r in store.q(
        "SELECT name, type FROM entities WHERE path=? AND type IN ('function','class','method')", ("demo.rb",))}
    assert got == {"inner": "function", "helper": "function"}
    assert ("helper", "demo.rb", "local") in _calls(store, "inner", "demo.rb")
    fid = store.q("SELECT id FROM entities WHERE type='file' AND path=?", ("demo.rb",))[0]["id"]
    mods = {r["name"] for r in store.q(
        "SELECT e.name AS name FROM relations r JOIN entities e ON e.id=r.dst WHERE r.src=? AND r.type='imports'", (fid,))}
    assert "json" in mods


def test_tree_sitter_off_skips_other_languages(store, tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_TREE_SITTER", "0")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "demo.go").write_text('package p\nimport "fmt"\nfunc helper() {}\n')
    st = index_repo(store, str(root), "poly")
    assert st["files"] == 1 and st["symbols"] == 0 and st["imports"] == 0
