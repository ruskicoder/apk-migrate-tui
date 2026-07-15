"""End-to-end integration and unit tests for the Single Device Management Mode."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from textual.widgets import Button, Input, DataTable

from apk_migrate_tui import adb
from apk_migrate_tui.app import ApkMigrateApp
from apk_migrate_tui.models import AppInfo
from apk_migrate_tui.screens.uninstall_confirm import UninstallConfirmScreen
from apk_migrate_tui.screens.single_device_app_list import SingleAppEntry


FAKE_DEVICES = [
    adb.DeviceEntry(serial="DEVICE123", state="device", model="Pixel_6"),
]

# Package name -> (installer, version_code, version_name)
DEVICE_PACKAGES = {
    "org.fdroid.fdroid": (None, 1015050, "1.15.0"),
    "com.example.ondevice": (None, 5, "1.0"),
    "com.example.outdated": (None, 2, "0.5"),   # Archive version is newer
    "com.example.newer": (None, 10, "2.0"),     # Device version is newer
}

ARCHIVE_PACKAGES = {
    "com.example.ondevice": {"version_code": 5, "version_name": "1.0", "installer": None, "apk_files": ["base.apk"]},
    "com.example.outdated": {"version_code": 7, "version_name": "0.9", "installer": None, "apk_files": ["base.apk"]},
    "com.example.newer": {"version_code": 3, "version_name": "0.1", "installer": None, "apk_files": ["base.apk"]},
    "com.example.onlyarchive": {"version_code": 1, "version_name": "1.0", "installer": None, "apk_files": ["base.apk"]},
}


def _fake_list_packages(adb_path, serial, third_party_only=True):
    apps = {
        pkg: AppInfo(package=pkg, installer=inst, version_code=vc, version_name=vn)
        for pkg, (inst, vc, vn) in DEVICE_PACKAGES.items()
    }
    return apps, adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")


def _fake_get_package_version(adb_path, serial, package):
    inst, vc, vn = DEVICE_PACKAGES[package]
    return vc, vn, adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")


def _fake_get_apk_remote_paths(adb_path, serial, package):
    return [f"/data/app/{package}-1/base.apk"], adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")


def _patch_common(monkeypatch):
    monkeypatch.setattr(adb, "find_adb", lambda explicit_path=None: "/fake/adb")
    monkeypatch.setattr(adb, "list_packages", _fake_list_packages)
    monkeypatch.setattr(adb, "get_package_version", _fake_get_package_version)
    monkeypatch.setattr(adb, "get_apk_remote_paths", _fake_get_apk_remote_paths)


# ---------------------------------------------------------------------------
# Test 1: UninstallConfirmScreen safety verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uninstall_confirm_safety_unlocks_on_keyword(tmp_path):
    screen = UninstallConfirmScreen("Confirm Deletion", ["com.example.app"])
    app = ApkMigrateApp()
    app.settings.sessions_dir = str(tmp_path / "sessions")
    app.settings.archive_dir = str(tmp_path / "archive")

    async with app.run_test() as pilot:
        # push modal screen
        app.push_screen(screen)
        await pilot.pause()

        # button should start disabled
        confirm_btn = screen.query_one("#confirm", Button)
        assert confirm_btn.disabled is True

        # type incorrect keyword
        input_field = screen.query_one("#confirmation_input", Input)
        input_field.value = "wrongword"
        await pilot.pause()
        assert confirm_btn.disabled is True

        # type correct keyword (case-insensitive)
        input_field.value = "UnInStAlL"
        await pilot.pause()
        assert confirm_btn.disabled is False

        # click cancel
        await pilot.click("#cancel")
        await pilot.pause()


# ---------------------------------------------------------------------------
# Test 2: SingleDeviceAppScreen Union display, Archive, Install, Uninstall
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_device_full_flow(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(adb, "list_devices", lambda adb_path: FAKE_DEVICES)

    # populate fake archive
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for pkg, manifest in ARCHIVE_PACKAGES.items():
        pkg_dir = archive_dir / pkg
        pkg_dir.mkdir(exist_ok=True)
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest))
        (pkg_dir / "base.apk").write_bytes(b"apk bytes")

    # mock device functions
    archived_packages = []
    installed_packages = []
    uninstalled_packages = []

    def fake_pull_file(adb_path, serial, remote_path, local_dest):
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        local_dest.write_bytes(b"pulled apk")
        return adb.AdbResult(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adb, "pull_file", fake_pull_file)

    def fake_install_apks(adb_path, serial, local_apk_paths):
        installed_packages.append(serial)
        return adb.AdbResult(ok=True, returncode=0, stdout="Success", stderr="")

    monkeypatch.setattr(adb, "install_apks", fake_install_apks)

    def fake_uninstall_package(adb_path, serial, package, keep_data=False):
        uninstalled_packages.append(package)
        raw = adb.AdbResult(ok=True, returncode=0, stdout="Success", stderr="")
        return adb.UninstallResult(adb.UninstallOutcome.REMOVED, raw, "Fully removed.")

    monkeypatch.setattr(adb, "uninstall_package", fake_uninstall_package)

    app = ApkMigrateApp()
    app.settings.sessions_dir = str(tmp_path / "sessions")
    app.settings.archive_dir = str(archive_dir)
    app.settings.hide_identical = False

    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        # Choose Single Device mode (key 2)
        await pilot.press("2")
        await pilot.pause()

        assert app.screen.__class__.__name__ == "SingleDeviceSelectScreen"

        # wait for device to be listed in table
        table = app.screen.query_one("#table", DataTable)
        for _ in range(40):
            await pilot.pause()
            if table.row_count > 0:
                break

        # Select DEVICE123 (the only device)
        await pilot.press("c")
        await pilot.pause()

        # wait for scan
        for _ in range(40):
            await pilot.pause()
            if app.screen.__class__.__name__ == "SingleDeviceAppScreen" and not getattr(app.screen, "busy", True):
                break

        list_screen = app.screen
        assert list_screen.__class__.__name__ == "SingleDeviceAppScreen"

        # Verify Union listing entries:
        # device union archive contains: org.fdroid.fdroid, com.example.ondevice, com.example.outdated, com.example.newer, com.example.onlyarchive
        assert len(list_screen.entries) == 5

        by_pkg = {e.package: e for e in list_screen.entries}

        # 1. Device only
        assert by_pkg["org.fdroid.fdroid"].device_app is not None
        assert by_pkg["org.fdroid.fdroid"].archive_manifest is None
        assert "not archived" in by_pkg["org.fdroid.fdroid"].archive_status

        # 2. Archive only
        assert by_pkg["com.example.onlyarchive"].device_app is None
        assert by_pkg["com.example.onlyarchive"].archive_manifest is not None
        assert "not installed" in by_pkg["com.example.onlyarchive"].display_version

        # 3. Both matching
        assert "matching" in by_pkg["com.example.ondevice"].archive_status

        # 4. Outdated (Archive version is newer)
        assert "newer" in by_pkg["com.example.outdated"].archive_status

        # 5. Newer (Device version is newer)
        assert "outdated" in by_pkg["com.example.newer"].archive_status

        # Select com.example.onlyarchive and install
        by_pkg["com.example.onlyarchive"].selected = True
        list_screen.action_install_selected()
        await pilot.pause()
        for _ in range(20):
            await pilot.pause()
            if not getattr(list_screen, "busy", True):
                break
        assert len(installed_packages) == 1

        # Select com.example.newer and archive
        by_pkg["com.example.newer"].selected = True
        list_screen.action_archive_selected()
        await pilot.pause()
        for _ in range(20):
            await pilot.pause()
            if not getattr(list_screen, "busy", True):
                break
        # manifest updated on disk
        manifest_updated = list_screen.archive_mgr.read_manifest("com.example.newer")
        assert manifest_updated is not None
        assert manifest_updated["version_code"] == 10

        # Select org.fdroid.fdroid and uninstall
        by_pkg["org.fdroid.fdroid"].selected = True
        list_screen.action_uninstall_selected()
        await pilot.pause()

        # Safe confirm modal pops up
        assert app.screen.__class__.__name__ == "UninstallConfirmScreen"
        # Enter verification keyword
        input_field = app.screen.query_one("#confirmation_input", Input)
        input_field.value = "uninstall"
        await pilot.pause()
        await pilot.click("#confirm")
        for _ in range(20):
            await pilot.pause()
            if not getattr(list_screen, "busy", True):
                break

        assert "org.fdroid.fdroid" in uninstalled_packages

        await pilot.press("q")
        await pilot.pause()
