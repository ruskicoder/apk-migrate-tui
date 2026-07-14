# apk-migrate-tui

Terminal UI to compare installed apps between two Android devices over `adb`, archive
their APKs to a local folder, and/or install them onto a second device. Built for the
"migrate FOSS/sideloaded apps that Android's own Move-to-new-device skips" use case
(e.g. Pixel 6 -> Pixel 10), but works for any two devices reachable via `adb`.

Cross-platform (Linux/macOS/Windows) - only depends on Python and `adb` being on PATH.

## Requirements

- Python 3.10+
- Android platform-tools (`adb`) on your `PATH` - or point to it explicitly via the
  in-app Settings screen (`adb_path`)
- USB debugging enabled on both devices, with the RSA authorization prompt accepted for
  this computer on each device

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python3 -m apk_migrate_tui
```

Or, after `pip install -e .`:

```bash
apk-migrate-tui
```

## Flow

1. **Device selection screen** - connect both phones, press `r` to refresh the device
   list, move the cursor to a device and press `s` to mark it as SOURCE (the phone you're
   migrating *from*) or `t` to mark it TARGET (migrating *to*). Press `c` to continue.
2. **App list screen** - the tool scans both devices (`pm list packages`, `dumpsys
   package`, `pm path`) and shows a diff:
   - `identical` - same package, same versionCode on both devices (hidden by default)
   - `version diff` - same package, different versionCode, or version couldn't be read
     on one side (treated as "different" rather than silently skipped, since that's the
     safer default)
   - `source only` - only on the source device (the main "needs migrating" case)
   - `target only` - only on the target device, informational, hidden by default

## Key bindings (app list screen)

| Key | Action |
|---|---|
| `↑ / ↓` | Move cursor |
| `space` | Toggle selection on the current row |
| `a` / `A` | Select all visible / select none |
| `s` | Archive selected apps to local folder |
| `i` | Install selected apps onto TARGET (archives first if needed) |
| `b` | Archive **and** install |
| `u` | Force reinstall: **uninstalls** the app on TARGET first, then installs from source. Use this only when a normal install fails with a signature mismatch - it erases that app's data on the target. Requires explicit confirmation. |
| `f` | Toggle hide-identical-versions |
| `/` | Focus the search box (filters by package name) |
| `,` | Settings |
| `r` | Rescan both devices |
| `d` | Back to device selection |
| `q` | Quit |

Every archive/install/uninstall batch shows a confirmation screen listing exactly which
packages will be touched before anything happens. Destructive actions (anything that can
overwrite or erase data on the target) are styled in red and require you to click/press
the actual confirm button - not just Enter.

## Archive layout

Flat folder, one subfolder per package, no version history (re-archiving a package
overwrites its previous archived copy):

```
~/.apk-migrate-tui/archive/
  org.fdroid.fdroid/
    manifest.json          # version_code, version_name, installer, apk_files, archived_at
    base.apk
  com.example.splitapp/
    manifest.json
    base.apk
    split_config.arm64_v8a.apk
```

Writes are staged in a temporary directory and swapped into place at the end, so a pull
that's interrupted (USB unplugged, process killed) can't leave a `manifest.json` pointing
at APK files that don't actually exist.

## Settings (`,` key)

Stored at `~/.apk-migrate-tui/settings.json`:

- **Hide identical-version apps** - the "ignore identical package version" toggle
- **Show target-only apps**
- **Third-party apps only** - excludes system apps entirely (`pm list packages -3`)
- **App source filter** - "all non-system" vs. "FOSS/sideloaded" (matches installer
  package `org.fdroid.fdroid`, `dev.imranr.obtainium`, `com.aurora.store`, or no
  installer at all / installed via `adb install`)
- **Archive directory**
- **adb path override**

## Safety notes

- **This does not migrate app data** (saves, logins, local settings) - only APKs. A
  reinstalled app starts fresh. Non-root app-data migration on modern Android is
  unreliable (`adb backup` is deprecated and most apps opt out via
  `android:allowBackup="false"`), so it's intentionally out of scope here.
- **Split APKs** are detected and pulled/installed together automatically
  (`pm path` -> multiple files -> `adb install-multiple`).
- **Signature mismatches**: if TARGET already has a differently-signed build of the same
  package, a normal install fails safely with `INSTALL_FAILED_UPDATE_INCOMPATIBLE` and is
  reported as such - nothing is uninstalled automatically. Use `u` (force reinstall) only
  if you've read and accept the data-loss warning it shows.
- **Version comparison uses `versionCode`**, not the human-readable `versionName`, since
  versionCode is guaranteed to be a comparable integer.
- If a version can't be read from either device (a `dumpsys` failure, e.g. due to a
  permission-restricted package), the app is flagged `version diff` rather than assumed
  identical - it will show up for review instead of silently being skipped.
- A batch operation that hits 3 consecutive timeouts (device likely disconnected) stops
  itself early rather than grinding through the rest of a long selection with the same
  failure.
- Every run writes a persistent log file to `~/.apk-migrate-tui/logs/run-<timestamp>.log`
  for after-the-fact review, in addition to the in-app log panel.
- All `adb` calls use argument lists (never a shell string), and every call has a timeout
  so a dropped USB connection can't hang the UI indefinitely.

## Known limitations

- App display names (labels) aren't fetched - only package names are shown. Getting the
  label reliably needs `aapt`/`aapt2` against the pulled APK, which isn't guaranteed to
  be available; can be added later if useful.
- No archive version history - archiving a package again replaces the previously archived
  copy of that package.
- Scanning is sequential per package (not parallelized across `adb shell` calls), which
  keeps things safe/predictable on flaky USB connections but means a device with many
  hundreds of apps will take a while to scan.
- App-data migration is out of scope (see Safety notes above).

## Tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

Includes unit tests for `adb` output parsing, the diff engine, and the archive manager's
atomic staging/commit behavior, plus headless end-to-end UI smoke tests (Textual's Pilot
harness) that drive the real screens/workers with `adb` fully mocked - no physical device
needed to run the test suite.
