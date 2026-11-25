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

    def __init__(self, script, verdicts=()):
        self.script = list(script)
        self.requests = []
        self.verdicts = list(verdicts)  # addressee check answers ("yes"/"no", or an exception)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kwargs):
        await asyncio.sleep(0.05)  # slower than the reply's first tokens: speech must wait for it
        verdict = self.verdicts.pop(0) if self.verdicts else "yes"
        if isinstance(verdict, Exception):
            raise verdict
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=verdict)])

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


def test_clear_context_forgets_after_the_goodbye(env):
    session, client = env
    session._client = FakeClaude([
        FakeStream([], message([tool_block("clear_context", {})], "tool_use")),
        FakeStream(text_events("{happy}謝謝光臨，掰掰！"), message([text_block("謝謝光臨，掰掰！")], "end_turn")),
        FakeStream(text_events("歡迎光臨！"), message([text_block("歡迎光臨！")], "end_turn")),
    ])  # fmt: skip

    async def run():
        await session.user_said("謝謝，掰掰")
        await settle(session)
        assert session.messages == []
        await session.user_said("你好")
        await settle(session)

    asyncio.run(run())
    assert [e["text"] for e in client.of("speech")] == ["謝謝光臨，掰掰！", "歡迎光臨！"]
    reset = client.of("reset")[0]
    assert reset["keep_audio"] and not client.of("interrupted")  # the goodbye keeps playing
    assert session._client.requests[2]["messages"] == [{"role": "user", "content": "你好"}]


def test_clear_context_waits_for_a_running_pick(env):
    session, _ = env
    session.pick = kiosk.Pick(
        job_id="j", item_id="tiramisu", item_name="提拉米蘇", started_at=0, time_limit_s=40
    )
    with pytest.raises(kiosk.ToolError):
        asyncio.run(session.tool_clear_context())
    assert not session.clear_after_turn


def test_side_talk_is_dropped_before_it_says_or_does_anything(env):
    session, client = env
    session._client = FakeClaude(
        [FakeStream([], message([tool_block("check_inventory", {})], "tool_use"))], verdicts=["No"]
    )

    async def run():
        await session.user_said("欸你今天晚上要去吃什麼", {}, mic=True)
        await settle(session)

    asyncio.run(run())
    assert session.messages == []
    assert not client.of("tool") and not client.of("speech") and not client.of("user")
    assert client.of("ignored") and client.of("addressee")[0]["to_robot"] is False


def test_customer_speech_passes_the_check_and_is_answered(env):
    session, client = env
    session._client = FakeClaude(
        [FakeStream(text_events("好的。"), message([text_block("好的。")], "end_turn"))], verdicts=["yes"]
    )

    async def run():
        await session.user_said("提拉米蘇好了", {}, mic=True)
        await settle(session)

    asyncio.run(run())
    assert [e["text"] for e in client.of("speech")] == ["好的。"]
    assert [e["type"] for e in client.events if e["type"] in ("addressee", "user", "speech")] == [
        "addressee",
        "user",
        "speech",
    ]


def test_side_talk_while_the_robot_talks_does_not_interrupt_it(env):
    session, client = env
    session._client = FakeClaude(
        [FakeStream(text_events("我們有草莓蛋糕和提拉米蘇，兩個都很好吃。"), message([text_block("x")], "end_turn"), delay=0.01)],
        verdicts=["no"],
    )  # fmt: skip

    async def run():
        await session.user_said("有什麼？")
        await asyncio.sleep(0.05)
        await session.user_said("那個機器人好可愛", {}, mic=True, robot_talking=True)
        await settle(session)

    asyncio.run(run())
    assert not client.of("interrupted") and client.of("ignored")
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


