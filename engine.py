"""
engine.py -- WinWall live-wallpaper engine.

Renders a looping video as the desktop wallpaper on all monitors using mpv
(hardware decode), and pauses when the wallpaper is covered or (optionally) on
battery. Runs in a background thread so a GUI can drive it.

Embedding technique (important for Windows 11 24H2+ / build 2620x):
  mpv's `--wid` refuses to embed into a cross-process child of Progman, so we
  1) create our OWN borderless top-level window (off-screen),
  2) launch mpv embedded into it via `--wid` (works: same process, top-level),
  3) reparent that window behind the desktop icons and size it to the monitor.
This is the same approach Lively Wallpaper uses.
"""

import os
import sys
import json
import time
import ctypes
import threading
import subprocess

import win32api
import win32con
import win32gui
import win32file

# --- config (overridable by the app) ---------------------------------------
MPV_PATH = r"C:\Program Files\MPV Player\mpv.exe"
HWDEC = "auto"

CREATE_NO_WINDOW = 0x08000000
WNDCLASS_NAME = "WinWallChild"
SPAWN_WORKERW_MSG = 0x052C
OFFSCREEN = -32000

# extended/style bits not always in win32con
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
GWL_STYLE = -16

_HINST = None
_ATOM = 0
_WC_REF = None
_dpi_set = False


# --- one-time setup ---------------------------------------------------------
def _set_dpi_awareness():
    global _dpi_set
    if _dpi_set:
        return
    for fn in (
        lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
        lambda: ctypes.windll.user32.SetProcessDPIAware(),
    ):
        try:
            fn()
            break
        except Exception:
            continue
    _dpi_set = True


def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == win32con.WM_ERASEBKGND:
        return 1
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def _register_class():
    global _HINST, _ATOM, _WC_REF
    if _ATOM:
        return _ATOM
    _HINST = win32api.GetModuleHandle(None)
    wc = win32gui.WNDCLASS()
    wc.hInstance = _HINST
    wc.lpszClassName = WNDCLASS_NAME
    wc.lpfnWndProc = _wnd_proc
    wc.hbrBackground = win32gui.CreateSolidBrush(0x000000)
    # NB: do NOT set wc.hCursor -- some Win11 builds reject it (RegisterClass 87).
    _WC_REF = wc
    _ATOM = win32gui.RegisterClass(wc)
    return _ATOM


# --- battery ----------------------------------------------------------------
class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def on_battery():
    s = _PowerStatus()
    if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return s.ACLineStatus == 0
    return False


# --- desktop host -----------------------------------------------------------
def _screen_area():
    return win32api.GetSystemMetrics(78) * win32api.GetSystemMetrics(79)


