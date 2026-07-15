"""Thin, defensive wrapper around the `adb` binary.

Design goals, because this talks to real hardware and real user data:
  - Never use shell=True / string-built commands. Always argv lists.
  - Every external call has a timeout. A yanked USB cable must not hang the TUI forever.
  - Low-level `_run` never raises on a non-zero exit code - callers decide what a given
    non-zero result means (adb overloads exit codes/stderr text for very different situations).
  - High-level functions return typed results (AdbResult) instead of throwing, so the UI layer
    can always show *something* to the user rather than crash mid-batch-operation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .models import AppInfo

DEFAULT_TIMEOUT = 30        # seconds, for quick metadata calls
PULL_TIMEOUT = 300          # seconds, APKs can be large
INSTALL_TIMEOUT = 300


class AdbNotFoundError(RuntimeError):
    pass


@dataclass
class AdbResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    command: list[str] = field(default_factory=list)

    @property
    def combined_output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


@dataclass
class DeviceEntry:
    serial: str
    state: str  # "device", "unauthorized", "offline", "no permissions", ...
    model: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.state == "device"


def find_adb(explicit_path: str | None = None) -> str:
    """Locate the adb binary. Raises AdbNotFoundError with an actionable message."""
    if explicit_path:
        p = Path(explicit_path)
        if p.is_file():
            return str(p)
        raise AdbNotFoundError(
            f"Configured adb path '{explicit_path}' does not exist or is not a file."
        )
    found = shutil.which("adb")
    if found:
        return found
    raise AdbNotFoundError(
        "Could not find 'adb' on your PATH. Install Android platform-tools and either "
        "add it to PATH, or set \"adb_path\" in settings.json to the full path of the adb binary."
    )


def _run(adb_path: str, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> AdbResult:
    cmd = [adb_path, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return AdbResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            command=cmd,
        )
    except subprocess.TimeoutExpired as e:
        return AdbResult(
            ok=False,
            returncode=-1,
            stdout=e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr=f"Command timed out after {timeout}s (device disconnected or unresponsive).",
            timed_out=True,
            command=cmd,
        )
    except FileNotFoundError:
        return AdbResult(
            ok=False, returncode=-1, stdout="", stderr=f"adb binary not found: {adb_path}", command=cmd
        )
    except OSError as e:
        return AdbResult(ok=False, returncode=-1, stdout="", stderr=str(e), command=cmd)


def _run_on(adb_path: str, serial: str, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> AdbResult:
    return _run(adb_path, ["-s", serial, *args], timeout=timeout)


def list_devices(adb_path: str) -> list[DeviceEntry]:
    """Parse `adb devices -l`. Never raises; returns [] on total failure (caller shows the error)."""
    result = _run(adb_path, ["devices", "-l"], timeout=10)
    devices: list[DeviceEntry] = []
    if not result.ok and not result.stdout:
        return devices
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = None
        m = re.search(r"model:(\S+)", line)
        if m:
            model = m.group(1)
        devices.append(DeviceEntry(serial=serial, state=state, model=model))
    return devices


def list_packages(
    adb_path: str, serial: str, third_party_only: bool = True
) -> tuple[dict[str, AppInfo], AdbResult]:
    """Returns (package -> AppInfo with installer filled, last AdbResult for diagnostics)."""
    flags = ["-i"]
    if third_party_only:
        flags.append("-3")
    result = _run_on(adb_path, serial, ["shell", "pm", "list", "packages", *flags], timeout=DEFAULT_TIMEOUT)
    apps: dict[str, AppInfo] = {}
    if not result.ok:
        return apps, result
    # Lines look like: "package:com.example.app  installer=org.fdroid.fdroid"
    # or "package:com.example.app" with no installer suffix on some Android versions.
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        body = line[len("package:"):]
        installer = None
        if "installer=" in body:
            pkg_part, _, inst_part = body.partition("installer=")
            pkg = pkg_part.strip()
            installer = inst_part.strip() or None
            if installer == "null":
                installer = None
        else:
            pkg = body.strip()
        if pkg:
            apps[pkg] = AppInfo(package=pkg, installer=installer)
    return apps, result


_VERSION_CODE_RE = re.compile(r"versionCode=(\d+)")
_VERSION_NAME_RE = re.compile(r"versionName=([^\s]+)")


def get_package_version(adb_path: str, serial: str, package: str) -> tuple[int | None, str | None, AdbResult]:
    """dumpsys package <pkg> -> (versionCode, versionName, raw_result)."""
    result = _run_on(adb_path, serial, ["shell", "dumpsys", "package", package], timeout=DEFAULT_TIMEOUT)
    if not result.ok:
        return None, None, result
    vc_match = _VERSION_CODE_RE.search(result.stdout)
    vn_match = _VERSION_NAME_RE.search(result.stdout)
    version_code = int(vc_match.group(1)) if vc_match else None
    version_name = vn_match.group(1) if vn_match else None
    return version_code, version_name, result


def get_apk_remote_paths(adb_path: str, serial: str, package: str) -> tuple[list[str], AdbResult]:
    """pm path <pkg> -> list of remote apk paths (base + splits)."""
    result = _run_on(adb_path, serial, ["shell", "pm", "path", package], timeout=DEFAULT_TIMEOUT)
    paths: list[str] = []
    if not result.ok:
        return paths, result
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            paths.append(line[len("package:"):].strip())
    return paths, result


def pull_file(adb_path: str, serial: str, remote_path: str, local_dest: Path) -> AdbResult:
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    return _run_on(
        adb_path, serial, ["pull", remote_path, str(local_dest)], timeout=PULL_TIMEOUT
    )


# Known adb install failure substrings we want to explain in plain language rather than
# just dumping raw adb output at the user.
_KNOWN_INSTALL_FAILURES = {
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": (
        "Signature mismatch: the target already has this app installed, signed with a "
        "different key. A normal install cannot overwrite it without uninstalling first "
        "(which erases that app's data on the target)."
    ),
    "INSTALL_FAILED_VERSION_DOWNGRADE": (
        "Target already has a newer versionCode installed. adb refuses downgrade installs "
        "by default."
    ),
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "Target device is out of storage space.",
    "INSTALL_FAILED_DUPLICATE_PACKAGE": "Package already installed identically.",
    "INSTALL_FAILED_NO_MATCHING_ABIS": "Split APKs don't match the target device's CPU architecture.",
    "INSTALL_FAILED_MISSING_SPLIT": "One or more required split APKs were not included.",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "APK is unsigned or corrupted.",
}


def explain_install_failure(raw_output: str) -> str | None:
    for code, explanation in _KNOWN_INSTALL_FAILURES.items():
        if code in raw_output:
            return f"{code}: {explanation}"
    return None


def install_apks(adb_path: str, serial: str, local_apk_paths: list[str]) -> AdbResult:
    """Installs one app. Uses install-multiple automatically when there's more than one APK
    (split APKs), matching how the app was originally shipped."""
    if not local_apk_paths:
        return AdbResult(ok=False, returncode=-1, stdout="", stderr="No APK files to install.")
    if len(local_apk_paths) == 1:
        args = ["install", "-r", local_apk_paths[0]]
    else:
        args = ["install-multiple", "-r", *local_apk_paths]
    return _run_on(adb_path, serial, args, timeout=INSTALL_TIMEOUT)


# ---------------------------------------------------------------------------
# Uninstall — typed outcome with 2-tier cascade + mandatory post-verify
# ---------------------------------------------------------------------------

class UninstallOutcome(str, Enum):
    REMOVED  = "removed"   # fully removed (user-installed app, verified gone)
    HIDDEN   = "hidden"    # system app: removed from user 0 profile, APK stays on /system
    FAILED   = "failed"    # all tiers exhausted; app still active on device


@dataclass
class UninstallResult:
    outcome: UninstallOutcome
    raw: AdbResult
    message: str


_KNOWN_UNINSTALL_FAILURES: dict[str, str] = {
    "DELETE_FAILED_INTERNAL_ERROR": (
        "System app cannot be removed without root. "
        "Try the Disable action to freeze it instead."
    ),
    "DELETE_FAILED_DEVICE_POLICY_MANAGER": (
        "App is a device administrator and cannot be uninstalled. "
        "Remove it as a device admin in Settings first, or use Disable."
    ),
    "DELETE_FAILED_OWNER_BLOCKED": (
        "App is blocked by a device owner or enterprise policy."
    ),
}


def explain_uninstall_failure(raw_output: str) -> str | None:
    for code, explanation in _KNOWN_UNINSTALL_FAILURES.items():
        if code in raw_output:
            return f"{code}: {explanation}"
    return None


def check_package_installed_for_user(
    adb_path: str, serial: str, package: str, user: int = 0
) -> bool:
    """Ground-truth post-uninstall verifier.

    Runs ``pm list packages --user <N> <package>`` **without** the ``-u`` flag so
    packages that have been per-user uninstalled but still reside on the read-only
    /system partition are excluded from the output.

    Returns True if the package is still visible/installed for that user,
    False if it has been successfully removed from that user's view.
    """
    result = _run_on(
        adb_path, serial,
        ["shell", "pm", "list", "packages", "--user", str(user), package],
        timeout=DEFAULT_TIMEOUT,
    )
    return f"package:{package}" in result.stdout


def disable_package_for_user(
    adb_path: str, serial: str, package: str, user: int = 0
) -> AdbResult:
    """Freeze an app for a specific user via ``pm disable-user``.

    This is a reversible alternative to uninstalling protected system apps:
    the app cannot launch or receive updates, but the APK remains on the
    device.  To re-enable: ``adb shell pm enable --user <N> <package>``.
    """
    return _run_on(
        adb_path, serial,
        ["shell", "pm", "disable-user", "--user", str(user), package],
        timeout=DEFAULT_TIMEOUT,
    )


def uninstall_package(
    adb_path: str, serial: str, package: str, keep_data: bool = False
) -> UninstallResult:
    """2-tier uninstall cascade with mandatory per-tier post-action verification.

    Tier 1 — ``adb uninstall [-k] <pkg>``
        Works for normal user-installed apps.  For system apps that received
        Play Store updates, Android removes the update layer and returns
        ``Success`` (exit 0) even though the factory APK on /system is still
        active.  The post-verify step catches this false-positive and lets the
        cascade fall through to Tier 2.

    Tier 2 — ``adb shell pm uninstall [-k] --user 0 <pkg>``
        Removes the package from the primary user's profile without touching
        the read-only system partition.  Outcome is ``HIDDEN``; the app is
        invisible and non-runnable for user 0 but can be restored via
        ``pm install-existing <pkg>``.

    If both tiers fail (or a nominal ``Success`` still leaves the package
    present on post-verify), outcome is ``FAILED`` with an actionable message.
    """
    keep_flag = ["-k"] if keep_data else []

    # --- Tier 1: standard adb uninstall ---
    r1 = _run_on(adb_path, serial, ["uninstall", *keep_flag, package], timeout=DEFAULT_TIMEOUT)
    if r1.ok:
        still_present = check_package_installed_for_user(adb_path, serial, package)
        if not still_present:
            return UninstallResult(UninstallOutcome.REMOVED, r1, "Fully removed.")
        # r1.ok == True but package is still present → Tier 1 only stripped a Play
        # Store update layer; factory APK on /system is still active.  Fall through.

    # --- Tier 2: pm uninstall --user 0 ---
    r2 = _run_on(
        adb_path, serial,
        ["shell", "pm", "uninstall", *keep_flag, "--user", "0", package],
        timeout=DEFAULT_TIMEOUT,
    )
    if "Success" in r2.stdout:
        still_present = check_package_installed_for_user(adb_path, serial, package)
        if not still_present:
            return UninstallResult(
                UninstallOutcome.HIDDEN, r2,
                "Removed from this device profile. "
                "System APK remains on /system (not visible or runnable for this user). "
                "Restore with: adb shell pm install-existing " + package,
            )
        # Extremely rare: pm reported Success but package still present (device bug).
        # Fall through to FAILED.

    # --- Both tiers exhausted ---
    raw_err = (
        explain_uninstall_failure(r2.combined_output)
        or (r2.combined_output.splitlines()[-1] if r2.combined_output else "")
        or "All uninstall strategies failed."
    )
    return UninstallResult(
        UninstallOutcome.FAILED, r2,
        f"{raw_err} — If this is a protected system app, try the Disable action instead.",
    )