def test_repeating_yourself_while_the_robot_replies_does_not_interrupt_it(env):
    session, client = env
    session._client = FakeClaude(
        [
            FakeStream(
                text_events("草莓蛋糕來囉，請稍等一下。"), message([text_block("x")], "end_turn"), delay=0.01
            )
        ]
    )
    checks = []
    create = session._client.create

    async def spy(**kwargs):
        checks.append(kwargs)
        return await create(**kwargs)

    session._client.messages.create = spy

    async def run():
        await session.user_said("我要一個草莓蛋糕")
        await asyncio.sleep(0.03)  # still thinking/speaking
        await session.user_said("草莓蛋糕", {}, mic=True, robot_talking=True)
        await settle(session)

    asyncio.run(run())
    assert not client.of("interrupted") and not checks  # spotted locally, no Haiku call
    assert client.of("noted")[0]["text"] == "草莓蛋糕"
    assert [m["role"] for m in session.messages] == ["user", "assistant", "user"]
    assert session.messages[2]["content"].startswith(kiosk.HEARD_PREFIX)
    assert "草莓蛋糕" in session.messages[2]["content"]
    assert len(session._client.requests) == 1  # no second reply


def test_nothing_new_keeps_talking_but_something_new_interrupts(env):
    session, client = env

    def long():
        text = "我們有草莓蛋糕和提拉米蘇，兩個都很好吃喔。"
        return FakeStream(text_events(text), message([text_block("x")], "end_turn"), delay=0.01)

    session._client = FakeClaude([long(), long()], verdicts=["same", "new"])

    async def run():
        await session.user_said("有什麼？")
        await asyncio.sleep(0.05)
        await session.user_said("嗯嗯好", {}, mic=True, robot_talking=True)  # backchannel
        assert not client.of("interrupted")
        await session.user_said("那提拉米蘇多少錢", {}, mic=True, robot_talking=True)
        await settle(session)

    asyncio.run(run())
    assert len(client.of("interrupted")) == 1 and len(client.of("noted")) == 1
    contents = [m["content"] for m in session.messages if m["role"] == "user"]
    assert contents[0] == "有什麼？" and contents[-1] == "那提拉米蘇多少錢"
    assert any(str(c).startswith(kiosk.HEARD_PREFIX) and "嗯嗯好" in c for c in contents)


def test_is_repeat():
    assert kiosk.is_repeat("草莓蛋糕。", ["我要一個草莓蛋糕"])
    assert kiosk.is_repeat("我要一個草莓蛋糕", ["我要一個草莓蛋糕！"])
    assert kiosk.is_repeat("I want the tiramisu", ["i want tiramisu"])
    assert not kiosk.is_repeat("那提拉米蘇多少錢", ["我要一個草莓蛋糕"])
    assert not kiosk.is_repeat("草莓", [])


def test_stop_words_and_check_failures_never_block_a_customer(env):
    session, client = env
    session._client = FakeClaude(
        [
            FakeStream(text_events("好。"), message([text_block("好。")], "end_turn")),
            FakeStream(text_events("好。"), message([text_block("好。")], "end_turn")),
        ],
        verdicts=[RuntimeError("overloaded")],
    )

    async def run():
        await session.user_said("等一下", {}, mic=True)  # no check at all
        await settle(session)
        await session.user_said("我要草莓", {}, mic=True)  # check fails: answered anyway
        await settle(session)

    asyncio.run(run())
    assert len(client.of("speech")) == 2 and len(client.of("user")) == 2
    assert client.of("error")[0]["where"] == "addressee"


