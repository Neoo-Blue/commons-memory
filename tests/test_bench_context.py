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


def test_a_question_is_never_asked_before_the_last_session_of_its_haystack():
    q = {"question_date": "2023/03/10 (Fri) 03:39", "haystack_dates": ["2023/02/01 (Wed) 10:00", "2023/03/15 (Wed) 09:00"]}
    assert bench._lme_asked(q) == bench._lme_date("2023/03/15 (Wed) 09:00")  # the evidence is dated after the question
    q["haystack_dates"] = ["2023/02/01 (Wed) 10:00"]
    assert bench._lme_asked(q) == bench._lme_date("2023/03/10 (Fri) 03:39")


def test_the_keyword_query_keeps_what_is_asked_and_drops_when_and_that_it_was_said(monkeypatch):
    from commons_memory import ranking as R
    assert R.fts_query("What kitchen appliance did I buy 10 days ago?") == '"kitchen" OR "appliance" OR "buy"'
    assert R.fts_query("I mentioned cooking something for my friend a couple of days ago. What was it?") == '"cooking" OR "friend"'
    assert '"week"' in R.fts_query("What did we decide last week about pricing?")  # only an exact "... ago" is taken out
    assert R.fts_query("three days ago") == '"three" OR "days" OR "ago"'            # nothing else to search for: kept
    monkeypatch.setenv("COMMONS_RANK_WHAT_ONLY", "0")
    assert '"days"' in R.fts_query("What kitchen appliance did I buy 10 days ago?")


def test_an_exact_time_scores_highest_at_its_middle():
    from commons_memory import ranking as R
    ref, day = 1_700_000_000.0, 86400.0
    win, focus = R.time_window("what did I buy 10 days ago", ref), R.time_focus("what did I buy 10 days ago", ref)
    assert focus == ref - 10 * day and R.time_focus("what did I do last month", ref) is None
    at = lambda d: R.time_score(ref - d * day, win, focus, 0.5)
    assert at(10) == 1.0 and 0.5 <= at(8.1) < at(9) < 1.0 and at(30) < at(12.5) < 0.5
    assert R.time_score(ref - 9 * day, win) == 1.0  # no focus: flat inside the window, as before
