# WinWall audit and updater handoff

Last updated: 2026-09-20 (1.0.3-to-1.0.4 bootstrap failure confirmed; bridge release in progress)

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

- Publish a bridge release that can be launched by the unfixed 1.0.3 updater.
- Reproduce the legacy updater handoff in isolation and verify that the bridge build starts automatically.
- The installed executable has already been swapped to 1.0.4 by the failed-restart update attempt; do not overwrite it during isolated bridge testing.

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

## 2026-09-20 bootstrap finding

- A real update initiated by installed WinWall 1.0.3 downloaded and swapped in the verified 1.0.4 executable, but automatic restart still showed `Security validation failure: parent process has different executable!`.
- The screenshot's old text-only progress presentation confirms that the updater performing the handoff was 1.0.3. The restart fix inside 1.0.4 cannot execute because the PyInstaller bootloader fails before Python starts.
- The installed target is now a valid 1.0.4 executable (95,229,088 bytes); only its inherited-environment launch failed. A clean/manual launch is expected to work.
- PyInstaller changed restart semantics in 6.9: a restarted one-file executable must receive `PYINSTALLER_RESET_ENVIRONMENT=1`. The legacy 1.0.3 updater cannot be retroactively changed to set it.
- Resolution: build a one-time 1.0.5 bridge with the pre-6.9 bootloader behavior, while retaining the fixed 1.0.5 Python updater code. Once users reach 1.0.5, future releases can return to the current PyInstaller toolchain because the updater now resets the environment correctly.
- An isolated PyInstaller 6.8.0 environment was created successfully with Python 3.12.14, PySide6 6.11.2, and the current contrib hooks. `requirements-bridge.txt` records this one-time build toolchain; normal builds remain pinned to PyInstaller 6.22.2.

## 1.0.5 bridge verification

- Version 1.0.5 was built successfully with the one-time PyInstaller 6.8.0 bridge toolchain.
- File size: 95,202,186 bytes.
- SHA-256: `621E56A62BE7FAEAEFA60C3EFF2B64963CE3BD074C49A18D8294DE1823D1CF62`.
- Direct packaged self-test exited successfully.
- Legacy-path integration test passed using a disposable PyInstaller 6.22.2 parent and the exact pre-1.0.4 batch helper: swap observed, 1.0.5 bridge process observed starting, self-test observed exiting, no security dialog/process remained, and no `.new` or `.bak` file was stranded.
- Future-path integration test passed using a PyInstaller 6.8.0 parent with the fixed updater and a PyInstaller 6.22.2 child: swap, child start, clean exit, and backup cleanup were all observed.
- Seven unit tests and Python syntax compilation pass.
