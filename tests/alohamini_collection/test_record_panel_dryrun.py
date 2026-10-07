"""Dry run of the recording engine on simulated hardware (examples/alohamini/record_panel.py --simulate).

Drives it exactly like the panel does (JSON commands on stdin, state read from the events file) and then checks
the dataset on disk: discarded episodes leave nothing behind, metadata is stored, interruptions never leave a
partial episode, and sessions resume on the one shared dataset.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("av")
pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "examples" / "alohamini"))

import collection as col  # noqa: E402
import yaml  # noqa: E402

DATASET = "test/collect"
EPISODE_S = 6
FPS = 10
GOOD_EPISODE_S = 3.6  # longer than quality.min_episode_s


class EngineProc:
    """One engine process plus a view of its state."""

    def __init__(self, home: Path, config: Path, operator: str, tag: str):
        self.home, self.events = home, home / f"events-{tag}.jsonl"
        env = {**os.environ, "HF_LEROBOT_HOME": str(home), "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, str(REPO / "examples/alohamini/record_panel.py"), "--simulate", "--operator", operator,
             "--events", str(self.events), "--config", str(config), "--episode-time-s", str(EPISODE_S), "--fps", str(FPS)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=REPO,
        )  # fmt: skip
        self.root = home / DATASET

    def rows(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.events.read_text().splitlines() if line.strip()]
        except (OSError, ValueError):
            return []

    def state(self) -> dict | None:
        states = [r["state"] for r in self.rows() if r["type"] == "state"]
        return states[-1] if states else None

    def closed(self) -> dict | None:
        return next((r for r in self.rows() if r["type"] == "closed"), None)

    def send(self, cmd: str, **kw) -> None:
        self.proc.stdin.write(json.dumps({"cmd": cmd, **kw}) + "\n")
        self.proc.stdin.flush()

    def wait_phase(self, phase: str, timeout: float = 90) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            st = self.state()
            if st and st["phase"] == phase:
                return st
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"engine exited ({self.proc.returncode}) while waiting for {phase}: {self.closed()}\n{self.output()}"
                )
            time.sleep(0.05)
        raise AssertionError(
            f"timeout waiting for {phase}; at {(self.state() or {}).get('phase')}\n{self.output()}"
        )

    def output(self) -> str:
        return "" if self.proc.poll() is None else (self.proc.stdout.read() or "")[-3000:]

    def record(self, seconds: float = GOOD_EPISODE_S) -> dict:
        """start -> record for `seconds` -> done; returns the review state."""
        self.wait_phase("ready")
        self.send("start")
        self.wait_phase("recording")
        time.sleep(seconds)
        self.send("done")
        return self.wait_phase("review")

    def exit_code(self, timeout: float = 90) -> int:
        return self.proc.wait(timeout)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()


def info(root: Path) -> dict:
    return json.loads((root / "meta" / "info.json").read_text())


@pytest.fixture(scope="module")
def config(tmp_path_factory) -> Path:
    raw = yaml.safe_load(col.DEFAULT_CONFIG_PATH.read_text())
    raw["dataset"] = DATASET
    raw["checkpoint_every"] = 2  # exercise close-and-reopen after every 2 saved episodes
    path = tmp_path_factory.mktemp("cfg") / "collection.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.fixture(scope="module")
def recorded(tmp_path_factory, config):
    """Session 1: discard, re-record, save, save a faulty one anyway, checkpoint, save again, finish."""
    home = tmp_path_factory.mktemp("home")
    cfg = col.load_config(config)
    e = EngineProc(home, config, "Alex  Tester", "s1")  # also checks that the name is normalised
    try:
        ready = e.wait_phase("ready")
        log = {"first_layout": ready["layout"], "suggested": ready["suggested_task_id"]}
        assert ready["operator"] == "Alex Tester" and ready["episode_number"] == 1

        # 1. a too-short recording is flagged bad -> discard: nothing may reach the dataset
        review = e.record(1.0)
        assert review["review"]["recommend"] == "discard"
        assert {f["code"] for f in review["review"]["flags"]} >= {"too_short"}
        e.send("discard")
        after_discard = e.wait_phase("ready")
        assert (after_discard["discarded"], after_discard["saved"]) == (1, 0)
        assert info(e.root)["total_episodes"] == 0 and not list(e.root.glob("data/**/*.parquet"))
        assert after_discard["layout"]["slots"] != ready["layout"]["slots"]  # moved on to a new layout

        # 2. a good recording, but the operator re-records: same task and layout again, still nothing saved
        e.record()
        before = e.wait_phase("review")
        e.send("rerecord")
        again = e.wait_phase("ready")
        assert again["layout"] == after_discard["layout"] and again["task_id"] == after_discard["task_id"]
        assert again["attempt"] == 2 and again["saved"] == 0 and info(e.root)["total_episodes"] == 0
        assert before["review"]["flags"] == []

        # 3. save it (episode 0), recording the layout/task that were on screen
        layout1, task1 = again["layout"], again["task_id"]
        e.record()
        e.send("save")
        saved1 = e.wait_phase("ready")
        assert saved1["saved"] == 1 and saved1["episode_number"] == 2
        log["ep0"] = (layout1, task1)

        # 4. a camera goes black: flagged bad, but the operator may still save; then the checkpoint kicks in
        e.send("sim_fault", value="black_camera")
        layout2, task2 = saved1["layout"], saved1["task_id"]
        review = e.record()
        assert any(f["code"] == "camera_black" and f["severity"] == "bad" for f in review["review"]["flags"])
        e.send("save")
        e.wait_phase("ready")
        log["ep1"] = (layout2, task2)
        e.send("sim_fault", value=None)

        # 5. recording continues normally after the dataset was closed and reopened
        review = e.record()
        assert review["review"]["flags"] == []
        e.send("save")
        final = e.wait_phase("ready")
        assert final["saved"] == 3 and final["discarded"] == 2  # one discard + one re-record
        e.send("finish")
        assert e.exit_code() == 0
        assert e.closed()["reason"] == "finished"
        log["counts"] = {t["id"]: t["count"] for t in final["tasks"]}
    finally:
        e.kill()
    return {"home": home, "root": home / DATASET, "log": log, "cfg": cfg}


def test_dataset_holds_exactly_the_saved_episodes(recorded):
    root = recorded["root"]
    assert col.validate_dataset(root) == []
    assert info(root)["total_episodes"] == 3  # the discarded and re-recorded ones are not there
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    ds = LeRobotDataset(DATASET, root=root)
    assert ds.meta.total_episodes == 3 and len(ds) == info(root)["total_frames"]
    assert sorted({int(i) for i in ds.hf_dataset["episode_index"]}) == [0, 1, 2]
    allowed = {t.text for t in recorded["cfg"].tasks}
    assert set(ds.meta.tasks.index) <= allowed  # task strings only ever come from the config


def test_every_saved_episode_has_metadata(recorded):
    root, log = recorded["root"], recorded["log"]
    rows = col.read_records(root)
    assert [r["episode_index"] for r in rows] == [0, 1, 2]
    assert {r["operator"] for r in rows} == {"Alex Tester"}
    # the layout and task saved are the ones that were shown for that episode
    assert (rows[0]["layout"]["slots"], rows[0]["task_id"]) == (log["ep0"][0]["slots"], log["ep0"][1])
    assert rows[0]["attempt"] == 2 and rows[1]["attempt"] == 1
    for r in rows:
        lay = r["layout"]
        assert sorted(lay["slots"].values()) == sorted(o.id for o in recorded["cfg"].objects)
        assert lay["slots"][lay["target_slot"]] == lay["target_object"]
        assert r["frames"] > 20 and r["health"]["fps"] > 5 and r["robot"]["simulated"] is True
    assert rows[1]["flagged"] is True and "camera_black" in {f["code"] for f in rows[1]["flags"]}
    assert rows[0]["flagged"] is False and rows[2]["flagged"] is False
    # frames in the sidecar match what the dataset says
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    ds = LeRobotDataset(DATASET, root=root)
    assert [ds.meta.episodes[i]["length"] for i in range(3)] == [r["frames"] for r in rows]


def test_task_counts_come_from_the_dataset(recorded):
    counts = col.task_counts(recorded["root"], recorded["cfg"])
    assert sum(v for k, v in counts.items() if k != "_other") == 3 and counts["_other"] == 0
    assert counts == {**recorded["log"]["counts"], "_other": 0}


def test_interrupt_mid_episode_leaves_no_partial_episode(recorded, config, tmp_path):
    home = tmp_path / "home"
    shutil.copytree(recorded["home"] / "test", home / "test")
    root = home / DATASET
    frames_before = info(root)["total_frames"]
    e = EngineProc(home, config, "Sam", "s2")
    try:
        ready = e.wait_phase("ready")
        assert ready["episode_number"] == 4  # resumed the shared dataset
        assert {t["id"]: t["count"] for t in ready["tasks"]} == recorded["log"]["counts"]
        e.send("start")
        e.wait_phase("recording")
        time.sleep(2)  # well into an episode, frames are buffered and being encoded
        e.proc.send_signal(signal.SIGINT)
        assert e.exit_code() == 0
        assert e.closed()["reason"] == "interrupted"
    finally:
        e.kill()
    assert col.validate_dataset(root) == []
    assert info(root)["total_episodes"] == 3 and info(root)["total_frames"] == frames_before
    assert len(col.read_records(root)) == 3
    assert not list(root.glob("**/*.tmp"))
    assert not (root / "images").exists() or not any(
        (root / "images").rglob("*.*")
    )  # no frames left over from the lost episode


def test_resume_second_operator_and_exclusive_writer(recorded, config, tmp_path):
    home = tmp_path / "home"
    shutil.copytree(recorded["home"] / "test", home / "test")
    root = home / DATASET
    last_layout = col.read_records(root)[-1]["layout"]["slots"]
    e = EngineProc(home, config, "Robin", "s3")
    try:
        ready = e.wait_phase("ready")
        assert (
            ready["layout"]["slots"] != last_layout
        )  # first layout of a new session differs from the last one
        # only one writer at a time
        other = EngineProc(home, config, "Intruder", "s3b")
        assert other.exit_code() == 1
        assert "already being recorded" in other.closed()["error"]
        e.record()
        e.send("save")
        saved = e.wait_phase("ready")
        assert saved["saved"] == 1 and saved["episode_number"] == 5
        # the panel dying (stdin closed) mid-episode must not save the half episode either
        e.send("start")
        e.wait_phase("recording")
        time.sleep(1.5)
        e.proc.stdin.close()
        assert e.exit_code() == 0 and e.closed()["reason"] == "interrupted"
    finally:
        e.kill()
    assert col.validate_dataset(root) == []
    rows = col.read_records(root)
    assert [(r["episode_index"], r["operator"]) for r in rows] == [
        (0, "Alex Tester"),
        (1, "Alex Tester"),
        (2, "Alex Tester"),
        (3, "Robin"),
    ]
    assert info(root)["total_episodes"] == 4


def test_a_damaged_dataset_is_refused(recorded, config, tmp_path):
    home = tmp_path / "home"
    shutil.copytree(recorded["home"] / "test", home / "test")
    victim = sorted((home / DATASET / "data").glob("chunk-*/file-*.parquet"))[-1]
    victim.write_bytes(victim.read_bytes()[:64])  # what a crash before the parquet footer leaves behind
    e = EngineProc(home, config, "Sam", "s4")
    try:
        assert e.exit_code() == 1
        assert "damaged" in e.closed()["error"] and "unreadable" in e.closed()["error"]
    finally:
        e.kill()
    assert col.validate_dataset(home / DATASET)
