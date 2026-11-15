"""Tests for taskbox.py: store, permissions, MCP protocol, doorbell, adapter hook."""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import taskbox
from taskbox import Agent, ServerConfig, Store, TaskError, Tools, build_app

HERMES_TOKEN = "h" * 40
GROK_TOKEN = "g" * 40
OTHER_TOKEN = "o" * 40


def make_cfg(webhook_url: str = "", **extra) -> ServerConfig:
    agents = {
        "hermes": Agent(name="hermes", token=HERMES_TOKEN, admin=True, external=False),
        "grokbot": Agent(name="grokbot", token=GROK_TOKEN, external=True,
                         webhook_url=webhook_url, webhook_key="wk_test"),
        "other": Agent(name="other", token=OTHER_TOKEN, external=True),
    }
    return ServerConfig(agents=agents, **extra)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db", clock=lambda: 1_700_000_000)
    yield s
    s.close()


# --- store ----------------------------------------------------------------

def test_create_and_history(store) -> None:
    t = store.create(actor="hermes", title="Book dentist", body="next week")
    assert t["id"].startswith("t_") and t["status"] == "open" and t["source"] == "hermes"
    hist = store.history(t["id"])
    assert [h["kind"] for h in hist] == ["created"]


def test_origin_ref_is_idempotent(store) -> None:
    a = store.create(actor="pebble", source="pebble", title="x y z", origin_ref="d-1")
    b = store.create(actor="pebble", source="pebble", title="x y z", origin_ref="d-1")
    assert a["id"] == b["id"]
    assert len(store.list()) == 1


def test_claim_is_exclusive(store) -> None:
    t = store.create(actor="hermes", title="t", assignee="grokbot")
    with pytest.raises(TaskError):
        store.claim(t["id"], actor="other")  # assigned to someone else
    assert store.claim(t["id"], actor="grokbot")["claimed_by"] == "grokbot"
    with pytest.raises(TaskError):
        store.claim(t["id"], actor="grokbot")  # already claimed


def test_limits_and_bad_ids(store) -> None:
    with pytest.raises(TaskError):
        store.create(actor="hermes", title="x" * 201)
    with pytest.raises(TaskError):
        store.create(actor="hermes", title="   ")
    with pytest.raises(TaskError):
        store.get("'; drop table tasks; --")
    with pytest.raises(TaskError):
        store.list(status="weird")


def test_update_rules(store) -> None:
    t = store.create(actor="hermes", title="t", assignee="grokbot")
    with pytest.raises(TaskError):
        store.update(t["id"], actor="other", is_admin=False, note="hi")
    with pytest.raises(TaskError):
        store.update(t["id"], actor="grokbot", is_admin=False, status="open")
    done = store.update(t["id"], actor="grokbot", is_admin=False, status="done", result="42")
    assert done["status"] == "done" and done["result"] == "42"
    kinds = [h["kind"] for h in store.history(t["id"])]
    assert kinds[-2:] == ["done", "result"]


# --- tool permissions -------------------------------------------------------

async def test_external_agent_sees_only_its_own_non_sensitive(store) -> None:
    cfg = make_cfg()
    tools = Tools(store, cfg)
    h, g = cfg.agents["hermes"], cfg.agents["grokbot"]
    mine = await tools.call(h, "create_task", {"title": "for grok", "assignee": "grokbot"})
    await tools.call(h, "create_task", {"title": "private", "sensitive": True})
    await tools.call(h, "create_task", {"title": "unassigned"})
    listed = await tools.call(g, "list_tasks", {"assignee": "hermes"})  # assignee filter is forced
    assert [t["id"] for t in listed["tasks"]] == [mine["id"]]
    assert len((await tools.call(h, "list_tasks", {}))["tasks"]) == 3
    secret = (await tools.call(h, "list_tasks", {"status": "open"}))["tasks"]
    hidden = [t for t in secret if t["sensitive"]][0]
    with pytest.raises(TaskError):
        await tools.call(g, "get_task", {"task_id": hidden["id"]})
    with pytest.raises(TaskError):
        await tools.call(g, "claim_task", {"task_id": hidden["id"]})
    with pytest.raises(TaskError):
        await tools.call(g, "update_task", {"task_id": hidden["id"], "note": "x"})


