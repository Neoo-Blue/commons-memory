# Retro learning

`commons learn` reads your past AI-assistant history and project artifacts. It turns what it finds into Commons knowledge, and you review everything before it is applied.

```bash
commons learn --dry-run                      # what would be learned, estimated tokens and cost; writes nothing
commons learn                                # extract into the review queue (incremental)
commons learn queue [--kind decision]        # review
commons learn approve ID... | all [--kind preference]
commons learn reject ID... | all
commons learn status                         # detected sources, runs, pending counts
commons learn --source git --repo ~/code/app --source memory-files --path memory-files=~/code/app
```

The same features are available as MCP tools (`retro_learn`, which defaults to dry_run and is admin only; `retro_queue`; `retro_decide`) and on the dashboard's **Retro learning** page, which has bulk select, approve and reject.

## Sources

| Source | Default location | Notes |
|---|---|---|
| `claude-code` | `~/.claude/projects/**/*.jsonl` | User and assistant text only. Tool calls and results are dropped. The project is the session `cwd`. The model is recorded. Incremental by byte offset. |
| `codex` | `~/.codex/sessions/**/rollout-*.jsonl` | `response_item` messages. The model comes from `turn_context`. Environment and instruction preambles are skipped. The legacy `.json` format is supported. |
| `cursor` | `state.vscdb` in Cursor's `globalStorage` and `workspaceStorage` (Linux, macOS, Windows) | `composerData:*` with inline `conversation` or separate `bubbleId:*` rows, plus the legacy `aichat` chat data. The DB is copied first and opened read-only. Markdown exports are read with `--path cursor=FILE.md`. |
| `chatgpt` | `~/Downloads/chatgpt-export*/conversations.json`, or `--path` | Walks the `current_node` branch, so abandoned edits are ignored. |
| `claude-ai` | `~/Downloads/claude-export*/conversations.json` | `chat_messages` |
| `gemini` | `~/.gemini/tmp/*/` | `chats/session-*.json`, `logs.json` and `checkpoint-*.json` |
| `generic` | `--path` | `.md`/`.txt` with `User:` or `## Assistant` turns, or `.json`/`.jsonl` role/content lists |
| `git` | `--repo` (default `.`) | Commits, incremental from the last seen HEAD. PR titles and bodies come from `gh pr list` when `gh` is installed and authenticated. |
| `memory-files` | `.` plus `~/.claude`, `~/.codex`, `~/.gemini` | CLAUDE.md, AGENTS.md, GEMINI.md, .cursorrules, .cursor/rules/*.mdc, .windsurfrules, mem0 JSON exports, and basic-memory notes (`- [category] text`) |

## What is extracted

| Kind | Applied as | Heuristic signal |
|---|---|---|
| preference | `preferences` (source `retro:<src>`, confidence capped at 0.8) | "I prefer / always / never / use X instead of Y" in user turns, and memory-file bullets |
| decision | `decision` item with `Rationale:` | "decided / going with / switched to …" plus "because / since / so that" |
| fact | `memory` item tagged `fact` (feeds the project KB) | "the project uses / runs on / is deployed to …", memory-file bullets, `feat:` commits |
| leftover | open leftover (picked up by `pickup`) | TODO / next steps / still need to / not yet implemented near the end of a conversation |
| performance | `model_scores` for (model, task type), which feeds routing | Feedback in later user turns ("that works" vs "still broken") |
| skill | skill-pool **draft**, which goes through the normal review gate | A "how do I…" question answered with 4 or more numbered steps |

When a model is assigned to the `retro` role (or the distributor role), each chunk of about 3k tokens goes to that model for JSON extraction. Spending is capped by `--budget` / `COMMONS_RETRO_BUDGET` (default $1). Once the budget runs out, or when the model's output can't be parsed, extraction falls back to the heuristics. The dry run reports estimated tokens and cost for that model.

## Guarantees

- **Secrets are redacted** before any text is chunked, sent to a model or queued.
- **Provenance.** Every candidate records its source kind, file, conversation id, title, timestamp, confidence and method (llm or heuristic). This is stored in the item's `meta.retro`.
- **Dedupe.** Duplicates are dropped by fingerprint (anything queued, applied or rejected before) and by comparison with existing preferences, open leftovers, skills and similar memories.
- **Idempotent, incremental re-runs.** Checkpoints are kept per source per run: byte offsets, file hashes, per-conversation message counts and the last git commit. `commons learn reset` forgets them.
- **Review first.** Nothing is applied without review unless you pass `--auto-approve` (optionally with `--min-confidence`).
