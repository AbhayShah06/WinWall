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
        if a.get("name", "").lower().endswith(".exe"):
            return {"version": tag, "url": a["browser_download_url"],
                    "size": int(a.get("size", 0)), "notes": data.get("body", "") or ""}
    return None


def download(url, dest, expected_size=0, progress=None):
    """Download to dest; verify size. Returns True on a complete, valid file."""
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "WinWall"})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length", expected_size or 0))
        got = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(got, total)
    # verify: non-trivial size, and matches expected/content-length if known
    size = os.path.getsize(tmp)
    if size < 1_000_000 or (expected_size and size != expected_size):
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
    return True


def apply_and_relaunch(new_exe, target_exe):
    """Write + launch a detached script that swaps the exe and relaunches."""
    bak = target_exe + ".bak"
    name = os.path.basename(target_exe)
    bat = os.path.join(tempfile.gettempdir(), "winwall_update.bat")
    script = (
        "@echo off\r\n"
        "setlocal\r\n"
        ":wait\r\n"
        'tasklist /fi "imagename eq %s" 2>nul | find /i "%s" >nul\r\n' % (name, name) +
        "if not errorlevel 1 (\r\n"
        "  ping -n 2 127.0.0.1 >nul\r\n"
        "  goto wait\r\n"
        ")\r\n"
        'if exist "%s" del "%s" >nul 2>&1\r\n' % (bak, bak) +
        'move /y "%s" "%s" >nul 2>&1\r\n' % (target_exe, bak) +
        'move /y "%s" "%s" >nul 2>&1\r\n' % (new_exe, target_exe) +
        'if not exist "%s" move /y "%s" "%s" >nul 2>&1\r\n' % (target_exe, bak, target_exe) +
        'start "" "%s" --tray\r\n' % target_exe +
        'ping -n 3 127.0.0.1 >nul\r\n'
        'del "%s" >nul 2>&1\r\n' % bak +
        'del "%%~f0" >nul 2>&1\r\n'
    )
    with open(bat, "w") as f:
        f.write(script)
    subprocess.Popen(["cmd", "/c", bat], creationflags=_CREATE_NO_WINDOW | _NEW_GROUP,
                     close_fds=True)
