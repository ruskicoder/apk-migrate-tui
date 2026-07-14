"""Main application entry point.

Startup flow:
1. Find adb.
2. Check for incomplete migration sessions on disk → show SessionResumeScreen if any.
3. If new flow: show ModeSelectScreen to choose between Migrate Mode and Manage Single Device Mode.
4. Route accordingly to Migrate Mode loops or Per Device Mode loops.
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
from .screens.mode_select import ModeSelectScreen
from .screens.resume_screen import SessionResumeScreen
from .screens.single_device_app_list import SingleDeviceAppScreen
from .screens.single_device_select import SingleDeviceSelectScreen
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
        selected_mode: str | None = None

        if incomplete:
            self.logger.info("%d incomplete session(s) found on disk.", len(incomplete))
            result = await self.push_screen_wait(
                SessionResumeScreen(incomplete, session_mgr)
            )
            if result is None:
                # User chose "new session" -> will ask for mode
                pass
            else:
                # result is a Session, meaning we automatically resume in "migrate" mode
                active_session = result
                selected_mode = "migrate"
                self.logger.info("Resuming session %s", active_session.session_id)

        # ------------------------------------------------------------------
        # Step 3: Choose TUI Mode if not resuming a session
        # ------------------------------------------------------------------
        if selected_mode is None:
            selected_mode = await self.push_screen_wait(ModeSelectScreen())
            if selected_mode is None:
                self.exit()
                return

        # ------------------------------------------------------------------
        # Step 4: Route based on Mode selection
        # ------------------------------------------------------------------
        if selected_mode == "migrate":
            if active_session is None:
                active_session = session_mgr.new_session()

            # Main Migrate Mode loop
            while True:
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
                    active_session.source = None
                    active_session.target = None
                    session_mgr.save(active_session)
                    continue

                break  # quit or session completed

        elif selected_mode == "per_device":
            # Main Per Device Mode loop
            while True:
                device_info = await self.push_screen_wait(
                    SingleDeviceSelectScreen(adb_path=adb_path)
                )
                if device_info is None:
                    # Back to ModeSelectScreen
                    self.run_flow()
                    return

                serial, model = device_info
                self.logger.info("Selected single device: serial=%s model=%s", serial, model)

                nav_result = await self.push_screen_wait(
                    SingleDeviceAppScreen(
                        adb_path=adb_path,
                        serial=serial,
                        model=model,
                        settings=self.settings,
                    )
                )

                if nav_result == "change_device":
                    continue

                break

        self.exit()


def main() -> None:
    ApkMigrateApp().run()


if __name__ == "__main__":
    main()
