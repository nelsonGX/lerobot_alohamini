#!/usr/bin/env python3
"""Recording engine behind the web panel: one long-lived process per recording session.

Unlike record_bi.py (free-text task, keys read from a terminal) this one is driven by JSON commands on stdin and
reports its whole state as JSON lines in `--events`. It enforces the data-collection rules:

  * the task is always one of the ids in collection.yaml; there is no way to pass free text
  * before every episode the operator is shown a random object layout and must confirm it (`start`)
  * a recorded episode is only held in memory/encoder until the operator decides: save, discard or re-record
  * nothing partial reaches the dataset: stopping or crashing mid-episode throws the episode away, and a
    save that fails ends the session instead of continuing on a possibly inconsistent dataset
  * one shared dataset: created on first use, resumed afterwards; one writer at a time (lock file)
  * per-episode metadata (operator, task, layout, stream health, flags) goes to meta/collection.jsonl

Commands (one JSON object per line on stdin):
  {"cmd": "set_task", "task_id": "..."}   choose the task for the next episode (only while `ready`)
  {"cmd": "start"}                         layout is set up, begin recording (`ready`)
  {"cmd": "done"}                          stop recording and review it (`recording`)
  {"cmd": "save"} | {"cmd": "discard"} | {"cmd": "rerecord"}      decide (`recording` or `review`)
  {"cmd": "finish"}                        end the session (only while `ready`)
`rerecord` discards and redoes the same task and layout; `discard` discards and moves on to a new layout.
SIGINT/SIGTERM/SIGHUP or closing stdin end the session gracefully: the episode in progress is discarded.

Use --simulate to run everything against fake hardware.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import os
import queue
import random
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from collection import (
    CollectionConfig,
    ConfigError,
    DatasetLockedError,
    DatasetProblemError,
    HealthMonitor,
    append_record,
    dataset_lock,
    describe_layout,
    episode_flags,
    load_config,
    new_layout,
    now_iso,
    read_records,
    reconcile_records,
    suggest_task,
    task_counts,
    validate_dataset,
)
from record_utils import record_loop

PHASES_WITH_ARMS_LIVE = ("ready", "review")
STATE_EVERY_S = 0.4


@contextlib.contextmanager
def native_stderr_as_debug():
    """Route the video encoder's native stderr to DEBUG logging instead of the console."""
    try:
        stderr_fd = sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        yield
        return
    sys.stderr.flush()
    saved = os.dup(stderr_fd)
    try:
        with tempfile.TemporaryFile(mode="w+b") as captured:
            os.dup2(captured.fileno(), stderr_fd)
            try:
                yield
            finally:
                sys.stderr.flush()
                os.dup2(saved, stderr_fd)
                captured.seek(0)
                if out := captured.read().decode(errors="replace").strip():
                    logging.debug("Native video encoder output:\n%s", out)
    finally:
        os.close(saved)


