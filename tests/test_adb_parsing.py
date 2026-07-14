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
