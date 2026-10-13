"""Regenerate the prompt block in PROMPT.md from the master instructions (the single source), so it never goes stale."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from commons_memory.instructions import master  # noqa: E402

p = Path(__file__).resolve().parents[1] / "PROMPT.md"
text = p.read_text()
block = "```text\n" + master("{{COMMONS_URL}}", "{{AGENT_NAME}}") + "\n```"
new = re.sub(r"```text\n.*?\n```", lambda _m: block, text, count=1, flags=re.S)
if "--check" in sys.argv:
    sys.exit(0 if new == text else "PROMPT.md is stale; run scripts/gen_prompt.py")
p.write_text(new)
print("wrote", p)