class Engine:
    def __init__(self, args: argparse.Namespace, cfg: CollectionConfig) -> None:
        self.args = args
        self.cfg = cfg
        self.operator = args.operator
        self.session_id = args.session_id
        self.fps = args.fps
        self.episode_time_s = args.episode_time_s
        self.rng = random.Random()
        self.cmds: queue.Queue[dict] = queue.Queue()
        self.shutdown = threading.Event()
        self.shutdown_reason = "finished"
        self.events = {"exit_early": False, "rerecord_episode": False, "stop_recording": False}
        self.phase = "connecting"
        self.message = ""
        self.counts: dict[str, int] = {t.id: 0 for t in cfg.tasks}
        self.episodes: list[dict] = []  # this session: saved / discarded episodes
        self.task_id: str | None = None
        self.layout: dict | None = None
        self.attempt = 1
        self.remaining_s: int | None = None
        self.review: dict | None = None
        self.pending_stats: dict | None = None
        self.saved_since_checkpoint = 0
        self.created_here = False
        self.dataset = None
        self.robot = self.leader = self.keyboard = None
        self.processors = None
        self.health: HealthMonitor | None = None
        self._last_emit = 0.0
        self._recording_started = 0.0
        self._events_fh = open(args.events, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        self.root = Path(args.dataset_root) if args.dataset_root else None

    # ---------------------------------------------------------------- reporting

    def emit(self, kind: str, **payload) -> None:
        self._events_fh.write(json.dumps({"type": kind, "t": time.time(), **payload}, default=str) + "\n")

    def state(self) -> dict:
        tasks = [{**t.__dict__, "count": self.counts.get(t.id, 0)} for t in self.cfg.tasks]
        cur = self.cfg.task(self.task_id) if self.task_id else None
        return {
            "session_id": self.session_id,
            "operator": self.operator,
            "dataset": self.cfg.dataset,
            "fps": self.fps,
            "episode_time_s": self.episode_time_s,
            "phase": self.phase,
            "message": self.message,
            "tasks": tasks,
            "suggested_task_id": suggest_task(self.counts, self.cfg),
            "task_id": self.task_id,
            "task_text": cur.text if cur else None,
            "layout": describe_layout(self.cfg, self.layout, cur) if cur and self.layout else None,
            "attempt": self.attempt,
            "episode_number": (self.dataset.meta.total_episodes + 1) if self.dataset is not None else None,
            "remaining_s": self.remaining_s,
            "health": self.health.live() if self.health else None,
            "review": self.review,
            "episodes": self.episodes[-200:],
            "saved": sum(e["status"] == "saved" for e in self.episodes),
            "discarded": sum(e["status"] == "discarded" for e in self.episodes),
            "other_count": self.counts.get("_other", 0),
            "simulated": bool(self.args.simulate),
        }

    def publish(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_emit >= STATE_EVERY_S:
            self._last_emit = now
            self.emit("state", state=self.state())

    def set_phase(self, phase: str, message: str = "") -> None:
        self.phase, self.message = phase, message
        self.publish(force=True)

    # ---------------------------------------------------------------- commands

    def read_stdin(self) -> None:
        for line in sys.stdin:
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            if not isinstance(cmd, dict) or not isinstance(cmd.get("cmd"), str):
                continue
            name, phase = cmd["cmd"], self.phase
            # Decisions only count in the phase they were made for: a stale "save" must never save a later episode.
            if name == "save" and phase != "review":
                continue
            if name in ("discard", "rerecord") and phase not in ("recording", "review"):
                continue
            self.cmds.put(cmd)
            # An episode in progress must stop now, not at the next queue poll.
            if phase == "recording" and name in ("done", "discard", "rerecord"):
                self.events["exit_early"] = True
        self.request_shutdown("interrupted")  # stdin closed: the panel is gone

    def request_shutdown(self, reason: str) -> None:
        self.shutdown_reason = reason if not self.shutdown.is_set() else self.shutdown_reason
        self.shutdown.set()
        self.events["exit_early"] = True

    # ---------------------------------------------------------------- setup

    def build_devices(self) -> None:
        a = self.args
        from lerobot.processor import make_default_processors

        self.processors = make_default_processors()
        if a.simulate:
            from sim_devices import SimKeyboard, SimLeader, SimRobot

            self.robot, self.leader, self.keyboard = SimRobot(self.fps), SimLeader(), SimKeyboard()
            return
        from lerobot.robots.alohamini import AlohaMiniClient, AlohaMiniClientConfig
        from lerobot.teleoperators.bi_so_leader import BiSOLeader, BiSOLeaderConfig
        from lerobot.teleoperators.keyboard import KeyboardTeleop, KeyboardTeleopConfig
        from lerobot.teleoperators.so_leader import SOLeaderConfig

        self.robot = AlohaMiniClient(
            AlohaMiniClientConfig(remote_ip=a.remote_ip, id=a.robot_id, robot_model=a.robot_model)
        )
        arm = {"arm_profile": a.arm_profile, "use_degrees": False}  # -100..100, like the follower
        self.leader = BiSOLeader(
            BiSOLeaderConfig(
                left_arm_config=SOLeaderConfig(port="/dev/am_arm_leader_left", **arm),
                right_arm_config=SOLeaderConfig(port="/dev/am_arm_leader_right", **arm),
                id=a.leader_id,
            )
        )
        self.keyboard = KeyboardTeleop(KeyboardTeleopConfig())

    def open_dataset(self) -> None:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        from lerobot.utils.constants import ACTION, OBS_STR
        from lerobot.utils.feature_utils import hw_to_dataset_features

        common = {
            "image_writer_threads": 4,
            "streaming_encoding": self.args.streaming_encoding,
            "encoder_threads": self.args.encoder_threads,
        }
        if (self.root / "meta" / "info.json").exists():
            self.dataset = LeRobotDataset.resume(self.cfg.dataset, root=self.root, **common)
        else:
            features = {
                **hw_to_dataset_features(self.robot.action_features, ACTION),
                **hw_to_dataset_features(self.robot.observation_features, OBS_STR),
            }
            self.dataset = LeRobotDataset.create(
                repo_id=self.cfg.dataset,
                root=self.root,
                fps=self.fps,
                features=features,
                robot_type=self.robot.name,
                use_videos=True,
                **common,
            )
            self.created_here = True

    def checkpoint(self) -> None:
        """Close and reopen the dataset: parquet footers are only written on close, so this bounds crash loss."""
        self.set_phase("saving", "Checkpointing the dataset…")
        with native_stderr_as_debug():
            self.dataset.finalize()
        self.dataset = None
        problems = validate_dataset(self.root)
        if problems:
            raise DatasetProblemError("; ".join(problems))
        self.open_dataset()
        self.saved_since_checkpoint = 0
        self.health.reset_clock()

    # ---------------------------------------------------------------- recording steps

    def on_frame(self, info: dict) -> None:
        self.health.frame(
            fresh=info["fresh"],
            latency_s=info["latency_s"],
            images=info["observation"],
            action=info["action"] if self.phase == "recording" else None,
        )
        if self.phase == "recording":
            self.remaining_s = max(
                0, math.ceil(self.episode_time_s - (time.monotonic() - self._recording_started))
            )
        self.publish()

    def _loop(self, *, dataset, seconds: float, task_text: str) -> None:
        p = self.processors
        record_loop(
            robot=self.robot,
            events=self.events,
            fps=self.fps,
            dataset=dataset,
            leader_arm=self.leader,
            keyboard=self.keyboard,
            control_time_s=seconds,
            single_task=task_text,
            teleop_action_processor=p[0],
            robot_action_processor=p[1],
            robot_observation_processor=p[2],
            frame_callback=self.on_frame,
        )

    def idle_step(self) -> None:
        """Keep the follower arms mirroring the leader (and the health numbers fresh) without recording."""
        self._loop(dataset=None, seconds=0.2, task_text="")
        self.events["exit_early"] = self.shutdown.is_set()

    def fresh_observation(self) -> bool:
        before, deadline = (
            self.robot.observation_sequence,
            time.monotonic() + self.robot.config.connect_timeout_s,
        )
        while self.robot.observation_sequence == before:
            self.robot.get_observation()
            if time.monotonic() >= deadline:
                return False
        return True

    def start_new_slot(self, *, reroll: bool, same_attempt: bool = False) -> None:
        """Back to `ready`: pick the task suggestion and a new layout (or keep both for a re-record)."""
        if reroll:
            if self.layout is None and (records := read_records(self.root)):
                self.layout = records[-1].get(
                    "layout"
                )  # so the first layout of a session differs from the last one
            self.layout = new_layout(
                self.cfg, self.rng, self.layout if self.layout and "slots" in self.layout else None
            )
            self.task_id = suggest_task(self.counts, self.cfg) or self.task_id or self.cfg.tasks[0].id
            self.attempt = 1
        elif same_attempt:
            self.attempt += 1
        self.review = None
        self.remaining_s = None
        self.set_phase("ready", "Place the objects as shown, then start.")

    def record_episode(self) -> None:
        if not self.fresh_observation():
            self.set_phase(
                "ready",
                "The robot is not sending data - check the Jetson host and the network, then try again.",
            )
            return
        task = self.cfg.task(self.task_id)
        self.events["exit_early"] = False
        self.health.begin_episode()
        self._recording_started = time.monotonic()
        self.remaining_s = self.episode_time_s
        self.set_phase("recording", "")
        try:
            self._loop(dataset=self.dataset, seconds=self.episode_time_s, task_text=task.text)
        finally:
            self.pending_stats = self.health.end_episode()
            self.events["exit_early"] = self.shutdown.is_set()
        self.remaining_s = None

    def discard_buffer(self) -> None:
        if self.dataset is not None and self.dataset.has_pending_frames():
            self.dataset.clear_episode_buffer()
        self.pending_stats = None
        self.health.reset_clock()

    def enter_review(self) -> None:
        stats = self.pending_stats or {"frames": 0, "duration_s": 0.0}
        flags = episode_flags(
            stats, self.cfg.quality, target_fps=self.fps, episode_time_s=self.episode_time_s
        )
        bad = any(f["severity"] == "bad" for f in flags)
        self.review = {"stats": stats, "flags": flags, "recommend": "discard" if bad else "save"}
        self.set_phase("review", "Recording stopped. Nothing is saved yet.")

    def save_episode(self) -> None:
        task = self.cfg.task(self.task_id)
        stats, review = self.pending_stats or {}, self.review or {}
        self.set_phase("saving", "Encoding and saving the episode…")
        started = time.perf_counter()
        with native_stderr_as_debug():
            self.dataset.save_episode()
        index = self.dataset.meta.total_episodes - 1
        flags = review.get("flags", [])
        append_record(
            self.root,
            {
                "episode_index": index,
                "uid": uuid.uuid4().hex,
                "task_id": task.id,
                "task": task.text,
                "operator": self.operator,
                "session_id": self.session_id,
                "recorded_at": now_iso(),
                "layout": describe_layout(self.cfg, self.layout, task),
                "attempt": self.attempt,
                "duration_s": stats.get("duration_s"),
                "frames": stats.get("frames"),
                "health": stats,
                "flags": flags,
                "flagged": any(f["severity"] == "bad" for f in flags),
                "robot": {"model": self.args.robot_model, "simulated": bool(self.args.simulate)},
            },
        )
        self.counts[task.id] = self.counts.get(task.id, 0) + 1
        self.episodes.append({"index": index, "task_id": task.id, "status": "saved",
                              "flags": [f["code"] for f in flags], "duration_s": stats.get("duration_s")})  # fmt: skip
        self.pending_stats = None
        self.saved_since_checkpoint += 1
        self.health.reset_clock()
        logging.info("Episode %d saved in %.1fs", index, time.perf_counter() - started)
        every = self.cfg.checkpoint_every
        if every and self.saved_since_checkpoint >= every:
            self.checkpoint()
        self.start_new_slot(reroll=True)
        self.message = f"Episode {index + 1} saved."
        self.publish(force=True)

    def drop(self, *, redo: bool) -> None:
        self.discard_buffer()
        self.episodes.append({"index": None, "task_id": self.task_id, "status": "discarded",
                              "flags": [f["code"] for f in (self.review or {}).get("flags", [])]})  # fmt: skip
        self.start_new_slot(reroll=not redo, same_attempt=redo)
        self.message = "Episode thrown away; it was not saved." + (" Same layout again." if redo else "")
        self.publish(force=True)

    def handle(self, cmd: dict) -> bool:
        """Apply one command. Returns True when the session should end."""
        name, phase = cmd["cmd"], self.phase
        if name == "set_task" and phase == "ready":
            if cmd.get("task_id") in {t.id for t in self.cfg.tasks}:
                self.task_id = cmd["task_id"]
                self.publish(force=True)
        elif name == "start" and phase == "ready":
            self.record_episode()
            if self.shutdown.is_set():
                return True
            # `done`/timeout -> review; save/discard/rerecord while recording act on the queue next.
            self.enter_review()
        elif name == "done" and phase == "review":
            pass
        elif name == "save" and phase == "review":
            self.save_episode()
        elif name == "discard" and phase == "review":
            self.drop(redo=False)
        elif name == "rerecord" and phase == "review":
            self.drop(redo=True)
        elif name == "finish":
            if phase == "ready":
                return True
            self.message = "Save or discard the current episode first."
            self.publish(force=True)
        elif name == "sim_fault" and self.args.simulate:
            fault = cmd.get("value")
            self.robot.fault = fault if fault in ("stall", "black_camera") else None
            self.leader.frozen = fault == "no_motion"
        return False

    def run(self) -> str:
        """The session. Returns why it ended; always leaves the dataset consistent."""
        with dataset_lock(self.root):
            existing = (self.root / "meta" / "info.json").exists()
            if existing:
                if problems := validate_dataset(self.root):
                    raise DatasetProblemError("The shared dataset is damaged: " + "; ".join(problems))
                info = json.loads((self.root / "meta" / "info.json").read_text())
                self.fps = int(info["fps"])
                for note in reconcile_records(self.root, int(info["total_episodes"])):
                    logging.warning("collection metadata: %s", note)
                self.counts.update(task_counts(self.root, self.cfg))
            self.set_phase("connecting", "Connecting to the robot and leader arms…")
            self.build_devices()
            self.robot.connect()
            self.leader.connect()
            self.keyboard.connect()
            if not self.robot.is_connected or not self.leader.is_connected:
                raise RuntimeError("Robot or leader arms did not connect.")
            cams = [k for k, v in self.robot.observation_features.items() if isinstance(v, tuple)]
            self.health = HealthMonitor(self.fps, cams, self.cfg.quality)
            self.open_dataset()
            try:
                self.start_new_slot(reroll=True)
                threading.Thread(target=self.read_stdin, daemon=True).start()
                while not self.shutdown.is_set():
                    try:
                        cmd = self.cmds.get_nowait()
                    except queue.Empty:
                        if self.phase in PHASES_WITH_ARMS_LIVE:
                            self.idle_step()
                        else:
                            time.sleep(0.05)
                        self.publish()
                        continue
                    if self.handle(cmd):
                        break
                return self.shutdown_reason
            finally:
                self.close_down()

    def close_down(self) -> None:
        self.set_phase("finalizing", "Writing dataset metadata. Do not unplug anything.")
        errors = []
        with contextlib.suppress(Exception):
            self.discard_buffer()
        for dev in (self.robot, self.leader, self.keyboard):
            with contextlib.suppress(Exception):
                if dev is not None and dev.is_connected:
                    dev.disconnect()
        try:
            if self.dataset is not None:
                with native_stderr_as_debug():
                    self.dataset.finalize()
                if self.dataset.meta.total_episodes == 0 and self.created_here:
                    shutil.rmtree(self.root, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            errors.append(f"finalizing the dataset failed: {e}")
        if errors:
            raise RuntimeError("; ".join(errors))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--operator", required=True, help="Name stored with every episode")
    p.add_argument("--events", required=True, help="File the engine appends its JSON-lines events to")
    p.add_argument("--session-id", default=uuid.uuid4().hex[:10])
    p.add_argument("--config", default=None, help="collection.yaml (default: next to this script)")
    p.add_argument(
        "--dataset-root",
        default=None,
        help="Override the dataset folder (default: $HF_LEROBOT_HOME/<dataset>)",
    )
    p.add_argument("--fps", type=int, default=30, help="Only used when the dataset is created")
    p.add_argument("--episode-time-s", type=int, default=60, help="Maximum episode length")
    p.add_argument("--remote-ip", default="127.0.0.1")
    p.add_argument("--robot-id", default="my_alohamini")
    p.add_argument(
        "--robot-model", default="alohamini1", choices=["alohamini1", "alohamini2", "alohamini2pro"]
    )
    p.add_argument("--teleop-id", dest="leader_id", default="so101_leader_bi")
    p.add_argument("--arm-profile", default="so-arm-5dof", choices=["so-arm-5dof", "am-leader-6dof"])
    p.add_argument("--streaming-encoding", type=lambda s: s.lower() == "true", default=True)
    p.add_argument("--encoder-threads", type=int, default=2)
    p.add_argument("--simulate", action="store_true", help="Fake robot and leader arms (no hardware)")
    return p.parse_args()


def main() -> int:
    from lerobot.utils.utils import init_logging

    init_logging(console_level="INFO")
    args = parse_args()
    args.operator = " ".join(args.operator.split())[:60]
    if not args.operator:
        print("--operator must not be empty", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    from lerobot.utils.constants import HF_LEROBOT_HOME

    engine = Engine(args, cfg)
    engine.root = engine.root or HF_LEROBOT_HOME / cfg.dataset
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: engine.request_shutdown("interrupted"))
    try:
        reason = engine.run()
        engine.emit("closed", reason=reason, error=None)
        return 0
    except (DatasetProblemError, DatasetLockedError) as e:
        engine.emit("closed", reason="error", error=str(e))
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except BaseException as e:  # noqa: BLE001 - the panel must always learn why we stopped
        import traceback

        traceback.print_exc()
        engine.emit("closed", reason="error", error=f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
