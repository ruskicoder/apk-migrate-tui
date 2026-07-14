from __future__ import annotations

from . import adb
from .logging_utils import setup_logger
from .screens.app_list import AppListScreen
from .screens.device_select import DeviceSelectScreen
from .screens.dialogs import MessageScreen
from .settings import Settings

from textual import work
from textual.app import App


class ApkMigrateApp(App[None]):
    TITLE = "APK Migrate TUI"
    SUB_TITLE = "Pixel 6 -> Pixel 10 (or any two Android devices)"

    def __init__(self) -> None:
        super().__init__()
        self.settings = Settings.load()
        self.logger = setup_logger()

    async def on_mount(self) -> None:
        self.run_flow()

    @work(exclusive=True)
    async def run_flow(self) -> None:
        try:
            adb_path = adb.find_adb(self.settings.adb_path)
        except adb.AdbNotFoundError as e:
            await self.push_screen_wait(MessageScreen("adb not found", str(e)))
            self.exit()
            return

        self.logger.info("Using adb at: %s", adb_path)

        while True:
            devices = await self.push_screen_wait(DeviceSelectScreen(adb_path))
            if devices is None:
                break
            source_serial, target_serial = devices
            self.logger.info("Selected source=%s target=%s", source_serial, target_serial)
            result = await self.push_screen_wait(
                AppListScreen(adb_path, source_serial, target_serial, self.settings)
            )
            if result != "change_devices":
                break

        self.exit()


def main() -> None:
    ApkMigrateApp().run()


if __name__ == "__main__":
    main()
