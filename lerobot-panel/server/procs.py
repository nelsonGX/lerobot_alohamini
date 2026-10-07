"""Console programs the panel runs on behalf of teammates, so nobody needs a terminal.

- host:      the robot host on the Jetson (over SSH), what ./host does
- teleop:    teleoperation without recording, what ./client does
- calibrate: leader arm calibration here (./lcalibrate) or follower calibration on the Jetson (./fcalibrate)

Each runs in a pseudo-terminal; its screen is kept in a TermBuffer and interactive
prompts ("press ENTER") are detected so the UI can show buttons for them.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import shlex
import signal
import socket
import struct
import subprocess
import termios
import threading
import time

from config import DATA_DIR, PYTHON, REPO_ROOT, Settings
from term import TermBuffer

SSH_DIR = DATA_DIR / "ssh"
SSH_KEY = SSH_DIR / "id_ed25519"
KNOWN_HOSTS = SSH_DIR / "known_hosts"
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
    def __init__(self, name: str, title: str, cmd: list[str], *, remote: bool, env: dict | None = None) -> None:
        self.name = name
        self.title = title
        self.cmd = cmd
        self.remote = remote  # ssh -tt: stop with Ctrl+C through the remote terminal
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
        if self.remote:
            try:
                os.write(self._master, b"\x03")  # forwarded to the Jetson's terminal -> SIGINT there
            except OSError:
                pass
        else:
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


# ---------- Jetson over SSH ----------


def _tcp_open(host: str, port: int, timeout: float = 0.8) -> bool | None:
    """True if listening, False if refused (machine up), None if unreachable."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return None


def ensure_key() -> str:
    SSH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not SSH_KEY.exists():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"lerobot-panel@{socket.gethostname()}", "-f", str(SSH_KEY)],
            check=True,
        )  # fmt: skip
    return SSH_KEY.with_suffix(".pub").read_text().strip()


def ssh_cmd(settings: Settings, script: str, *, tty: bool = False) -> list[str]:
    """ssh into the Jetson and run `script` in a login shell from the repo directory."""
    remote = "export PATH=\"$HOME/.local/bin:$HOME/.cargo/bin:$PATH\"; "
    if settings.jetson_repo:
        remote += f"cd {shlex.quote(settings.jetson_repo)} && "
    remote += script
    return [
        "ssh", "-tt" if tty else "-T",
        "-i", str(SSH_KEY),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        f"{settings.jetson_user}@{settings.jetson_ip}",
        f"bash -lc {shlex.quote(remote)}",
    ]  # fmt: skip


