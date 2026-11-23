"""Shop-assistant chat for an iPad on the robot ("kiosk", served at /kiosk).

    iPad mic -> AssemblyAI streaming (browser, temporary token minted here)
             -> final transcript over /api/kiosk/ws
             -> Claude with tools (here) -> sentences -> ElevenLabs -> mp3 back over the WebSocket

Claude's tools look through the robot's cameras, read a simulated shop inventory, run the trained
ACT pick policy for an item (a one-episode live evaluation on the GPU server, the same job the
Train & Eval page starts), follow that job, stop it, and leave notes for the shop owner.

Keys come from the environment of ./panel: ANTHROPIC_API_KEY (or an `ant auth login` profile),
ASSEMBLYAI_API_KEY, ELEVENLABS_API_KEY. Without ElevenLabs the iPad falls back to the browser voice.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any, Literal

from config import DATA_DIR, load_settings
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/kiosk")
CONFIG_FILE = DATA_DIR / "kiosk.json"
EVENT_PREFIX = "[EVAL:event] "  # examples/alohamini/eval_control.py
FACES = ("happy", "excited", "thinking", "sad", "surprised", "love", "neutral")
MAX_TOOL_STEPS = 8
PICK_POLL_S = 1.0
SIM_PICK_S = 8  # how long a simulated pick pretends to take

# ---------- config & simulated inventory (one JSON file, edited from the kiosk's debug drawer) ----------


class Item(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    price: str = Field(default="", max_length=20)
    position: str = Field(
        default="", max_length=80, description="Where it sits on the table, as Claude should say it"
    )
    on_table: bool = True
    back_stock: int = Field(default=0, ge=0, le=999)
    sold: int = Field(default=0, ge=0)
    checkpoint: str = Field(default="", max_length=1000, description="ACT checkpoint on the GPU server")
    task: str = Field(default="", max_length=500, description="Task text used when this policy was recorded")
    episode_time_s: int = Field(default=40, ge=5, le=300)


class KioskConfig(BaseModel):
    shop_name: str = Field(default="AlohaMini 甜點店", max_length=80)
    # Conversation language (BCP 47). Speech recognition, the voice and Claude's replies follow it.
    language: str = Field(default="zh-TW", max_length=20)
    model: str = Field(default="claude-opus-5-5", max_length=80)
    effort: Literal["low", "medium", "high"] = "low"
    tts_model: str = Field(default="eleven_v4", max_length=80)
    voice_id: str = Field(default="21m00Tcm4TlvDq8ikWAM", max_length=80)
    extra_instructions: str = Field(default="", max_length=4000)
    # Safety: the robot only moves while staff have armed it here (the iPad's debug drawer).
    armed: bool = False
    # Rehearse the conversation without the robot: picks pretend to run for a few seconds.
    simulate_picks: bool = False
    idle_reset_s: int = Field(default=180, ge=0, le=3600)
    items: list[Item] = Field(
        default_factory=lambda: [
            Item(
                id="strawberry_cake",
                name="草莓蛋糕",
                price="NT$180",
                position="桌子左邊，紅色盒子",
                description="香草海綿蛋糕夾新鮮草莓和鮮奶油，果香清爽、不會太甜。",
                back_stock=3,
                task="Pick the strawberry cake box and put it in the cardboard box",
            ),
            Item(
                id="tiramisu",
                name="提拉米蘇",
                price="NT$200",
                position="桌子右邊，黑色盒子",
                description="浸過濃縮咖啡的手指餅乾、馬斯卡彭起司奶油和可可粉。含咖啡因和少許酒精。",
                back_stock=0,
                task="Pick the tiramisu box and put it in the cardboard box",
            ),
        ]
    )


class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()

    def load(self) -> KioskConfig:
        with self.lock:
            try:
                return KioskConfig.model_validate_json(CONFIG_FILE.read_text())
            except (OSError, ValueError):
                return KioskConfig()

    def save(self, cfg: KioskConfig) -> KioskConfig:
        ids = [i.id for i in cfg.items]
        if len(ids) != len(set(ids)):
            raise HTTPException(400, "Item ids must be unique.")
        with self.lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(cfg.model_dump_json(indent=2))
            tmp.replace(CONFIG_FILE)
        return cfg

    def update(self, fn: Callable[[KioskConfig], None]) -> KioskConfig:
        cfg = self.load()
        fn(cfg)
        return self.save(cfg)


store = Store()

# Set by main.py: the panel's camera relay and its guarded "start a GPU job" route.
_hooks: dict[str, Any] = {}


def configure(
    *, camera_feed, camera_port: int, start_job: Callable[[Any], dict], gpu_call: Callable[..., dict]
) -> None:
    _hooks.update(camera_feed=camera_feed, camera_port=camera_port, start_job=start_job, gpu_call=gpu_call)


# ---------- speech text helpers ----------

_FACE_RE = re.compile(r"\{(" + "|".join(FACES) + r")\}\s*", re.I)
# Latin sentences end at ". " etc.; Chinese ones at 。！？ (no space follows).
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|(?<=[。！？])[」』\"')）]*|\n+")
_COMMA = re.compile(r"[,，、；;：:]")


class SentenceSplitter:
    """Turn streamed text into speakable sentences; pull out {face} tags and markdown."""

    def __init__(self, first_flush_chars: int = 24) -> None:
        self.buf = ""
        self.first = True
        self.first_flush_chars = first_flush_chars

    def feed(self, text: str) -> tuple[list[str], list[str]]:
        self.buf += text
        faces = [m.lower() for m in _FACE_RE.findall(self.buf)]
        # Keep an unfinished "{hap" at the end of the buffer until it completes.
        cut = self.buf.rfind("{")
        tail = ""
        if cut != -1 and "}" not in self.buf[cut:] and len(self.buf) - cut < 12:
            self.buf, tail = self.buf[:cut], self.buf[cut:]
        self.buf = _FACE_RE.sub("", self.buf)
        out = []
        while True:
            m = _SENTENCE_END.search(self.buf)
            if not m:
                break
            out.append(self.buf[: m.start()])
            self.buf = self.buf[m.end() :]
        # Start talking sooner: the first chunk may end at a comma once it is long enough.
        if self.first and not out and len(self.buf) > self.first_flush_chars:
            commas = [m.end() for m in _COMMA.finditer(self.buf) if m.start() >= 6]
            if commas:
                out.append(self.buf[: commas[-1]])
                self.buf = self.buf[commas[-1] :]
        self.buf += tail
        out = [s for s in (clean_speech(x) for x in out) if s]
        if out:
            self.first = False
        return out, faces

    def flush(self) -> tuple[list[str], list[str]]:
        faces = [m.lower() for m in _FACE_RE.findall(self.buf)]
        rest = clean_speech(_FACE_RE.sub("", self.buf))
        self.buf = ""
        return ([rest] if rest else []), faces


def clean_speech(text: str) -> str:
    text = re.sub(r"[*_#`>]+", "", text)
    text = re.sub(r"\{[a-z]*\}?", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------- prompt & tools ----------


def system_prompt(cfg: KioskConfig) -> str:
    catalog = "\n".join(
        f"- {i.name} (id `{i.id}`, {i.price or 'no price set'}), normally {i.position or 'on the table'}: {i.description}"
        for i in cfg.items
    )
    return f"""You are the voice of AlohaMini, a friendly two-armed robot working the counter at {cfg.shop_name}. \
