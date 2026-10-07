"""Console programs the panel runs on behalf of teammates, so nobody needs a terminal.

- host:      the robot host on the Jetson (through the panel agent, ./agent there), what ./host does
- teleop:    teleoperation without recording, what ./client does
- calibrate: leader arm calibration here (./lcalibrate) or follower calibration on the Jetson (./fcalibrate)

Each runs in a pseudo-terminal; its screen is kept in a TermBuffer and interactive
prompts ("press ENTER") are detected so the UI can show buttons for them.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import signal
import socket
import struct
import subprocess
import termios
import threading
import time

import requests
from websockets.sync.client import connect as ws_connect

from config import DATA_DIR, PYTHON, REPO_ROOT, Settings
from term import TermBuffer

AGENT_TOKEN_FILE = DATA_DIR / "agent_token"
HOST_MODULE = "lerobot.robots.alohamini.alohamini_host"

# Prompts printed by lerobot's calibration code -> what the UI should offer.
PROMPTS = [
    (re.compile(r"type 'c' and press ENTER", re.I), "choice",
     "A calibration file already exists. Keep it, or recalibrate?"),
    (re.compile(r"middle of its range of motion", re.I), "enter",
     "Move the arm named above so every joint is roughly in the middle of its range, then press Continue."),
    (re.compile(r"Press ENTER to stop", re.I), "enter",
     "Slowly move every joint through its full range (watch MIN/MAX grow in the table), then press Done."),
    (re.compile(r"press enter", re.I), "enter", ""),
]  # fmt: skip


class ProcBusy(RuntimeError):
    pass


class ConsoleProcess:
    def __init__(self, name: str, title: str, cmd: list[str], *, env: dict | None = None) -> None:
        self.name = name
        self.title = title
        self.cmd = cmd
        self.screen = TermBuffer()
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        self.stopping = False
        self._input_mark = -1  # prompts at or above this line were already answered
        self._lock = threading.Lock()

        full_env = {**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1", "TERM": "xterm"}
        # No desktop keyboard hooks: keys come from the panel through the PTY.
        full_env.pop("DISPLAY", None)
        full_env.pop("WAYLAND_DISPLAY", None)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=slave, stdout=slave, stderr=slave, cwd=REPO_ROOT, env=full_env,
                start_new_session=True, close_fds=True,
            )  # fmt: skip
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self._master = master
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._wait, daemon=True).start()

    @property
    def running(self) -> bool:
        return self.exit_code is None

    @property
    def version(self) -> int:
        return self.screen.version

    def _read(self) -> None:
        while True:
            try:
                data = os.read(self._master, 4096)
            except OSError:
                break
            if not data:
                break
            with self._lock:
                self.screen.feed(data)
        os.close(self._master)

    def _wait(self) -> None:
        code = self._proc.wait()
        time.sleep(0.2)  # let the reader drain the last output
        with self._lock:
            self.exit_code = code
            self.ended_at = time.time()
            if self.stopping:
                self.screen.append_note("[panel] stopped")
            elif code != 0:
                self.screen.append_note(f"[panel] exited with code {code}")

    def write(self, text: str) -> None:
        with self._lock:
            if not self.running:
                raise RuntimeError(f"{self.title} is not running")
            self._input_mark = self.screen.cursor_line
            os.write(self._master, text.encode())

    def stop(self) -> None:
        """Ctrl+C, escalating if the program does not exit."""
        with self._lock:
            if not self.running:
                return
            self.stopping = True
            self.screen.append_note("[panel] stopping (Ctrl+C)...")
        self._interrupt()

        def escalate() -> None:
            for sig in (None, signal.SIGTERM, signal.SIGKILL):
                try:
                    self._proc.wait(timeout=8)
                    return
                except subprocess.TimeoutExpired:
                    if sig is None:
                        self._interrupt()
                    else:
                        os.killpg(self._proc.pid, sig)

        threading.Thread(target=escalate, daemon=True).start()

    def _interrupt(self) -> None:
        try:
            os.killpg(self._proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    def pids(self) -> set[int]:
        return {self._proc.pid} if self.running else set()

    def _prompt(self) -> dict | None:
        if not self.running or self.stopping:
            return None
        first, lines = self.screen.tail(40)
        for offset in range(len(lines) - 1, -1, -1):
            if first + offset <= self._input_mark:
                break
            for regex, kind, hint in PROMPTS:
                if regex.search(lines[offset]):
                    return {"kind": kind, "text": lines[offset].strip(), "hint": hint}
        return None

    def snapshot(self, version: int = -1) -> dict:
        with self._lock:
            snap = {
                "name": self.name,
                "title": self.title,
                "state": "running" if self.running and not self.stopping else "stopping" if self.running else "exited",
                "exit_code": self.exit_code,
                "stopped": self.stopping,  # stopped from the panel (a non-zero exit code is expected)
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "version": self.screen.version,
                "prompt": self._prompt(),
            }
            if version != self.screen.version:
                snap["first_line"], snap["lines"] = self.screen.tail(400)
            return snap


# ---------- the Jetson, through its agent ----------


def tcp_open(host: str, port: int, timeout: float = 0.8) -> bool | None:
    """True if listening, False if refused (machine up), None if unreachable."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return None


