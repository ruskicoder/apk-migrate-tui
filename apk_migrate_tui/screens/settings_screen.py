"""Settings modal screen.

Changes in this version:
- Live disk-usage display updates as the user types the archive_dir path.
- ``cleanup_after_install`` checkbox — off by default.
- ``connection_mode`` selector — dual / single cable.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
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
    #disk_info { color: $text-muted; height: 1; margin-bottom: 1; }
    #disk_info.ok     { color: $success; }
    #disk_info.warn   { color: $warning; }
    #disk_info.danger { color: $error; }
    """

    def __init__(self, settings: Settings):
        super().__init__()
        # Work on a deep copy so Cancel truly discards all changes.
        self._original = settings
        self.draft = copy.deepcopy(settings)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[b]Settings[/b]")

            # ---- visibility toggles ----
            yield Checkbox(
                "Hide identical-version apps",
                value=self.draft.hide_identical, id="hide_identical",
            )
            yield Checkbox(
                "Show target-only apps (installed on new device but not old one)",
                value=self.draft.show_target_only, id="show_target_only",
            )
            yield Checkbox(
                "Third-party apps only (exclude system apps, pm list -3)",
                value=self.draft.third_party_only, id="third_party_only",
            )
            yield Checkbox(
                "Delete local archive copy after successful install",
                value=self.draft.cleanup_after_install, id="cleanup_after_install",
            )

            # ---- source filter ----
            yield Label("App source filter:")
            yield Select(
                [(f.value, f.value) for f in SourceFilter],
                value=self.draft.source_filter,
                id="source_filter",
                allow_blank=False,
            )

            # ---- cable mode ----
            yield Label("Cable connection mode:")
            yield Select(
                [("Dual-cable (both connected simultaneously)", "dual"),
                 ("Single-cable (connect one at a time)", "single")],
                value=self.draft.connection_mode,
                id="connection_mode",
                allow_blank=False,
            )

            # ---- archive dir with live disk info ----
            yield Label("Archive directory:")
            yield Input(value=self.draft.archive_dir, id="archive_dir")
            yield Static("", id="disk_info")

            # ---- adb override ----
            yield Label("adb path override (leave blank to auto-detect):")
            yield Input(value=self.draft.adb_path or "", id="adb_path")

            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        # Show initial disk info for the current archive dir
        self._update_disk_info(self.draft.archive_dir)

    # ------------------------------------------------------------------
    # Live disk usage display
    # ------------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "archive_dir":
            self._update_disk_info(event.value)

    def _update_disk_info(self, path_str: str) -> None:
        widget = self.query_one("#disk_info", Static)
        if not path_str.strip():
            widget.update("")
            widget.remove_class("ok", "warn", "danger")
            return

        try:
            # Walk up to the nearest existing ancestor directory
            check = Path(path_str.strip())
            visited = 0
            while not check.exists() and check != check.parent and visited < 20:
                check = check.parent
                visited += 1

            if not check.exists():
                widget.update("⚠ Path not found")
                widget.remove_class("ok", "danger")
                widget.add_class("warn")
                return

            usage = shutil.disk_usage(check)
            free_gib = usage.free / (1024 ** 3)

            if free_gib < 1.0:
                css_class, icon = "danger", "⚠⚠"
            elif free_gib < 5.0:
                css_class, icon = "warn", "⚠"
            else:
                css_class, icon = "ok", "📂"

            widget.update(f"{icon} {check}  —  {free_gib:.1f} GiB free")
            widget.remove_class("ok", "warn", "danger")
            widget.add_class(css_class)

        except OSError as exc:
            widget.update(f"⚠ Cannot read disk usage: {exc}")
            widget.remove_class("ok", "danger")
            widget.add_class("warn")

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.draft.hide_identical = self.query_one("#hide_identical", Checkbox).value
        self.draft.show_target_only = self.query_one("#show_target_only", Checkbox).value
        self.draft.third_party_only = self.query_one("#third_party_only", Checkbox).value
        self.draft.cleanup_after_install = self.query_one("#cleanup_after_install", Checkbox).value

        src_filter_val = self.query_one("#source_filter", Select).value
        self.draft.source_filter = str(src_filter_val)

        conn_mode_val = self.query_one("#connection_mode", Select).value
        self.draft.connection_mode = str(conn_mode_val)

        archive_dir = self.query_one("#archive_dir", Input).value.strip()
        self.draft.archive_dir = archive_dir or self._original.archive_dir

        adb_override = self.query_one("#adb_path", Input).value.strip()
        self.draft.adb_path = adb_override or None

        self.draft.save()
        self.dismiss(self.draft)
