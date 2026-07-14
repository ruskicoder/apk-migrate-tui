from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import SourceFilter

CONFIG_DIR = Path.home() / ".apk-migrate-tui"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
DEFAULT_ARCHIVE_DIR = CONFIG_DIR / "archive"
LOG_DIR = CONFIG_DIR / "logs"


@dataclass
class Settings:
    adb_path: str | None = None                 # override auto-detection if set
    archive_dir: str = str(DEFAULT_ARCHIVE_DIR)
    hide_identical: bool = True                  # the "ignore identical version" toggle
    show_target_only: bool = False
    source_filter: str = SourceFilter.ALL_NON_SYSTEM.value
    third_party_only: bool = True                # -3 flag: exclude system apps entirely
    sessions_dir: str = str(CONFIG_DIR / "sessions")  # where session JSON files are stored
    cleanup_after_install: bool = False           # delete local APK archive after install
    connection_mode: str = "dual"                 # "dual" | "single" cable mode

    @classmethod
    def load(cls) -> "Settings":
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text())
                known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
                return cls(**known)
            except (json.JSONDecodeError, OSError, TypeError):
                # Corrupt settings file must never crash startup - fall back to defaults.
                return cls()
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(SETTINGS_PATH)  # atomic on POSIX and Windows
