"""Hardware-free stand-ins for the AlohaMini client and the leader arms (record_panel.py --simulate).

They let the whole recording flow (panel, layouts, save/discard, metadata, quality flags) be exercised on a
laptop without the Jetson or the arms. They are not used on the real robot.
"""

from __future__ import annotations

import math
import random
import time
from types import SimpleNamespace

import numpy as np

JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
CAMERAS = ("forward", "overhead")
IMG_H, IMG_W = 48, 64


class SimRobot:
    """Looks like AlohaMiniClient to the recording loop. `fault` injects the failures the quality flags look for."""

    name = "alohamini_sim"

    def __init__(self, fps: int = 30) -> None:
        self.fps = fps
        self.config = SimpleNamespace(connect_timeout_s=2.0)
        self.fault: str | None = None  # None | "stall" | "black_camera"
        self.logs: dict = {}
        self._connected = False
        self._sequence = 0
        self._latency: float | None = None
        self._rng = random.Random(0)
        self._last_obs: dict | None = None
        self._state = {f"arm_{side}_{j}.pos": 0.0 for side in ("left", "right") for j in JOINTS}
        self._base = {"x.vel": 0.0, "y.vel": 0.0, "theta.vel": 0.0, "lift_axis.height_mm": 0.0}

    @property
    def action_features(self) -> dict[str, type]:
        return dict.fromkeys((*self._state, *self._base), float)

    @property
    def observation_features(self) -> dict[str, type | tuple]:
        return {**self.action_features, **dict.fromkeys(CAMERAS, (IMG_H, IMG_W, 3))}

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def observation_sequence(self) -> int:
        return self._sequence

    @property
    def last_observation_latency_s(self) -> float | None:
        return self._latency

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def get_observation(self) -> dict:
        if self.fault == "stall":
            time.sleep(0.4)  # the host went quiet: no new data, the cached observation is returned
            if self._last_obs is not None:
                return dict(self._last_obs)
        self._sequence += 1
        self._latency = 0.012 + self._rng.random() * 0.01
        t = time.monotonic()
        obs: dict = {**self._state, **self._base}
        for i, cam in enumerate(CAMERAS):
            img = np.full((IMG_H, IMG_W, 3), 40 + 20 * i, dtype=np.uint8)
            img[:, int(t * 20) % IMG_W : int(t * 20) % IMG_W + 4] = 220  # a moving bar so frames differ
            obs[cam] = np.zeros_like(img) if self.fault == "black_camera" and cam == "forward" else img
        self._last_obs = obs
        return dict(obs)

    def send_action(self, action: dict) -> dict:
        for key in self._state:
            if key in action:
                self._state[key] = float(action[key])
        return action

    def _from_keyboard_to_base_action(self, _pressed) -> dict:
        return dict.fromkeys(("x.vel", "y.vel", "theta.vel"), 0.0)

    def _from_keyboard_to_lift_action(self, _pressed) -> dict:
        return {"lift_axis.height_mm": 0.0}


class SimLeader:
    """Two arms swaying; `frozen` makes them stand still (the 'arms never moved' failure)."""

    def __init__(self) -> None:
        self.frozen = False
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def get_action(self) -> dict:
        t = 0.0 if self.frozen else time.monotonic()
        return {
            f"{side}_{j}.pos": 30.0 * math.sin(t * (0.6 + 0.2 * k) + (0 if side == "left" else 1.0))
            for side in ("left", "right")
            for k, j in enumerate(JOINTS)
        }


class SimKeyboard:
    is_connected = False

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def get_action(self) -> dict:
        return {}
