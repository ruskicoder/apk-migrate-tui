from apk_migrate_tui import adb


def test_list_devices_parses_states():
    class FakeResult:
        ok = True
        stdout = (
            "List of devices attached\n"
            "1234567 device usb:1-1 product:panther model:Pixel_7 device:panther transport_id:1\n"
            "ABCDEF unauthorized usb:1-2 transport_id:2\n"
            "\n"
        )

    devices = None
    import unittest.mock as mock

    with mock.patch.object(adb, "_run", return_value=FakeResult()):
        devices = adb.list_devices("adb")

    assert len(devices) == 2
    assert devices[0].serial == "1234567"
    assert devices[0].state == "device"
    assert devices[0].is_ready
    assert devices[0].model == "Pixel_7"
    assert devices[1].state == "unauthorized"
    assert not devices[1].is_ready


def test_list_devices_empty_on_no_output():
    class FakeResult:
        ok = False
        stdout = ""

    import unittest.mock as mock

    with mock.patch.object(adb, "_run", return_value=FakeResult()):
        devices = adb.list_devices("adb")
    assert devices == []


def test_list_packages_parses_installer():
    class FakeResult:
        ok = True
        stdout = (
            "package:org.fdroid.fdroid installer=null\n"
            "package:com.example.sideload installer=null\n"
            "package:com.aurora.store installer=com.android.vending\n"
        )

    import unittest.mock as mock

    with mock.patch.object(adb, "_run_on", return_value=FakeResult()):
        apps, result = adb.list_packages("adb", "SERIAL", third_party_only=True)

    assert set(apps.keys()) == {"org.fdroid.fdroid", "com.example.sideload", "com.aurora.store"}
    assert apps["org.fdroid.fdroid"].installer is None
    assert apps["com.aurora.store"].installer == "com.android.vending"


def test_get_package_version_parses_dumpsys():
    class FakeResult:
        ok = True
        stdout = (
            "Package [org.fdroid.fdroid] (abcd1234):\n"
            "  userId=10123\n"
            "    versionCode=1015050 minSdk=21 targetSdk=33\n"
            "    versionName=1.15.0\n"
        )

    import unittest.mock as mock

    with mock.patch.object(adb, "_run_on", return_value=FakeResult()):
        vc, vn, result = adb.get_package_version("adb", "SERIAL", "org.fdroid.fdroid")

    assert vc == 1015050
    assert vn == "1.15.0"


def test_get_package_version_missing_fields_returns_none():
    class FakeResult:
        ok = True
        stdout = "Package [x] not found\n"

    import unittest.mock as mock

    with mock.patch.object(adb, "_run_on", return_value=FakeResult()):
        vc, vn, result = adb.get_package_version("adb", "SERIAL", "x")

    assert vc is None
    assert vn is None


def test_get_apk_remote_paths_handles_split_apks():
    class FakeResult:
        ok = True
        stdout = (
            "package:/data/app/~~abc==/com.example-1/base.apk\n"
            "package:/data/app/~~abc==/com.example-1/split_config.arm64_v8a.apk\n"
            "package:/data/app/~~abc==/com.example-1/split_config.xxhdpi.apk\n"
        )

    import unittest.mock as mock

    with mock.patch.object(adb, "_run_on", return_value=FakeResult()):
        paths, result = adb.get_apk_remote_paths("adb", "SERIAL", "com.example")

    assert len(paths) == 3
    assert paths[0].endswith("base.apk")


def test_explain_install_failure_known_code():
    msg = adb.explain_install_failure("Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: some detail]")
    assert msg is not None
    assert "Signature mismatch" in msg


def test_explain_install_failure_unknown_code_returns_none():
    assert adb.explain_install_failure("Failure [SOMETHING_WE_DONT_KNOW]") is None


def test_install_apks_uses_install_multiple_for_splits():
    import unittest.mock as mock

    captured = {}

    def fake_run_on(adb_path, serial, args, timeout=30):
        captured["args"] = args
        class R:
            ok = True
            stdout = "Success"
            stderr = ""
            combined_output = "Success"
        return R()

    with mock.patch.object(adb, "_run_on", side_effect=fake_run_on):
        adb.install_apks("adb", "SERIAL", ["base.apk", "split.apk"])

    assert captured["args"][0] == "install-multiple"


def test_install_apks_uses_plain_install_for_single_apk():
    import unittest.mock as mock

    captured = {}

    def fake_run_on(adb_path, serial, args, timeout=30):
        captured["args"] = args
        class R:
            ok = True
            stdout = "Success"
            stderr = ""
            combined_output = "Success"
        return R()

    with mock.patch.object(adb, "_run_on", side_effect=fake_run_on):
        adb.install_apks("adb", "SERIAL", ["base.apk"])

    assert captured["args"][0] == "install"


# ---------------------------------------------------------------------------
# Uninstall cascade — 2-tier with post-verify (check_package_installed_for_user)
# ---------------------------------------------------------------------------

def _make_adb_result(ok: bool, stdout: str = "", stderr: str = "") -> adb.AdbResult:
    return adb.AdbResult(ok=ok, returncode=0 if ok else 1, stdout=stdout, stderr=stderr)


def test_uninstall_normal_app_fully_removed():
    """Tier 1 succeeds and post-verify confirms package is gone → REMOVED."""
    import unittest.mock as mock

    call_n = {"n": 0}
    def fake_run_on(adb_path, serial, args, timeout=30):
        call_n["n"] += 1
        if call_n["n"] == 1:
            # Tier 1: adb uninstall → ok
            return _make_adb_result(ok=True, stdout="Success")
        # verify: pm list packages --user 0 → empty (gone)
        return _make_adb_result(ok=True, stdout="")

    with mock.patch.object(adb, "_run_on", side_effect=fake_run_on):
        result = adb.uninstall_package("/adb", "SERIAL", "com.example.app")

    assert result.outcome == adb.UninstallOutcome.REMOVED


