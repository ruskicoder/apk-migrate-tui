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
