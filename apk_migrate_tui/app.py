"""Main application entry point.

Startup flow:
1. Find adb.
2. Check for incomplete sessions on disk → show SessionResumeScreen if any.
3. DeviceSelectScreen (scan both devices → write session).
4. AppListScreen (diff + batch migrate).
5. Loop: 'change_devices' returns to DeviceSelectScreen for the same session.
6. On quit or completion the session is already handled by AppListScreen.
"""

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import App

from . import adb
from .logging_utils import setup_logger
from .screens.app_list import AppListScreen
from .screens.device_select import DeviceSelectScreen
from .screens.dialogs import MessageScreen
from .screens.resume_screen import SessionResumeScreen
from .session import Session, SessionManager
from .settings import Settings


class ApkMigrateApp(App[None]):
    TITLE = "APK Migrate TUI"
    SUB_TITLE = "Pixel 6 → Pixel 10 (or any two Android devices)"

    def __init__(self) -> None:
        super().__init__()
        self.settings = Settings.load()
        self.logger = setup_logger()

    async def on_mount(self) -> None:
        self.run_flow()

    @work(exclusive=True)
    async def run_flow(self) -> None:
        # ------------------------------------------------------------------
        # Step 1: locate adb
        # ------------------------------------------------------------------
        try:
            adb_path = adb.find_adb(self.settings.adb_path)
        except adb.AdbNotFoundError as e:
            await self.push_screen_wait(
                MessageScreen("adb not found", str(e))
            )
            self.exit()
            return

        self.logger.info("Using adb at: %s", adb_path)

        # ------------------------------------------------------------------
        # Step 2: session manager + resume picker
        # ------------------------------------------------------------------
        session_mgr = SessionManager(Path(self.settings.sessions_dir))
        incomplete = session_mgr.list_incomplete()

        active_session: Session | None = None

        if incomplete:
            self.logger.info("%d incomplete session(s) found on disk.", len(incomplete))
            result = await self.push_screen_wait(
                SessionResumeScreen(incomplete, session_mgr)
            )
            if result is None:
                # User chose "new session"
                active_session = session_mgr.new_session()
            else:
                # result is a Session
                active_session = result
                self.logger.info("Resuming session %s", active_session.session_id)
        else:
            active_session = session_mgr.new_session()

        # ------------------------------------------------------------------
        # Main loop (device select → app list → optionally loop back)
        # ------------------------------------------------------------------
        while True:
            # If the session already has both sides scanned, we can jump
            # straight to AppListScreen on resume.  Otherwise go through DeviceSelectScreen.
            if active_session.is_ready:
                self.logger.info(
                    "Session %s has scan data — jumping to AppListScreen",
                    active_session.session_id,
                )
            else:
                device_result = await self.push_screen_wait(
                    DeviceSelectScreen(
                        adb_path=adb_path,
                        session=active_session,
                        session_mgr=session_mgr,
                        settings=self.settings,
                    )
                )
                if device_result is None:
                    # User pressed quit from DeviceSelectScreen
                    break
                active_session = device_result

            if not active_session.is_ready:
                # DeviceSelectScreen dismissed without completing scans (edge case)
                break

            src = active_session.source
            tgt = active_session.target
            self.logger.info(
                "Starting AppListScreen: source=%s (%s), target=%s (%s)",
                src.serial if src else "?",
                src.model if src else "?",
                tgt.serial if tgt else "?",
                tgt.model if tgt else "?",
            )

            nav_result = await self.push_screen_wait(
                AppListScreen(
                    adb_path=adb_path,
                    session=active_session,
                    session_mgr=session_mgr,
                    settings=self.settings,
                )
            )

            if nav_result == "change_devices":
                # Reset the scan data so the user can re-select / re-scan;
                # keep the session ID (preserves execution history for resumed items)
                active_session.source = None
                active_session.target = None
                session_mgr.save(active_session)
                continue

            break   # quit or session completed

        self.exit()


def main() -> None:
    ApkMigrateApp().run()


if __name__ == "__main__":
    main()
