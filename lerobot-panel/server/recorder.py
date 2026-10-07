"""Runs one recording session: examples/alohamini/record_panel.py as a child process.

The engine is driven with JSON commands on its stdin and reports its whole state as JSON lines in an events file,
which a thread tails here. Nothing is scraped from terminal output; the console output is only kept as a log.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

import collection
from config import DATA_DIR, PYTHON, RECORD_SCRIPT, REPO_ROOT, SESSIONS_DIR, SIMULATE, Settings, lerobot_home

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]")
NOISE_RE = re.compile(r"^(Svt\[|\[libsvtav1|\[out#|Stream mapping|frame=)")  # encoder chatter

HISTORY_PATH = DATA_DIR / "history.json"
LOG_LIMIT = 2000
KEEP_SESSION_DIRS = 15
# A graceful stop may have to finish encoding an episode; only a hung engine gets killed.
KILL_AFTER_S = 120

COMMANDS = {"set_task", "start", "done", "save", "discard", "rerecord", "finish"}

# Known failures -> what the operator should do about them.
ERROR_HINTS = [
    (re.compile(r"Timeout waiting for AlohaMini Host"), "The robot host is not answering. Start ./host on the Jetson, check the Jetson IP in Settings, then try again."),
    (re.compile(r"ttyACM|am_arm_leader|could not open port|Permission denied: '/dev"), "A leader arm could not be opened. Check its USB cable/power and that no other teleop or recording is running."),
    (re.compile(r"Motor .* (not found|missing)|Missing motor IDs|Failed to (read|write)"), "A leader arm motor did not respond. Power-cycle the leader arms and try again."),
    (re.compile(r"already being recorded into"), "Another recording process is already writing to the shared dataset."),
    (re.compile(r"dataset is damaged|is unreadable"), "The shared dataset has a damaged file, probably from a crash. Do not record more; ask whoever maintains the panel."),
]


@dataclass
class Session:
    id: str
    operator: str
    dataset: str
    episode_time_s: int
    fps: int
    command: list[str]
    started_at: float
    events_path: str
    state: dict | None = None  # the engine's latest full state
    closed: dict | None = None  # the engine's final event
    ended_at: float | None = None
    exit_code: int | None = None
    final: str | None = None  # done | failed | aborted
    error: str | None = None
    error_hint: str | None = None
    stop_requested: bool = False

    @property
    def phase(self) -> str:
        if self.final:
            return self.final
        return (self.state or {}).get("phase", "starting")

    @property
    def active(self) -> bool:
        return self.final is None

    def counts(self) -> tuple[int, int, int]:
        eps = (self.state or {}).get("episodes", [])
        saved = [e for e in eps if e["status"] == "saved"]
        flagged = sum(1 for e in saved if e.get("flags"))
        return len(saved), sum(e["status"] == "discarded" for e in eps), flagged

    def summary(self) -> dict:
        saved, discarded, flagged = self.counts()
        return {
            "id": self.id,
            "operator": self.operator,
            "dataset": self.dataset,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "outcome": self.phase,
            "episodes_saved": saved,
            "episodes_discarded": discarded,
            "episodes_flagged": flagged,
            "error": self.error,
        }


class RecorderBusy(RuntimeError):
    pass


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session: Session | None = None
        self._proc: subprocess.Popen | None = None
        self._log: deque[tuple[int, str]] = deque(maxlen=LOG_LIMIT)
        self._log_seq = 0
        self._tail_done = threading.Event()

    # ---------- lifecycle ----------

    def start(self, *, settings: Settings, operator: str, episode_time_s: int) -> Session:
        with self._lock:
            if self._session and self._session.active:
                raise RecorderBusy(f"{self._session.operator} is already recording")
            cfg = collection.load_config()
            sid = uuid.uuid4().hex[:10]
            session_dir = SESSIONS_DIR / sid
            session_dir.mkdir(parents=True, exist_ok=True)
            events_path = session_dir / "events.jsonl"
            events_path.touch()
            cmd = [
                PYTHON, str(RECORD_SCRIPT),
                "--operator", operator,
                "--session-id", sid,
                "--events", str(events_path),
                "--fps", str(settings.default_fps),
                "--episode-time-s", str(episode_time_s),
                "--remote-ip", settings.jetson_ip,
                "--robot-model", settings.robot_model,
                "--teleop-id", settings.teleop_id,
                "--arm-profile", settings.arm_profile,
            ]  # fmt: skip
            if SIMULATE:
                cmd.append("--simulate")
            env = os.environ.copy()
            # No desktop key listener: the engine takes its commands from us.
            env.pop("DISPLAY", None)
            env.pop("WAYLAND_DISPLAY", None)
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                bufsize=1, cwd=REPO_ROOT, env=env, start_new_session=True,
            )  # fmt: skip
            self._proc = proc
            self._log.clear()
            self._tail_done = threading.Event()
            s = self._session = Session(
                id=sid, operator=operator, dataset=cfg.dataset, episode_time_s=episode_time_s,
                fps=settings.default_fps, command=cmd, started_at=time.time(), events_path=str(events_path),
            )  # fmt: skip
            self._append_log(f"$ {' '.join(cmd[1:])}")
            threading.Thread(target=self._read_log, args=(proc,), daemon=True).start()
            threading.Thread(target=self._tail_events, args=(s, proc), daemon=True).start()
            threading.Thread(target=self._wait_exit, args=(s, proc), daemon=True).start()
            _prune_sessions(keep=sid)
            return s

    def command(self, name: str, **fields) -> None:
        """Send one command to the engine; the engine ignores it if it makes no sense in its current phase."""
        if name not in COMMANDS:
            raise RuntimeError(f"Unknown command {name}")
        with self._lock:
            s, proc = self._session, self._proc
            if not s or not s.active or proc is None or proc.stdin is None:
                raise RuntimeError("No active recording session")
            if name == "finish":
                s.stop_requested = True
            try:
                proc.stdin.write(json.dumps({"cmd": name, **fields}) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise RuntimeError("The recorder is not accepting commands (it is shutting down)") from e

    def abort(self) -> None:
        """Ask the engine to stop gracefully (episode in progress is discarded, dataset is closed properly)."""
        with self._lock:
            proc = self._proc
            if not proc or proc.poll() is not None:
                raise RuntimeError("No running recorder")
            if self._session:
                self._session.stop_requested = True
            self._append_log("[panel] stop requested: the episode in progress is discarded and the dataset is closed")
        os.killpg(proc.pid, signal.SIGINT)

        def escalate() -> None:
            try:
                proc.wait(timeout=KILL_AFTER_S)
            except subprocess.TimeoutExpired:
                self._append_log("[panel] recorder did not stop, killing it")
                os.killpg(proc.pid, signal.SIGKILL)

        threading.Thread(target=escalate, daemon=True).start()

    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc and proc.poll() is None else None

    def recording_dataset(self) -> str | None:
        with self._lock:
            return self._session.dataset if self._session and self._session.active else None

    def live_counts(self) -> dict[str, int] | None:
        """Per-task episode counts straight from the running engine (the files on disk are mid-write)."""
        with self._lock:
            s = self._session
            if not s or not s.active or not s.state:
                return None
            return {t["id"]: t["count"] for t in s.state["tasks"]}

    # ---------- state ----------

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            s = self._session
            lines = [{"id": i, "text": t} for i, t in self._log if i > since]
            session = None
            if s is not None:
                saved, discarded, _ = s.counts()
                session = {
                    "id": s.id, "operator": s.operator, "dataset": s.dataset, "episode_time_s": s.episode_time_s,
                    "fps": s.fps, "started_at": s.started_at, "ended_at": s.ended_at, "phase": s.phase,
                    "active": s.active, "error": s.error, "error_hint": s.error_hint,
                    "stop_requested": s.stop_requested, "saved_count": saved, "discarded_count": discarded,
                    "state": s.state, "now": time.time(),
                }  # fmt: skip
            return {"session": session, "log": lines[-800:], "log_seq": self._log_seq}

    # ---------- engine output ----------

    def _append_log(self, text: str) -> None:
        with self._lock:
            self._log_seq += 1
            self._log.append((self._log_seq, text))

    def _read_log(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = ANSI_RE.sub("", raw).rstrip()
            if line.strip() and not NOISE_RE.match(line):
                self._append_log(line)

    def _tail_events(self, s: Session, proc: subprocess.Popen) -> None:
        pos, buf = 0, b""
        while True:
            exited = proc.poll() is not None
            try:
                with open(s.events_path, "rb") as f:
                    f.seek(pos)
                    data = f.read()
            except OSError:
                data = b""
            if data:
                pos += len(data)
                buf += data
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    with self._lock:
                        if ev.get("type") == "state":
                            s.state = ev["state"]
                        elif ev.get("type") == "closed":
                            s.closed = ev
            elif exited:
                break
            time.sleep(0.1)
        self._tail_done.set()

    def _wait_exit(self, s: Session, proc: subprocess.Popen) -> None:
        code = proc.wait()
        self._tail_done.wait(5)
        with self._lock:
            s.exit_code, s.ended_at = code, time.time()
            closed = s.closed or {}
            reason = closed.get("reason")
            if reason == "finished":
                s.final = "done"
            elif reason == "interrupted":
                s.final = "aborted"
                s.error = "The session was stopped. The episode in progress was discarded; saved episodes are safe."
            else:
                s.final = "failed"
                tail = [t for _, t in list(self._log)[-8:]]
                s.error = closed.get("error") or "\n".join(tail) or f"The recorder exited with code {code}"
                if reason != "error":  # it never got to say why: it was killed or crashed hard
                    s.error = f"The recorder stopped unexpectedly (exit code {code}).\n{s.error}"
                    if problems := _dataset_problems(s.dataset):
                        s.error += "\nThe dataset needs attention: " + "; ".join(problems)
                s.error_hint = next((h for pat, h in ERROR_HINTS if pat.search(s.error)), None)
            self._append_log(f"[panel] recorder exited with code {code}")
            _append_history(s.summary())


def _dataset_problems(repo_id: str) -> list[str]:
    root = lerobot_home() / repo_id
    if not (root / "meta" / "info.json").exists():
        return []
    return collection.validate_dataset(root)


def _prune_sessions(keep: str) -> None:
    if not SESSIONS_DIR.exists():
        return
    dirs = sorted((d for d in SESSIONS_DIR.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[KEEP_SESSION_DIRS:]:
        if d.name != keep:
            shutil.rmtree(d, ignore_errors=True)


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
