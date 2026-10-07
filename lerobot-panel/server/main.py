"""LeRobot recording panel: FastAPI backend + the exported Next.js UI.

Run from the repo root with ./panel (see lerobot-panel/README.md).
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import dataset_store
from camera_feed import CameraFeed
from live import Hub
from config import EDIT_DATASET, REPO_ROOT, STATIC_DIR, Settings, load_settings, save_settings
from preflight import run_checks
from procs import AgentError, Controls, ProcBusy, agent_call, read_token, save_token
from recorder import Recorder, RecorderBusy, load_history

recorder = Recorder()
controls = Controls()
camera_feed = CameraFeed()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Programs run from the panel are in their own sessions; don't leave them behind.
    controls.shutdown()
    if recorder.pid():
        recorder.abort()


app = FastAPI(title="LeRobot Panel", lifespan=lifespan)


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
    return _preflight()


def _preflight() -> dict:
    settings = load_settings()
    checks = run_checks(settings, recorder_pid=recorder.pid(), panel_users=controls.leader_users(),
                        panel_pids=controls.leader_pids())  # fmt: skip
    host = controls.get("host")
    for c in checks:
        if c["id"] != "jetson" or c["status"] == "ok":
            continue
        if host and host.running:
            c.update(status="warn", detail="The robot host is starting on the Jetson…", hint="", action="")
        elif c["action"] == "start_host" and not read_token():
            c.update(action="setup_jetson", hint="Connect the panel to the Jetson on the Robot page, then start the host there.")
    return {"checks": checks, "checked_at": time.time()}


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
    if users := controls.leader_users():
        raise HTTPException(409, f"{users[0]} is using the leader arms. Stop it on the Robot page first.")
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


# ---------- robot: Jetson host, teleoperation, calibration ----------


def _proc_error(e: Exception) -> HTTPException:
    return HTTPException(409 if isinstance(e, ProcBusy) else 400, str(e))


@app.get("/api/robot")
def robot_state() -> dict:
    return {"jetson": controls.jetson_status(load_settings()), "procs": controls.overview(),
            "recording": recorder.recording_dataset()}  # fmt: skip


@app.get("/api/host/telemetry")
def host_telemetry() -> dict:
    """What the host's terminal dashboard shows (arm positions, currents, link state), streamed from the Jetson agent."""
    return controls.host_telemetry(load_settings())


CAMERA_STREAM_PORT = 5557  # the host's --camera_stream port


@app.get("/api/cameras")
def cameras() -> dict:
    """Cameras currently streaming from the host (polling this keeps the preview connection open)."""
    settings = load_settings()
    camera_feed.touch(settings.jetson_ip, CAMERA_STREAM_PORT)
    return camera_feed.status()


@app.get("/api/camera/{name}")
async def camera_mjpeg(name: str) -> StreamingResponse:
    settings = load_settings()

    async def gen():
        last = -1
        while True:
            camera_feed.touch(settings.jetson_ip, CAMERA_STREAM_PORT)
            item = camera_feed.frame(name)
            if item is None or item[1] == last:
                await asyncio.sleep(0.03)
                continue
            jpeg, last = item
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store"})


class JetsonSetupRequest(BaseModel):
    token: str = Field(min_length=8, max_length=200, pattern=r"^\S+$")


@app.post("/api/jetson/setup")
def jetson_setup(req: JetsonSetupRequest) -> dict:
    """Check the token the agent printed, then remember it."""
    settings = load_settings()
    try:
        info = agent_call(settings.jetson_ip, settings.agent_port, "GET", "/status", token=req.token)
    except AgentError as e:
        raise HTTPException(400, str(e)) from e
    save_token(req.token)
    return {"hostname": info["hostname"], "repo": info["repo"]}


class HostStartRequest(BaseModel):
    cameras: bool = False


