"""End-to-end smoke test of the TUI using Textual's Pilot harness.

adb is fully mocked at the apk_migrate_tui.adb module level so this exercises the real
screen/widget/worker wiring without needing physical hardware.

The new architecture routes through a Session object and SessionManager.  The smoke
tests mock both the session manager (to avoid disk I/O) and all adb calls to test
the screen wiring in isolation.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from apk_migrate_tui import adb
from apk_migrate_tui.app import ApkMigrateApp
from apk_migrate_tui.models import AppInfo
from apk_migrate_tui.session import (
    DeviceRecord,
    Session,
    SessionManager,
    appinfo_to_dict,
)
from apk_migrate_tui.settings import Settings


FAKE_DEVICES = [
    adb.DeviceEntry(serial="SOURCE123", state="device", model="Pixel_6"),
    adb.DeviceEntry(serial="TARGET456", state="device", model="Pixel_10"),
]

# package → (installer, version_code, version_name)
SOURCE_PACKAGES = {
    "org.fdroid.fdroid": (None, 1015050, "1.15.0"),
    "com.example.sameversion": (None, 5, "1.0"),
    "com.example.newerlocal": (None, 9, "2.0"),
    "com.example.onlysource": (None, 3, "0.3"),
}
TARGET_PACKAGES = {
    "com.example.sameversion": (None, 5, "1.0"),
    "com.example.newerlocal": (None, 7, "1.5"),
    "com.example.onlytarget": (None, 1, "1.0"),
}


def _make_app_infos(packages: dict) -> dict[str, AppInfo]:
    return {
        pkg: AppInfo(package=pkg, installer=inst, version_code=vc, version_name=vn)
        for pkg, (inst, vc, vn) in packages.items()
    }


def _make_ready_session(sessions_dir: Path) -> Session:
    """Create a pre-scanned session for use in tests that go straight to AppListScreen."""
    src_apps = {
        pkg: appinfo_to_dict(info)
        for pkg, info in _make_app_infos(SOURCE_PACKAGES).items()
    }
    tgt_apps = {
        pkg: appinfo_to_dict(info)
        for pkg, info in _make_app_infos(TARGET_PACKAGES).items()
    }
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    sess = Session(
        session_id="smoke-test-session",
        created_at=now,
        source=DeviceRecord(serial="SOURCE123", model="Pixel_6", apps=src_apps, scanned_at=now),
        target=DeviceRecord(serial="TARGET456", model="Pixel_10", apps=tgt_apps, scanned_at=now),
    )
    mgr = SessionManager(sessions_dir)
    mgr.save(sess)
    return sess


def fake_list_packages(adb_path, serial, third_party_only=True):
    src = serial == "SOURCE123"
    data = SOURCE_PACKAGES if src else TARGET_PACKAGES
    apps = {pkg: AppInfo(package=pkg, installer=inst, version_code=vc, version_name=vn)
            for pkg, (inst, vc, vn) in data.items()}
    result = adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")
    return apps, result


def fake_get_package_version(adb_path, serial, package):
    src = serial == "SOURCE123"
    data = SOURCE_PACKAGES if src else TARGET_PACKAGES
    inst, vc, vn = data[package]
    result = adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")
    return vc, vn, result


def fake_get_apk_remote_paths(adb_path, serial, package):
    result = adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")
    return [f"/data/app/{package}-1/base.apk"], result


# ---------------------------------------------------------------------------
# Full flow: device select → scan → app list → archive
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_flow_scan_select_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(adb, "find_adb", lambda explicit_path=None: "/fake/adb")
    monkeypatch.setattr(adb, "list_devices", lambda adb_path: FAKE_DEVICES)
    monkeypatch.setattr(adb, "list_packages", fake_list_packages)
    monkeypatch.setattr(adb, "get_package_version", fake_get_package_version)
    monkeypatch.setattr(adb, "get_apk_remote_paths", fake_get_apk_remote_paths)

    def fake_pull_file(adb_path, serial, remote_path, local_dest):
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        local_dest.write_bytes(b"fake apk contents")
        return adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adb, "pull_file", fake_pull_file)
    monkeypatch.setattr(
        adb, "install_apks",
        lambda adb_path, serial, local_apk_paths: adb.AdbResult(
            ok=True, returncode=0, stdout="Success", stderr=""
        ),
    )

    app = ApkMigrateApp()
    app.settings.archive_dir = str(tmp_path / "archive")
    app.settings.sessions_dir = str(tmp_path / "sessions")
    app.settings.hide_identical = False  # see all apps including identical

    async with app.run_test(size=(120, 50)) as pilot:
        # Land on ModeSelectScreen first, select option 1 (Migrate)
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        screen = app.screen
        assert screen.__class__.__name__ == "DeviceSelectScreen"

        # mark SOURCE (row 0), TARGET (row 1), continue
        await pilot.press("s")
        # Wait for SOURCE scan to complete
        for _ in range(40):
            await pilot.pause()
            if (
                screen.__class__.__name__ == "DeviceSelectScreen"
                and not getattr(screen, "_scanning", True)
                and screen.session.source and screen.session.source.apps
            ):
                break
        await pilot.press("down")
        await pilot.press("t")
        # Wait for TARGET scan to complete
        for _ in range(40):
            await pilot.pause()
            if (
                screen.__class__.__name__ == "DeviceSelectScreen"
                and not getattr(screen, "_scanning", True)
                and screen.session.target and screen.session.target.apps
            ):
                break
        await pilot.press("c")
        await pilot.pause()

        # Now AppListScreen should appear
        for _ in range(20):
            await pilot.pause()
            if app.screen.__class__.__name__ == "AppListScreen" and not getattr(app.screen, "busy", True):
                break

        list_screen = app.screen
        assert list_screen.__class__.__name__ == "AppListScreen"
        assert len(list_screen.entries) == 5  # union of both package sets

        by_pkg = {e.package: e for e in list_screen.entries}
        assert by_pkg["com.example.sameversion"].status.value == "identical"
        assert by_pkg["com.example.newerlocal"].status.value == "version_diff"
        assert by_pkg["com.example.onlysource"].status.value == "source_only"
        assert by_pkg["com.example.onlytarget"].status.value == "target_only"
        assert by_pkg["org.fdroid.fdroid"].status.value == "source_only"

        # Select the source-only fdroid app and archive it
        by_pkg["org.fdroid.fdroid"].selected = True
        list_screen.action_archive_selected()
        await pilot.pause()
        # Confirm dialog should now be up
        for _ in range(10):
            await pilot.pause()
            if app.screen.__class__.__name__ == "ConfirmScreen":
                break
        assert app.screen.__class__.__name__ == "ConfirmScreen"
        await pilot.click("#confirm")
        for _ in range(20):
            await pilot.pause()
            if not getattr(list_screen, "busy", True):
                break

        assert by_pkg["org.fdroid.fdroid"].archived is True
        archived_manifest = list_screen.archive_mgr.read_manifest("org.fdroid.fdroid")
        assert archived_manifest is not None
        assert archived_manifest["version_code"] == 1015050

        await pilot.press("q")
        await pilot.pause()


# ---------------------------------------------------------------------------
# Install flow updates status to identical
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_install_flow_updates_status_to_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(adb, "find_adb", lambda explicit_path=None: "/fake/adb")
    monkeypatch.setattr(adb, "list_devices", lambda adb_path: FAKE_DEVICES)
    monkeypatch.setattr(adb, "list_packages", fake_list_packages)
    monkeypatch.setattr(adb, "get_package_version", fake_get_package_version)
    monkeypatch.setattr(adb, "get_apk_remote_paths", fake_get_apk_remote_paths)

    def fake_pull_file(adb_path, serial, remote_path, local_dest):
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        local_dest.write_bytes(b"fake apk contents")
        return adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adb, "pull_file", fake_pull_file)
    monkeypatch.setattr(
        adb, "install_apks",
        lambda adb_path, serial, local_apk_paths: adb.AdbResult(
            ok=True, returncode=0, stdout="Success", stderr=""
        ),
    )

    app = ApkMigrateApp()
    app.settings.archive_dir = str(tmp_path / "archive")
    app.settings.sessions_dir = str(tmp_path / "sessions")

    async with app.run_test(size=(120, 50)) as pilot:
        # Land on ModeSelectScreen first, select option 1 (Migrate)
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("s")
        for _ in range(40):
            await pilot.pause()
            screen = app.screen
            if (screen.__class__.__name__ == "DeviceSelectScreen"
                    and not getattr(screen, "_scanning", True)
                    and screen.session.source and screen.session.source.apps):
                break
        await pilot.press("down")
        await pilot.press("t")
        for _ in range(40):
            await pilot.pause()
            screen = app.screen
            if (screen.__class__.__name__ == "DeviceSelectScreen"
                    and not getattr(screen, "_scanning", True)
                    and screen.session.target and screen.session.target.apps):
                break
        await pilot.press("c")
        for _ in range(20):
            await pilot.pause()
            if app.screen.__class__.__name__ == "AppListScreen" and not getattr(app.screen, "busy", True):
                break

        list_screen = app.screen
        by_pkg = {e.package: e for e in list_screen.entries}
        target_entry = by_pkg["com.example.newerlocal"]
        assert target_entry.status.value == "version_diff"
        target_entry.selected = True

        list_screen.action_install_selected()
        for _ in range(10):
            await pilot.pause()
            if app.screen.__class__.__name__ == "ConfirmScreen":
                break
        await pilot.click("#confirm")
        for _ in range(20):
            await pilot.pause()
            if not getattr(list_screen, "busy", True):
                break

        assert target_entry.status.value == "identical"
        assert target_entry.target.version_code == 9  # now matches source
        assert target_entry.archived is True


# ---------------------------------------------------------------------------
# Signature mismatch failure — must NOT mark entry as identical
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_install_signature_mismatch_reports_explanation_and_does_not_mark_identical(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(adb, "find_adb", lambda explicit_path=None: "/fake/adb")
    monkeypatch.setattr(adb, "list_devices", lambda adb_path: FAKE_DEVICES)
    monkeypatch.setattr(adb, "list_packages", fake_list_packages)
    monkeypatch.setattr(adb, "get_package_version", fake_get_package_version)
    monkeypatch.setattr(adb, "get_apk_remote_paths", fake_get_apk_remote_paths)

    def fake_pull_file(adb_path, serial, remote_path, local_dest):
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        local_dest.write_bytes(b"fake apk contents")
        return adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adb, "pull_file", fake_pull_file)
    monkeypatch.setattr(
        adb, "install_apks",
        lambda adb_path, serial, local_apk_paths: adb.AdbResult(
            ok=False, returncode=1, stdout="",
            stderr="Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]",
        ),
    )

    app = ApkMigrateApp()
    app.settings.archive_dir = str(tmp_path / "archive")
    app.settings.sessions_dir = str(tmp_path / "sessions")

    async with app.run_test(size=(120, 50)) as pilot:
        # Land on ModeSelectScreen first, select option 1 (Migrate)
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("s")
        for _ in range(40):
            await pilot.pause()
            screen = app.screen
            if (screen.__class__.__name__ == "DeviceSelectScreen"
                    and not getattr(screen, "_scanning", True)
                    and screen.session.source and screen.session.source.apps):
                break
        await pilot.press("down")
        await pilot.press("t")
        for _ in range(40):
            await pilot.pause()
            screen = app.screen
            if (screen.__class__.__name__ == "DeviceSelectScreen"
                    and not getattr(screen, "_scanning", True)
                    and screen.session.target and screen.session.target.apps):
                break
        await pilot.press("c")
        for _ in range(20):
            await pilot.pause()
            if app.screen.__class__.__name__ == "AppListScreen" and not getattr(app.screen, "busy", True):
                break

        list_screen = app.screen
        by_pkg = {e.package: e for e in list_screen.entries}
        target_entry = by_pkg["com.example.newerlocal"]
        target_entry.selected = True

        list_screen.action_install_selected()
        for _ in range(10):
            await pilot.pause()
            if app.screen.__class__.__name__ == "ConfirmScreen":
                break
        await pilot.click("#confirm")
        for _ in range(20):
            await pilot.pause()
            if not getattr(list_screen, "busy", True):
                break

        # Must NOT be silently marked identical after a failed install
        assert target_entry.status.value == "version_diff"
        log_text = "\n".join(str(line) for line in list_screen.log_widget.lines)
        assert "Signature mismatch" in log_text or "UPDATE_INCOMPATIBLE" in log_text


# ---------------------------------------------------------------------------
# Session resume: pre-populated session goes straight to AppListScreen
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_resume_goes_to_applist(tmp_path, monkeypatch):
    """When a complete session is on disk, the resume picker appears, and on 'Resume'
    the app goes straight to AppListScreen (skipping device rescan)."""
    sessions_dir = tmp_path / "sessions"
    sess = _make_ready_session(sessions_dir)

    monkeypatch.setattr(adb, "find_adb", lambda explicit_path=None: "/fake/adb")
    monkeypatch.setattr(adb, "list_devices", lambda adb_path: FAKE_DEVICES)
    monkeypatch.setattr(adb, "list_packages", fake_list_packages)
    monkeypatch.setattr(adb, "get_package_version", fake_get_package_version)
    monkeypatch.setattr(adb, "get_apk_remote_paths", fake_get_apk_remote_paths)

    def fake_pull_file(adb_path, serial, remote_path, local_dest):
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        local_dest.write_bytes(b"fake apk")
        return adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adb, "pull_file", fake_pull_file)
    monkeypatch.setattr(
        adb, "install_apks",
        lambda adb_path, serial, local_apk_paths: adb.AdbResult(
            ok=True, returncode=0, stdout="Success", stderr=""
        ),
    )

    app = ApkMigrateApp()
    app.settings.archive_dir = str(tmp_path / "archive")
    app.settings.sessions_dir = str(sessions_dir)

    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()

        # Session resume picker should be shown first
        assert app.screen.__class__.__name__ == "SessionResumeScreen"

        # Click Resume
        await pilot.click("#btn_resume")
        await pilot.pause()

        # Should go straight to AppListScreen (session already has scan data)
        for _ in range(20):
            await pilot.pause()
            if app.screen.__class__.__name__ == "AppListScreen" and not getattr(app.screen, "busy", True):
                break

        assert app.screen.__class__.__name__ == "AppListScreen"
        assert len(app.screen.entries) == 5

        await pilot.press("q")
        await pilot.pause()
