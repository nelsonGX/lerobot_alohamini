"""The Upload button's script: rsync really runs against a local folder; the Hub path is exercised with a fake API."""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pyarrow")
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "examples" / "alohamini"))

import upload_dataset as up  # noqa: E402


@pytest.fixture(scope="module")
def tiny_dataset(tmp_path_factory) -> Path:
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    root = tmp_path_factory.mktemp("ds") / "t" / "tiny"
    feats = {"observation.state": {"dtype": "float32", "shape": (2,), "names": ["a", "b"]},
             "action": {"dtype": "float32", "shape": (2,), "names": ["a", "b"]}}  # fmt: skip
    ds = LeRobotDataset.create("t/tiny", fps=10, features=feats, root=root, use_videos=False)
    for _ in range(2):
        for _ in range(5):
            ds.add_frame(
                {
                    "observation.state": np.zeros(2, np.float32),
                    "action": np.ones(2, np.float32),
                    "task": "pick up the red cube",
                }
            )
        ds.save_episode()
    ds.finalize()
    (root / "images").mkdir()
    (root / "images" / "frame.png").write_bytes(b"x")  # temp frames must never be uploaded
    return root


def run(*args: str, env=None):
    return subprocess.run([sys.executable, str(REPO / "examples/alohamini/upload_dataset.py"), *args],
                          capture_output=True, text=True, cwd=REPO, env=env)  # fmt: skip


@pytest.mark.skipif(not __import__("shutil").which("rsync"), reason="rsync not installed")
def test_rsync_copies_the_dataset_but_not_temp_files(tiny_dataset, tmp_path):
    dest = tmp_path / "server"
    dry = run("--method", "rsync", "--dry-run", "--dataset-root", str(tiny_dataset), "--dest", str(dest))
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert not (dest / "meta").exists()  # a dry run copies nothing
    real = run("--method", "rsync", "--dataset-root", str(tiny_dataset), "--dest", str(dest))
    assert real.returncode == 0, real.stdout + real.stderr
    assert (dest / "meta" / "info.json").exists() and list((dest / "data").rglob("*.parquet"))
    assert not (dest / "images").exists()


def test_a_damaged_dataset_is_not_sent(tiny_dataset, tmp_path):
    import shutil

    broken = tmp_path / "t" / "tiny"
    shutil.copytree(tiny_dataset, broken)
    f = next(broken.glob("data/chunk-*/file-*.parquet"))
    f.write_bytes(f.read_bytes()[:40])
    out = run("--method", "rsync", "--dataset-root", str(broken), "--dest", str(tmp_path / "server"))
    assert out.returncode == 1 and "not uploading" in out.stdout
    assert not (tmp_path / "server").exists()


def test_missing_destination_and_missing_login_give_clear_errors(tiny_dataset, tmp_path):
    out = run("--method", "hub", "--dataset-root", str(tiny_dataset))
    assert out.returncode == 1 and "hub_repo_id" in out.stdout  # the default config has no destination
    import os

    env = {**os.environ, "HF_HOME": str(tmp_path), "HF_HUB_OFFLINE": "1"}
    env.pop("HF_TOKEN", None)
    out = run("--method", "hub", "--dataset-root", str(tiny_dataset), "--dest", "o/n", env=env)
    assert out.returncode == 1 and "Not logged in" in out.stdout


class FakeApi:
    """Records what the uploader asks the Hub to do."""

    calls: list = []
    existing: dict = {}  # repo_id -> private?

    def whoami(self):
        return {"name": "tester"}

    def repo_info(self, repo_id, repo_type):
        from huggingface_hub.errors import RepositoryNotFoundError

        if repo_id not in self.existing:
            raise RepositoryNotFoundError.__new__(
                RepositoryNotFoundError
            )  # its __init__ wants an HTTP response
        return type("I", (), {"private": self.existing[repo_id]})()

    def create_repo(self, repo_id, repo_type, private):
        self.calls.append(("create_repo", repo_id, private))
        self.existing[repo_id] = private

    def upload_folder(self, **kw):
        self.calls.append(("upload_folder", kw["repo_id"], tuple(kw["ignore_patterns"])))

    def delete_tag(self, *a, **k):
        pass

    def create_tag(self, repo_id, tag, repo_type):
        self.calls.append(("create_tag", repo_id, tag))


@pytest.fixture
def fake_hub(monkeypatch):
    import huggingface_hub

    FakeApi.calls, FakeApi.existing = [], {}
    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    return FakeApi


def test_hub_upload_creates_a_private_repo(fake_hub, tiny_dataset):
    up.upload_hub(tiny_dataset, "org/ds", dry_run=False)
    assert ("create_repo", "org/ds", True) in fake_hub.calls
    assert any(c[0] == "upload_folder" and "images/" in c[2] for c in fake_hub.calls)
    assert any(c[0] == "create_tag" for c in fake_hub.calls)


def test_hub_upload_refuses_a_public_repo_and_dry_run_sends_nothing(fake_hub, tiny_dataset):
    fake_hub.existing["org/public"] = False
    with pytest.raises(RuntimeError, match="PUBLIC"):
        up.upload_hub(tiny_dataset, "org/public", dry_run=False)
    up.upload_hub(tiny_dataset, "org/new", dry_run=True)
    assert fake_hub.calls == []
