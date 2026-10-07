"""The Upload button: runs examples/alohamini/upload_dataset.py (Hugging Face Hub or rsync) as a background job."""

from __future__ import annotations

import json
import subprocess
import threading
import time

import collection
from config import DATA_DIR, PYTHON, REPO_ROOT, UPLOAD_SCRIPT, lerobot_home

LAST_PATH = DATA_DIR / "last_upload.json"


def _episodes(cfg: collection.CollectionConfig) -> int | None:
    try:
        return int(json.loads((lerobot_home() / cfg.dataset / "meta" / "info.json").read_text())["total_episodes"])
    except (OSError, ValueError, KeyError):
        return None


class UploadJob:
    """One upload at a time; its output is shown live and the last success is remembered across restarts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.method: str | None = None
        self.dry_run = False
        self.status = "idle"  # idle | running | done | failed
        self.output: list[str] = []
        self.started_at: float | None = None
        self.finished_at: float | None = None

    def start(self, method: str, dry_run: bool, operator: str) -> None:
        cfg = collection.load_config()
        target = cfg.hub_repo_id if method == "hub" else cfg.rsync_dest
        if not target:
            raise RuntimeError(f"Upload by {method} is not set up: fill in upload.{'hub_repo_id' if method == 'hub' else 'rsync_dest'} in collection.yaml.")
        with self._lock:
            if self.running:
                raise RuntimeError("An upload is already running.")
            self.running, self.method, self.dry_run, self.status = True, method, dry_run, "running"
            self.output, self.started_at, self.finished_at = [], time.time(), None
        cmd = [PYTHON, str(UPLOAD_SCRIPT), "--method", method] + (["--dry-run"] if dry_run else [])
        threading.Thread(target=self._run, args=(cmd, method, target, dry_run, operator, _episodes(cfg)), daemon=True).start()

    def _run(self, cmd: list[str], method: str, target: str, dry_run: bool, operator: str, episodes: int | None) -> None:
        ok = False
        try:
            proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    stdin=subprocess.DEVNULL)  # fmt: skip
            assert proc.stdout is not None
            for line in proc.stdout:  # rsync progress rewrites one line with \r
                for part in line.replace("\r", "\n").splitlines():
                    if part.strip():
                        self.output = (self.output + [part.rstrip()])[-200:]
            ok = proc.wait() == 0
        except OSError as e:
            self.output.append(str(e))
        finally:
            self.status = "done" if ok else "failed"
            self.running, self.finished_at = False, time.time()
            if ok and not dry_run:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                LAST_PATH.write_text(json.dumps({"method": method, "target": target, "at": time.time(),
                                                 "by": operator, "episodes": episodes}))  # fmt: skip

    @staticmethod
    def last_success() -> dict | None:
        try:
            return json.loads(LAST_PATH.read_text())
        except (OSError, ValueError):
            return None

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "method": self.method,
            "dry_run": self.dry_run,
            "status": self.status,
            "output": self.output[-30:],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_success": self.last_success(),
        }
