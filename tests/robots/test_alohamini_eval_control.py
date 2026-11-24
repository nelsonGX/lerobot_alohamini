import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import torch

from examples.alohamini import eval_control, evaluate_bi, evaluation_safety


def test_panel_control_reads_known_commands_only():
    control = eval_control.PanelControl(io.StringIO("success\n  START \nrm -rf /\n\ndiscard\n"))
    seen = []
    for _ in range(200):
        if command := control.poll():
            seen.append(command)
        if len(seen) == 3:
            break
    assert seen == ["success", "start", "discard"]
    assert eval_control.PanelControl().poll() is None


def test_outcomes_relabel_and_aggregate(tmp_path):
    outcomes = eval_control.EvalOutcomes([tmp_path / "a/eval_info.json"])
    assert not outcomes.relabel("fail")
    outcomes.add(outcome="success", frames=10, duration_s=1.234, ended_by="success", dataset_index=0)
    outcomes.add(outcome=None, frames=5, duration_s=2, ended_by="timeout", dataset_index=1)
    assert outcomes.relabel("fail")
    info = json.loads((tmp_path / "a/eval_info.json").read_text())
    assert info["aggregated"] == {"done": 2, "success": 1, "fail": 1, "unlabeled": 0, "pc_success": 50.0}
    assert [e["outcome"] for e in info["episodes"]] == ["success", "fail"]
    assert info["episodes"][0]["duration_s"] == 1.23


class ScriptedControl:
    """Returns the next scripted command on each poll; None entries are polls with nothing sent."""

    script: list = []

    def __init__(self, _stream=None):
        self.queue = list(self.script)

    def poll(self):
        return self.queue.pop(0) if self.queue else None


