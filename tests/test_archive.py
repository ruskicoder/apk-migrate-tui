from pathlib import Path

from apk_migrate_tui.archive import ArchiveManager
from apk_migrate_tui.models import AppInfo


def test_commit_writes_manifest_and_apk(tmp_path):
    mgr = ArchiveManager(tmp_path)
    info = AppInfo(package="com.example", version_code=42, version_name="4.2.0")

    staged = mgr.staging_dir("com.example")
    staged.mkdir(parents=True)
    (staged / "base.apk").write_bytes(b"fake apk bytes")

    final_dir = mgr.commit("com.example", staged, info, ["base.apk"])

    assert final_dir == tmp_path / "com.example"
    assert (final_dir / "base.apk").read_bytes() == b"fake apk bytes"
    manifest = mgr.read_manifest("com.example")
    assert manifest["version_code"] == 42
    assert manifest["version_name"] == "4.2.0"
    assert manifest["apk_files"] == ["base.apk"]


def test_already_has_version_true_after_commit(tmp_path):
    mgr = ArchiveManager(tmp_path)
    info = AppInfo(package="com.example", version_code=42)
    staged = mgr.staging_dir("com.example")
    staged.mkdir(parents=True)
    (staged / "base.apk").write_bytes(b"x")
    mgr.commit("com.example", staged, info, ["base.apk"])

    assert mgr.already_has_version("com.example", 42) is True
    assert mgr.already_has_version("com.example", 43) is False
    assert mgr.already_has_version("com.example", None) is False


def test_commit_replaces_previous_version_atomically(tmp_path):
    mgr = ArchiveManager(tmp_path)

    # first commit: v1
    info1 = AppInfo(package="com.example", version_code=1)
    staged1 = mgr.staging_dir("com.example")
    staged1.mkdir(parents=True)
    (staged1 / "base.apk").write_bytes(b"v1")
    mgr.commit("com.example", staged1, info1, ["base.apk"])

    # second commit: v2 replaces v1
    info2 = AppInfo(package="com.example", version_code=2)
    staged2 = mgr.staging_dir("com.example")
    staged2.mkdir(parents=True)
    (staged2 / "base.apk").write_bytes(b"v2")
    final_dir = mgr.commit("com.example", staged2, info2, ["base.apk"])

    assert (final_dir / "base.apk").read_bytes() == b"v2"
    assert mgr.archived_version_code("com.example") == 2
    # no leftover staging/backup directories
    leftovers = [p for p in tmp_path.iterdir() if p.name != "com.example"]
    assert leftovers == []


def test_read_manifest_returns_none_when_missing(tmp_path):
    mgr = ArchiveManager(tmp_path)
    assert mgr.read_manifest("com.doesnotexist") is None


def test_read_manifest_returns_none_on_corrupt_json(tmp_path):
    mgr = ArchiveManager(tmp_path)
    pkg_dir = tmp_path / "com.corrupt"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "manifest.json").write_text("{not valid json")
    assert mgr.read_manifest("com.corrupt") is None
