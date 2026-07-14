"""Archive layout (flat, per package - no version history, by design):

archive/
  com.package.name/
    manifest.json
    base.apk
    split_config.arm64_v8a.apk   (if present)

Writes are staged in a temp sibling directory and swapped into place at the end, so a
pull that gets interrupted (USB unplugged, process killed) can never leave a half-written
manifest.json pointing at APK files that don't actually exist.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from .models import AppInfo


class ArchiveManager:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    # ---- read side -------------------------------------------------------

    def manifest_path(self, package: str) -> Path:
        return self.root / package / "manifest.json"

    def read_manifest(self, package: str) -> dict | None:
        path = self.manifest_path(package)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def archived_version_code(self, package: str) -> int | None:
        manifest = self.read_manifest(package)
        if not manifest:
            return None
        return manifest.get("version_code")

    def already_has_version(self, package: str, version_code: int | None) -> bool:
        if version_code is None:
            return False
        return self.archived_version_code(package) == version_code

    # ---- write side (staged + atomic swap) --------------------------------

    def staging_dir(self, package: str) -> Path:
        return self.root / f".{package}.staging-{int(time.time() * 1000)}"

    def commit(self, package: str, staged_dir: Path, info: AppInfo, local_apk_names: list[str]) -> Path:
        """Move a fully-populated staged_dir into place as archive/<package>/, replacing
        any previous archived version of this package. Returns the final path."""
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "package": info.package,
            "version_code": info.version_code,
            "version_name": info.version_name,
            "installer": info.installer,
            "apk_files": local_apk_names,
            "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        (staged_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

        final_dir = self.root / package
        backup_dir = self.root / f".{package}.old-{int(time.time() * 1000)}"

        if final_dir.exists():
            final_dir.rename(backup_dir)
        try:
            staged_dir.rename(final_dir)
        except OSError:
            # Roll back: restore the previous archive if the swap itself failed.
            if backup_dir.exists():
                backup_dir.rename(final_dir)
            raise
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        return final_dir

    def discard_staging(self, staged_dir: Path) -> None:
        shutil.rmtree(staged_dir, ignore_errors=True)