def test_panel_commands_end_discard_judge_and_start_episodes(monkeypatch, tmp_path, capsys):
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(evaluate_bi.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(evaluate_bi.time, "perf_counter", lambda: clock.now)
    monkeypatch.setattr(evaluate_bi, "precise_sleep", lambda s: setattr(clock, "now", clock.now + s))

    robot = Mock(observation_updated=True, last_remote_state={"joint.pos": 1.0})
    robot.name = "alohamini"
    robot.observation_features = {"joint.pos": float}
    robot.action_features = {"joint.pos": float}
    robot.get_observation.return_value = {"joint.pos": 1.0}
    robot.send_action.return_value = True
    engine = Mock(_rtc_thread=None)
    engine.get_action.return_value = torch.tensor([2.0])
    dataset = Mock(root=tmp_path / "ds", meta=SimpleNamespace(total_episodes=0, stats={}))
    dataset.has_pending_frames.return_value = False

    def save_episode():
        dataset.meta.total_episodes += 1

    dataset.save_episode.side_effect = save_episode
    guard = Mock(context=(None,))
    guard.check_observation.side_effect = lambda _robot, obs, **_kw: (obs, None)

    monkeypatch.setattr(evaluate_bi, "AlohaMiniClient", lambda _config: robot)
    monkeypatch.setattr(evaluate_bi, "ThreadSafeRobot", lambda r: r)
    monkeypatch.setattr(evaluate_bi, "EvaluationSafetyGuard", lambda: guard)
    monkeypatch.setattr(evaluate_bi, "EngageRamp", lambda **_kw: Mock(apply=lambda action, _obs: action))
    monkeypatch.setattr(evaluate_bi, "auto_select_torch_device", lambda: "cpu")
    monkeypatch.setattr(
        evaluate_bi.PreTrainedConfig, "from_pretrained", lambda _p: SimpleNamespace(type="act")
    )
    monkeypatch.setattr(evaluate_bi, "get_policy_class", lambda _type: Mock())
    monkeypatch.setattr(
        evaluate_bi, "make_default_processors", lambda: (lambda p: p[0], lambda p: p[0], lambda o: o)
    )
    monkeypatch.setattr(evaluate_bi, "aggregate_pipeline_dataset_features", lambda **_kw: {})
    monkeypatch.setattr(evaluate_bi, "hw_to_dataset_features", lambda *_a: {})
    monkeypatch.setattr(evaluate_bi, "build_dataset_frame", lambda _f, data, prefix: {prefix: dict(data)})
    monkeypatch.setattr(evaluate_bi, "make_pre_post_processors", lambda **_kw: (None, None))
    monkeypatch.setattr(evaluate_bi, "create_inference_engine", lambda *_a, **_kw: engine)
    monkeypatch.setattr(evaluate_bi.LeRobotDataset, "create", lambda **_kw: dataset)
    monkeypatch.setattr(evaluate_bi, "log_say", lambda *_a: None)
    ScriptedControl.script = [
        None,
        None,
        None,
        "discard",  # episode 1: thrown away and run again
        None,
        None,
        "start",  # waits for the operator, however long
        None,
        None,
        "success",  # episode 1 again, judged a success
        "fail",
        None,
        "start",  # relabeled while waiting
        # episode 2 runs to its time limit without a verdict
    ]
    monkeypatch.setattr(evaluate_bi, "PanelControl", ScriptedControl)
    output = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv",
        ["evaluate_bi", "--policy.path=test", "--dataset.repo_id=test/test", "--eval.n_episodes=2",
         "--eval.episode_time_s=1", "--dataset.push_to_hub=false", "--eval.panel_control=true",
         "--eval.wait_for_start=true", f"--output_dir={output}"],
    )  # fmt: skip

    evaluate_bi.main()

    dataset.clear_episode_buffer.assert_called_once()
    assert dataset.save_episode.call_count == 2
    info = json.loads((output / "eval_info.json").read_text())
    assert [(e["outcome"], e["ended_by"], e["dataset_index"]) for e in info["episodes"]] == [
        ("fail", "success", 0),
        (None, "timeout", 1),
    ]
    assert json.loads((tmp_path / "ds/meta/eval_outcomes.json").read_text()) == info
    events = [
        json.loads(line.removeprefix(eval_control.EVENT_PREFIX))
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(eval_control.EVENT_PREFIX)
    ]
    assert [(e["event"], e["episode"]) for e in events] == [
        ("episode_start", 1), ("discarded", 1), ("waiting", 1),
        ("episode_start", 1), ("episode_end", 1), ("waiting", 2), ("outcome", 1),
        ("episode_start", 2), ("episode_end", 2),
    ]  # fmt: skip
    assert events[1]["done"] == 0 and events[-1]["done"] == 2 and events[-1]["fail"] == 1
    assert events[2]["until"] is None and events[0]["cap"] == 1


def test_parse_command_accepts_start_with_policy_only():
    assert eval_control.parse_command(" START  Tiramisu ") == "start tiramisu"
    assert eval_control.parse_command("start") == "start"
    assert eval_control.parse_command("end tiramisu") is None
    assert eval_control.parse_command("start ../etc") is None
    assert eval_control.parse_command("start a b") is None


class StandbyControl(ScriptedControl):
    """Like ScriptedControl, but ends the (endless) standby run once the script is used up."""

    def poll(self):
        if not self.queue:
            raise KeyboardInterrupt
        return self.queue.pop(0)


