"""Helpers shared by the recording engine (record_panel.py) and the web panel's backend.

Stdlib + numpy + pyarrow + yaml only: the panel imports this without pulling in torch.

  * the data-collection plan (collection.yaml): fixed task list, objects, targets, quality limits
  * per-task episode counts read from the dataset, and the "furthest behind" suggestion
  * random 3-object layouts
  * a sidecar `meta/collection.jsonl` with one row of metadata per saved episode
  * stream-health tracking and automatic episode flags
  * dataset validation and a single-writer lock
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import time
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import yaml

DEFAULT_CONFIG_PATH = Path(__file__).with_name("collection.yaml")
SIDECAR_NAME = "collection.jsonl"
REPO_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    pass


class DatasetProblemError(RuntimeError):
    """The dataset on disk is not in a state we can safely append to or upload."""


class DatasetLockedError(RuntimeError):
    pass


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class Obj:
    id: str
    name: str
    color: str = "#888888"


@dataclass(frozen=True)
class Task:
    id: str
    text: str
    object: str
    target: int


@dataclass(frozen=True)
class Quality:
    min_episode_s: float = 3.0
    min_fps_ratio: float = 0.90
    max_gap_s: float = 0.30
    max_stale_frac: float = 0.10
    max_latency_p95_s: float = 0.25
    min_arm_motion: float = 2.0
    black_camera_frac: float = 0.20
    time_limit_margin_s: float = 0.5


@dataclass(frozen=True)
class CollectionConfig:
    dataset: str
    objects: tuple[Obj, ...]
    slots: tuple[str, ...]
    tasks: tuple[Task, ...]
    quality: Quality
    hub_repo_id: str = ""
    rsync_dest: str = ""
    checkpoint_every: int = 1
    source: str = ""

    def task(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    def obj(self, obj_id: str) -> Obj:
        return next(o for o in self.objects if o.id == obj_id)

    def public(self) -> dict:
        """What the UI needs; no free-text fields exist anywhere in here."""
        return {
            "dataset": self.dataset,
            "objects": [o.__dict__ for o in self.objects],
            "slots": list(self.slots),
            "tasks": [t.__dict__ for t in self.tasks],
            "upload": {
                "hub": bool(self.hub_repo_id),
                "rsync": bool(self.rsync_dest),
                "hub_repo_id": self.hub_repo_id,
                "rsync_dest": self.rsync_dest,
            },  # fmt: skip
            "source": self.source,
        }


def load_config(path: str | os.PathLike | None = None) -> CollectionConfig:
    path = Path(path or os.environ.get("COLLECTION_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"Cannot read {path}: {e}") from e

    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise ConfigError(f"{path.name}: {msg}")

    dataset = str(raw.get("dataset", ""))
    need(bool(REPO_ID_RE.match(dataset)), "`dataset` must look like namespace/name")
    objects = tuple(
        Obj(str(o["id"]), str(o["name"]), str(o.get("color", "#888888"))) for o in raw.get("objects", [])
    )
    need(
        len(objects) >= 2 and len({o.id for o in objects}) == len(objects),
        "`objects` needs 2+ objects with unique ids",
    )
    slots = tuple(str(s) for s in raw.get("slots", []))
    need(
        len(slots) == len(objects) and len(set(slots)) == len(slots),
        "`slots` needs one unique name per object",
    )
    default_target = int(raw.get("default_target", 50))
    need(default_target > 0, "`default_target` must be positive")
    tasks = tuple(
        Task(str(t["id"]), str(t["text"]).strip(), str(t["object"]), int(t.get("target", default_target)))
        for t in raw.get("tasks", [])
    )
    need(len(tasks) > 0, "`tasks` is empty")
    need(len({t.id for t in tasks}) == len(tasks), "task ids must be unique")
    need(len({t.text for t in tasks}) == len(tasks), "task texts must be unique")
    need(
        all(len(t.text) >= 3 and t.target > 0 for t in tasks),
        "task text needs 3+ characters and a positive target",
    )
    need(
        all(t.object in {o.id for o in objects} for t in tasks),
        "every task `object` must be one of `objects`",
    )
    quality = Quality(**{k: float(v) for k, v in (raw.get("quality") or {}).items()})
    upload = raw.get("upload") or {}
    return CollectionConfig(
        dataset=dataset,
        objects=objects,
        slots=slots,
        tasks=tasks,
        quality=quality,
        hub_repo_id=str(upload.get("hub_repo_id") or "").strip(),
        rsync_dest=str(upload.get("rsync_dest") or "").strip(),
        checkpoint_every=int(raw.get("checkpoint_every", 1)),
        source=str(path),
    )


def save_plan(items: list[dict], path: str | os.PathLike | None = None) -> CollectionConfig:
    """Edit each object's name/colour and its task's sentence/target (matched by object id); other settings stay.

    The file is validated before it replaces the old one. (YAML comments are not preserved.)
    """
    path = Path(path or os.environ.get("COLLECTION_CONFIG") or DEFAULT_CONFIG_PATH)
    raw = yaml.safe_load(path.read_text())
    by_id = {str(i.get("id")): i for i in items}
    if set(by_id) != {o["id"] for o in raw["objects"]}:
        raise ConfigError("The list of objects does not match the plan")
    for o in raw["objects"]:
        o["name"], o["color"] = str(by_id[o["id"]]["name"]).strip(), str(by_id[o["id"]]["color"])
    for t in raw["tasks"]:
        item = by_id[t["object"]]
        t["text"], t["target"] = str(item["task_text"]).strip(), int(item["target"])
    header = "# Data-collection plan. Edited from the panel's Settings; see RECORDING.md for what each field does.\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(header + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    try:
        load_config(tmp)
    except ConfigError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)
    return load_config(path)


# ---------------------------------------------------------------- counts and suggestion


def _episode_files(root: Path) -> list[Path]:
    return sorted((root / "meta" / "episodes").glob("chunk-*/file-*.parquet"))


def task_counts(root: Path, cfg: CollectionConfig) -> dict[str, int]:
    """Saved episodes per task id, read from the dataset itself. Unknown task strings count as `_other`."""
    by_text = {t.text: t.id for t in cfg.tasks}
    counts = {t.id: 0 for t in cfg.tasks}
    counts["_other"] = 0
    for f in _episode_files(root):
        try:
            tasks = pq.read_table(f, columns=["tasks"]).column("tasks").to_pylist()
        except Exception as e:  # noqa: BLE001 - unreadable file = interrupted writer
            raise DatasetProblemError(
                f"{f.relative_to(root)} is unreadable ({e}); the dataset needs repair"
            ) from e
        for ep_tasks in tasks:
            for text in set(ep_tasks or []):
                counts[by_text.get(text, "_other")] += 1
    return counts


def suggest_task(counts: dict[str, int], cfg: CollectionConfig) -> str | None:
    """The task furthest behind its target (lowest share done, then most episodes missing). None = all done."""
    behind = [(counts.get(t.id, 0) / t.target, -(t.target - counts.get(t.id, 0)), i, t.id)
              for i, t in enumerate(cfg.tasks) if counts.get(t.id, 0) < t.target]  # fmt: skip
    return min(behind)[3] if behind else None


# ---------------------------------------------------------------- layouts


def new_layout(cfg: CollectionConfig, rng: random.Random, previous: dict | None = None) -> dict:
    """Random assignment of the objects to the slots; differs from `previous` so the operator really rearranges."""
    ids = [o.id for o in cfg.objects]
    prev = [previous["slots"][s] for s in cfg.slots] if previous else None
    order = rng.sample(ids, len(ids))
    for _ in range(50):
        if order != prev:
            break
        order = rng.sample(ids, len(ids))
    return {"slots": dict(zip(cfg.slots, order, strict=True)), "id": "|".join(order)}


def describe_layout(cfg: CollectionConfig, layout: dict, task: Task) -> dict:
    """Layout + names + where the target sits, as stored per episode."""
    slots = layout["slots"]
    return {
        "id": layout["id"],
        "slots": dict(slots),
        "names": {s: cfg.obj(o).name for s, o in slots.items()},
        "target_object": task.object,
        "target_slot": next(s for s, o in slots.items() if o == task.object),
    }


# ---------------------------------------------------------------- per-episode sidecar metadata


def sidecar_path(root: Path) -> Path:
    return root / "meta" / SIDECAR_NAME


def append_record(root: Path, record: dict) -> None:
    """Append one episode's metadata durably (fsync): it must not be lost if the process dies right after."""
    path = sidecar_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_records(root: Path) -> list[dict]:
    path = sidecar_path(root)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):  # a torn last line from a crash
            rows.append(json.loads(line))
    return rows


