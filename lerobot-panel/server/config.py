"""Paths and persisted panel settings (shared by everyone using the panel)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from pydantic import BaseModel, Field

PANEL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PANEL_DIR.parent
DATA_DIR = PANEL_DIR / ".data"
STATIC_DIR = PANEL_DIR / "out"
RECORD_SCRIPT = REPO_ROOT / "examples" / "alohamini" / "record_bi.py"


def _venv_bin(name: str) -> str:
    """Prefer the repo's uv venv so child processes see the same deps as ./record_server."""
    candidate = REPO_ROOT / ".venv" / "bin" / name
    return str(candidate) if candidate.exists() else name


PYTHON = _venv_bin("python")
EDIT_DATASET = _venv_bin("lerobot-edit-dataset")


def lerobot_home() -> Path:
    # Mirrors lerobot.utils.constants without importing torch-heavy modules.
    if "HF_LEROBOT_HOME" in os.environ:
        return Path(os.environ["HF_LEROBOT_HOME"]).expanduser()
    hf_home = Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser()
    return hf_home / "lerobot"


def calibration_home() -> Path:
    if "HF_LEROBOT_CALIBRATION" in os.environ:
        return Path(os.environ["HF_LEROBOT_CALIBRATION"]).expanduser()
    return lerobot_home() / "calibration"


LEADER_PORTS = {"left": "/dev/am_arm_leader_left", "right": "/dev/am_arm_leader_right"}


class Settings(BaseModel):
    jetson_ip: str = Field(default_factory=lambda: os.environ.get("JETSON_IP", "10.95.27.248"))
    robot_model: str = "alohamini1"
    teleop_id: str = "so101_leader_bi"
    arm_profile: str = "so-arm-5dof"
    default_namespace: str = "alohamini"
    default_fps: int = 30
    default_episode_time_s: int = 60
    default_reset_time_s: int = 10
    default_num_episodes: int = 10
    obs_port: int = 5556


_settings_lock = threading.Lock()
_SETTINGS_PATH = DATA_DIR / "settings.json"


def load_settings() -> Settings:
    with _settings_lock:
        if _SETTINGS_PATH.exists():
            try:
                return Settings.model_validate_json(_SETTINGS_PATH.read_text())
            except ValueError:
                pass
        return Settings()


def save_settings(settings: Settings) -> None:
    with _settings_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings.model_dump(), indent=2))
        tmp.replace(_SETTINGS_PATH)
