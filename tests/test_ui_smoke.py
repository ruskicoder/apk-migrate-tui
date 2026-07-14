"""End-to-end smoke test of the TUI using Textual's Pilot harness.

adb is fully mocked at the apk_migrate_tui.adb module level so this exercises the real
screen/widget/worker wiring without needing physical hardware.
"""
from __future__ import annotations

import unittest.mock as mock

import pytest

from apk_migrate_tui import adb
from apk_migrate_tui.app import ApkMigrateApp
from apk_migrate_tui.models import AppInfo


FAKE_DEVICES = [
    adb.DeviceEntry(serial="SOURCE123", state="device", model="Pixel_6"),
    adb.DeviceEntry(serial="TARGET456", state="device", model="Pixel_10"),
]

# package -> (installer, version_code, version_name)
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


def fake_list_packages(adb_path, serial, third_party_only=True):
    src = serial == "SOURCE123"
    data = SOURCE_PACKAGES if src else TARGET_PACKAGES
    apps = {pkg: AppInfo(package=pkg, installer=inst) for pkg, (inst, vc, vn) in data.items()}
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

    def fake_install_apks(adb_path, serial, local_apk_paths):
        return adb.AdbResult(ok=True, returncode=0, stdout="Success", stderr="")

    monkeypatch.setattr(adb, "install_apks", fake_install_apks)

    app = ApkMigrateApp()
    app.settings.archive_dir = str(tmp_path / "archive")
    app.settings.hide_identical = False  # so we can see + select the identical row too

    async with app.run_test(size=(120, 50)) as pilot:
        # DeviceSelectScreen should be up first
        await pilot.pause()
        screen = app.screen
        assert screen.__class__.__name__ == "DeviceSelectScreen"

        # mark first row SOURCE, second row TARGET
        await pilot.press("s")
        await pilot.press("down")
        await pilot.press("t")
        await pilot.press("c")
        await pilot.pause()

        # give the scan worker a moment to finish (it awaits asyncio.to_thread internally)
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

        # select the source-only fdroid app and archive it
        by_pkg["org.fdroid.fdroid"].selected = True
        list_screen.action_archive_selected()
        await pilot.pause()
        # confirm dialog should now be up
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
        lambda adb_path, serial, local_apk_paths: adb.AdbResult(ok=True, returncode=0, stdout="Success", stderr=""),
    )

    app = ApkMigrateApp()
    app.settings.archive_dir = str(tmp_path / "archive")

    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.press("down")
        await pilot.press("t")
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


@pytest.mark.asyncio
async def test_install_signature_mismatch_reports_explanation_and_does_not_mark_identical(tmp_path, monkeypatch):
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

    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.press("down")
        await pilot.press("t")
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

        # must NOT have been silently marked identical after a failed install
        assert target_entry.status.value == "version_diff"
        log_text = "\n".join(str(line) for line in list_screen.log_widget.lines)
        assert "Signature mismatch" in log_text or "UPDATE_INCOMPATIBLE" in log_text