Customers talk to you out loud; an iPad on your chest shows your face. Your words are converted to speech, \
so write exactly what you would say: one to three short, natural sentences, no lists, no markdown, no emoji, \
and prices said the way people say them.

Language: speak {language_name(cfg.language)} by default. Customers mostly speak Mandarin as used in Taiwan: \
reply in Traditional Chinese characters with Taiwanese wording (e.g. 蛋糕、外帶盒、好喔、沒問題), never Simplified \
characters. The speech recognizer may hand you Simplified characters, English words mixed in, or small \
mistranscriptions (e.g. 提拉米蘇 heard as 踢拉米酥); read through them. If a customer speaks another language, \
answer in that language.

Start every reply with one face tag that matches your mood: {", ".join("{" + f + "}" for f in FACES)}. \
You may change it mid-reply with another tag. Tags are never spoken.

What the shop sells (the catalog; stock levels change, so check them):
{catalog}

The cakes on display sit on the table in front of you, with an open cardboard takeaway box in the middle. \
When a customer decides on a cake, you pick it up with your arms and put it in the takeaway box.

How to work:
- When someone asks what is available, look at the table and check the inventory (call both tools at once), \
then answer from what you actually see. If the camera and the inventory disagree, trust the camera for what \
is on the table and mention it to the owner with notify_owner.
- Recommend with a short reason that fits the customer. Respect a change of mind without arguing.
- Stock matters: if an item is the last one, you may say so. If an item is not on the table but there is \
back stock, say staff can bring one from the back and notify the owner. If it is sold out, say so and suggest \
the alternative.
- Call pick_item only once the customer has clearly chosen one item that is on the table. If the choice is \
unclear, ask. Tell them you are getting it as you start. Never start a second pick while one is running.
- While a pick runs, keep chatting if spoken to; check_pick_status says how it is going. If the customer says \
stop, or anything sounds unsafe, call stop_robot immediately.
- Messages starting with [Robot event] come from your own systems, not the customer. React to them out loud \
in one short sentence (for example, hand over the cake when a pick finishes).
- If a tool reports the robot is not armed or another problem, explain briefly and offer to call staff \
(notify_owner).
- Payment, allergies beyond the descriptions and anything you cannot do: say a staff member will help.
{cfg.extra_instructions}""".strip()


def language_name(code: str) -> str:
    return {
        "zh-TW": "Mandarin Chinese as spoken in Taiwan (繁體中文)",
        "zh": "Mandarin Chinese",
        "en": "English",
        "ja": "Japanese",
    }.get(code, code)


def _tool(
    name: str, description: str, properties: dict | None = None, required: list[str] | None = None
) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    }


TOOLS = [
    _tool(
        "look_at_table",
        "Take a photo with the robot's cameras (chest camera looking across the table, overhead "
        "camera looking down at the arms and table). Use it to see which items are on the table, where they are, "
        "and whether a pick worked.",
        {
            "camera": {
                "type": "string",
                "enum": ["both", "chest", "overhead"],
                "description": "Which camera; default both.",
            }
        },
    ),
    _tool(
        "check_inventory",
        "Read the shop's inventory system: for every item, whether one is on display on the "
        "table, how many are in the back, how many were sold today, and whether the robot has a trained pick "
        "skill for it.",
    ),
    _tool(
        "pick_item",
        "Start the robot arm picking one item from the table and putting it into the takeaway box. "
        "Returns immediately; the pick takes 20-60 seconds. You will get a [Robot event] when it ends.",
        {"item_id": {"type": "string", "description": "Item id from the catalog."}},
        ["item_id"],
    ),
    _tool(
        "check_pick_status", "How the current (or last) pick is going: state, seconds elapsed, time limit."
    ),
    _tool(
        "stop_robot",
        "Immediately stop the running pick (customer said stop, something is in the way, or it "
        "looks unsafe).",
    ),
    _tool(
        "notify_owner",
        "Send a note to the shop owner's screen, e.g. restock the display, an item is sold out, "
        "a customer needs help, or the robot needs attention.",
        {"message": {"type": "string"}, "urgent": {"type": "boolean"}},
        ["message"],
    ),
]


def validate_input(name: str, data: Any) -> str | None:
    """Tool inputs stream eagerly (no server-side validation), so check them here."""
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        return f"Unknown tool {name}."
    if not isinstance(data, dict):
        return "Tool input must be an object."
    props = tool["input_schema"]["properties"]
    for key in tool["input_schema"]["required"]:
        if key not in data:
            return f"Missing required field {key}."
    for key, value in data.items():
        spec = props.get(key)
        if spec is None:
            return f"Unexpected field {key}."
        kind = {"string": str, "boolean": bool}[spec["type"]]
        if not isinstance(value, kind) or ("enum" in spec and value not in spec["enum"]):
            return f"Invalid value for {key}."
    return None


# ---------- picks (one-episode live evaluations on the GPU server) ----------


class Pick(BaseModel):
    job_id: str
    item_id: str
    item_name: str
    started_at: float
    time_limit_s: int
    state: str = (
        "starting"  # starting | running | stopping | succeeded | failed | stopped | interrupted | error
    )
    detail: str = ""
    episode_started_at: float | None = None
    ended_at: float | None = None
    simulated: bool = False

    @property
    def active(self) -> bool:
        return self.state in ("starting", "running", "stopping")

    def summary(self) -> dict:
        now = self.ended_at or time.time()
        return {**self.model_dump(), "active": self.active, "elapsed_s": round(now - self.started_at)}


def pick_request(item: Item):
    from gpu_jobs import JobRequest

    return JobRequest(
        kind="robot_eval",
        name=f"kiosk-{item.id.replace('_', '-')}"[:60],
        checkpoint=item.checkpoint,
        task=item.task or f"Pick the {item.name} and put it in the box",
        episodes=1,
        episode_time_s=item.episode_time_s,
        reset_time_s=0,
        record=False,
        robot_ready=True,
    )


def eval_events(lines: list[str]) -> list[dict]:
    events = []
    for line in lines:
        i = line.find(EVENT_PREFIX)
        if i != -1:
            with contextlib.suppress(ValueError):
                events.append(json.loads(line[i + len(EVENT_PREFIX) :]))
    return events


# ---------- text to speech ----------


async def synthesize(cfg: KioskConfig, text: str, previous: str, http) -> bytes | None:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return None
    params = {"output_format": "mp3_44100_128"}
    headers = {"xi-api-key": key}
    if cfg.tts_model.startswith("eleven_v4"):
        # Eleven v4 is served through the Text to Dialogue API (one speaker here).
        url = "https://api.elevenlabs.io/v1/text-to-dialogue/stream"
        body: dict = {
            "inputs": [{"text": text, "voice_id": cfg.voice_id}],
            "model_id": cfg.tts_model,
            "language_code": cfg.language.split("-")[0],
        }
        if previous:
            body["previous_text"] = previous[-100:]
    else:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{cfg.voice_id}/stream"
        body = {"text": text, "model_id": cfg.tts_model}
        if cfg.tts_model != "eleven_multilingual_v2":  # the only model that rejects language_code
            body["language_code"] = cfg.language.split("-")[0]
        if previous:
            body["previous_text"] = previous[-500:]
    response = await http.post(url, params=params, headers=headers, json=body, timeout=20)
    if response.status_code >= 400:
        raise RuntimeError(f"ElevenLabs {response.status_code}: {response.text[:200]}")
    return response.content


class Speaker:
    """Synthesizes sentences concurrently and sends them to the iPad in order."""

    def __init__(self, session: Session, turn: str) -> None:
        self.session, self.turn = session, turn
        self.queue: asyncio.Queue = asyncio.Queue()
        self.tasks: list[asyncio.Task] = []
        self.previous = ""
        self.seq = 0
        self.limit = asyncio.Semaphore(3)
        self.first_audio_at: float | None = None
        self.sender = asyncio.create_task(self._send())

    def say(self, sentence: str) -> None:
        cfg, previous = store.load(), self.previous

        async def job():
            async with self.limit:
                return await synthesize(cfg, sentence, previous, self.session.http())

        task = asyncio.create_task(job())
        self.tasks.append(task)
        self.previous = f"{self.previous} {sentence}".strip()
        self.queue.put_nowait((sentence, task))

    async def _send(self) -> None:
        while (item := await self.queue.get()) is not None:
            sentence, task = item
            error = None
            try:
                audio = await task
            except Exception as e:  # noqa: BLE001 — speech failure must not end the turn
                audio, error = None, str(e)
            if self.first_audio_at is None:
                self.first_audio_at = time.time()
            await self.session.emit({
                "type": "speech", "turn": self.turn, "seq": self.seq, "text": sentence,
                "audio": base64.b64encode(audio).decode() if audio else None, "error": error,
            })  # fmt: skip
            self.seq += 1

    async def finish(self) -> None:
        self.queue.put_nowait(None)
        await self.sender

    def cancel(self) -> None:
        self.sender.cancel()
        for task in self.tasks:
            task.cancel()


# ---------- the conversation ----------


class Session:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.clients: set[WebSocket] = set()
        self.log: deque[dict] = deque(maxlen=400)
        self.turn_task: asyncio.Task | None = None
        self.state = "idle"
        self.pick: Pick | None = None
        self.pick_task: asyncio.Task | None = None
        self.notifications: deque[dict] = deque(maxlen=50)
        self.last_activity = time.time()
        self._client = None
        self._http = None

    # -- plumbing

    def anthropic(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic()
        return self._client

    def http(self):
        if self._http is None:
            import httpx2

            self._http = httpx2.AsyncClient()
        return self._http

    async def emit(self, event: dict, keep: bool = True) -> None:
        event = {"at": time.time(), **event}
        if keep and event["type"] != "speech":
            self.log.append(event)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            await self.emit({"type": "state", "state": state}, keep=False)

    def snapshot(self) -> dict:
        return {
            "type": "snapshot", "state": self.state, "log": list(self.log),
            "pick": self.pick.summary() if self.pick else None, "notifications": list(self.notifications),
            "config": store.load().model_dump(), "keys": keys_status(),
        }  # fmt: skip

    # -- turns

    async def user_said(self, text: str, metrics: dict | None = None) -> None:
        text = text.strip()
        if not text:
            return
        cfg = store.load()
        if cfg.idle_reset_s and self.messages and time.time() - self.last_activity > cfg.idle_reset_s:
            await self.reset("new customer (idle)")
        self.last_activity = time.time()
        await self.interrupt("customer spoke")
        self.messages.append({"role": "user", "content": text})
        await self.emit({"type": "user", "text": text, "metrics": metrics or {}})
        self._start_turn()

    async def robot_event(self, text: str) -> None:
        """Our own systems speaking (e.g. a pick finished): wait for the current reply, then react."""
        if self.turn_task and not self.turn_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.turn_task
        self.messages.append({"role": "user", "content": f"[Robot event] {text}"})
        await self.emit({"type": "robot_event", "text": text})
        self._start_turn()

    def _start_turn(self) -> None:
        self.turn_task = asyncio.create_task(self._turn(uuid.uuid4().hex[:8]))

    async def interrupt(self, reason: str) -> None:
        task = self.turn_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await self.emit({"type": "interrupted", "reason": reason})

    async def reset(self, reason: str = "reset") -> None:
        await self.interrupt(reason)
        self.messages = []
        await self.emit({"type": "reset", "reason": reason})

    async def _turn(self, turn: str) -> None:
        speaker = Speaker(self, turn)
        splitter = SentenceSplitter()
        partial: list[str] = []
        pending: asyncio.Future | None = None
        t0 = time.time()
        timing: dict = {}
        try:
            for _step in range(MAX_TOOL_STEPS):
                await self.set_state("thinking")
                message = await self._stream(turn, speaker, splitter, partial, timing)
                self.messages.append({"role": "assistant", "content": message.content})
                partial.clear()
                if message.stop_reason == "refusal":
                    speaker.say("Sorry, I can't help with that one. A staff member can.")
                    break
                uses = [b for b in message.content if b.type == "tool_use"]
                if not uses:
                    break
                pending = asyncio.ensure_future(self._run_tools(uses))
                results = await asyncio.shield(pending)
                pending = None
                self.messages.append({"role": "user", "content": results})
            else:
                speaker.say("Sorry, I got a bit tangled up there.")
            await self.set_state("speaking")
            await speaker.finish()
            await self.emit({"type": "turn_done", "turn": turn, "metrics": {
                "first_text_s": round(timing["first_text"] - t0, 2) if "first_text" in timing else None,
                "first_audio_s": round(speaker.first_audio_at - t0, 2) if speaker.first_audio_at else None,
                "total_s": round(time.time() - t0, 2),
            }})  # fmt: skip
        except asyncio.CancelledError:
            speaker.cancel()
            # Keep the history valid: every tool_use needs its result, even when interrupted.
            if pending is not None:
                self.messages.append({"role": "user", "content": await pending})
            elif partial:
                said = clean_speech(_FACE_RE.sub("", "".join(partial)))
                if said:
                    self.messages.append(
                        {"role": "assistant", "content": f"{said}... (cut off by the customer)"}
                    )
            raise
        except Exception as e:  # noqa: BLE001
            speaker.cancel()
            await self.emit({"type": "error", "where": "llm", "message": f"{type(e).__name__}: {e}"})
            fallback = Speaker(self, turn + "e")
            fallback.say("Sorry, my brain hiccupped. Could you say that again?")
            await fallback.finish()
        finally:
            await self.set_state("idle")

    async def _stream(
        self, turn: str, speaker: Speaker, splitter: SentenceSplitter, partial: list[str], timing: dict
    ):
        cfg = store.load()
        kwargs: dict = {
            "model": cfg.model,
            "max_tokens": 16000,
            "system": system_prompt(cfg),
            "tools": TOOLS,
            "messages": self.messages,
            "output_config": {"effort": cfg.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if cfg.model.startswith(("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")):
            # Policy declines are re-run on a fallback model inside the same call.
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")

        async with self.anthropic().beta.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    timing.setdefault("first_text", time.time())
                    partial.append(event.delta.text)
                    sentences, faces = splitter.feed(event.delta.text)
                    await self._speak(turn, speaker, sentences, faces)
                elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                    await self.set_state(f"tool:{event.content_block.name}")
            message = await stream.get_final_message()
        sentences, faces = splitter.flush()
        await self._speak(turn, speaker, sentences, faces)
        splitter.first = True
        await self.emit(
            {
                "type": "llm_step",
                "turn": turn,
                "stop_reason": message.stop_reason,
                "model": message.model,
                "usage": {
                    k: getattr(message.usage, k, None)
                    for k in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                },
            }
        )
        return message

    async def _speak(self, turn: str, speaker: Speaker, sentences: list[str], faces: list[str]) -> None:
        for face in faces:
            await self.emit({"type": "face", "face": face}, keep=False)
        for sentence in sentences:
            speaker.say(sentence)
            await self.emit({"type": "assistant", "turn": turn, "text": sentence})

    async def _run_tools(self, uses) -> list[dict]:
        async def one(use) -> dict:
            started = time.time()
            error = validate_input(use.name, use.input)
            content: Any
            if error:
                content, is_error = error, True
            else:
                try:
                    content, is_error = await getattr(self, f"tool_{use.name}")(**use.input), False
                except ToolError as e:
                    content, is_error = str(e), True
                except Exception as e:  # noqa: BLE001
                    content, is_error = f"{type(e).__name__}: {e}", True
            images = (
                [c["source"]["data"] for c in content if c.get("type") == "image"]
                if isinstance(content, list)
                else []
            )
            text = (
                content
                if isinstance(content, str)
                else " ".join(c.get("text", "") for c in content if c.get("type") == "text")
            )
            await self.emit(
                {
                    "type": "tool",
                    "name": use.name,
                    "input": use.input,
                    "result": text[:2000],
                    "images": images,
                    "is_error": is_error,
                    "duration_s": round(time.time() - started, 2),
                }
            )
            return {"type": "tool_result", "tool_use_id": use.id, "content": content, "is_error": is_error}

        # Parallel calls: all results go back together in one user message.
        return list(await asyncio.gather(*(one(u) for u in uses)))

    # -- tools

    async def tool_look_at_table(self, camera: str = "both") -> list[dict]:
        feed = _hooks.get("camera_feed")
        if feed is None:
            raise ToolError("Cameras are not connected to the chat service.")
        settings = load_settings()
        wanted = None if camera == "both" else camera
        deadline = time.monotonic() + 4
        frames: dict[str, bytes] = {}
        while time.monotonic() < deadline:
            feed.touch(settings.jetson_ip, _hooks["camera_port"])
            names = [c["name"] for c in feed.status()["cameras"] if c["live"]]
            names = [n for n in names if wanted is None or wanted in n.lower()]
            frames = {n: f[0] for n in names if (f := feed.frame(n))}
            if frames:
                break
            await asyncio.sleep(0.2)
        if not frames:
            error = feed.status().get("error")
            raise ToolError(
                "No camera image is available"
                + (f" ({error})" if error else ". The robot host may be running without cameras.")
            )
        content: list[dict] = []
        for name, jpeg in sorted(frames.items()):
            content.append({"type": "text", "text": f"{name} camera:"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(jpeg).decode(),
                    },
                }
            )
        return content

    async def tool_check_inventory(self) -> str:
        cfg = store.load()
        rows = [{
            "id": i.id, "name": i.name, "price": i.price, "on_display_on_table": i.on_table,
            "in_the_back": i.back_stock, "sold_today": i.sold,
            "total_available": int(i.on_table) + i.back_stock,
            "robot_can_pick": bool(i.checkpoint) or cfg.simulate_picks,
        } for i in cfg.items]  # fmt: skip
        note = (
            "" if cfg.armed else " The robot arm is currently NOT armed by staff, so picks will be refused."
        )
        return json.dumps({"items": rows}, ensure_ascii=False) + note

    async def tool_pick_item(self, item_id: str) -> str:
        cfg = store.load()
        item = next((i for i in cfg.items if i.id == item_id), None)
        if item is None:
            raise ToolError(f"No item with id {item_id}. Valid ids: {', '.join(i.id for i in cfg.items)}.")
        if self.pick and self.pick.active:
            raise ToolError(f"A pick of the {self.pick.item_name} is already running.")
        if not item.on_table:
            raise ToolError(
                f"The inventory says no {item.name} is on the table "
                f"({item.back_stock} in the back). Staff must restock the display first."
            )
        if not cfg.armed:
            raise ToolError("The robot arm is not armed by staff, so it cannot move right now.")
        if cfg.simulate_picks:
            self.pick = Pick(
                job_id=f"sim-{uuid.uuid4().hex[:8]}",
                item_id=item.id,
                item_name=item.name,
                started_at=time.time(),
                time_limit_s=min(item.episode_time_s, SIM_PICK_S),
                simulated=True,
            )
        else:
            if not item.checkpoint:
                raise ToolError(f"No trained pick policy is configured for the {item.name}.")
            try:
                job = await asyncio.to_thread(_hooks["start_job"], pick_request(item))
            except HTTPException as e:
                raise ToolError(f"The robot could not start: {e.detail}") from None
            self.pick = Pick(
                job_id=job["id"],
                item_id=item.id,
                item_name=item.name,
                started_at=time.time(),
                time_limit_s=item.episode_time_s,
            )
        await self.emit({"type": "pick", "pick": self.pick.summary()})
        self.pick_task = asyncio.create_task(self._watch_pick(self.pick))
        return (
            f"Started picking the {item.name} (up to {self.pick.time_limit_s} s). "
            "You will get a [Robot event] when it ends."
        )

    async def tool_check_pick_status(self) -> str:
        if not self.pick:
            return "No pick has run in this conversation."
        return json.dumps(self.pick.summary(), ensure_ascii=False)

    async def tool_stop_robot(self) -> str:
        pick = self.pick
        if not pick or not pick.active:
            return "Nothing is running; the arm is idle."
        await self.stop_pick()
        return f"Stop sent for the {pick.item_name} pick."

    async def tool_notify_owner(self, message: str, urgent: bool = False) -> str:
        await self.notify(message, urgent, source="robot")
        return "The owner has been notified."

    # -- pick lifecycle

    async def notify(self, message: str, urgent: bool = False, source: str = "system") -> None:
        note = {"at": time.time(), "message": message, "urgent": urgent, "source": source}
        self.notifications.appendleft(note)
        await self.emit({"type": "notification", **note})

    async def stop_pick(self) -> None:
        pick = self.pick
        if not pick or not pick.active:
            return
        pick.state = "stopping"
        if not pick.simulated:
            try:
                await asyncio.to_thread(_hooks["gpu_call"], "POST", f"/jobs/{pick.job_id}/stop")
            except HTTPException as e:
                pick.detail = f"Stop failed: {e.detail}"
        await self.emit({"type": "pick", "pick": pick.summary()})

    async def end_episode(self) -> None:
        """Staff button: the cake is in the box, end the episode now instead of waiting for the time limit."""
        pick = self.pick
        if pick and pick.active and not pick.simulated:
            await asyncio.to_thread(
                _hooks["gpu_call"], "POST", f"/jobs/{pick.job_id}/command", {"command": "end"}
            )
        elif pick and pick.active:
            pick.time_limit_s = 0

    async def _watch_pick(self, pick: Pick) -> None:
        failures = 0
        while pick.active:
            await asyncio.sleep(PICK_POLL_S)
            if pick.simulated:
                if pick.state == "stopping":
                    pick.state = "stopped"
                elif time.time() - pick.started_at >= pick.time_limit_s:
                    pick.state = "succeeded"
                else:
                    pick.state, pick.episode_started_at = "running", pick.episode_started_at or time.time()
            else:
                try:
                    record = await asyncio.to_thread(_hooks["gpu_call"], "GET", f"/jobs/{pick.job_id}")
                    failures = 0
                except HTTPException as e:
                    failures += 1
                    pick.detail = str(e.detail)
                    if failures >= 20:
                        pick.state = "error"
                    await self.emit({"type": "pick", "pick": pick.summary()}, keep=False)
                    continue
                for ev in eval_events(record.get("lines") or []):
                    if ev.get("event") == "episode_start" and not pick.episode_started_at:
                        pick.episode_started_at = ev.get("at", time.time())
                state = record["state"]
                if pick.state == "stopping" and state == "running":
                    state = "stopping"
                elif pick.state == "starting" and state == "running" and not pick.episode_started_at:
                    state = "starting"
                pick.state = state
            await self.emit({"type": "pick", "pick": pick.summary()}, keep=False)
        pick.ended_at = time.time()
        await self.emit({"type": "pick", "pick": pick.summary()})
        await self._pick_finished(pick)

    async def _pick_finished(self, pick: Pick) -> None:
        took = round(pick.ended_at - pick.started_at) if pick.ended_at else None
        if pick.state == "succeeded":
            cfg = store.load()
            item = next((i for i in cfg.items if i.id == pick.item_id), None)
            if item:

                def sell(c: KioskConfig) -> None:
                    for i in c.items:
                        if i.id == pick.item_id:
                            i.on_table, i.sold = False, i.sold + 1

                cfg = store.update(sell)
                item = next(i for i in cfg.items if i.id == pick.item_id)
                await self.emit({"type": "config", "config": cfg.model_dump()}, keep=False)
                if item.back_stock:
                    await self.notify(
                        f"Restock the display: {item.name} was sold ({item.back_stock} left in the back)."
                    )
                else:
                    await self.notify(f"{item.name} is now sold out.", urgent=True)
            text = (
                f"The pick of the {pick.item_name} finished after {took} s. It should now be in the takeaway "
                "box; you can look_at_table to confirm. The inventory was updated."
            )
        elif pick.state == "stopped":
            text = f"The pick of the {pick.item_name} was stopped after {took} s. The arm is holding still."
        else:
            text = (
                f"The pick of the {pick.item_name} ended with a problem ({pick.state}"
                f"{': ' + pick.detail if pick.detail else ''})."
            )
            await self.notify(f"Robot pick of {pick.item_name} ended: {pick.state}.", urgent=True)
        await self.robot_event(text)


class ToolError(Exception):
    pass


session = Session()


def keys_status() -> dict:
    return {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "assemblyai": bool(os.environ.get("ASSEMBLYAI_API_KEY")),
        "elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY")),
    }


# ---------- routes ----------


@router.get("/config")
def get_config() -> KioskConfig:
    return store.load()


@router.put("/config")
async def put_config(cfg: KioskConfig) -> KioskConfig:
    saved = store.save(cfg)
    await session.emit({"type": "config", "config": saved.model_dump()}, keep=False)
    return saved


@router.get("/stt-token")
async def stt_token() -> dict:
    """A short-lived AssemblyAI token so the iPad streams audio straight to AssemblyAI without our key."""
    key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not key:
        raise HTTPException(409, "Set ASSEMBLYAI_API_KEY for ./panel to use the microphone.")
    response = await session.http().post(
        "https://token.assemblyai.com/v1/token",
        headers={"Authorization": key},
        json={"product": "streaming", "expires_in_seconds": 60, "max_session_duration_seconds": 3600},
        timeout=10,
    )
    if response.status_code >= 400:
        raise HTTPException(502, f"AssemblyAI token request failed ({response.status_code}).")
    cfg = store.load()
    from urllib.parse import urlencode

    terms = sorted(
        {i.name for i in cfg.items} | {"AlohaMini", "提拉米蘇", "草莓蛋糕", "tiramisu", "strawberry"}
    )
    params = {
        "token": response.json()["token"],
        "speech_model": "assemblyai-stt-3-7-realtime-agent",
        "encoding": "pcm_s16le",
        "sample_rate": 16000,
        "keyterms_prompt": json.dumps(terms),
        "voice_focus": "far-field",
    }  # fmt: skip
    if cfg.language.startswith("zh"):
        # Taiwanese customers mix in English words (tiramisu, OK), so allow both.
        params["language_codes"] = json.dumps(["zh", "en"])
        params["prompt"] = "台灣甜點店櫃台的對話，顧客說台灣華語，可能夾雜英文。請用繁體中文轉寫。"
    query = urlencode(params)
    return {"url": f"wss://streaming.assemblyai.com/v3/ws?{query}", "sample_rate": 16000}


@router.websocket("/ws")
async def kiosk_ws(ws: WebSocket) -> None:
    await ws.accept()
    session.clients.add(ws)
    await ws.send_json(session.snapshot())
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "user_text":
                asyncio.create_task(session.user_said(str(msg.get("text", ""))[:2000], msg.get("metrics")))
            elif kind == "interrupt":
                asyncio.create_task(session.interrupt("barge-in"))
            elif kind == "reset":
                asyncio.create_task(session.reset())
            elif kind == "stop_robot":
                asyncio.create_task(session.stop_pick())
            elif kind == "end_episode":
                asyncio.create_task(_safe(session.end_episode()))
            elif kind == "clear_notifications":
                session.notifications.clear()
                await session.emit({"type": "notifications_cleared"}, keep=False)
            elif kind == "client_log":
                await session.emit({"type": "client", "message": str(msg.get("message", ""))[:500]})
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        session.clients.discard(ws)


async def _safe(coro) -> None:
    try:
        await coro
    except HTTPException as e:
        await session.emit({"type": "error", "where": "robot", "message": str(e.detail)})
