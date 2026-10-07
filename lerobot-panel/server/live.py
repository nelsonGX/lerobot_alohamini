"""One WebSocket (/api/ws) that pushes live panel state, replacing the browser's polling.

Clients subscribe to topics; each topic is computed at most once per interval no matter how many
browsers watch it, and only sent to a client when its value changed.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

TICK_S = 0.1


@dataclass
class _Topic:
    fn: Callable[[], Any]
    interval: float
    at: float = 0.0
    seq: int = 0
    raw: str = ""
    payload: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Hub:
    def __init__(self) -> None:
        self._topics: dict[str, _Topic] = {}
        self._resolvers: list[Callable[[str], _Topic | None]] = []
        self._per_client: dict[str, tuple[float, Callable[[], Callable[[], Any]]]] = {}

    def topic(self, name: str, interval: float, fn: Callable[[], Any]) -> None:
        self._topics[name] = _Topic(fn, interval)

    def per_client(self, name: str, interval: float, make: Callable[[], Callable[[], Any]]) -> None:
        """A topic whose content depends on what this client already has: `make()` builds one stateful
        function per connection, and it returns None when there is nothing new to send."""
        self._per_client[name] = (interval, make)

    def family(self, prefix: str, interval: float, fn: Callable[[str], Any], valid: Callable[[str], bool]) -> None:
        """Topics named `prefix:arg`, created on first subscription."""

        def resolve(name: str) -> _Topic | None:
            head, _, arg = name.partition(":")
            if head != prefix or not valid(arg):
                return None
            return self._topics.setdefault(name, _Topic(lambda: fn(arg), interval))

        self._resolvers.append(resolve)

    def _find(self, name: str) -> _Topic | None:
        if name in self._topics:
            return self._topics[name]
        for resolve in self._resolvers:
            if t := resolve(name):
                return t
        return None

    async def _fresh(self, t: _Topic) -> _Topic:
        if time.monotonic() - t.at < t.interval:
            return t
        async with t.lock:
            if time.monotonic() - t.at >= t.interval:
                try:
                    data = await asyncio.to_thread(t.fn)
                    payload = {"data": data}
                except Exception as e:  # noqa: BLE001 - surfaced to the UI instead of killing the socket
                    payload = {"error": str(e) or type(e).__name__}
                raw = json.dumps(payload, default=str, sort_keys=True)
                if raw != t.raw:
                    t.raw, t.payload, t.seq = raw, payload, t.seq + 1
                t.at = time.monotonic()
        return t

    async def serve(self, sock: WebSocket) -> None:
        await sock.accept()
        subs: dict[str, int] = {}  # topic -> last seq sent
        mine: dict[str, tuple[Callable[[], Any], float]] = {}  # per-client topic -> (fn, next due)
        stop = asyncio.Event()

        async def reader() -> None:
            try:
                while True:
                    msg = json.loads(await sock.receive_text())
                    if name := msg.get("sub"):
                        if name in self._per_client:
                            mine[name] = (self._per_client[name][1](), 0.0)
                        elif self._find(name) is not None:
                            subs.setdefault(name, 0)
                    elif name := msg.get("refresh"):
                        if t := self._find(name):
                            t.at = 0.0
                        if name in mine:
                            mine[name] = (mine[name][0], 0.0)
                    elif name := msg.get("unsub"):
                        subs.pop(name, None)
                        mine.pop(name, None)
            except (WebSocketDisconnect, RuntimeError, ValueError):
                pass
            finally:
                stop.set()

        task = asyncio.create_task(reader())
        try:
            while not stop.is_set():
                for name in list(subs):
                    t = self._find(name)
                    if t is None:
                        continue
                    await self._fresh(t)
                    if t.seq != subs.get(name):
                        subs[name] = t.seq
                        await sock.send_text(json.dumps({"topic": name, **t.payload}, default=str))
                for name, (fn, due) in list(mine.items()):
                    if time.monotonic() < due:
                        continue
                    data = await asyncio.to_thread(fn)
                    if name in mine:
                        mine[name] = (fn, time.monotonic() + self._per_client[name][0])
                    if data is not None:
                        await sock.send_text(json.dumps({"topic": name, "data": data}, default=str))
                await asyncio.sleep(TICK_S)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            task.cancel()
