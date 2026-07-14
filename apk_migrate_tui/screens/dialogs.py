from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class MessageScreen(ModalScreen[None]):
    """Simple dismissable message/error dialog."""

    DEFAULT_CSS = """
    MessageScreen {
        align: center middle;
    }
    #dialog {
        width: 70%;
        max-width: 90;
        border: heavy $error;
        background: $surface;
        padding: 1 2;
    }
    #dialog.info { border: heavy $primary; }
    #msg { margin-bottom: 1; }
    """

    def __init__(self, title: str, message: str, is_error: bool = True):
        super().__init__()
        self._title = title
        self._message = message
        self._is_error = is_error

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="" if self._is_error else "info"):
            yield Static(f"[b]{self._title}[/b]", id="title")
            yield Static(self._message, id="msg")
            yield Button("OK", id="ok", variant="primary")

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No confirmation. `danger=True` renders it as a destructive-action warning and
    requires the user to explicitly press the Confirm button (Enter does not confirm)."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #dialog {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: heavy $warning;
        background: $surface;
        padding: 1 2;
    }
    #dialog.danger { border: heavy $error; }
    #body { margin-bottom: 1; height: auto; max-height: 20; overflow-y: auto; }
    #buttons { height: 3; align: right middle; }
    Button { margin-left: 1; }
    """

    def __init__(self, title: str, body: str, danger: bool = False, confirm_label: str = "Confirm"):
        super().__init__()
        self._title = title
        self._body = body
        self._danger = danger
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="danger" if self._danger else ""):
            yield Static(f"[b]{self._title}[/b]")
            yield Static(self._body, id="body")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    self._confirm_label, id="confirm", variant="error" if self._danger else "primary"
                )

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)


class RescanChoiceScreen(ModalScreen[str | None]):
    """Asks the user which device(s) to re-scan from a live connection.

    Dismisses with ``"source"``, ``"target"``, ``"both"``, or ``None`` (cancel).
    """

    BINDINGS = [
        ("s", "choose_source", "Rescan Source"),
        ("t", "choose_target", "Rescan Target"),
        ("b", "choose_both", "Rescan Both"),
        ("escape", "cancel_choice", "Cancel"),
    ]

    DEFAULT_CSS = """
    RescanChoiceScreen { align: center middle; }
    #dialog {
        width: 64; height: auto;
        border: heavy $primary; background: $surface; padding: 1 2;
    }
    #body { color: $text-muted; margin: 1 0; }
    #buttons { height: 3; align: center middle; margin-top: 1; }
    Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[b]Rescan from live device[/b]")
            yield Static(
                "Re-query a connected device to update the package list.\n"
                "This replaces the cached scan data and recomputes the diff.",
                id="body",
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="btn_cancel", variant="default")
                yield Button("[s]ource", id="btn_source", variant="primary")
                yield Button("[t]arget", id="btn_target", variant="primary")
                yield Button("[b]oth", id="btn_both", variant="primary")

    def action_choose_source(self) -> None:
        self.dismiss("source")

    def action_choose_target(self) -> None:
        self.dismiss("target")

    def action_choose_both(self) -> None:
        self.dismiss("both")

    def action_cancel_choice(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn_source")
    def _src(self) -> None:
        self.dismiss("source")

    @on(Button.Pressed, "#btn_target")
    def _tgt(self) -> None:
        self.dismiss("target")

    @on(Button.Pressed, "#btn_both")
    def _both(self) -> None:
        self.dismiss("both")

    @on(Button.Pressed, "#btn_cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

