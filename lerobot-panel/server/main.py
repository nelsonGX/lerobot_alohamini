"""LeRobot recording panel: FastAPI backend + the exported Next.js UI.

Run from the repo root with ./panel (see lerobot-panel/README.md).
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import dataset_store
from config import EDIT_DATASET, REPO_ROOT, STATIC_DIR, Settings, load_settings, save_settings
from preflight import run_checks
from recorder import Recorder, RecorderBusy, load_history

app = FastAPI(title="LeRobot Panel")
recorder = Recorder()


# ---------- settings & checks ----------


@app.get("/api/settings")
def get_settings() -> Settings:
    return load_settings()


@app.put("/api/settings")
def put_settings(settings: Settings) -> Settings:
    save_settings(settings)
    return settings


@app.get("/api/preflight")
def preflight() -> dict:
    return {"checks": run_checks(load_settings(), recorder_pid=recorder.pid()), "checked_at": time.time()}


# ---------- recording ----------


class StartRequest(BaseModel):
    operator: str = Field(default="", max_length=60)
    dataset: str
    task: str = Field(min_length=3, max_length=500)
    num_episodes: int = Field(ge=1, le=500)
    episode_time_s: int = Field(ge=3, le=3600)
    reset_time_s: int = Field(ge=0, le=600)
    fps: int = Field(ge=1, le=120)


@app.post("/api/recorder/start")
def start_recording(req: StartRequest) -> dict:
    if not dataset_store.valid_repo_id(req.dataset):
        raise HTTPException(400, "Dataset name must look like namespace/name (letters, digits, _ . -)")
    if job.running and job.repo_id == req.dataset:
        raise HTTPException(409, "This dataset is being edited right now; wait for it to finish.")
    resume = dataset_store.dataset_exists(req.dataset)
    fps = req.fps
    if resume:
        # Appending must keep the dataset's original frame rate.
        fps = dataset_store.summarize(req.dataset, dataset_store.dataset_root(req.dataset), with_size=False)["fps"]
    try:
        session = recorder.start(
            settings=load_settings(),
            operator=req.operator.strip(),
            dataset=req.dataset,
            task=req.task.strip(),
            num_episodes=req.num_episodes,
            episode_time_s=req.episode_time_s,
            reset_time_s=req.reset_time_s,
            fps=fps,
            resume=resume,
        )
    except RecorderBusy as e:
        raise HTTPException(409, str(e)) from e
    return {"id": session.id, "resume": resume}


class ControlRequest(BaseModel):
    action: str = Field(pattern="^(next|rerecord|stop|discard_stop|abort)$")


@app.post("/api/recorder/control")
def control(req: ControlRequest) -> dict:
    try:
        if req.action == "abort":
            recorder.abort()
        else:
            recorder.send_control(req.action)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.get("/api/recorder")
def recorder_state(since: int = 0) -> dict:
    return recorder.snapshot(since)


@app.get("/api/history")
def history() -> list[dict]:
    return load_history()


# ---------- datasets ----------


def _not_found(e: Exception) -> HTTPException:
    return HTTPException(404, str(e))


@app.get("/api/datasets")
def datasets() -> dict:
    return {"datasets": dataset_store.list_datasets(), "recording": recorder.recording_dataset()}


@app.get("/api/dataset")
def dataset(repo: str) -> dict:
    try:
        detail = dataset_store.dataset_detail(repo)
    except (dataset_store.DatasetNotFound, FileNotFoundError) as e:
        raise _not_found(e) from e
    return {**detail, "recording": recorder.recording_dataset() == repo}


@app.get("/api/episode")
def episode(repo: str, ep: int) -> JSONResponse:
    try:
        data = dataset_store.episode_data(repo, ep)
    except (dataset_store.DatasetNotFound, FileNotFoundError) as e:
        raise _not_found(e) from e
    return JSONResponse(data)


@app.get("/api/video")
def video(repo: str, camera: str, chunk: int, file: int) -> FileResponse:
    try:
        path = dataset_store.video_path(repo, camera, chunk, file)
    except (dataset_store.DatasetNotFound, FileNotFoundError) as e:
        raise _not_found(e) from e
    return FileResponse(path, media_type="video/mp4")


def _guard_editable(repo: str) -> None:
    if recorder.recording_dataset() == repo:
        raise HTTPException(409, "This dataset is being recorded right now.")
    if job.running:
        raise HTTPException(409, f"Another edit is running on {job.repo_id}; try again when it finishes.")
    if not dataset_store.dataset_exists(repo):
        raise HTTPException(404, f"Dataset {repo} not found")


class DeleteEpisodesRequest(BaseModel):
    repo: str
    episodes: list[int] = Field(min_length=1)


@app.post("/api/dataset/delete-episodes")
def delete_episodes(req: DeleteEpisodesRequest) -> dict:
    _guard_editable(req.repo)
    job.start_delete_episodes(req.repo, sorted(set(req.episodes)))
    return {"ok": True}


class DeleteDatasetRequest(BaseModel):
    repo: str
    confirm: str


@app.post("/api/dataset/delete")
def delete_dataset(req: DeleteDatasetRequest) -> dict:
    if req.confirm != req.repo:
        raise HTTPException(400, "Type the dataset name exactly to confirm.")
    _guard_editable(req.repo)
    dataset_store.delete_dataset(req.repo)
    return {"ok": True}


@app.get("/api/job")
def job_state() -> dict:
    return job.snapshot()


class EditJob:
    """One dataset edit at a time (lerobot-edit-dataset can take minutes on long videos)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.repo_id: str | None = None
        self.description = ""
        self.status = "idle"  # idle | running | done | failed
        self.output: list[str] = []
        self.finished_at: float | None = None

    def start_delete_episodes(self, repo_id: str, episodes: list[int]) -> None:
        with self._lock:
            if self.running:
                raise HTTPException(409, "Another edit is already running.")
            self.running, self.repo_id, self.status, self.output = True, repo_id, "running", []
            self.description = f"Deleting episode{'s' if len(episodes) > 1 else ''} {', '.join(map(str, episodes))}"
            self.finished_at = None
        cmd = [
            EDIT_DATASET,
            "--repo_id", repo_id,
            "--operation.type", "delete_episodes",
            "--operation.episode_indices", "[" + ", ".join(map(str, episodes)) + "]",
        ]  # fmt: skip
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()

    def _run(self, cmd: list[str]) -> None:
        try:
            proc = subprocess.Popen(
                cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self.output = (self.output + [line.rstrip()])[-200:]
            code = proc.wait()
            self.status = "done" if code == 0 else "failed"
        except OSError as e:
            self.output.append(str(e))
            self.status = "failed"
        finally:
            self.running = False
            self.finished_at = time.time()

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "repo_id": self.repo_id,
            "description": self.description,
            "status": self.status,
            "output": self.output[-40:],
            "finished_at": self.finished_at,
        }


job = EditJob()


# ---------- frontend ----------


class SPAStaticFiles(StaticFiles):
    """Serve Next's static export: /datasets -> datasets.html, unknown paths -> index."""

    async def get_response(self, path, scope):
        if path not in ("", ".") and not Path(path).suffix:
            candidate = Path(self.directory) / f"{path}.html"
            if candidate.exists():
                path = f"{path}.html"
        return await super().get_response(path, scope)


if STATIC_DIR.exists():
    app.mount("/", SPAStaticFiles(directory=STATIC_DIR, html=True), name="ui")
else:

    @app.get("/")
    def no_ui() -> JSONResponse:
        return JSONResponse({"error": "UI not built. Run ./panel (it builds the UI) or `npm run build` in lerobot-panel."})