def test_uninstall_system_app_ota_update_false_positive_caught():
    """Scenario A: Tier 1 'Success' but package still present (OTA update stripped only).
    Cascade must fall through to Tier 2, which then succeeds → HIDDEN.
    """
    import unittest.mock as mock

    call_n = {"n": 0}
    def fake_run_on(adb_path, serial, args, timeout=30):
        call_n["n"] += 1
        if call_n["n"] == 1:
            # Tier 1: adb uninstall → ok (stripped update layer)
            return _make_adb_result(ok=True, stdout="Success")
        if call_n["n"] == 2:
            # Tier 1 post-verify: package STILL present (factory APK on /system)
            return _make_adb_result(ok=True, stdout="package:com.android.youtube")
        if call_n["n"] == 3:
            # Tier 2: pm uninstall --user 0 → Success
            return _make_adb_result(ok=True, stdout="Success")
        # Tier 2 post-verify: package gone from user 0 view
        return _make_adb_result(ok=True, stdout="")

    with mock.patch.object(adb, "_run_on", side_effect=fake_run_on):
        result = adb.uninstall_package("/adb", "SERIAL", "com.android.youtube")

    assert result.outcome == adb.UninstallOutcome.HIDDEN
    assert call_n["n"] == 4  # must have run all 4 calls


def test_uninstall_system_app_no_updates_tier1_fails_tier2_hidden():
    """Tier 1 fails (non-zero exit); Tier 2 succeeds → HIDDEN."""
    import unittest.mock as mock

    call_n = {"n": 0}
    def fake_run_on(adb_path, serial, args, timeout=30):
        call_n["n"] += 1
        if call_n["n"] == 1:
            # Tier 1 fails
            return _make_adb_result(ok=False, stderr="Failure [DELETE_FAILED_INTERNAL_ERROR]")
        if call_n["n"] == 2:
            # Tier 2: pm uninstall --user 0 → Success
            return _make_adb_result(ok=True, stdout="Success")
        # Tier 2 post-verify: gone
        return _make_adb_result(ok=True, stdout="")

    with mock.patch.object(adb, "_run_on", side_effect=fake_run_on):
        result = adb.uninstall_package("/adb", "SERIAL", "com.android.chrome")

    assert result.outcome == adb.UninstallOutcome.HIDDEN


def test_uninstall_protected_app_all_tiers_fail():
    """Both Tier 1 and Tier 2 fail → FAILED with actionable message."""
    import unittest.mock as mock

    call_n = {"n": 0}
    def fake_run_on(adb_path, serial, args, timeout=30):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return _make_adb_result(ok=False, stderr="Failure [DELETE_FAILED_DEVICE_POLICY_MANAGER]")
        # Tier 2 returns Failure
        return _make_adb_result(ok=False, stdout="Failure [DELETE_FAILED_DEVICE_POLICY_MANAGER]",
                                stderr="")

    with mock.patch.object(adb, "_run_on", side_effect=fake_run_on):
        result = adb.uninstall_package("/adb", "SERIAL", "com.carrier.dpc")

    assert result.outcome == adb.UninstallOutcome.FAILED
    assert "device administrator" in result.message.lower() or "disable" in result.message.lower()


def test_uninstall_tier2_success_but_package_still_present_is_failed():
    """Edge case: Tier 1 fails, Tier 2 returns 'Success' but post-verify shows package still
    present (rare Android bug). Must NOT be accepted as HIDDEN → outcome is FAILED."""
    import unittest.mock as mock

    call_n = {"n": 0}
    def fake_run_on(adb_path, serial, args, timeout=30):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return _make_adb_result(ok=False, stderr="Failure [DELETE_FAILED_INTERNAL_ERROR]")
        if call_n["n"] == 2:
            # Tier 2 claims Success
            return _make_adb_result(ok=True, stdout="Success")
        # Tier 2 post-verify: package STILL present (device-level bug / inconsistency)
        return _make_adb_result(ok=True, stdout="package:com.example.stubborn")

    with mock.patch.object(adb, "_run_on", side_effect=fake_run_on):
        result = adb.uninstall_package("/adb", "SERIAL", "com.example.stubborn")

    assert result.outcome == adb.UninstallOutcome.FAILED


def test_disable_package_for_user_success():
    """disable_package_for_user wraps pm disable-user and returns ok result."""
    import unittest.mock as mock

    captured = {}
    def fake_run_on(adb_path, serial, args, timeout=30):
        captured["args"] = args
        return _make_adb_result(ok=True, stdout="Package com.example.app new state: disabled-user")

    with mock.patch.object(adb, "_run_on", side_effect=fake_run_on):
        result = adb.disable_package_for_user("/adb", "SERIAL", "com.example.app")

    assert result.ok
    assert "disable-user" in captured["args"]
    assert "--user" in captured["args"]


def test_explain_uninstall_failure_known_codes():
    assert "device administrator" in (
        adb.explain_uninstall_failure("Failure [DELETE_FAILED_DEVICE_POLICY_MANAGER]") or ""
    ).lower()
    assert "root" in (
        adb.explain_uninstall_failure("Failure [DELETE_FAILED_INTERNAL_ERROR]") or ""
    ).lower()
    assert adb.explain_uninstall_failure("Failure [UNKNOWN_CODE]") is None

