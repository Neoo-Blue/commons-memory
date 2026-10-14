"""web/kit.js: syntax of the assembled page script, and the pure helpers run under node's vm with a tiny DOM shim."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
src_dir = str(SRC)
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)
import commons_memory  # noqa: E402  (conftest may already have loaded another checkout)

cm_dir = os.path.join(src_dir, "commons_memory")
if cm_dir not in commons_memory.__path__:
    commons_memory.__path__.insert(0, cm_dir)
sys.modules.pop("commons_memory.dashboard", None)

from commons_memory import dashboard  # noqa: E402

KIT = SRC / "commons_memory" / "web" / "kit.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def _page_script() -> str:
    page = dashboard.PAGE
    return page[page.index("<script>") + len("<script>"):page.rindex("</script>")]


def _node(tmp_path, name, code, *args):
    f = tmp_path / name
    f.write_text(code)
    return subprocess.run([NODE, *args, str(f)], capture_output=True, text=True, timeout=60)


def test_kit_is_loaded_after_legacy():
    assert dashboard.JS_FILES.index("kit.js") > dashboard.JS_FILES.index("legacy.js")
    assert "var K = (() =>" in _page_script()


def test_no_em_dash():
    text = KIT.read_text()
    assert "—" not in text and "&mdash;" not in text and "&#8212;" not in text


@needs_node
def test_node_check_kit_and_page(tmp_path):
    r = subprocess.run([NODE, "--check", str(KIT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = _node(tmp_path, "page.js", _page_script(), "--check")
    assert r.returncode == 0, r.stderr


HELPERS = r"""
const vm = require("vm"), fs = require("fs"), assert = require("assert");
const noop = () => {};
const store = { get localStorage() { throw new Error("storage blocked"); } };  // proves the try/catch around it
const ctx = vm.createContext({
  console, URLSearchParams, setTimeout: noop, clearTimeout: noop, requestAnimationFrame: noop,
  navigator: { platform: "MacIntel", userAgent: "" },
  location: { hash: "#Board", origin: "http://x", pathname: "/" },
  history: { state: null, pushState: noop, replaceState: noop, back: noop },
  document: { addEventListener: noop, querySelector: () => null, activeElement: null, body: {} },
  addEventListener: noop,
});
Object.defineProperty(ctx, "localStorage", { get: () => store.localStorage });
vm.runInContext(`
  var window = this;
  var T = ["Overview","Board","Capacity","Setup","Agents","Memory","Org chart","MCP hub","Mission control","Secrets","Graph","Usage","Workflow"];
  var GROUPS = [["Live", ["Overview","Board"]]], ICON = { Memory: '<path d="M1 1"/>' }, tab = "Board";
  var E = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  var $ = () => null;
`, ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx, { filename: "kit.js" });
const K = ctx.K;
assert.ok(K && K.fmt && K.drawer && K.palette && K.keys && typeof K.refresh === "function", "K is exposed");

// formatting
const num = K.fmt.num;
assert.strictEqual(num(0), "0"); assert.strictEqual(num(999), "999"); assert.strictEqual(num(1200), "1.2k");
assert.strictEqual(num(12345), "12.3k"); assert.strictEqual(num(150000), "150k"); assert.strictEqual(num(3400000), "3.4M");
assert.strictEqual(num(999950), "1M"); assert.strictEqual(num(999940), "1M"); assert.strictEqual(num(-1500), "-1.5k");
assert.strictEqual(num("x"), "0"); assert.strictEqual(K.fmt.tokens(12345), "12.3k tok");
assert.strictEqual(K.fmt.money(1234.5), "$1,234.50"); assert.strictEqual(K.fmt.money(0.0042), "$0.0042");
assert.strictEqual(K.fmt.money(0), "$0.00"); assert.strictEqual(K.fmt.money(null), "$0.00");
const now = Date.parse("2026-10-13T12:00:00Z");
assert.strictEqual(K.fmt.ago(now / 1000 - 90, now), "1m ago");
assert.strictEqual(K.fmt.ago(now - 3 * 3600e3, now), "3h ago");           // epoch milliseconds
assert.strictEqual(K.fmt.ago("2026-10-11T12:00:00Z", now), "2d ago");
assert.strictEqual(K.fmt.ago(String(now / 1000 - 30), now), "30s ago");    // numeric string
assert.strictEqual(K.fmt.ago(now / 1000 + 300, now), "in 5m");
assert.strictEqual(K.fmt.ago(now / 1000, now), "just now");
assert.strictEqual(K.fmt.ago("not a date", now), ""); assert.strictEqual(K.fmt.ago(null, now), "");

