"""MCP prompts and resources on Commons' own server. Clients show prompts as slash commands (in Claude Code:
/mcp__commons__checkin and friends), so the routine is one keystroke away; resources let a client attach the playbook,
a project's board, the team view or any memory by reference."""
from __future__ import annotations

import json


def register_prompts(mcp, store, who) -> None:
    @mcp.prompt(name="checkin", description="Check in with Commons: what's waiting for you, and the user's preferences")
    def checkin() -> str:
        return ("Check in with Commons now: call pickup() and msg_recv(), follow the preferences and playbook they "
                "return, and tell me in a few lines what is waiting for you and what you'll do first.")

    @mcp.prompt(name="start", description="Claim a task in Commons before starting it (no duplicate work)")
    def start(task: str) -> str:
        return (f"Before you start this task, call start_work(what={json.dumps(task)}) and follow its answer: if "
                "another agent has it, don't start; if it was done before, build on that; read the `related` memories "
                "and update the ones your change makes outdated. Then do the task and finish with finish_work.")

    @mcp.prompt(name="handover", description="Hand everything you hold over to Commons")
    def handover() -> str:
        return ("Call handover_status() (through call('handover_status', {}) if it isn't in your tool list) and hand "
                "over everything it says Commons still needs from you: profile and reach, memories, keys "
                "(vault_deposit), scheduled tasks (schedule_report), usage windows, current work. Report what you did.")

    @mcp.prompt(name="wrapup", description="Finish a session properly: board, claims, handoff, usage")
    def wrapup() -> str:
        return ("Before you stop: check board() for anything I asked that is still open, close your work with "
                "finish_work (with the result), then call handoff(summary, next_steps, open_questions) and "
                "report_usage(). Tell me what is done and what is still open.")

    @mcp.prompt(name="whats_new", description="What changed in Commons and what you must adapt")
    def whats_new() -> str:
        return ("Call whats_new() (through call('whats_new', {}) if it isn't listed), then tell me in a few lines what "
                "changed in Commons and what you are changing in how you work.")

    @mcp.prompt(name="remember", description="Save something to Commons' shared memory")
    def remember(fact: str) -> str:
        return (f"Save this to Commons with remember(content={json.dumps(fact)}) and confirm the id. If it is a "
                "decision, use record_decision(what, why) instead.")

    @mcp.prompt(name="skill", description="Load a skill from the Commons pool and follow it (nothing to install)")
    def skill_prompt(name: str) -> str:
        from .skillpool import SkillPool
        try:
            sk = SkillPool(store).get(name, include_files=True)
        except Exception as e:  # noqa: BLE001
            return (f"The skill {name!r} isn't available from the Commons pool ({e}). If another agent has it, ask "
                    f"them with skill_help(name={json.dumps(name)}, task=<what you need>).")
        files = sk.get("files") or {}
        extra = "".join(f"\n\n--- file: {k} ---\n{v}" for k, v in files.items()) if isinstance(files, dict) else ""
        return (f"Follow this skill from the Commons pool (version {sk['version']}). If you can't run part of it, use "
                f"skill_help(name={json.dumps(name)}, task=...).\n\n{sk['content']}{extra}")

    @mcp.resource("commons-hub://skill/{name}", name="skill", description="A skill from the Commons pool",
                  mime_type="text/markdown")
    def skill_res(name: str) -> str:
        from .skillpool import SkillPool
        return SkillPool(store).get(name)["content"]

    @mcp.resource("commons-hub://playbook", name="playbook", description="The routine every agent follows",
                  mime_type="text/plain")
    def playbook() -> str:
        from .server import INSTRUCTIONS
        return INSTRUCTIONS

    @mcp.resource("commons-hub://team", name="team", description="What every agent is doing right now",
                  mime_type="application/json")
    def team() -> str:
        from .status import team as _team
        return json.dumps(_team(store), default=str)

    @mcp.resource("commons-hub://board/{project}", name="board", description="A project's board: asks, targets, "
                  "actions, deliveries", mime_type="text/plain")
    def board(project: str) -> str:
        from . import board as B
        return B.view(store, project, "", "open", max_tokens=3000)["text"]

    @mcp.resource("commons-hub://memory/{item_id}", name="memory", description="Any memory, decision or handoff by id",
                  mime_type="application/json")
    def memory(item_id: str) -> str:
        return json.dumps(store.expand(item_id, depth=0, max_tokens=3000), default=str)