@app.post("/api/host/start")
def host_start(req: HostStartRequest) -> dict:
    settings = load_settings()
    if settings.host_cameras != req.cameras:
        save_settings(settings := settings.model_copy(update={"host_cameras": req.cameras}))
    try:
        controls.start_host(settings, req.cameras)
    except (RuntimeError, OSError) as e:
        raise _proc_error(e) from e
    return {"ok": True}


@app.post("/api/host/stop")
def host_stop() -> dict:
    if recorder.recording_dataset():
        raise HTTPException(409, "A recording is running. Stop it before stopping the robot host.")
    try:
        return {"result": controls.stop_host(load_settings())}
    except (RuntimeError, OSError) as e:
        raise _proc_error(e) from e


@app.post("/api/teleop/start")
def teleop_start() -> dict:
    try:
        controls.start_teleop(load_settings(), recorder_active=recorder.recording_dataset() is not None)
    except (RuntimeError, OSError) as e:
        raise _proc_error(e) from e
    return {"ok": True}


@app.post("/api/restore/start")
def restore_start() -> dict:
    try:
        controls.start_restore(load_settings(), recorder_active=recorder.recording_dataset() is not None)
    except (RuntimeError, OSError) as e:
        raise _proc_error(e) from e
    return {"ok": True}


class CalibrateRequest(BaseModel):
    target: str = Field(pattern="^(leader|follower)$")


@app.post("/api/calibrate/start")
def calibrate_start(req: CalibrateRequest) -> dict:
    try:
        controls.start_calibration(load_settings(), req.target, recorder_active=recorder.recording_dataset() is not None)
    except (RuntimeError, OSError) as e:
        raise _proc_error(e) from e
    return {"ok": True}


def _proc(name: str):
    try:
        return controls.get(name)
    except KeyError as e:
        raise HTTPException(404, f"Unknown program {name}") from e


@app.get("/api/proc/{name}")
def proc_state(name: str, version: int = -1) -> dict:
    p = _proc(name)
    return {"proc": None if p is None else p.snapshot(version)}


class InputRequest(BaseModel):
    text: str = Field(max_length=200)


@app.post("/api/proc/{name}/input")
def proc_input(name: str, req: InputRequest) -> dict:
    p = _proc(name)
    if p is None:
        raise HTTPException(409, "Not running")
    try:
        p.write(req.text)
    except (RuntimeError, OSError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.post("/api/proc/{name}/stop")
def proc_stop(name: str) -> dict:
    if name == "host":
        return host_stop()
    p = _proc(name)
    if p is not None:
        try:
            p.stop()
        except (RuntimeError, OSError) as e:
            raise _proc_error(e) from e
    return {"ok": True}


# ---------- live state over one WebSocket ----------

hub = Hub()


def _recorder_log_stream():
    """Per-connection: only the log lines this browser hasn't seen; `reset` when a new session starts."""
    last = {"id": 0, "session": None}

    def next_lines():
        snap = recorder.snapshot(last["id"])
        sid = (snap["session"] or {}).get("id")
        reset = sid != last["session"]
        if reset:
            snap = recorder.snapshot(0)
            last["session"] = sid
        lines = snap["log"]
        if lines:
            last["id"] = lines[-1]["id"]
        return {"reset": reset, "lines": lines} if (reset or lines) else None

    return next_lines


def _proc_snapshot(name: str) -> dict:
    p = _proc(name)
    return {"proc": None if p is None else p.snapshot(-1)}


hub.topic("recorder", 0.3, lambda: recorder.snapshot(10**12))
hub.per_client("recorder.log", 0.3, _recorder_log_stream)
hub.topic("robot", 1.0, robot_state)
hub.topic("host", 0.25, host_telemetry)
hub.topic("cameras", 1.0, cameras)
hub.topic("preflight", 5.0, _preflight)
hub.topic("job", 1.0, lambda: job.snapshot())
hub.family("proc", 0.4, _proc_snapshot, lambda name: name in controls.procs)


@app.websocket("/api/ws")
async def live(sock: WebSocket) -> None:
    await hub.serve(sock)


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
