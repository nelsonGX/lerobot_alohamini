"""Kiosk chat tests: fake Claude stream, fake GPU agent and cameras; no keys or network needed."""

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

SERVER = Path(__file__).resolve().parents[2] / "lerobot-panel" / "server"
sys.path.insert(0, str(SERVER))
kiosk = importlib.import_module("kiosk")


# ---------- fakes ----------


def text_events(text):
    return [
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=c))
        for c in text
    ]


def message(content, stop_reason):
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    return SimpleNamespace(content=content, stop_reason=stop_reason, model="fake", usage=usage)


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(name, input_, id_="tu_1"):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


class FakeStream:
    def __init__(self, events, final, delay=0.0):
        self.events, self.final, self.delay = events, final, delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event in self.events:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield event

    async def get_final_message(self):
        return self.final


class FakeClaude:
    """Replays scripted responses; records each request's messages."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.script.pop(0)


class Client:
    def __init__(self):
        self.events = []

    async def send_json(self, event):
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(kiosk, "CONFIG_FILE", tmp_path / "kiosk.json")
    monkeypatch.setattr(kiosk, "DATA_DIR", tmp_path)
    monkeypatch.setattr(kiosk, "PICK_POLL_S", 0.01)
    monkeypatch.setattr(kiosk, "SIM_PICK_S", 0)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(kiosk, "_hooks", {})
    session = kiosk.Session()
    client = Client()
    session.clients.add(client)
    return session, client


async def settle(session):
    for _ in range(200):
        await asyncio.sleep(0.01)
        busy = (session.turn_task and not session.turn_task.done()) or (
            session.pick_task and not session.pick_task.done()
        )
        if not busy:
            return
    pytest.fail("session did not settle")


def assert_valid_history(messages):
    """Every assistant tool_use must be answered by tool_results in the next message."""
    for i, msg in enumerate(messages):
        if msg["role"] == "assistant" and isinstance(msg["content"], list):
            ids = {b.id for b in msg["content"] if getattr(b, "type", None) == "tool_use"}
            if ids:
                nxt = messages[i + 1]
                assert nxt["role"] == "user"
                assert {r["tool_use_id"] for r in nxt["content"]} == ids


# ---------- speech text ----------


def test_splitter_chinese_sentences_and_faces():
    s = kiosk.SentenceSplitter()
    out, faces = [], []
    for chunk in ["{hap", "py}我推薦提拉米蘇。", "這是今天最後一個喔！要幫", "你拿嗎？{sad} OK. done"]:
        o, f = s.feed(chunk)
        out += o
        faces += f
    o, f = s.flush()
    out += o
    faces += f
    assert out == ["我推薦提拉米蘇。", "這是今天最後一個喔！", "要幫你拿嗎？", "OK.", "done"]
    assert faces == ["happy", "sad"]


def test_splitter_flushes_long_first_clause_at_comma():
    s = kiosk.SentenceSplitter()
    out, _ = s.feed("好的，我們今天有草莓蛋糕和提拉米蘇兩種蛋糕可以選，")
    assert out and out[0].endswith("，")


def test_validate_input():
    assert kiosk.validate_input("pick_item", {"item_id": "tiramisu"}) is None
    assert kiosk.validate_input("pick_item", {}) is not None
    assert kiosk.validate_input("look_at_table", {"camera": "side"}) is not None
    assert kiosk.validate_input("notify_owner", {"message": "x", "urgent": "yes"}) is not None
    assert kiosk.validate_input("nope", {}) is not None


# ---------- turns ----------


def test_turn_with_tool_then_speech(env):
    session, client = env
    session._client = FakeClaude([
        FakeStream([], message([tool_block("check_inventory", {})], "tool_use")),
        FakeStream(text_events("{happy}我們有草莓蛋糕和提拉米蘇。"), message([text_block("…")], "end_turn")),
    ])  # fmt: skip

    async def run():
        await session.user_said("這裡有什麼？")
        await settle(session)

    asyncio.run(run())
    assert [e["text"] for e in client.of("speech")] == ["我們有草莓蛋糕和提拉米蘇。"]
    assert client.of("speech")[0]["audio"] is None  # no ElevenLabs key: the iPad uses its own voice
    assert any(e["face"] == "happy" for e in client.of("face"))
    tool = client.of("tool")[0]
    assert tool["name"] == "check_inventory" and not tool["is_error"] and "草莓蛋糕" in tool["result"]
    assert "NOT armed" in tool["result"]
    assert_valid_history(session.messages)
    second = session._client.requests[1]
    assert second["output_config"] == {"effort": "low"} and second["fallbacks"] == "default"
    assert client.of("turn_done")


def test_interrupt_keeps_history_valid(env):
    session, client = env
    session._client = FakeClaude([
        FakeStream(text_events("這是一段很長很長的回答，會被打斷。還有更多。"), message([text_block("x")], "end_turn"), delay=0.02),
        FakeStream(text_events("好的。"), message([text_block("好的。")], "end_turn")),
    ])  # fmt: skip

    async def run():
        await session.user_said("推薦哪個？")
        await asyncio.sleep(0.15)
        await session.user_said("算了，草莓好了")
        await settle(session)

    asyncio.run(run())
    assert client.of("interrupted")
    roles = [m["role"] for m in session.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert "cut off" in session.messages[1]["content"]


def test_look_at_table_returns_images(env, monkeypatch):
    session, _ = env

    class Feed:
        def touch(self, *a):
            pass

        def status(self):
            return {
                "cameras": [{"name": "chest", "live": True}, {"name": "overhead", "live": True}],
                "error": None,
            }

        def frame(self, name):
            return (b"\xff\xd8jpeg-" + name.encode(), 1)

    monkeypatch.setattr(kiosk, "load_settings", lambda: SimpleNamespace(jetson_ip="1.2.3.4"))
    kiosk.configure(camera_feed=Feed(), camera_port=5557, start_job=None, gpu_call=None)
    content = asyncio.run(session.tool_look_at_table("overhead"))
    assert [c["type"] for c in content] == ["text", "image"]
    assert content[0]["text"].startswith("overhead")


# ---------- picks ----------


def test_pick_refused_until_armed(env):
    session, _ = env
    with pytest.raises(kiosk.ToolError, match="not armed"):
        asyncio.run(session.tool_pick_item("strawberry_cake"))


def test_simulated_pick_updates_inventory_and_reacts(env):
    session, client = env
    kiosk.store.update(lambda c: setattr(c, "armed", True) or setattr(c, "simulate_picks", True))
    session._client = FakeClaude([
        FakeStream([], message([tool_block("pick_item", {"item_id": "tiramisu"})], "tool_use")),
        FakeStream(text_events("好，我幫你拿。"), message([text_block("好，我幫你拿。")], "end_turn")),
        FakeStream(text_events("{happy}你的提拉米蘇好了！"), message([text_block("…")], "end_turn")),
    ])  # fmt: skip

    async def run():
        await session.user_said("我要提拉米蘇")
        await settle(session)
        await settle(session)

    asyncio.run(run())
    item = next(i for i in kiosk.store.load().items if i.id == "tiramisu")
    assert item.sold == 1 and not item.on_table
    assert client.of("robot_event") and "finished" in client.of("robot_event")[0]["text"]
    assert any("sold out" in n["message"] for n in client.of("notification"))  # tiramisu has no back stock
    assert [e["text"] for e in client.of("speech")][-1] == "你的提拉米蘇好了！"
    assert_valid_history(session.messages)
    # Not on the table any more: a second pick is refused by the inventory.
    with pytest.raises(kiosk.ToolError, match="no 提拉米蘇 is on the table"):
        asyncio.run(session.tool_pick_item("tiramisu"))


def test_real_pick_starts_gpu_job_and_follows_it(env):
    session, client = env
    kiosk.store.update(
        lambda c: setattr(c, "armed", True) or setattr(c.items[0], "checkpoint", "outputs/straw/last")
    )
    started, polls = [], []

    def start_job(req):
        started.append(req)
        return {"id": "a" * 32}

    def gpu_call(method, path, body=None):
        polls.append((method, path))
        if len(polls) < 3:
            return {"state": "running", "lines": ['[EVAL:event] {"event": "episode_start", "at": 1.0}']}
        return {"state": "succeeded", "lines": []}

    kiosk.configure(camera_feed=None, camera_port=5557, start_job=start_job, gpu_call=gpu_call)
    session._client = FakeClaude(
        [FakeStream(text_events("好了。"), message([text_block("好了。")], "end_turn"))]
    )

    async def run():
        result = await session.tool_pick_item("strawberry_cake")
        assert "Started" in result
        await settle(session)
        await settle(session)

    asyncio.run(run())
    req = started[0]
    assert (req.kind, req.episodes, req.record, req.checkpoint) == (
        "robot_eval",
        1,
        False,
        "outputs/straw/last",
    )
    assert session.pick.state == "succeeded" and session.pick.episode_started_at == 1.0
    item = kiosk.store.load().items[0]
    assert item.sold == 1 and item.back_stock == 3
    assert any("Restock" in n["message"] for n in client.of("notification"))


def test_pick_start_failure_is_reported_to_claude(env):
    session, _ = env
    kiosk.store.update(lambda c: setattr(c, "armed", True) or setattr(c.items[0], "checkpoint", "x"))

    def start_job(req):
        raise HTTPException(409, "Start the robot host on the Robot page before live evaluation.")

    kiosk.configure(camera_feed=None, camera_port=5557, start_job=start_job, gpu_call=None)
    with pytest.raises(kiosk.ToolError, match="robot host"):
        asyncio.run(session.tool_pick_item("strawberry_cake"))
    assert session.pick is None
