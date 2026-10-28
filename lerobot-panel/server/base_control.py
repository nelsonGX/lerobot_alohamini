"""Independent base controller. Only the watchdog thread touches the serial bus while running."""

from __future__ import annotations

import math
import os
import secrets
import threading
import time
from pathlib import Path

from pydantic import BaseModel, Field

BASE_PORT = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B79018624-if00"
NAMES = ("left_wheel", "back_wheel", "right_wheel", "lift")


class BaseConnect(BaseModel):
    robot_model: str = Field(default="alohamini1", pattern="^alohamini(1|2|2pro)$")
    # No homing or guessed absolute height: null allows wheels only.
    lift_height_mm: float | None = Field(default=None, ge=0, le=600, allow_inf_nan=False)


class BaseCommand(BaseModel):
    session: str = Field(min_length=16, max_length=100)
    seq: int = Field(ge=0)
    direction: str = Field(pattern="^(stop|forward|backward|left|right|rotate_left|rotate_right|up|down)$")


def assert_port_free(port: str) -> None:
    target = os.path.realpath(port)
    for process in Path("/proc").glob("[0-9]*"):
        if process.name == str(os.getpid()):
            continue
        try:
            for fd in (process / "fd").iterdir():
                try:
                    if os.path.realpath(fd) == target:
                        raise RuntimeError("Base serial port is in use. Stop its other controller first.")
                except FileNotFoundError:
                    pass
        except (PermissionError, FileNotFoundError):
            continue


