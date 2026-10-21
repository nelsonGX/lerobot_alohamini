"""Discover saved policies in outputs, including runs started outside the panel."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _checkpoint(path: Path, root: Path) -> dict | None:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            return None
        config = json.loads((resolved / "config.json").read_text())
        weights = resolved / "model.safetensors"
        if not isinstance(config, dict) or not weights.is_file() or weights.stat().st_size == 0:
            return None
        name = resolved.parent.name if resolved.name == "pretrained_model" else resolved.name
        return {
            "path": str(resolved),
            "label": name,
            "step": int(name) if name.isdigit() else None,
            "policy": config.get("type", "unknown"),
            "modified_at": weights.stat().st_mtime,
        }
    except (OSError, ValueError, RuntimeError):
        # A checkpoint may be removed or still being written while we scan.
        return None


def scan_checkpoints(root: Path) -> dict:
    root = root.resolve()
    runs = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        folder = Path(directory)
        if "checkpoints" in dirs:
            dirs.remove("checkpoints")  # Skip optimizer states and other large subtrees.
            checkpoint_dir = folder / "checkpoints"
            try:
                children = sorted(checkpoint_dir.iterdir())
            except OSError:
                continue
            found = {}
            last_path = None
            for child in children:
                checkpoint = _checkpoint(child / "pretrained_model", root)
                if checkpoint is None:
                    continue
                found[checkpoint["path"]] = checkpoint
                if child.name == "last":
                    last_path = checkpoint["path"]
            checkpoints = sorted(
                found.values(),
                key=lambda c: (c["step"] is not None, c["step"] or 0, c["modified_at"], c["path"]),
                reverse=True,
            )
            if checkpoints:
                latest = last_path or checkpoints[0]["path"]
                for checkpoint in checkpoints:
                    checkpoint["latest"] = checkpoint["path"] == latest
                runs.append({"name": str(folder.relative_to(root)), "checkpoints": checkpoints})
        if "config.json" in files and (checkpoint := _checkpoint(folder, root)):
            checkpoint["latest"] = True
            runs.append({"name": str(folder.relative_to(root)), "checkpoints": [checkpoint]})
            dirs[:] = []
    runs.sort(key=lambda r: max(c["modified_at"] for c in r["checkpoints"]), reverse=True)
    return {"root": str(root), "runs": runs}
