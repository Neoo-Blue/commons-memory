---
description: Hand off the current work through Commons so any other AI can resume it
---
Write a Commons `handoff` for the current session (call `session_start` first if there is none): summary, current state, decisions made (also `record_decision` for important ones with why + alternatives), next_steps, open_questions and relevant ids. $ARGUMENTS may name a `to_agent`. Then print the handoff id.
