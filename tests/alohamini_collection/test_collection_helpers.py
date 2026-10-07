"""Unit tests for the data-collection helpers (examples/alohamini/collection.py): no hardware, no dataset writes."""

import json
import random
import sys
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("yaml")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples" / "alohamini"))

import collection as col  # noqa: E402


@pytest.fixture
def cfg():
    return col.load_config()


def test_default_config_is_valid_and_has_three_tasks(cfg):
    assert len(cfg.objects) == 3 and len(cfg.slots) == 3 and len(cfg.tasks) == 3
    assert all(t.target == 50 for t in cfg.tasks)


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda c: c["tasks"].append(dict(c["tasks"][0])), "unique"),
        (lambda c: c["tasks"][0].update(object="nope"), "one of `objects`"),
        (lambda c: c.update(dataset="no-slash"), "namespace/name"),
        (lambda c: c.update(slots=["a", "b"]), "slots"),
    ],
)
def test_invalid_config_is_rejected(tmp_path, mutation, message):
    import yaml

    raw = yaml.safe_load(col.DEFAULT_CONFIG_PATH.read_text())
    mutation(raw)
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(col.ConfigError, match=message):
        col.load_config(path)


def test_suggest_task_is_the_one_furthest_behind(cfg):
    assert (
        col.suggest_task({"pick_red_cube": 10, "pick_blue_ball": 3, "pick_green_cup": 7}, cfg)
        == "pick_blue_ball"
    )
    # ties go to the earlier task in the config; reached targets are skipped; nothing left -> None
    assert col.suggest_task({}, cfg) == "pick_red_cube"
    assert (
        col.suggest_task({"pick_red_cube": 50, "pick_blue_ball": 50, "pick_green_cup": 49}, cfg)
        == "pick_green_cup"
    )
    assert col.suggest_task({t.id: 50 for t in cfg.tasks}, cfg) is None


def test_layouts_are_permutations_and_never_repeat_the_previous(cfg):
    rng = random.Random(0)
    layout = col.new_layout(cfg, rng)
    for _ in range(200):
        nxt = col.new_layout(cfg, rng, layout)
        assert sorted(nxt["slots"].values()) == sorted(o.id for o in cfg.objects)
        assert nxt["slots"] != layout["slots"]
        layout = nxt
    seen = {col.new_layout(cfg, rng)["id"] for _ in range(300)}
    assert len(seen) == 6  # all 3! arrangements show up


def test_describe_layout_marks_the_target(cfg):
    layout = {"slots": {"left": "green_cup", "center": "red_cube", "right": "blue_ball"}, "id": "x"}
    d = col.describe_layout(cfg, layout, cfg.task("pick_red_cube"))
    assert d["target_slot"] == "center" and d["names"]["left"] == "green cup"


def _stats(**over):
    base = {"frames": 300, "duration_s": 10.0, "fps": 30.0, "max_gap_s": 0.05, "gaps": 0, "stale_frac": 0.0,
            "latency_mean_ms": 20.0, "latency_p95_ms": 30.0, "latency_max_ms": 40.0, "arm_motion": 50.0,
            "black_frac": {"forward": 0.0, "overhead": 0.0}}  # fmt: skip
    return {**base, **over}


def _codes(stats, cfg, **kw):
    kw = {"target_fps": 30, "episode_time_s": 60, **kw}
    return {(f["code"], f["severity"]) for f in col.episode_flags(stats, cfg.quality, **kw)}


def test_a_healthy_episode_has_no_flags(cfg):
    assert _codes(_stats(), cfg) == set()


@pytest.mark.parametrize(
    "over, expected",
    [
        ({"duration_s": 1.0}, ("too_short", "bad")),
        ({"fps": 20.0}, ("low_fps", "bad")),
        ({"max_gap_s": 0.8, "gaps": 2}, ("frame_gaps", "bad")),
        ({"stale_frac": 0.3}, ("stale_frames", "bad")),
        ({"black_frac": {"forward": 0.9, "overhead": 0.0}}, ("camera_black", "bad")),
        ({"arm_motion": 0.1}, ("no_motion", "bad")),
        ({"latency_p95_ms": 400.0}, ("high_latency", "warn")),
        ({"duration_s": 59.8}, ("hit_time_limit", "warn")),
    ],
)
def test_each_problem_is_flagged(cfg, over, expected):
    assert expected in _codes(_stats(**over), cfg)


