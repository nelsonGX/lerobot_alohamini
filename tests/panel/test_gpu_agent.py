"""GPU orchestration tests: no torch, GPU, robot or Hub connection needed."""

import importlib
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

SERVER = Path(__file__).resolve().parents[2] / "lerobot-panel" / "server"
sys.path.insert(0, str(SERVER))
gpu_agent = importlib.import_module("gpu_agent")
gpu_remote = importlib.import_module("gpu_remote")
gpu_jobs = importlib.import_module("gpu_jobs")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_agent, "DATA_DIR", tmp_path)
    monkeypatch.setattr(gpu_agent, "TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr(gpu_agent, "STATE_DIR", tmp_path / "jobs")
    monkeypatch.setattr(gpu_agent, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(gpu_agent, "gpu_status", lambda: ([], None))
    with TestClient(gpu_agent.app) as c:
        c.headers["X-Panel-Token"] = gpu_agent.token
        yield c


def wait_done(client, job_id):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        record = client.get(f"/jobs/{job_id}").json()
        if record["state"] not in ("running", "stopping"):
            return record
        time.sleep(0.05)
    pytest.fail("Job did not complete")


def test_auth_and_private_token(client, tmp_path):
    assert client.get("/status", headers={"X-Panel-Token": "wrong"}).status_code == 401
    assert client.post("/jobs", json={}, headers={"X-Panel-Token": "wrong"}).status_code == 401
    assert client.get("/status").json()["agent"] == "gpu"
    assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600


def test_checkpoint_catalog_route_and_panel_proxy(client, tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_agent, "CHECKPOINT_ROOT", tmp_path / "outputs")
    model = tmp_path / "outputs/train/from-terminal/checkpoints/001000/pretrained_model"
    model.mkdir(parents=True)
    (model / "config.json").write_text('{"type": "act"}')
    (model / "model.safetensors").write_bytes(b"weights")
    assert client.get("/checkpoints", headers={"X-Panel-Token": "wrong"}).status_code == 401
    result = client.get("/checkpoints").json()
    assert result["runs"][0]["checkpoints"][0]["path"] == str(model)

    def forward(method, path):
        assert (method, path) == ("GET", "/checkpoints")
        return result

    monkeypatch.setattr(gpu_remote, "call", forward)
    main = importlib.import_module("main")
    with TestClient(main.app) as panel:
        assert panel.get("/api/gpu/checkpoints").json() == result


def test_commands_are_literal_argv_and_match_modes(tmp_path):
    req = gpu_jobs.JobRequest(
        kind="train", dataset="team/data", dataset_root="/data with spaces/$(touch nope)"
    )
    cmd = gpu_jobs.command(req, tmp_path / "out")
    assert cmd[1:3] == ["-m", "lerobot.scripts.lerobot_train"]
    assert "--dataset.root=/data with spaces/$(touch nope)" in cmd
    assert "--policy.push_to_hub=false" in cmd
    assert "--env_eval_freq=0" in cmd
    req = gpu_jobs.JobRequest(kind="sim_eval", checkpoint="team/policy", episodes=2, batch_size=8)
    assert "--eval.batch_size=2" in gpu_jobs.command(req, tmp_path)
    req = gpu_jobs.JobRequest(
        kind="robot_eval",
        checkpoint="team/policy",
        dataset="team/eval_run",
        robot_ready=True,
        robot_ip="10.0.0.2",
        task="Pick 'red'; $(echo nope)",
    )
    cmd = gpu_jobs.command(req, tmp_path)
    assert "--dataset.single_task=Pick 'red'; $(echo nope)" in cmd
    assert "--dataset.push_to_hub=false" in cmd
    assert "--robot.remote_ip=10.0.0.2" in cmd


def test_chunking_and_continuous_options_reach_commands(tmp_path):
    train = gpu_jobs.JobRequest(
        kind="train", dataset="team/data", policy="smolvla", chunk_size=50, n_action_steps=10
    )
    cmd = gpu_jobs.command(train, tmp_path)
    assert "--policy.chunk_size=50" in cmd and "--policy.n_action_steps=10" in cmd
    sim = gpu_jobs.JobRequest(kind="sim_eval", checkpoint="team/act", temporal_ensemble_coeff=0.01)
    cmd = gpu_jobs.command(sim, tmp_path)
    assert "--policy.n_action_steps=1" in cmd and "--policy.temporal_ensemble_coeff=0.01" in cmd
    live = gpu_jobs.JobRequest(
        kind="robot_eval",
        checkpoint="team/smolvla",
        robot_ready=True,
        continuous=True,
        record=False,
        inference="rtc",
        rtc_queue_threshold=40,
    )
    cmd = gpu_jobs.command(live, tmp_path)
    assert "--eval.continuous=true" in cmd and "--dataset.record=false" in cmd
    assert "--inference.type=rtc" in cmd and "--inference.rtc.queue_threshold=40" in cmd
    assert not any(arg.startswith(("--dataset.repo_id", "--policy.n_action_steps")) for arg in cmd)
    recorded = gpu_jobs.JobRequest(
        kind="robot_eval", checkpoint="team/act", dataset="team/eval", robot_ready=True, n_action_steps=5
    )
    cmd = gpu_jobs.command(recorded, tmp_path)
    assert "--dataset.repo_id=team/eval" in cmd and "--policy.n_action_steps=5" in cmd
    assert "--inference.type=sync" in cmd
    assert "--eval.panel_control=true" in cmd and "--eval.wait_for_start=false" in cmd
    assert f"--output_dir={tmp_path}" in cmd
    manual = recorded.model_copy(update={"wait_for_start": True})
    assert "--eval.wait_for_start=true" in gpu_jobs.command(manual, tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "train", "dataset": "../../secret"},
        {"kind": "train", "dataset": "team/data", "steps": 0},
        {"kind": "train", "dataset": "team/data", "name": "../overwrite"},
        {"kind": "train", "dataset": "team/data", "command": "anything"},
        {"kind": "sim_eval"},
        {"kind": "robot_eval", "checkpoint": "team/policy", "dataset": "team/eval"},
        {"kind": "robot_eval", "checkpoint": "team/policy", "robot_ready": True},
        {"kind": "train", "dataset": "team/data", "checkpoint": "team/policy", "chunk_size": 20},
        {"kind": "train", "dataset": "team/data", "policy": "diffusion", "chunk_size": 20},
        {"kind": "train", "dataset": "team/data", "chunk_size": 10, "n_action_steps": 20},
        {"kind": "sim_eval", "checkpoint": "team/policy", "continuous": True},
        {"kind": "sim_eval", "checkpoint": "team/policy", "inference": "rtc"},
        {"kind": "sim_eval", "checkpoint": "team/policy", "wait_for_start": True},
        {
            "kind": "sim_eval",
            "checkpoint": "team/policy",
            "temporal_ensemble_coeff": 0.01,
            "n_action_steps": 5,
        },
        {
            "kind": "robot_eval",
            "checkpoint": "team/policy",
            "record": False,
            "robot_ready": True,
            "inference": "rtc",
            "n_action_steps": 5,
        },
    ],
)
def test_invalid_jobs_rejected(payload):
    with pytest.raises(ValidationError):
        gpu_jobs.JobRequest(**payload)


