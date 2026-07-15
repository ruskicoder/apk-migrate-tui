from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import adb
from .archive import ArchiveManager
from .models import AppInfo, DiffEntry

logger = logging.getLogger("apk_migrate_tui")


@dataclass
class OpResult:
    package: str
    action: str          # "archive" | "install" | "removed" | "hidden (system)" | "disabled (system)" | "uninstall" (failed)
    success: bool
    message: str
    skipped: bool = False


def scan_device(
    adb_path: str,
    serial: str,
    third_party_only: bool,
    on_progress=None,
) -> tuple[dict[str, AppInfo], list[str]]:
    """Full inventory of one device: packages + version info + apk paths.

    Returns (apps, warnings). Never raises - a single package failing to report its
    version doesn't abort the whole scan, it just gets flagged as unknown/version_diff
    downstream (see diff.compute_diff), which is the safe default.
    """
    apps, list_result = adb.list_packages(adb_path, serial, third_party_only=third_party_only)
    warnings: list[str] = []
    if not list_result.ok:
        warnings.append(f"Could not list packages on {serial}: {list_result.combined_output}")
        return {}, warnings

    total = len(apps)
    for i, (pkg, info) in enumerate(apps.items(), start=1):
        if on_progress:
            on_progress(i, total, pkg)
        vc, vn, vresult = adb.get_package_version(adb_path, serial, pkg)
        info.version_code = vc
        info.version_name = vn
        if not vresult.ok:
            warnings.append(f"{pkg}: could not read version ({vresult.combined_output.splitlines()[-1] if vresult.combined_output else 'unknown error'})")

        paths, presult = adb.get_apk_remote_paths(adb_path, serial, pkg)
        info.apk_remote_paths = paths
        if not presult.ok or not paths:
            warnings.append(f"{pkg}: could not resolve APK path(s), will be skipped for archive/install")

    return apps, warnings


def archive_package(
    adb_path: str,
    source_serial: str,
    info: AppInfo,
    archive_mgr: ArchiveManager,
    force: bool = False,
) -> OpResult:
    pkg = info.package
    if not info.apk_remote_paths:
        return OpResult(pkg, "archive", False, "No APK path known for this package (scan issue).")

    if not force and archive_mgr.already_has_version(pkg, info.version_code):
        return OpResult(pkg, "archive", True, "Already archived at this version, skipped.", skipped=True)

    staged_dir = archive_mgr.staging_dir(pkg)
    staged_dir.mkdir(parents=True, exist_ok=True)
    local_names: list[str] = []
    try:
        for remote_path in info.apk_remote_paths:
            local_name = Path(remote_path).name  # base.apk / split_config.xxx.apk
            local_dest = staged_dir / local_name
            result = adb.pull_file(adb_path, source_serial, remote_path, local_dest)
            if not result.ok or not local_dest.exists():
                archive_mgr.discard_staging(staged_dir)
                logger.error("Pull failed for %s (%s): %s", pkg, remote_path, result.combined_output)
                return OpResult(pkg, "archive", False, f"Pull failed: {result.combined_output.splitlines()[-1] if result.combined_output else 'unknown error'}")
            local_names.append(local_name)

        final_dir = archive_mgr.commit(pkg, staged_dir, info, local_names)
        logger.info("Archived %s v%s -> %s", pkg, info.version_code, final_dir)
        return OpResult(pkg, "archive", True, f"Archived to {final_dir}")
    except Exception as e:  # noqa: BLE001 - last line of defense, must never crash the batch
        archive_mgr.discard_staging(staged_dir)
        logger.exception("Unexpected error archiving %s", pkg)
        return OpResult(pkg, "archive", False, f"Unexpected error: {e}")


def install_package(
    adb_path: str,
    target_serial: str,
    local_apk_paths: list[str],
    package: str,
) -> OpResult:
    if not local_apk_paths:
        return OpResult(package, "install", False, "No local APK files available to install.")
    result = adb.install_apks(adb_path, target_serial, local_apk_paths)
    if result.ok:
        logger.info("Installed %s on %s", package, target_serial)
        return OpResult(package, "install", True, "Installed successfully.")

    explanation = adb.explain_install_failure(result.combined_output)
    msg = explanation or (result.combined_output.splitlines()[-1] if result.combined_output else "Install failed (no output).")
    logger.error("Install failed for %s: %s", package, result.combined_output)
    return OpResult(package, "install", False, msg)


_UNINSTALL_ACTION_LABELS: dict[adb.UninstallOutcome, str] = {
    adb.UninstallOutcome.REMOVED: "removed",
    adb.UninstallOutcome.HIDDEN:  "hidden (system)",
    adb.UninstallOutcome.FAILED:  "uninstall",
}


def uninstall_package(adb_path: str, target_serial: str, package: str, keep_data: bool) -> OpResult:
    result = adb.uninstall_package(adb_path, target_serial, package, keep_data=keep_data)
    success = result.outcome != adb.UninstallOutcome.FAILED
    action = _UNINSTALL_ACTION_LABELS[result.outcome]
    if success:
        logger.warning("Uninstalled %s from %s — outcome: %s", package, target_serial, result.outcome)
    else:
        logger.error("Uninstall failed for %s: %s", package, result.message)
    return OpResult(package, action, success, result.message)


def disable_package(adb_path: str, target_serial: str, package: str) -> OpResult:
    """Freeze app for user 0 via pm disable-user. Does NOT remove it — explicit user action only."""
    result = adb.disable_package_for_user(adb_path, target_serial, package)
    ok = result.ok or "disabled" in result.stdout.lower()
    if ok:
        msg = f"App frozen. Cannot launch or update. Restore with: adb shell pm enable {package}"
        logger.warning("Disabled %s on %s", package, target_serial)
        return OpResult(package, "disabled (system)", True, msg)
    raw = result.combined_output.splitlines()[-1] if result.combined_output else "Disable failed."
    logger.error("Disable failed for %s: %s", package, result.combined_output)
    return OpResult(package, "disable", False, raw)


def archive_and_install(
    adb_path: str,
    source_serial: str,
    target_serial: str,
    info: AppInfo,
    archive_mgr: ArchiveManager,
    force_archive: bool = False,
) -> list[OpResult]:
    results = [archive_package(adb_path, source_serial, info, archive_mgr, force=force_archive)]
    if not results[0].success:
        return results  # don't attempt install off a failed/incomplete pull

    manifest = archive_mgr.read_manifest(info.package) or {}
    apk_dir = archive_mgr.root / info.package
    local_paths = [str(apk_dir / name) for name in manifest.get("apk_files", [])]
    results.append(install_package(adb_path, target_serial, local_paths, info.package))
    return results
