"""Robot-side agent: run it on the Jetson (./agent) and the panel controls the robot through it.

Replaces SSH. It starts/stops the robot host and follower calibration in pseudo-terminals
(same ConsoleProcess the panel uses locally), and reports basic Jetson health.
Every request needs the token in `.data/agent_token` (created on first run, printed at startup).
"""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import shlex
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from config import DATA_DIR, REPO_ROOT
from procs import HOST_MODULE, ConsoleProcess

TOKEN_FILE = DATA_DIR / "agent_token"
CALIBRATE_MODULE = "lerobot.robots.alohamini.alohamini_calibrate"
BOOT = time.time()


def load_token() -> str:
    if not TOKEN_FILE.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(secrets.token_urlsafe(24))
        TOKEN_FILE.chmod(0o600)
    return TOKEN_FILE.read_text().strip()


TOKEN = load_token()
procs: dict[str, ConsoleProcess | None] = {"host": None, "calibrate": None}
TITLES = {"host": "Robot host", "calibrate": "Follower arm calibration"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    print(f"\nPanel agent ready. Paste this token into the panel (Robot -> Jetson):\n\n    {TOKEN}\n", flush=True)
    yield
    for p in procs.values():
        if p and p.running:
            p.stop()


app = FastAPI(title="LeRobot Panel Agent", lifespan=lifespan)


def auth(x_panel_token: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_panel_token.encode(), TOKEN.encode()):
        raise HTTPException(401, "Wrong agent token")


def _running(name: str) -> ConsoleProcess | None:
    p = procs[name]
    return p if p and p.running else None


def _proc(name: str) -> ConsoleProcess | None:
    if name not in procs:
        raise HTTPException(404, f"Unknown program {name}")
    return procs[name]


# ---------- health ----------


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _cpu_percent(interval: float = 0.2) -> float | None:
    def sample() -> tuple[int, int]:
        f = _read("/proc/stat").splitlines()[0].split()[1:]
        vals = [int(x) for x in f]
        return sum(vals), vals[3] + (vals[4] if len(vals) > 4 else 0)  # total, idle+iowait

    try:
        t1, i1 = sample()
        time.sleep(interval)
        t2, i2 = sample()
        return round(100 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)
    except (ValueError, IndexError):
        return None


def _meminfo() -> dict:
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if v:
            info[k] = int(v.split()[0]) * 1024
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    return {"total": total, "used": total - avail}


def _temps() -> list[dict]:
    out = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        t = _read(str(zone / "temp"))
        if t.lstrip("-").isdigit():
            out.append({"name": _read(str(zone / "type")) or zone.name, "c": round(int(t) / 1000, 1)})
    return out


def _devices() -> list[dict]:
    """Serial/camera devices the robot needs (udev symlinks like /dev/am_arm_follower_left)."""
    names = sorted(p.name for p in Path("/dev").glob("am_*"))
    out = [{"name": f"/dev/{n}", "ok": Path(f"/dev/{n}").exists()} for n in names]
    out += [{"name": f"/dev/{p.name}", "ok": True} for p in sorted(Path("/dev").glob("video*"))[:12]]
    return out


def _host_pids() -> list[int]:
    res = subprocess.run(["pgrep", "-f", HOST_MODULE], capture_output=True, text=True)
    return [int(x) for x in res.stdout.split() if int(x) != os.getpid()]


@app.get("/ping")
def ping() -> dict:
    return {"agent": True}


@app.get("/status", dependencies=[Depends(auth)])
def status() -> dict:
    disk = shutil.disk_usage(REPO_ROOT)
    return {
        "procs": {n: None if p is None else {"state": p.snapshot(p.screen.version)["state"]} for n, p in procs.items()},
        "host_pids": _host_pids(),
        "hostname": os.uname().nodename,
        "repo": str(REPO_ROOT),
        "uptime_s": int(float(_read("/proc/uptime").split()[0] or 0)),
        "agent_uptime_s": int(time.time() - BOOT),
        "load": list(os.getloadavg()),
        "cpu_percent": _cpu_percent(),
        "cpus": os.cpu_count(),
        "mem": _meminfo(),
        "disk": {"total": disk.total, "used": disk.used},
        "temps": _temps(),
        "devices": _devices(),
    }


# ---------- programs ----------


class StartRequest(BaseModel):
    name: str = Field(pattern="^(host|calibrate)$")
    robot_model: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    host_args: str = Field(default="", pattern=r"^[A-Za-z0-9_.=\- ]*$")
    cameras: bool = False


@app.post("/start", dependencies=[Depends(auth)])
def start(req: StartRequest) -> dict:
    if _running("host") or _running("calibrate") or _host_pids():
        raise HTTPException(409, "The robot host or a calibration is already running on the Jetson.")
    model = shlex.quote(req.robot_model)
    if req.name == "host":
        args = f"--robot_model {model} {req.host_args}" + ("" if req.cameras else " --no_cameras")
        # Piping through cat makes stdout a pipe, so the host prints plain logs instead of its full-screen dashboard.
        script = f"uv run python -m {HOST_MODULE} {args} 2>&1 | cat"
    else:
        no_base = " --no_base" if "--no_base" in req.host_args else ""
        script = f"uv run python -m {CALIBRATE_MODULE} --robot_model {model}{no_base}"
    env = {"PATH": f"{Path.home()}/.local/bin:{Path.home()}/.cargo/bin:{os.environ.get('PATH', '')}"}
    try:
        procs[req.name] = ConsoleProcess(req.name, TITLES[req.name], ["bash", "-c", "set -o pipefail; " + script], env=env)
    except OSError as e:
        raise HTTPException(500, str(e)) from e
    return procs[req.name].snapshot()


@app.get("/proc/{name}", dependencies=[Depends(auth)])
def proc_state(name: str, version: int = -1) -> dict:
    p = _proc(name)
    return {"proc": None if p is None else p.snapshot(version)}


class InputRequest(BaseModel):
    text: str = Field(max_length=200)


@app.post("/proc/{name}/input", dependencies=[Depends(auth)])
def proc_input(name: str, req: InputRequest) -> dict:
    p = _proc(name)
    if p is None:
        raise HTTPException(409, "Not running")
    try:
        p.write(req.text)
    except (RuntimeError, OSError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.post("/proc/{name}/stop", dependencies=[Depends(auth)])
def proc_stop(name: str) -> dict:
    p = _proc(name)
    if p is not None:
        p.stop()
    return {"ok": True}


@app.post("/host/kill", dependencies=[Depends(auth)])
def host_kill() -> dict:
    """Interrupt a robot host that was started from a terminal on the Jetson."""
    pids = _host_pids()
    for pid in pids:
        try:
            os.kill(pid, 2)
        except ProcessLookupError:
            pass
    return {"result": "stopping" if pids else "not running"}



# ---------- live stream ----------


@app.websocket("/ws")
async def stream(sock: WebSocket) -> None:
    """Pushes program screens when they change and Jetson health every 2 s. Controls stay plain POSTs."""
    if not hmac.compare_digest(sock.query_params.get("token", "").encode(), TOKEN.encode()):
        await sock.close(code=4401)
        return
    await sock.accept()
    sent: dict[str, tuple] = {}
    versions: dict[str, int] = {}
    last_status = 0.0
    try:
        while True:
            for name, p in procs.items():
                if p is None:
                    continue
                snap = p.snapshot(versions.get(name, -1))
                key = (snap["version"], snap["state"], str(snap["prompt"]))
                if sent.get(name) != key:
                    sent[name], versions[name] = key, snap["version"]
                    await sock.send_json({"type": "proc", "name": name, "proc": snap})
            if time.time() - last_status >= 2:
                last_status = time.time()
                await sock.send_json({"type": "status", "data": await asyncio.to_thread(status)})
            await asyncio.sleep(0.2)
    except (WebSocketDisconnect, RuntimeError):
        return
