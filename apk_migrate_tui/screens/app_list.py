"""App diff / batch-action screen.

This screen is session-aware: it reads source and target app inventories from the
Session object (populated by DeviceSelectScreen scans) and writes per-package
execution states back to the session after each step.

Key behavioural changes vs. the original:
- ``do_scan()`` reads from the session cache (no ADB calls on initial load).
- ``_run_batch()`` checks device presence before each item, parks items whose
  required device is absent, and retries them when the user reconnects + presses [r].
- Per-package ExecutionState is written to the session before and after each step
  so a crash leaves a recoverable in-flight marker.
- ``escape`` cancels the current batch after the in-progress item finishes.
- On session completion, the session file is deleted from disk.
- If ``settings.cleanup_after_install`` is true, the local archive folder for an
  app is deleted after a successful install.
- Disk space pre-flight estimate is shown in the confirm dialog body.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from .. import adb
from ..archive import ArchiveManager
from ..diff import compute_diff, filter_entries
from ..models import ActionKind, AppInfo, DiffEntry, DiffStatus, SourceFilter
from ..operations import archive_package, install_package, scan_device, uninstall_package
from ..session import (
    ExecutionState,
    PackageExecution,
    Session,
    SessionManager,
    TERMINAL_STATES,
    appinfo_to_dict,
)
from ..settings import Settings
from .dialogs import ConfirmScreen, MessageScreen, RescanChoiceScreen
from .settings_screen import SettingsScreen

_STATUS_LABEL = {
    DiffStatus.IDENTICAL:    "[green]identical[/green]",
    DiffStatus.VERSION_DIFF: "[yellow]version diff[/yellow]",
    DiffStatus.SOURCE_ONLY:  "[cyan]source only[/cyan]",
    DiffStatus.TARGET_ONLY:  "[dim]target only[/dim]",
}

_MAX_CONSECUTIVE_TIMEOUTS = 3
_EST_BYTES_PER_APP = 30 * 1024 * 1024   # 30 MiB conservative estimate per FOSS app


class AppListScreen(Screen[str]):
    """Dismisses with 'change_devices' or 'quit'."""

    BINDINGS = [
        ("space", "toggle_select", "Select"),
        ("a", "select_all", "Select all visible"),
        ("A", "select_none", "Select none"),
        ("s", "archive_selected", "Archive"),
        ("i", "install_selected", "Install"),
        ("b", "both_selected", "Archive+Install"),
        ("u", "force_reinstall_selected", "Force reinstall (erases target data)"),
        ("f", "toggle_hide_identical", "Toggle hide-identical"),
        ("comma", "open_settings", "Settings"),
        ("r", "rescan_or_retry", "Rescan / Retry parked"),
        ("escape", "cancel_batch", "Cancel batch"),
        ("d", "change_devices", "Change devices"),
        ("slash", "focus_search", "Search"),
        ("q", "quit_app", "Quit"),
    ]

    DEFAULT_CSS = """
    AppListScreen { layout: vertical; }
    #summary { height: 1; padding: 0 1; }
    #search { height: 3; }
    DataTable { height: 1fr; }
    #log { height: 10; border-top: solid $primary; }
    """

    def __init__(
        self,
        adb_path: str,
        session: Session,
        session_mgr: SessionManager,
        settings: Settings,
    ) -> None:
        super().__init__()
        self.adb_path = adb_path
        self.session = session
        self.session_mgr = session_mgr
        self.settings = settings
        self.archive_mgr = ArchiveManager(settings.archive_dir)
        self.entries: list[DiffEntry] = []
        self.visible_entries: list[DiffEntry] = []
        self.search_term = ""
        self.busy = False
        self._cancel_requested = False
        # Parked items: entries whose required device was absent during the last batch run.
        self._parked_items: list[DiffEntry] = []
        self._parked_action: ActionKind | None = None

        # Populated from session in on_mount
        self.source_serial: str = ""
        self.target_serial: str = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("", id="summary")
        yield Input(placeholder="Search package / label… (press / to focus)", id="search")
        yield DataTable(id="table", cursor_type="row")
        yield RichLog(id="log", wrap=True, markup=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        # Validate session has both sides populated
        if not self.session.source or not self.session.target:
            self.app.push_screen(
                MessageScreen(
                    "Session error",
                    "Session is missing source or target scan data.  "
                    "Please go back and rescan both devices.",
                )
            )
            return

        self.source_serial = self.session.source.serial
        self.target_serial = self.session.target.serial

        src_name = self.session.source.model or self.source_serial
        tgt_name = self.session.target.model or self.target_serial

        table = self.query_one("#table", DataTable)
        table.add_columns("Sel", "Package", "Status", "Source ver", "Target ver")

        self.log_widget = self.query_one("#log", RichLog)
        self.log_widget.write(
            f"[b]Source:[/b] {src_name} ({self.source_serial})   "
            f"[b]Target:[/b] {tgt_name} ({self.target_serial})\n"
            f"[b]Archive dir:[/b] {self.archive_mgr.root}"
        )
        table.focus()
        self.do_scan()

    # ------------------------------------------------------------------
    # Status / summary helpers
    # ------------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.query_one("#summary", Static).update(text)

    def _update_summary(self) -> None:
        total = len(self.entries)
        selected = sum(1 for e in self.entries if e.selected)
        identical = sum(1 for e in self.entries if e.status is DiffStatus.IDENTICAL)
        diff = sum(1 for e in self.entries if e.status is DiffStatus.VERSION_DIFF)
        src_only = sum(1 for e in self.entries if e.status is DiffStatus.SOURCE_ONLY)
        parked_note = f"  [yellow]⚠ {len(self._parked_items)} parked — press [r] to retry[/yellow]" if self._parked_items else ""
        self._set_status(
            f"{total} apps  |  identical={identical} diff={diff} src-only={src_only}  |  "
            f"selected={selected}  |  hide-identical={'on' if self.settings.hide_identical else 'off'}"
            f"{parked_note}"
        )

    # ------------------------------------------------------------------
    # Scanning — reads from session cache, no live ADB on first load
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def do_scan(self) -> None:
        """Rebuild the diff from session-cached scan data (no ADB calls)."""
        self.busy = True
        self._set_status("Building package diff from session data…")

        source_apps = self.session.source.get_app_infos()
        target_apps = self.session.target.get_app_infos()

        src_filter = SourceFilter(self.settings.source_filter)
        filtered_source = {
            pkg: info for pkg, info in source_apps.items()
            if src_filter.matches(info)
        }

        self.entries = compute_diff(filtered_source, target_apps)

        # Apply existing execution states from session (for resumed sessions)
        for exe in self.session.executions.values():
            entry = next((e for e in self.entries if e.package == exe.package), None)
            if entry is None:
                continue
            if exe.state is ExecutionState.INSTALLED:
                # Already installed on target — show as identical
                if entry.source:
                    entry.target = AppInfo(
                        package=entry.package,
                        version_code=entry.source.version_code,
                        version_name=entry.source.version_name,
                    )
                entry.status = DiffStatus.IDENTICAL
                entry.archived = True
            elif exe.state in (ExecutionState.ARCHIVED,):
                entry.archived = True

        self.busy = False
        self.refresh_table()

        if not source_apps and not target_apps:
            self.app.push_screen(
                MessageScreen(
                    "No app data",
                    "Both device scans returned no packages.  "
                    "Press [r] to rescan from the live devices.",
                )
            )

    # ------------------------------------------------------------------
    # Rescan / retry parked items
    # ------------------------------------------------------------------

    def action_rescan_or_retry(self) -> None:
        if self.busy:
            self.notify("An operation is running.", severity="warning")
            return
        if self._parked_items:
            self._retry_parked()
            return
        # No parked items → ask which device to rescan
        def _handle(choice: str | None) -> None:
            if choice is None:
                return
            self._rescan_live_worker(choice)

        self.app.push_screen(RescanChoiceScreen(), _handle)

    def _retry_parked(self) -> None:
        """Re-select parked entries and re-run the batch with the same action."""
        if not self._parked_items or self._parked_action is None:
            self._parked_items.clear()
            return
        items = list(self._parked_items)
        action = self._parked_action
        self._parked_items.clear()
        self._parked_action = None
        for entry in items:
            entry.selected = True
            self._update_row(entry)
        self._update_summary()
        # Re-run through the normal confirm + batch flow
        self._start_batch(action)

    @work(exclusive=True)
    async def _rescan_live_worker(self, which: str) -> None:
        """Live-scan source, target, or both and rebuild the diff."""
        self.busy = True

        if which in ("source", "both") and self.session.source:
            serial = self.session.source.serial

            def prog_src(i: int, t: int, pkg: str) -> None:
                self.app.call_from_thread(
                    self._set_status, f"Rescanning SOURCE ({serial}): {pkg} ({i}/{t})"
                )

            try:
                apps, warnings = await asyncio.to_thread(
                    scan_device, self.adb_path, serial,
                    self.settings.third_party_only, prog_src
                )
                if apps:
                    self.session.source.apps = {
                        pkg: appinfo_to_dict(info) for pkg, info in apps.items()
                    }
                    if warnings:
                        self.log_widget.write(
                            f"[yellow]{len(warnings)} warning(s) during SOURCE rescan.[/yellow]"
                        )
                else:
                    self.log_widget.write(
                        f"[yellow]SOURCE rescan returned 0 packages — "
                        "check connection and USB debugging authorization.[/yellow]"
                    )
            except Exception as exc:
                self.log_widget.write(f"[red]SOURCE rescan error: {exc}[/red]")

        if which in ("target", "both") and self.session.target:
            serial = self.session.target.serial

            def prog_tgt(i: int, t: int, pkg: str) -> None:
                self.app.call_from_thread(
                    self._set_status, f"Rescanning TARGET ({serial}): {pkg} ({i}/{t})"
                )

            try:
                apps, warnings = await asyncio.to_thread(
                    scan_device, self.adb_path, serial,
                    self.settings.third_party_only, prog_tgt
                )
                if apps:
                    self.session.target.apps = {
                        pkg: appinfo_to_dict(info) for pkg, info in apps.items()
                    }
                    if warnings:
                        self.log_widget.write(
                            f"[yellow]{len(warnings)} warning(s) during TARGET rescan.[/yellow]"
                        )
                else:
                    self.log_widget.write(
                        f"[yellow]TARGET rescan returned 0 packages — "
                        "check connection and USB debugging authorization.[/yellow]"
                    )
            except Exception as exc:
                self.log_widget.write(f"[red]TARGET rescan error: {exc}[/red]")

        self.session_mgr.save(self.session)
        self.busy = False
        self.do_scan()   # rebuild diff from new session data

    # ------------------------------------------------------------------
    # Table rendering
    # ------------------------------------------------------------------

    def refresh_table(self) -> None:
        self.visible_entries = filter_entries(
            self.entries,
            hide_identical=self.settings.hide_identical,
            show_target_only=self.settings.show_target_only,
            search=self.search_term,
        )
        table = self.query_one("#table", DataTable)
        table.clear()
        for e in self.visible_entries:
            table.add_row(*self._row_cells(e), key=e.package)
        self._update_summary()

    def _row_cells(self, e: DiffEntry) -> tuple[str, str, str, str, str]:
        sel = "[x]" if e.selected else "[ ]"
        if e.status is DiffStatus.TARGET_ONLY:
            sel = " - "
        exe = self.session.executions.get(e.package)
        state_badge = ""
        if exe:
            if exe.state is ExecutionState.INSTALL_FAILED:
                state_badge = " [red](install failed)[/red]"
            elif exe.state is ExecutionState.ARCHIVE_FAILED:
                state_badge = " [red](archive failed)[/red]"
            elif exe.state is ExecutionState.CANCELLED:
                state_badge = " [dim](cancelled)[/dim]"
        pkg_display = (
            e.package
            + (" [dim](archived)[/dim]" if e.archived else "")
            + state_badge
        )
        src_ver = e.source.display_version if e.source else "-"
        tgt_ver = e.target.display_version if e.target else "-"
        return (sel, pkg_display, _STATUS_LABEL[e.status], src_ver, tgt_ver)

    def _update_row(self, e: DiffEntry) -> None:
        table = self.query_one("#table", DataTable)
        try:
            row_index = table.get_row_index(e.package)
        except Exception:
            return
        for col_index, value in enumerate(self._row_cells(e)):
            table.update_cell_at((row_index, col_index), value)

    def _entry_under_cursor(self) -> DiffEntry | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        pkg = str(row_key.value)
        return next((e for e in self.entries if e.package == pkg), None)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.search_term = event.value
            self.refresh_table()

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def action_toggle_select(self) -> None:
        if self.busy:
            return
        e = self._entry_under_cursor()
        if not e:
            return
        if e.status is DiffStatus.TARGET_ONLY:
            self.notify(
                "Target-only app: nothing on source to archive/install.",
                severity="information",
            )
            return
        e.selected = not e.selected
        self._update_row(e)
        self._update_summary()

    def action_select_all(self) -> None:
        if self.busy:
            return
        for e in self.visible_entries:
            if e.status is not DiffStatus.TARGET_ONLY:
                e.selected = True
        self.refresh_table()

    def action_select_none(self) -> None:
        if self.busy:
            return
        for e in self.entries:
            e.selected = False
        self.refresh_table()

    def action_toggle_hide_identical(self) -> None:
        self.settings.hide_identical = not self.settings.hide_identical
        self.settings.save()
        self.refresh_table()

    # ------------------------------------------------------------------
    # Settings / navigation
    # ------------------------------------------------------------------

    def action_open_settings(self) -> None:
        if self.busy:
            return

        def _handle(result: Settings | None) -> None:
            if result is None:
                return
            self.settings = result
            self.archive_mgr = ArchiveManager(result.archive_dir)
            self.refresh_table()

        self.app.push_screen(SettingsScreen(self.settings), _handle)

    def action_change_devices(self) -> None:
        if self.busy:
            self.notify("Wait for the current operation to finish first.", severity="warning")
            return
        self.dismiss("change_devices")

    def action_quit_app(self) -> None:
        if self.busy:
            self.notify(
                "An operation is running — wait for it to finish before quitting.",
                severity="warning",
            )
            return
        self.dismiss("quit")

    def action_cancel_batch(self) -> None:
        if not self.busy:
            return
        self._cancel_requested = True
        self.notify("Cancelling after current item finishes…", severity="warning")

    # ------------------------------------------------------------------
    # Batch action entry points
    # ------------------------------------------------------------------

    def _selected_actionable(self) -> list[DiffEntry]:
        return [
            e for e in self.entries
            if e.selected and e.status is not DiffStatus.TARGET_ONLY
        ]

    def action_archive_selected(self) -> None:
        self._start_batch(ActionKind.ARCHIVE)

    def action_install_selected(self) -> None:
        self._start_batch(ActionKind.INSTALL)

    def action_both_selected(self) -> None:
        self._start_batch(ActionKind.ARCHIVE_AND_INSTALL)

    # ------------------------------------------------------------------
    # Disk pre-flight
    # ------------------------------------------------------------------

    def _disk_preflight(
        self, selected: list[DiffEntry], action: ActionKind
    ) -> tuple[bool, str]:
        """Return (space_ok, info_text) for the confirm dialog body."""
        if action not in (ActionKind.ARCHIVE, ActionKind.ARCHIVE_AND_INSTALL):
            return True, ""  # install-only doesn't need local disk space

        archive_path = Path(self.settings.archive_dir)
        try:
            check = archive_path
            visited = 0
            while not check.exists() and check != check.parent and visited < 20:
                check = check.parent
                visited += 1
            if not check.exists():
                return True, "⚠ Could not determine disk usage — path not found."

            usage = shutil.disk_usage(check)
            free_gib = usage.free / (1024 ** 3)
            est_bytes = len(selected) * _EST_BYTES_PER_APP
            est_gib = est_bytes / (1024 ** 3)
            ok = usage.free > est_bytes * 1.2

            if free_gib < 1.0:
                marker = "[red]⚠⚠ CRITICAL[/red]"
            elif free_gib < 5.0:
                marker = "[yellow]⚠ Low space[/yellow]"
            else:
                marker = "[green]OK[/green]"

            info = (
                f"Disk: {check}  —  {free_gib:.1f} GiB free  {marker}\n"
                f"Estimated pull: ~{est_gib:.1f} GiB "
                f"({len(selected)} app(s) × ~30 MiB each)"
            )
            return ok, info

        except OSError as exc:
            return True, f"⚠ Could not read disk usage: {exc}"

    # ------------------------------------------------------------------
    # _start_batch — confirm dialog + launch
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def _start_batch(self, action: ActionKind) -> None:
        if self.busy:
            self.notify("An operation is already running.", severity="warning")
            return
        selected = self._selected_actionable()
        if not selected:
            self.notify(
                "Nothing selected. Press space on a row to select it first.",
                severity="warning",
            )
            return

        # Build confirm body with disk info
        body_lines = [f"{len(selected)} app(s)  —  action: {action.value}", ""]
        for e in selected[:25]:
            note = ""
            if (
                action in (ActionKind.INSTALL, ActionKind.ARCHIVE_AND_INSTALL)
                and e.status is DiffStatus.VERSION_DIFF
                and e.target
            ):
                note = "  [will overwrite differing version; fails safely on signature mismatch]"
            body_lines.append(f"  • {e.package} ({e.status.value}){note}")
        if len(selected) > 25:
            body_lines.append(f"  … and {len(selected) - 25} more")

        space_ok, disk_info = self._disk_preflight(selected, action)
        if disk_info:
            body_lines.append("")
            body_lines.append(disk_info)
        if not space_ok:
            body_lines.append("")
            body_lines.append(
                "[red]Low disk space — the pull may fail mid-way.  "
                "Consider freeing space or changing the archive directory.[/red]"
            )

        title = ("⚠ Low disk — " if not space_ok else "") + f"Confirm: {action.value}"
        body = "\n".join(body_lines)

        confirmed = await self.app.push_screen_wait(
            ConfirmScreen(title, body, danger=(action != ActionKind.ARCHIVE))
        )
        if not confirmed:
            return

        await self._run_batch(action, selected)

    # ------------------------------------------------------------------
    # Device connectivity check (async, non-blocking)
    # ------------------------------------------------------------------

    async def _check_device_connected(self, serial: str) -> bool:
        try:
            devices = await asyncio.to_thread(adb.list_devices, self.adb_path)
            return any(d.serial == serial and d.is_ready for d in devices)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Core batch loop
    # ------------------------------------------------------------------

    async def _run_batch(self, action: ActionKind, selected: list[DiffEntry]) -> None:
        self.busy = True
        self._cancel_requested = False
        total = len(selected)
        ok_count = fail_count = skip_count = park_count = 0
        consecutive_timeouts = 0
        aborted_early = False
        parked_this_run: list[DiffEntry] = []

        src_serial = self.source_serial
        tgt_serial = self.target_serial

        needs_source_globally = action in (ActionKind.ARCHIVE, ActionKind.ARCHIVE_AND_INSTALL)
        needs_target_globally = action in (ActionKind.INSTALL, ActionKind.ARCHIVE_AND_INSTALL)

        for i, e in enumerate(selected, start=1):

            # --- cancellation check (between items, never mid-item) ---
            if self._cancel_requested:
                remaining = total - i + 1
                self.log_widget.write(
                    f"[yellow]Cancelled by user — {remaining} item(s) remain as PENDING.[/yellow]"
                )
                for remaining_entry in selected[i - 1:]:
                    exe = self.session.executions.get(remaining_entry.package)
                    if exe:
                        exe.state = ExecutionState.CANCELLED
                self.session_mgr.save(self.session)
                break

            # --- device presence check ---
            src_ok = not needs_source_globally or await self._check_device_connected(src_serial)
            tgt_ok = not needs_target_globally or await self._check_device_connected(tgt_serial)

            if not src_ok or not tgt_ok:
                missing = []
                if not src_ok:
                    src_name = (self.session.source.model or src_serial) if self.session.source else src_serial
                    missing.append(f"SOURCE ({src_name})")
                if not tgt_ok:
                    tgt_name = (self.session.target.model or tgt_serial) if self.session.target else tgt_serial
                    missing.append(f"TARGET ({tgt_name})")
                self.log_widget.write(
                    f"[yellow]park[/yellow]  {e.package}: "
                    f"waiting for {', '.join(missing)}"
                )
                park_count += 1
                parked_this_run.append(e)
                continue

            self._set_status(f"Running {action.value}: {e.package} ({i}/{total})")

            # --- ensure execution record exists ---
            exe = self.session.executions.get(e.package)
            if exe is None:
                exe = PackageExecution(
                    package=e.package,
                    action=action.value,
                    state=ExecutionState.PENDING,
                )
                self.session.executions[e.package] = exe

            # --- refresh APK paths before archiving (paths may have changed) ---
            if needs_source_globally and e.source:
                try:
                    fresh_paths, path_result = await asyncio.to_thread(
                        adb.get_apk_remote_paths, self.adb_path, src_serial, e.package
                    )
                    if path_result.ok and fresh_paths:
                        e.source.apk_remote_paths = fresh_paths
                    elif not e.source.apk_remote_paths:
                        # Cannot resolve path — skip this item
                        msg = path_result.combined_output or "pm path returned empty"
                        self.log_widget.write(
                            f"[red]FAIL[/red]  {e.package}: "
                            f"cannot resolve APK path on source: {msg}"
                        )
                        exe.state = ExecutionState.ARCHIVE_FAILED
                        self.session_mgr.save(self.session)
                        fail_count += 1
                        e.selected = False
                        self._update_row(e)
                        continue
                except Exception as exc:
                    self.log_widget.write(
                        f"[yellow]warn[/yellow]  {e.package}: "
                        f"path refresh failed ({exc}), using cached paths"
                    )

            # --- write in-flight marker BEFORE the operation ---
            if needs_source_globally:
                exe.state = ExecutionState.ARCHIVING
            elif needs_target_globally:
                exe.state = ExecutionState.INSTALLING
            self.session_mgr.save(self.session)

            # --- execute ---
            results = await asyncio.to_thread(self._do_one, action, e)

            # --- interpret results ---
            timed_out_this_entry = False
            archive_ok = False
            install_ok = False

            for r in results:
                if r.skipped:
                    skip_count += 1
                    self.log_widget.write(f"[dim]skip[/dim]  {r.package}: {r.message}")
                    if r.action == "archive":
                        archive_ok = True    # skipped because already current
                elif r.success:
                    ok_count += 1
                    self.log_widget.write(
                        f"[green]ok[/green]    {r.package} [{r.action}]: {r.message}"
                    )
                    if r.action == "archive":
                        archive_ok = True
                    elif r.action == "install":
                        install_ok = True
                else:
                    fail_count += 1
                    self.log_widget.write(
                        f"[red]FAIL[/red]  {r.package} [{r.action}]: {r.message}"
                    )
                    if "timed out" in r.message.lower() or "timeout" in r.message.lower():
                        timed_out_this_entry = True

            # --- write terminal execution state ---
            if action is ActionKind.ARCHIVE:
                exe.state = ExecutionState.ARCHIVED if archive_ok else ExecutionState.ARCHIVE_FAILED
            elif action is ActionKind.INSTALL:
                exe.state = ExecutionState.INSTALLED if install_ok else ExecutionState.INSTALL_FAILED
            elif action is ActionKind.ARCHIVE_AND_INSTALL:
                if install_ok:
                    exe.state = ExecutionState.INSTALLED
                elif archive_ok:
                    exe.state = ExecutionState.ARCHIVED   # archived but install failed
                else:
                    exe.state = ExecutionState.ARCHIVE_FAILED

            # --- optional cleanup: delete local archive after install ---
            if install_ok and self.settings.cleanup_after_install:
                pkg_archive_dir = Path(self.settings.archive_dir) / e.package
                if pkg_archive_dir.exists():
                    try:
                        shutil.rmtree(pkg_archive_dir)
                        self.log_widget.write(
                            f"[dim]clean[/dim] {e.package}: local archive removed."
                        )
                    except OSError as exc:
                        self.log_widget.write(
                            f"[yellow]warn[/yellow]  {e.package}: "
                            f"cleanup failed: {exc}"
                        )

            self.session_mgr.save(self.session)

            # --- update in-memory diff entry for UI ---
            if archive_ok:
                e.archived = True
            if install_ok and e.source:
                e.target = AppInfo(
                    package=e.package,
                    version_code=e.source.version_code,
                    version_name=e.source.version_name,
                    installer=None,
                )
                e.status = DiffStatus.IDENTICAL

            e.selected = False
            self._update_row(e)

            # --- consecutive-timeout abort guard ---
            consecutive_timeouts = consecutive_timeouts + 1 if timed_out_this_entry else 0
            if consecutive_timeouts >= _MAX_CONSECUTIVE_TIMEOUTS:
                aborted_early = True
                remaining = total - i
                self.log_widget.write(
                    f"[red]Stopping batch:[/red] {_MAX_CONSECUTIVE_TIMEOUTS} consecutive "
                    f"timeouts — device appears disconnected.  "
                    f"{remaining} app(s) not processed."
                )
                break

        # ------------------------------------------------------------------
        # Post-loop: parked items, session completion
        # ------------------------------------------------------------------
        self.busy = False
        self._update_summary()

        if parked_this_run:
            self._parked_items.extend(parked_this_run)
            self._parked_action = action
            src_name = (self.session.source.model or src_serial) if self.session.source else src_serial
            tgt_name = (self.session.target.model or tgt_serial) if self.session.target else tgt_serial
            self.log_widget.write(
                f"[yellow]⚠ {park_count} item(s) parked — required device not connected.[/yellow]\n"
                f"  Connect the device and press [r] to retry.\n"
                f"  SOURCE: {src_name} ({src_serial})   "
                f"TARGET: {tgt_name} ({tgt_serial})"
            )
            self._set_status(
                f"⚠ {park_count} parked — connect device and press [r] to retry"
            )
        else:
            # Check if the full session is now done
            self.session.check_completion()
            if self.session.completed:
                self.session_mgr.save(self.session)
                self.log_widget.write(
                    "[green][b]All selected operations complete![/b][/green]"
                )
                self.session_mgr.delete(self.session.session_id)

        summary = (
            f"Done: {ok_count} ok, {skip_count} skipped, "
            f"{fail_count} failed, {park_count} parked."
        )
        if aborted_early:
            summary += " Batch stopped early — device disconnected."
        self.log_widget.write(f"[b]{summary}[/b]")
        self.notify(
            summary,
            severity="information" if fail_count == 0 else "warning",
        )

    # ------------------------------------------------------------------
    # _do_one — runs in asyncio.to_thread
    # ------------------------------------------------------------------

    def _do_one(self, action: ActionKind, e: DiffEntry):
        if action is ActionKind.ARCHIVE:
            return [archive_package(
                self.adb_path, self.source_serial, e.source, self.archive_mgr
            )]

        if action is ActionKind.INSTALL:
            # Always archive first (skip if already current)
            ar = archive_package(self.adb_path, self.source_serial, e.source, self.archive_mgr)
            if not ar.success:
                return [ar]
            manifest = self.archive_mgr.read_manifest(e.package) or {}
            apk_dir = self.archive_mgr.root / e.package
            local_paths = [str(apk_dir / name) for name in manifest.get("apk_files", [])]
            return [ar, install_package(self.adb_path, self.target_serial, local_paths, e.package)]

        if action is ActionKind.ARCHIVE_AND_INSTALL:
            ar = archive_package(self.adb_path, self.source_serial, e.source, self.archive_mgr)
            if not ar.success:
                return [ar]
            manifest = self.archive_mgr.read_manifest(e.package) or {}
            apk_dir = self.archive_mgr.root / e.package
            local_paths = [str(apk_dir / name) for name in manifest.get("apk_files", [])]
            return [ar, install_package(self.adb_path, self.target_serial, local_paths, e.package)]

        return []

    # ------------------------------------------------------------------
    # Force-reinstall (signature mismatch recovery)
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def action_force_reinstall_selected(self) -> None:
        if self.busy:
            self.notify("An operation is already running.", severity="warning")
            return
        selected = [e for e in self._selected_actionable() if e.target is not None]
        if not selected:
            self.notify(
                "Select app(s) that already exist on the target (e.g. failed with a signature "
                "mismatch) before using force-reinstall.",
                severity="warning",
            )
            return

        body = (
            "This will UNINSTALL the existing app on the TARGET device first, "
            "ERASING that app's local data (saves, logins, settings) on the target, "
            "then install the source's version.\n\n"
            "Apps affected:\n"
            + "\n".join(f"  • {e.package}" for e in selected[:25])
        )
        confirmed = await self.app.push_screen_wait(
            ConfirmScreen(
                "Force reinstall — DATA LOSS on target",
                body,
                danger=True,
                confirm_label="Erase & Install",
            )
        )
        if not confirmed:
            return

        self.busy = True
        for e in selected:
            self._set_status(f"Force reinstalling: {e.package}")
            un = await asyncio.to_thread(
                uninstall_package, self.adb_path, self.target_serial, e.package, False
            )
            color = "green" if un.success else "red"
            label = "ok" if un.success else "FAIL"
            self.log_widget.write(
                f"[{color}]{label}[/] uninstall {e.package}: {un.message}"
            )
            if not un.success:
                continue
            results = await asyncio.to_thread(
                self._do_one, ActionKind.ARCHIVE_AND_INSTALL, e
            )
            for r in results:
                c = "green" if r.success else "red"
                l = "ok" if r.success else "FAIL"
                self.log_widget.write(f"[{c}]{l}[/] {r.package} [{r.action}]: {r.message}")
            if results and results[-1].success and e.source:
                e.target = AppInfo(
                    package=e.package,
                    version_code=e.source.version_code,
                    version_name=e.source.version_name,
                )
                e.status = DiffStatus.IDENTICAL
                exe = self.session.executions.get(e.package)
                if exe:
                    exe.state = ExecutionState.INSTALLED
                    self.session_mgr.save(self.session)
            e.selected = False
            self._update_row(e)

        self.busy = False
        self._update_summary()
