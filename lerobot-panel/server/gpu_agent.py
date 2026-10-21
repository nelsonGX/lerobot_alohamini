"""GPU-side training/evaluation service. Start with ./gpu_agent on the GPU machine."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from config import DATA_DIR, REPO_ROOT, lerobot_home
from fastapi import Depends, FastAPI, Header, HTTPException
from gpu_jobs import JobRequest, command
from procs import ConsoleProcess

STATE_DIR = DATA_DIR / "gpu_jobs"
OUTPUT_DIR = REPO_ROOT / "outputs" / "panel"
TOKEN_FILE = DATA_DIR / "gpu_agent_token"


def load_token() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(TOKEN_FILE, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
            f.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    return TOKEN_FILE.read_text().strip()


class Jobs:
    def __init__(self, state_dir: Path, output_dir: Path):
        self.state_dir, self.output_dir = state_dir, output_dir
        self.lock = threading.RLock()
        self.records: dict[str, dict] = {}
        self.procs: dict[str, ConsoleProcess] = {}
        state_dir.mkdir(parents=True, exist_ok=True)
        for path in state_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text())
                if record["state"] in ("running", "stopping"):
                    record.update(state="interrupted", ended_at=time.time())
                self.records[record["id"]] = record
            except (OSError, ValueError, KeyError):
                continue

    def _save(self, record: dict) -> None:
        path = self.state_dir / f"{record['id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2))
        tmp.replace(path)

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.records:
                raise HTTPException(404, "Unknown GPU job")
            return dict(self.records[job_id])

    def start(self, req: JobRequest) -> dict:
        with self.lock:
            if any(p.running for p in self.procs.values()):
                raise HTTPException(409, "A GPU job is already running. Stop it or wait for it to finish.")
            if req.kind == "robot_eval" and (lerobot_home() / req.dataset).exists():
                raise HTTPException(
                    409, "This evaluation dataset already exists on the GPU server. Choose a new name."
                )
            job_id = uuid.uuid4().hex
            output = self.output_dir / f"{req.name}-{job_id[:10]}"
            output.parent.mkdir(parents=True, exist_ok=True)
            cmd = command(req, output)
            try:
                proc = ConsoleProcess(job_id, f"{req.kind}: {req.name}", cmd, env={"MUJOCO_GL": "egl"})
            except OSError as e:
                raise HTTPException(500, str(e)) from e
            record = {
                "id": job_id,
                "name": req.name,
                "kind": req.kind,
                "request": req.model_dump(),
                "output_dir": str(output),
                "command": cmd,
                "state": "running",
                "started_at": proc.started_at,
                "ended_at": None,
                "exit_code": None,
                "lines": [],
            }
            self.records[job_id], self.procs[job_id] = record, proc
            try:
                self._save(record)
            except OSError:
                proc.stop()
                raise
            threading.Thread(target=self._watch, args=(job_id,), daemon=True).start()
            return dict(record)

    def _watch(self, job_id: str) -> None:
        proc = self.procs[job_id]
        while True:
            snap = proc.snapshot()
            state = snap["state"]
            if state == "exited":
                state = "stopped" if snap["stopped"] else "succeeded" if snap["exit_code"] == 0 else "failed"
            with self.lock:
                record = self.records[job_id]
                record.update({k: snap[k] for k in ("lines", "exit_code", "ended_at")})
                record["state"] = state
                self._save(record)
            if not proc.running:
                return
            time.sleep(1)

    def stop(self, job_id: str) -> dict:
        with self.lock:
            self.get(job_id)
            if proc := self.procs.get(job_id):
                proc.stop()
            return {"ok": True}

    def list(self) -> list[dict]:
        with self.lock:
            return [
                {k: v for k, v in record.items() if k not in ("lines", "command")}
                for record in sorted(self.records.values(), key=lambda r: r["started_at"], reverse=True)
            ][:100]


token = ""
jobs: Jobs | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global token, jobs
    token = load_token()
    jobs = Jobs(STATE_DIR, OUTPUT_DIR)
    print(f"\nGPU agent ready. Paste this token into Train & Eval → Connect:\n\n    {token}\n", flush=True)
    yield
    for proc in jobs.procs.values():
        if proc.running:
            proc.stop()
    # Allow ConsoleProcess's interrupt/terminate/kill sequence to finish before uvicorn exits.
    deadline = time.monotonic() + 26
    while any(p.running for p in jobs.procs.values()) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)


def auth(x_panel_token: str = Header(default="")) -> None:
    if not token or not hmac.compare_digest(x_panel_token.encode(), token.encode()):
        raise HTTPException(401, "Wrong GPU agent token")


app = FastAPI(title="LeRobot GPU Agent", lifespan=lifespan, dependencies=[Depends(auth)])


def manager() -> Jobs:
    if jobs is None:
        raise HTTPException(503, "GPU agent is starting")
    return jobs


def gpu_status() -> tuple[list[dict], str | None]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        import csv

        gpus = []
        for row in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
            index, name, used, total, utilization, temperature = row

            def number(value: str) -> float | None:
                try:
                    return float(value)
                except ValueError:
                    return None

            gpus.append(
                {
                    "index": index,
                    "name": name,
                    "used_mb": number(used),
                    "total_mb": number(total),
                    "utilization": number(utilization),
                    "temperature": number(temperature),
                }
            )
        return gpus, None
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return [], f"GPU monitoring unavailable: {e}"


@app.get("/status")
def status() -> dict:
    gpus, error = gpu_status()
    disk = shutil.disk_usage(REPO_ROOT)
    return {
        "agent": "gpu",
        "hostname": os.uname().nodename,
        "repo": str(REPO_ROOT),
        "gpus": gpus,
        "gpu_error": error,
        "disk_free": disk.free,
        "jobs": manager().list(),
    }


@app.post("/jobs")
def start(req: JobRequest) -> dict:
    return manager().start(req)


@app.get("/jobs/{job_id}")
def detail(job_id: str) -> dict:
    record = manager().get(job_id)
    output = Path(record["output_dir"])
    record["checkpoints"] = sorted(
        str(p.parent) for p in output.glob("checkpoints/*/pretrained_model/config.json")
    )
    result = output / "eval_info.json"
    try:
        record["result"] = json.loads(result.read_text()) if result.is_file() else None
    except (OSError, ValueError):
        record["result"] = None
    if record["kind"] == "robot_eval":
        record["dataset_path"] = str(lerobot_home() / record["request"]["dataset"])
    return record


@app.post("/jobs/{job_id}/stop")
def stop(job_id: str) -> dict:
    return manager().stop(job_id)
