"""Episode commands and outcome tracking for evaluate_bi.py when it is run by the panel.

The panel writes one command per line to the program's terminal and reads progress from
``[EVAL:event] {json}`` lines, so no extra connection is needed between them.
"""

import json
import queue
import threading
import time
from pathlib import Path

EVENT_PREFIX = "[EVAL:event] "
# success/fail/end finish the running episode (with or without a verdict); during the wait
# between episodes success/fail relabel the last one and start begins the next one now.
COMMANDS = ("success", "fail", "end", "discard", "start")


class PanelControl:
    def __init__(self, stream=None):
        self._queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        if stream is not None:
            disable_echo(stream)
            threading.Thread(target=self._read, args=(stream,), daemon=True).start()

    def _read(self, stream) -> None:
        for line in stream:
            command = line.strip().lower()
            if command in COMMANDS:
                self._queue.put(command)

    def poll(self) -> str | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


def disable_echo(stream) -> None:
    """Keep typed commands out of the log the panel shows."""
    try:
        import termios

        fd = stream.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (ImportError, OSError, ValueError, AttributeError):
        pass


class EvalOutcomes:
    """Per-episode verdicts, written as eval_info.json (and into the eval dataset's meta/)."""

    def __init__(self, paths: list[Path]):
        self.paths = paths
        self.episodes: list[dict] = []

    def add(self, *, outcome: str | None, frames: int, duration_s: float, ended_by: str,
            dataset_index: int | None) -> None:  # fmt: skip
        self.episodes.append({
            "episode": len(self.episodes) + 1,
            "dataset_index": dataset_index,
            "outcome": outcome,
            "frames": frames,
            "duration_s": round(duration_s, 2),
            "ended_by": ended_by,
        })  # fmt: skip
        self.write()

    def relabel(self, outcome: str) -> bool:
        if not self.episodes:
            return False
        self.episodes[-1]["outcome"] = outcome
        self.write()
        return True

    def counts(self) -> dict:
        success = sum(e["outcome"] == "success" for e in self.episodes)
        fail = sum(e["outcome"] == "fail" for e in self.episodes)
        return {"done": len(self.episodes), "success": success, "fail": fail}

    def write(self) -> None:
        counts = self.counts()
        judged = counts["success"] + counts["fail"]
        info = {
            "aggregated": {
                **counts,
                "unlabeled": counts["done"] - judged,
                "pc_success": round(100 * counts["success"] / judged, 1) if judged else None,
            },
            "episodes": self.episodes,
        }
        for path in self.paths:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(info, indent=2))
                tmp.replace(path)
            except OSError as e:
                print(f"[EVAL] Could not write {path}: {e}", flush=True)


def emit(event: str, **fields) -> None:
    print(EVENT_PREFIX + json.dumps({"event": event, "at": round(time.time(), 2), **fields}), flush=True)