def _write_records(root: Path, rows: list[dict]) -> None:
    path = sidecar_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def reconcile_records(root: Path, total_episodes: int) -> list[str]:
    """Make the sidecar match the dataset: one row per episode, none for episodes that do not exist."""
    notes: list[str] = []
    rows = {r["episode_index"]: r for r in read_records(root) if isinstance(r.get("episode_index"), int)}
    extra = sorted(i for i in rows if i >= total_episodes)
    if extra:
        notes.append(f"dropped metadata for episodes that are not in the dataset: {extra}")
    missing = [i for i in range(total_episodes) if i not in rows]
    if missing:
        notes.append(f"episodes {missing} had no metadata (recorded outside the panel?)")
    if extra or missing:
        kept = [rows[i] for i in range(total_episodes) if i in rows]
        stubs = [{"episode_index": i, "stub": True, "operator": None, "flags": []} for i in missing]
        _write_records(root, sorted(kept + stubs, key=lambda r: r["episode_index"]))
    return notes


def carry_records_after_delete(old_root: Path, new_root: Path, deleted: Iterable[int]) -> None:
    """lerobot-edit-dataset rebuilds the dataset without our sidecar and renumbers the episodes that remain.

    Copy the sidecar from the backup (`<name>_old`) into the new dataset, dropping the deleted episodes and
    shifting the others down to their new indices.
    """
    gone = sorted(set(deleted))
    rows = [r for r in read_records(old_root) if r.get("episode_index") not in gone]
    for r in rows:
        r["episode_index"] -= sum(1 for d in gone if d < r["episode_index"])
    _write_records(new_root, rows)