def ssh_run(settings: Settings, script: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(ssh_cmd(settings, script), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


FIND_REPO = r"""for d in ~/lerobot_alohamini ~/*/ ~/*/*/; do d="${d%/}"; [ -f "$d/host" ] && [ -d "$d/src/lerobot" ] && echo "$d"; done | awk '!seen[$0]++' | head -n 5"""


def setup_ssh(settings: Settings, user: str, password: str) -> dict:
    """Install the panel's key on the Jetson with a one-time password login, then find the repo."""
    pub = ensure_key()
    askpass = SSH_DIR / "askpass.sh"
    askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$PANEL_SSH_PASSWORD"\n')
    askpass.chmod(0o700)
    install = (
        "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
        f"(grep -qxF {shlex.quote(pub)} ~/.ssh/authorized_keys || echo {shlex.quote(pub)} >> ~/.ssh/authorized_keys)"
    )
    env = {**os.environ, "SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force", "PANEL_SSH_PASSWORD": password, "DISPLAY": ":0"}
    try:
        res = subprocess.run(
            ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
             "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
             "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=6",
             f"{user}@{settings.jetson_ip}", install],
            capture_output=True, text=True, timeout=25, env=env, stdin=subprocess.DEVNULL, start_new_session=True,
        )  # fmt: skip
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Timed out connecting to {settings.jetson_ip}") from e
    if res.returncode != 0:
        msg = res.stderr.strip().splitlines()[-1] if res.stderr.strip() else f"ssh exited with {res.returncode}"
        if "Permission denied" in msg:
            msg = "Wrong username or password."
        raise RuntimeError(msg)

    trial = settings.model_copy(update={"jetson_user": user, "jetson_repo": ""})
    res = ssh_run(trial, FIND_REPO)
    if res.returncode != 0:
        raise RuntimeError("Key installed, but logging in with it failed: " + res.stderr.strip()[-300:])
    repos = [r for r in res.stdout.split() if r]
    return {"user": user, "repo": repos[0] if repos else "", "candidates": repos}


def host_command(settings: Settings, cameras: bool) -> str:
    args = f"--robot_model {shlex.quote(settings.robot_model)} {settings.host_args}"
    if not cameras:
        args += " --no_cameras"
    # Piping through cat makes stdout a pipe, so the host prints plain logs instead of its full-screen dashboard.
    return f"PYTHONUNBUFFERED=1 uv run python -m {HOST_MODULE} {args} 2>&1 | cat"


# ---------- the controller ----------


class Controls:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.procs: dict[str, ConsoleProcess | None] = {"host": None, "teleop": None, "calibrate": None}
        self.calibrate_target: str | None = None  # leader | follower
        self._jetson_cache: tuple[float, str, dict] | None = None

    def _running(self, name: str) -> ConsoleProcess | None:
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

    def _start(self, name: str, proc_factory) -> ConsoleProcess:
        with self._lock:
            if self._running(name):
                raise ProcBusy(f"{self.procs[name].title} is already running")
            proc = proc_factory()
            self.procs[name] = proc
            return proc

    # --- Jetson host ---

    def jetson_status(self, settings: Settings, fresh: bool = False) -> dict:
        key = f"{settings.jetson_ip}:{settings.obs_port}"
        cached = self._jetson_cache
        if not fresh and cached and cached[1] == key and time.time() - cached[0] < 2.5:
            return cached[2]
        ssh_port = _tcp_open(settings.jetson_ip, 22)
        host_port = _tcp_open(settings.jetson_ip, settings.obs_port) if ssh_port is not None else None
        managed = self._running("host")
        status = {
            "ip": settings.jetson_ip,
            "reachable": ssh_port is not None,
            "ssh_configured": bool(settings.jetson_user and SSH_KEY.exists()),
            "user": settings.jetson_user,
            "repo": settings.jetson_repo,
            "host_listening": bool(host_port),
            "host_managed": managed is not None,
        }
        self._jetson_cache = (time.time(), key, status)
        return status

    def start_host(self, settings: Settings, cameras: bool) -> ConsoleProcess:
        if not settings.jetson_user or not SSH_KEY.exists():
            raise RuntimeError("Connect the panel to the Jetson first (Robot page → Jetson).")
        if self._running("calibrate") and self.calibrate_target == "follower":
            raise ProcBusy("Follower calibration is running on the Jetson; finish it first.")
        if _tcp_open(settings.jetson_ip, settings.obs_port):
            raise ProcBusy("A robot host is already running on the Jetson (started outside the panel). Stop it first.")
        cmd = ssh_cmd(settings, host_command(settings, cameras), tty=True)
        self._jetson_cache = None
        return self._start("host", lambda: ConsoleProcess("host", "Robot host", cmd, remote=True))

    def stop_host(self, settings: Settings) -> str:
        self._jetson_cache = None
        if p := self._running("host"):
            p.stop()
            return "stopping"
        # Started from a terminal on the Jetson: interrupt it there.
        res = ssh_run(settings, f"pkill -INT -f {shlex.quote(HOST_MODULE)} && echo stopped || echo none", timeout=15)
        if res.returncode != 0 and not res.stdout:
            raise RuntimeError(res.stderr.strip()[-300:] or "Could not reach the Jetson over SSH")
        return "stopping" if "stopped" in res.stdout else "not running"

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
        return self._start("teleop", lambda: ConsoleProcess("teleop", "Teleoperation", cmd, remote=False))

    def start_calibration(self, settings: Settings, target: str, recorder_active: bool) -> ConsoleProcess:
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
            factory = lambda: ConsoleProcess("calibrate", "Leader arm calibration", cmd, remote=False)  # noqa: E731
        else:
            if not settings.jetson_user or not SSH_KEY.exists():
                raise RuntimeError("Connect the panel to the Jetson first.")
            if self._running("host") or _tcp_open(settings.jetson_ip, settings.obs_port):
                raise ProcBusy("Stop the robot host first: it is using the follower arms.")
            no_base = " --no_base" if "--no_base" in settings.host_args else ""
            script = f"uv run python -m lerobot.robots.alohamini.alohamini_calibrate --robot_model {shlex.quote(settings.robot_model)}{no_base}"
            cmd = ssh_cmd(settings, script, tty=True)
            factory = lambda: ConsoleProcess("calibrate", "Follower arm calibration", cmd, remote=True)  # noqa: E731
        proc = self._start("calibrate", factory)
        self.calibrate_target = target
        return proc

    def get(self, name: str) -> ConsoleProcess | None:
        if name not in self.procs:
            raise KeyError(name)
        return self.procs[name]

    def overview(self) -> dict:
        out = {}
        for name, p in self.procs.items():
            out[name] = None if p is None else {
                "title": p.title,
                "state": "running" if p.running and not p.stopping else "stopping" if p.running else "exited",
                "exit_code": p.exit_code,
                "stopped": p.stopping,
                "started_at": p.started_at,
                "ended_at": p.ended_at,
                "waiting_for_input": p.snapshot(p.screen.version)["prompt"] is not None,
            }  # fmt: skip
        out["calibrate_target"] = self.calibrate_target
        return out

    def shutdown(self) -> None:
        for p in self.procs.values():
            if p and p.running:
                p.stop()

