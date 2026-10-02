"""Keep follower arms limp until the incoming command matches their current pose.

Feetech servos enable torque on the first Goal_Position write, so without this gate the first
leader command (at startup, after a watchdog stop or after an overcurrent release) snaps the
follower to wherever the leader happens to be. The gate drops an arm's position targets until
every joint of the command is within ``tolerance_deg`` of the follower, then lets them through.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .alohamini import _position_delta_degrees

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass
class ArmEngageStatus:
    engaged: bool = False
    worst_motor: str | None = None
    worst_error_deg: float | None = None


class ArmEngageGate:
    def __init__(self, robot: Any, tolerance_deg: float = 15.0):
        self.robot = robot
        self.tolerance_deg = tolerance_deg
        self.status: dict[str, ArmEngageStatus] = {}

    def disengage(self, reason: str) -> None:
        was_engaged = [side for side, st in self.status.items() if st.engaged]
        for st in self.status.values():
            st.engaged = False
        if was_engaged:
            logger.warning(
                "%s disengaged (%s); match the leader to the follower pose to resume",
                " & ".join(side.upper() for side in was_engaged),
                reason,
            )

    def filter(self, action: dict[str, float], observation: dict[str, Any]) -> dict[str, float]:
        """Return ``action`` without the position targets of arms that are not engaged yet."""
        filtered = dict(action)
        for side in ("left", "right"):
            prefix = f"arm_{side}_"
            if not any(k.startswith(prefix) and k.endswith(".pos") for k in action):
                continue
            st = self.status.setdefault(side, ArmEngageStatus())
            if st.engaged:
                continue
            bus = self.robot.left_bus if side == "left" else self.robot.right_bus
            motors = [m for m in bus.motors if m.startswith(prefix)] if bus is not None else []
            keys = [f"{m}.pos" for m in motors]

            worst_motor, worst_err, complete = None, 0.0, True
            for motor, key in zip(motors, keys, strict=True):
                if key not in action or key not in observation:
                    complete = False
                    continue
                err = abs(_position_delta_degrees(bus, motor, float(action[key]) - float(observation[key])))
                if worst_motor is None or err > worst_err:
                    worst_motor, worst_err = motor, err
            st.worst_motor, st.worst_error_deg = worst_motor, worst_err if worst_motor else None

            if complete and worst_err <= self.tolerance_deg:
                st.engaged = True
                logger.info("%s arm engaged (max mismatch %.1f°)", side.upper(), worst_err)
                continue
            for key in keys:
                filtered.pop(key, None)
        return filtered
