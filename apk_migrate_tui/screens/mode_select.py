"""Mode selection screen shown at startup when beginning a new flow."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static


class ModeSelectScreen(Screen[str | None]):
    """Returns:
      - ``"migrate"``     → Migrate Mode
      - ``"per_device"``  → Per Device Mode
      - ``None``          → User pressed Quit (q)
    """

    BINDINGS = [
        ("1", "select_migrate", "Migrate Apps"),
        ("2", "select_per_device", "Manage Single Device"),
        ("q", "quit_app", "Quit"),
    ]

    DEFAULT_CSS = """
    ModeSelectScreen { align: center middle; }
    #panel {
        width: 80;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #title {
        text-align: center;
        margin-bottom: 1;
    }
    .option {
        margin: 1 0;
        height: auto;
        padding: 1;
        background: $boost;
        border: solid $primary-muted;
    }
    #buttons {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button {
        margin-left: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="panel"):
            yield Static("[b]Select APK Migrate TUI Mode[/b]", id="title")

            with Vertical(classes="option", id="opt_migrate"):
                yield Static("[b]1. Migrate Apps between two devices[/b]")
                yield Static(
                    "Compare installed packages between an old and new device.\n"
                    "Supports both dual-cable (simultaneous) and single-cable (plug-and-unplug) flows.",
                    classes="desc",
                )

            with Vertical(classes="option", id="opt_per_device"):
                yield Static("[b]2. Manage Apps on a single device[/b]")
                yield Static(
                    "Archive APKs from a connected device, uninstall packages,\n"
                    "or install/restore applications directly from your local archive folder.",
                    classes="desc",
                )

            with Horizontal(id="buttons"):
                yield Button("Quit", id="btn_quit", variant="default")
                yield Button("Migrate Apps", id="btn_migrate", variant="primary")
                yield Button("Manage Device", id="btn_per_device", variant="primary")
        yield Footer()

    def action_select_migrate(self) -> None:
        self.dismiss("migrate")

    def action_select_per_device(self) -> None:
        self.dismiss("per_device")

    def action_quit_app(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn_migrate")
    def _btn_migrate(self) -> None:
        self.action_select_migrate()

    @on(Button.Pressed, "#btn_per_device")
    def _btn_per_device(self) -> None:
        self.action_select_per_device()

    @on(Button.Pressed, "#btn_quit")
    def _btn_quit(self) -> None:
        self.action_quit_app()
