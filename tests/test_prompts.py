import asyncio
import json


def test_commons_offers_prompts_and_resources(store):
    from commons_memory.server import INSTRUCTIONS, build_server
    from commons_memory import board as B
    mcp = build_server(store)
    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert {"checkin", "start", "handover", "wrapup", "whats_new", "remember"} <= names
    msg = asyncio.run(mcp.get_prompt("start", {"task": "Ship the pricing page"}))
    assert "start_work" in json.dumps(msg.model_dump()) and "Ship the pricing page" in json.dumps(msg.model_dump())
    res = asyncio.run(mcp.read_resource("commons-hub://playbook"))
    assert INSTRUCTIONS in list(res)[0].content
    B.add(store, "ask", "Make the hub lighter", project="hubproj")
    board = asyncio.run(mcp.read_resource("commons-hub://board/hubproj"))
    assert "Make the hub lighter" in list(board)[0].content
    templates = {t.uriTemplate for t in asyncio.run(mcp.list_resource_templates())}
    assert "commons-hub://memory/{item_id}" in templates
