"""Device selection screen for single-device management mode."""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from .. import adb


class SingleDeviceSelectScreen(Screen[tuple[str, str | None] | None]):
    """Returns (serial, model) on success, or None if the user quit."""

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("c", "continue_", "Select Device"),
        ("q", "quit_app", "Back / Quit"),
    ]

    DEFAULT_CSS = """
    SingleDeviceSelectScreen { align: center middle; }
    #panel {
        width: 90%; max-width: 110; height: auto;
        border: round $primary; padding: 1 2;
    }
    #status { margin: 1 0; height: auto; }
    #hint { color: $text-muted; margin-top: 1; }
    DataTable { height: auto; max-height: 14; }
    """

    def __init__(self, adb_path: str) -> None:
        super().__init__()
        self.adb_path = adb_path
        self._devices: list[adb.DeviceEntry] = []
        self._scanning: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="panel"):
            yield Static("[b]Select a connected Android device to manage[/b]")
            yield DataTable(id="table", cursor_type="row")
            yield Static("", id="status")
            yield Static(
                "Connect your phone with USB debugging enabled, then accept the RSA prompt.\n"
                "[c] Select Device (Enter)   [r] Refresh   [q] Back / Quit",
                id="hint",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()

    @work(exclusive=True)
    async def action_refresh(self) -> None:
        self._scanning = True
        self.query_one("#status", Static).update("Refreshing device list...")
        try:
            self._devices = await asyncio.to_thread(
                adb.list_devices, self.adb_path
            )
        except Exception as exc:
            self._devices = []
            self.notify(f"Could not list devices: {exc}", severity="error")

        self._scanning = False
        self._rebuild_table()
        self._update_status()

    def _rebuild_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Serial", "State", "Model")
        for d in self._devices:
            state_display = d.state if d.is_ready else f"[red]{d.state}[/red]"
            table.add_row(d.serial, state_display, d.model or "-", key=d.serial)
        table.focus()

    def _update_status(self) -> None:
        status = self.query_one("#status", Static)
        if not self._devices:
            status.update(
                "[yellow]No devices found.[/yellow] Check USB connection, developer options, "
                "and accept the fingerprint prompt."
            )
        else:
            status.update(f"Found {len(self._devices)} connected device(s).")

    def _selected_device(self) -> adb.DeviceEntry | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            serial = str(row_key.value)
            return next((d for d in self._devices if d.serial == serial), None)
        except Exception:
            return None

    def action_continue_(self) -> None:
        if self._scanning:
            return
        dev = self._selected_device()
        if not dev:
            self.notify("Select a device from the list first.", severity="warning")
            return
        if not dev.is_ready:
            self.notify(
                f"Device {dev.serial} is not ready (state: {dev.state}). "
                "Accept RSA prompt or turn on USB debugging.",
                severity="warning",
            )
            return
        self.dismiss((dev.serial, dev.model))

    def action_quit_app(self) -> None:
        self.dismiss(None)
