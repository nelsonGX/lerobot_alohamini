"""Read-only access to local LeRobot v3 datasets for the panel's browser and preview."""

from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

from config import lerobot_home

REPO_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SKIP_TOP_LEVEL = {"calibration"}


class DatasetNotFound(LookupError):
    pass


def valid_repo_id(repo_id: str) -> bool:
    return bool(REPO_ID_RE.match(repo_id)) and ".." not in repo_id


def dataset_root(repo_id: str) -> Path:
    if not valid_repo_id(repo_id):
        raise DatasetNotFound(f"Invalid dataset name: {repo_id}")
    return lerobot_home() / repo_id


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _read_info(root: Path) -> dict:
    return json.loads((root / "meta" / "info.json").read_text())


def _camera_keys(info: dict) -> list[str]:
    return [k for k, v in info["features"].items() if v.get("dtype") in ("video", "image")]


def _tasks(root: Path) -> list[str]:
    path = root / "meta" / "tasks.parquet"
    if not path.exists():
        return []
    table = pq.read_table(path)
    # Tasks are stored as the pandas index (column "task" or "__index_level_0__").
    for name in ("task", "__index_level_0__"):
        if name in table.column_names:
            return [str(t) for t in table.column(name).to_pylist()]
    return []


def summarize(repo_id: str, root: Path, with_size: bool = True) -> dict:
    info = _read_info(root)
    total_frames = int(info.get("total_frames", 0))
    fps = info.get("fps") or 30
    mtime = max((p.stat().st_mtime for p in (root / "meta").glob("**/*") if p.is_file()), default=0)
    return {
        "repo_id": repo_id,
        "path": str(root),
        "codebase_version": info.get("codebase_version"),
        "robot_type": info.get("robot_type"),
        "fps": fps,
        "total_episodes": int(info.get("total_episodes", 0)),
        "total_frames": total_frames,
        "duration_s": total_frames / fps,
        "cameras": _camera_keys(info),
        "tasks": _tasks(root),
        "size_bytes": _dir_size(root) if with_size else None,
        "modified_at": mtime,
    }


def list_datasets() -> list[dict]:
    home = lerobot_home()
    if not home.exists():
        return []
    found = []
    for ns in sorted(home.iterdir()):
        if not ns.is_dir() or ns.name in SKIP_TOP_LEVEL or ns.name.startswith("."):
            continue
        for ds in sorted(ns.iterdir()):
            # lerobot-edit-dataset leaves "<name>_old" backups after in-place edits.
            if ds.name.endswith("_old") or not (ds / "meta" / "info.json").exists():
                continue
            try:
                found.append(summarize(f"{ns.name}/{ds.name}", ds))
            except (OSError, ValueError, KeyError) as e:
                found.append({"repo_id": f"{ns.name}/{ds.name}", "path": str(ds), "error": str(e)})
    found.sort(key=lambda d: d.get("modified_at", 0), reverse=True)
    return found


def _episodes_table(root: Path):
    files = sorted((root / "meta" / "episodes").glob("chunk-*/file-*.parquet"))
    if not files:
        return []
    rows: list[dict] = []
    for f in files:
        schema = pq.read_schema(f)
        cols = [c for c in schema.names if not c.startswith("stats/")]
        rows.extend(pq.read_table(f, columns=cols).to_pylist())
    rows.sort(key=lambda r: r["episode_index"])
    return rows


def dataset_detail(repo_id: str) -> dict:
    root = dataset_root(repo_id)
    if not (root / "meta" / "info.json").exists():
        raise DatasetNotFound(repo_id)
    summary = summarize(repo_id, root)
    info = _read_info(root)
    fps = summary["fps"]
    cameras = summary["cameras"]
    episodes = []
    for r in _episodes_table(root):
        videos = {}
        for cam in cameras:
            if f"videos/{cam}/chunk_index" not in r:
                continue
            videos[cam] = {
                "chunk": r[f"videos/{cam}/chunk_index"],
                "file": r[f"videos/{cam}/file_index"],
                "from_s": r[f"videos/{cam}/from_timestamp"],
                "to_s": r[f"videos/{cam}/to_timestamp"],
            }
        episodes.append(
            {
                "index": r["episode_index"],
                "length": r["length"],
                "duration_s": r["length"] / fps,
                "tasks": r.get("tasks") or [],
                "videos": videos,
            }
        )
    features = {
        k: {"dtype": v.get("dtype"), "shape": v.get("shape"), "names": v.get("names")}
        for k, v in info["features"].items()
    }
    return {**summary, "episodes": episodes, "features": features}


def _round(values: list, ndigits: int = 3) -> list:
    return [None if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, ndigits) for v in values]


def episode_data(repo_id: str, episode_index: int) -> dict:
    """Per-frame state/action series for one episode, transposed to one list per joint."""
    root = dataset_root(repo_id)
    info = _read_info(root)
    ep = next((r for r in _episodes_table(root) if r["episode_index"] == episode_index), None)
    if ep is None:
        raise DatasetNotFound(f"Episode {episode_index} not found in {repo_id}")
    path = root / info["data_path"].format(chunk_index=ep["data/chunk_index"], file_index=ep["data/file_index"])
    vector_keys = [
        k for k, v in info["features"].items()
        if v.get("dtype") in ("float32", "float64") and v.get("names") and len(v.get("shape") or []) == 1
    ]  # fmt: skip
    schema_names = pq.read_schema(path).names
    columns = ["timestamp", "frame_index", "episode_index"] + [k for k in vector_keys if k in schema_names]
    table = pq.read_table(path, columns=columns, filters=[("episode_index", "=", episode_index)])
    table = table.sort_by("frame_index")
    series: dict[str, dict] = {}
    for key in vector_keys:
        if key not in table.column_names:
            continue
        names = info["features"][key]["names"]
        if isinstance(names, dict):  # older datasets: {"motors": [...]}
            names = next(iter(names.values()))
        flat = pc.list_flatten(table.column(key)).to_numpy(zero_copy_only=False)
        width = len(names)
        matrix = flat.reshape(-1, width)
        series[key] = {name: _round(matrix[:, j].tolist()) for j, name in enumerate(names)}
    return {
        "episode_index": episode_index,
        "fps": info.get("fps"),
        "timestamps": _round(table.column("timestamp").to_pylist(), 4),
        "series": series,
    }


def video_path(repo_id: str, camera: str, chunk: int, file: int) -> Path:
    root = dataset_root(repo_id)
    info = _read_info(root)
    if camera not in _camera_keys(info):
        raise DatasetNotFound(camera)
    path = (root / info["video_path"].format(video_key=camera, chunk_index=chunk, file_index=file)).resolve()
    if not path.is_relative_to(root.resolve()) or not path.exists():
        raise DatasetNotFound(str(path))
    return path


def delete_dataset(repo_id: str) -> None:
    root = dataset_root(repo_id)
    if not (root / "meta" / "info.json").exists():
        raise DatasetNotFound(repo_id)
    shutil.rmtree(root)
    backup = root.with_name(root.name + "_old")
    if backup.exists():
        shutil.rmtree(backup)


def dataset_exists(repo_id: str) -> bool:
    try:
        return (dataset_root(repo_id) / "meta" / "info.json").exists()
    except DatasetNotFound:
        return False
