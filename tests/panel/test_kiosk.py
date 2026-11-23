"""Kiosk chat tests: fake Claude stream, fake GPU agent and cameras; no keys or network needed."""

import asyncio
import importlib
import json
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


class FakeGPU:
    """A GPU agent: jobs, their [EVAL:event] lines, and the commands sent to them."""

    def __init__(self, standby_supported=True):
        self.standby_supported = standby_supported
        self.jobs = {}
        self.commands = []

    def line(self, job, event, **fields):
        job["at"] += 1
        job["lines"].append("[EVAL:event] " + json.dumps({"event": event, "at": job["at"], **fields}))

    def start_job(self, req):
        if req.standby_policies and not self.standby_supported:
            raise HTTPException(422, [{"msg": "Extra inputs are not permitted"}])
        if any(j["state"] == "running" for j in self.jobs.values()):
            raise HTTPException(409, "A GPU job is already running.")
        job_id = f"{len(self.jobs) + 1:032x}"
        self.jobs[job_id] = {
            "id": job_id,
            "name": req.name,
            "kind": req.kind,
            "request": req.model_dump(),
            "state": "running",
            "lines": [],
            "started_at": 100.0,
            "at": 100.0,
            "polls": 0,
        }
        return {"id": job_id}

    def call(self, method, path, body=None):
        if path == "/status":
            return {"jobs": [{k: j[k] for k in ("id", "name", "kind", "state")} for j in self.jobs.values()]}
        job = self.jobs[path.split("/")[2]]
        if path.endswith("/stop"):
            job["state"] = "stopped"
            return {"ok": True}
        if path.endswith("/command"):
            self.commands.append((job["id"], body))
            if body["command"] == "start":
                job["pending"] = body["policy"]
            elif body["command"] == "end" and job.get("running"):
                job["running"] = None
                self.line(job, "episode_end", episode=1)
                self.line(job, "waiting", episode=2)
            return {"ok": True}
        job["polls"] += 1
        standby = bool(job["request"]["standby_policies"])
        if standby and job["polls"] == 2:  # loaded and connected
            self.line(job, "waiting", episode=1)
        if job.get("pending") and job["polls"] >= 2:
            job["running"], job["pending"] = job["pending"], None
            self.line(job, "episode_start", episode=1, policy=job["running"])
        elif job.get("running") and job["polls"] % 3 == 0:  # runs a few polls, then hits its time limit
            job["running"] = None
            self.line(job, "episode_end", episode=1)
            self.line(job, "waiting", episode=2)
        if not standby:
            if job["polls"] == 2:
                self.line(job, "episode_start", episode=1)
            if job["polls"] >= 4:
                job["state"] = "succeeded"
        return {**job, "request": job["request"]}


def arm_with_checkpoints(armed=True):
    def change(c):
        c.armed = armed
        c.items[0].checkpoint = "outputs/straw/last"
        c.items[1].checkpoint = "outputs/tira/last"

    return kiosk.store.update(change)


def test_arming_warms_up_and_picks_use_the_loaded_policy(env):
    session, client = env
    gpu = FakeGPU()
    kiosk.configure(camera_feed=None, camera_port=5557, start_job=gpu.start_job, gpu_call=gpu.call)
    session._client = FakeClaude(
        [FakeStream(text_events("好了。"), message([text_block("好了。")], "end_turn"))]
    )

    async def run():
        await session.config_changed(arm_with_checkpoints())
        for _ in range(100):
            if session.standby.state == "ready":
                break
            await asyncio.sleep(0.01)
        assert session.standby.state == "ready"
        result = await session.tool_pick_item("tiramisu")
        assert "Started" in result
        await settle(session)
        for _ in range(100):
            if (
                session.pick_task
                and session.pick_task.done()
                and session.turn_task
                and session.turn_task.done()
            ):
                break
            await asyncio.sleep(0.01)

    asyncio.run(run())
    (job,) = gpu.jobs.values()  # one warm job served the pick
    assert job["name"] == "kiosk-standby" and job["state"] == "running"
    assert [p["name"] for p in job["request"]["standby_policies"]] == ["strawberry_cake", "tiramisu"]
    assert gpu.commands == [(job["id"], {"command": "start", "policy": "tiramisu"})]
    assert session.pick.mode == "standby" and session.pick.state == "succeeded"
    assert next(i for i in kiosk.store.load().items if i.id == "tiramisu").sold == 1
    assert session.standby.state == "ready"  # still warm for the next customer
    assert client.of("robot_event")
    assert any(e["standby"]["state"] == "loading" for e in client.of("standby"))