def standby_harness(monkeypatch, tmp_path, robot, script):
    """Run evaluate_bi in standby mode with two fake policies until ``script`` (control commands) runs out."""
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(evaluate_bi.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(evaluate_bi.time, "perf_counter", lambda: clock.now)
    monkeypatch.setattr(evaluate_bi, "precise_sleep", lambda s: setattr(clock, "now", clock.now + s))
    engines, loaded = {}, []

    def make_engine(*_a, task, **_kw):
        engine = Mock(_rtc_thread=None)
        engine.get_action.return_value = torch.tensor([2.0 if "cake" in task else 3.0])
        engines[task] = engine
        return engine

    guard = Mock(context=(None,))
    guard.check_observation.side_effect = lambda _robot, obs, **_kw: (obs, None)
    monkeypatch.setattr(evaluate_bi, "AlohaMiniClient", lambda _config: robot)
    monkeypatch.setattr(evaluate_bi, "ThreadSafeRobot", lambda r: r)
    monkeypatch.setattr(evaluate_bi, "EvaluationSafetyGuard", lambda: guard)
    monkeypatch.setattr(evaluate_bi, "EngageRamp", lambda **_kw: Mock(apply=lambda action, _obs: action))
    monkeypatch.setattr(evaluate_bi, "auto_select_torch_device", lambda: "cpu")
    monkeypatch.setattr(
        evaluate_bi.PreTrainedConfig,
        "from_pretrained",
        lambda p: loaded.append(p) or SimpleNamespace(type="act"),
    )
    monkeypatch.setattr(evaluate_bi, "get_policy_class", lambda _type: Mock())
    monkeypatch.setattr(
        evaluate_bi, "make_default_processors", lambda: (lambda p: p[0], lambda p: p[0], lambda o: o)
    )
    monkeypatch.setattr(evaluate_bi, "aggregate_pipeline_dataset_features", lambda **_kw: {})
    monkeypatch.setattr(evaluate_bi, "hw_to_dataset_features", lambda *_a: {})
    monkeypatch.setattr(evaluate_bi, "build_dataset_frame", lambda _f, data, prefix: {prefix: dict(data)})
    monkeypatch.setattr(evaluate_bi, "make_pre_post_processors", lambda **_kw: (None, None))
    monkeypatch.setattr(evaluate_bi, "create_inference_engine", make_engine)
    monkeypatch.setattr(evaluate_bi, "log_say", lambda *_a: None)
    StandbyControl.script = script
    monkeypatch.setattr(evaluate_bi, "PanelControl", StandbyControl)
    policies = [
        {"name": "strawberry_cake", "path": "ckpt/straw", "task": "pick the cake", "episode_time_s": 30},
        {"name": "tiramisu", "path": "ckpt/tira", "task": "pick the tiramisu", "episode_time_s": 1},
    ]
    monkeypatch.setattr(
        "sys.argv",
        ["evaluate_bi", f"--eval.standby_policies={json.dumps(policies)}", "--dataset.record=false",
         "--eval.panel_control=true", f"--output_dir={tmp_path}"],
    )  # fmt: skip
    with pytest.raises(KeyboardInterrupt):
        evaluate_bi.main()
    return engines, loaded


def eval_events(capsys):
    return [
        json.loads(line.removeprefix(eval_control.EVENT_PREFIX))
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(eval_control.EVENT_PREFIX)
    ]


def test_standby_loads_all_policies_and_runs_the_one_each_start_names(monkeypatch, tmp_path, capsys):
    robot = Mock(observation_updated=True, last_remote_state={"joint.pos": 1.0})
    robot.name = "alohamini"
    robot.observation_features = {"joint.pos": float}
    robot.action_features = {"joint.pos": float}
    robot.get_observation.return_value = {"joint.pos": 1.0}
    robot.send_action.return_value = True
    engines, loaded = standby_harness(monkeypatch, tmp_path, robot, [
        None, "start nope", None,  # unknown names are ignored; it keeps waiting
        "start tiramisu",  # runs to tiramisu's own 1 s limit (the loop polls every tick)
        *[None] * 40,
        "start strawberry_cake", None, "end",  # ended early by the panel
        None,
    ])  # fmt: skip

    assert loaded == ["ckpt/straw", "ckpt/tira"]  # both loaded up front, once
    sent = [
        call.args[0]["joint.pos"] for call in robot.send_action.call_args_list if "joint.pos" in call.args[0]
    ]
    assert 3.0 in sent and 2.0 in sent and sent.index(3.0) < sent.index(2.0)  # tiramisu first, then cake
    for engine in engines.values():
        engine.stop.assert_called()  # unloaded when stopped
    robot.disconnect.assert_called_once()
    events = eval_events(capsys)
    assert [(e["event"], e.get("policy")) for e in events] == [
        ("waiting", None), ("episode_start", "tiramisu"), ("episode_end", None),
        ("waiting", None), ("episode_start", "strawberry_cake"), ("episode_end", None), ("waiting", None),
    ]  # fmt: skip
    assert events[0]["policies"] == ["strawberry_cake", "tiramisu"]
    assert events[1]["cap"] == 1 and events[4]["cap"] == 30


def test_standby_requires_panel_control_and_valid_policies(monkeypatch, capsys):
    for argv, message in [
        (['--eval.standby_policies=[{"name": "a", "path": "p"}]'], "panel_control"),
        (["--eval.standby_policies=[]", "--eval.panel_control=true"], "non-empty"),
        (['--eval.standby_policies=[{"name": "A B", "path": "p"}]', "--eval.panel_control=true"], "unique"),
        (["--dataset.record=false"], "--policy.path is required"),
    ]:
        monkeypatch.setattr("sys.argv", ["evaluate_bi", "--dataset.record=false", *argv])
        with pytest.raises(SystemExit):
            evaluate_bi.main()
        assert message in capsys.readouterr().err


def test_home_moves_the_arms_back_to_where_they_started(monkeypatch, tmp_path, capsys):
    state = {"joint.pos": 0.0, "gripper.pos": 5.0}  # the pose when staff armed the robot

    def follow(action):  # a perfect follower arm
        state.update({k: v for k, v in action.items() if k in state})
        return True

    robot = Mock(observation_updated=True, last_remote_state=state)
    robot.name = "alohamini"
    robot.observation_features = {"joint.pos": float, "gripper.pos": float}
    robot.action_features = {"joint.pos": float}
    robot.get_observation.side_effect = lambda: dict(state)
    robot.send_action.side_effect = follow
    standby_harness(monkeypatch, tmp_path, robot, [
        "start tiramisu", *[None] * 40,  # the policy leaves the arm at 3.0
        "home", *[None] * 30,
        None,
    ])  # fmt: skip

    events = eval_events(capsys)
    assert [e["event"] for e in events] == [
        "waiting", "episode_start", "episode_end", "waiting", "homing", "homed",
    ]  # fmt: skip
    assert events[-1]["ok"] is True and state["joint.pos"] == 0.0
    homing = [c.args[0] for c in robot.send_action.call_args_list if "x.vel" in c.args[0]][
        :-1
    ]  # last: exit hold
    steps = [b["joint.pos"] - a["joint.pos"] for a, b in zip(homing, homing[1:], strict=False)]
    assert homing and max(abs(d) for d in steps) <= 1.0 + 1e-9  # slewed, not jumped
    assert all(a["x.vel"] == 0.0 and a["gripper.pos"] == 5.0 for a in homing)


def test_home_move_slews_holds_and_reports_arrival():
    move = evaluation_safety.HomeMove({"a.pos": 0.0, "b.pos": 10.0}, max_step=2.0, tolerance=0.5)
    obs = {"a.pos": 5.0, "b.pos": 10.0, "lift_axis.height_mm": 120.0}
    first = move.step(obs)
    assert first["a.pos"] == 3.0 and first["b.pos"] == 10.0
    assert first["lift_axis.height_mm"] == 120.0 and first["theta.vel"] == 0.0
    assert not move.arrived(obs) and move.error(obs) == 5.0
    for _ in range(3):
        move.step(obs)
    assert move.step(obs)["a.pos"] == 0.0
    assert not move.arrived(obs)  # commanded home, but the arm hasn't followed yet
    assert move.arrived({"a.pos": 0.3, "b.pos": 10.0})
