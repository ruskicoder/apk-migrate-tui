from __future__ import annotations

import copy

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from ..models import SourceFilter
from ..settings import Settings


class SettingsScreen(ModalScreen[Settings | None]):
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    #dialog {
        width: 80%; max-width: 100; height: auto;
        border: heavy $primary; background: $surface; padding: 1 2;
    }
    .row { height: 3; margin-bottom: 1; }
    #buttons { height: 3; align: right middle; margin-top: 1; }
    Button { margin-left: 1; }
    Label { margin-top: 1; }
    """

    def __init__(self, settings: Settings):
        super().__init__()
        # work on a copy so Cancel truly discards changes
        self._original = settings
        self.draft = copy.deepcopy(settings)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[b]Settings[/b]")
            yield Checkbox(
                "Hide identical-version apps (ignore-identical toggle)",
                value=self.draft.hide_identical, id="hide_identical"
            )
            yield Checkbox(
                "Show target-only apps (installed on Pixel 10 but not Pixel 6)",
                value=self.draft.show_target_only, id="show_target_only"
            )
            yield Checkbox(
                "Third-party apps only (exclude system apps, pm list -3)",
                value=self.draft.third_party_only, id="third_party_only"
            )
            yield Label("App source filter:")
            yield Select(
                [(f.value, f.value) for f in SourceFilter],
                value=self.draft.source_filter,
                id="source_filter",
                allow_blank=False,
            )
            yield Label("Archive directory:")
            yield Input(value=self.draft.archive_dir, id="archive_dir")
            yield Label("adb path override (leave blank to auto-detect):")
            yield Input(value=self.draft.adb_path or "", id="adb_path")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.draft.hide_identical = self.query_one("#hide_identical", Checkbox).value
        self.draft.show_target_only = self.query_one("#show_target_only", Checkbox).value
        self.draft.third_party_only = self.query_one("#third_party_only", Checkbox).value
        self.draft.source_filter = str(self.query_one("#source_filter", Select).value)
        archive_dir = self.query_one("#archive_dir", Input).value.strip()
        self.draft.archive_dir = archive_dir or self._original.archive_dir
        adb_override = self.query_one("#adb_path", Input).value.strip()
        self.draft.adb_path = adb_override or None
        self.draft.save()
        self.dismiss(self.draft)
