"""Adaptive device selection screen supporting both dual-cable and single-cable workflows.

Dual-cable mode
---------------
Both source and target are simultaneously connected.  The user marks each device
(triggering an immediate background scan), and presses [c] only when both scans
have completed *and* both serials are still physically connected.

Single-cable mode
-----------------
Only one cable is available.  The user marks the source (scan starts immediately),
unplugs it, plugs in the target, marks the target (scan starts), and then presses
[c].  The continue gate only requires that both scan *results* exist — it does not
require both devices to be live simultaneously.

Both modes use the same scan-on-mark logic; the only difference is the [c] gate.
The mode can be toggled with [m] at any time (including mid-session) and is
persisted to settings.

Session persistence
-------------------
As soon as a device scan completes, the DeviceRecord (serial, model, apps dict) is
written to the session file so the progress survives app restarts.  If the user
quits after scanning the source but before scanning the target, the session resume
screen will show the session; re-opening DeviceSelectScreen will show the source as
cached so only the target needs to be plugged in.

Error guards
------------
- Device not ready (state ≠ "device"): blocked, shown in status.
- Same serial for both roles: hard block.
- Different serial from session-cached record: mismatch dialog → rescan as new /
  wait for original.
- Scan yields 0 packages: reported as failure, session record not saved.
- Scan fails entirely: error notification, session record cleared for that role.
"""

from __future__ import annotations

import asyncio
import time

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from .. import adb
from ..operations import scan_device
from ..session import DeviceRecord, Session, SessionManager, appinfo_to_dict
from ..settings import Settings
from .dialogs import ConfirmScreen


