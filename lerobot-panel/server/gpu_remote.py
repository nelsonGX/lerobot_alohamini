"""Panel-to-GPU connection. Credentials stay on the panel server."""

from __future__ import annotations

import inspect
import json
import os
import threading
from functools import wraps

import requests
from config import DATA_DIR
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

CONNECTION_FILE = DATA_DIR / "gpu_connection.json"
LIVE_FILE = DATA_DIR / "gpu_robot_eval.json"
robot_lock = threading.RLock()
router = APIRouter(prefix="/api/gpu")


class Connection(BaseModel):
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$", max_length=253)
    port: int = Field(default=8766, ge=1, le=65535)
    token: str = Field(min_length=8, max_length=200, pattern=r"^\S+$")


def connection() -> Connection | None:
    try:
        return Connection.model_validate_json(CONNECTION_FILE.read_text())
    except (OSError, ValueError):
        return None


def call(method: str, path: str, body: dict | None = None, target: Connection | None = None) -> dict:
    target = target or connection()
    if target is None:
        raise HTTPException(409, "Connect a GPU server on Train & Eval first.")
    try:
        response = requests.request(
            method,
            f"http://{target.host}:{target.port}{path}",
            json=body,
            headers={"X-Panel-Token": target.token},
            timeout=(3, 10),
        )
    except requests.RequestException as e:
        raise HTTPException(
            502, "Cannot reach the GPU agent. Check the address and that ./gpu_agent is running."
        ) from e
    if response.status_code == 401:
        raise HTTPException(401, "GPU token rejected. Reconnect with the token printed by ./gpu_agent.")
    try:
        data = response.json()
    except ValueError as e:
        raise HTTPException(502, "The GPU agent returned an invalid response.") from e
    if not response.ok:
        raise HTTPException(response.status_code, data.get("detail", "GPU request failed"))
    return data


def assert_robot_idle() -> None:
    """Fail closed when a live eval was dispatched and the GPU can no longer be reached."""
    if not connection() and not LIVE_FILE.exists():
        return
    try:
        info = call("GET", "/status")
    except HTTPException:
        if LIVE_FILE.exists():
            raise HTTPException(
                409,
                "Cannot confirm live evaluation has stopped. Reconnect the GPU agent before controlling the robot.",
            ) from None
        return
    if any(j["kind"] == "robot_eval" and j["state"] in ("running", "stopping") for j in info["jobs"]):
        raise HTTPException(409, "Live evaluation is using the robot. Stop it in Train & Eval first.")
    LIVE_FILE.unlink(missing_ok=True)


def exclusive_robot(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with robot_lock:
            assert_robot_idle()
            return fn(*args, **kwargs)

    # FastAPI sees a wrapper in this module; resolve forward references using the
    # original endpoint's globals before it constructs the request schema.
    wrapped.__signature__ = inspect.signature(fn, eval_str=True)
    return wrapped


@router.get("/connection")
def get_connection() -> dict:
    target = connection()
    return {
        "configured": target is not None,
        "host": target.host if target else "",
        "port": target.port if target else 8766,
    }


@router.post("/connect")
def connect(req: Connection) -> dict:
    with robot_lock:
        current = connection()
        if current and (current.host, current.port) != (req.host, req.port):
            assert_robot_idle()
            try:
                previous = call("GET", "/status")
            except HTTPException:
                previous = None
            if previous and any(j["state"] in ("running", "stopping") for j in previous["jobs"]):
                raise HTTPException(409, "Finish or stop the current GPU job before changing servers.")
        info = call("GET", "/status", target=req)
        if info.get("agent") != "gpu":
            raise HTTPException(400, "This address is not a GPU agent. Use ./gpu_agent (default port 8766).")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONNECTION_FILE.with_suffix(".tmp")
        with open(tmp, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
            f.write(req.model_dump_json())
        tmp.replace(CONNECTION_FILE)
        return info


@router.get("/status")
def status() -> dict:
    return call("GET", "/status")


@router.get("/jobs/{job_id}")
def detail(job_id: str) -> dict:
    return call("GET", f"/jobs/{_job_id(job_id)}")


@router.get("/checkpoints")
def checkpoints() -> dict:
    return call("GET", "/checkpoints")


@router.post("/jobs/{job_id}/stop")
def stop(job_id: str) -> dict:
    return call("POST", f"/jobs/{_job_id(job_id)}/stop")


def _job_id(value: str) -> str:
    import re

    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise HTTPException(400, "Invalid GPU job ID")
    return value


def mark_live() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_FILE.write_text(json.dumps({"pending": True}))
