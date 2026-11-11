import json

from commons_memory import bench


def test_context_cost_counts_the_qa_prompt_against_the_whole_history(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_TRUST_WEIGHTING", "0")  # the benchmark sets it for the process; put it back afterwards
    turns = [{"speaker": "Ana" if i % 2 else "Ben", "dia_id": f"D1:{i}",
              "text": f"turn {i}: " + ("we talked about the garden and the tomato seeds for a long while " * 4)} for i in range(40)]
    turns[7]["text"] = "Ben adopted a grey cat named Miso last spring."
    conv = {"sample_id": "c1", "conversation": {"session_1": turns, "session_1_date_time": "1:56 pm on 8 May, 2023"},
            "qa": [{"question": "What is the name of Ben's cat?", "answer": "Miso", "category": 1, "evidence": ["D1:7"]},
                   {"question": "skipped", "answer": "x", "category": 5}]}
    data = tmp_path / "locomo.json"
    data.write_text(json.dumps([conv]))
    s = bench.context_cost("locomo", str(data), str(tmp_path / "out"), k=3, embedder="hash", neighbors=2)
    assert s["n"] == 1 and s["whole_history"]["mean"] > s["with_commons"]["mean"] > 0
    assert 50 < s["saved_pct"] < 100 and s["times_smaller"] > 2
    row = json.loads((tmp_path / "out" / "context-locomo.jsonl").read_text().splitlines()[0])
    assert row["with"] < row["without"] and json.loads((tmp_path / "out" / "context-locomo.summary.json").read_text())["n"] == 1