def test_visibly_talking_customer_skips_the_check_and_camera_hints_reach_it(env):
    session, client = env
    reply = lambda: FakeStream(text_events("好。"), message([text_block("好。")], "end_turn"))  # noqa: E731
    session._client = FakeClaude([reply(), reply()], verdicts=["yes"])
    prompts = []
    create = session._client.create

    async def spy(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return await create(**kwargs)

    session._client.messages.create = spy
    talking = kiosk.VisionSummary(present=1, facing=0.9, mouth=0.08, lips=True)
    still = kiosk.VisionSummary(present=1, facing=0.2, mouth=0.0, lips=False)

    async def run():
        await session.user_said("我要草莓", {}, mic=True, vision=talking)
        await settle(session)
        await session.user_said("好啊", {}, mic=True, vision=still)
        await settle(session)

    asyncio.run(run())
    assert len(prompts) == 1  # only the second utterance was checked
    assert "lips did not visibly move" in prompts[0] and "turned away" in prompts[0]
    assert client.of("user")[0]["vision"]["lips"] is True


def test_greets_on_arrival_and_starts_fresh_after_they_leave(env):
    session, client = env
    kiosk.store.update(lambda c: setattr(c, "leave_reset_s", 1))
    session._client = FakeClaude(
        [FakeStream(text_events("{happy}歡迎光臨！"), message([text_block("歡迎光臨！")], "end_turn"))]
    )

    async def run():
        await session.presence(True)
        await settle(session)
        await session.presence(True)  # no repeat
        await session.presence(False)
        await asyncio.sleep(1.1)

    asyncio.run(run())
    assert [e["text"] for e in client.of("speech")] == ["歡迎光臨！"]
    assert "walked up" in client.of("robot_event")[0]["text"]
    assert session.messages == [] and client.of("reset")[0]["reason"] == "customer left"


def test_coming_back_cancels_the_reset(env):
    session, client = env
    kiosk.store.update(lambda c: setattr(c, "leave_reset_s", 1) or setattr(c, "greet_on_arrival", False))
    session.messages = [{"role": "user", "content": "你好"}]

    async def run():
        await session.presence(True)
        await session.presence(False)
        await asyncio.sleep(0.3)
        await session.presence(True)
        await asyncio.sleep(1.0)

    asyncio.run(run())
    assert session.messages and not client.of("reset")


class CameraClient(Client):
    """The iPad: answers photo requests with a JPEG (or an error) like the kiosk page does."""

    def __init__(self, session, image=None, error=None):
        super().__init__()
        self.session, self.image, self.error = session, image, error

    async def send_json(self, event):
        await super().send_json(event)
        if event["type"] == "photo_request":
            self.session.photo_received(event["id"], self.image, self.error)


def test_look_at_customer_returns_the_ipads_photo(env):
    session, client = env  # client: a monitor laptop, which never answers
    session.clients.add(CameraClient(session, image="aGVsbG8="))
    content = asyncio.run(session.tool_look_at_customer())
    assert content[1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "aGVsbG8="}
    assert not session.photo_requests  # cleaned up


def test_look_at_customer_explains_why_there_is_no_photo(env, monkeypatch):
    session, _ = env
    monkeypatch.setattr(kiosk, "PHOTO_TIMEOUT_S", 0.05)
    session.clients.add(CameraClient(session, error="camera error"))
    with pytest.raises(kiosk.ToolError, match="camera error"):
        asyncio.run(session.tool_look_at_customer())
    kiosk.store.update(lambda c: setattr(c, "customer_photos", False))
    with pytest.raises(kiosk.ToolError, match="turned off"):
        asyncio.run(session.tool_look_at_customer())


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

    def __init__(self, standby_supported=True, home_ok=True, episode_polls=3):
        self.standby_supported = standby_supported
        self.home_ok = home_ok
        self.episode_polls = episode_polls  # polls an episode runs before it hits its time limit
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
            elif body["command"] == "home":
                self.line(job, "homing")
                self.line(
                    job, "homed", ok=self.home_ok, **({} if self.home_ok else {"reason": "joint protection"})
                )
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
        elif job.get("running") and job["polls"] % self.episode_polls == 0:  # runs a while, then times out
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


async def warm_up(session):
    await session.config_changed(arm_with_checkpoints())
    for _ in range(100):
        if session.standby.state == "ready":
            return
        await asyncio.sleep(0.01)
    pytest.fail("standby never got ready")


async def pick_done(session):
    for _ in range(200):
        if session.pick and not session.pick.active and session.pick_task and session.pick_task.done():
            await settle(session)
            return
        await asyncio.sleep(0.01)
    pytest.fail("pick never finished")


def test_a_missed_pick_is_retried_after_moving_the_arms_home(env):
    session, client = env
    gpu = FakeGPU()
    kiosk.configure(camera_feed=None, camera_port=5557, start_job=gpu.start_job, gpu_call=gpu.call)
    reply = lambda: FakeStream(text_events("好。"), message([text_block("好。")], "end_turn"))  # noqa: E731
    session._client = FakeClaude([reply(), reply()])
    tiramisu = lambda: next(i for i in kiosk.store.load().items if i.id == "tiramisu")  # noqa: E731

    async def run():
        await warm_up(session)
        await session.tool_pick_item("tiramisu")
        await pick_done(session)
        assert tiramisu().sold == 1 and not tiramisu().on_table  # the episode "finished": counted as sold
        assert "Not while" not in await session.tool_reset_arm()  # Claude saw it missed
        assert session.standby.state == "ready"
        with pytest.raises(kiosk.ToolError, match="no 提拉米蘇 is on the table"):
            await session.tool_pick_item("tiramisu")  # a new pick: the inventory says it was sold
        assert "Started" in await session.tool_pick_item("tiramisu", retry=True)  # undoes the sale first
        assert tiramisu().sold == 0
        await pick_done(session)

    asyncio.run(run())
    job_id = next(iter(gpu.jobs))
    assert [c for _, c in gpu.commands] == [
        {"command": "start", "policy": "tiramisu"},
        {"command": "home"},
        {"command": "start", "policy": "tiramisu"},
    ]
    assert all(j == job_id for j, _ in gpu.commands)
    assert tiramisu().sold == 1  # sold once, not twice
    assert any("Retrying" in n["message"] for n in session.notifications)


class TableFeed:
    def touch(self, *a):
        pass

    def status(self):
        return {"cameras": [{"name": "overhead", "live": True}], "error": None}

    def frame(self, name):
        return (b"\xff\xd8jpeg", 1)


def watched_pick(env, monkeypatch, verdicts):
    """A warm pick that would run for ~10 s, with the camera watch looking every second."""
    session, client = env
    gpu = FakeGPU(episode_polls=1000)
    monkeypatch.setattr(kiosk, "load_settings", lambda: SimpleNamespace(jetson_ip="1.2.3.4"))
    kiosk.configure(camera_feed=TableFeed(), camera_port=5557, start_job=gpu.start_job, gpu_call=gpu.call)
    kiosk.store.update(lambda c: setattr(c, "pick_watch_after_s", 1) or setattr(c, "pick_watch_every_s", 1))
    reply = lambda: FakeStream(text_events("好。"), message([text_block("好。")], "end_turn"))  # noqa: E731
    session._client = FakeClaude([reply()], verdicts=verdicts)

    async def run():
        await warm_up(session)
        await session.tool_pick_item("tiramisu")
        for _ in range(600):
            if session.pick_task and session.pick_task.done():
                break
            await asyncio.sleep(0.01)
        await settle(session)

    asyncio.run(run())
    return session, client, gpu


def test_camera_watch_ends_a_failing_pick_early_without_selling(env, monkeypatch):
    session, client, gpu = watched_pick(
        env, monkeypatch, ["trying: reaching for it", "failing: box tipped over", "failing: box on its side"]
    )
    assert [c["command"] for _, c in gpu.commands] == ["start", "end"]
    assert session.pick.verdict == "missed" and "box on its side" in session.pick.detail
    assert [e["verdict"] for e in client.of("pick_check")] == ["trying", "failing", "failing"]
    tiramisu = next(i for i in kiosk.store.load().items if i.id == "tiramisu")
    assert tiramisu.on_table and tiramisu.sold == 0  # a miss is not a sale
    (event,) = client.of("robot_event")
    assert "failed" in event["text"] and "reset_arm" in event["text"]
    assert session.standby.state == "ready"  # still warm: reset_arm and pick again right away


def test_camera_watch_ends_the_pick_once_it_is_in_the_box(env, monkeypatch):
    session, client, gpu = watched_pick(env, monkeypatch, ["in_box: tiramisu inside, grippers open"])
    assert [c["command"] for _, c in gpu.commands] == ["start", "end"]
    assert session.pick.verdict == "in_box" and session.pick.state == "succeeded"
    assert next(i for i in kiosk.store.load().items if i.id == "tiramisu").sold == 1
    assert "camera saw it in the box" in client.of("robot_event")[0]["text"]


def test_claude_can_end_a_missed_pick(env, monkeypatch):
    session, client = env
    kiosk.store.update(lambda c: setattr(c, "armed", True) or setattr(c, "simulate_picks", True))
    monkeypatch.setattr(kiosk, "SIM_PICK_S", 60)  # runs until Claude ends it
    session._client = FakeClaude([FakeStream(text_events("好。"), message([text_block("好。")], "end_turn"))])

    async def run():
        assert "No pick" in await session.tool_end_pick("missed")
        await session.tool_pick_item("strawberry_cake")
        assert "Ending" in await session.tool_end_pick("missed")
        await pick_done(session)

    asyncio.run(run())
    item = kiosk.store.load().items[0]
    assert item.on_table and item.sold == 0
    assert "Nothing was sold" in client.of("robot_event")[0]["text"]


def test_reset_arm_refuses_mid_pick_and_reports_a_failed_move(env):
    session, client = env
    gpu = FakeGPU(home_ok=False)
    kiosk.configure(camera_feed=None, camera_port=5557, start_job=gpu.start_job, gpu_call=gpu.call)
    session._client = FakeClaude([FakeStream(text_events("好。"), message([text_block("好。")], "end_turn"))])

    async def run():
        await warm_up(session)
        await session.tool_pick_item("strawberry_cake")
        with pytest.raises(kiosk.ToolError, match="still running"):
            await session.tool_reset_arm()
        await pick_done(session)
        with pytest.raises(kiosk.ToolError, match="joint protection"):
            await session.tool_reset_arm()

    asyncio.run(run())
    assert session.notifications[0]["urgent"] and "starting pose" in session.notifications[0]["message"]
    assert session.standby.state == "ready"


def test_picks_of_one_item_are_capped_per_conversation(env):
    session, _ = env
    kiosk.store.update(lambda c: setattr(c, "armed", True) or setattr(c, "simulate_picks", True))
    session.attempts = {"strawberry_cake": kiosk.MAX_ATTEMPTS}
    with pytest.raises(kiosk.ToolError, match="already been tried"):
        asyncio.run(session.tool_pick_item("strawberry_cake"))
    assert "simulated" in asyncio.run(session.tool_reset_arm())


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


# ---------- mock payment ----------


def test_payment_needs_a_successful_pick(env):
    session, _ = env
    with pytest.raises(kiosk.ToolError, match="nothing to pay for"):
        asyncio.run(session.tool_request_payment("strawberry_cake"))


def test_payment_after_pick_approved_and_declined(env):
    session, client = env
    session.pick = kiosk.Pick(job_id="j", item_id="strawberry_cake", item_name="草莓蛋糕", started_at=1,
                              time_limit_s=40, state="succeeded", ended_at=2)  # fmt: skip
    session._client = FakeClaude([
        FakeStream(text_events("{sad}刷卡失敗了，再試一次好嗎？"), message([text_block("…")], "end_turn")),
        FakeStream(text_events("{happy}謝謝！"), message([text_block("…")], "end_turn")),
    ])  # fmt: skip

    async def run():
        assert "NT$180" in await session.tool_request_payment("strawberry_cake")
        with pytest.raises(kiosk.ToolError, match="already waiting"):
            await session.tool_request_payment("strawberry_cake")
        with pytest.raises(kiosk.ToolError, match="payment is still waiting"):
            await session.tool_clear_context()
        first = session.payment.id
        await session.payment_result("stale", True)  # ignored
        await session.payment_result(first, False, "insufficient funds")
        await settle(session)
        await session.payment_result(first, True)  # already answered: ignored
        await session.tool_request_payment("strawberry_cake")
        await session.payment_result(session.payment.id, True)
        await settle(session)
        with pytest.raises(kiosk.ToolError, match="already paid"):
            await session.tool_request_payment("strawberry_cake")

    asyncio.run(run())
    events = [e["text"] for e in client.of("robot_event")]
    assert len(events) == 2 and "declined (insufficient funds)" in events[0] and "approved" in events[1]
    assert [e["payment"]["state"] for e in client.of("payment")] == ["pending", "declined", "pending", "paid"]
    assert_valid_history(session.messages)


def test_reset_cancels_a_waiting_payment(env):
    session, client = env
    session.pick = kiosk.Pick(job_id="j", item_id="tiramisu", item_name="提拉米蘇", started_at=1,
                              time_limit_s=40, state="succeeded", ended_at=2)  # fmt: skip

    async def run():
        await session.tool_request_payment("tiramisu")
        await session.reset()

    asyncio.run(run())
    assert session.payment.state == "cancelled" and session.payments == 0
    assert "payment" in session.snapshot()


def test_duck_gain_is_bounded():
    assert kiosk.KioskConfig().duck_gain == 0.2
    with pytest.raises(ValueError):
        kiosk.KioskConfig(duck_gain=1.5)
