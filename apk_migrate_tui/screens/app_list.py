from __future__ import annotations

import asyncio

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
from ..settings import Settings
from .dialogs import ConfirmScreen, MessageScreen
from .settings_screen import SettingsScreen

_STATUS_LABEL = {
    DiffStatus.IDENTICAL: "[green]identical[/green]",
    DiffStatus.VERSION_DIFF: "[yellow]version diff[/yellow]",
    DiffStatus.SOURCE_ONLY: "[cyan]source only[/cyan]",
    DiffStatus.TARGET_ONLY: "[dim]target only[/dim]",
}

_MAX_CONSECUTIVE_TIMEOUTS = 3


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
        ("r", "rescan", "Rescan"),
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

    def __init__(self, adb_path: str, source_serial: str, target_serial: str, settings: Settings):
        super().__init__()
        self.adb_path = adb_path
        self.source_serial = source_serial
        self.target_serial = target_serial
        self.settings = settings
        self.archive_mgr = ArchiveManager(settings.archive_dir)
        self.entries: list[DiffEntry] = []
        self.visible_entries: list[DiffEntry] = []
        self.search_term = ""
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("", id="summary")
        yield Input(placeholder="Search package / label... (press / to focus)", id="search")
        yield DataTable(id="table", cursor_type="row")
        yield RichLog(id="log", wrap=True, markup=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("Sel", "Package", "Status", "Source ver", "Target ver")
        self.log_widget = self.query_one("#log", RichLog)
        self.log_widget.write(
            f"[b]Source:[/b] {self.source_serial}   [b]Target:[/b] {self.target_serial}\n"
            f"[b]Archive dir:[/b] {self.archive_mgr.root}"
        )
        table.focus()
        self.do_scan()

    # ---- status/summary helpers -------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.query_one("#summary", Static).update(text)

    def _update_summary(self) -> None:
        total = len(self.entries)
        selected = sum(1 for e in self.entries if e.selected)
        identical = sum(1 for e in self.entries if e.status is DiffStatus.IDENTICAL)
        diff = sum(1 for e in self.entries if e.status is DiffStatus.VERSION_DIFF)
        src_only = sum(1 for e in self.entries if e.status is DiffStatus.SOURCE_ONLY)
        self._set_status(
            f"{total} apps  |  identical={identical} diff={diff} source-only={src_only}  |  "
            f"selected={selected}  |  hide-identical={'on' if self.settings.hide_identical else 'off'}"
        )

    # ---- scanning -----------------------------------------------------------

    @work(exclusive=True)
    async def do_scan(self) -> None:
        self.busy = True
        self._set_status("Scanning devices...")

        def progress_source(i: int, total: int, pkg: str) -> None:
            self.app.call_from_thread(self._set_status, f"Scanning SOURCE: {pkg} ({i}/{total})")

        def progress_target(i: int, total: int, pkg: str) -> None:
            self.app.call_from_thread(self._set_status, f"Scanning TARGET: {pkg} ({i}/{total})")

        source_apps, source_warnings = await asyncio.to_thread(
            scan_device, self.adb_path, self.source_serial, self.settings.third_party_only, progress_source
        )
        target_apps, target_warnings = await asyncio.to_thread(
            scan_device, self.adb_path, self.target_serial, self.settings.third_party_only, progress_target
        )

        src_filter = SourceFilter(self.settings.source_filter)
        filtered_source = {pkg: info for pkg, info in source_apps.items() if src_filter.matches(info)}

        self.entries = compute_diff(filtered_source, target_apps)
        self.busy = False
        self.refresh_table()

        warnings = source_warnings + target_warnings
        if warnings:
            self.log_widget.write(f"[yellow]{len(warnings)} warning(s) during scan:[/yellow]")
            for w in warnings[:50]:
                self.log_widget.write(f"  [yellow]-[/yellow] {w}")
            if len(warnings) > 50:
                self.log_widget.write(f"  ... and {len(warnings) - 50} more (see log file).")

        if not source_apps and not target_apps:
            self.app.push_screen(
                MessageScreen(
                    "Scan failed",
                    "Could not read package lists from either device. Check the connection and "
                    "USB debugging authorization, then press 'r' to retry.",
                )
            )

    def action_rescan(self) -> None:
        if self.busy:
            self.notify("A scan or operation is already running.", severity="warning")
            return
        self.do_scan()

    # ---- table rendering ------------------------------------------------------

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
            sel = " - "  # not actionable
        pkg_display = e.package + (" [dim](archived)[/dim]" if e.archived else "")
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

    # ---- search -----------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.search_term = event.value
            self.refresh_table()

    # ---- selection ----------------------------------------------------------

    def action_toggle_select(self) -> None:
        if self.busy:
            return
        e = self._entry_under_cursor()
        if not e:
            return
        if e.status is DiffStatus.TARGET_ONLY:
            self.notify("Target-only app: nothing on source to archive/install.", severity="information")
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

    # ---- settings / device change / quit --------------------------------------

    def action_open_settings(self) -> None:
        if self.busy:
            return

        def handle_result(result: Settings | None) -> None:
            if result is None:
                return
            self.settings = result
            self.archive_mgr = ArchiveManager(result.archive_dir)
            self.refresh_table()

        self.app.push_screen(SettingsScreen(self.settings), handle_result)

    def action_change_devices(self) -> None:
        if self.busy:
            self.notify("Wait for the current operation to finish first.", severity="warning")
            return
        self.dismiss("change_devices")

    def action_quit_app(self) -> None:
        if self.busy:
            self.notify("An operation is running - wait for it to finish before quitting.", severity="warning")
            return
        self.dismiss("quit")

    # ---- batch actions ------------------------------------------------------

    def _selected_actionable(self) -> list[DiffEntry]:
        return [e for e in self.entries if e.selected and e.status is not DiffStatus.TARGET_ONLY]

    def action_archive_selected(self) -> None:
        self._start_batch(ActionKind.ARCHIVE)

    def action_install_selected(self) -> None:
        self._start_batch(ActionKind.INSTALL)

    def action_both_selected(self) -> None:
        self._start_batch(ActionKind.ARCHIVE_AND_INSTALL)

    @work(exclusive=True)
    async def _start_batch(self, action: ActionKind) -> None:
        if self.busy:
            self.notify("An operation is already running.", severity="warning")
            return
        selected = self._selected_actionable()
        if not selected:
            self.notify("Nothing selected. Press space on a row to select it first.", severity="warning")
            return

        body_lines = [f"{len(selected)} app(s) - action: {action.value}", ""]
        for e in selected[:25]:
            note = ""
            if action in (ActionKind.INSTALL, ActionKind.ARCHIVE_AND_INSTALL) and e.status is DiffStatus.VERSION_DIFF and e.target:
                note = "  [will overwrite differing version on target; fails safely if signatures differ]"
            body_lines.append(f"  - {e.package} ({e.status.value}){note}")
        if len(selected) > 25:
            body_lines.append(f"  ... and {len(selected) - 25} more")
        body = "\n".join(body_lines)

        confirmed = await self.app.push_screen_wait(
            ConfirmScreen(f"Confirm: {action.value}", body, danger=(action != ActionKind.ARCHIVE))
        )
        if not confirmed:
            return

        await self._run_batch(action, selected)

    async def _run_batch(self, action: ActionKind, selected: list[DiffEntry]) -> None:
        self.busy = True
        total = len(selected)
        ok_count = fail_count = skip_count = 0
        consecutive_timeouts = 0
        aborted_early = False

        for i, e in enumerate(selected, start=1):
            self._set_status(f"Running {action.value}: {e.package} ({i}/{total})")
            results = await asyncio.to_thread(self._do_one, action, e)

            timed_out_this_entry = False
            for r in results:
                if r.skipped:
                    skip_count += 1
                    self.log_widget.write(f"[dim]skip[/dim]  {r.package}: {r.message}")
                elif r.success:
                    ok_count += 1
                    self.log_widget.write(f"[green]ok[/green]    {r.package} [{r.action}]: {r.message}")
                else:
                    fail_count += 1
                    self.log_widget.write(f"[red]FAIL[/red]  {r.package} [{r.action}]: {r.message}")
                    if "timed out" in r.message.lower() or "timeout" in r.message.lower():
                        timed_out_this_entry = True

            # reflect success in-memory so the table updates without a full rescan
            if results and results[0].action == "archive" and results[0].success:
                e.archived = True
            if len(results) > 1 and results[-1].success and results[-1].action == "install" and e.source:
                e.target = AppInfo(
                    package=e.package,
                    version_code=e.source.version_code,
                    version_name=e.source.version_name,
                    installer=None,
                )
                e.status = DiffStatus.IDENTICAL
            e.selected = False
            self._update_row(e)

            consecutive_timeouts = consecutive_timeouts + 1 if timed_out_this_entry else 0
            if consecutive_timeouts >= _MAX_CONSECUTIVE_TIMEOUTS:
                aborted_early = True
                remaining = total - i
                self.log_widget.write(
                    f"[red]Stopping batch:[/red] {_MAX_CONSECUTIVE_TIMEOUTS} consecutive timeouts - a device "
                    f"appears disconnected. {remaining} app(s) not processed."
                )
                break

        self.busy = False
        self._update_summary()
        summary = f"Done: {ok_count} ok, {skip_count} skipped, {fail_count} failed."
        if aborted_early:
            summary += " Batch stopped early - device disconnected."
        self.log_widget.write(f"[b]{summary}[/b]")
        self.notify(summary, severity="information" if fail_count == 0 else "warning")

    def _do_one(self, action: ActionKind, e: DiffEntry):
        if action is ActionKind.ARCHIVE:
            return [archive_package(self.adb_path, self.source_serial, e.source, self.archive_mgr)]
        if action is ActionKind.INSTALL:
            # install requires an archived copy; archive first (skips if already current)
            archive_result = archive_package(self.adb_path, self.source_serial, e.source, self.archive_mgr)
            if not archive_result.success:
                return [archive_result]
            manifest = self.archive_mgr.read_manifest(e.package) or {}
            apk_dir = self.archive_mgr.root / e.package
            local_paths = [str(apk_dir / name) for name in manifest.get("apk_files", [])]
            return [archive_result, install_package(self.adb_path, self.target_serial, local_paths, e.package)]
        if action is ActionKind.ARCHIVE_AND_INSTALL:
            archive_result = archive_package(self.adb_path, self.source_serial, e.source, self.archive_mgr)
            if not archive_result.success:
                return [archive_result]
            manifest = self.archive_mgr.read_manifest(e.package) or {}
            apk_dir = self.archive_mgr.root / e.package
            local_paths = [str(apk_dir / name) for name in manifest.get("apk_files", [])]
            return [archive_result, install_package(self.adb_path, self.target_serial, local_paths, e.package)]
        return []

    # ---- destructive force-reinstall (signature mismatch recovery) --------------

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
            "This will UNINSTALL the existing app on the TARGET device first, ERASING that app's "
            "local data (saves, logins, settings) on the target, then install the source's version.\n\n"
            "Apps affected:\n" + "\n".join(f"  - {e.package}" for e in selected[:25])
        )
        confirmed = await self.app.push_screen_wait(
            ConfirmScreen("Force reinstall - DATA LOSS on target", body, danger=True, confirm_label="Erase & Install")
        )
        if not confirmed:
            return

        self.busy = True
        for e in selected:
            self._set_status(f"Force reinstalling: {e.package}")
            un = await asyncio.to_thread(uninstall_package, self.adb_path, self.target_serial, e.package, False)
            self.log_widget.write(
                f"[{'green' if un.success else 'red'}]{'ok' if un.success else 'FAIL'}[/] "
                f"uninstall {e.package}: {un.message}"
            )
            if not un.success:
                continue
            results = await asyncio.to_thread(self._do_one, ActionKind.ARCHIVE_AND_INSTALL, e)
            for r in results:
                color = "green" if r.success else "red"
                self.log_widget.write(f"[{color}]{'ok' if r.success else 'FAIL'}[/] {r.package} [{r.action}]: {r.message}")
            if results and results[-1].success and e.source:
                e.target = AppInfo(package=e.package, version_code=e.source.version_code, version_name=e.source.version_name)
                e.status = DiffStatus.IDENTICAL
            e.selected = False
            self._update_row(e)
        self.busy = False
        self._update_summary()