@pytest.mark.parametrize("exit_code, expected", [(0, "succeeded"), (7, "failed")])
def test_job_logs_exit_and_history_survive_restart(client, monkeypatch, tmp_path, exit_code, expected):
    monkeypatch.setattr(
        gpu_agent,
        "command",
        lambda req, output: [sys.executable, "-c", f"print('training log'); raise SystemExit({exit_code})"],
    )
    response = client.post("/jobs", json={"kind": "train", "dataset": "team/data"})
    assert response.status_code == 200
    job_id = response.json()["id"]
    record = wait_done(client, job_id)
    assert record["state"] == expected
    assert record["exit_code"] == exit_code
    assert any("training log" in line for line in record["lines"])
    reloaded = gpu_agent.Jobs(tmp_path / "jobs", tmp_path / "outputs")
    assert reloaded.get(job_id)["state"] == expected
    assert len(reloaded.list()) == 1
    assert "lines" not in reloaded.list()[0]


def test_only_one_job_and_stop_releases_slot(client, monkeypatch):
    monkeypatch.setattr(
        gpu_agent,
        "command",
        lambda req, output: [sys.executable, "-c", "import time; print('ready'); time.sleep(30)"],
    )
    body = {"kind": "train", "dataset": "team/data"}
    job_id = client.post("/jobs", json=body).json()["id"]
    assert client.post("/jobs", json=body).status_code == 409
    assert client.post(f"/jobs/{job_id}/stop").status_code == 200
    assert wait_done(client, job_id)["state"] == "stopped"


