"""Live camera preview: relay the host's camera-only ZMQ stream (port 5557) to the browser as MJPEG.

The host only encodes JPEGs while someone is subscribed, so this connects when the first viewer
arrives and disconnects shortly after the last one leaves. It never touches the control/recording
path on port 5556.
"""

from __future__ import annotations

import json
import threading
import time

IDLE_DISCONNECT_S = 5.0
STALE_S = 3.0


class CameraFeed:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames: dict[str, tuple[bytes, float, int]] = {}  # name -> (jpeg, received_at, seq)
        self._endpoint: str | None = None
        self._thread: threading.Thread | None = None
        self._last_request = 0.0
        self._error: str | None = None

    def touch(self, host: str, port: int) -> None:
        """Note that a viewer wants frames from host:port; (re)start the receiver if needed."""
        endpoint = f"tcp://{host}:{port}"
        with self._lock:
            self._last_request = time.monotonic()
            if self._thread and self._thread.is_alive() and self._endpoint == endpoint:
                return
            self._endpoint = endpoint
            self._frames = {}
            self._error = None
            self._thread = threading.Thread(target=self._run, args=(endpoint,), name="camera-feed", daemon=True)
            self._thread.start()

    def _active(self, endpoint: str) -> bool:
        with self._lock:
            return self._endpoint == endpoint and time.monotonic() - self._last_request < IDLE_DISCONNECT_S

    def _run(self, endpoint: str) -> None:
        try:
            import zmq
        except ImportError:
            self._error = "pyzmq is not installed for the panel (restart it with ./panel)."
            return
        ctx = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVHWM, 8)
        sock.setsockopt(zmq.SUBSCRIBE, b"camera/")
        sock.connect(endpoint)
        try:
            while self._active(endpoint):
                if not sock.poll(500):
                    continue
                try:
                    _topic, meta, jpeg = sock.recv_multipart(zmq.NOBLOCK)
                    info = json.loads(meta)
                except (zmq.Again, ValueError):
                    continue
                with self._lock:
                    if self._endpoint == endpoint:
                        self._frames[info["camera_name"]] = (jpeg, time.monotonic(), int(info.get("sequence", 0)))
        finally:
            sock.close(linger=0)
            ctx.term()

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            cams = [{"name": n, "age_s": round(now - t, 2), "live": now - t < STALE_S} for n, (_j, t, _s) in sorted(self._frames.items())]
            return {"cameras": cams, "error": self._error}

    def frame(self, name: str) -> tuple[bytes, int] | None:
        with self._lock:
            item = self._frames.get(name)
        if item is None or time.monotonic() - item[1] > STALE_S:
            return None
        return item[0], item[2]

    def idle(self) -> bool:
        return time.monotonic() - self._last_request > IDLE_DISCONNECT_S
