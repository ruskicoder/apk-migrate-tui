"""Single-device application management screen."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from .. import adb
from ..archive import ArchiveManager
from ..models import ActionKind, AppInfo, SourceFilter
from ..operations import archive_package, disable_package, install_package, scan_device, uninstall_package
from ..settings import Settings
from .dialogs import ConfirmScreen, MessageScreen
from .settings_screen import SettingsScreen
from .uninstall_confirm import UninstallConfirmScreen


@dataclass
class SingleAppEntry:
    package: str
    device_app: AppInfo | None = None
    archive_manifest: dict | None = None
    selected: bool = False

    @property
    def display_version(self) -> str:
        if self.device_app:
            return self.device_app.display_version
        return "[red]not installed[/red]"

    @property
    def archive_status(self) -> str:
        if not self.archive_manifest:
            return "[dim]not archived[/dim]"

        arch_vc = self.archive_manifest.get("version_code")
        arch_vn = self.archive_manifest.get("version_name") or "unknown"
        arch_ver_str = f"{arch_vn} ({arch_vc})" if arch_vc is not None else arch_vn

        if not self.device_app:
            return f"[green]archived ({arch_ver_str})[/green]"

        dev_vc = self.device_app.version_code
        if dev_vc is None or arch_vc is None:
            return f"[yellow]archived ({arch_ver_str})[/yellow]"

        if dev_vc == arch_vc:
            return f"[green]archived (matching: {arch_ver_str})[/green]"
        elif dev_vc > arch_vc:
            return f"[yellow]archived (outdated: {arch_ver_str})[/yellow]"
        else:
            return f"[cyan]archived (newer: {arch_ver_str})[/cyan]"


class SingleDeviceAppScreen(Screen[str]):
    """Dismisses with 'change_device' or 'quit'."""

    BINDINGS = [
        ("space", "toggle_select", "Select"),
        ("a", "select_all", "Select all visible"),
        ("A", "select_none", "Select none"),
        ("s", "archive_selected", "Archive to folder"),
        ("i", "install_selected", "Install from archive"),
        ("d", "uninstall_selected", "Uninstall from device"),
        ("x", "disable_selected", "Disable (freeze) on device"),
        ("comma", "open_settings", "Settings"),
        ("r", "rescan", "Rescan"),
        ("escape", "cancel_batch", "Cancel batch"),
        ("c", "change_device", "Change device"),
        ("slash", "focus_search", "Search"),
        ("q", "quit_app", "Quit"),
    ]

    DEFAULT_CSS = """
    SingleDeviceAppScreen { layout: vertical; }
    #summary { height: 1; padding: 0 1; }
    #search { height: 3; }
    DataTable { height: 1fr; }
    #log { height: 10; border-top: solid $primary; }
    """

    def __init__(
        self,
        adb_path: str,
        serial: str,
        model: str | None,
        settings: Settings,
    ) -> None:
        super().__init__()
        self.adb_path = adb_path
        self.serial = serial
        self.model = model or serial
        self.settings = settings
        self.archive_mgr = ArchiveManager(settings.archive_dir)
        self.entries: list[SingleAppEntry] = []
        self.visible_entries: list[SingleAppEntry] = []
        self.search_term = ""
        self.busy = False
        self._cancel_requested = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("", id="summary")
        yield Input(placeholder="Search package... (press / to focus)", id="search")
        yield DataTable(id="table", cursor_type="row")
        yield RichLog(id="log", wrap=True, markup=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("Sel", "Package", "Device version", "Archive status")

        self.log_widget = self.query_one("#log", RichLog)
        self.log_widget.write(
            f"[b]Managing Device:[/b] {self.model} ({self.serial})\n"
            f"[b]Archive directory:[/b] {self.archive_mgr.root}"
        )
        table.focus()
        self.do_scan()

    def _set_status(self, text: str) -> None:
        self.query_one("#summary", Static).update(text)

    def _update_summary(self) -> None:
        total = len(self.entries)
        selected = sum(1 for e in self.entries if e.selected)
        archived = sum(1 for e in self.entries if e.archive_manifest is not None)
        installed = sum(1 for e in self.entries if e.device_app is not None)
        self._set_status(
            f"{total} total apps  |  installed={installed} archived={archived}  |  "
            f"selected={selected}  |  filters={'on' if self.settings.third_party_only else 'off'}"
        )

    # ------------------------------------------------------------------
    # Scan logic
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def do_scan(self) -> None:
        self.busy = True
        self._set_status("Scanning device apps and archive directory...")

        # 1. Scan live device apps
        def progress(i: int, total: int, pkg: str) -> None:
            self.app.call_from_thread(self._set_status, f"Scanning device: {pkg} ({i}/{total})")

        try:
            device_apps, warnings = await asyncio.to_thread(
                scan_device,
                self.adb_path,
                self.serial,
                self.settings.third_party_only,
                progress,
            )
        except Exception as exc:
            device_apps = {}
            warnings = [f"Device scan error: {exc}"]

        # 2. Scan local archive manifests
        archive_manifests: dict[str, dict] = {}
        archive_root = Path(self.settings.archive_dir)
        if archive_root.exists():
            try:
                for sub in archive_root.iterdir():
                    if sub.is_dir() and not sub.name.startswith("."):
                        manifest = self.archive_mgr.read_manifest(sub.name)
                        if manifest:
                            archive_manifests[sub.name] = manifest
            except Exception as exc:
                warnings.append(f"Archive scan error: {exc}")

        # 3. Apply settings filters
        src_filter = SourceFilter(self.settings.source_filter)
        all_packages = set(device_apps.keys()) | set(archive_manifests.keys())

        new_entries: list[SingleAppEntry] = []
        for pkg in all_packages:
            dev_app = device_apps.get(pkg)
            arch_man = archive_manifests.get(pkg)

            # Apply installer/foss filters if applicable
            if dev_app and not src_filter.matches(dev_app):
                continue
            if not dev_app and arch_man:
                # If only in archive, build a temporary AppInfo to test filters
                temp_info = AppInfo(
                    package=pkg,
                    installer=arch_man.get("installer"),
                    version_code=arch_man.get("version_code"),
                    version_name=arch_man.get("version_name"),
                )
                if not src_filter.matches(temp_info):
                    continue

            new_entries.append(
                SingleAppEntry(package=pkg, device_app=dev_app, archive_manifest=arch_man)
            )

        self.entries = sorted(new_entries, key=lambda e: e.package)
        self.busy = False
        self.refresh_table()

        if warnings:
            self.log_widget.write("[yellow]Warnings during scan:[/yellow]")
            for w in warnings:
                self.log_widget.write(f"  - {w}")

        if not self.entries:
            self.log_widget.write("[yellow]No apps matched current settings/filters.[/yellow]")

    def action_rescan(self) -> None:
        if self.busy:
            self.notify("A scan or operation is already running.", severity="warning")
            return
        self.do_scan()

    # ------------------------------------------------------------------
    # Table rendering
    # ------------------------------------------------------------------

    def refresh_table(self) -> None:
        # Search filter
        term = self.search_term.strip().lower()
        if term:
            self.visible_entries = [
                e for e in self.entries
                if term in e.package.lower()
            ]
        else:
            self.visible_entries = list(self.entries)

        # Hide identical check
        if self.settings.hide_identical:
            filtered = []
            for e in self.visible_entries:
                if e.device_app and e.archive_manifest:
                    dev_vc = e.device_app.version_code
                    arch_vc = e.archive_manifest.get("version_code")
                    if dev_vc is not None and arch_vc is not None and dev_vc == arch_vc:
                        continue
                filtered.append(e)
            self.visible_entries = filtered

        table = self.query_one("#table", DataTable)
        table.clear()
        for e in self.visible_entries:
            table.add_row(*self._row_cells(e), key=e.package)
        self._update_summary()

    def _row_cells(self, e: SingleAppEntry) -> tuple[str, str, str, str]:
        sel = "[x]" if e.selected else "[ ]"
        return (sel, e.package, e.display_version, e.archive_status)

    def _update_row(self, e: SingleAppEntry) -> None:
        table = self.query_one("#table", DataTable)
        try:
            row_index = table.get_row_index(e.package)
        except Exception:
            return
        for col_index, value in enumerate(self._row_cells(e)):
            table.update_cell_at((row_index, col_index), value)

    def _entry_under_cursor(self) -> SingleAppEntry | None:
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
        e.selected = not e.selected
        self._update_row(e)
        self._update_summary()

    def action_select_all(self) -> None:
        if self.busy:
            return
        for e in self.visible_entries:
            e.selected = True
        self.refresh_table()

    def action_select_none(self) -> None:
        if self.busy:
            return
        for e in self.entries:
            e.selected = False
        self.refresh_table()

    # ------------------------------------------------------------------
    # Settings & navigation
    # ------------------------------------------------------------------

    def action_open_settings(self) -> None:
        if self.busy:
            return

        def _handle(result: Settings | None) -> None:
            if result is None:
                return
            self.settings = result
            self.archive_mgr = ArchiveManager(result.archive_dir)
            self.do_scan()

        self.app.push_screen(SettingsScreen(self.settings), _handle)

    def action_change_device(self) -> None:
        if self.busy:
            self.notify("Wait for the current operation to finish first.", severity="warning")
            return
        self.dismiss("change_device")

    def action_quit_app(self) -> None:
        if self.busy:
            self.notify("Wait for the current operation to finish first.", severity="warning")
            return
        self.dismiss("quit")

    def action_cancel_batch(self) -> None:
        if not self.busy:
            return
        self._cancel_requested = True
        self.notify("Cancelling after current item finishes...", severity="warning")

    # ------------------------------------------------------------------
    # Batch actions
    # ------------------------------------------------------------------

    def _selected_actionable(self) -> list[SingleAppEntry]:
        return [e for e in self.entries if e.selected]

    # ---- Action 1: Archive ----
    def action_archive_selected(self) -> None:
        selected = [e for e in self._selected_actionable() if e.device_app is not None]
        if not selected:
            self.notify("Select installed apps to archive first.", severity="warning")
            return
        self._start_archive_batch(selected)

    @work(exclusive=True)
    async def _start_archive_batch(self, selected: list[SingleAppEntry]) -> None:
        self.busy = True
        self._cancel_requested = False
        total = len(selected)

        self.log_widget.write(f"[b]Starting batch archive of {total} package(s)[/b]")
        ok_count = fail_count = 0

        for i, e in enumerate(selected, start=1):
            if self._cancel_requested:
                self.log_widget.write("[yellow]Batch archive cancelled by user.[/yellow]")
                break

            self._set_status(f"Archiving: {e.package} ({i}/{total})")

            # Verify connection
            devices = await asyncio.to_thread(adb.list_devices, self.adb_path)
            if not any(d.serial == self.serial and d.is_ready for d in devices):
                self.log_widget.write(f"[red]FAIL[/red]  {e.package}: Device disconnected.")
                fail_count += 1
                continue

            # fresh pm path
            try:
                fresh_paths, path_result = await asyncio.to_thread(
                    adb.get_apk_remote_paths, self.adb_path, self.serial, e.package
                )
                if path_result.ok and fresh_paths and e.device_app:
                    e.device_app.apk_remote_paths = fresh_paths
            except Exception:
                pass

            result = await asyncio.to_thread(
                archive_package, self.adb_path, self.serial, e.device_app, self.archive_mgr
            )

            if result.success:
                ok_count += 1
                self.log_widget.write(f"[green]ok[/green]    {e.package}: Archived successfully.")
                e.archive_manifest = self.archive_mgr.read_manifest(e.package)
            else:
                fail_count += 1
                self.log_widget.write(f"[red]FAIL[/red]  {e.package}: {result.message}")

            e.selected = False
            self._update_row(e)

        self.busy = False
        self._update_summary()
        summary = f"Archive done: {ok_count} ok, {fail_count} failed."
        self.log_widget.write(f"[b]{summary}[/b]")
        self.notify(summary, severity="information" if fail_count == 0 else "warning")

    # ---- Action 2: Install ----
    def action_install_selected(self) -> None:
        selected = [e for e in self._selected_actionable() if e.archive_manifest is not None]
        if not selected:
            self.notify("Select archived apps to install first.", severity="warning")
            return
        self._start_install_batch(selected)

    @work(exclusive=True)
    async def _start_install_batch(self, selected: list[SingleAppEntry]) -> None:
        self.busy = True
        self._cancel_requested = False
        total = len(selected)

        self.log_widget.write(f"[b]Starting batch install of {total} package(s)[/b]")
        ok_count = fail_count = 0

        for i, e in enumerate(selected, start=1):
            if self._cancel_requested:
                self.log_widget.write("[yellow]Batch install cancelled by user.[/yellow]")
                break

            self._set_status(f"Installing: {e.package} ({i}/{total})")

            # Verify connection
            devices = await asyncio.to_thread(adb.list_devices, self.adb_path)
            if not any(d.serial == self.serial and d.is_ready for d in devices):
                self.log_widget.write(f"[red]FAIL[/red]  {e.package}: Device disconnected.")
                fail_count += 1
                continue

            manifest = e.archive_manifest or {}
            apk_dir = self.archive_mgr.root / e.package
            local_paths = [str(apk_dir / name) for name in manifest.get("apk_files", [])]

            result = await asyncio.to_thread(
                install_package, self.adb_path, self.serial, local_paths, e.package
            )

            if result.success:
                ok_count += 1
                self.log_widget.write(f"[green]ok[/green]    {e.package}: Installed successfully.")
                # update row in-memory
                e.device_app = AppInfo(
                    package=e.package,
                    version_code=manifest.get("version_code"),
                    version_name=manifest.get("version_name"),
                )
            else:
                fail_count += 1
                self.log_widget.write(f"[red]FAIL[/red]  {e.package}: {result.message}")

            e.selected = False
            self._update_row(e)

        self.busy = False
        self._update_summary()
        summary = f"Install done: {ok_count} ok, {fail_count} failed."
        self.log_widget.write(f"[b]{summary}[/b]")
        self.notify(summary, severity="information" if fail_count == 0 else "warning")

    # ---- Action 3: Uninstall ----
    def action_uninstall_selected(self) -> None:
        selected = [e for e in self._selected_actionable() if e.device_app is not None]
        if not selected:
            self.notify("Select installed apps to uninstall first.", severity="warning")
            return

        pkgs = [e.package for e in selected]

        def _confirm_and_run(confirmed: bool | None) -> None:
            if not confirmed:
                return
            self._run_uninstall_batch(selected)

        self.app.push_screen(
            UninstallConfirmScreen("Danger: Confirm Batch Uninstall", pkgs),
            _confirm_and_run,
        )

    @work(exclusive=True)
    async def _run_uninstall_batch(self, selected: list[SingleAppEntry]) -> None:
        self.busy = True
        self._cancel_requested = False
        total = len(selected)

        self.log_widget.write(f"[b]Starting batch uninstall of {total} package(s)[/b]")
        ok_count = fail_count = 0

        for i, e in enumerate(selected, start=1):
            if self._cancel_requested:
                self.log_widget.write("[yellow]Batch uninstall cancelled by user.[/yellow]")
                break

            self._set_status(f"Uninstalling: {e.package} ({i}/{total})")

            # Verify connection
            devices = await asyncio.to_thread(adb.list_devices, self.adb_path)
            if not any(d.serial == self.serial and d.is_ready for d in devices):
                self.log_widget.write(f"[red]FAIL[/red]  {e.package}: Device disconnected.")
                fail_count += 1
                continue

            result = await asyncio.to_thread(
                uninstall_package, self.adb_path, self.serial, e.package, False
            )

            if result.success:
                ok_count += 1
                if result.action == "removed":
                    label = "[green]removed[/green]"
                    detail = "Fully removed from device."
                else:
                    # hidden (system) — app no longer visible/runnable for this user
                    label = "[yellow]hidden — system app[/yellow]"
                    detail = result.message
                self.log_widget.write(f"[green]ok[/green]    {e.package}: {label}  {detail}")
                e.device_app = None

                # Clean up local archive if settings say so
                if self.settings.cleanup_after_install:
                    pkg_archive = self.archive_mgr.root / e.package
                    if pkg_archive.exists():
                        try:
                            shutil.rmtree(pkg_archive)
                            e.archive_manifest = None
                            self.log_widget.write(f"[dim]clean[/dim] {e.package}: Archive removed.")
                        except Exception as exc:
                            self.log_widget.write(f"[yellow]warn[/yellow] {e.package}: Cleanup failed: {exc}")
            else:
                fail_count += 1
                self.log_widget.write(
                    f"[red]FAIL[/red]  {e.package}: [red]failed[/red]  {result.message}"
                )

            e.selected = False
            self._update_row(e)

        self.busy = False
        self._update_summary()
        summary = f"Uninstall done: {ok_count} ok, {fail_count} failed."
        self.log_widget.write(f"[b]{summary}[/b]")
        self.notify(summary, severity="information" if fail_count == 0 else "warning")

    # ---- Action 4: Disable (freeze) ----
    def action_disable_selected(self) -> None:
        selected = [e for e in self._selected_actionable() if e.device_app is not None]
        if not selected:
            self.notify("Select installed apps to disable first.", severity="warning")
            return

        pkgs = [e.package for e in selected]

        def _confirm_and_run(confirmed: bool | None) -> None:
            if not confirmed:
                return
            self._run_disable_batch(selected)

        self.app.push_screen(
            UninstallConfirmScreen(
                "Confirm Batch Disable (Freeze)",
                pkgs,
                confirm_keyword="disable",
                warning_text=(
                    "Disable will FREEZE these apps. They cannot launch or receive updates.\n"
                    "This is NOT a full uninstall. The APK remains on the device.\n"
                    "Restore with: adb shell pm enable <package>"
                ),
            ),
            _confirm_and_run,
        )

    @work(exclusive=True)
    async def _run_disable_batch(self, selected: list[SingleAppEntry]) -> None:
        self.busy = True
        self._cancel_requested = False
        total = len(selected)

        self.log_widget.write(f"[b]Starting batch disable of {total} package(s)[/b]")
        ok_count = fail_count = 0

        for i, e in enumerate(selected, start=1):
            if self._cancel_requested:
                self.log_widget.write("[yellow]Batch disable cancelled by user.[/yellow]")
                break

            self._set_status(f"Disabling: {e.package} ({i}/{total})")

            # Verify connection
            devices = await asyncio.to_thread(adb.list_devices, self.adb_path)
            if not any(d.serial == self.serial and d.is_ready for d in devices):
                self.log_widget.write(f"[red]FAIL[/red]  {e.package}: Device disconnected.")
                fail_count += 1
                continue

            result = await asyncio.to_thread(
                disable_package, self.adb_path, self.serial, e.package
            )

            if result.success:
                ok_count += 1
                self.log_widget.write(
                    f"[green]ok[/green]    {e.package}: [yellow]disabled — system app[/yellow]  {result.message}"
                )
                # App is frozen but still technically on device — clear device_app to
                # reflect it can no longer run. A rescan will also confirm its status.
                e.device_app = None
            else:
                fail_count += 1
                self.log_widget.write(f"[red]FAIL[/red]  {e.package}: {result.message}")

            e.selected = False
            self._update_row(e)

        self.busy = False
        self._update_summary()
        summary = f"Disable done: {ok_count} ok, {fail_count} failed."
        self.log_widget.write(f"[b]{summary}[/b]")
        self.notify(summary, severity="information" if fail_count == 0 else "warning")
