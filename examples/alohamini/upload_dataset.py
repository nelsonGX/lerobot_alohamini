#!/usr/bin/env python3
"""Send the shared dataset off this machine: to a private Hugging Face Hub repo, or by rsync to the GPU server.

Run by the panel's Upload button, or by hand:
  uv run python examples/alohamini/upload_dataset.py --method hub [--dry-run]
  uv run python examples/alohamini/upload_dataset.py --method rsync [--dry-run]

Destinations come from the `upload:` section of collection.yaml. Nothing is sent when the dataset fails validation.
The Hub repo is created private, and an existing PUBLIC repo is refused rather than written to.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

from collection import ConfigError, DatasetProblemError, load_config, validate_dataset

IGNORE = ["images/", ".*lock", "*.tmp"]
RSYNC_DEST_RE = re.compile(
    r"^[A-Za-z0-9_.@:/~+-]+$"
)  # no spaces/shell characters: it is passed as one argument


def say(msg: str) -> None:
    print(msg, flush=True)


def upload_hub(root: Path, repo_id: str, dry_run: bool) -> None:
    from huggingface_hub import HfApi
    from huggingface_hub.errors import RepositoryNotFoundError

    from lerobot.datasets.dataset_metadata import CODEBASE_VERSION

    api = HfApi()
    try:
        who = api.whoami()["name"]
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "Not logged in to Hugging Face. Run `uv run hf auth login` (or set HF_TOKEN) on this machine."
        ) from e
    say(f"Logged in to Hugging Face as {who}.")
    exists = True
    try:
        info = api.repo_info(repo_id, repo_type="dataset")
    except RepositoryNotFoundError:
        exists = False
    if exists and not info.private:
        raise RuntimeError(
            f"{repo_id} already exists and is PUBLIC. Make it private on huggingface.co, then try again."
        )
    if dry_run:
        say(
            f"[dry run] would {'update' if exists else 'create'} the private dataset repo {repo_id} from {root}"
        )
        return
    if not exists:
        say(f"Creating private dataset repo {repo_id}…")
        api.create_repo(repo_id, repo_type="dataset", private=True)
    say(f"Uploading {root} to {repo_id} (this can take a while for videos)…")
    api.upload_folder(folder_path=root, repo_id=repo_id, repo_type="dataset", ignore_patterns=IGNORE)
    with contextlib.suppress(Exception):  # no tag yet on a fresh repo
        api.delete_tag(repo_id, tag=CODEBASE_VERSION, repo_type="dataset")
    api.create_tag(repo_id, tag=CODEBASE_VERSION, repo_type="dataset")
    if not api.repo_info(repo_id, repo_type="dataset").private:
        raise RuntimeError(
            f"{repo_id} is not private after the upload. Check its visibility on huggingface.co NOW."
        )
    say(f"Done. https://huggingface.co/datasets/{repo_id} (private)")


def upload_rsync(root: Path, dest: str, dry_run: bool) -> None:
    if shutil.which("rsync") is None:
        raise RuntimeError("rsync is not installed on this machine.")
    if not RSYNC_DEST_RE.match(dest):
        raise RuntimeError(
            f"upload.rsync_dest {dest!r} has characters that are not allowed (use user@host:/path)."
        )
    cmd = ["rsync", "-a", "--partial", "--info=progress2,stats1", "--human-readable",
           # Never wait for a password: fail with a message instead of hanging the panel.
           "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10"]  # fmt: skip
    for pattern in IGNORE:
        cmd += ["--exclude", pattern]
    if dry_run:
        cmd.append("--dry-run")
    cmd += [f"{root}/", dest.rstrip("/") + "/"]
    say("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, stdout=sys.stdout, stderr=subprocess.STDOUT, text=True, check=False)
    if proc.returncode != 0:
        hint = " Set up SSH key login to the server first (ssh-copy-id)." if proc.returncode == 255 else ""
        raise RuntimeError(f"rsync failed with code {proc.returncode}.{hint}")
    say("Done." if not dry_run else "[dry run] nothing was copied.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--method", choices=["hub", "rsync"], required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--config", default=None)
    p.add_argument("--dataset-root", default=None)
    p.add_argument("--dest", default=None, help="Override the destination from collection.yaml")
    args = p.parse_args()
    try:
        cfg = load_config(args.config)
        if args.dataset_root:
            root = Path(args.dataset_root)
        else:
            from lerobot.utils.constants import HF_LEROBOT_HOME

            root = HF_LEROBOT_HOME / cfg.dataset
        if not (root / "meta" / "info.json").exists():
            raise DatasetProblemError(f"There is no dataset at {root} yet - record something first.")
        say(f"Checking {root} …")
        if problems := validate_dataset(root):
            raise DatasetProblemError("The dataset is damaged, not uploading: " + "; ".join(problems))
        target = args.dest or (cfg.hub_repo_id if args.method == "hub" else cfg.rsync_dest)
        if not target:
            raise ConfigError(
                f"No destination for {args.method}: fill in upload.{'hub_repo_id' if args.method == 'hub' else 'rsync_dest'} in collection.yaml"
            )
        if args.method == "hub":
            upload_hub(root, target, args.dry_run)
        else:
            upload_rsync(root, target, args.dry_run)
    except (ConfigError, DatasetProblemError, RuntimeError) as e:
        say(f"ERROR: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
