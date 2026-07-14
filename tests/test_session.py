"""Unit tests for session.py — pure logic, no I/O."""

from __future__ import annotations

import json
import time

import pytest

from apk_migrate_tui.session import (
    DeviceRecord,
    ExecutionState,
    PackageExecution,
    Session,
    SessionManager,
    TERMINAL_STATES,
    appinfo_from_dict,
    appinfo_to_dict,
)
from apk_migrate_tui.models import AppInfo


# ---------------------------------------------------------------------------
# AppInfo serialization round-trip
# ---------------------------------------------------------------------------

def test_appinfo_roundtrip():
    info = AppInfo(
        package="com.example.foo",
        version_code=42,
        version_name="1.2.3",
        label="Foo App",
        installer="org.fdroid.fdroid",
        apk_remote_paths=["/data/app/foo/base.apk"],
    )
    d = appinfo_to_dict(info)
    info2 = appinfo_from_dict(d)
    assert info2.package == info.package
    assert info2.version_code == info.version_code
    assert info2.version_name == info.version_name
    assert info2.label == info.label
    assert info2.installer == info.installer
    assert info2.apk_remote_paths == info.apk_remote_paths


def test_appinfo_roundtrip_minimal():
    info = AppInfo(package="com.min")
    d = appinfo_to_dict(info)
    info2 = appinfo_from_dict(d)
    assert info2.package == "com.min"
    assert info2.version_code is None
    assert info2.apk_remote_paths == []


# ---------------------------------------------------------------------------
# DeviceRecord serialization
# ---------------------------------------------------------------------------

def test_device_record_roundtrip():
    apps = {
        "com.example.a": appinfo_to_dict(AppInfo(package="com.example.a", version_code=1)),
    }
    record = DeviceRecord(serial="ABC123", model="Pixel_6", apps=apps, scanned_at="2025-01-01T12:00:00")
    d = record.to_dict()
    record2 = DeviceRecord.from_dict(d)
    assert record2.serial == "ABC123"
    assert record2.model == "Pixel_6"
    assert "com.example.a" in record2.apps
    assert record2.scanned_at == "2025-01-01T12:00:00"


def test_device_record_get_app_infos():
    apps = {
        "com.test.app": appinfo_to_dict(
            AppInfo(package="com.test.app", version_code=7, version_name="1.7")
        )
    }
    record = DeviceRecord(serial="X", model=None, apps=apps)
    infos = record.get_app_infos()
    assert "com.test.app" in infos
    assert infos["com.test.app"].version_code == 7


# ---------------------------------------------------------------------------
# PackageExecution serialization
# ---------------------------------------------------------------------------

def test_package_execution_roundtrip():
    pe = PackageExecution(
        package="org.fdroid.fdroid",
        action="archive",
        state=ExecutionState.ARCHIVED,
    )
    d = pe.to_dict()
    pe2 = PackageExecution.from_dict(d)
    assert pe2.package == pe.package
    assert pe2.action == pe.action
    assert pe2.state is ExecutionState.ARCHIVED


@pytest.mark.parametrize("state", list(ExecutionState))
def test_all_execution_states_roundtrip(state):
    pe = PackageExecution(package="pkg", action="install", state=state)
    d = pe.to_dict()
    pe2 = PackageExecution.from_dict(d)
    assert pe2.state is state


# ---------------------------------------------------------------------------
# Session serialization
# ---------------------------------------------------------------------------

def _make_session() -> Session:
    src = DeviceRecord(serial="SRC", model="Pixel_6", apps={
        "com.a": appinfo_to_dict(AppInfo(package="com.a", version_code=1)),
    }, scanned_at="2025-01-01T00:00:00")
    tgt = DeviceRecord(serial="TGT", model="Pixel_10", apps={
        "com.b": appinfo_to_dict(AppInfo(package="com.b", version_code=2)),
    }, scanned_at="2025-01-01T00:00:00")
    return Session(
        session_id="abc123",
        created_at="2025-01-01T00:00:00",
        source=src,
        target=tgt,
        executions={
            "com.a": PackageExecution(package="com.a", action="archive", state=ExecutionState.ARCHIVED),
        },
    )


def test_session_roundtrip():
    sess = _make_session()
    d = sess.to_dict()
    sess2 = Session.from_dict(d)
    assert sess2.session_id == "abc123"
    assert sess2.source is not None
    assert sess2.source.serial == "SRC"
    assert sess2.target is not None
    assert "com.a" in sess2.executions
    assert sess2.executions["com.a"].state is ExecutionState.ARCHIVED


def test_session_is_ready_when_both_scanned():
    sess = _make_session()
    assert sess.is_ready is True


def test_session_not_ready_when_source_missing():
    sess = _make_session()
    sess.source = None
    assert sess.is_ready is False


def test_session_not_ready_when_source_empty_apps():
    sess = _make_session()
    sess.source.apps = {}
    assert sess.is_ready is False


def test_session_display_name():
    sess = _make_session()
    assert "Pixel_6" in sess.display_name
    assert "Pixel_10" in sess.display_name


def test_session_check_completion():
    sess = _make_session()
    # Not complete — source app not in executions yet
    sess.executions = {
        "com.a": PackageExecution(package="com.a", action="archive", state=ExecutionState.ARCHIVED),
        "com.b": PackageExecution(package="com.b", action="install", state=ExecutionState.PENDING),
    }
    sess.check_completion()
    assert sess.completed is False

    # All in terminal states
    sess.executions["com.b"].state = ExecutionState.INSTALLED
    sess.check_completion()
    assert sess.completed is True


