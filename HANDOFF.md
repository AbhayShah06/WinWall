# WinWall audit and updater handoff

Last updated: 2026-09-17 (implementation, verification, and GitHub release complete)

## User-reported issue

- Updating an older WinWall build from the in-app **Update now** button produced:
  `Security validation failure: parent process has different executable!`
- The update progress presentation is not clear enough.

## Confirmed context

- WinWall is a Windows/PySide6 live-wallpaper application packaged as a one-file executable with PyInstaller.
- Current source version is 1.0.4.
- The current build dependency pins PyInstaller 6.22.2.
- The exact error is emitted by PyInstaller's one-file bootloader parent-process validation.
- PyInstaller 6.22.1 had a confirmed Windows false-positive for executables launched through a directory junction or symlink; the upstream fix landed for 6.22.2. The installed path and all of its parent directories are ordinary directories, so that bug is not the trigger here.
- The repository is clean at the start of this work.
- The installed executable is version 1.0.3 and its SHA-256 hash exactly matches `dist/WinWall.exe`; the update download and swap did not corrupt it.
- A clean launch of the installed executable in `--selftest` mode exits successfully with code 0.
- The Startup-folder shortcut resolves correctly to `%LOCALAPPDATA%\WinWall\WinWall.exe --tray`. An earlier blank-target reading was an artifact of inspecting the shortcut without access to its target, not a broken shortcut.

## Audit scope

- Application lifecycle and single-instance handling
- Startup shortcut and tray behavior
- Wallpaper engine and watchdog behavior
- Local data, settings, thumbnails, and imports
- Browse/shop and download flow
- Self-update check, download, verification, swap, rollback, and relaunch
- PyInstaller build/release configuration
- Automated tests and important missing coverage

## Confirmed root cause

The updater launches a batch helper from inside the frozen WinWall process and lets it inherit PyInstaller's private environment. The helper then starts the new `WinWall.exe` with the same environment. PyInstaller treats that restart as a child stage of the old application; because its actual parent is `cmd.exe`, the new bootloader rejects it with `parent process has different executable`. PyInstaller's documented restart contract requires `PYINSTALLER_RESET_ENVIRONMENT=1` for this exact scenario.

The current helper also deletes the old `.bak` executable only a few seconds after launch, without verifying that the new application started successfully. That removes automatic rollback precisely when a restart fails.

## Architecture summary

- `app.py`: PySide6 shell, settings/data lifecycle, library and Browse UI, tray/startup integration, workers, update orchestration, and single-instance IPC.
- `engine.py`: Windows WorkerW/Progman discovery, one mpv-backed child window per monitor, playback IPC, coverage/battery pausing, and display/Explorer self-healing.
- `shop.py`: MotionBgs scraping, thumbnail retrieval, and streamed wallpaper downloads.
- `updater.py`: GitHub latest-release lookup, streamed download and SHA-256/size verification, in-place executable swap, rollback, and restart.
- `winwall.py`: minimal CLI around the wallpaper engine.
- `build_exe.py`: authoritative one-file PyInstaller build; bundles mpv and excludes QtMultimedia from packaged builds to reduce weight.
- `tests/test_downloads.py`: seven passing tests covering version comparison, wallpaper streaming, empty search behavior, update integrity/status handling, interrupted-download cleanup, and restart-helper behavior.

## Audit findings to preserve for follow-up

- Updater restart environment is the confirmed failure and is in scope to fix now.
- Update UI has only changing label text; it needs a real progress indicator and explicit download/verify/install/restart phases.
- Update rollback is incomplete because the backup is deleted before successful startup is established.
- The helper waits by image name rather than the exact process ID and uses a predictable temp filename.
- Updater partial files are not cleaned up when a network exception interrupts a download.
- The shipped build deliberately excludes QtMultimedia, so hover video previews work from source but not in the packaged executable.
- Engine stop/start shares one event and clears the thread reference after a timed join; if shutdown exceeds the timeout, a new start could overlap the old engine thread.
- Active Qt worker threads are not explicitly cancelled/waited during Quit.
- Thumbnail cache keys use only the filename stem, so different extensions with the same stem collide.
- Shop video responses are not content-type/format validated before being handed to mpv.
- Version values are duplicated in `app.py` and `version_info.txt` without a consistency test.

## Work remaining

- Replace the installed 1.0.3 executable only if the user requests installation; it was intentionally left untouched during development.

## Implemented changes

- Restart helper and restarted WinWall both receive `PYINSTALLER_RESET_ENVIRONMENT=1`.
- The helper waits for the exact current PID instead of every process named `WinWall.exe`.
- Each update uses a unique temporary helper file.
- Swap failures restore and relaunch the old executable.
- The `.bak` rollback copy survives the restart and is removed only after the new app reaches Python/UI initialization.
- Interrupted update downloads remove their `.part` files.
- Response `Content-Length` is verified in addition to the GitHub asset size and SHA-256 digest.
- The banner now has a visible percentage bar and distinct downloading, verifying, installing, restarting, and failure states.
- Disabled update buttons now look disabled; banner text renders on a transparent background.
- Source and Windows metadata versions were advanced to 1.0.4.
- Seven unit tests pass, including new regression coverage for environment reset, exact-PID helper generation, backup preservation, status phases, and interrupted-download cleanup.
- All Python files compile successfully and `git diff --check` reports no errors.
- A local Qt-rendered visual QA pass confirmed the progress banner is legible and fits the existing design.

## Final verification

- `dist/WinWall.exe` built successfully with PyInstaller 6.22.2 and bundled mpv.
- File version: 1.0.4.
- Product version: 1.0.4.
- Size: 95,229,088 bytes.
- SHA-256: `EBA917AF4B29E4D5BB73D4206CB5C31AB8B5E0349C1B511005627276317569E9`.
- Packaged `--selftest` launch exited with code 0.
- Seven unit tests pass.
- Python syntax compilation passes for all source/build modules.
- `git diff --check` passes; only line-ending normalization warnings are reported.
- PyInstaller warnings are limited to expected non-Windows optional modules, deliberately excluded QtMultimedia preview modules, and optional `win32com.gen_py`.

## GitHub synchronization

- Source commit `c9b1756` was pushed to `AbhayShah06/WinWall` on `main`.
- Release `v1.0.4` was published to `AbhayShah06/WinWall-dist` and marked latest.
- GitHub reports the uploaded `WinWall.exe` as 95,229,088 bytes with digest `sha256:eba917af4b29e4d5bb73d4206cb5c31ab8b5e0349c1b511005627276317569e9`, matching the verified local build.
- Release URL: https://github.com/AbhayShah06/WinWall-dist/releases/tag/v1.0.4