def test_episode_commands_reach_running_live_eval_only(client, monkeypatch):
    echo = "import sys\nprint('ready', flush=True)\nfor line in sys.stdin:\n    print('got', line.strip(), flush=True)"
    monkeypatch.setattr(gpu_agent, "command", lambda req, output: [sys.executable, "-c", echo])
    live = {"kind": "robot_eval", "checkpoint": "team/policy", "record": False, "robot_ready": True}
    job_id = client.post("/jobs", json=live).json()["id"]
    assert client.post(f"/jobs/{job_id}/command", json={"command": "rm -rf"}).status_code == 422
    assert client.post(f"/jobs/{job_id}/command", json={"command": "success"}).status_code == 200
    deadline = time.monotonic() + 6
    while not any("got success" in line for line in client.get(f"/jobs/{job_id}").json()["lines"]):
        assert time.monotonic() < deadline, "command never reached the program"
        time.sleep(0.05)
    client.post(f"/jobs/{job_id}/stop")
    wait_done(client, job_id)
    assert client.post(f"/jobs/{job_id}/command", json={"command": "start"}).status_code == 409

    train = client.post("/jobs", json={"kind": "train", "dataset": "team/data"}).json()["id"]
    assert client.post(f"/jobs/{train}/command", json={"command": "end"}).status_code == 409
    client.post(f"/jobs/{train}/stop")
    wait_done(client, train)


def test_artifacts_and_interrupted_history(client, tmp_path):
    manager = gpu_agent.manager()
    output = tmp_path / "out"
    checkpoint = output / "checkpoints/000100/pretrained_model"
    checkpoint.mkdir(parents=True)
    (checkpoint / "config.json").write_text("{}")
    (output / "eval_info.json").write_text('{"aggregated": {"pc_success": 75}}')
    record = {"id": "abc", "state": "running", "kind": "train", "started_at": 1, "output_dir": str(output)}
    manager.records["abc"] = record
    manager._save(record)
    detail = client.get("/jobs/abc").json()
    assert detail["checkpoints"] == [str(checkpoint)]
    assert detail["result"]["aggregated"]["pc_success"] == 75
    assert gpu_agent.Jobs(tmp_path / "jobs", output).get("abc")["state"] == "interrupted"


def test_live_eval_requires_new_dataset(client, tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_agent, "lerobot_home", lambda: tmp_path)
    (tmp_path / "team/eval").mkdir(parents=True)
    res = client.post(
        "/jobs",
        json={"kind": "robot_eval", "dataset": "team/eval", "checkpoint": "team/policy", "robot_ready": True},
    )
    assert res.status_code == 409


def test_robot_lock_blocks_conflicts_and_unknown_status(tmp_path, monkeypatch):
    marker = tmp_path / "live.json"
    marker.write_text("{}")
    monkeypatch.setattr(gpu_remote, "LIVE_FILE", marker)
    monkeypatch.setattr(gpu_remote, "connection", lambda: True)

    def offline(*args, **kwargs):
        raise HTTPException(502, "offline")

    monkeypatch.setattr(gpu_remote, "call", offline)
    with pytest.raises(HTTPException, match="Cannot confirm"):
        gpu_remote.assert_robot_idle()
    monkeypatch.setattr(gpu_remote, "call", lambda *a: {"jobs": [{"kind": "robot_eval", "state": "running"}]})
    with pytest.raises(HTTPException, match="using the robot"):
        gpu_remote.assert_robot_idle()
    monkeypatch.setattr(gpu_remote, "call", lambda *a: {"jobs": []})
    gpu_remote.assert_robot_idle()
    assert not marker.exists()


