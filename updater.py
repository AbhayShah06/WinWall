"""
updater.py -- one-click self-update for WinWall (GitHub Releases).

Flow: check the latest release -> if newer, download the .exe to a temp file ->
verify size -> write a small detached script that waits for WinWall to exit,
backs up the old exe, swaps in the new one (rolling back on failure), then
relaunches. Downloads are programmatic so no Mark-of-the-Web / SmartScreen.
"""

import os
import re
import json
import hashlib
import tempfile
import subprocess
import urllib.request

REPO = "AbhayShah06/WinWall-dist"   # public releases-only repo (code stays private)
_CREATE_NO_WINDOW = 0x08000000
_NEW_GROUP = 0x00000200


def _parts(v):
    nums = [int(x) for x in re.findall(r"\d+", v or "")][:3]
    return nums or [0]


def is_newer(remote, local):
    return _parts(remote) > _parts(local)


def check(current_version):
    """Return {version, url, size, notes} if a newer release exists, else None."""
    url = "https://api.github.com/repos/%s/releases/latest" % REPO
    req = urllib.request.Request(url, headers={"User-Agent": "WinWall",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = (data.get("tag_name") or "").lstrip("vV")
    if not tag or not is_newer(tag, current_version):
        return None
    for a in data.get("assets", []):
        # Setup executables must never replace the running portable application.
        if a.get("name", "").lower() == "winwall.exe":
            return {"version": tag, "url": a["browser_download_url"],
                    "size": int(a.get("size", 0)), "digest": a.get("digest", ""),
                    "notes": data.get("body", "") or ""}
    return None


def download(url, dest, expected_size=0, expected_digest="", progress=None,
             status=None):
    """Download to dest; verify its size and GitHub-provided SHA-256 digest."""
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "WinWall"})
    if status:
        status("downloading")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length", expected_size or 0))
            got = 0
            digest = hashlib.sha256()
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    if progress and total:
                        progress(got, total)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if status:
        status("verifying")
    # verify: non-trivial size, and matches expected/content-length if known
    size = os.path.getsize(tmp)
    wanted = expected_digest.removeprefix("sha256:").lower()
    bad_digest = bool(wanted) and digest.hexdigest().lower() != wanted
    if (size < 1_000_000 or (expected_size and size != expected_size)
            or (total and size != total) or bad_digest):
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False
    if os.path.exists(dest):
        try:
            os.remove(dest)
        except Exception:
            pass
    os.replace(tmp, dest)
    if status:
        status("ready")
    return True


def cleanup_backup(target_exe):
    """Remove the rollback copy after the updated app reaches Python startup."""
    bak = os.path.abspath(target_exe) + ".bak"
    try:
        if os.path.isfile(bak):
            os.remove(bak)
    except OSError:
        pass


def _batch_path(path):
    """Quote a literal path embedded in a batch file."""
    return os.path.abspath(path).replace("%", "%%")


def apply_and_relaunch(new_exe, target_exe, process_id=None):
    """Launch a helper that swaps the exe, preserves rollback, and restarts."""
    new_exe = os.path.abspath(new_exe)
    target_exe = os.path.abspath(target_exe)
    if not os.path.isfile(new_exe):
        raise FileNotFoundError(new_exe)
    if not os.path.isfile(target_exe):
        raise FileNotFoundError(target_exe)
    process_id = int(process_id or os.getpid())
    bak = target_exe + ".bak"
    fd, bat = tempfile.mkstemp(prefix="winwall-update-", suffix=".bat")
    os.close(fd)
    new_q = _batch_path(new_exe)
    target_q = _batch_path(target_exe)
    bak_q = _batch_path(bak)
    script = (
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"PYINSTALLER_RESET_ENVIRONMENT=1\"\r\n"
        ":wait\r\n"
        'tasklist /fi "pid eq %d" /nh 2>nul | find "%d" >nul\r\n'
        % (process_id, process_id) +
        "if not errorlevel 1 (\r\n"
        "  ping -n 2 127.0.0.1 >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        'if exist "%s" del "%s" >nul 2>&1\r\n' % (bak_q, bak_q) +
        'move /y "%s" "%s" >nul 2>&1\r\n' % (target_q, bak_q) +
        "if errorlevel 1 goto swap_failed\r\n"
        'move /y "%s" "%s" >nul 2>&1\r\n' % (new_q, target_q) +
        "if errorlevel 1 goto swap_failed\r\n"
        'start "" "%s" --tray\r\n' % target_q +
        "goto cleanup\r\n"
        ":swap_failed\r\n"
        'if not exist "%s" if exist "%s" move /y "%s" "%s" >nul 2>&1\r\n'
        % (target_q, bak_q, bak_q, target_q) +
        'if exist "%s" start "" "%s" --tray\r\n' % (target_q, target_q) +
        ":cleanup\r\n"
        'del "%%~f0" >nul 2>&1\r\n'
    )
    try:
        with open(bat, "w", encoding="mbcs") as f:
            f.write(script)
        env = os.environ.copy()
        # Required by PyInstaller for a restarted executable that must outlive
        # the current one-file process. Without it, the new bootloader inherits
        # the old private state and mistakes itself for a child stage.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen(
            ["cmd", "/c", bat], env=env,
            creationflags=_CREATE_NO_WINDOW | _NEW_GROUP, close_fds=True)
    except Exception:
        try:
            os.remove(bat)
        except OSError:
            pass
        raise
