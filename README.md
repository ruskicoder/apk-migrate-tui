# apk-migrate-tui

Terminal UI to compare installed apps between two Android devices over `adb`, archive their APKs to a local folder, and/or install them onto a second device. Built for the "migrate FOSS/sideloaded apps that Android's own Move-to-new-device skips" use case (e.g. Pixel 6 -> Pixel 10), but works for any two devices reachable via `adb`.

Cross-platform (Linux/macOS/Windows) - only depends on Python and `adb` being on PATH.

---

## Requirements

- **Python 3.10+**
- **Android SDK Platform-Tools (`adb`)** on your `PATH` (or specified explicitly via the in-app Settings screen)
- **USB Debugging** enabled on both devices, with the computer's RSA authorization fingerprint accepted on each device.

---

## Install & Run

1. **Clone and setup a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Run the TUI:**
   ```bash
   python3 -m apk_migrate_tui
   ```

   Or install in editable developer mode to run via the command name directly:
   ```bash
   pip install -e .
   apk-migrate-tui
   ```

---

## Connection Modes

The TUI adapts to your hardware setup using two distinct connection modes. You can toggle between them in the Settings modal (`,` key) or using the `[m]` hotkey on the device selection screen:

### 1. Dual-Cable Mode (Default)
- **Scenario:** Both devices are connected simultaneously to your PC.
- **Workflow:** Mark the SOURCE device with `s` and the TARGET device with `t`. The tool starts scanning both devices in parallel.
- **Guard:** The "Continue" button (`c`) is only enabled when both devices are scanned and actively detected online.

### 2. Single-Cable Mode
- **Scenario:** You have only one USB cable or port, allowing only one device to be connected at a time.
- **Workflow:** 
  1. Plug in your old phone (SOURCE) and press `s` to mark and scan it.
  2. Once complete, unplug it. The scan is cached and visible in the device table.
  3. Plug in your new phone (TARGET) and press `t` to mark and scan it.
  4. Press `c` to continue.
- **Guard:** The "Continue" button only requires that both scan results exist. It does not require both physical devices to be online simultaneously.

---

## Session Persistence & Crash Safety

Migration jobs are saved as JSON files in `~/.apk-migrate-tui/sessions/<session_id>.json`. This provides several enterprise-grade safety nets:

- **State Persistence:** If the terminal window closes, the USB cable is unplugged, or the system restarts, you can resume your exact progress from the **Session Resume Picker** upon relaunch.
- **In-Flight Crash Recovery:** Active migration steps write an in-flight status (`ARCHIVING` or `INSTALLING`) to the session file *before* executing the action. If the program crashes, the next startup detects the in-flight status and safely reverts it to `PENDING` so it can be retried cleanly.
- **Atomic Commits:** Writes to the settings and session files use temporary staging files and atomic swaps (`replace`) to guarantee your session configuration is never corrupted.
- **Auto-Deletion:** Once all planned migration steps have succeeded, the session file is automatically deleted from disk to prevent cluttering.

---

## Features

- **Disk Space Pre-Flight Checks:** The confirmation dialog calculates the estimated size of your archive batch against the free space of your destination disk to prevent mid-batch write failures.
- **Live Disk Usage Display:** While modifying the archive path in the Settings screen, a live-updating indicator reports disk space availability (colored green/yellow/red depending on the remaining space).
- **Graceful Error Parking:** During batch migrations, if a device unplugs, the app queues the affected packages into a "Parked" list. Connect the device again and press `r` to retry the parked actions.
- **Clean Installation:** Toggle the "Delete local archive copy after successful install" setting to automatically remove the APK folder from your disk once target device installation succeeds.
- **Safe Overwrite Guards:** Signature mismatches on target installs fail safely with `INSTALL_FAILED_UPDATE_INCOMPATIBLE`. To force an installation by uninstalling first (erasing application data), select the app and press `u`.

---

## Key Bindings

### Device Selection Screen
| Key | Action |
|---|---|
| `r` | Refresh the list of live connected devices |
| `s` | Mark and scan the highlighted device as SOURCE (old phone) |
| `t` | Mark and scan the highlighted device as TARGET (new phone) |
| `m` | Toggle between Dual-Cable and Single-Cable modes |
| `c` | Continue to the package diff screen (saves selection state) |
| `q` | Save session progress and quit the app |

### App List Screen
| Key | Action |
|---|---|
| `space` | Toggle selection on the highlighted package row |
| `a` / `A` | Select all visible / Select none |
| `s` | Archive selected apps to your local folder |
| `i` | Install selected apps onto the TARGET device (archives first if missing) |
| `b` | Archive **and** install selected apps |
| `u` | Force reinstall (Uninstalls from target first, erasing data, then installs) |
| `f` | Toggle hiding identical versions of apps |
| `r` | Rescan live devices (or retry parked items if any exist) |
| `escape` | Cancel batch migration cleanly after the currently processing item finishes |
| `/` | Focus the search input field to filter packages |
| `,` | Open the Settings screen |
| `d` | Back to the Device Selection screen (resets active selection) |
| `q` | Save session progress and quit the app |

---

## Developer Testing

To set up development dependencies and run the complete test suite (60 unit and Textual UI integration tests):

```bash
pip install -r requirements-dev.txt
uv run pytest
```

The test suite runs headlessly, mocking physical `adb` calls to verify worker threads, UI screen transitions, session serialization, and device state anomalies.