def test_empty_episode_is_flagged_bad(cfg):
    assert _codes({"frames": 0, "duration_s": 0.0}, cfg) == {("empty", "bad")}


def test_health_monitor_measures_rate_gaps_stale_and_black_cameras(cfg):
    mon = col.HealthMonitor(fps=10, cameras=["forward"], quality=cfg.quality)
    mon.begin_episode()
    import numpy as np

    img, black = np.full((32, 32, 3), 100, np.uint8), np.zeros((32, 32, 3), np.uint8)
    t = 0.0
    for i in range(50):
        t += 0.1 if i != 25 else 0.9  # one 0.9 s stall
        mon.frame(fresh=(i % 10 != 0), latency_s=0.02, images={"forward": black if i > 40 else img},
                  action={"arm_left_x.pos": float(i)}, now=t)  # fmt: skip
    s = mon.end_episode()
    assert s["frames"] == 50 and s["gaps"] == 1 and s["max_gap_s"] == pytest.approx(0.9)
    assert s["stale_frac"] == pytest.approx(0.1) and s["arm_motion"] == 49.0
    assert s["black_frac"]["forward"] == pytest.approx(9 / 50)
    live = mon.live()
    assert live["cameras"]["forward"] == "black" and live["ok"] == "bad"


def test_sidecar_reconcile_fills_stubs_and_drops_orphans(tmp_path):
    for i in (0, 1, 5):
        col.append_record(tmp_path, {"episode_index": i, "operator": "a"})
    with open(col.sidecar_path(tmp_path), "a") as f:
        f.write('{"episode_index": 2, "oper')  # torn write from a crash
    notes = col.reconcile_records(tmp_path, total_episodes=3)
    rows = col.read_records(tmp_path)
    assert [r["episode_index"] for r in rows] == [0, 1, 2]
    assert rows[2]["stub"] is True and len(notes) == 2


def test_carry_records_after_delete_renumbers(tmp_path):
    old, new = tmp_path / "ds_old", tmp_path / "ds"
    for i in range(5):
        col.append_record(old, {"episode_index": i, "uid": f"u{i}"})
    col.carry_records_after_delete(old, new, deleted=[1, 3])
    assert [(r["episode_index"], r["uid"]) for r in col.read_records(new)] == [
        (0, "u0"),
        (1, "u2"),
        (2, "u4"),
    ]


def test_validate_dataset_reports_garbage(tmp_path):
    assert col.validate_dataset(tmp_path)  # no info.json
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "info.json").write_text(json.dumps({"total_episodes": 0, "total_frames": 0}))
    assert col.validate_dataset(tmp_path) == []


def test_dataset_lock_is_exclusive(tmp_path):
    import os

    root = tmp_path / "ns" / "ds"
    lock = root.parent / f".{root.name}.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getppid()))  # a live process that is not us
    with pytest.raises(col.DatasetLockedError), col.dataset_lock(root):
        pass
    lock.write_text("999999999")  # stale lock from a dead process is taken over
    with col.dataset_lock(root):
        assert lock.read_text() == str(os.getpid())
    assert not lock.exists()


def test_save_plan_edits_objects_and_tasks_and_rejects_bad_input(tmp_path):
    import shutil

    path = tmp_path / "c.yaml"
    shutil.copy(col.DEFAULT_CONFIG_PATH, path)
    cfg = col.load_config(path)
    items = [
        {"id": o.id, "name": o.name, "color": o.color, "task_text": next(t.text for t in cfg.tasks if t.object == o.id), "target": 50}
        for o in cfg.objects
    ]
    items[0].update(name="yellow duck", color="#ffcc00", task_text="pick up the yellow duck", target=30)
    new = col.save_plan(items, path)
    assert new.objects[0].name == "yellow duck" and new.objects[0].color == "#ffcc00"
    assert next(t for t in new.tasks if t.object == items[0]["id"]).text == "pick up the yellow duck"
    assert new.dataset == cfg.dataset and new.quality == cfg.quality  # everything else is kept
    items[1]["task_text"] = items[0]["task_text"]  # duplicate sentence
    with pytest.raises(col.ConfigError):
        col.save_plan(items, path)
    assert col.load_config(path).objects[0].name == "yellow duck"  # a rejected edit leaves the file alone
