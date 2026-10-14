"""Feedback refresh and protection recovery for AlohaMini evaluation."""

import time
from copy import deepcopy


def safety_snapshot(robot):
    status = deepcopy(getattr(robot, "latest_safety_status", {}))
    if status:
        received_at = getattr(robot, "_last_safety_received_at", None)
        status["feedback_age_s"] = None if received_at is None else time.monotonic() - received_at
    return status


def hold_action(observation):
    action = {key: value for key, value in observation.items() if key.endswith(".pos")}
    action.update({"x.vel": 0.0, "y.vel": 0.0, "theta.vel": 0.0})
    if "lift_axis.height_mm" in observation:
        action["lift_axis.height_mm"] = observation["lift_axis.height_mm"]
    return action


def stop_inference(engine):
    """Require an idle worker before resetting the policy or starting a new episode."""
    engine.stop()
    thread = getattr(engine, "_rtc_thread", None)
    if thread is not None and thread.is_alive():
        raise RuntimeError("RTC inference is still running; refusing to reset or restart it")


class EvaluationSafetyGuard:
    """Separate control availability, active protection, and prediction invalidation."""

    def __init__(self):
        self._host_id = None
        self._joint_hold_events = 0

    @property
    def context(self):
        return self._host_id, self._joint_hold_events

    def acknowledge(self, status):
        self._host_id = status["host_session_id"]
        self._joint_hold_events = status["joint_hold_events"]

    def reason(self, robot):
        status = getattr(robot, "latest_safety_status", {})
        received_at = getattr(robot, "_last_safety_received_at", None)
        if status.get("version") != 1:
            return "Host 未提供保护状态，请更新树莓派 Host"
        if not getattr(robot, "command_permitted", True):
            return "控制权由其他客户端持有"
        if not getattr(robot, "control_feedback_valid", True):
            return "Host 反馈中断或过期"
        feedback_timeout = status.get("command_watchdog_timeout_s", 1.0)
        if received_at is None or time.monotonic() - received_at > feedback_timeout:
            return "Host 反馈中断"
        if self._host_id is not None and self._host_id != status["host_session_id"]:
            return "Host 已重新启动"
        self.acknowledge(status)
        if status["joint_holds"]:
            return "关节保护：" + ", ".join(status["joint_holds"])
        return None

    def check_observation(self, robot, observation, *, refresh=False):
        """Refresh after blocking inference or prolonged loss, not on a dataset age limit."""
        if refresh or not getattr(robot, "control_feedback_valid", True):
            observation = robot.refresh_observation()
        reason = self.reason(robot)
        if reason not in (None, "Host 反馈中断或过期", "Host 反馈中断") and not reason.startswith(
            "关节保护："
        ):
            raise RuntimeError(f"{reason}；评估停止。")
        return observation, reason


class EngageRamp:
    """Ramp arm targets from the follower's current pose to the policy's, so the Host engage gate opens.

    The Host keeps an arm limp until a command lands within its engage tolerance of the follower pose.
    A policy's first targets are usually further away than that, so after every (re)start this slews
    each ``.pos`` target at most ``max_step`` per tick, starting from the observed pose, and passes
    actions through untouched once every joint has caught up with the policy.
    """

    def __init__(self, max_step: float):
        self.max_step = max_step
        self._last = None

    def reset(self):
        self._last = None

    def apply(self, action, observation):
        if self.max_step <= 0:
            return action
        keys = [k for k in action if k.endswith(".pos") and k in observation]
        if self._last is None:
            self._last = {k: float(observation[k]) for k in keys}
        if not self._last:
            return action
        ramped = dict(action)
        caught_up = True
        for key in keys:
            delta = float(action[key]) - self._last[key]
            if abs(delta) > self.max_step:
                caught_up = False
                delta = self.max_step if delta > 0 else -self.max_step
            ramped[key] = self._last[key] + delta
            self._last[key] = ramped[key]
        if caught_up:
            self._last = {}
        return ramped
