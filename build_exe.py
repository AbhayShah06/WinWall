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
    for c in (r"C:\Program Files\MPV Player\mpv.exe",
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
        "--add-data", "%s%sassets" % (os.path.join(HERE, "assets"), os.pathsep),
        "--add-binary", "%s%s." % (mpv, os.pathsep),   # mpv.exe at bundle root
        "--hidden-import", "PySide6.QtMultimedia",
        "--hidden-import", "PySide6.QtMultimediaWidgets",
        "--hidden-import", "PySide6.QtNetwork",
        os.path.join(HERE, "app.py"),
    ]
    print("Running PyInstaller...")
    rc = subprocess.call(args, cwd=HERE)
    if rc == 0:
        print("\nDone -> dist\\WinWall.exe")
    return rc


if __name__ == "__main__":
    sys.exit(main())
