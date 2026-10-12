"""Free-text contradiction detection between two memories: TypeSafe (jev) vs Claude Haiku 5.5 on a labeled set.
Pairs are written once by Sonnet 5.5 (cached in contra_pairs.json): contradiction / paraphrase-or-update-consistent /
related-but-compatible. Run: sesame run typesafe -- sesame run anthropic -- python -I ts_contra.py"""
import json, os, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
PAIRS = HERE / "contra_pairs.json"


def post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers | {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def claude(model, system, user, max_tokens=800):
    d = post("https://api.anthropic.com/v1/messages", {"model": model, "max_tokens": max_tokens, "system": system,
                                                       "messages": [{"role": "user", "content": user}]},
             {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
    return "".join(c.get("text", "") for c in d["content"] if c.get("type") == "text"), d.get("usage", {})


if not PAIRS.exists():
    spec = ("Write evaluation data for detecting contradictions between two notes in a team's shared AI memory "
            "(software teams: deploys, configs, owners, schedules, preferences, decisions, people's facts). Return JSON "
            "only: a list of objects {\"a\":..., \"b\":..., \"label\":...}. Make 30 with label \"contradict\" (b states "
            "something incompatible with a, often subtly: different number, day, owner, negation, reversed decision), "
            "30 with label \"consistent\" (paraphrase, or b adds compatible detail), and 30 with label \"related\" (same "
            "topic and many shared words but no conflict, e.g. different service, different environment, or different "
            "time period explicitly). Vary length 8-40 words. No markdown fences.")
    text, _ = claude("claude-sonnet-5-5", "You produce clean JSON datasets.", spec, max_tokens=12000)
    PAIRS.write_text(json.dumps(json.loads(text[text.find("["):text.rfind("]") + 1]), indent=1))
pairs = json.loads(PAIRS.read_text())


def ts(p):
    t = time.perf_counter()
    d = post("https://api.typesafe.ai/v1/systemone", {
        "state": f"Note A: {p['a']}\nNote B: {p['b']}", "model": "jev-latest",
        "questions": {"c": {"type": "noul", "instructions": "Do note A and note B contradict each other, so that both "
                            "cannot be true at the same time about the same thing?"}}},
        {"Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"]})
    return d["answers"]["c"]["noul"], (time.perf_counter() - t) * 1000


def haiku(p):
    t = time.perf_counter()
    text, u = claude("claude-haiku-5-5", "You check two notes from a team memory. Reply with exactly CONTRADICT if both "
                     "cannot be true at the same time about the same thing, otherwise OK.",
                     f"Note A: {p['a']}\nNote B: {p['b']}", max_tokens=300)
    return (1.0 if "CONTRADICT" in text.upper() else 0.0), (time.perf_counter() - t) * 1000


def score(name, fn, thr=0.5):
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(fn, pairs))
    tp = sum(1 for p, (v, _) in zip(pairs, res) if v >= thr and p["label"] == "contradict")
    fp = sum(1 for p, (v, _) in zip(pairs, res) if v >= thr and p["label"] != "contradict")
    fn_ = sum(1 for p, (v, _) in zip(pairs, res) if v < thr and p["label"] == "contradict")
    lat = sorted(ms for _, ms in res)
    fp_by = {l: sum(1 for p, (v, _) in zip(pairs, res) if v >= thr and p["label"] == l) for l in ("consistent", "related")}
    print(f"{name:10} thr={thr:.2f} precision={tp / max(1, tp + fp):.3f} recall={tp / max(1, tp + fn_):.3f} "
          f"false_pos={fp_by} p50={lat[len(lat) // 2]:.0f}ms p95={lat[int(len(lat) * .95)]:.0f}ms")
    return res


print(f"{len(pairs)} pairs:", {l: sum(p["label"] == l for p in pairs) for l in ("contradict", "consistent", "related")})
r = score("typesafe", ts)
for thr in (0.7, 0.85):
    tp = sum(1 for p, (v, _) in zip(pairs, r) if v >= thr and p["label"] == "contradict")
    fp = sum(1 for p, (v, _) in zip(pairs, r) if v >= thr and p["label"] != "contradict")
    print(f"typesafe   thr={thr:.2f} precision={tp / max(1, tp + fp):.3f} recall={tp / 30:.3f}")
score("haiku-5.5", haiku)
