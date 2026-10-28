"""Manual base control safety checks using a fake serial bus; no robot movement."""

import importlib
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lerobot-panel/server"))
mod = importlib.import_module("base_control")


class FakeBus:
    def __init__(self):
        self.writes = []
        self.closed = False
        self.tick = 1242
        self.current = 0
        self.fail = None

    def read(self, register, name, **kwargs):
        return {"Present_Position": self.tick, "Present_Voltage": 121,
                "Present_Temperature": 30, "Present_Current": self.current}[register]

    def write(self, register, name, value, **kwargs):
        self.writes.append((register, name, value))
        if name == self.fail:
            raise OSError("Motor disconnected")

    def disconnect(self, **kwargs):
        self.closed = True


@pytest.fixture
def control():
    c = mod.BaseController()
    c.bus = FakeBus()
    c.session = "test-session-0123456789"
    c.height = 100
    c.pitch, c.radius, c.base_radius, c.last_tick = 84, .05, .125, 1242
    return c


def command(c, seq, direction):
    return c.command(mod.BaseCommand(session=c.session, seq=seq, direction=direction))


def test_release_rejects_late_movement(control):
    command(control, 1, "forward")
    control._tick()
    assert any(value != 0 for _, _, value in control.bus.writes)
    command(control, 3, "stop")
    command(control, 2, "forward")
    assert control.direction == "stop"
    assert all(value == 0 for _, _, value in control.bus.writes[-4:])


@pytest.mark.parametrize("reason", ["heartbeat", "hold"])
def test_timeout_stops_all_motors_and_revokes_session(control, reason):
    command(control, 1, "up")
    if reason == "heartbeat":
        control.deadline = time.monotonic() - 1
    else:
        control.hold_started = time.monotonic() - 4
    bus, session = control.bus, control.session
    control._tick()
    assert control.bus is None and bus.closed
    assert all(("Goal_Velocity", name, 0) in bus.writes for name in mod.NAMES)
    with pytest.raises(RuntimeError, match="expired"):
        control.command(mod.BaseCommand(session=session, seq=100, direction="forward"))


def test_height_limits_and_encoder_wrap(control):
    control.height = 5
    command(control, 1, "down")
    assert control._velocities()["lift"] == 0
    control.height = 595
    command(control, 2, "up")
    assert control._velocities()["lift"] == 0
    control.height = 100
    control.last_tick = 4090
    control.bus.tick = 10
    control._sample()
    assert control.height == pytest.approx(100 - 16 * 84 / 4096)


def test_unknown_height_allows_wheels_only(control):
    control.height = None
    with pytest.raises(RuntimeError, match="height"):
        command(control, 1, "up")
    command(control, 2, "forward")
    control._tick()
    assert not any(name == "lift" for _, name, _ in control.bus.writes)


def test_stop_attempts_other_motors_after_serial_failure(control):
    bus = control.bus
    bus.fail = "left_wheel"
    control.stop()
    assert bus.closed
    assert ("Goal_Velocity", "lift", 0) in bus.writes
    assert "disconnected" in control.error


def test_current_limit(control):
    control.bus.current = 80
    with pytest.raises(RuntimeError, match="limit"):
        control._sample()


@pytest.mark.parametrize("height", [float("nan"), float("inf"), -1, 601])
def test_invalid_height_rejected(height):
    with pytest.raises(ValidationError):
        mod.BaseConnect(lift_height_mm=height)


def test_agent_auth_and_stop_route(monkeypatch):
    from fastapi.testclient import TestClient

    agent = importlib.import_module("agent")
    monkeypatch.setattr(agent, "base", mod.BaseController())
    with TestClient(agent.app) as client:
        assert client.post("/base/stop").status_code == 401
        headers = {"X-Panel-Token": agent.TOKEN}
        assert client.get("/base/status", headers=headers).json()["connected"] is False
        assert client.post("/base/stop", headers=headers).status_code == 200


@pytest.mark.parametrize("height", [None, 0, 100])
def test_connect_initializes_zero_speed_without_homing(monkeypatch, height):
    from types import SimpleNamespace
    from lerobot.motors import feetech

    class ConnectBus(FakeBus):
        model_number_table = {"sts3215": 777}
        is_connected = False

        def __init__(self, **kwargs):
            super().__init__()
            self.port_handler = SimpleNamespace(ser=SimpleNamespace(exclusive=False))

        def connect(self, **kwargs):
            self.is_connected = True

        def set_baudrate(self, value):
            assert value == 1_000_000

        def ping(self, name, **kwargs):
            return 777

    monkeypatch.setattr(feetech, "FeetechMotorsBus", ConnectBus)
    monkeypatch.setattr(mod, "assert_port_free", lambda port: None)
    c = mod.BaseController()
    try:
        result = c.connect(mod.BaseConnect(lift_height_mm=height))
        assert result["connected"] and result["session"]
        assert all(value == 0 for register, _, value in c.bus.writes if register == "Goal_Velocity")
        assert c.height == height
        if height is None:
            assert not any(name == "lift" for _, name, _ in c.bus.writes)
    finally:
        c.stop()


def test_old_session_does_not_cancel_current_owner(control):
    with pytest.raises(RuntimeError, match="expired"):
        control.command(mod.BaseCommand(session="old-session-0123456789", seq=999, direction="stop"))
    assert control.bus is not None


def test_panel_proxy_uses_persisted_robot_model(monkeypatch):
    from fastapi.testclient import TestClient
    main = importlib.import_module("main")
    monkeypatch.setattr(main.recorder, "recording_dataset", lambda: None)
    monkeypatch.setattr(main.gpu_remote, "assert_robot_idle", lambda: None)
    calls = []

    def forward(ip, port, method, path, **kwargs):
        calls.append((method, path, kwargs.get("body")))
        return {"connected": False}

    monkeypatch.setattr(main, "agent_call", forward)
    with TestClient(main.app) as client:
        assert client.get("/api/base/status").status_code == 200
        assert client.post("/api/base/connect", json={"lift_height_mm": 100}).status_code == 200
        assert calls[-1][2]["robot_model"] == main.load_settings().robot_model
        assert client.post("/api/base/stop").status_code == 200


def test_lift_can_rise_from_zero_but_cannot_descend(control):
    control.height = mod.BaseConnect(lift_height_mm=0).lift_height_mm
    command(control, 1, "up")
    assert control._velocities()["lift"] < 0
    command(control, 2, "down")
    assert control._velocities()["lift"] == 0
