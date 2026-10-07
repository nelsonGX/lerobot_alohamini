"""Live terminal dashboard for the AlohaMini host (plain ANSI, no extra dependencies)."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import deque
from typing import Any

from lerobot.motors import MotorNormMode

_CURRENT_MA_PER_RAW_UNIT = 6.5
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


def _fg(code: int) -> str:
    return f"\x1b[38;5;{code}m"


CYAN, MAGENTA, GREEN, YELLOW, RED, GREY, WHITE = (
    _fg(51),
    _fg(213),
    _fg(84),
    _fg(221),
    _fg(203),
    _fg(244),
    _fg(255),
)

_LOGO = (
    "▄▀█ █░░ █▀█ █░█ ▄▀█ █▀▄▀█ █ █▄░█ █",
    "█▀█ █▄▄ █▄█ █▀█ █▀█ █░▀░█ █ █░▀█ █",
)
_SPARK = "▁▂▃▄▅▆▇█"
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def _gradient(text: str, colors: tuple[int, ...]) -> str:
    out = []
    for i, ch in enumerate(text):
        out.append(_fg(colors[i * len(colors) // max(len(text), 1)]) + ch)
    return "".join(out) + RESET


class _LogCapture(logging.Handler):
    def __init__(self, sink: deque[tuple[float, str, str]]):
        super().__init__(level=logging.INFO)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        color = RED if record.levelno >= logging.ERROR else YELLOW if record.levelno >= logging.WARNING else GREEN
        self._sink.append((time.time(), color, record.getMessage().splitlines()[0]))


class _StdoutCapture(io.TextIOBase):
    """Collect stray print() output into the log panel instead of corrupting the screen."""

    def __init__(self, sink: deque[tuple[float, str, str]]):
        self._sink = sink
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._sink.append((time.time(), GREY, line.strip()))
        return len(text)

    def flush(self) -> None:
        pass


class HostTui:
    def __init__(self, robot: Any, *, subtitle: str, refresh_hz: float = 10.0):
        self.robot = robot
        self.subtitle = subtitle
        self.refresh_s = 1.0 / refresh_hz
        self.started_at = time.monotonic()
        self.logs: deque[tuple[float, str, str]] = deque(maxlen=200)
        self.hz_history: deque[float] = deque(maxlen=40)
        self._loop_count = 0
        self._loop_ms_total = 0.0
        self._window_start = time.perf_counter()
        self._last_render = 0.0
        self._frame = 0
        self._active = False
        self._handler = _LogCapture(self.logs)
        self._real_stdout = sys.stdout

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._real_stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        self._real_stdout.flush()
        logging.getLogger().addHandler(self._handler)
        sys.stdout = _StdoutCapture(self.logs)
        self._active = True

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        sys.stdout = self._real_stdout
        logging.getLogger().removeHandler(self._handler)
        self._real_stdout.write("\x1b[?25h\x1b[?1049l")
        for _, _, line in list(self.logs)[-15:]:
            self._real_stdout.write(line + "\n")
        self._real_stdout.flush()

    # ------------------------------------------------------------------ updates
    def update(self, *, loop_ms: float, **state: Any) -> None:
        self._loop_count += 1
        self._loop_ms_total += loop_ms
        now = time.perf_counter()
        if now - self._window_start >= 0.5:
            self.hz_history.append(self._loop_count / (now - self._window_start))
            self._avg_loop_ms = self._loop_ms_total / self._loop_count
            self._loop_count = 0
            self._loop_ms_total = 0.0
            self._window_start = now
        if self._active and now - self._last_render >= self.refresh_s:
            self._last_render = now
            self._frame += 1
            try:
                self._render(**state)
            except Exception as e:  # never let the dashboard take the control loop down
                self.logs.append((time.time(), RED, f"TUI render error: {e}"))

    # ------------------------------------------------------------------ drawing
    def _render(
        self,
        *,
        observation: dict[str, Any],
        sent_action: dict[str, float],
        requested_action: dict[str, float],
        engage_status: dict[str, Any],
        currents_ma: dict[str, float],
        last_cmd_age_s: float | None,
        watchdog_active: bool,
        watchdog_events: int,
        target_source: str,
        owner: str | None,
    ) -> None:
        cols, rows = shutil.get_terminal_size((100, 40))
        width = max(min(cols, 120), 72)
        lines: list[str] = []

        # Header
        uptime = int(time.monotonic() - self.started_at)
        up = f"{uptime // 3600:02d}:{uptime % 3600 // 60:02d}:{uptime % 60:02d}"
        spin = CYAN + _SPINNER[self._frame % len(_SPINNER)] + RESET
        lines.append("")
        lines.append(f"  {_gradient(_LOGO[0], (51, 45, 39, 99, 135, 171, 207))}   {BOLD}{WHITE}HOST{RESET} {spin}")
        lines.append(
            f"  {_gradient(_LOGO[1], (51, 45, 39, 99, 135, 171, 207))}   {GREY}{self.subtitle}  ·  up {up}{RESET}"
        )
        lines.append("")

        # Status row
        if watchdog_active:
            client = f"{RED}{BOLD}● WATCHDOG{RESET}"
        elif last_cmd_age_s is None:
            client = f"{YELLOW}{'●' if self._frame % 10 < 5 else '○'} WAITING FOR CLIENT{RESET}"
        elif last_cmd_age_s < 0.25:
            client = f"{GREEN}{BOLD}● LIVE{RESET} {GREY}{last_cmd_age_s * 1000:.0f}ms{RESET}"
        else:
            client = f"{YELLOW}● IDLE{RESET} {GREY}{last_cmd_age_s:.1f}s{RESET}"
        safety = self.robot.get_safety_status()
        holds = len(safety["joint_holds"]) + len(safety["gripper_holds"])
        hold_txt = f"{RED}{holds} holding{RESET}" if holds else f"{GREEN}ok{RESET}"
        if safety.get("arm_fault_events"):
            hold_txt += f"  {GREY}overcurrent releases{RESET} {RED}{safety['arm_fault_events']}{RESET}"
        lines.append(
            f"  {client}   {GREY}source{RESET} {WHITE}{target_source}{RESET}   "
            f"{GREY}owner{RESET} {WHITE}{(owner or '-')[:12]}{RESET}   "
            f"{GREY}watchdog trips{RESET} {WHITE}{watchdog_events}{RESET}   {GREY}protection{RESET} {hold_txt}"
        )

        hz = self.hz_history[-1] if self.hz_history else 0.0
        spark = self._sparkline(width - 40)
        hz_color = GREEN if hz >= 45 else YELLOW if hz >= 30 else RED
        lines.append(
            f"  {GREY}loop{RESET} {hz_color}{BOLD}{hz:5.1f} Hz{RESET} {CYAN}{spark}{RESET} "
            f"{GREY}{getattr(self, '_avg_loop_ms', 0.0):5.1f} ms/loop{RESET}"
        )
        lines.append("")

        # Arms
        bar_w = max(16, min(40, width - 66))
        for side, bus, color in (("LEFT", self.robot.left_bus, MAGENTA), ("RIGHT", self.robot.right_bus, CYAN)):
            motors = [m for m in (bus.motors if bus else {}) if m.startswith(f"arm_{side.lower()}_")]
            if not motors:
                continue
            title = f"─ {side} ARM "
            st = engage_status.get(side.lower())
            engaged = st is not None and st.engaged
            if engaged:
                badge, badge_len = f"{GREEN}{BOLD}● ENGAGED{RESET}", 9
            else:
                worst = ""
                if st is not None and st.worst_motor:
                    worst = f" · worst {st.worst_motor.split('_', 2)[2]} {st.worst_error_deg:.0f}°"
                blink = "○" if self._frame % 10 < 5 else "●"
                text = f"{blink} LIMP · match leader to follower{worst}"
                badge, badge_len = f"{YELLOW}{BOLD}{text}{RESET}", len(text)
            fill = max(width - len(title) - badge_len - 6, 2)
            lines.append(f"  {color}{BOLD}{title}{RESET}{GREY}{'─' * fill}{RESET} {badge}")
            lines.append(
                f"  {GREY}{'joint':<15} {'position  ● now  ◆ cmd':<{bar_w + 2}} {'now':>7} {'cmd':>7} {'err':>6}  current{RESET}"
            )
            for motor in motors:
                targets = sent_action if engaged else requested_action
                lines.append(self._joint_row(motor, bus, observation, targets, currents_ma, safety, bar_w))
            lines.append("")

        # Base / lift
        if self.robot.base_motors or self.robot.lift.enabled:
            lines.append(
                f"  {GREEN}{BOLD}─ BASE & LIFT {RESET}{GREY}{'─' * (width - 18)}{RESET}"
            )
            lines.append(
                f"  {GREY}x{RESET} {observation.get('x.vel', 0.0):+.2f} m/s   "
                f"{GREY}y{RESET} {observation.get('y.vel', 0.0):+.2f} m/s   "
                f"{GREY}θ{RESET} {observation.get('theta.vel', 0.0):+.1f} °/s   "
                f"{GREY}lift{RESET} {observation.get('lift_axis.height_mm', 0.0):.1f} mm"
            )
        else:
            lines.append(f"  {GREY}base & lift: not connected (arms-only mode){RESET}")
        lines.append("")

        # Log tail
        lines.append(f"  {YELLOW}{BOLD}─ EVENTS {RESET}{GREY}{'─' * (width - 13)}{RESET}")
        room = max(3, rows - len(lines) - 2)
        recent = list(self.logs)[-room:]
        if not recent:
            lines.append(f"  {GREY}(nothing yet){RESET}")
        for ts, color, msg in recent:
            stamp = time.strftime("%H:%M:%S", time.localtime(ts))
            lines.append(f"  {GREY}{stamp}{RESET} {color}{msg[: width - 14]}{RESET}")
        lines.append(f"  {DIM}Ctrl+C to stop{RESET}")

        frame = "\x1b[H" + "\x1b[K\n".join(lines[:rows]) + "\x1b[K\x1b[J"
        self._real_stdout.write(frame)
        self._real_stdout.flush()

    def _sparkline(self, n: int) -> str:
        values = list(self.hz_history)[-max(n, 1):]
        if not values:
            return ""
        top = max(max(values), 1.0)
        return "".join(_SPARK[min(int(v / top * (len(_SPARK) - 1)), len(_SPARK) - 1)] for v in values)

    def _joint_row(
        self,
        motor: str,
        bus: Any,
        observation: dict[str, Any],
        sent_action: dict[str, float],
        currents_ma: dict[str, float],
        safety: dict[str, Any],
        bar_w: int,
    ) -> str:
        name = motor.split("_", 2)[2]
        now = observation.get(f"{motor}.pos")
        cmd = sent_action.get(f"{motor}.pos")
        mode = bus.motors[motor].norm_mode
        lo, hi = {
            MotorNormMode.RANGE_0_100: (0.0, 100.0),
            MotorNormMode.DEGREES: (-180.0, 180.0),
        }.get(mode, (-100.0, 100.0))

        def slot(v: float) -> int:
            return round((min(max(v, lo), hi) - lo) / (hi - lo) * (bar_w - 1))

        cells = [f"{GREY}─{RESET}"] * bar_w
        if lo < 0 < hi:
            cells[slot(0.0)] = f"{GREY}┼{RESET}"
        if cmd is not None:
            cells[slot(cmd)] = f"{YELLOW}◆{RESET}"
        if now is not None:
            cells[slot(now)] = f"{WHITE}{BOLD}●{RESET}"
        bar = f"{GREY}├{RESET}{''.join(cells)}{GREY}┤{RESET}"

        fmt = lambda v: f"{v:+7.1f}" if v is not None else f"{'-':>7}"  # noqa: E731
        err = (cmd - now) if (cmd is not None and now is not None) else None
        err_txt = f"{'-':>6}"
        if err is not None:
            err_color = GREEN if abs(err) < 3 else YELLOW if abs(err) < 10 else RED
            err_txt = f"{err_color}{err:+6.1f}{RESET}"

        ma = abs(currents_ma.get(motor, 0.0))
        limits = self.robot._current_limits.get(motor)
        cap = limits.collision_ma if limits else 1000.0
        frac = min(ma / cap, 1.0) if cap else 0.0
        cur_color = GREEN if frac < 0.5 else YELLOW if frac < 0.85 else RED
        blocks = round(frac * 8)
        cur_bar = f"{cur_color}{'▮' * blocks}{GREY}{'▯' * (8 - blocks)}{RESET}"
        hold = motor in safety["joint_holds"] or motor in safety["gripper_holds"]
        hold_txt = f" {RED}{BOLD}HOLD{RESET}" if hold else ""
        return (
            f"  {WHITE}{name:<15}{RESET} {bar} {fmt(now)} {YELLOW}{fmt(cmd)}{RESET} {err_txt}  "
            f"{cur_bar} {cur_color}{ma:5.0f}mA{RESET}{hold_txt}"
        )


class HostStatusPublisher:
    """Writes what the dashboard shows as JSON, so the web panel can mirror it when there is no terminal.

    Same inputs as `HostTui.update`; the file is replaced atomically a few times a second.
    """

    def __init__(self, robot: Any, path: str, *, subtitle: str, rate_hz: float = 5.0):
        self.robot = robot
        self.path = path
        self.subtitle = subtitle
        self.period_s = 1.0 / rate_hz
        self.started_at = time.monotonic()
        self.events: deque[dict[str, Any]] = deque(maxlen=40)
        self.hz_history: deque[float] = deque(maxlen=40)
        self._loop_count = 0
        self._loop_ms_total = 0.0
        self._avg_loop_ms = 0.0
        self._window_start = time.perf_counter()
        self._last_write = 0.0
        self._handler = _EventCapture(self.events)

    def start(self) -> None:
        logging.getLogger().addHandler(self._handler)

    def stop(self) -> None:
        logging.getLogger().removeHandler(self._handler)
        try:
            os.remove(self.path)
        except OSError:
            pass

    def update(self, *, loop_ms: float, **state: Any) -> None:
        self._loop_count += 1
        self._loop_ms_total += loop_ms
        now = time.perf_counter()
        if now - self._window_start >= 0.5:
            self.hz_history.append(round(self._loop_count / (now - self._window_start), 1))
            self._avg_loop_ms = self._loop_ms_total / self._loop_count
            self._loop_count, self._loop_ms_total, self._window_start = 0, 0.0, now
        if now - self._last_write < self.period_s:
            return
        self._last_write = now
        try:
            self._write(self._snapshot(**state))
        except Exception as e:  # never let telemetry take the control loop down
            logging.warning("host status publish failed: %s", e)

    def _write(self, snap: dict[str, Any]) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f, separators=(",", ":"))
        os.replace(tmp, self.path)

    def _snapshot(
        self,
        *,
        observation: dict[str, Any],
        sent_action: dict[str, float],
        requested_action: dict[str, float],
        engage_status: dict[str, Any],
        currents_ma: dict[str, float],
        last_cmd_age_s: float | None,
        watchdog_active: bool,
        watchdog_events: int,
        target_source: str,
        owner: str | None,
    ) -> dict[str, Any]:
        safety = self.robot.get_safety_status()
        holds = set(safety["joint_holds"]) | set(safety["gripper_holds"])
        if watchdog_active:
            client = "watchdog"
        elif last_cmd_age_s is None:
            client = "waiting"
        elif last_cmd_age_s < 0.25:
            client = "live"
        else:
            client = "idle"

        arms = []
        for side, bus in (("left", self.robot.left_bus), ("right", self.robot.right_bus)):
            motors = [m for m in (bus.motors if bus else {}) if m.startswith(f"arm_{side}_")]
            if not motors:
                continue
            st = engage_status.get(side)
            engaged = st is not None and st.engaged
            targets = sent_action if engaged else requested_action
            joints = []
            for motor in motors:
                lo, hi = {
                    MotorNormMode.RANGE_0_100: (0.0, 100.0),
                    MotorNormMode.DEGREES: (-180.0, 180.0),
                }.get(bus.motors[motor].norm_mode, (-100.0, 100.0))
                limits = self.robot._current_limits.get(motor)
                now_v, cmd_v = observation.get(f"{motor}.pos"), targets.get(f"{motor}.pos")
                joints.append(
                    {
                        "name": motor.split("_", 2)[2],
                        "now": now_v,
                        "cmd": cmd_v,
                        "lo": lo,
                        "hi": hi,
                        "ma": abs(currents_ma.get(motor, 0.0)),
                        "cap_ma": limits.collision_ma if limits else 1000.0,
                        "hold": motor in holds,
                    }
                )
            arms.append(
                {
                    "side": side,
                    "engaged": engaged,
                    "worst_joint": st.worst_motor.split("_", 2)[2] if st is not None and st.worst_motor else None,
                    "worst_error_deg": st.worst_error_deg if st is not None and st.worst_motor else None,
                    "joints": joints,
                }
            )

        has_base = bool(self.robot.base_motors or self.robot.lift.enabled)
        return {
            "t": time.time(),
            "subtitle": self.subtitle,
            "uptime_s": int(time.monotonic() - self.started_at),
            "loop_hz": self.hz_history[-1] if self.hz_history else 0.0,
            "loop_ms": round(self._avg_loop_ms, 1),
            "hz_history": list(self.hz_history),
            "client": {"state": client, "age_s": last_cmd_age_s},
            "source": target_source,
            "owner": owner,
            "watchdog_events": watchdog_events,
            "protection": {"holds": len(holds), "overcurrent_releases": safety.get("arm_fault_events", 0)},
            "arms": arms,
            "base": (
                {
                    "x": observation.get("x.vel", 0.0),
                    "y": observation.get("y.vel", 0.0),
                    "theta": observation.get("theta.vel", 0.0),
                    "lift_mm": observation.get("lift_axis.height_mm", 0.0),
                }
                if has_base
                else None
            ),
            "events": list(self.events),
        }


class _EventCapture(logging.Handler):
    def __init__(self, sink: deque[dict[str, Any]]):
        super().__init__(level=logging.INFO)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        level = "error" if record.levelno >= logging.ERROR else "warn" if record.levelno >= logging.WARNING else "info"
        self._sink.append({"t": time.time(), "level": level, "msg": record.getMessage().splitlines()[0]})
