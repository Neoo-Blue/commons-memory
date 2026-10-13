from commons_memory import skillsync as SS


def test_pool_skills_appear_in_every_client_without_touching_the_users_own(tmp_path):
    claude, agents = tmp_path / "claude", tmp_path / "agents"
    claude.mkdir(); agents.mkdir()
    (claude / "mine").mkdir()
    (claude / "mine" / "SKILL.md").write_text("my own skill")
    skills = [{"name": "deploy-nas", "description": "Deploy a container to the Synology", "version": 2},
              {"name": "mine", "description": "pool skill with the user's name", "version": 1}]
    r = SS.write(skills, [str(claude), str(agents)])
    ptr = (claude / "deploy-nas" / "SKILL.md").read_text()
    assert ptr.startswith('---\nname: deploy-nas\ndescription: "Deploy a container') and 'skill_get(name="deploy-nas")' in ptr
    assert (agents / "deploy-nas" / "SKILL.md").exists()
    assert (claude / "mine" / "SKILL.md").read_text() == "my own skill" and str(claude / "mine") in r["left_alone"]
    assert SS.write(skills, [str(claude)])["wrote"] == []                       # unchanged versions: nothing to do
    skills[0]["version"] = 3
    assert SS.write(skills, [str(claude)])["wrote"] == [str(claude / "deploy-nas")]
    SS.write([skills[1]], [str(claude), str(agents)])                            # retired from the pool
    assert not (claude / "deploy-nas").exists() and (claude / "mine").exists()


def test_session_start_delivers_the_skill_index_and_start_work_suggests_skills(store):
    from commons_memory.capture import Capture
    from commons_memory.skillpool import SkillPool
    from commons_memory import work as W
    sp = SkillPool(store)
    sp.publish("deploy-nas", "---\nname: deploy-nas\ndescription: Deploy a container to the Synology NAS\n---\nSteps...",
               agent_id=store.ensure_agent("author"))
    v = store.q("SELECT id FROM skills WHERE name='deploy-nas'")[0]["id"]
    store.x("UPDATE skills SET active_version=1 WHERE id=?", (v,))
    a = store.ensure_agent("dev")
    res = Capture(store).event(a, "session_start", {"session_id": "s"}, harness="claude-code")
    assert any(sk["name"] == "deploy-nas" for sk in res["skills"])
    r = W.start(store, a, "Deploy the new container to the Synology NAS", project="home")
    assert any(sk["name"] == "deploy-nas" for sk in r["skills"])


def test_a_changed_description_rewrites_the_pointer_and_hash_descriptions_are_quoted(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    SS.write([{"name": "x", "description": "# heading", "version": 1}], [str(d)])
    assert 'description: "# heading"' in (d / "x" / "SKILL.md").read_text()      # quoted: not a YAML comment
    SS.write([{"name": "x", "description": "Bind services to the tailnet IP", "version": 1}], [str(d)])
    assert "Bind services to the tailnet IP" in (d / "x" / "SKILL.md").read_text()  # same version, new text


def test_any_agent_uses_a_pool_skill_directly_and_asks_for_help_when_it_cant(store):
    import asyncio
    import json
    from commons_memory.server import build_server
    from commons_memory.skillpool import SkillPool
    from commons_memory.orchestrator import Orchestrator
    author, user, helper = store.ensure_agent("author2"), store.ensure_agent("chat-agent"), store.ensure_agent("shell-agent")
    SkillPool(store).publish("restart-plex", "---\nname: restart-plex\ndescription: Restart Plex safely\n---\n"
                             "1. ssh pve\n2. docker restart plex", agent_id=author)
    store.x("UPDATE skills SET active_version=1 WHERE name='restart-plex'")
    SkillPool(store).rate("restart-plex", True, agent_id=helper)                  # shell-agent has run it successfully
    Orchestrator(store).profiles.set(helper, expertise={"general": 0.9}, max_concurrent=3)
    mcp = build_server(store)
    call = lambda n, **a: asyncio.run(mcp._tool_manager.get_tool(n).fn(**a))  # noqa: E731
    got = json.loads(call("skill", name="restart-plex", agent="chat-agent"))
    assert "docker restart plex" in got["content"] and "skill_help" in got["how"]   # usable with nothing installed
    assert "restart-plex" in call("skill", query="plex", agent="chat-agent")
    h = json.loads(call("skill_help", name="restart-plex", task="Plex is frozen, restart it",
                        why="I have no shell", agent="chat-agent"))
    assert h["helper"] in ("shell-agent", "author2") and h["candidates"][0] == "shell-agent"
    body = store.q("SELECT body FROM subtasks WHERE id=?", (h["subtask_id"],))[0]["body"]
    assert "I have no shell" in body and "skill(name='restart-plex')" in body
    Orchestrator(store).profiles.set(helper, skills=["local-only-tool"])
    nip = json.loads(call("skill", name="local-only-tool", agent="chat-agent"))
    assert nip["local_to"] == ["shell-agent"]                                      # not in the pool, but someone has it
    prompt = asyncio.run(mcp.get_prompt("skill", {"name": "restart-plex"}))
    assert "docker restart plex" in json.dumps(prompt.model_dump())
