import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import torch

from examples.alohamini import eval_control, evaluate_bi


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
