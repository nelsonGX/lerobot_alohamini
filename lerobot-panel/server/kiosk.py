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
import difflib
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
STANDBY_JOB_NAME = "kiosk-standby"
STANDBY_START_TIMEOUT_S = 120
PHOTO_TIMEOUT_S = 3
MAX_PHOTO_B64 = 2_000_000
HOME_WAIT_S = 40  # the eval script gives up after 20 s of moving
SIM_HOME_S = 2
MAX_ATTEMPTS = 3  # picks of one item per conversation (the first try and two retries)
MAX_PAYMENTS = 3  # card attempts per conversation
RETRY_WINDOW_S = 600  # a finished pick can be retried (and its sale undone) this long after it ended  # a pick waiting for the warm job to load (or to be idle) gives up after this
SIM_PICK_S = 8  # how long a simulated pick pretends to take
PICK_WATCH_TIMEOUT_S = 10  # a camera check that takes longer counts as "still trying"

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
    voice_id: str = Field(default="EXAVITQu4vr4xnSDxMaL", max_length=80)
    extra_instructions: str = Field(default="", max_length=4000)
    # Safety: the robot only moves while staff have armed it here (the iPad's debug drawer).
    armed: bool = False
    # Rehearse the conversation without the robot: picks pretend to run for a few seconds.
    simulate_picks: bool = False
    idle_reset_s: int = Field(default=180, ge=0, le=3600)
    # Listening (applied on the iPad). Speech quieter than this (dBFS) can't interrupt the robot or start a
    # turn: the customer at the iPad is much louder than people chatting nearby. -80 turns the gate off.
    mic_gate_db: int = Field(default=-45, ge=-80, le=-10)
    # How much a customer must say (CJK characters; a Latin word counts 2) to interrupt the robot mid-sentence.
    # Short stop words (停, 等一下, stop, ...) always interrupt.
    barge_in_chars: int = Field(default=6, ge=1, le=30)
    # The robot's volume (fraction of full) while someone talks over it, until we know whether it was an
    # interruption. 1 = don't turn it down, 0 = silent. Applied on the iPad.
    duck_gain: float = Field(default=0.2, ge=0, le=1)
    # A fast model decides whether each heard utterance was said to the robot (vs. people talking among
    # themselves). "" turns the check off; any failure or timeout counts as "yes".
    addressee_model: str = Field(default="claude-haiku-5-5", max_length=80)
    # The iPad's front camera: only listen while a face at least this wide (fraction of the picture) is in front,
    # and tell the addressee check whether that person faced the robot and moved their lips.
    camera_listen: bool = True
    face_min_size: float = Field(default=0.06, ge=0.01, le=0.6)
    # The face model misses faces smaller than ~8% of what it looks at, so it looks at the middle of the
    # picture enlarged by this much (customers stand in front of the robot).
    camera_zoom: float = Field(default=1.5, ge=1, le=3)
    camera_crop_y: float = Field(default=0.3, ge=0, le=1)  # where the zoomed part sits: 0 top, 1 bottom
    greet_on_arrival: bool = True
    # Start over for the next customer once nobody has been at the counter this long (0 = off).
    leave_reset_s: int = Field(default=20, ge=0, le=600)
    # Let Claude ask the iPad for a photo of the customer (look_at_customer). Off: the camera stays on-device.
    customer_photos: bool = True
    # While a pick runs, a fast vision model looks through the robot's cameras every few seconds and ends the
    # episode early once the item is in the box, or once it has seen the pick fail this many checks in a row
    # (so Claude can reset the arm and retry instead of waiting out the time limit). "" turns it off.
    pick_watch_model: str = Field(default="claude-haiku-5-5", max_length=80)
    pick_watch_after_s: int = Field(default=6, ge=1, le=120)  # first check, after the episode starts
    pick_watch_every_s: int = Field(default=4, ge=1, le=60)
    pick_watch_misses: int = Field(default=2, ge=1, le=10)
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
- When a pick ends, look_at_table to see whether the cake is in the takeaway box. If it missed (the cake \
is still on the table, tipped over, or the arm stopped short), tell the customer you will try again, call \
reset_arm, then pick_item for the same item with retry=true (that corrects the inventory). If the [Robot event] \
says the camera already saw it miss, skip the look: tell the customer, reset_arm and pick_item again right away. \
After a second miss, apologise, notify_owner, and say staff will help.
- Once look_at_table shows the cake in the takeaway box, tell the customer the price and call request_payment \
for that item: the card terminal lights up and they tap their card. You get a [Robot event] when it is paid \
or declined. Paid: thank them and say the box is ready to take. Declined: say so kindly and offer to try \
again (call request_payment again) or another card; after two declines, notify_owner and say staff will help.
- While a pick runs, a camera watch checks it every few seconds and ends it early when the cake lands in the box \
or the pick clearly fails, so you hear about it quickly. Keep chatting if spoken to; check_pick_status says how it \
is going. If the customer says it is already in the box, or that it fell or the arm keeps missing, look_at_table \
and call end_pick with what you see. If the customer says stop, or anything sounds unsafe, call stop_robot \
immediately.
- look_at_customer shows you the customer (the iPad's front camera). Use it when they point, gesture or \
say "this one", not on every turn. Never comment on looks, age guesses or anything personal beyond what \
helps serve them.
- Messages starting with [Robot event] come from your own systems, not the customer. React to them out loud \
in one short sentence (for example, hand over the cake when a pick finishes).
- If a tool reports the robot is not armed or another problem, explain briefly and offer to call staff \
(notify_owner).
- When the customer says goodbye or walks off, or asks to start over, say a short goodbye and call \
clear_context so the next customer starts fresh (not while a payment is waiting).
- Cash, refunds, allergies beyond the descriptions and anything you cannot do: say a staff member will help.
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
        "look_at_customer",
        "Take a photo with the iPad's front camera, which faces the customer standing at the counter. Use it "
        "when you need to see them: they point or gesture ('this one', holding up fingers), you want to know "
        "who you are talking to (a child, a group), or to check they are still there. Not needed for normal "
        "talk. The photo is the camera's view, so the customer's right hand appears on the left.",
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
        {
            "item_id": {"type": "string", "description": "Item id from the catalog."},
            "retry": {
                "type": "boolean",
                "description": "true only to retry the item whose last pick missed, after look_at_table showed "
                "it is still on the table (undoes that pick's sale). Leave out for a new pick.",
            },
        },
        ["item_id"],
    ),
    _tool(
        "check_pick_status",
        "How the current (or last) pick is going: state, seconds elapsed, time limit, and what the camera "
        "watch last saw (last_check).",
    ),
    _tool(
        "end_pick",
        "End the running pick now, before its time limit, without stopping or disarming the robot. Use it "
        "when look_at_table shows the item is already in the takeaway box (outcome in_box), or the pick has "
        "clearly failed: dropped, tipped over, knocked out of reach, or the arm keeps missing (outcome missed). "
        "You get a [Robot event] when it has ended. For danger use stop_robot instead.",
        {"outcome": {"type": "string", "enum": ["in_box", "missed"]}},
        ["outcome"],
    ),
    _tool(
        "reset_arm",
        "Move both arms back to their starting pose (where they were when staff armed the robot). Use it "
        "after a pick that missed, before calling pick_item again, or when the arms are left in an odd "
        "position. Takes up to ~20 s and returns once the arms are home. Not while a pick is running.",
    ),
    _tool(
        "request_payment",
        "Show the price of an item the robot just put in the takeaway box on the card terminal and wait for "
        "the customer to pay. Returns immediately; you get a [Robot event] when the payment is approved or "
        "declined. Only after a successful pick of that item.",
        {"item_id": {"type": "string", "description": "Item id from the catalog."}},
        ["item_id"],
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
    _tool(
        "clear_context",
        "Forget this conversation once you have finished replying, so the next customer starts "
        "fresh. Use it when the customer says goodbye or leaves, or asks to start over. Say your goodbye in "
        "the same reply; it is spoken before the memory is cleared.",
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
    mode: Literal["job", "standby", "sim"] = "job"  # standby: one episode of the warm job
    retried: bool = False  # a retry already undid this pick's sale
    # Set when the pick was ended before its time limit: by the camera watch or Claude's end_pick.
    verdict: Literal["in_box", "missed"] | None = None
    last_check: str = ""  # the camera watch's latest verdict, e.g. "trying: gripper holding the box"

    @property
    def active(self) -> bool:
        return self.state in ("starting", "running", "stopping")

    def summary(self) -> dict:
        now = self.ended_at or time.time()
        return {**self.model_dump(), "active": self.active, "elapsed_s": round(now - self.started_at)}


class Payment(BaseModel):
    """A mock card payment: shown on the fake terminal (/kiosk/terminal), where staff press approve or decline."""

    id: str
    item_id: str
    item_name: str
    amount: str
    state: Literal["pending", "paid", "declined", "cancelled"] = "pending"
    reason: str = ""
    created_at: float
    ended_at: float | None = None


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


class Standby(BaseModel):
    """The warm live-evaluation job: every item's policy loaded and the robot connected while armed."""

    job_id: str = ""
    state: Literal["off", "loading", "ready", "busy", "stopping", "error"] = "off"
    detail: str = ""
    policies: list[str] = Field(default_factory=list)
    signature: str = ""
    started_at: float | None = None
    ready_at: float | None = None

    @property
    def running(self) -> bool:
        return self.state in ("loading", "ready", "busy")


def standby_policies(cfg: KioskConfig) -> list[dict]:
    return [
        {
            "name": i.id,
            "checkpoint": i.checkpoint,
            "task": i.task or f"Pick the {i.name} and put it in the box",
            "episode_time_s": i.episode_time_s,
        }
        for i in cfg.items
        if i.checkpoint
    ]


def standby_signature(policies: list[dict]) -> str:
    return json.dumps(policies, sort_keys=True, ensure_ascii=False)


def standby_request(policies: list[dict]):
    from gpu_jobs import JobRequest

    return JobRequest(
        kind="robot_eval", name=STANDBY_JOB_NAME, standby_policies=policies, record=False, robot_ready=True
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


# ---------- is it for me? ----------

ADDRESSEE_TIMEOUT_S = 2.0
_STOP_WORDS = re.compile(r"停|等一下|等等|暫停|不要了|stop|wait|hold on", re.I)
REPEAT_WINDOW_S = 30.0  # customer speech this recent counts when looking for repeats
# Customer speech heard while the robot was replying that didn't stop it goes into the history behind this.
HEARD_PREFIX = "[While you were replying the customer also said this; it did not interrupt you. Take it into account, but don't repeat yourself:]"


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())  # CJK characters count as word characters


def is_repeat(text: str, earlier: list[str]) -> bool:
    """The customer said (part of) the same thing again, usually because the reply hadn't started yet."""
    a = _norm(text)
    if not a:
        return True
    for b in map(_norm, earlier):
        if b and ((len(a) >= 2 and a in b) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6):
            return True
    return False


class VisionSummary(BaseModel):
    """What the iPad's front camera saw while one utterance was spoken (see app/kiosk/vision.ts)."""

    present: float = Field(ge=0, le=1)  # share of frames with someone at the counter
    facing: float = Field(ge=0, le=1)  # of those, share facing the robot
    mouth: float = Field(default=0, ge=0, le=1)  # jawOpen std dev
    lips: bool = False  # their lips moved


def camera_line(v: VisionSummary | None) -> str:
    if v is None:
        return ""
    facing = "facing the robot" if v.facing >= 0.5 else "mostly turned away from the robot"
    if v.lips:
        return f"\nCamera: a person stood at the counter, {facing}, and their lips moved while this was said."
    return (
        f"\nCamera: a person stood at the counter, {facing}, but their lips did not visibly move while this was "
        "said, so it may have come from someone else nearby (or they wear a mask)."
    )


def trusted(v: VisionSummary | None) -> bool:
    """The person in front, facing the robot, was visibly talking: no need to ask the addressee model."""
    return v is not None and v.lips and v.facing >= 0.5


def addressee_prompt(
    cfg: KioskConfig, recent: list[str], text: str, robot_talking: bool, vision: VisionSummary | None = None
) -> str:
    history = "\n".join(recent[-6:]) or "(no conversation yet)"
    return f"""A robot works the cake counter at {cfg.shop_name}. A microphone on it hears everything nearby, \
including people who are not talking to it. Decide whether the latest utterance was said TO THE ROBOT.

Yes: a customer greeting it, asking about or choosing cakes, answering its question, reacting to it, saying \
thanks or goodbye, or telling it to stop or wait. Short replies (好, 對, 不要, OK, 嗯) right after the robot \
asked something are yes.
No: people talking to each other (about the robot, the cakes, or anything else), phone calls, staff talking \
among themselves, announcements, and fragments that mean nothing to the robot.
When unsure, answer yes.

Recent conversation:
{history}

The robot {"was still replying (thinking or speaking)" if robot_talking else "was silent"} when this was heard.\
{camera_line(vision)}
Latest utterance (speech recognition, may contain errors): {text}

{_BUSY_ANSWER if robot_talking else "Answer with one word: yes or no."}"""


_BUSY_ANSWER = """The robot answers with a delay, so customers often repeat themselves or chime in while it replies. \
Answer with one word:
new: said to the robot and it must stop and listen: a new question or request, a change or correction, saying \
no or stop, or something its reply has not covered.
same: said to the robot but no need to stop: repeating or rephrasing what they already said, or a backchannel \
while it talks (嗯, 好, 對, OK, 哈哈, 謝謝).
no: not said to the robot.
When unsure between new and same, answer new."""


# ---------- the conversation ----------


class Session:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.clear_after_turn = False
        self.gate: asyncio.Future | None = None
        self.said: deque[tuple[float, str]] = deque(maxlen=4)  # recent customer speech, for spotting repeats
        self.heard: list[str] = []  # said during a reply without interrupting it (see _note)
        self._warmed = False
        self.present = False
        self.greeted_at = 0.0
        self.leave_task: asyncio.Task | None = None
        self.home_result: asyncio.Future | None = None
        self.photo_requests: dict[str, asyncio.Future] = {}
        self.photo_errors: dict[str, str] = {}
        self.attempts: dict[str, int] = {}  # picks per item in this conversation
        self.clients: set[WebSocket] = set()
        self.log: deque[dict] = deque(maxlen=400)
        self.turn_task: asyncio.Task | None = None
        self.state = "idle"
        self.pick: Pick | None = None
        self.pick_task: asyncio.Task | None = None
        self.watch_task: asyncio.Task | None = None  # the camera watch of the running pick
        self.payment: Payment | None = None
        self.payments = 0  # payment requests in this conversation
        self.standby = Standby()
        self.standby_task: asyncio.Task | None = None
        self._standby_lock: asyncio.Lock | None = None
        self._stop_requested: set[str] = set()  # warm jobs we asked to stop (their end is not an error)
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
            "payment": self.payment.model_dump() if self.payment else None,
            "standby": self.standby.model_dump(),
            "config": store.load().model_dump(), "keys": keys_status(),
        }  # fmt: skip

    # -- turns

    async def user_said(
        self,
        text: str,
        metrics: dict | None = None,
        mic: bool = False,
        robot_talking: bool = False,
        vision: VisionSummary | None = None,
    ) -> None:
        """A customer utterance. From the mic, it only counts once the addressee check says it was for us:
        while the robot talks we wait for the check before interrupting; when it is quiet the reply starts
        right away but holds its speech and tools until the check passes (the check is usually done first).
        While a reply is under way, repeats and backchannels don't interrupt it: they go into the history."""
        text = text.strip()
        if not text:
            return
        cfg = store.load()
        if cfg.idle_reset_s and self.messages and time.time() - self.last_activity > cfg.idle_reset_s:
            await self.reset("new customer (idle)")
        stop = bool(_STOP_WORDS.search(text))
        talking = robot_talking or self._busy()
        if mic and talking and not stop and is_repeat(text, self._recent_said()):
            return await self._note(text, "repeat")
        check = None
        if mic and cfg.addressee_model and not stop and not trusted(vision):
            check = asyncio.create_task(self.addressed(cfg, text, talking, vision))
            if talking:
                verdict = await check
                if verdict == "no":
                    return await self._ignore(text)
                if verdict == "same":
                    return await self._note(text, "nothing new")
                check = None
        self.last_activity = time.time()
        self.said.append((time.time(), text))
        await self.interrupt("customer spoke")
        before = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        if check is None:
            await self.emit(
                {
                    "type": "user",
                    "text": text,
                    "metrics": metrics or {},
                    "vision": vision.model_dump() if vision else None,
                }
            )
            self._start_turn()
            return
        gate = asyncio.get_running_loop().create_future()
        self._start_turn(gate)
        task = self.turn_task
        ok = await check != "no"
        if ok:
            await self.emit(
                {
                    "type": "user",
                    "text": text,
                    "metrics": metrics or {},
                    "vision": vision.model_dump() if vision else None,
                }
            )
            gate.set_result(True)
            return
        # Not for us: drop the reply before it said or did anything (unless newer speech already took over).
        if self.turn_task is task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            # Keep what the customer said meanwhile (it was flushed when the reply ended).
            self.messages[before:] = [
                m for m in self.messages[before + 1 :] if str(m["content"]).startswith(HEARD_PREFIX)
            ]
        self.said = deque((s for s in self.said if s[1] != text), maxlen=self.said.maxlen)
        await self._ignore(text)

    def _busy(self) -> bool:
        return bool(self.turn_task and not self.turn_task.done())

    def _recent_said(self) -> list[str]:
        return [t for at, t in self.said if time.time() - at < REPEAT_WINDOW_S]

    async def _note(self, text: str, why: str) -> None:
        """Heard while the robot replied, but no reason to cut it off: keep talking, remember it was said."""
        self.last_activity = time.time()
        self.said.append((time.time(), text))
        self.heard.append(text)
        await self.emit({"type": "noted", "text": text, "message": f"kept talking ({why}): {text}"})
        if not self._busy():
            self._flush_heard()

    def _flush_heard(self) -> None:
        if self.heard:
            said = " ".join(f"「{t}」" for t in self.heard)
            self.heard = []
            self.messages.append({"role": "user", "content": f"{HEARD_PREFIX} {said}"})

    async def addressed(
        self, cfg: KioskConfig, text: str, robot_talking: bool, vision: VisionSummary | None = None
    ) -> Literal["yes", "same", "no"]:
        """yes: for the robot (when it was busy: stop it and listen); same: for it, but nothing new; no: not."""
        recent = []
        for m in self.messages:
            content = m["content"]
            if isinstance(content, str):
                said = content
            else:
                said = " ".join(getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text")
            if said.startswith(HEARD_PREFIX):
                who, said = "Customer (while the robot talked)", said[len(HEARD_PREFIX) :]
            elif m["role"] == "user" and not said.startswith("["):
                who = "Customer"
            else:
                who = "Robot"
            if said.strip():
                recent.append(f"{who}: {clean_speech(_FACE_RE.sub('', said))[:200]}")
        started = time.time()
        try:
            response = await asyncio.wait_for(
                self.anthropic().messages.create(
                    model=cfg.addressee_model,
                    max_tokens=8,
                    thinking={"type": "disabled"},  # plain yes/no answers in ~0.7 s
                    output_config={"effort": "low"},
                    messages=[
                        {
                            "role": "user",
                            "content": addressee_prompt(cfg, recent, text, robot_talking, vision),
                        }
                    ],
                ),
                ADDRESSEE_TIMEOUT_S,
            )
            answer = "".join(b.text for b in response.content if b.type == "text").strip().lower()
            verdict = "no" if answer.startswith("no") else "same" if answer.startswith("same") else "yes"
        except Exception as e:  # noqa: BLE001 — fail open: better to answer side-talk than ignore a customer
            await self.emit({"type": "error", "where": "addressee", "message": f"{type(e).__name__}: {e}"})
            return "yes"
        await self.emit({
            "type": "addressee", "text": text, "to_robot": verdict != "no", "verdict": verdict,
            "duration_s": round(time.time() - started, 2),
        })  # fmt: skip
        return verdict

    async def warm_up(self) -> None:
        """Open the connection the addressee check uses, so the first customer doesn't wait for it."""
        cfg = store.load()
        if self._warmed or not cfg.addressee_model or not os.environ.get("ANTHROPIC_API_KEY"):
            return
        self._warmed = True
        with contextlib.suppress(Exception):
            await self.anthropic().messages.create(
                model=cfg.addressee_model,
                max_tokens=1,
                thinking={"type": "disabled"},
                messages=[{"role": "user", "content": "hi"}],
            )

    async def presence(self, present: bool) -> None:
        """The iPad's camera saw someone walk up to the counter, or nobody has been there for a few seconds."""
        if present == self.present:
            return
        self.present = present
        await self.emit({"type": "presence", "present": present})
        if self.leave_task:
            self.leave_task.cancel()
            self.leave_task = None
        cfg = store.load()
        if present:
            busy = self.turn_task and not self.turn_task.done()
            if cfg.greet_on_arrival and not self.messages and not busy and time.time() - self.greeted_at > 30:
                self.greeted_at = time.time()
                await self.robot_event(
                    "Someone just walked up to the counter. Greet them in one short sentence and invite them "
                    "to choose a cake."
                )
        elif cfg.leave_reset_s:
            self.leave_task = asyncio.create_task(self._reset_after_leaving(cfg.leave_reset_s))

    async def _reset_after_leaving(self, delay: float) -> None:
        await asyncio.sleep(delay)
        busy = (
            (self.turn_task and not self.turn_task.done())
            or (self.pick and self.pick.active)
            or (self.payment and self.payment.state == "pending")
        )
        if not self.present and self.messages and not busy:  # mid-pick they are coming back for the cake
            await self.reset("customer left")

    async def _ignore(self, text: str) -> None:
        await self.emit({"type": "ignored", "message": f"not said to me: {text}"})

    async def robot_event(self, text: str) -> None:
        """Our own systems speaking (e.g. a pick finished): wait for the current reply, then react."""
        if self.turn_task and not self.turn_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.turn_task
        self.messages.append({"role": "user", "content": f"[Robot event] {text}"})
        await self.emit({"type": "robot_event", "text": text})
        self._start_turn()

    def _start_turn(self, gate: asyncio.Future | None = None) -> None:
        self.gate = gate
        self.turn_task = asyncio.create_task(self._turn(uuid.uuid4().hex[:8]))

    async def _wait_gate(self) -> None:
        """Hold speech and tools until the addressee check has passed (see user_said)."""
        if self.gate is not None:
            await self.gate

    async def interrupt(self, reason: str) -> None:
        task = self.turn_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await self.emit({"type": "interrupted", "reason": reason})

    async def reset(self, reason: str = "reset", keep_audio: bool = False) -> None:
        await self.interrupt(reason)
        self.messages = []
        self.said.clear()
        self.heard = []
        self.attempts = {}
        self.payments = 0
        self.clear_after_turn = False
        await self.emit({"type": "reset", "reason": reason, "keep_audio": keep_audio})
        await self.cancel_payment("conversation reset")

    async def _turn(self, turn: str) -> None:
        speaker = Speaker(self, turn)
        splitter = SentenceSplitter()
        partial: list[str] = []
        pending: asyncio.Future | None = None
        t0 = time.time()
        timing: dict = {}
        self.clear_after_turn = False
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
                await self._wait_gate()
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
            if self.clear_after_turn:
                # Claude asked to forget the conversation; its goodbye has been sent, let it finish playing.
                self.turn_task = None
                await self.reset("conversation cleared by the assistant", keep_audio=True)
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
            self._flush_heard()
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
        if sentences or faces:
            await self._wait_gate()
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

    async def tool_look_at_customer(self) -> list[dict]:
        if not store.load().customer_photos:
            raise ToolError("Staff turned off photos of customers; go by what they say.")
        jpeg = await self.customer_photo()
        return [
            {"type": "text", "text": "iPad front camera (facing the customer):"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpeg}},
        ]

    async def customer_photo(self) -> str:
        """Ask the iPad(s) for a front-camera JPEG (base64); the first answer wins."""
        request_id = uuid.uuid4().hex[:8]
        future = asyncio.get_running_loop().create_future()
        self.photo_requests[request_id] = future
        try:
            await self.emit({"type": "photo_request", "id": request_id}, keep=False)
            try:
                return await asyncio.wait_for(future, PHOTO_TIMEOUT_S)
            except TimeoutError:
                why = (
                    self.photo_errors.get(request_id)
                    or "camera off, not allowed, or the kiosk page not started"
                )
                raise ToolError(f"The iPad's front camera gave no photo ({why}).") from None
        finally:
            self.photo_requests.pop(request_id, None)
            self.photo_errors.pop(request_id, None)

    def photo_received(self, request_id: str, image: str | None, error: str | None) -> None:
        future = self.photo_requests.get(request_id)
        if future is None or future.done():
            return
        if image and len(image) <= MAX_PHOTO_B64:
            future.set_result(image)
        elif error:  # this iPad can't take one; another client may still answer
            self.photo_errors[request_id] = error

    async def tool_check_inventory(self) -> str:
        cfg = store.load()
        rows = [{
            "id": i.id, "name": i.name, "price": i.price, "on_display_on_table": i.on_table,
            "in_the_back": i.back_stock, "sold_today": i.sold,
            "total_available": int(i.on_table) + i.back_stock,
            "robot_can_pick": bool(i.checkpoint) or cfg.simulate_picks,
        } for i in cfg.items]  # fmt: skip
        if not cfg.armed:
            note = " The robot arm is currently NOT armed by staff, so picks will be refused."
        elif self.standby.state == "loading":
            note = " The arm is still warming up; a pick started now begins as soon as it is ready."
        else:
            note = ""
        return json.dumps({"items": rows}, ensure_ascii=False) + note

    async def tool_pick_item(self, item_id: str, retry: bool = False) -> str:
        cfg = store.load()
        item = next((i for i in cfg.items if i.id == item_id), None)
        if item is None:
            raise ToolError(f"No item with id {item_id}. Valid ids: {', '.join(i.id for i in cfg.items)}.")
        if self.pick and self.pick.active:
            raise ToolError(f"A pick of the {self.pick.item_name} is already running.")
        if self.attempts.get(item.id, 0) >= MAX_ATTEMPTS:
            raise ToolError(
                f"The {item.name} has already been tried {MAX_ATTEMPTS} times in this conversation. "
                "Apologise and ask staff to help (notify_owner)."
            )
        last = self.pick
        retry = (
            retry
            and not item.on_table
            and cfg.armed
            and last is not None
            and last.item_id == item.id
            and last.state == "succeeded"
            and not last.retried
            and time.time() - (last.ended_at or 0) < RETRY_WINDOW_S
        )
        if retry:
            # The last pick of this item "finished" but missed: it is still on the table, so undo the sale.
            def unsell(c: KioskConfig) -> None:
                for i in c.items:
                    if i.id == item.id:
                        i.on_table, i.sold = True, max(0, i.sold - 1)

            cfg = store.update(unsell)
            item = next(i for i in cfg.items if i.id == item_id)
            last.retried = True
            await self.emit({"type": "config", "config": cfg.model_dump()}, keep=False)
            await self.notify(f"Retrying the {item.name}: the last pick missed (ignore the restock note).")
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
            await self.ensure_standby(cfg)
            if self.standby.running and item.id in self.standby.policies:
                # Warm path: the policy is already loaded; one command starts the episode.
                try:
                    await asyncio.to_thread(
                        _hooks["gpu_call"], "POST", f"/jobs/{self.standby.job_id}/command",
                        {"command": "start", "policy": item.id},
                    )  # fmt: skip
                except HTTPException as e:
                    raise ToolError(f"The robot could not start: {e.detail}") from None
                self.pick = Pick(job_id=self.standby.job_id, item_id=item.id, item_name=item.name,
                                 started_at=time.time(), time_limit_s=item.episode_time_s, mode="standby")  # fmt: skip
            else:
                # Cold path (no warm job, e.g. the GPU server runs older code): one job for this pick.
                try:
                    job = await asyncio.to_thread(_hooks["start_job"], pick_request(item))
                except HTTPException as e:
                    raise ToolError(f"The robot could not start: {e.detail}") from None
                self.pick = Pick(job_id=job["id"], item_id=item.id, item_name=item.name, started_at=time.time(),
                                 time_limit_s=item.episode_time_s)  # fmt: skip
        self.attempts[item.id] = self.attempts.get(item.id, 0) + 1
        if cfg.simulate_picks:
            self.pick.mode = "sim"
        await self.emit({"type": "pick", "pick": self.pick.summary()})
        if self.pick.mode != "standby":  # the standby watcher follows warm picks
            self.pick_task = asyncio.create_task(self._watch_pick(self.pick))
        if self.pick.mode != "sim" and cfg.pick_watch_model and _hooks.get("camera_feed"):
            self.watch_task = asyncio.create_task(self._watch_camera(self.pick))
        return (
            f"Started picking the {item.name} (up to {self.pick.time_limit_s} s). "
            "You will get a [Robot event] when it ends."
        )

    async def tool_reset_arm(self) -> str:
        cfg = store.load()
        if self.pick and self.pick.active:
            raise ToolError(
                f"The {self.pick.item_name} pick is still running; wait for it or call stop_robot."
            )
        if not cfg.armed:
            raise ToolError("The robot arm is not armed by staff, so it cannot move right now.")
        if cfg.simulate_picks:
            await asyncio.sleep(SIM_HOME_S)
            return "The arms are back in their starting pose (simulated). You can pick again."
        await self.ensure_standby(cfg)
        if not self.standby.running or self.standby.state != "ready":
            raise ToolError(
                f"The robot isn't ready to move its arms home (warm job: {self.standby.state}"
                f"{', ' + self.standby.detail if self.standby.detail else ''}). Ask staff to check it."
            )
        self.home_result = asyncio.get_running_loop().create_future()
        try:
            try:
                await asyncio.to_thread(
                    _hooks["gpu_call"], "POST", f"/jobs/{self.standby.job_id}/command", {"command": "home"}
                )
            except HTTPException as e:
                raise ToolError(
                    f"The robot could not move home: {e.detail} (the GPU server may need updating)."
                ) from None
            await self._set_standby(state="busy", detail="moving the arms home")
            try:
                ok, reason = await asyncio.wait_for(asyncio.shield(self.home_result), HOME_WAIT_S)
            except TimeoutError:
                raise ToolError(
                    "The robot did not report back about moving home. Ask staff to check it."
                ) from None
        finally:
            self.home_result = None
        if not ok:
            await self.notify(f"The arms could not get back to their starting pose: {reason}.", urgent=True)
            raise ToolError(f"The arms could not get home ({reason}). Staff have been notified.")
        return "The arms are back in their starting pose. You can pick again."

    async def tool_check_pick_status(self) -> str:
        if not self.pick:
            return "No pick has run in this conversation."
        return json.dumps(self.pick.summary(), ensure_ascii=False)

    async def tool_end_pick(self, outcome: Literal["in_box", "missed"]) -> str:
        pick = self.pick
        if not pick or not pick.active:
            return "No pick is running."
        if pick.state == "stopping":
            return "The pick is already ending."
        await self._end_early(pick, outcome, "Claude ended it")
        return f"Ending the {pick.item_name} pick now. You will get a [Robot event] when it has ended."

    async def tool_stop_robot(self) -> str:
        pick = self.pick
        if not pick or not pick.active:
            return "Nothing is running; the arm is idle."
        await self.stop_pick()
        return f"Stop sent for the {pick.item_name} pick."

    async def tool_clear_context(self) -> str:
        if self.pick and self.pick.active:
            raise ToolError(
                f"The {self.pick.item_name} pick is still running; keep the conversation until it finishes."
            )
        if self.payment and self.payment.state == "pending":
            raise ToolError("A payment is still waiting on the terminal; wait for it to finish.")
        self.clear_after_turn = True
        return "The conversation will be forgotten after this reply. Say goodbye now; no more tool calls."

    async def tool_notify_owner(self, message: str, urgent: bool = False) -> str:
        await self.notify(message, urgent, source="robot")
        return "The owner has been notified."

    async def tool_request_payment(self, item_id: str) -> str:
        cfg = store.load()
        item = next((i for i in cfg.items if i.id == item_id), None)
        if item is None:
            raise ToolError(f"No item with id {item_id}. Valid ids: {', '.join(i.id for i in cfg.items)}.")
        if self.payment and self.payment.state == "pending":
            raise ToolError(f"A payment of {self.payment.amount} is already waiting on the terminal.")
        pick = self.pick
        if not pick or pick.item_id != item.id or pick.state != "succeeded" or pick.retried:
            raise ToolError(
                f"The robot has not put a {item.name} in the box yet, so there is nothing to pay for."
            )
        if self.payment and self.payment.state == "paid" and self.payment.created_at >= pick.started_at:
            raise ToolError(f"The {item.name} is already paid for.")
        if self.payments >= MAX_PAYMENTS:
            raise ToolError(
                "The card was tried too many times. Apologise and ask staff to help (notify_owner)."
            )
        self.payments += 1
        self.payment = Payment(
            id=uuid.uuid4().hex[:8], item_id=item.id, item_name=item.name,
            amount=item.price or "?", created_at=time.time(),
        )  # fmt: skip
        await self.emit({"type": "payment", "payment": self.payment.model_dump()})
        return (
            f"The card terminal is showing {self.payment.amount} for the {item.name}. Ask the customer to tap "
            "their card; you will get a [Robot event] when it is paid or declined."
        )

    # -- payment lifecycle (the fake terminal at /kiosk/terminal answers)

    async def payment_result(self, payment_id: str, ok: bool, reason: str = "") -> None:
        p = self.payment
        if not p or p.id != payment_id or p.state != "pending":
            return  # stale button press (another terminal answered, or the conversation was reset)
        p.state, p.reason, p.ended_at = ("paid" if ok else "declined"), reason[:200], time.time()
        await self.emit({"type": "payment", "payment": p.model_dump()})
        if ok:
            text = f"The card payment of {p.amount} for the {p.item_name} was approved."
        else:
            text = (
                f"The card payment of {p.amount} for the {p.item_name} was declined"
                f"{' (' + p.reason + ')' if p.reason else ''}."
            )
        await self.robot_event(text)

    async def cancel_payment(self, reason: str) -> None:
        p = self.payment
        if p and p.state == "pending":
            p.state, p.reason, p.ended_at = "cancelled", reason, time.time()
            await self.emit({"type": "payment", "payment": p.model_dump()})

    # -- pick lifecycle

    async def notify(self, message: str, urgent: bool = False, source: str = "system") -> None:
        note = {"at": time.time(), "message": message, "urgent": urgent, "source": source}
        self.notifications.appendleft(note)
        await self.emit({"type": "notification", **note})

    async def stop_pick(self) -> None:
        """STOP: end the pick and unload the warm job (the arm holds), and disarm until staff re-arm."""
        if store.load().armed:
            cfg = store.update(lambda c: setattr(c, "armed", False))
            await self.emit({"type": "config", "config": cfg.model_dump()}, keep=False)
            await self.notify("Robot stopped and disarmed. Re-arm it in the debug drawer.", urgent=True)
        pick = self.pick
        if pick and pick.active:
            pick.state = "stopping"
            if pick.mode == "job":
                try:
                    await asyncio.to_thread(_hooks["gpu_call"], "POST", f"/jobs/{pick.job_id}/stop")
                except HTTPException as e:
                    pick.detail = f"Stop failed: {e.detail}"
            await self.emit({"type": "pick", "pick": pick.summary()})
        await self.stop_standby()

    # -- the warm job

    def _lock(self) -> asyncio.Lock:
        if self._standby_lock is None:
            self._standby_lock = asyncio.Lock()
        return self._standby_lock

    async def _set_standby(self, **changes) -> None:
        for key, value in changes.items():
            setattr(self.standby, key, value)
        await self.emit({"type": "standby", "standby": self.standby.model_dump()}, keep=False)

    async def config_changed(self, cfg: KioskConfig) -> None:
        """Arm loads the policies (warm), disarm unloads them; changed checkpoints reload while idle."""
        try:
            if cfg.armed and not cfg.simulate_picks:
                await self.ensure_standby(cfg)
            elif self.standby.running or self.standby.state == "error":
                await self.stop_standby()
        except Exception as e:  # noqa: BLE001
            await self._set_standby(state="error", detail=f"{type(e).__name__}: {e}")

    async def ensure_standby(self, cfg: KioskConfig) -> None:
        policies = standby_policies(cfg)
        if not policies or cfg.simulate_picks or "gpu_call" not in _hooks:
            return
        signature = standby_signature(policies)
        async with self._lock():
            if self.standby.running and self.standby.signature == signature:
                return
            if self.standby.running:
                if self.pick and self.pick.active:
                    return  # reload after this pick
                await self._stop_standby_job(wait=True)
            gpu = _hooks["gpu_call"]
            # After a panel restart the warm job may still be running on the GPU server: adopt it.
            try:
                jobs = (await asyncio.to_thread(gpu, "GET", "/status"))["jobs"]
            except HTTPException as e:
                await self._set_standby(state="error", detail=str(e.detail))
                return
            for job in jobs:
                if job["name"] == STANDBY_JOB_NAME and job["state"] in ("running", "stopping"):
                    record = await asyncio.to_thread(gpu, "GET", f"/jobs/{job['id']}")
                    theirs = standby_signature(record["request"].get("standby_policies") or [])
                    if job["state"] == "running" and theirs == signature:
                        await self._adopt(job["id"], policies, signature, record["started_at"])
                        return
                    await asyncio.to_thread(gpu, "POST", f"/jobs/{job['id']}/stop")
                    await self._wait_job_end(job["id"])
            try:
                job = await asyncio.to_thread(_hooks["start_job"], standby_request(policies))
            except HTTPException as e:
                detail = e.detail if isinstance(e.detail, str) else json.dumps(e.detail)[:300]
                await self._set_standby(state="error", detail=f"Could not warm up: {detail}", job_id="")
                return
            await self._adopt(job["id"], policies, signature, time.time())

    async def _adopt(self, job_id: str, policies: list[dict], signature: str, started_at: float) -> None:
        self.standby = Standby(job_id=job_id, state="loading", policies=[p["name"] for p in policies],
                               signature=signature, started_at=started_at)  # fmt: skip
        await self._set_standby()
        self.standby_task = asyncio.create_task(self._watch_standby(job_id))

    async def stop_standby(self) -> None:
        async with self._lock():
            await self._stop_standby_job(wait=False)

    async def _stop_standby_job(self, wait: bool) -> None:
        job_id = self.standby.job_id
        if not job_id or not self.standby.running:
            await self._set_standby(state="off", detail="")
            return
        self._stop_requested.add(job_id)
        await self._set_standby(state="stopping")
        try:
            await asyncio.to_thread(_hooks["gpu_call"], "POST", f"/jobs/{job_id}/stop")
        except HTTPException as e:
            await self._set_standby(state="error", detail=f"Stop failed: {e.detail}")
            return
        if wait:
            await self._wait_job_end(job_id)

    async def _wait_job_end(self, job_id: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = await asyncio.to_thread(_hooks["gpu_call"], "GET", f"/jobs/{job_id}")
                if record["state"] not in ("running", "stopping"):
                    return
            except HTTPException:
                pass
            await asyncio.sleep(PICK_POLL_S)

    async def _watch_standby(self, job_id: str) -> None:
        """Follow the warm job's [EVAL:event] lines: loading -> ready -> busy (a pick) -> ready ..."""
        seen: set = set()
        failures = 0
        while self.standby.job_id == job_id:
            try:
                record = await asyncio.to_thread(_hooks["gpu_call"], "GET", f"/jobs/{job_id}")
                failures = 0
            except HTTPException as e:
                failures += 1
                if failures >= 20:
                    await self._set_standby(state="error", detail=str(e.detail))
                    return
                await asyncio.sleep(PICK_POLL_S)
                continue
            pick = self.pick if self.pick and self.pick.active and self.pick.job_id == job_id else None
            for ev in eval_events(record.get("lines") or []):
                key = (ev.get("event"), ev.get("episode"), ev.get("at"))
                if key in seen:
                    continue
                seen.add(key)
                kind = ev.get("event")
                if job_id in self._stop_requested and kind != "episode_end":
                    continue
                if kind == "waiting":
                    await self._set_standby(
                        state="ready", detail="", ready_at=self.standby.ready_at or ev.get("at")
                    )
                elif kind == "episode_start":
                    await self._set_standby(state="busy")
                    if pick and pick.state == "starting" and ev.get("policy") == pick.item_id:
                        pick.state, pick.episode_started_at = "running", ev.get("at", time.time())
                        await self.emit({"type": "pick", "pick": pick.summary()}, keep=False)
                elif kind == "homed":
                    await self._set_standby(state="ready", detail="")
                    if self.home_result and not self.home_result.done():
                        self.home_result.set_result((bool(ev.get("ok")), ev.get("reason") or ""))
                elif kind == "episode_end" and pick and pick.state in ("running", "stopping"):
                    pick.state = "succeeded" if pick.state == "running" else "stopped"
                    await self._end_pick(pick)
                    pick = None
            if pick and pick.state == "starting" and time.time() - pick.started_at > STANDBY_START_TIMEOUT_S:
                pick.state, pick.detail = "error", "the warm job never started the episode"
                await self._end_pick(pick)
                pick = None
            if record["state"] not in ("running", "stopping"):
                ended_by_us = job_id in self._stop_requested
                if self.home_result and not self.home_result.done():
                    self.home_result.set_result((False, "the robot job ended"))
                tail = " ".join((record.get("lines") or [])[-3:])[-300:]
                await self._set_standby(
                    state="off" if ended_by_us else "error",
                    detail="" if ended_by_us else f"Warm job ended ({record['state']}): {tail}",
                )
                if pick:
                    pick.state = "stopped" if record["state"] == "stopped" else "failed"
                    pick.detail = pick.detail or record["state"]
                    await self._end_pick(pick)
                return
            if pick:
                await self.emit({"type": "pick", "pick": pick.summary()}, keep=False)
            await asyncio.sleep(PICK_POLL_S)

    async def _end_pick(self, pick: Pick) -> None:
        pick.ended_at = time.time()
        await self.emit({"type": "pick", "pick": pick.summary()})
        # React in the background: the watcher keeps following the warm job meanwhile.
        self.pick_task = asyncio.create_task(self._pick_finished(pick))

    async def end_episode(self) -> None:
        """Staff button: the cake is in the box, end the episode now instead of waiting for the time limit."""
        pick = self.pick
        if pick and pick.active:
            await self._end_early(pick, "in_box", "staff ended it")

    async def _end_early(self, pick: Pick, verdict: Literal["in_box", "missed"], why: str) -> None:
        """End the episode before its time limit (the arm stays armed); _pick_finished reacts to the verdict."""
        if not pick.active or pick.verdict:
            return
        pick.verdict = verdict
        pick.detail = why
        if pick.mode == "sim":
            pick.time_limit_s = 0
        else:
            try:
                await asyncio.to_thread(
                    _hooks["gpu_call"], "POST", f"/jobs/{pick.job_id}/command", {"command": "end"}
                )
            except HTTPException as e:
                pick.verdict, pick.detail = None, f"could not end early: {e.detail}"
                await self.emit({"type": "error", "where": "end_pick", "message": pick.detail})
                raise ToolError(f"The robot did not take the end command ({e.detail}).") from None
        await self.emit({"type": "pick", "pick": pick.summary()})

    async def _watch_camera(self, pick: Pick) -> None:
        """Look at the table every few seconds while the episode runs: end it once the item is in the box, or
        after several "failing" looks in a row, instead of letting a done or hopeless pick run to its limit."""
        cfg = store.load()
        item = next((i for i in cfg.items if i.id == pick.item_id), None)
        running_since: float | None = None
        last_look = 0.0
        misses = 0
        history: list[str] = []
        while pick.active and not pick.verdict:
            await asyncio.sleep(min(1.0, PICK_POLL_S))
            if pick.state != "running":
                continue
            now = time.monotonic()
            if running_since is None:
                running_since = now
            if now - running_since < cfg.pick_watch_after_s or now - last_look < cfg.pick_watch_every_s:
                continue
            last_look = now
            started = time.time()
            try:
                frames = await self.tool_look_at_table("both")
            except ToolError as e:
                await self.emit(
                    {"type": "error", "where": "pick_watch", "message": f"{e} (watch off for this pick)"}
                )
                return
            elapsed = round(now - running_since)
            verdict, reason = await self._judge_pick(cfg, pick, item, frames, history, elapsed)
            if not pick.active or pick.verdict:
                return
            history.append(f"{elapsed}s: {verdict} ({reason})" if reason else f"{elapsed}s: {verdict}")
            misses = misses + 1 if verdict == "failing" else 0
            pick.last_check = f"{verdict}: {reason}" if reason else verdict
            await self.emit({
                "type": "pick_check", "item": pick.item_name, "verdict": verdict, "reason": reason,
                "misses": misses, "elapsed_s": elapsed, "duration_s": round(time.time() - started, 2),
            })  # fmt: skip
            with contextlib.suppress(ToolError):
                if verdict == "in_box":
                    await self._end_early(pick, "in_box", f"camera saw it in the box: {reason}")
                elif misses >= cfg.pick_watch_misses:
                    await self._end_early(pick, "missed", f"camera saw it fail: {reason}")

    async def _judge_pick(
        self,
        cfg: KioskConfig,
        pick: Pick,
        item: Item | None,
        frames: list[dict],
        history: list[str],
        elapsed: int,
    ) -> tuple[Literal["in_box", "trying", "failing"], str]:
        """One look by the fast vision model. Any failure counts as "trying" (the pick just runs on)."""
        where = f", normally {item.position}" if item and item.position else ""
        task = item.task if item and item.task else f"Pick the {pick.item_name} and put it in the box"
        earlier = "\n".join(history[-4:]) or "(none yet)"
        prompt = f"""A two-armed robot is running a learned policy to pick up the {pick.item_name}{where} and put it \
into the open cardboard takeaway box in the middle of the table (task: "{task}"). These are live photos from its \
cameras, {elapsed} s into the pick (time limit {pick.time_limit_s} s).

Earlier looks:
{earlier}

Answer with one word, a colon, and a reason of at most 12 words:
in_box: the {pick.item_name} rests inside the takeaway box and the grippers have let go of it.
trying: anything that may still work: reaching, grasping, holding or carrying it, adjusting, regrasping after a slip.
failing: it cannot work without starting over: the item fell off the table, tipped over or was knocked out of \
reach, it was dropped outside the box, or the arms are stuck, idle, or keep closing on nothing away from it.
When unsure, answer trying."""
        try:
            response = await asyncio.wait_for(
                self.anthropic().messages.create(
                    model=cfg.pick_watch_model,
                    max_tokens=40,
                    thinking={"type": "disabled"},
                    output_config={"effort": "low"},
                    messages=[{"role": "user", "content": [*frames, {"type": "text", "text": prompt}]}],
                ),
                PICK_WATCH_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001
            await self.emit({"type": "error", "where": "pick_watch", "message": f"{type(e).__name__}: {e}"})
            return "trying", ""
        answer = "".join(b.text for b in response.content if b.type == "text").strip()
        word, _, reason = answer.partition(":")
        word = word.strip().lower()
        verdict = word if word in ("in_box", "failing") else "trying"
        return verdict, clean_speech(reason).strip()[:120]

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
        if pick.state == "succeeded" and pick.verdict == "missed":
            # Ended early because it failed: nothing was sold, the item is still on the table.
            left = MAX_ATTEMPTS - self.attempts.get(pick.item_id, 0)
            text = (
                f"The pick of the {pick.item_name} was ended after {took} s because it failed ({pick.detail}). "
                "Nothing was sold; the inventory still counts it on the table. "
                + (
                    "Tell the customer you will try again, call reset_arm, then pick_item the same item (no "
                    "retry flag needed)."
                    if left > 0
                    else "No tries are left: apologise, notify_owner, and say staff will help."
                )
            )
        elif pick.state == "succeeded":
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
            early = f" ({pick.detail})" if pick.verdict == "in_box" else ""
            text = (
                f"The pick of the {pick.item_name} finished after {took} s{early} and the inventory counts it as "
                "sold. look_at_table to check it is in the takeaway box; if it is, request_payment; if it missed, "
                "reset_arm and pick_item it again with retry=true (that undoes the sale)."
            )
        elif pick.state == "stopped":
            text = f"The pick of the {pick.item_name} was stopped after {took} s. The arm is holding still."
        else:
            text = (
                f"The pick of the {pick.item_name} ended with a problem ({pick.state}"
                f"{': ' + pick.detail if pick.detail else ''}). If the robot is still ready, you may reset_arm "
                "and try once more; otherwise tell the customer staff will help."
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
    # Arming warms the robot up (loads every item's policy); disarming unloads it.
    asyncio.create_task(session.config_changed(saved))
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
    asyncio.create_task(session.warm_up())
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "user_text":
                asyncio.create_task(
                    session.user_said(
                        str(msg.get("text", ""))[:2000],
                        msg.get("metrics"),
                        mic=bool(msg.get("mic")),
                        robot_talking=bool(msg.get("robot_talking")),
                        vision=_vision(msg.get("vision")),
                    )
                )
            elif kind == "photo":
                session.photo_received(str(msg.get("id", "")), msg.get("image"), msg.get("error"))
            elif kind == "presence":
                asyncio.create_task(session.presence(bool(msg.get("present"))))
            elif kind == "interrupt":
                asyncio.create_task(session.interrupt("barge-in"))
            elif kind == "reset":
                asyncio.create_task(session.reset())
            elif kind == "payment_result":
                asyncio.create_task(
                    session.payment_result(
                        str(msg.get("id", "")), bool(msg.get("ok")), str(msg.get("reason") or "")
                    )
                )
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


def _vision(data: Any) -> VisionSummary | None:
    try:
        return VisionSummary.model_validate(data) if data else None
    except ValueError:
        return None


async def _safe(coro) -> None:
    try:
        await coro
    except HTTPException as e:
        await session.emit({"type": "error", "where": "robot", "message": str(e.detail)})
