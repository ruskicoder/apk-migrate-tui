"""Pure comparison logic - no adb calls here, just dict-in/list-out. Easy to unit test."""

from __future__ import annotations

from .models import AppInfo, DiffEntry, DiffStatus


def compute_diff(
    source_apps: dict[str, AppInfo], target_apps: dict[str, AppInfo]
) -> list[DiffEntry]:
    entries: list[DiffEntry] = []
    all_packages = sorted(set(source_apps) | set(target_apps))

    for pkg in all_packages:
        src = source_apps.get(pkg)
        tgt = target_apps.get(pkg)

        if src and tgt:
            if src.version_code is not None and src.version_code == tgt.version_code:
                status = DiffStatus.IDENTICAL
            else:
                # Covers: different version codes, OR one/both codes unknown (couldn't be
                # read - dumpsys failed / permission issue). Treat "unknown" as "different"
                # rather than silently assuming identical, since that's the safer default
                # for a destructive-adjacent tool.
                status = DiffStatus.VERSION_DIFF
        elif src and not tgt:
            status = DiffStatus.SOURCE_ONLY
        else:
            status = DiffStatus.TARGET_ONLY

        entries.append(DiffEntry(package=pkg, status=status, source=src, target=tgt))

    return entries


def filter_entries(
    entries: list[DiffEntry],
    hide_identical: bool,
    show_target_only: bool,
    search: str = "",
) -> list[DiffEntry]:
    search_lower = search.strip().lower()
    out = []
    for e in entries:
        if hide_identical and e.status is DiffStatus.IDENTICAL:
            continue
        if not show_target_only and e.status is DiffStatus.TARGET_ONLY:
            continue
        if search_lower:
            haystack = f"{e.package} {e.label}".lower()
            if search_lower not in haystack:
                continue
        out.append(e)
    return out