class BaseController:
    timeout_s = 0.5
    max_hold_s = 3.0

    def __init__(self):
        self.lock = threading.RLock()
        self.bus = None
        self.session = None
        self.seq = -1
        self.direction = "stop"
        self.deadline = 0.0
        self.hold_started = 0.0
        self.error = None
        self.height = None
        self.telemetry = {}
        self._quit = threading.Event()
        self._thread = None

    def snapshot(self):
        with self.lock:
            return {"connected": self.bus is not None, "direction": self.direction,
                    "lift_height_mm": self.height, "motors": self.telemetry,
                    "error": self.error, "port": BASE_PORT}

    def connect(self, request: BaseConnect):
        from lerobot.motors import Motor, MotorNormMode
        from lerobot.motors.feetech import FeetechMotorsBus
        from lerobot.robots.alohamini.model_specs import validate_robot_model

        with self.lock:
            if self.bus is not None:
                raise RuntimeError("Wheels and lift are already connected. Disconnect before taking control.")
            assert_port_free(BASE_PORT)
            specs = validate_robot_model(request.robot_model)
            motors = {name: Motor(i, specs["lift_motor" if name == "lift" else "base_motor"],
                                  MotorNormMode.DEGREES) for name, i in zip(NAMES, (8, 9, 10, 11))}
            bus = FeetechMotorsBus(port=BASE_PORT, motors=motors)
            try:
                bus.connect(handshake=False)
                bus.port_handler.ser.exclusive = True
                bus.set_baudrate(1_000_000)
                # Ping all four before changing any register.
                for name, motor in motors.items():
                    model = bus.ping(name, num_retry=1)
                    if model != bus.model_number_table[motor.model]:
                        raise RuntimeError(f"{name}: motor missing or model mismatch ({model}).")
                self.bus = bus
                self.height = request.lift_height_mm
                self.pitch = specs["lead_mm_per_rev"]
                self.radius = specs["wheel_radius"]
                self.base_radius = specs["base_radius"]
                self.last_tick = bus.read("Present_Position", "lift", normalize=False, num_retry=0) % 4096
                for name in NAMES:
                    # The lift stays untouched when its height is unknown.
                    if name == "lift" and self.height is None:
                        continue
                    bus.write("Torque_Enable", name, 0, num_retry=0)
                    bus.write("Operating_Mode", name, 1, num_retry=0)
                    bus.write("Goal_Velocity", name, 0, normalize=False, num_retry=0)
                    bus.write("Torque_Enable", name, 1, num_retry=0)
                self._sample()
            except Exception:
                if self.bus is not None:
                    self._close()
                elif bus.is_connected:
                    bus.disconnect(disable_torque=False)
                raise
            self.session = secrets.token_urlsafe(24)
            self.seq = -1
            self.direction = "stop"
            self.error = None
            self._quit.clear()
            self._thread = threading.Thread(target=self._run, args=(self.session,), daemon=True)
            self._thread.start()
            return {**self.snapshot(), "session": self.session}

    def command(self, req: BaseCommand):
        with self.lock:
            if self.bus is None or req.session != self.session:
                raise RuntimeError("Control session expired. Connect again.")
            if req.seq <= self.seq:
                return self.snapshot()
            self.seq = req.seq
            if req.direction in ("up", "down") and self.height is None:
                raise RuntimeError("Reconnect with the measured lift height to enable the lift.")
            now = time.monotonic()
            if self.direction == "stop" and req.direction != "stop":
                self.hold_started = now
            self.direction = req.direction
            self.deadline = now + self.timeout_s
            if self.direction == "stop":
                try:
                    self._write_stop()
                except Exception:
                    self._close()
                    raise
            return self.snapshot()

    def stop(self):
        """Global stop revokes ownership so delayed commands cannot restart motion."""
        with self.lock:
            self._quit.set()
            self._close()
            return self.snapshot()

    def _write_stop(self):
        errors = []
        for name in NAMES:
            if name == "lift" and self.height is None:
                continue
            try:
                self.bus.write("Goal_Velocity", name, 0, normalize=False, num_retry=0)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))

    def _close(self):
        if self.bus is not None:
            try:
                self._write_stop()
            except Exception as exc:
                self.error = f"Stop could not be confirmed: {exc}"
            finally:
                # Zero velocity holds the lift; releasing its torque could let a loaded lift descend.
                for name in NAMES[:3]:
                    try:
                        self.bus.write("Torque_Enable", name, 0, num_retry=0)
                    except Exception as exc:
                        self.error = str(exc)
                try:
                    self.bus.disconnect(disable_torque=False)
                except Exception as exc:
                    self.error = str(exc)
                finally:
                    self.bus = None
        self.session = None
        self.direction = "stop"

    def _sample(self):
        tick = self.bus.read("Present_Position", "lift", normalize=False, num_retry=0) % 4096
        delta = (tick - self.last_tick + 2048) % 4096 - 2048
        self.last_tick = tick
        if self.height is not None:
            self.height -= delta * self.pitch / 4096
        for name in NAMES:
            values = {key: self.bus.read(register, name, normalize=False, num_retry=0) * scale
                      for key, register, scale in (("voltage", "Present_Voltage", .1),
                                                   ("temperature", "Present_Temperature", 1),
                                                   ("current_ma", "Present_Current", 6.5))}
            self.telemetry[name] = values
            if values["temperature"] >= 60 or abs(values["current_ma"]) >= 500:
                raise RuntimeError(f"{name}: temperature/current limit reached. Release and check the robot.")

    def _velocities(self):
        speeds = dict.fromkeys(NAMES, 0)
        direction = self.direction
        if direction in ("up", "down"):
            if (direction == "up" and self.height < 595) or (direction == "down" and self.height > 5):
                speeds["lift"] = round((1 if direction == "down" else -1) * 10 * 4096 / self.pitch)
        else:
            x, y, theta = {"forward": (.03, 0, 0), "backward": (-.03, 0, 0),
                           "left": (0, .03, 0), "right": (0, -.03, 0),
                           "rotate_left": (0, 0, math.radians(10)),
                           "rotate_right": (0, 0, -math.radians(10))}.get(direction, (0, 0, 0))
            for name, angle in zip(NAMES[:3], (150, -90, 30)):
                a = math.radians(angle)
                speeds[name] = round((-x * math.cos(a) - y * math.sin(a) + self.base_radius * theta)
                                     / self.radius * 4096 / (2 * math.pi))
        return speeds

    def _tick(self):
        now = time.monotonic()
        if self.direction != "stop" and (now > self.deadline or now - self.hold_started > self.max_hold_s):
            self.error = "Motion stopped: command timeout or 3-second hold limit. Connect again."
            self._close()
            return
        self._sample()
        for name, speed in self._velocities().items():
            if name != "lift" or self.height is not None:
                self.bus.write("Goal_Velocity", name, speed, normalize=False, num_retry=0)

    def _run(self, session):
        while not self._quit.wait(.05):
            with self.lock:
                if self.bus is None or self.session != session:
                    return
                try:
                    self._tick()
                except Exception as exc:
                    self.error = str(exc)
                    self._close()
                    return
