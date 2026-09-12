"""
Build a standalone WinWall.exe with PyInstaller.

Bundles Python, PySide6, and mpv.exe into a single file, so it runs on a
Windows PC with nothing installed. Output: dist/WinWall.exe
"""

import os
import sys
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def find_mpv():
    for c in (os.path.join(HERE, "resources", "bin", "mpv.exe"),
              r"C:\Program Files\MPV Player\mpv.exe",
              r"C:\Program Files\mpv\mpv.exe",
              shutil.which("mpv")):
        if c and os.path.isfile(c):
            return c
    return None


def main():
    mpv = find_mpv()
    if not mpv:
        print("mpv.exe not found. Install it first:  winget install shinchiro.mpv")
        return 1
    print("Bundling mpv from:", mpv)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--windowed",
        "--name", "WinWall",
        "--icon", os.path.join(HERE, "assets", "icon.ico"),
        "--version-file", os.path.join(HERE, "version_info.txt"),
        "--add-data", "%s%sassets" % (os.path.join(HERE, "assets"), os.pathsep),
        "--add-binary", "%s%s." % (mpv, os.pathsep),   # mpv.exe at bundle root
        # The packaged app keeps Browse lightweight: thumbnails are included,
        # while the large QtMultimedia hover-preview backend is omitted.
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "PySide6.QtMultimediaWidgets",
        "--hidden-import", "PySide6.QtNetwork",
        "--hidden-import", "win32com.client",
        "--hidden-import", "pythoncom",
        "--hidden-import", "pywintypes",
        os.path.join(HERE, "app.py"),
    ]
    print("Running PyInstaller...")
    # Build with a clean DLL search path. Developer shells often add unrelated
    # tools (Poppler, Git, Node, etc.) whose DLLs PyInstaller may accidentally
    # collect. A mismatched ICU DLL, for example, prevents Qt from starting.
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(filter(None, [
        os.path.dirname(sys.executable),
        os.path.dirname(mpv),
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
        os.environ.get("SystemRoot", r"C:\Windows"),
    ]))
    rc = subprocess.call(args, cwd=HERE, env=env)
    if rc == 0:
        print("\nDone -> dist\\WinWall.exe")
    return rc


if __name__ == "__main__":
    sys.exit(main())