// kv escapes keys and values, truncates long values behind an expandable button
const kv = K.kv({ "<k>": "<img src=x onerror=alert(1)>", n: { a: [1, 2] }, long: "y".repeat(100) + "<b>" });
assert.ok(!kv.includes("<img") && !kv.includes("<k>") && !kv.includes("<b>"), kv);
assert.ok(kv.includes("&lt;img src=x onerror=alert(1)&gt;") && kv.includes("&lt;k&gt;"));
assert.ok(kv.includes('{&quot;a&quot;:[1,2]}'));
assert.ok(kv.includes('data-kv-full="' + "y".repeat(100) + '&lt;b&gt;"') && kv.includes("…"));
assert.ok(K.kv("plain").includes(">value<"));

// empty, skeleton, spark
const em = K.empty({ icon: "Memory", title: "<x>", hint: "a & b", actions: [{ label: "<go>", run: noop }] });
assert.ok(em.includes("&lt;x&gt;") && em.includes("a &amp; b") && em.includes("&lt;go&gt;") && /data-kact="k\d+"/.test(em));
assert.ok(!em.includes("<x>") && em.includes("<svg"));
assert.strictEqual((K.skeleton(3).match(/skeleton-line/g) || []).length, 3);
assert.strictEqual(K.spark([]), "");
assert.ok(/<svg[^>]*width="80"/.test(K.spark([1, 2, 3], { w: 80, h: 20 })) && K.spark([5]).includes("polyline"));

// fuzzy ranking over page names
const best = q => ctx.T.map(t => [t, K.fuzzy(q, t)]).filter(x => x[1]).sort((a, b) => b[1].score - a[1].score)[0][0];
assert.strictEqual(best("mc"), "MCP hub"); assert.strictEqual(best("ov"), "Overview"); assert.strictEqual(best("set"), "Setup");
assert.strictEqual(best("brd"), "Board"); assert.strictEqual(best("mem"), "Memory"); assert.strictEqual(best("g"), "Graph");
assert.strictEqual(best("oc"), "Org chart"); assert.strictEqual(best("mis con"), "Mission control");
assert.strictEqual(K.fuzzy("zzq", "Board"), null);
assert.deepStrictEqual(Array.from(K.fuzzy("bd", "Board").pos), [0, 4]);

// combo parsing and event normalisation
assert.deepStrictEqual(Array.from(K.keys.parse("g o")), ["g", "o"]);
assert.deepStrictEqual(Array.from(K.keys.parse("mod+k")), ["meta+k"]);   // platform MacIntel
assert.deepStrictEqual(Array.from(K.keys.parse("Shift+Ctrl+P")), ["ctrl+shift+p"]);
assert.deepStrictEqual(Array.from(K.keys.parse("?")), ["?"]);
assert.deepStrictEqual(Array.from(K.keys.parse("esc")), ["escape"]);
assert.deepStrictEqual(Array.from(K.keys.parse("ctrl++")), ["ctrl++"]);
assert.strictEqual(K.keys.fromEvent({ key: "?", shiftKey: true }), "?");
assert.strictEqual(K.keys.fromEvent({ key: "K", metaKey: true, shiftKey: true }), "meta+shift+k");
assert.strictEqual(K.keys.fromEvent({ key: "Escape" }), "escape");
assert.strictEqual(K.keys.kbd("mod+k"), "<kbd>⌘</kbd><kbd>k</kbd>");
assert.strictEqual(K.keys.kbd("g o"), "<kbd>g</kbd> <kbd>o</kbd>");

// event bus
let got = [];
const off = K.on("change", d => got.push(d));
K.on("change", () => { throw new Error("one bad handler"); });
const err = console.error; console.error = noop;
K.emit("change", 1); off(); K.emit("change", 2);
console.error = err;
assert.deepStrictEqual(got, [1]);
console.log("kit helpers ok");
"""


@needs_node
def test_pure_helpers_under_vm(tmp_path):
    (tmp_path / "helpers.js").write_text(HELPERS)
    r = subprocess.run([NODE, str(tmp_path / "helpers.js"), str(KIT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "kit helpers ok" in r.stdout
