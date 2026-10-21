"""Checkpoint discovery uses only metadata, without loading model tensors."""

import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lerobot-panel/server"))
scan_checkpoints = importlib.import_module("gpu_checkpoints").scan_checkpoints


def save_checkpoint(path, policy="act"):
    path.mkdir(parents=True)
    (path / "config.json").write_text(json.dumps({"type": policy}))
    (path / "model.safetensors").write_bytes(b"weights")
    return path


def test_discovers_external_runs_and_deduplicates_last(tmp_path):
    checkpoints = tmp_path / "train/manual-run/checkpoints"
    first = save_checkpoint(checkpoints / "9/pretrained_model")
    last = save_checkpoint(checkpoints / "10/pretrained_model", "smolvla")
    (checkpoints / "last").symlink_to("10", target_is_directory=True)
    result = scan_checkpoints(tmp_path)
    assert result["root"] == str(tmp_path)
    assert len(result["runs"]) == 1
    run = result["runs"][0]
    assert run["name"] == "train/manual-run"
    assert [c["path"] for c in run["checkpoints"]] == [str(last), str(first)]
    assert [c["latest"] for c in run["checkpoints"]] == [True, False]
    assert run["checkpoints"][0]["policy"] == "smolvla"


def test_valid_last_takes_priority_and_broken_last_falls_back(tmp_path):
    checkpoints = tmp_path / "panel/run/checkpoints"
    save_checkpoint(checkpoints / "000010/pretrained_model")
    save_checkpoint(checkpoints / "000020/pretrained_model")
    link = checkpoints / "last"
    link.symlink_to("000010", target_is_directory=True)
    entries = scan_checkpoints(tmp_path)["runs"][0]["checkpoints"]
    assert next(c for c in entries if c["latest"])["step"] == 10
    link.unlink()
    link.symlink_to("missing", target_is_directory=True)
    entries = scan_checkpoints(tmp_path)["runs"][0]["checkpoints"]
    assert next(c for c in entries if c["latest"])["step"] == 20


def test_skips_missing_empty_or_invalid_files(tmp_path):
    checkpoints = tmp_path / "run/checkpoints"
    incomplete = save_checkpoint(checkpoints / "10/pretrained_model")
    (incomplete / "model.safetensors").unlink()
    empty = save_checkpoint(checkpoints / "20/pretrained_model")
    (empty / "model.safetensors").write_bytes(b"")
    malformed = save_checkpoint(checkpoints / "30/pretrained_model")
    (malformed / "config.json").write_text("{partial")
    assert scan_checkpoints(tmp_path)["runs"] == []


def test_standalone_export_and_missing_outputs(tmp_path):
    assert scan_checkpoints(tmp_path / "missing")["runs"] == []
    exported = save_checkpoint(tmp_path / "exported/pretrained_model", "diffusion")
    entry = scan_checkpoints(tmp_path)["runs"][0]["checkpoints"][0]
    assert entry["path"] == str(exported)
    assert entry["latest"] is True
    assert entry["step"] is None


def test_symlinks_cannot_escape_outputs(tmp_path):
    outside = save_checkpoint(tmp_path / "outside/pretrained_model")
    root = tmp_path / "outputs"
    checkpoints = root / "run/checkpoints"
    checkpoints.mkdir(parents=True)
    (checkpoints / "last").symlink_to(outside.parent, target_is_directory=True)
    (root / "loop").symlink_to(root, target_is_directory=True)
    assert scan_checkpoints(root)["runs"] == []
