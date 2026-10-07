"""Runs examples/alohamini/record_bi.py in a pseudo-terminal and tracks its progress.

record_bi.py reads episode controls (n = next, r = re-record, q = quit) from its
controlling TTY when no X display is available, so the panel drives it by writing
those keys to the PTY. Its console output is parsed into a structured session state.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field

from config import DATA_DIR, PYTHON, RECORD_SCRIPT, REPO_ROOT, Settings, lerobot_home

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]")
STATUS_RE = re.compile(
    r"\[(?P<phase>RECORD|RESET)\s*\]\s*Ep\s+(?P<ep>\d+)\s*\|\s*(?P<rem>\d+)s\s*\|\s*(?:NOT )?RECORDING"
    r"(?:\s*\|\s*FPS\s+(?P<fps>[\d.]+)/(?P<target>\d+))?"
)
STARTED_RE = re.compile(r"Episode (\d+) recording started")
DISCARD_RE = re.compile(r"Discarding episode (\d+)")
SAVING_RE = re.compile(r"Saving episode (\d+)")
SAVED_RE = re.compile(r"Episode (\d+) saved in ([\d.]+) second")
RATE_RE = re.compile(r"Episode (\d+) capture rate: ([\d.]+) FPS \((\d+) frames in ([\d.]+)s")
DONE_RE = re.compile(r"Dataset saved at (.+)$")

HISTORY_PATH = DATA_DIR / "history.json"
LOG_LIMIT = 4000

# Known failures -> what the operator should do about them.
ERROR_HINTS = [
    (re.compile(r"Timeout waiting for AlohaMini Host"), "The robot host is not answering. Start ./host on the Jetson, check the Jetson IP in Settings, then try again."),
    (re.compile(r"ttyACM|am_arm_leader|could not open port|Permission denied: '/dev"), "A leader arm could not be opened. Check its USB cable/power and that no other teleop or recording is running."),
    (re.compile(r"did not receive a fresh observation"), "The robot stopped sending observations. Check the Jetson host terminal and the network."),
    (re.compile(r"FileExistsError"), "A folder with this dataset name already exists but is not a valid dataset. Pick another name or delete that folder."),
    (re.compile(r"Motor .* (not found|missing)|Missing motor IDs|Failed to (read|write)"), "A leader arm motor did not respond. Power-cycle the leader arms and try again."),
]


@dataclass
class EpisodeInfo:
    number: int
    status: str = "recording"  # recording | resetting | saving | saved | discarded
    frames: int | None = None
    duration_s: float | None = None
    fps: float | None = None


@dataclass
class Session:
    id: str
    operator: str
    dataset: str
    task: str
    num_episodes: int
    episode_time_s: int
    reset_time_s: int
    fps: int
    resume: bool
    command: list[str]
    started_at: float
    phase: str = "starting"  # starting | recording | resetting | saving | waiting | finalizing | done | failed | aborted
    episode: int | None = None
    remaining_s: int | None = None
    live_fps: float | None = None
    episodes: list[EpisodeInfo] = field(default_factory=list)
    ended_at: float | None = None
    exit_code: int | None = None
    error: str | None = None
    error_hint: str | None = None
    dataset_path: str | None = None
    stop_requested: bool = False

    @property
    def active(self) -> bool:
        return self.phase not in ("done", "failed", "aborted")

    def summary(self) -> dict:
        saved = sum(e.status == "saved" for e in self.episodes)
        discarded = sum(e.status == "discarded" for e in self.episodes)
        return {
            "id": self.id,
            "operator": self.operator,
            "dataset": self.dataset,
            "task": self.task,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "outcome": self.phase,
            "episodes_saved": saved,
            "episodes_discarded": discarded,
            "error": self.error,
        }


class RecorderBusy(RuntimeError):
    pass


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session: Session | None = None
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._log: deque[tuple[int, str]] = deque(maxlen=LOG_LIMIT)
        self._log_seq = 0
        self._error_lines: list[str] = []

    # ---------- lifecycle ----------

    def start(
        self,
        *,
        settings: Settings,
        operator: str,
        dataset: str,
        task: str,
        num_episodes: int,
        episode_time_s: int,
        reset_time_s: int,
        fps: int,
        resume: bool,
    ) -> Session:
        with self._lock:
            if self._session and self._session.active:
                raise RecorderBusy(f"{self._session.operator or 'Someone'} is already recording {self._session.dataset}")

            cmd = [
                PYTHON, str(RECORD_SCRIPT),
                "--robot.remote_ip", settings.jetson_ip,
                "--robot.robot_model", settings.robot_model,
                "--teleop.id", settings.teleop_id,
                "--teleop.arm_profile", settings.arm_profile,
                "--dataset.repo_id", dataset,
                "--dataset.single_task", task,
                "--dataset.num_episodes", str(num_episodes),
                "--dataset.episode_time_s", str(episode_time_s),
                "--dataset.reset_time_s", str(reset_time_s),
                "--dataset.fps", str(fps),
                "--push_to_hub", "false",
            ]  # fmt: skip
            if resume:
                cmd.append("--resume")

            env = os.environ.copy()
            # Force record_bi.py onto its terminal key listener (the PTY we control)
            # instead of a pynput global listener on someone's desktop session.
            env.pop("DISPLAY", None)
            env.pop("WAYLAND_DISPLAY", None)
            env["PYTHONUNBUFFERED"] = "1"
            env["TERM"] = "xterm"

            master, slave = pty.openpty()
            # Wide terminal so the one-line countdown never wraps.
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 220, 0, 0))
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    cwd=REPO_ROOT,
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError:
                os.close(master)
                raise
            finally:
                os.close(slave)

            self._proc = proc
            self._master_fd = master
            self._log.clear()
            self._error_lines = []
            self._session = Session(
                id=uuid.uuid4().hex[:10],
                operator=operator,
                dataset=dataset,
                task=task,
                num_episodes=num_episodes,
                episode_time_s=episode_time_s,
                reset_time_s=reset_time_s,
                fps=fps,
                resume=resume,
                command=cmd,
                started_at=time.time(),
            )
            self._append_log(f"$ {' '.join(cmd[1:])}")
            threading.Thread(target=self._read_output, args=(master,), daemon=True).start()
            threading.Thread(target=self._wait_exit, args=(proc,), daemon=True).start()
            return self._session

    def send_control(self, action: str) -> None:
        """Send an episode control to record_bi.py's terminal listener."""
        keys = {"next": "n", "rerecord": "r", "stop": "q", "discard_stop": "rq"}[action]
        with self._lock:
            s = self._session
            if not s or not s.active or self._master_fd is None:
                raise RuntimeError("No active recording")
            if s.phase not in ("recording", "resetting"):
                raise RuntimeError("Controls are only available while recording or resetting")
            if action in ("stop", "discard_stop"):
                s.stop_requested = True
            self._append_log(f"[panel] sent '{keys}' ({action})")
            os.write(self._master_fd, keys.encode())

    def abort(self) -> None:
        """Interrupt the recorder (Ctrl+C), escalating to SIGKILL if it hangs."""
        with self._lock:
            proc = self._proc
            if not proc or proc.poll() is not None:
                raise RuntimeError("No running recorder")
            if self._session:
                self._session.stop_requested = True
            self._append_log("[panel] abort requested: sending SIGINT")
        os.killpg(proc.pid, signal.SIGINT)

        def escalate() -> None:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._append_log("[panel] recorder did not exit, sending SIGKILL")
                os.killpg(proc.pid, signal.SIGKILL)

        threading.Thread(target=escalate, daemon=True).start()

    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc and proc.poll() is None else None

    def recording_dataset(self) -> str | None:
        with self._lock:
            return self._session.dataset if self._session and self._session.active else None

    def _remove_if_empty(self, repo_id: str) -> None:
        """record_bi.py creates the dataset before connecting; drop it if nothing was saved."""
        root = lerobot_home() / repo_id
        try:
            info = json.loads((root / "meta" / "info.json").read_text())
        except (OSError, ValueError):
            return
        if info.get("total_episodes", 0) == 0:
            shutil.rmtree(root, ignore_errors=True)
            self._append_log(f"[panel] removed empty dataset folder {root}")

    # ---------- state ----------

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            s = self._session
            lines = [{"id": i, "text": t} for i, t in self._log if i > since]
            return {
                "session": None if s is None else {
                    **{k: v for k, v in asdict(s).items() if k != "command"},
                    "active": s.active,
                    "saved_count": sum(e.status == "saved" for e in s.episodes),
                    "now": time.time(),
                },
                "log": lines[-800:],
                "log_seq": self._log_seq,
            }  # fmt: skip

    # ---------- output parsing ----------

    def _append_log(self, text: str) -> None:
        self._log_seq += 1
        self._log.append((self._log_seq, text))

    def _episode(self, number: int) -> EpisodeInfo:
        assert self._session is not None
        for ep in self._session.episodes:
            if ep.number == number and ep.status not in ("saved", "discarded"):
                return ep
        ep = EpisodeInfo(number=number)
        self._session.episodes.append(ep)
        return ep

    def _apply_status(self, m: re.Match) -> None:
        s = self._session
        assert s is not None
        ep = int(m["ep"])
        s.episode = ep
        s.remaining_s = int(m["rem"])
        if m["phase"] == "RECORD":
            s.phase = "recording"
            self._episode(ep).status = "recording"
            if m["fps"]:
                s.live_fps = float(m["fps"])
        else:
            s.phase = "resetting"
            self._episode(ep).status = "resetting"

    def _handle_line(self, line: str) -> None:
        s = self._session
        if s is None:
            return
        status = STATUS_RE.search(line)
        if status:  # countdown ticks update state but stay out of the log
            self._apply_status(status)
            return
        if not line.strip():
            return
        self._append_log(line)

        if m := STARTED_RE.search(line):
            s.phase, s.episode, s.remaining_s, s.live_fps = "recording", int(m[1]), s.episode_time_s, None
            self._episode(int(m[1])).status = "recording"
        elif m := RATE_RE.search(line):
            ep = self._episode(int(m[1]))
            ep.fps, ep.frames, ep.duration_s = float(m[2]), int(m[3]), float(m[4])
        elif "Resetting before save" in line:
            s.phase, s.remaining_s = "resetting", s.reset_time_s
        elif m := DISCARD_RE.search(line):
            self._episode(int(m[1])).status = "discarded"
            s.phase, s.remaining_s = "waiting", None
        elif m := SAVING_RE.search(line):
            s.phase, s.remaining_s = "saving", None
            self._episode(int(m[1])).status = "saving"
        elif m := SAVED_RE.search(line):
            self._episode(int(m[1])).status = "saved"
            s.phase = "waiting"
        elif line.startswith("Saving dataset"):
            s.phase, s.remaining_s = "finalizing", None
        elif m := DONE_RE.search(line):
            s.dataset_path = m[1].strip()
        elif line.startswith("Traceback") or self._error_lines:
            self._error_lines.append(line)
        elif re.match(r"^\w*(Error|Exception)\b", line):
            self._error_lines.append(line)

    def _read_output(self, fd: int) -> None:
        pending = ""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:  # EIO once the child closes the PTY
                break
            if not chunk:
                break
            text = ANSI_RE.sub("", (pending + chunk.decode(errors="replace")).replace("\x1b[2K", ""))
            parts = re.split(r"\r\n|\r|\n", text)
            pending = parts.pop()
            with self._lock:
                for part in parts:
                    self._handle_line(part)
                # The countdown rewrites one row without a newline; read it eagerly.
                if (m := STATUS_RE.search(pending)) and self._session:
                    self._apply_status(m)
        with self._lock:
            if pending.strip():
                self._handle_line(pending)
        try:
            os.close(fd)
        except OSError:
            pass

    def _wait_exit(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        time.sleep(0.3)  # let the reader drain the last output
        with self._lock:
            s = self._session
            if s is None or self._proc is not proc:
                return
            s.exit_code = code
            s.ended_at = time.time()
            s.remaining_s = None
            if code == 0 and s.dataset_path:
                s.phase = "done"
            elif code in (-signal.SIGINT, -signal.SIGKILL, 130) and s.stop_requested:
                s.phase = "aborted"
                s.error = "Recording was interrupted. Episodes saved before the interrupt may need checking."
            else:
                s.phase = "failed"
                tail = [line for line in self._error_lines if line.strip()][-12:]
                if not tail:
                    tail = [t for _, t in list(self._log)[-8:]]
                s.error = "\n".join(tail) or f"Recorder exited with code {code}"
                s.error_hint = next((hint for pat, hint in ERROR_HINTS if pat.search(s.error)), None)
            for ep in s.episodes:
                if ep.status in ("recording", "resetting", "saving"):
                    ep.status = "discarded" if s.phase != "done" else ep.status
            self._append_log(f"[panel] recorder exited with code {code}")
            if not s.resume and s.phase != "done":
                self._remove_if_empty(s.dataset)
            self._master_fd = None
            _append_history(s.summary())


_history_lock = threading.Lock()


def _append_history(entry: dict) -> None:
    with _history_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        history = load_history()
        history.insert(0, entry)
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(history[:200], indent=1))
        tmp.replace(HISTORY_PATH)


def load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text())
    except ValueError:
        return []
