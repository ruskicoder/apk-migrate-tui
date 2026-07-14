"""Session persistence for multi-device APK migration.

A Session captures the full state of a migration job:
  - Which two devices are involved (source/target) and their scanned app inventories.
  - Which packages the user selected for migration and the action to take per-package.
  - The per-package execution state (pending → archiving → archived, etc.).

Sessions are persisted to disk so the job survives app restarts, terminal crashes,
and USB cable unplugs mid-operation.  The session file is written atomically (temp
file + rename) after every state mutation so a crash can never produce a partial write.

Session files live in:
    ~/.apk-migrate-tui/sessions/<session_id>.json

ExecutionState state machine:
    PENDING
      │
      ├─→ ARCHIVING → ARCHIVED     (archive completed)
      │              → ARCHIVE_FAILED
      │
      └─→ INSTALLING → INSTALLED   (install completed)
                     → INSTALL_FAILED

Any state → SKIPPED   (user explicitly skipped)
Any state → CANCELLED (user cancelled the running batch)

ARCHIVING and INSTALLING are *in-flight markers*.  If the process is killed while
either of these is set, SessionManager._recover_in_flight() reverts them to PENDING
on the next load so the step is safely retried.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Execution state machine
# ---------------------------------------------------------------------------

class ExecutionState(str, Enum):
    PENDING        = "pending"
    ARCHIVING      = "archiving"        # in-flight crash-detection marker
    ARCHIVED       = "archived"
    ARCHIVE_FAILED = "archive_failed"
    INSTALLING     = "installing"       # in-flight crash-detection marker
    INSTALLED      = "installed"
    INSTALL_FAILED = "install_failed"
    SKIPPED        = "skipped"
    CANCELLED      = "cancelled"


#: States from which no further transitions are expected (except by explicit retry).
TERMINAL_STATES: frozenset[ExecutionState] = frozenset({
    ExecutionState.ARCHIVED,
    ExecutionState.ARCHIVE_FAILED,
    ExecutionState.INSTALLED,
    ExecutionState.INSTALL_FAILED,
    ExecutionState.SKIPPED,
    ExecutionState.CANCELLED,
})


# ---------------------------------------------------------------------------
# AppInfo serialization helpers (JSON-safe round-trip)
# ---------------------------------------------------------------------------

def appinfo_to_dict(info: Any) -> dict[str, Any]:  # info: models.AppInfo
    """Serialize an AppInfo to a plain dict suitable for JSON storage."""
    return {
        "package": info.package,
        "version_code": info.version_code,
        "version_name": info.version_name,
        "label": info.label,
        "installer": info.installer,
        "apk_remote_paths": list(info.apk_remote_paths),
    }


def appinfo_from_dict(d: dict[str, Any]) -> Any:  # returns models.AppInfo
    """Deserialize an AppInfo from a plain dict.  Import is lazy to avoid circular deps."""
    from .models import AppInfo
    return AppInfo(
        package=d.get("package", ""),
        version_code=d.get("version_code"),
        version_name=d.get("version_name"),
        label=d.get("label"),
        installer=d.get("installer"),
        apk_remote_paths=list(d.get("apk_remote_paths", [])),
    )


# ---------------------------------------------------------------------------
# DeviceRecord — one scanned device
# ---------------------------------------------------------------------------

@dataclass
class DeviceRecord:
    """All we know about one device in this migration session.

    ``apps`` maps package name → appinfo_to_dict() result.  It starts as ``{}``
    while a scan is in progress and becomes non-empty once the scan completes.
    """

    serial: str
    model: str | None
    apps: dict[str, dict]      # package → appinfo_to_dict(AppInfo)
    scanned_at: str | None = None   # ISO-8601 timestamp, None until scan completes

    # --- serialization ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "model": self.model,
            "apps": self.apps,
            "scanned_at": self.scanned_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeviceRecord":
        return cls(
            serial=d["serial"],
            model=d.get("model"),
            apps=d.get("apps", {}),
            scanned_at=d.get("scanned_at"),
        )

    # --- convenience ---

    def get_app_infos(self) -> dict[str, Any]:  # dict[str, AppInfo]
        """Reconstruct AppInfo objects from the stored JSON dicts."""
        return {pkg: appinfo_from_dict(d) for pkg, d in self.apps.items()}


# ---------------------------------------------------------------------------
# PackageExecution — per-package execution record
# ---------------------------------------------------------------------------

@dataclass
class PackageExecution:
    """Tracks the migration state of a single package across restarts."""

    package: str
    action: str          # ActionKind.value  ("archive" | "install" | "archive_and_install")
    state: ExecutionState

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "action": self.action,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PackageExecution":
        return cls(
            package=d["package"],
            action=d["action"],
            state=ExecutionState(d["state"]),
        )


# ---------------------------------------------------------------------------
# Session — the top-level persistent job record
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """Full state of one APK migration job."""

    session_id: str           # uuid4 hex  — used as the filename
    created_at: str           # ISO-8601
    source: DeviceRecord | None = None
    target: DeviceRecord | None = None
    executions: dict[str, PackageExecution] = field(default_factory=dict)
    completed: bool = False

    # --- computed properties ---

    @property
    def is_ready(self) -> bool:
        """True when both devices have been scanned and have non-empty app data."""
        return (
            self.source is not None and bool(self.source.apps)
            and self.target is not None and bool(self.target.apps)
        )

    @property
    def display_name(self) -> str:
        """Human-readable 'Pixel_6 → Pixel_10' label for the resume screen."""
        src = (self.source.model or self.source.serial) if self.source else "?"
        tgt = (self.target.model or self.target.serial) if self.target else "?"
        return f"{src} → {tgt}"

    @property
    def done_count(self) -> int:
        return sum(1 for e in self.executions.values() if e.state in TERMINAL_STATES)

    @property
    def total_count(self) -> int:
        return len(self.executions)

    def check_completion(self) -> None:
        """Auto-mark completed when every execution has reached a terminal state."""
        if self.executions and all(
            e.state in TERMINAL_STATES for e in self.executions.values()
        ):
            self.completed = True

    # --- serialization ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "source": self.source.to_dict() if self.source else None,
            "target": self.target.to_dict() if self.target else None,
            "executions": {
                pkg: exe.to_dict() for pkg, exe in self.executions.items()
            },
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        return cls(
            session_id=d["session_id"],
            created_at=d["created_at"],
            source=DeviceRecord.from_dict(d["source"]) if d.get("source") else None,
            target=DeviceRecord.from_dict(d["target"]) if d.get("target") else None,
            executions={
                pkg: PackageExecution.from_dict(e)
                for pkg, e in d.get("executions", {}).items()
            },
            completed=d.get("completed", False),
        )


# ---------------------------------------------------------------------------
# SessionManager — disk I/O + crash-recovery
# ---------------------------------------------------------------------------

class SessionManager:
    """Manages the on-disk lifecycle of Session objects.

    All writes go through ``save()`` which uses an atomic temp-file + rename so
    a process crash can never leave a partially-written session file.
    """

    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir

    # --- public API ---

    def new_session(self) -> "Session":
        """Create a fresh Session, persist it immediately, and return it."""
        session_id = uuid.uuid4().hex
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        session = Session(session_id=session_id, created_at=now)
        self.save(session)
        return session

    def save(self, session: Session) -> None:
        """Atomically write the session to disk.

        If the write fails (e.g. disk full) the error is silently swallowed so
        the user's current operation is not interrupted.  The session simply
        becomes non-resumable after that point.
        """
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            path = self._session_path(session.session_id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            # Swallow silently — a failed save must never crash the UI.
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def load(self, session_id: str) -> "Session | None":
        """Load and crash-recover a session from disk.  Returns None on any error."""
        path = self._session_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            session = Session.from_dict(data)
            self._recover_in_flight(session)
            return session
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
            return None

    def list_incomplete(self) -> list["Session"]:
        """Return all incomplete sessions, sorted oldest-first."""
        if not self.sessions_dir.exists():
            return []
        incomplete: list[Session] = []
        for path in sorted(self.sessions_dir.glob("*.json")):
            if ".tmp" in path.suffixes:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("completed", False):
                    continue
                session = Session.from_dict(data)
                self._recover_in_flight(session)
                incomplete.append(session)
            except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
                continue
        return incomplete

    def delete(self, session_id: str) -> None:
        """Delete a session file.  Safe to call if the file does not exist."""
        try:
            self._session_path(session_id).unlink(missing_ok=True)
        except OSError:
            pass

    # --- internals ---

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    @staticmethod
    def _recover_in_flight(session: "Session") -> None:
        """Revert in-flight markers left by a previous crash.

        ARCHIVING and INSTALLING are written *before* the operation starts so
        a crash leaves a visible in-flight marker.  On the next load we revert
        these to PENDING so the step is safely retried:

        - ARCHIVING → PENDING  (archive.py skips if already_has_version is True,
                                so re-running is idempotent)
        - INSTALLING → PENDING (adb install -r is idempotent)
        """
        for exe in session.executions.values():
            if exe.state in (ExecutionState.ARCHIVING, ExecutionState.INSTALLING):
                exe.state = ExecutionState.PENDING