async def test_sensitive_never_goes_external(store) -> None:
    cfg = make_cfg()
    tools = Tools(store, cfg)
    h = cfg.agents["hermes"]
    with pytest.raises(TaskError):
        await tools.call(h, "create_task", {"title": "pw", "sensitive": True, "assignee": "grokbot"})
    t = await tools.call(h, "create_task", {"title": "pw", "sensitive": True})
    with pytest.raises(TaskError):
        await tools.call(h, "assign_task", {"task_id": t["id"], "assignee": "grokbot"})


async def test_external_cannot_assign_or_spoof(store) -> None:
    cfg = make_cfg()
    tools = Tools(store, cfg)
    g = cfg.agents["grokbot"]
    t = await tools.call(g, "create_task", {"title": "x", "assignee": "hermes", "sensitive": True})
    assert t["assignee"] == "grokbot" and t["sensitive"] is False and t["created_by"] == "grokbot"
    with pytest.raises(TaskError):
        await tools.call(g, "assign_task", {"task_id": t["id"], "assignee": "other"})
    with pytest.raises(TaskError):
        await tools.call(cfg.agents["hermes"], "assign_task", {"task_id": t["id"], "assignee": "nobody"})


# --- doorbell ---------------------------------------------------------------

async def _fake_bot():
    seen: list[tuple[dict, str]] = []

    async def hook(request: web.Request) -> web.Response:
        seen.append((await request.json(), request.headers.get("Authorization", "")))
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/hook", hook)
    srv = TestServer(app)
    await srv.start_server()
    return srv, seen


async def test_assign_rings_doorbell(tmp_path) -> None:
    bot, seen = await _fake_bot()
    try:
        cfg = make_cfg(webhook_url=str(bot.make_url("/hook")))
        client = TestClient(TestServer(build_app(cfg, Store(tmp_path / "d.db"))))
        await client.start_server()
        try:
            hdr = {"Authorization": f"Bearer {HERMES_TOKEN}"}
            r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "create_task", "arguments": {"title": "Research flights", "assignee": "grokbot"}}}, headers=hdr)
            out = json.loads((await r.json())["result"]["content"][0]["text"])
            assert out["doorbell"] == "http-200"
            assert len(seen) == 1
            body, auth = seen[0]
            assert body["task_id"] == out["id"] and body["title"] == "Research flights"
            assert auth == "Bearer wk_test"
        finally:
            await client.close()
    finally:
        await bot.close()


async def test_dead_doorbell_does_not_fail_task(store) -> None:
    cfg = make_cfg(webhook_url="http://127.0.0.1:9/nothing")
    import aiohttp

    async with aiohttp.ClientSession() as session:
        tools = Tools(store, cfg, session_getter=lambda: session)
        t = await tools.call(cfg.agents["hermes"], "create_task", {"title": "x", "assignee": "grokbot"})
    assert t["doorbell"].startswith("error-")
    assert store.get(t["id"])["status"] == "open"


# --- MCP over HTTP ------------------------------------------------------------

async def _client(tmp_path, **cfg_extra):
    client = TestClient(TestServer(build_app(make_cfg(**cfg_extra), Store(tmp_path / "m.db"))))
    await client.start_server()
    return client


async def test_mcp_handshake_and_tools(tmp_path) -> None:
    client = await _client(tmp_path)
    try:
        hdr = {"Authorization": f"Bearer {HERMES_TOKEN}", "Accept": "application/json, text/event-stream"}
        r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                       "clientInfo": {"name": "t", "version": "1"}}}, headers=hdr)
        init = await r.json()
        assert r.status == 200 and r.content_type == "application/json"
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert "tools" in init["result"]["capabilities"]
        r = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=hdr)
        assert r.status == 202
        r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=hdr)
        names = {t["name"] for t in (await r.json())["result"]["tools"]}
        assert {"create_task", "list_tasks", "claim_task", "update_task", "assign_task"} <= names
        r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                            "params": {"name": "get_task", "arguments": {"task_id": "t_0000000000"}}},
                              headers=hdr)
        res = (await r.json())["result"]
        assert res["isError"] is True and "no task" in res["content"][0]["text"]
        r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "nope"}, headers=hdr)
        assert (await r.json())["error"]["code"] == -32601
        r = await client.get("/mcp", headers=hdr)
        assert r.status == 405
    finally:
        await client.close()


