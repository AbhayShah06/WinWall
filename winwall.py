"""
WinWall CLI -- run the live wallpaper from the terminal.

    python winwall.py "C:\\path\\to\\video.mp4"      run until Ctrl+C
    python winwall.py --selftest "C:\\...\\video.mp4" run ~6s, verify embedding

The GUI app is app.py.
"""

import sys
import time

import engine


def main():
    args = [a for a in sys.argv[1:]]
    selftest = "--selftest" in args
    args = [a for a in args if a != "--selftest"]
    if not args:
        print('Usage: python winwall.py "C:\\path\\to\\video.mp4"')
        return 1

    eng = engine.WallpaperEngine()
    eng.start(args[0])

    if selftest:
        time.sleep(5)
        floats = engine.floating_mpv_windows()
        if floats:
            print(f"SELFTEST: FAIL -- {len(floats)} floating mpv window(s): {floats}")
        else:
            print("SELFTEST: OK -- no floating mpv window; embedded as wallpaper")
        eng.stop()
        return 0 if not floats else 2

    print("WinWall running. Press Ctrl+C to stop.")
    try:
        while eng.is_running():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
        eng.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
