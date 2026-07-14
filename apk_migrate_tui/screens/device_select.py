from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static, Button

from .. import adb


class DeviceSelectScreen(Screen[tuple[str, str] | None]):
    """Result is (source_serial, target_serial), or None if the user quit."""

    BINDINGS = [
        ("r", "refresh", "Refresh devices"),
        ("s", "mark_source", "Mark as SOURCE (Pixel 6)"),
        ("t", "mark_target", "Mark as TARGET (Pixel 10)"),
        ("c", "continue_", "Continue"),
        ("q", "quit_app", "Quit"),
    ]

    DEFAULT_CSS = """
    DeviceSelectScreen { align: center middle; }
    #panel { width: 90%; max-width: 110; height: auto; border: round $primary; padding: 1 2; }
    #status { margin: 1 0; height: auto; }
    #hint { color: $text-muted; margin-top: 1; }
    DataTable { height: auto; max-height: 14; }
    """

    def __init__(self, adb_path: str):
        super().__init__()
        self.adb_path = adb_path
        self.source_serial: str | None = None
        self.target_serial: str | None = None
        self._devices: list[adb.DeviceEntry] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="panel"):
            yield Static("[b]Select source (Pixel 6) and target (Pixel 10) devices[/b]")
            yield DataTable(id="table", cursor_type="row")
            yield Static("", id="status")
            yield Static(
                "Connect both phones via USB (or one via USB, one via a computer relay) with "
                "USB debugging enabled, then accept the RSA authorization prompt on each device.\n"
                "[s] mark selected row as SOURCE   [t] mark as TARGET   [r] refresh   [c] continue   [q] quit",
                id="hint",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self._devices = adb.list_devices(self.adb_path)
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Serial", "State", "Model", "Role")
        for d in self._devices:
            role = ""
            if d.serial == self.source_serial:
                role = "SOURCE"
            elif d.serial == self.target_serial:
                role = "TARGET"
            state_display = d.state if d.is_ready else f"[red]{d.state}[/red]"
            table.add_row(d.serial, state_display, d.model or "-", role, key=d.serial)
        self._update_status()

    def _update_status(self) -> None:
        status = self.query_one("#status", Static)
        if not self._devices:
            status.update(
                "[yellow]No devices found.[/yellow] Check USB connection, cable (data-capable, "
                "not charge-only), and that 'adb devices' authorization was accepted on-device."
            )
            return
        lines = []
        lines.append(f"Source: {self.source_serial or '(not set)'}")
        lines.append(f"Target: {self.target_serial or '(not set)'}")
        status.update("\n".join(lines))

    def _selected_serial(self) -> str | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            return str(row_key.value)
        except Exception:
            return None

    def _device_by_serial(self, serial: str) -> adb.DeviceEntry | None:
        return next((d for d in self._devices if d.serial == serial), None)

    def action_mark_source(self) -> None:
        serial = self._selected_serial()
        if not serial:
            return
        dev = self._device_by_serial(serial)
        if not dev or not dev.is_ready:
            self.notify(f"Device {serial} is not ready (state: {dev.state if dev else 'unknown'}).", severity="warning")
            return
        if serial == self.target_serial:
            self.notify("That device is already marked as TARGET. Pick a different device.", severity="warning")
            return
        self.source_serial = serial
        self.action_refresh()

    def action_mark_target(self) -> None:
        serial = self._selected_serial()
        if not serial:
            return
        dev = self._device_by_serial(serial)
        if not dev or not dev.is_ready:
            self.notify(f"Device {serial} is not ready (state: {dev.state if dev else 'unknown'}).", severity="warning")
            return
        if serial == self.source_serial:
            self.notify("That device is already marked as SOURCE. Pick a different device.", severity="warning")
            return
        self.target_serial = serial
        self.action_refresh()

    def action_continue_(self) -> None:
        if not self.source_serial or not self.target_serial:
            self.notify("Mark both a SOURCE and a TARGET device first (s / t).", severity="warning")
            return
        if self.source_serial == self.target_serial:
            self.notify("Source and target must be different devices.", severity="error")
            return
        self.dismiss((self.source_serial, self.target_serial))

    def action_quit_app(self) -> None:
        self.dismiss(None)
