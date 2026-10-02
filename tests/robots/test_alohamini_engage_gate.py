from types import SimpleNamespace

from lerobot.motors import MotorNormMode
from lerobot.robots.alohamini.engage_gate import ArmEngageGate


def make_robot():
    motors = {
        name: SimpleNamespace(model="sts3215", norm_mode=MotorNormMode.DEGREES)
        for name in ("arm_left_shoulder_pan", "arm_left_elbow_flex")
    }
    return SimpleNamespace(left_bus=SimpleNamespace(motors=motors), right_bus=None)


OBS = {"arm_left_shoulder_pan.pos": 0.0, "arm_left_elbow_flex.pos": 90.0}


def test_arm_stays_limp_until_command_matches_follower():
    gate = ArmEngageGate(make_robot(), tolerance_deg=10.0)
    far = {"arm_left_shoulder_pan.pos": 40.0, "arm_left_elbow_flex.pos": 90.0, "x.vel": 0.1}

    assert gate.filter(far, OBS) == {"x.vel": 0.1}
    assert not gate.status["left"].engaged
    assert gate.status["left"].worst_motor == "arm_left_shoulder_pan"
    assert gate.status["left"].worst_error_deg == 40.0

    near = {"arm_left_shoulder_pan.pos": 5.0, "arm_left_elbow_flex.pos": 85.0}
    assert gate.filter(near, OBS) == near
    assert gate.status["left"].engaged
    # Once engaged, large moves pass through.
    assert gate.filter(far, OBS) == far


def test_partial_command_does_not_engage():
    gate = ArmEngageGate(make_robot(), tolerance_deg=10.0)
    assert gate.filter({"arm_left_shoulder_pan.pos": 0.0}, OBS) == {}
    assert not gate.status["left"].engaged


def test_disengage_requires_matching_again():
    gate = ArmEngageGate(make_robot(), tolerance_deg=10.0)
    gate.filter(dict(OBS), OBS)
    gate.disengage("overcurrent")
    far = {"arm_left_shoulder_pan.pos": 40.0, "arm_left_elbow_flex.pos": 90.0}
    assert gate.filter(far, OBS) == {}


def test_commands_without_arm_keys_pass_through():
    gate = ArmEngageGate(make_robot())
    assert gate.filter({"joint.pos": 1.0}, {}) == {"joint.pos": 1.0}
    assert gate.status == {}
