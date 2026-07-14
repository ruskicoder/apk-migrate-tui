"""Session resume picker — shown at startup when incomplete sessions exist on disk."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Static

from ..session import Session, SessionManager


class SessionResumeScreen(ModalScreen[Session | None]):
    """Lets the user pick an incomplete session to resume or start a new one.

    Dismisses with:
      - ``Session`` object  → resume that session
      - ``None``            → start a brand-new session
    Calls ``app.exit()`` directly for quit (``q``).
    """

    BINDINGS = [
        ("r", "resume", "Resume"),
        ("n", "new_session", "New session"),
        ("d", "delete_selected", "Delete"),
        ("q", "quit_app", "Quit"),
    ]

    DEFAULT_CSS = """
    SessionResumeScreen { align: center middle; }
    #dialog {
        width: 80%;
        max-width: 100;
        height: auto;
        border: heavy $primary;
        background: $surface;
        padding: 1 2;
    }
    DataTable { height: auto; max-height: 14; }
    #subtitle { color: $text-muted; margin: 1 0; }
    #hint { color: $text-muted; margin-top: 1; }
    #buttons { height: 3; align: right middle; margin-top: 1; }
    Button { margin-left: 1; }
    """

    def __init__(self, sessions: list[Session], session_mgr: SessionManager) -> None:
        super().__init__()
        self._sessions: list[Session] = list(sessions)
        self._session_mgr = session_mgr

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[b]Incomplete migration sessions found[/b]")
            yield Static(
                "Select a session to resume, or start a new one.",
                id="subtitle",
            )
            yield DataTable(id="table", cursor_type="row")
            yield Static(
                "[r] Resume   [n] New session   [d] Delete   [q] Quit",
                id="hint",
            )
            with Horizontal(id="buttons"):
                yield Button("New session", id="btn_new", variant="default")
                yield Button("Resume", id="btn_resume", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("Created", "Devices", "Progress")
        self._rebuild_table()
        table.focus()

    def _rebuild_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        for s in self._sessions:
            date = s.created_at[:10] if s.created_at else "?"
            devices = s.display_name
            if s.total_count:
                progress = f"{s.done_count} / {s.total_count} ops done"
            else:
                progress = "scanning phase"
            table.add_row(date, devices, progress, key=s.session_id)

    def _selected_session(self) -> Session | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            session_id = str(row_key.value)
            return next((s for s in self._sessions if s.session_id == session_id), None)
        except Exception:
            return None

    # --- key actions ---

    def action_resume(self) -> None:
        s = self._selected_session()
        if s:
            self.dismiss(s)
        else:
            self.notify("No session selected.", severity="warning")

    def action_new_session(self) -> None:
        self.dismiss(None)

    def action_delete_selected(self) -> None:
        s = self._selected_session()
        if not s:
            self.notify("No session selected.", severity="warning")
            return
        self._session_mgr.delete(s.session_id)
        self._sessions.remove(s)
        table = self.query_one("#table", DataTable)
        try:
            table.remove_row(s.session_id)
        except Exception:
            pass
        self.notify(
            f"Session {s.session_id[:8]}… deleted.",
            severity="information",
        )
        if not self._sessions:
            # All sessions gone — go straight to new session
            self.dismiss(None)

    def action_quit_app(self) -> None:
        self.app.exit()

    # --- button handlers ---

    @on(Button.Pressed, "#btn_resume")
    def _btn_resume(self) -> None:
        self.action_resume()

    @on(Button.Pressed, "#btn_new")
    def _btn_new(self) -> None:
        self.action_new_session()