# ---------------------------------------------------------------- dataset validation and lock


def validate_dataset(root: Path) -> list[str]:
    """Problems that make the dataset unsafe to append to or upload (empty list = fine)."""
    info_path = root / "meta" / "info.json"
    try:
        info = json.loads(info_path.read_text())
    except (OSError, ValueError) as e:
        return [f"meta/info.json is unreadable ({e})"]
    problems: list[str] = []
    total = int(info.get("total_episodes", 0))
    rows, frames, data_files = 0, 0, set()
    for f in _episode_files(root):
        try:
            table = pq.read_table(
                f, columns=["episode_index", "length", "data/chunk_index", "data/file_index"]
            )
        except Exception as e:  # noqa: BLE001
            problems.append(f"{f.relative_to(root)} is unreadable ({e})")
            continue
        rows += table.num_rows
        frames += sum(table.column("length").to_pylist())
        data_files |= set(
            zip(
                table.column("data/chunk_index").to_pylist(),
                table.column("data/file_index").to_pylist(),
                strict=True,
            )
        )
    for chunk, file in sorted(data_files):
        p = root / info["data_path"].format(chunk_index=chunk, file_index=file)
        try:
            pq.ParquetFile(p).metadata  # noqa: B018 - raises if the footer was never written
        except Exception as e:  # noqa: BLE001
            problems.append(f"{p.relative_to(root)} is unreadable ({e})")
    if not problems and rows != total:
        problems.append(f"info.json says {total} episodes but the episode table has {rows}")
    if not problems and frames != int(info.get("total_frames", 0)):
        problems.append(f"info.json says {info.get('total_frames')} frames but episodes add up to {frames}")
    return problems