async def test_mcp_auth_and_origin(tmp_path) -> None:
    client = await _client(tmp_path)
    try:
        msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert (await client.post("/mcp", json=msg)).status == 401
        assert (await client.get("/mcp")).status == 401
        assert (await client.post("/mcp", json=msg, headers={"Authorization": "Bearer wrong"})).status == 401
        r = await client.post("/mcp", json=msg, headers={"Authorization": f"Bearer {GROK_TOKEN}",
                                                         "Origin": "https://evil.example"})
        assert r.status == 403
        r = await client.post("/mcp", data=b"{not json", headers={"Authorization": f"Bearer {GROK_TOKEN}"})
        assert r.status == 400
    finally:
        await client.close()


def test_config_skips_agents_without_tokens(tmp_path) -> None:
    (tmp_path / "config.yaml").write_text(
        "taskbox:\n  port: 8646\n  agents:\n    hermes: {admin: true}\n    grokbot: {webhook_url: 'https://x/y'}\n"
    )
    (tmp_path / "s.env").write_text(f"TASKBOX_TOKEN_HERMES={HERMES_TOKEN}\nTASKBOX_TOKEN_GROKBOT=short\n")
    cfg = taskbox.load_server_config(tmp_path / "config.yaml", tmp_path / "s.env")
    assert list(cfg.agents) == ["hermes"] and cfg.agents["hermes"].admin
    assert cfg.agents["hermes"].external is False


# --- adapter hook -------------------------------------------------------------

async def test_ring_press_recorded_and_task_id_forwarded(tmp_path) -> None:
    from tests.test_adapter import FakeForwarder, make_client, make_config, valid_body

    db = tmp_path / "press.db"
    fwd = FakeForwarder(status=202)
    client = await make_client(make_config(taskbox_db=str(db)), fwd, clock=lambda: 1700000300.0)
    try:
        body, headers = valid_body()
        assert (await client.post("/x", data=body, headers=headers)).status == 202
        assert (await client.post("/x", data=body, headers=headers)).status == 202  # app retry
    finally:
        await client.close()
    ids = {c[0]["task_id"] for c in fwd.calls}
    assert len(ids) == 1
    s = Store(db)
    tasks = s.list()
    assert len(tasks) == 1 and tasks[0]["source"] == "pebble"
    assert tasks[0]["title"] == "remind me to water plants" and tasks[0]["origin_ref"] == "fileId-42"
    s.close()


async def test_broken_taskbox_never_blocks_press(tmp_path) -> None:
    from tests.test_adapter import FakeForwarder, make_client, make_config, valid_body

    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    fwd = FakeForwarder(status=202)
    client = await make_client(make_config(taskbox_db=str(blocker / "x.db")), fwd, clock=lambda: 1700000300.0)
    try:
        body, headers = valid_body()
        assert (await client.post("/x", data=body, headers=headers)).status == 202
    finally:
        await client.close()
    assert "task_id" not in fwd.calls[0][0]


async def test_external_finish_posts_to_whatsapp(store, monkeypatch) -> None:
    import whatsapp_ack

    sent = []

    async def fake_send(session, chat_id, text, bridge_port=3000, timeout=5.0):
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(whatsapp_ack, "send_ack", fake_send)
    cfg = make_cfg(notify_chat_id="123@g.us")
    tools = Tools(store, cfg)
    h, g = cfg.agents["hermes"], cfg.agents["grokbot"]
    t = await tools.call(h, "create_task", {"title": "Find a dentist", "assignee": "grokbot"})
    await tools.call(g, "claim_task", {"task_id": t["id"]})
    await tools.call(g, "update_task", {"task_id": t["id"], "note": "looking"})
    assert sent == []  # notes alone stay quiet
    await tools.call(g, "update_task", {"task_id": t["id"], "status": "done", "result": "Dr. A, Tue 3pm"})
    assert len(sent) == 1 and sent[0][0] == "123@g.us"
    assert "grokbot" in sent[0][1] and t["id"] in sent[0][1] and "Dr. A, Tue 3pm" in sent[0][1]
    t2 = await tools.call(h, "create_task", {"title": "mine"})
    await tools.call(h, "update_task", {"task_id": t2["id"], "status": "done", "result": "x"})
    assert len(sent) == 1  # Hermes answers in chat itself; no duplicate notice