class DeviceSelectScreen(Screen["Session | None"]):
    """Returns the populated Session on success, None if the user quit."""

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("s", "mark_source", "Mark SOURCE"),
        ("t", "mark_target", "Mark TARGET"),
        ("m", "toggle_mode", "Toggle cable mode"),
        ("c", "continue_", "Continue"),
        ("q", "quit_app", "Quit"),
    ]

    DEFAULT_CSS = """
    DeviceSelectScreen { align: center middle; }
    #panel {
        width: 92%; max-width: 120; height: auto;
        border: round $primary; padding: 1 2;
    }
    #mode_bar {
        height: 1; background: $boost; padding: 0 1; margin-bottom: 1;
        color: $text;
    }
    #status { margin: 1 0; height: auto; }
    #hint { color: $text-muted; margin-top: 1; }
    DataTable { height: auto; max-height: 12; }
    """

    def __init__(
        self,
        adb_path: str,
        session: Session,
        session_mgr: SessionManager,
        settings: Settings,
    ) -> None:
        super().__init__()
        self.adb_path = adb_path
        self.session = session
        self.session_mgr = session_mgr
        self.settings = settings
        self._devices: list[adb.DeviceEntry] = []
        self._scanning: bool = False
        self._scan_role: str | None = None   # "source" or "target" while a scan runs
        self._scan_progress: str = ""

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="panel"):
            yield Static("", id="mode_bar")
            yield Static("[b]Select source (old device) and target (new device)[/b]")
            yield DataTable(id="table", cursor_type="row")
            yield Static("", id="status")
            yield Static("", id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self._update_mode_bar()
        self._update_hint()
        self.action_refresh()

    # ------------------------------------------------------------------
    # Mode toggle
    # ------------------------------------------------------------------

    @property
    def _single_cable_mode(self) -> bool:
        return self.settings.connection_mode == "single"

    def _update_mode_bar(self) -> None:
        if self._single_cable_mode:
            text = "🔁  Single-cable mode — scan each device independently   [m] switch to dual"
        else:
            text = "⚡  Dual-cable mode — both devices must be connected   [m] switch to single"
        self.query_one("#mode_bar", Static).update(text)

    def _update_hint(self) -> None:
        if self._single_cable_mode:
            hint = (
                "Connect SOURCE device → press [s] to scan. "
                "Unplug it, connect TARGET → press [t] to scan. "
                "Press [c] once both are scanned (cached data is fine).\n"
                "[s] mark SOURCE  [t] mark TARGET  [r] refresh  "
                "[m] toggle mode  [c] continue  [q] quit"
            )
        else:
            hint = (
                "Connect BOTH devices simultaneously with USB debugging enabled.\n"
                "[s] mark SOURCE  [t] mark TARGET  [r] refresh  "
                "[m] toggle mode  [c] continue  [q] quit"
            )
        self.query_one("#hint", Static).update(hint)

    def action_toggle_mode(self) -> None:
        if self._scanning:
            self.notify("Cannot change mode while a scan is running.", severity="warning")
            return
        self.settings.connection_mode = "single" if not self._single_cable_mode else "dual"
        self.settings.save()
        self._update_mode_bar()
        self._update_hint()
        self._update_status()
        self.notify(
            f"Switched to {'single' if self._single_cable_mode else 'dual'}-cable mode.",
            severity="information",
        )

    # ------------------------------------------------------------------
    # Device list refresh + table rebuild
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        if self._scanning:
            return  # don't clobber scan progress display
        try:
            self._devices = adb.list_devices(self.adb_path)
        except Exception as exc:
            self._devices = []
            self.notify(f"Could not list devices: {exc}", severity="error")
        self._rebuild_table()
        self._update_status()

    def _rebuild_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns("Serial", "State", "Model", "Role", "Scan status")

        live_serials: set[str] = {d.serial for d in self._devices}

        # Live devices
        for d in self._devices:
            role = self._role_label_for(d.serial)
            scan_st = self._scan_status_for(d.serial)
            state_disp = d.state if d.is_ready else f"[red]{d.state}[/red]"
            table.add_row(
                d.serial, state_disp, d.model or "-", role, scan_st,
                key=d.serial,
            )

        # Cached-but-offline devices (important in single-cable mode)
        for record, role_label in (
            (self.session.source, "SOURCE"),
            (self.session.target, "TARGET"),
        ):
            if record and record.serial not in live_serials and record.apps:
                count = len(record.apps)
                name = record.model or "-"
                table.add_row(
                    record.serial,
                    "[dim]offline[/dim]",
                    name,
                    role_label,
                    f"[green]✓ cached ({count} apps)[/green]",
                    key=f"cached_{record.serial}",
                )

    def _role_label_for(self, serial: str) -> str:
        if self.session.source and serial == self.session.source.serial:
            return "SOURCE"
        if self.session.target and serial == self.session.target.serial:
            return "TARGET"
        return ""

    def _scan_status_for(self, serial: str) -> str:
        # Actively scanning?
        if self._scanning and self._scan_role:
            active = None
            if self._scan_role == "source" and self.session.source:
                active = self.session.source.serial
            elif self._scan_role == "target" and self.session.target:
                active = self.session.target.serial
            if active == serial:
                prog = f" {self._scan_progress}" if self._scan_progress else ""
                return f"[yellow]scanning…{prog}[/yellow]"

        # Cached from session?
        for record in (self.session.source, self.session.target):
            if record and serial == record.serial and record.apps:
                count = len(record.apps)
                return f"[green]✓ {count} apps[/green]"

        return "[dim](not scanned)[/dim]"

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------

    def _update_status(self) -> None:
        src_line = self._device_status_line("source", self.session.source)
        tgt_line = self._device_status_line("target", self.session.target)
        if not self._devices:
            conn_warning = (
                "\n[yellow]No devices detected.[/yellow]  "
                "Check USB cable (must be data-capable), USB debugging enabled on device, "
                "and accept the RSA fingerprint prompt."
            )
        else:
            conn_warning = ""
        self.query_one("#status", Static).update(
            f"{src_line}\n{tgt_line}{conn_warning}"
        )

    def _device_status_line(self, role: str, record: DeviceRecord | None) -> str:
        label = "Source" if role == "source" else "Target"
        if record is None:
            return f"{label}: [dim](not selected — press [{'s' if role == 'source' else 't'}])[/dim]"

        is_live = any(d.serial == record.serial and d.is_ready for d in self._devices)
        live_dot = "[green]●[/green]" if is_live else "[dim]○[/dim]"
        name = record.model or record.serial
        is_scanning = self._scanning and self._scan_role == role

        if is_scanning:
            prog = f" {self._scan_progress}" if self._scan_progress else ""
            return f"{label}: {name} ({record.serial}) {live_dot}  [yellow]scanning…{prog}[/yellow]"
        if record.apps:
            count = len(record.apps)
            ts = f"  on {record.scanned_at[:10]}" if record.scanned_at else ""
            cached = "" if is_live else "  [dim](cached — device offline)[/dim]"
            return (
                f"{label}: {name} ({record.serial}) {live_dot}  "
                f"[green]✓ {count} apps scanned{ts}[/green]{cached}"
            )
        return f"{label}: {name} ({record.serial}) {live_dot}  [dim](pending scan…)[/dim]"

    # ------------------------------------------------------------------
    # Row selection helper
    # ------------------------------------------------------------------

    def _selected_live_serial(self) -> str | None:
        """Return the serial of the highlighted *live* device row, or None."""
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            key_str = str(row_key.value)
            if key_str.startswith("cached_"):
                return None   # cached-offline rows are display-only
            return key_str
        except Exception:
            return None

    def _live_device(self, serial: str) -> adb.DeviceEntry | None:
        return next((d for d in self._devices if d.serial == serial), None)

    # ------------------------------------------------------------------
    # Mark SOURCE / TARGET
    # ------------------------------------------------------------------

    def action_mark_source(self) -> None:
        self._attempt_mark("source")

    def action_mark_target(self) -> None:
        self._attempt_mark("target")

    def _attempt_mark(self, role: str) -> None:
        # Guard: scan in progress
        if self._scanning:
            self.notify(
                "A scan is already running — wait for it to finish before marking another device.",
                severity="warning",
            )
            return

        # Guard: must select a live row
        serial = self._selected_live_serial()
        if not serial:
            self.notify(
                "Select a [live] device row first. "
                "Cached-offline rows cannot be re-scanned — connect the device and press [r].",
                severity="warning",
            )
            return

        dev = self._live_device(serial)
        if dev is None:
            return  # row vanished between refresh and key press — harmless

        # Guard: device must be authorized
        if not dev.is_ready:
            self.notify(
                f"Device {serial} is not ready (state: [b]{dev.state}[/b]).  "
                "Ensure USB debugging is enabled and accept the RSA fingerprint on-device.",
                severity="warning",
            )
            return

        # Guard: cannot use same serial for both roles
        other_record = self.session.target if role == "source" else self.session.source
        if other_record and other_record.serial == serial:
            other_label = "TARGET" if role == "source" else "SOURCE"
            self.notify(
                f"[b]{serial}[/b] is already assigned as {other_label}. "
                "Use a different physical device.",
                severity="error",
            )
            return

        # Guard: serial differs from what's already cached in session for this role
        current_record = self.session.source if role == "source" else self.session.target
        if current_record and current_record.serial != serial:
            self._show_mismatch_dialog(role, serial, dev.model, current_record)
            return

        # All guards passed — start scan immediately
        self._start_scan(role, serial, dev.model)

    def _show_mismatch_dialog(
        self,
        role: str,
        new_serial: str,
        new_model: str | None,
        existing: DeviceRecord,
    ) -> None:
        existing_name = existing.model or existing.serial
        new_name = new_model or new_serial
        body = (
            f"The session expects {role.upper()} = [b]{existing_name}[/b] ({existing.serial})\n"
            f"but the selected device is [b]{new_name}[/b] ({new_serial}).\n\n"
            f"Rescan as the new {role.upper()}?  "
            f"(The previous scan data for {existing_name} will be cleared from this session.)\n\n"
            f"Press Cancel to keep waiting for {existing_name}."
        )

        def _handle(confirmed: bool | None) -> None:
            if confirmed:
                if role == "source":
                    self.session.source = None
                else:
                    self.session.target = None
                self._start_scan(role, new_serial, new_model)

        self.app.push_screen(
            ConfirmScreen(
                f"Different {role.upper()} device detected",
                body,
                danger=False,
                confirm_label=f"Rescan as new {role.upper()}",
            ),
            _handle,
        )

    # ------------------------------------------------------------------
    # Scan lifecycle
    # ------------------------------------------------------------------

    def _start_scan(self, role: str, serial: str, model: str | None) -> None:
        """Create a placeholder DeviceRecord immediately, then fire background scan."""
        placeholder = DeviceRecord(serial=serial, model=model, apps={}, scanned_at=None)
        if role == "source":
            self.session.source = placeholder
        else:
            self.session.target = placeholder
        # Persist the placeholder so the session remembers which serial is assigned
        self.session_mgr.save(self.session)

        self._scanning = True
        self._scan_role = role
        self._scan_progress = ""
        self._rebuild_table()
        self._update_status()
        self._run_scan_worker(role, serial, model)

    @work
    async def _run_scan_worker(self, role: str, serial: str, model: str | None) -> None:
        """Background worker: calls scan_device() then updates the session + UI.

        NOTE: this is an *async* @work so it runs on the event loop thread.  UI
        mutations can be done directly (no call_from_thread needed).  However,
        the blocking scan_device() call is dispatched via asyncio.to_thread so
        it runs in a real OS thread — that thread may NOT touch any Textual
        widgets directly.
        """

        # The progress callback is invoked inside the worker thread created by
        # asyncio.to_thread, so we must use call_from_thread there.
        def on_progress(i: int, total: int, pkg: str) -> None:
            self.app.call_from_thread(self._on_scan_progress, f"({i}/{total})")

        try:
            apps, warnings = await asyncio.to_thread(
                scan_device,
                self.adb_path,
                serial,
                self.settings.third_party_only,
                on_progress,
            )
        except Exception as exc:
            self._on_scan_failed(role, f"Unexpected error during scan: {exc}")
            return

        if not apps:
            self._on_scan_failed(
                role,
                "No packages returned. "
                "Check USB debugging authorization — accept the RSA fingerprint on-device, "
                "then ensure 'adb devices' shows the device as [b]device[/b] (not unauthorized).",
            )
            return

        apps_dict = {pkg: appinfo_to_dict(info) for pkg, info in apps.items()}
        now_ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        record = DeviceRecord(serial=serial, model=model, apps=apps_dict, scanned_at=now_ts)
        self._on_scan_complete(role, record, warnings)

    def _on_scan_progress(self, progress: str) -> None:
        """May be called from a worker thread (via call_from_thread) or the event loop."""
        self._scan_progress = progress
        # Use call_from_thread-safe path: schedule the UI update on the event loop
        try:
            self._update_status()
        except Exception:
            pass

    def _on_scan_complete(
        self, role: str, record: DeviceRecord, warnings: list[str]
    ) -> None:
        if role == "source":
            self.session.source = record
        else:
            self.session.target = record
        self.session_mgr.save(self.session)

        self._scanning = False
        self._scan_role = None
        self._scan_progress = ""
        self._rebuild_table()
        self._update_status()

        count = len(record.apps)
        self.notify(
            f"{role.upper()} scanned: {count} app(s) found.",
            severity="information",
        )
        if warnings:
            self.notify(
                f"{len(warnings)} warning(s) during {role.upper()} scan — "
                "some apps may be missing version info.  Check the log file for details.",
                severity="warning",
            )

    def _on_scan_failed(self, role: str, message: str) -> None:
        # Clear the placeholder record so the user can retry
        if role == "source":
            self.session.source = None
        else:
            self.session.target = None
        self.session_mgr.save(self.session)

        self._scanning = False
        self._scan_role = None
        self._scan_progress = ""
        self._rebuild_table()
        self._update_status()
        self.notify(
            f"Scan failed for {role.upper()}: {message}",
            severity="error",
        )

    # ------------------------------------------------------------------
    # Continue gate
    # ------------------------------------------------------------------

    def action_continue_(self) -> None:
        if self._scanning:
            self.notify(
                "A scan is in progress — wait for it to finish.",
                severity="warning",
            )
            return

        src = self.session.source
        tgt = self.session.target

        if not src or not src.apps:
            self.notify(
                "SOURCE not scanned yet. "
                "Connect a device, select its row, and press [s].",
                severity="warning",
            )
            return
        if not tgt or not tgt.apps:
            self.notify(
                "TARGET not scanned yet. "
                "Connect a device, select its row, and press [t].",
                severity="warning",
            )
            return
        if src.serial == tgt.serial:
            self.notify(
                "Source and target must be different physical devices.",
                severity="error",
            )
            return

        # Dual-cable extra check: both must be physically connected right now
        if not self._single_cable_mode:
            src_live = any(d.serial == src.serial and d.is_ready for d in self._devices)
            tgt_live = any(d.serial == tgt.serial and d.is_ready for d in self._devices)
            if not src_live:
                self.notify(
                    f"[b]Dual-cable mode[/b]: SOURCE ({src.serial}) is not connected.  "
                    "Connect it, press [r] to refresh, or press [m] to switch to single-cable mode.",
                    severity="warning",
                )
                return
            if not tgt_live:
                self.notify(
                    f"[b]Dual-cable mode[/b]: TARGET ({tgt.serial}) is not connected.  "
                    "Connect it, press [r] to refresh, or press [m] to switch to single-cable mode.",
                    severity="warning",
                )
                return

        self.dismiss(self.session)

    # ------------------------------------------------------------------
    # Quit
    # ------------------------------------------------------------------

    def action_quit_app(self) -> None:
        if self._scanning:
            # Don't abandon mid-scan; the session placeholder is already on disk
            self.notify(
                "Scan in progress — session is being saved automatically.  "
                "Quit will be available once the scan finishes.",
                severity="warning",
            )
            return
        self.dismiss(None)
