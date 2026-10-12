# Temporal facts, verbatim sweep, spaces, diagrams (v0.8)

## Three-step search (progressive disclosure)
1. `search_index(query)` returns ranked ids, each with a gist of about 12 words (roughly 15 tokens per hit).
2. `timeline_around(id)` shows the items just before and after a hit.
3. `get_items([ids])` returns the full content, within a token budget.

## Temporal facts
Facts have a validity window: they hold from `valid_from` until `valid_to`.

- `fact_set(subject, predicate, value, valid_from)` records a new value. The previous value is closed at exactly
  that instant; it is not deleted.
- `fact_get(subject, as_of=…)` returns what was true at that time.
- `fact_history` lists every value a fact has had.
- `fact_invalidate` ends a fact.

Backfilled older values slot into the past. Relations also carry `valid_from` and `valid_to`.

CLI: `commons fact set|get|history|invalidate`.

## Verbatim sweep
`commons sweep PATH… [--project P]` stores each message as its own `message` item, tagged `verbatim`. Messages
are redacted and deduplicated by hash, keep their original timestamp, and can be read in context with
`timeline_around`. The same parsers as retro learning are used: Claude Code, Codex, ChatGPT, Claude.ai, Gemini and
generic logs.

## Scoped spaces and audit
A space is one of:
- `project:NAME`
- `topic:TAG`
- `person:NAME` — a person is an entity (`entities=["person:Bob"]`) and can span projects.

Tools: `spaces`, `space_search(scope, query)`, and `memory_audit`, which gives an organisation score plus fixes for
orphans, duplicates, conflicts and stale leftovers.

## Interactive diagrams
- `commons diagram code PROJECT` draws the file-level import and call graph from the code index.
- `commons diagram job JOB_ID` draws the plan as a DAG, showing agent, model, effort, cost and result for each
  subtask.
- The MCP tool `diagram` writes the file to Commons Drive.

The output is one self-contained HTML file with no CDN dependencies. It supports search, click-to-inspect,
upstream and downstream tracing, pan and zoom, and SVG or PNG export. The style was inspired by
[Archify](https://github.com/tt-a1i/archify) (MIT). It is an independent implementation and contains no Archify
code.