@contextlib.contextmanager
def dataset_lock(root: Path) -> Iterator[None]:
    """One writer per dataset. Lives next to the dataset folder (LeRobot refuses to create into a non-empty one)."""
    path = root.parent / f".{root.name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        other = int(path.read_text().strip())
        os.kill(other, 0)
    except (OSError, ValueError):
        pass
    else:
        if other != os.getpid():
            raise DatasetLockedError(f"{root.name} is already being recorded into (process {other})")
    path.write_text(str(os.getpid()))
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            if path.read_text().strip() == str(os.getpid()):
                path.unlink()


# ---------------------------------------------------------------- stream health and quality flags


class HealthMonitor:
    """Per-frame stream statistics: a rolling window for the live display and accumulators per episode."""

    WINDOW_S = 3.0

    def __init__(self, fps: int, cameras: Iterable[str], quality: Quality) -> None:
        self.fps = fps
        self.cameras = list(cameras)
        self.q = quality
        self._recent: deque[tuple[float, float | None, bool, float | None]] = deque()  # t, dt, fresh, latency
        self._last_t: float | None = None
        self._cam_black: dict[str, bool] = dict.fromkeys(self.cameras, False)
        self._ep: dict | None = None

    # -- feeding

    def reset_clock(self) -> None:
        """Call after anything slow that is not a stream problem (saving an episode, reopening the dataset)."""
        self._last_t = None

    def frame(
        self,
        *,
        fresh: bool,
        latency_s: float | None,
        images: dict | None = None,
        action: dict | None = None,
        now: float | None = None,
    ) -> None:
        now = time.perf_counter() if now is None else now
        dt = None if self._last_t is None else now - self._last_t
        self._last_t = now
        self._recent.append((now, dt, fresh, latency_s))
        while self._recent and now - self._recent[0][0] > self.WINDOW_S:
            self._recent.popleft()
        black = {}
        for name in self.cameras:
            img = (images or {}).get(name)
            black[name] = img is None or float(np.asarray(img)[::16, ::16].mean()) < 3.0
        self._cam_black = black
        if (ep := self._ep) is not None:
            ep["n"] += 1
            ep["t0"] = now if ep["t0"] is None else ep["t0"]
            ep["t1"] = now
            if dt is not None:
                ep["dts"].append(dt)
            ep["stale"] += 0 if fresh else 1
            if latency_s is not None and fresh:
                ep["lat"].append(latency_s)
            for name, is_black in black.items():
                ep["black"][name] += int(is_black)
            for key, val in (action or {}).items():
                if key.startswith("arm_") and isinstance(val, (int, float)):
                    lo, hi = ep["motion"].get(key, (val, val))
                    ep["motion"][key] = (min(lo, val), max(hi, val))

    # -- reading

    def live(self) -> dict:
        """What the operator sees while connected: capture rate, latency, worst pause, stale share, cameras."""
        rec = list(self._recent)
        if len(rec) < 3:
            return {"ok": None, "fps": None, "target_fps": self.fps}
        span = rec[-1][0] - rec[0][0]
        fps = (len(rec) - 1) / span if span > 0 else None
        lat = [r[3] for r in rec if r[3] is not None and r[2]]
        gaps = [r[1] for r in rec if r[1] is not None]
        stale = sum(1 for r in rec if not r[2]) / len(rec)
        out = {
            "fps": fps,
            "target_fps": self.fps,
            "latency_ms": None if not lat else float(np.mean(lat)) * 1000,
            "max_gap_ms": max(gaps) * 1000 if gaps else None,
            "stale_pct": stale * 100,
            "cameras": {n: ("black" if b else "ok") for n, b in self._cam_black.items()},
            "limits": {
                "min_fps_ratio": self.q.min_fps_ratio,
                "max_gap_s": self.q.max_gap_s,
                "max_stale_frac": self.q.max_stale_frac,
                "max_latency_p95_s": self.q.max_latency_p95_s,
            },  # fmt: skip
        }
        bad = (
            (fps is not None and fps < self.fps * self.q.min_fps_ratio)
            or (gaps and max(gaps) > self.q.max_gap_s)
            or stale > self.q.max_stale_frac
            or any(b for b in self._cam_black.values())
        )
        warn = bool(lat) and float(np.percentile(lat, 95)) > self.q.max_latency_p95_s
        out["ok"] = "bad" if bad else "warn" if warn else "good"
        return out

    def begin_episode(self) -> None:
        self._ep = {"n": 0, "t0": None, "t1": None, "dts": [], "stale": 0, "lat": [],
                    "black": dict.fromkeys(self.cameras, 0), "motion": {}}  # fmt: skip

    def end_episode(self) -> dict:
        ep, self._ep = self._ep, None
        if ep is None or ep["n"] == 0:
            return {"frames": 0, "duration_s": 0.0}
        dts, lat = ep["dts"], ep["lat"]
        duration = (ep["t1"] - ep["t0"]) + 1 / self.fps
        motion = max((hi - lo for lo, hi in ep["motion"].values()), default=0.0)
        return {
            "frames": ep["n"],
            "duration_s": duration,
            "fps": ep["n"] / duration,
            "max_gap_s": max(dts, default=0.0),
            "gaps": sum(1 for d in dts if d > self.q.max_gap_s),
            "stale_frac": ep["stale"] / ep["n"],
            "latency_mean_ms": float(np.mean(lat)) * 1000 if lat else None,
            "latency_p95_ms": float(np.percentile(lat, 95)) * 1000 if lat else None,
            "latency_max_ms": max(lat) * 1000 if lat else None,
            "arm_motion": float(motion),
            "black_frac": {n: c / ep["n"] for n, c in ep["black"].items()},
        }