def test_connection_response_hides_token(tmp_path, monkeypatch):
    config = tmp_path / "connection.json"
    config.write_text(json.dumps({"host": "gpu.local", "port": 8766, "token": "secret-token"}))
    monkeypatch.setattr(gpu_remote, "CONNECTION_FILE", config)
    assert gpu_remote.get_connection() == {"configured": True, "host": "gpu.local", "port": 8766}


def test_reconnect_same_server_with_new_token_during_live_eval(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_remote, "DATA_DIR", tmp_path)
    monkeypatch.setattr(gpu_remote, "CONNECTION_FILE", tmp_path / "connection.json")
    monkeypatch.setattr(gpu_remote, "LIVE_FILE", tmp_path / "live.json")
    old = {"host": "gpu.local", "port": 8766, "token": "old-token"}
    gpu_remote.CONNECTION_FILE.write_text(json.dumps(old))
    gpu_remote.LIVE_FILE.write_text("{}")

    def respond(method, path, target=None):
        assert target.token == "new-token"
        return {"agent": "gpu", "jobs": [{"kind": "robot_eval", "state": "running"}]}

    monkeypatch.setattr(gpu_remote, "call", respond)
    gpu_remote.connect(gpu_remote.Connection(**{**old, "token": "new-token"}))
    assert gpu_remote.connection().token == "new-token"
    assert gpu_remote.CONNECTION_FILE.stat().st_mode & 0o777 == 0o600


def test_panel_live_eval_uses_configured_robot_and_blocks_other_operations(tmp_path, monkeypatch):
    main = importlib.import_module("main")
    monkeypatch.setattr(gpu_remote, "DATA_DIR", tmp_path)
    monkeypatch.setattr(gpu_remote, "LIVE_FILE", tmp_path / "live.json")
    monkeypatch.setattr(gpu_remote, "connection", lambda: True)
    monkeypatch.setattr(
        main, "load_settings", lambda: main.Settings(jetson_ip="10.0.0.9", robot_model="alohamini2")
    )
    monkeypatch.setattr(main.recorder, "recording_dataset", lambda: None)
    monkeypatch.setattr(main.controls, "leader_users", lambda: [])
    monkeypatch.setattr(main.controls, "get", lambda name: None)
    monkeypatch.setattr(main.controls, "jetson_status", lambda settings: {"host_listening": True})
    dispatched = []

    def respond(method, path, body=None):
        if method == "POST":
            dispatched.append(body)
            return {"id": "123"}
        return {"jobs": [{"kind": "robot_eval", "state": "running"}]}

    monkeypatch.setattr(gpu_remote, "call", respond)
    with TestClient(main.app) as c:
        body = {
            "kind": "robot_eval",
            "dataset": "team/eval",
            "checkpoint": "team/policy",
            "robot_ready": True,
            "robot_ip": "wrong.local",
        }
        assert c.post("/api/gpu/jobs", json=body).status_code == 200
        assert dispatched[0]["robot_ip"] == "10.0.0.9"
        assert dispatched[0]["robot_model"] == "alohamini2"
        assert c.post("/api/teleop/start").status_code == 409
        assert (
            c.post("/api/recorder/start", json={"operator": "test", "episode_time_s": 10}).status_code == 409
        )
        assert c.post("/api/calibrate/start", json={"target": "follower"}).status_code == 409
        assert c.post("/api/host/stop").status_code == 409
        assert c.put("/api/settings", json={}).status_code == 409


def test_panel_refuses_eval_when_teleop_running(monkeypatch):
    main = importlib.import_module("main")
    monkeypatch.setattr(main.recorder, "recording_dataset", lambda: None)
    monkeypatch.setattr(main.controls, "leader_users", lambda: ["Teleoperation"])
    with TestClient(main.app) as c:
        res = c.post(
            "/api/gpu/jobs",
            json={
                "kind": "robot_eval",
                "dataset": "team/eval",
                "checkpoint": "team/policy",
                "robot_ready": True,
            },
        )
        assert res.status_code == 409
        assert "Stop recording" in res.json()["detail"]
