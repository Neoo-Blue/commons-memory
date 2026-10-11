"""Mock CLI agent for tests/demos: reads the Commons prompt on stdin, prints a deterministic result."""
import os
import re
import sys

prompt = sys.stdin.read()
typ = os.environ.get("COMMONS_TASK_TYPE", "general")
name = os.environ.get("MOCK_NAME", "mock")
title = re.search(r"^## (.+)$", prompt, re.M)
print(f"[{name}] {typ}: {title.group(1) if title else ''}")
if "User preferences" in prompt:
    pref = re.search(r"^- (.+?) \[", prompt[prompt.index("User preferences"):], re.M)
    print(f"Applied preference: {pref.group(1) if pref else ''}")
if typ in ("review", "red_team", "test", "vote"):
    reject_first = os.environ.get("MOCK_REJECT_FIRST") and "attempt 1)" in prompt and typ == "review"
    print("VERDICT: reject\nMissing error handling." if reject_first else "VERDICT: approve")
if typ == "code":
    print("LEFTOVER: add integration tests for the edge cases")
print(f"TOKENS: {len(prompt) // 4}")