def test_disarm_and_stop_unload_the_warm_job(env):
    session, client = env
    gpu = FakeGPU()
    kiosk.configure(camera_feed=None, camera_port=5557, start_job=gpu.start_job, gpu_call=gpu.call)

    async def run():
        await session.config_changed(arm_with_checkpoints())
        (job,) = gpu.jobs.values()
        await session.config_changed(arm_with_checkpoints(armed=False))
        assert job["state"] == "stopped"
        await asyncio.sleep(0.05)
        assert session.standby.state == "off"
        # Re-arm, start a pick, then STOP: job stopped, pick stopped, arm disarmed.
        await session.config_changed(arm_with_checkpoints())
        await session.tool_pick_item("strawberry_cake")
        await session.stop_pick()
        await settle(session)
        await asyncio.sleep(0.05)

    session._client = FakeClaude(
        [FakeStream(text_events("停了。"), message([text_block("停了。")], "end_turn"))]
    )
    asyncio.run(run())
    assert all(j["state"] == "stopped" for j in gpu.jobs.values())
    assert not kiosk.store.load().armed
    assert session.pick.state == "stopped"
    assert session.standby.state == "off"
    assert any("disarmed" in n["message"] for n in client.of("notification"))


def test_old_gpu_agent_falls_back_to_one_job_per_pick(env):
    session, _ = env
    gpu = FakeGPU(standby_supported=False)
    kiosk.configure(camera_feed=None, camera_port=5557, start_job=gpu.start_job, gpu_call=gpu.call)
    session._client = FakeClaude(
        [FakeStream(text_events("好了。"), message([text_block("好了。")], "end_turn"))]
    )

    async def run():
        await session.config_changed(arm_with_checkpoints())
        assert session.standby.state == "error" and "warm up" in session.standby.detail
        await session.tool_pick_item("strawberry_cake")
        await settle(session)
        await settle(session)

    asyncio.run(run())
    (job,) = gpu.jobs.values()
    req = job["request"]
    assert (req["episodes"], req["record"], req["checkpoint"]) == (1, False, "outputs/straw/last")
    assert session.pick.mode == "job" and session.pick.state == "succeeded"
    item = kiosk.store.load().items[0]
    assert item.sold == 1 and item.back_stock == 3


def test_panel_restart_adopts_the_running_warm_job(env):
    session, _ = env
    gpu = FakeGPU()
    kiosk.configure(camera_feed=None, camera_port=5557, start_job=gpu.start_job, gpu_call=gpu.call)
    cfg = arm_with_checkpoints()
    gpu.start_job(kiosk.standby_request(kiosk.standby_policies(cfg)))  # left over from before the restart
    asyncio.run(session.ensure_standby(cfg))
    assert len(gpu.jobs) == 1 and session.standby.job_id == next(iter(gpu.jobs))


def test_pick_start_failure_is_reported_to_claude(env):
    session, _ = env
    arm_with_checkpoints()

    def start_job(req):
        raise HTTPException(409, "Start the robot host on the Robot page before live evaluation.")

    kiosk.configure(camera_feed=None, camera_port=5557, start_job=start_job, gpu_call=FakeGPU().call)
    with pytest.raises(kiosk.ToolError, match="robot host"):
        asyncio.run(session.tool_pick_item("strawberry_cake"))
    assert session.pick is None
    assert "robot host" in session.standby.detail