def episode_flags(stats: dict, q: Quality, *, target_fps: int, episode_time_s: float) -> list[dict]:
    """Automatic quality flags. severity `bad` = recommend discarding; `warn` = look at it."""
    if not stats.get("frames"):
        return [{"code": "empty", "severity": "bad", "message": "No frames were recorded."}]
    flags: list[dict] = []

    def add(code: str, severity: str, message: str) -> None:
        flags.append({"code": code, "severity": severity, "message": message})

    if stats["duration_s"] < q.min_episode_s:
        add(
            "too_short",
            "bad",
            f"Only {stats['duration_s']:.1f}s long (minimum {q.min_episode_s:g}s) - a mis-press?",
        )
    if stats["fps"] < target_fps * q.min_fps_ratio:
        add("low_fps", "bad", f"Captured {stats['fps']:.1f} fps instead of {target_fps}.")
    if stats["max_gap_s"] > q.max_gap_s:
        add(
            "frame_gaps",
            "bad",
            f"{stats['gaps']} pause(s) between frames, longest {stats['max_gap_s'] * 1000:.0f} ms.",
        )
    if stats["stale_frac"] > q.max_stale_frac:
        add("stale_frames", "bad", f"{stats['stale_frac'] * 100:.0f}% of frames had no new robot data.")
    for cam, frac in stats["black_frac"].items():
        if frac > q.black_camera_frac:
            add("camera_black", "bad", f"Camera '{cam}' was black/missing in {frac * 100:.0f}% of frames.")
    if stats["arm_motion"] < q.min_arm_motion:
        add("no_motion", "bad", "The arms barely moved.")
    if stats["latency_p95_ms"] is not None and stats["latency_p95_ms"] / 1000 > q.max_latency_p95_s:
        add(
            "high_latency", "warn", f"Robot link was slow (95th percentile {stats['latency_p95_ms']:.0f} ms)."
        )
    if stats["duration_s"] >= episode_time_s - q.time_limit_margin_s:
        add("hit_time_limit", "warn", "Ran into the time limit - was the task finished?")
    return flags


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
