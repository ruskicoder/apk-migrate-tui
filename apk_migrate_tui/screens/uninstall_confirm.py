"""Explicit keyboard-entry verification modal for package uninstalls."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class UninstallConfirmScreen(ModalScreen[bool]):
    """Returns True if the user typed the confirmation phrase and confirmed, False otherwise."""

    DEFAULT_CSS = """
    UninstallConfirmScreen {
        align: center middle;
    }
    #dialog {
        width: 80%;
        max-width: 90;
        height: auto;
        border: heavy $error;
        background: $surface;
        padding: 1 2;
    }
    #body {
        margin: 1 0;
        max-height: 16;
        overflow-y: auto;
    }
    #prompt_label {
        margin-top: 1;
        color: $text;
    }
    #confirmation_input {
        margin-bottom: 1;
    }
    #buttons {
        height: 3;
        align: right middle;
    }
    Button {
        margin-left: 1;
    }
    """

    def __init__(self, title: str, packages: list[str]) -> None:
        super().__init__()
        self._title = title
        self._packages = packages

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"[red][b]{self._title}[/b][/red]")
            body_text = (
                "[yellow]WARNING: Uninstalling will delete the app and ALL its local user data "
                "(saves, settings, logins) from the device![/yellow]\n\n"
                "The following package(s) will be uninstalled:\n"
                + "\n".join(f"  • {pkg}" for pkg in self._packages[:20])
            )
            if len(self._packages) > 20:
                body_text += f"\n  ... and {len(self._packages) - 20} more"

            yield Static(body_text, id="body")
            yield Static(
                "Type the keyword [b][red]uninstall[/red][/b] below to confirm:",
                id="prompt_label",
            )
            yield Input(placeholder="type uninstall here", id="confirmation_input")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm Uninstall", id="confirm", variant="error", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#confirmation_input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "confirmation_input":
            is_valid = event.value.strip().lower() == "uninstall"
            self.query_one("#confirm", Button).disabled = not is_valid

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)