def get_wallpaper_host(debug=False):
    override = os.environ.get("WINWALL_HOST", "").strip().lower()
    progman = win32gui.FindWindow("Progman", None)
    if progman:
        try:
            win32gui.SendMessageTimeout(progman, SPAWN_WORKERW_MSG, 0, 0, 0, 1000)
        except Exception:
            pass
    if override == "progman" and progman:
        return progman
    if override.isdigit():
        return int(override)

    # Method A: WorkerW sibling of the DefView host (Win10 / many Win11).
    found = {"w": 0}

    def _enum(hwnd, _):
        if win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None):
            w = win32gui.FindWindowEx(0, hwnd, "WorkerW", None)
            if w:
                found["w"] = w
        return True

    win32gui.EnumWindows(_enum, None)
    if found["w"]:
        return found["w"]

    # Method A2 (Win11 24H2+): the wallpaper is painted by a WorkerW that is a
    # CHILD of Progman. Parent INTO it so our video draws over the wallpaper,
    # with the (transparent) icon layer staying on top.
    if progman:
        child_worker = win32gui.FindWindowEx(progman, 0, "WorkerW", None)
        if child_worker:
            return child_worker

    # Method B: a screen-covering top-level WorkerW without the icon layer.
    cands = []

    def _cb(hwnd, _):
        try:
            if win32gui.GetClassName(hwnd) == "WorkerW":
                has_def = bool(win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None))
                cands.append((hwnd, has_def, bool(win32gui.IsWindowVisible(hwnd)),
                              win32gui.GetWindowRect(hwnd)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    if debug:
        for c in cands:
            print("[host] cand", c)
    screen = _screen_area()
    covering = [c for c in cands if not c[1]
                and (c[3][2] - c[3][0]) * (c[3][3] - c[3][1]) >= screen * 0.9]
    covering.sort(key=lambda c: c[2], reverse=True)
    if covering:
        return covering[0][0]

    return progman or 0


def floating_mpv_windows():
    """Top-level VISIBLE real mpv player windows (a sign embedding failed)."""
    out = []

    def cb(h, _):
        try:
            cls = win32gui.GetClassName(h)
        except Exception:
            return True
        if cls == "mpv" and win32gui.IsWindowVisible(h):
            out.append((h, win32gui.GetWindowRect(h)))
        return True

    win32gui.EnumWindows(cb, None)
    return out


# --- mpv --------------------------------------------------------------------
def find_mpv():
    cands = [MPV_PATH]
    base = getattr(sys, "_MEIPASS", None)   # bundled inside a PyInstaller build
    if base:
        cands.append(os.path.join(base, "mpv.exe"))
    cands.append(os.path.join(os.path.dirname(sys.executable), "mpv.exe"))
    cands += [r"C:\Program Files\MPV Player\mpv.exe", r"C:\Program Files\mpv\mpv.exe"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _launch_mpv(mpv_exe, video, hwnd, pipe):
    args = [
        mpv_exe, video,
        f"--wid={int(hwnd)}",
        "--loop-file=inf", "--no-audio", f"--hwdec={HWDEC}", "--vo=gpu",
        "--panscan=1.0", "--no-osc", "--no-border",
        "--no-input-default-bindings", "--input-vo-keyboard=no",
        "--cursor-autohide=no", "--no-config", "--really-quiet", "--idle=no",
        # keep memory lean: no big read-ahead cache for a short local loop
        "--cache=no", "--demuxer-max-bytes=16MiB", "--demuxer-max-back-bytes=8MiB",
        f"--input-ipc-server={pipe}",
    ]
    return subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)


def _ipc_connect(pipe, attempts=60):
    for _ in range(attempts):
        try:
            return win32file.CreateFile(
                pipe, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None)
        except Exception:
            time.sleep(0.1)
    return None


def _ipc_send(handle, command):
    if not handle:
        return
    try:
        win32file.WriteFile(handle, (json.dumps({"command": command}) + "\n").encode("utf-8"))
    except Exception:
        pass


# --- coverage ---------------------------------------------------------------
_IGNORE = {"Progman", "WorkerW", "Shell_TrayWnd", WNDCLASS_NAME}


def _covers(hwnd, mon_rect):
    if not hwnd:
        return False
    try:
        if win32gui.GetClassName(hwnd) in _IGNORE:
            return False
    except Exception:
        return False
    try:
        if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED:
            return True
    except Exception:
        pass
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
    except Exception:
        return False
    ml, mt, mr, mb = mon_rect
    return l <= ml and t <= mt and r >= mr and b >= mb


def _enum_monitors():
    return [(int(hmon), tuple(rect)) for hmon, _h, rect in win32api.EnumDisplayMonitors()]


def resolve_video(path):
    path = os.path.abspath(path)
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.lower().endswith((".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".gif")):
                return os.path.join(path, name)
        raise FileNotFoundError(f"No video in folder: {path}")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    return path


# --- engine -----------------------------------------------------------------
class WallpaperEngine:
    def __init__(self, pause_on_battery=False, poll_interval=1.0, log=print):
        self.pause_on_battery = pause_on_battery
        self.poll_interval = poll_interval
        self.log = log
        self._thread = None
        self._stop = threading.Event()
        self._current = None

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def current(self):
        return self._current

    def start(self, video):
        self.stop()
        video = resolve_video(video)
        self._current = video
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(video,), daemon=True)
        self._thread.start()

    def stop(self):
        if self.is_running():
            self._stop.set()
            self._thread.join(timeout=6)
        self._thread = None
        self._current = None

    # runs on its own thread
    def _run(self, video):
        _set_dpi_awareness()
        mpv_exe = find_mpv()
        if not mpv_exe:
            self.log("[engine] mpv.exe not found")
            return
        _register_class()

        host = get_wallpaper_host()
        if not host:
            self.log("[engine] no desktop host window")
            return
        host_l, host_t, _, _ = win32gui.GetWindowRect(host)

        players = []
        for i, (hmon, rect) in enumerate(_enum_monitors()):
            l, t, r, b = rect
            w, h = r - l, b - t
            # 1) our own borderless top-level window, created off-screen
            hwnd = win32gui.CreateWindowEx(
                WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, _ATOM, None, win32con.WS_POPUP,
                OFFSCREEN, OFFSCREEN, w, h, 0, 0, _HINST, None)
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            # 2) embed mpv into it
            pipe = r"\\.\pipe\winwall-%d" % i
            try:
                proc = _launch_mpv(mpv_exe, video, hwnd, pipe)
            except Exception as e:
                self.log(f"[engine] mpv launch failed: {e}")
                continue
            # wait until mpv creates its render child inside our window
            for _ in range(40):
                if win32gui.FindWindowEx(hwnd, 0, None, None):
                    break
                if self._stop.is_set():
                    break
                time.sleep(0.1)
            # 3) reparent behind the icons and size to the monitor
            try:
                win32gui.SetParent(hwnd, host)
                win32gui.SetWindowLong(hwnd, GWL_STYLE, win32con.WS_CHILD | win32con.WS_VISIBLE)
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_BOTTOM, l - host_l, t - host_t, w, h,
                    win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW)
            except Exception as e:
                self.log(f"[engine] reparent failed: {e}")
            handle = _ipc_connect(pipe)
            # enforce fill-the-screen (crop overflow) at runtime -- the launch
            # flag alone doesn't always take once the window is reparented/resized
            _ipc_send(handle, ["set_property", "panscan", 1.0])
            _ipc_send(handle, ["set_property", "keepaspect", True])
            players.append({"proc": proc, "hwnd": hwnd, "monitor": (hmon, rect),
                            "pipe": handle, "paused": False})
            self.log(f"[engine] monitor {hmon}: {w}x{h} embedded"
                     f"{'' if handle else ' (no ipc)'}")

        if not players:
            self.log("[engine] nothing started")
            return
        self.log(f"[engine] running on {len(players)} monitor(s)")

        try:
            while not self._stop.is_set():
                win32gui.PumpWaitingMessages()
                self._update_pause(players)
                time.sleep(self.poll_interval)
        finally:
            for p in players:
                _ipc_send(p["pipe"], ["quit"])
            time.sleep(0.2)
            for p in players:
                try:
                    p["proc"].terminate()
                except Exception:
                    pass
                try:
                    win32gui.DestroyWindow(p["hwnd"])
                except Exception:
                    pass
            self.log("[engine] stopped")

    def _update_pause(self, players):
        if os.environ.get("WINWALL_NOPAUSE"):
            return  # testing: keep playing even when covered
        batt = self.pause_on_battery and on_battery()
        fg = win32gui.GetForegroundWindow()
        fg_mon = 0
        if fg:
            try:
                fg_mon = int(win32api.MonitorFromWindow(fg, win32con.MONITOR_DEFAULTTONEAREST))
            except Exception:
                fg_mon = 0
        for p in players:
            hmon, rect = p["monitor"]
            covered = (hmon == fg_mon) and _covers(fg, rect)
            want = batt or covered
            if want != p["paused"]:
                p["paused"] = want
                _ipc_send(p["pipe"], ["set_property", "pause", want])