def read_token() -> str:
    try:
        return AGENT_TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def save_token(token: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_TOKEN_FILE.write_text(token.strip())
    AGENT_TOKEN_FILE.chmod(0o600)


class AgentError(RuntimeError):
    pass


def agent_call(ip: str, port: int, method: str, path: str, *, token: str | None = None, body: dict | None = None, timeout: float = 5) -> dict:
    token = read_token() if token is None else token
    if not token:
        raise AgentError("Connect the panel to the Jetson first (Robot page → Jetson).")
    try:
        res = requests.request(method, f"http://{ip}:{port}{path}", json=body, headers={"X-Panel-Token": token}, timeout=timeout)
    except requests.RequestException as e:
        raise AgentError(f"Cannot reach the panel agent on {ip}:{port}. Is ./agent running on the Jetson?") from e
    if res.status_code == 401:
        raise AgentError("The Jetson agent rejected the token. Reconnect on the Robot page.")
    if res.status_code >= 400:
        try:
            detail = res.json().get("detail", res.text)
        except ValueError:
            detail = res.text
        if res.status_code == 409:
            raise ProcBusy(str(detail))
        raise AgentError(str(detail)[:300])
    return res.json()


class AgentLink:
    """Keeps one WebSocket open to the agent: program screens and Jetson health are pushed, never polled."""

    def __init__(self) -> None:
        self.target: tuple[str, int, str] | None = None
        self.connected = False
        self.auth_failed = False
        self.system: dict | None = None
        self.system_at = 0.0
        self.procs: dict[str, dict] = {}
        self._gen = 0
        self._lock = threading.Lock()

    def ensure(self, ip: str, port: int) -> None:
        target = (ip, port, read_token())
        if not target[2]:
            return
        with self._lock:
            if target == self.target:
                return
            self._gen += 1
            self.target, self.connected, self.auth_failed = target, False, False
            self.system, self.procs = None, {}
            threading.Thread(target=self._run, args=(self._gen, target), daemon=True).start()

    def _run(self, gen: int, target: tuple[str, int, str]) -> None:
        ip, port, token = target
        while gen == self._gen:
            try:
                with ws_connect(f"ws://{ip}:{port}/ws?token={token}", open_timeout=4, close_timeout=1) as ws:
                    self.connected, self.auth_failed = True, False
                    while gen == self._gen:
                        try:
                            raw = ws.recv(timeout=6)
                        except TimeoutError:
                            raise OSError("agent stream went quiet") from None
                        self._handle(gen, json.loads(raw))
            except Exception as e:  # noqa: BLE001 - any failure just means "retry"
                if getattr(e, "response", None) is not None and getattr(e.response, "status_code", 0) in (401, 403):
                    self.auth_failed = True
                elif "4401" in str(e):
                    self.auth_failed = True
            if gen == self._gen:
                self.connected = False
                time.sleep(2)

    def _handle(self, gen: int, msg: dict) -> None:
        if gen != self._gen:
            return
        if msg["type"] == "status":
            self.system, self.system_at = msg["data"], time.time()
            for name, info in msg["data"].get("procs", {}).items():
                if info is None:
                    self.procs.pop(name, None)
        elif msg["type"] == "proc":
            self.update_proc(msg["name"], msg["proc"])

    def update_proc(self, name: str, snap: dict) -> None:
        prev = self.procs.get(name) or {}
        if "lines" not in snap and prev.get("started_at") == snap.get("started_at"):
            snap = {**snap, "first_line": prev.get("first_line", 0), "lines": prev.get("lines", [])}
        self.procs[name] = snap


class RemoteProcess:
    """A program running on the Jetson, with the same surface as ConsoleProcess."""

    def __init__(self, name: str, title: str, link: AgentLink, ip: str, port: int) -> None:
        self.name, self.title, self.link, self.ip, self.port = name, title, link, ip, port

    @property
    def _snap(self) -> dict:
        snap = self.link.procs.get(self.name)
        if snap is not None and not self.link.connected and snap["state"] != "exited":
            # Lost the agent: show what we had, but don't claim it is still running.
            return {**snap, "state": "exited", "exit_code": -1, "lines": [*snap.get("lines", []), "[panel] lost contact with the Jetson agent"]}
        return snap or {"state": "exited", "exit_code": -1, "started_at": 0, "ended_at": None, "version": 0, "prompt": None, "lines": [], "first_line": 0}

    @property
    def running(self) -> bool:
        return self._snap["state"] != "exited"

    @property
    def stopping(self) -> bool:
        return self._snap["state"] == "stopping"

    exit_code = property(lambda self: self._snap["exit_code"])
    started_at = property(lambda self: self._snap["started_at"])
    ended_at = property(lambda self: self._snap["ended_at"])
    version = property(lambda self: self._snap["version"])

    def snapshot(self, version: int = -1) -> dict:
        snap = dict(self._snap)
        if version == snap["version"]:
            snap.pop("lines", None)
            snap.pop("first_line", None)
        return snap

    def write(self, text: str) -> None:
        agent_call(self.ip, self.port, "POST", f"/proc/{self.name}/input", body={"text": text})

    def stop(self) -> None:
        agent_call(self.ip, self.port, "POST", f"/proc/{self.name}/stop")

    def pids(self) -> set[int]:
        return set()


# ---------- the controller ----------


class Controls:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.procs: dict[str, ConsoleProcess | RemoteProcess | None] = {"host": None, "teleop": None, "calibrate": None}
        self.calibrate_target: str | None = None  # leader | follower
        self.link = AgentLink()

    def _running(self, name: str) -> ConsoleProcess | RemoteProcess | None:
        p = self.procs[name]
        return p if p and p.running else None

    def leader_users(self) -> list[str]:
        """Panel programs holding the leader arms."""
        users = []
        if self._running("teleop"):
            users.append("Teleoperation")
        if self._running("calibrate") and self.calibrate_target == "leader":
            users.append("Leader calibration")
        return users

    def leader_pids(self) -> set[int]:
        pids: set[int] = set()
        for name in ("teleop", "calibrate"):
            if p := self._running(name):
                pids |= p.pids()
        return pids

    def _start(self, name: str, proc_factory) -> ConsoleProcess | RemoteProcess:
        with self._lock:
            if self._running(name):
                raise ProcBusy(f"{self.procs[name].title} is already running")
            proc = proc_factory()
            self.procs[name] = proc
            return proc

    # --- Jetson host ---

    def _adopt(self, settings: Settings) -> None:
        """Pick up programs the agent already runs (e.g. after the panel restarted)."""
        for name, title in (("host", "Robot host"), ("calibrate", "Follower arm calibration")):
            snap = self.link.procs.get(name)
            if snap and snap["state"] != "exited" and not self._running(name):
                self.procs[name] = RemoteProcess(name, title, self.link, settings.jetson_ip, settings.agent_port)
                if name == "calibrate":
                    self.calibrate_target = "follower"

    def jetson_status(self, settings: Settings) -> dict:
        self.link.ensure(settings.jetson_ip, settings.agent_port)
        if self.link.connected:
            reachable = agent_running = agent_ok = True
            self._adopt(settings)
        else:
            port = tcp_open(settings.jetson_ip, settings.agent_port)
            reachable = port is not None or tcp_open(settings.jetson_ip, 22) is not None
            agent_running, agent_ok = bool(port), False
        host_port = tcp_open(settings.jetson_ip, settings.obs_port) if reachable else None
        fresh = self.link.system and time.time() - self.link.system_at < 6
        return {
            "ip": settings.jetson_ip,
            "reachable": reachable,
            "agent_running": agent_running,
            "agent_ok": agent_ok,
            "token_set": bool(read_token()),
            "token_rejected": self.link.auth_failed,
            "host_listening": bool(host_port),
            "host_managed": self._running("host") is not None,
            "system": self.link.system if fresh else None,
        }

    def _remote_start(self, settings: Settings, name: str, title: str, *, cameras: bool = False) -> RemoteProcess:
        self.link.ensure(settings.jetson_ip, settings.agent_port)
        body = {"name": name, "robot_model": settings.robot_model, "host_args": settings.host_args, "cameras": cameras}
        snap = agent_call(settings.jetson_ip, settings.agent_port, "POST", "/start", body=body)
        self.link.update_proc(name, snap)
        return RemoteProcess(name, title, self.link, settings.jetson_ip, settings.agent_port)

    def start_host(self, settings: Settings, cameras: bool) -> RemoteProcess:
        if not read_token():
            raise RuntimeError("Connect the panel to the Jetson first (Robot page → Jetson).")
        if self._running("calibrate") and self.calibrate_target == "follower":
            raise ProcBusy("Follower calibration is running on the Jetson; finish it first.")
        if tcp_open(settings.jetson_ip, settings.obs_port):
            raise ProcBusy("A robot host is already running on the Jetson (started outside the panel). Stop it first.")
        return self._start("host", lambda: self._remote_start(settings, "host", "Robot host", cameras=cameras))

    def stop_host(self, settings: Settings) -> str:
        if p := self._running("host"):
            p.stop()
            return "stopping"
        # Started from a terminal on the Jetson: have the agent interrupt it.
        res = agent_call(settings.jetson_ip, settings.agent_port, "POST", "/host/kill")
        return res["result"]

    # --- teleop & calibration ---

    def start_teleop(self, settings: Settings, recorder_active: bool) -> ConsoleProcess:
        if recorder_active:
            raise ProcBusy("A recording is running; it already teleoperates the robot.")
        if users := [u for u in self.leader_users() if u != "Teleoperation"]:
            raise ProcBusy(f"{users[0]} is using the leader arms.")
        cmd = [
            PYTHON, str(REPO_ROOT / "examples" / "alohamini" / "teleoperate_bi.py"),
            "--robot.remote_ip", settings.jetson_ip,
            "--robot.robot_model", settings.robot_model,
            "--teleop.id", settings.teleop_id,
            "--teleop.arm_profile", settings.arm_profile,
        ]  # fmt: skip
        return self._start("teleop", lambda: ConsoleProcess("teleop", "Teleoperation", cmd))

    def start_calibration(self, settings: Settings, target: str, recorder_active: bool) -> ConsoleProcess | RemoteProcess:
        if self._running("calibrate"):
            raise ProcBusy("A calibration is already running.")
        if target == "leader":
            if recorder_active or self._running("teleop"):
                raise ProcBusy("Stop the recording/teleoperation first: they are using the leader arms.")
            cmd = [
                PYTHON, str(REPO_ROOT / "examples" / "alohamini" / "calibrate_bi.py"),
                "--teleop.id", settings.teleop_id,
                "--teleop.arm_profile", settings.arm_profile,
            ]  # fmt: skip
            factory = lambda: ConsoleProcess("calibrate", "Leader arm calibration", cmd)  # noqa: E731
        else:
            if not read_token():
                raise RuntimeError("Connect the panel to the Jetson first.")
            if self._running("host") or tcp_open(settings.jetson_ip, settings.obs_port):
                raise ProcBusy("Stop the robot host first: it is using the follower arms.")
            factory = lambda: self._remote_start(settings, "calibrate", "Follower arm calibration")  # noqa: E731
        proc = self._start("calibrate", factory)
        self.calibrate_target = target
        return proc

    def get(self, name: str) -> ConsoleProcess | RemoteProcess | None:
        if name not in self.procs:
            raise KeyError(name)
        return self.procs[name]

    def overview(self) -> dict:
        out = {}
        for name, p in self.procs.items():
            if p is None:
                out[name] = None
                continue
            snap = p.snapshot(p.version)
            out[name] = {
                "title": p.title,
                "state": snap["state"],
                "exit_code": p.exit_code,
                "stopped": p.stopping,
                "started_at": p.started_at,
                "ended_at": p.ended_at,
                "waiting_for_input": snap["prompt"] is not None,
            }  # fmt: skip
        out["calibrate_target"] = self.calibrate_target
        return out

    def shutdown(self) -> None:
        """Stop local programs. Programs on the Jetson belong to its agent and keep running; the panel re-adopts them."""
        for p in self.procs.values():
            if isinstance(p, ConsoleProcess) and p.running:
                p.stop()
