"""Plain data models. No I/O here on purpose, so this module is trivially testable."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DiffStatus(str, Enum):
    IDENTICAL = "identical"          # same package, same versionCode on both devices
    VERSION_DIFF = "version_diff"    # same package, different versionCode
    SOURCE_ONLY = "source_only"      # only present on source (pixel 6)
    TARGET_ONLY = "target_only"      # only present on target (pixel 10) - informational


@dataclass
class AppInfo:
    """Everything we know about one installed app on one device."""

    package: str
    version_code: int | None = None
    version_name: str | None = None
    label: str | None = None
    installer: str | None = None      # e.g. org.fdroid.fdroid, dev.imranr.obtainium, None = sideloaded
    apk_remote_paths: list[str] = field(default_factory=list)  # /data/app/.../base.apk, splits...

    @property
    def is_split(self) -> bool:
        return len(self.apk_remote_paths) > 1

    @property
    def display_version(self) -> str:
        if self.version_name and self.version_code is not None:
            return f"{self.version_name} ({self.version_code})"
        if self.version_code is not None:
            return f"({self.version_code})"
        return self.version_name or "unknown"


@dataclass
class DiffEntry:
    package: str
    status: DiffStatus
    source: AppInfo | None = None
    target: AppInfo | None = None
    selected: bool = False
    archived: bool = False  # set in-session after a successful archive, for UI feedback only

    @property
    def label(self) -> str:
        info = self.source or self.target
        return (info.label if info and info.label else self.package)


class SourceFilter(str, Enum):
    ALL_NON_SYSTEM = "all_non_system"
    FOSS_SIDELOADED = "foss_sideloaded"   # installer is F-Droid/Obtainium/None(sideload)/adb

    def matches(self, app: AppInfo) -> bool:
        if self is SourceFilter.ALL_NON_SYSTEM:
            return True
        # FOSS_SIDELOADED
        known_foss_installers = {
            None,
            "",
            "org.fdroid.fdroid",
            "dev.imranr.obtainium",
            "com.aurora.store",
            "com.machiav3lli.backup",
            "com.android.shell",  # adb install
        }
        return app.installer in known_foss_installers


class ActionKind(str, Enum):
    ARCHIVE = "archive"
    INSTALL = "install"
    ARCHIVE_AND_INSTALL = "archive_and_install"
