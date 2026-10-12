"""Commons on LoCoMo under MemPalace's metric: per question, fraction of evidence ids (dialog turns or sessions)
present in the top-k, averaged over every question (no evidence = 1.0), as in mempalace/benchmarks/locomo_bench.py."""
import json, os, re, sys
from collections import defaultdict
from pathlib import Path
os.environ["COMMONS_TRUST_WEIGHTING"] = "0"
from commons_memory.store import Store
HERE = Path(__file__).parent / "locomo_stores"
DATA = Path.home() / "genesis/commons-bench-data/locomo10.json"
K = int(sys.argv[1]) if len(sys.argv) > 1 else 10
rec = {"dialog": [], "session": []}
cat = {"dialog": defaultdict(list), "session": defaultdict(list)}
for ci, conv in enumerate(json.load(open(DATA))):
    s = Store(str(HERE / f"c{ci}-fastembed.db"), embedder="fastembed")
    meta = json.load(open(HERE / f"c{ci}-fastembed.json"))
    for qa in conv["qa"]:
        ev = qa.get("evidence", []) or []
        ids, _, _ = s.rank(qa["question"], limit=200, as_of=meta["ref"])
        turns, sess = [], []
        for i in ids:
            t = meta["turn"].get(i)
            if t and t not in turns:
                turns.append(t)
            g = meta["sess"].get(i)
            g = f"session_{g[1:]}" if g else None
            if g and g not in sess:
                sess.append(g)
        dset = set(ev)
        sset = {f"session_{m.group(1)}" for e in ev for m in [re.match(r"D(\d+):", e)] if m}
        for gran, gold, got in (("dialog", dset, turns[:K]), ("session", sset, sess[:K])):
            r = 1.0 if not gold else sum(1 for e in gold if e in got) / len(gold)
            rec[gran].append(r)
            cat[gran][qa["category"]].append(r)
for gran in ("dialog", "session"):
    v = rec[gran]
    print(f"Commons {gran:<7} top-{K}: Avg Recall {sum(v)/len(v):.3f} (n={len(v)})  " +
          "  ".join(f"cat{c}={sum(x)/len(x):.3f}" for c, x in sorted(cat[gran].items())))
