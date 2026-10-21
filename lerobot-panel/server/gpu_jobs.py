"""Validated GPU job specifications shared by the panel and GPU agent.

Commands are argument lists, never shell scripts. All output paths belong to the
agent; a panel cannot overwrite an existing training run by choosing its name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from config import PYTHON, REPO_ROOT
from pydantic import BaseModel, ConfigDict, Field, model_validator


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["train", "sim_eval", "robot_eval"]
    name: str = Field(default="run", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")
    dataset: str = Field(default="", max_length=200)
    dataset_root: str = Field(default="", max_length=1000)
    policy: Literal["act", "diffusion", "smolvla", "pi0", "pi05"] = "act"
    checkpoint: str = Field(default="", max_length=1000)
    steps: int = Field(default=20000, ge=1, le=10000000)
    batch_size: int = Field(default=8, ge=1, le=4096)
    save_freq: int = Field(default=5000, ge=1, le=10000000)
    num_workers: int = Field(default=4, ge=0, le=64)
    env: Literal["pusht", "aloha", "libero", "metaworld"] = "aloha"
    task: str = Field(default="AlohaInsertion-v0", min_length=1, max_length=500)
    episodes: int = Field(default=10, ge=1, le=10000)
    episode_time_s: int = Field(default=60, ge=1, le=600)
    reset_time_s: int = Field(default=10, ge=0, le=600)
    fps: int = Field(default=30, ge=1, le=60)
    robot_ip: str = Field(default="127.0.0.1", pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
    robot_model: Literal["alohamini1", "alohamini2", "alohamini2pro"] = "alohamini1"
    robot_ready: bool = False

    @model_validator(mode="after")
    def validate_job(self) -> JobRequest:
        if self.kind in ("train", "robot_eval"):
            import re

            if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*", self.dataset):
                raise ValueError("Dataset must be a namespace/name (on the GPU server or Hugging Face).")
        if self.kind != "train" and not self.checkpoint:
            raise ValueError("Choose a checkpoint or enter a model repository ID.")
        if self.kind == "robot_eval" and not self.robot_ready:
            raise ValueError("Confirm that the robot workspace is clear and an operator is ready.")
        return self


def command(req: JobRequest, output: Path) -> list[str]:
    if req.kind == "train":
        cmd = [PYTHON, "-m", "lerobot.scripts.lerobot_train"]
        cmd += [f"--policy.path={req.checkpoint}" if req.checkpoint else f"--policy.type={req.policy}"]
        cmd += [
            f"--dataset.repo_id={req.dataset}",
            "--policy.device=cuda",
            "--policy.push_to_hub=false",
            f"--steps={req.steps}",
            f"--batch_size={req.batch_size}",
            f"--save_freq={req.save_freq}",
            f"--num_workers={req.num_workers}",
            f"--output_dir={output}",
            f"--job_name={req.name}",
            "--wandb.enable=false",
            "--env_eval_freq=0",
        ]
        if req.dataset_root:
            cmd += [f"--dataset.root={req.dataset_root}"]
        return cmd
    if req.kind == "sim_eval":
        return [
            PYTHON,
            "-m",
            "lerobot.scripts.lerobot_eval",
            f"--policy.path={req.checkpoint}",
            "--policy.device=cuda",
            f"--env.type={req.env}",
            f"--env.task={req.task}",
            f"--eval.n_episodes={req.episodes}",
            f"--eval.batch_size={min(req.batch_size, req.episodes)}",
            f"--output_dir={output}",
        ]
    return [
        PYTHON,
        str(REPO_ROOT / "examples/alohamini/evaluate_bi.py"),
        f"--policy.path={req.checkpoint}",
        f"--dataset.repo_id={req.dataset}",
        "--dataset.push_to_hub=false",
        f"--dataset.single_task={req.task}",
        f"--robot.remote_ip={req.robot_ip}",
        f"--robot.robot_model={req.robot_model}",
        f"--eval.n_episodes={req.episodes}",
        f"--eval.episode_time_s={req.episode_time_s}",
        f"--eval.reset_time_s={req.reset_time_s}",
        f"--fps={req.fps}",
    ]