def test_session_done_and_total_counts():
    sess = _make_session()
    sess.executions = {
        "com.a": PackageExecution(package="com.a", action="archive", state=ExecutionState.ARCHIVED),
        "com.b": PackageExecution(package="com.b", action="install", state=ExecutionState.PENDING),
        "com.c": PackageExecution(package="com.c", action="install", state=ExecutionState.INSTALL_FAILED),
    }
    assert sess.total_count == 3
    assert sess.done_count == 2  # ARCHIVED + INSTALL_FAILED are terminal


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

def test_session_manager_new_and_load(tmp_path):
    mgr = SessionManager(tmp_path / "sessions")
    sess = mgr.new_session()

    assert sess.session_id != ""
    assert (tmp_path / "sessions" / f"{sess.session_id}.json").exists()

    loaded = mgr.load(sess.session_id)
    assert loaded is not None
    assert loaded.session_id == sess.session_id


def test_session_manager_save_and_load_round_trip(tmp_path):
    mgr = SessionManager(tmp_path / "sessions")
    sess = mgr.new_session()

    src = DeviceRecord(
        serial="S1", model="Pixel_6",
        apps={"com.x": appinfo_to_dict(AppInfo(package="com.x", version_code=5))},
        scanned_at="2025-01-01T10:00:00",
    )
    sess.source = src
    mgr.save(sess)

    loaded = mgr.load(sess.session_id)
    assert loaded is not None
    assert loaded.source is not None
    assert loaded.source.serial == "S1"
    assert "com.x" in loaded.source.apps


def test_session_manager_load_nonexistent(tmp_path):
    mgr = SessionManager(tmp_path / "sessions")
    result = mgr.load("does-not-exist")
    assert result is None


def test_session_manager_load_corrupt_json(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    bad_file = sessions_dir / "bad.json"
    bad_file.write_text("{{{not valid json")
    mgr = SessionManager(sessions_dir)
    result = mgr.load("bad")
    assert result is None


def test_session_manager_list_incomplete(tmp_path):
    mgr = SessionManager(tmp_path / "sessions")

    s1 = mgr.new_session()
    s2 = mgr.new_session()
    s2.completed = True
    mgr.save(s2)

    incomplete = mgr.list_incomplete()
    ids = [s.session_id for s in incomplete]
    assert s1.session_id in ids
    assert s2.session_id not in ids


def test_session_manager_delete(tmp_path):
    mgr = SessionManager(tmp_path / "sessions")
    sess = mgr.new_session()
    path = tmp_path / "sessions" / f"{sess.session_id}.json"
    assert path.exists()
    mgr.delete(sess.session_id)
    assert not path.exists()


def test_session_manager_delete_nonexistent(tmp_path):
    # Should not raise
    mgr = SessionManager(tmp_path / "sessions")
    mgr.delete("ghost-id")


# ---------------------------------------------------------------------------
# Crash recovery: in-flight state reversion
# ---------------------------------------------------------------------------

def test_crash_recovery_reverts_archiving_to_pending(tmp_path):
    mgr = SessionManager(tmp_path / "sessions")
    sess = mgr.new_session()

    # Simulate a crash mid-archive: ARCHIVING was written to disk
    sess.executions["com.example.crash"] = PackageExecution(
        package="com.example.crash",
        action="archive",
        state=ExecutionState.ARCHIVING,
    )
    mgr.save(sess)

    loaded = mgr.load(sess.session_id)
    assert loaded is not None
    assert loaded.executions["com.example.crash"].state is ExecutionState.PENDING


def test_crash_recovery_reverts_installing_to_pending(tmp_path):
    mgr = SessionManager(tmp_path / "sessions")
    sess = mgr.new_session()

    sess.executions["com.example.crash"] = PackageExecution(
        package="com.example.crash",
        action="install",
        state=ExecutionState.INSTALLING,
    )
    mgr.save(sess)

    loaded = mgr.load(sess.session_id)
    assert loaded is not None
    assert loaded.executions["com.example.crash"].state is ExecutionState.PENDING


def test_non_in_flight_states_not_reverted(tmp_path):
    mgr = SessionManager(tmp_path / "sessions")
    sess = mgr.new_session()

    for state in TERMINAL_STATES:
        pkg = f"com.terminal.{state.value}"
        sess.executions[pkg] = PackageExecution(
            package=pkg, action="archive", state=state
        )
    mgr.save(sess)

    loaded = mgr.load(sess.session_id)
    assert loaded is not None
    for state in TERMINAL_STATES:
        pkg = f"com.terminal.{state.value}"
        assert loaded.executions[pkg].state is state


# ---------------------------------------------------------------------------
# Atomic write safety: temp file must not be left on disk after save
# ---------------------------------------------------------------------------

def test_atomic_write_no_tmp_file_left(tmp_path):
    mgr = SessionManager(tmp_path / "sessions")
    sess = mgr.new_session()
    mgr.save(sess)
    tmp_files = list((tmp_path / "sessions").glob("*.tmp"))
    assert tmp_files == [], f"Leftover tmp files: {tmp_files}"


# ---------------------------------------------------------------------------
# Session file structure (JSON sanity)
# ---------------------------------------------------------------------------

def test_session_json_is_valid_and_readable(tmp_path):
    mgr = SessionManager(tmp_path / "sessions")
    sess = mgr.new_session()
    path = tmp_path / "sessions" / f"{sess.session_id}.json"
    data = json.loads(path.read_text())
    assert data["session_id"] == sess.session_id
    assert "created_at" in data
    assert "executions" in data
    assert "completed" in data
