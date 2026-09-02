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
import tempfile
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
STM_SETIMAGE = 0x0172
IMAGE_BITMAP = 0
SS_BITMAP = 0x0E
SS_REALSIZECONTROL = 0x40
UNLOAD_DELAY = 2.5  # seconds the desktop must stay covered before unloading mpv

_gdip_token = None


def _gdip_start():
    global _gdip_token
    if _gdip_token is not None:
        return
    class _GSI(ctypes.Structure):
        _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                    ("DebugEventCallback", ctypes.c_void_p),
                    ("SuppressBackgroundThread", ctypes.c_int),
                    ("SuppressExternalCodecs", ctypes.c_int)]
    token = ctypes.c_void_p()
    si = _GSI()
    si.GdiplusVersion = 1
    ctypes.windll.gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(si), None)
    _gdip_token = token


def _load_hbitmap(path):
    """Load a PNG/JPG file into a GDI HBITMAP (via GDI+)."""
    try:
        _gdip_start()
        img = ctypes.c_void_p()
        if ctypes.windll.gdiplus.GdipCreateBitmapFromFile(ctypes.c_wchar_p(path),
                                                          ctypes.byref(img)) != 0:
            return None
        hbmp = ctypes.c_void_p()
        ctypes.windll.gdiplus.GdipCreateHBITMAPFromBitmap(img, ctypes.byref(hbmp),
                                                          ctypes.c_uint(0xFF000000))
        ctypes.windll.gdiplus.GdipDisposeImage(img)
        return hbmp.value
    except Exception:
        return None

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


_frame_bmps = {}  # frame-window hwnd -> HBITMAP of the last captured frame

_gdi = ctypes.windll.gdi32
_gdi.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi.SelectObject.restype = ctypes.c_void_p
_gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi.DeleteDC.argtypes = [ctypes.c_void_p]
_gdi.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
_gdi.StretchBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_gdi.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                ("bmBits", ctypes.c_void_p)]


def _paint_frame(hwnd, hbmp):
    hdc, ps = win32gui.BeginPaint(hwnd)
    try:
        rect = win32gui.GetClientRect(hwnd)
        cw, ch = rect[2] - rect[0], rect[3] - rect[1]
        bm = _BITMAP()
        _gdi.GetObjectW(ctypes.c_void_p(hbmp), ctypes.sizeof(bm), ctypes.byref(bm))
        memdc = _gdi.CreateCompatibleDC(ctypes.c_void_p(hdc))
        old = _gdi.SelectObject(memdc, ctypes.c_void_p(hbmp))
        _gdi.SetStretchBltMode(ctypes.c_void_p(hdc), 4)  # HALFTONE
        _gdi.StretchBlt(ctypes.c_void_p(hdc), 0, 0, cw, ch, memdc, 0, 0,
                        bm.bmWidth or cw, bm.bmHeight or ch, 0x00CC0020)  # SRCCOPY
        _gdi.SelectObject(memdc, old)
        _gdi.DeleteDC(memdc)
    finally:
        win32gui.EndPaint(hwnd, ps)


def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == win32con.WM_PAINT and hwnd in _frame_bmps:
        try:
            _paint_frame(hwnd, _frame_bmps[hwnd])
        except Exception:
            pass
        return 0
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
    def __init__(self, pause_on_battery=False, poll_interval=1.0, log=print,
                 unload_when_covered=True):
        self.pause_on_battery = pause_on_battery
        self.poll_interval = poll_interval
        self.log = log
        # Free mpv's RAM (~150MB) while the desktop is covered; a captured
        # freeze-frame is shown so the return to the desktop is instant.
        self.unload_when_covered = unload_when_covered
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
            # Freeze-frame window: a STATIC child of the wallpaper host that shows
            # a captured frame while mpv is unloaded. Created top-level then
            # reparented (same reason the video window is), hidden while playing.
            frame_hwnd = win32gui.CreateWindowEx(
                WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, _ATOM, None, win32con.WS_POPUP,
                OFFSCREEN, OFFSCREEN, w, h, 0, 0, _HINST, None)
            try:
                win32gui.SetParent(frame_hwnd, host)
                win32gui.SetWindowLong(frame_hwnd, GWL_STYLE, win32con.WS_CHILD)
                win32gui.SetWindowPos(frame_hwnd, win32con.HWND_BOTTOM, l - host_l, t - host_t,
                                      w, h, win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED)
                win32gui.ShowWindow(frame_hwnd, win32con.SW_HIDE)
            except Exception:
                pass
            p = {"monitor": (hmon, rect), "index": i,
                 "host": host, "host_l": host_l, "host_t": host_t,
                 "mpv_exe": mpv_exe, "video": video,
                 "pipe_name": r"\\.\pipe\winwall-%d" % i,
                 "frame_hwnd": frame_hwnd,
                 "frame_path": os.path.join(tempfile.gettempdir(), "winwall-frame-%d.png" % i),
                 "frame_bmp": None, "video_hwnd": 0, "proc": None, "pipe": None,
                 "loaded": False, "paused": False, "covered_since": None}
            if self._start_video(p):
                players.append(p)
                self.log(f"[engine] monitor {hmon}: {w}x{h} embedded")
            else:
                try:
                    win32gui.DestroyWindow(frame_hwnd)
                except Exception:
                    pass

        if not players:
            self.log("[engine] nothing started")
            return
        self.log(f"[engine] running on {len(players)} monitor(s)")

        try:
            while not self._stop.is_set():
                win32gui.PumpWaitingMessages()
                self._update_power(players)
                time.sleep(self.poll_interval)
        finally:
            for p in players:
                if p.get("pipe"):
                    _ipc_send(p["pipe"], ["quit"])
            time.sleep(0.2)
            for p in players:
                try:
                    if p.get("proc"):
                        p["proc"].terminate()
                except Exception:
                    pass
                for key in ("video_hwnd", "frame_hwnd"):
                    try:
                        if p.get(key):
                            win32gui.DestroyWindow(p[key])
                    except Exception:
                        pass
                bmp = _frame_bmps.pop(p.get("frame_hwnd"), None)
                if bmp:
                    try:
                        win32gui.DeleteObject(bmp)
                    except Exception:
                        pass
            self.log("[engine] stopped")

    def _start_video(self, p):
        """Create a fresh window, embed mpv, reparent it behind the icons."""
        hmon, rect = p["monitor"]
        l, t, r, b = rect
        w, h = r - l, b - t
        host, host_l, host_t = p["host"], p["host_l"], p["host_t"]
        hwnd = win32gui.CreateWindowEx(
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, _ATOM, None, win32con.WS_POPUP,
            OFFSCREEN, OFFSCREEN, w, h, 0, 0, _HINST, None)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        try:
            proc = _launch_mpv(p["mpv_exe"], p["video"], hwnd, p["pipe_name"])
        except Exception as e:
            self.log(f"[engine] mpv launch failed: {e}")
            try:
                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass
            return False
        for _ in range(40):
            if win32gui.FindWindowEx(hwnd, 0, None, None):
                break
            if self._stop.is_set():
                break
            time.sleep(0.1)
        try:
            win32gui.SetParent(hwnd, host)
            win32gui.SetWindowLong(hwnd, GWL_STYLE, win32con.WS_CHILD | win32con.WS_VISIBLE)
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_BOTTOM, l - host_l, t - host_t, w, h,
                win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW)
        except Exception as e:
            self.log(f"[engine] reparent failed: {e}")
        p["video_hwnd"] = hwnd
        p["proc"] = proc
        p["pipe"] = _ipc_connect(p["pipe_name"])
        _ipc_send(p["pipe"], ["set_property", "panscan", 1.0])
        _ipc_send(p["pipe"], ["set_property", "keepaspect", True])
        p["loaded"] = True
        p["paused"] = False
        try:  # video is up: hide the freeze-frame beneath it
            win32gui.ShowWindow(p["frame_hwnd"], win32con.SW_HIDE)
        except Exception:
            pass
        return True

    def _freeze(self, p):
        """Capture the current frame, show it, then unload mpv to free RAM."""
        try:
            if os.path.exists(p["frame_path"]):
                os.remove(p["frame_path"])
        except Exception:
            pass
        _ipc_send(p["pipe"], ["screenshot-to-file", p["frame_path"], "video"])
        for _ in range(20):
            if os.path.exists(p["frame_path"]) and os.path.getsize(p["frame_path"]) > 0:
                break
            time.sleep(0.05)
        hbmp = _load_hbitmap(p["frame_path"]) if os.path.exists(p["frame_path"]) else None
        if hbmp:
            old = _frame_bmps.get(p["frame_hwnd"])
            _frame_bmps[p["frame_hwnd"]] = hbmp
            try:
                win32gui.ShowWindow(p["frame_hwnd"], win32con.SW_SHOWNOACTIVATE)
                win32gui.SetWindowPos(
                    p["frame_hwnd"], win32con.HWND_BOTTOM, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
                win32gui.InvalidateRect(p["frame_hwnd"], None, True)
                win32gui.UpdateWindow(p["frame_hwnd"])
            except Exception:
                pass
            if old:
                try:
                    win32gui.DeleteObject(old)
                except Exception:
                    pass
        _ipc_send(p["pipe"], ["quit"])
        time.sleep(0.15)
        try:
            if p.get("proc"):
                p["proc"].terminate()
        except Exception:
            pass
        try:
            if p.get("video_hwnd"):
                win32gui.DestroyWindow(p["video_hwnd"])
        except Exception:
            pass
        p["video_hwnd"] = 0
        p["proc"] = None
        p["pipe"] = None
        p["loaded"] = False
        p["paused"] = False
        self.log(f"[engine] monitor {p['monitor'][0]}: unloaded mpv (freed RAM), freeze-frame shown")

    def _update_power(self, players):
        if os.environ.get("WINWALL_NOPAUSE"):
            return
        battery_off = self.pause_on_battery and on_battery()
        fg = win32gui.GetForegroundWindow()
        fg_mon = 0
        if fg:
            try:
                fg_mon = int(win32api.MonitorFromWindow(fg, win32con.MONITOR_DEFAULTTONEAREST))
            except Exception:
                fg_mon = 0
        now = time.time()
        for p in players:
            hmon, rect = p["monitor"]
            covered = self.unload_when_covered and (hmon == fg_mon) and _covers(fg, rect)
            if covered:
                if p["covered_since"] is None:
                    p["covered_since"] = now
            else:
                p["covered_since"] = None
            off = battery_off or (covered and (now - (p["covered_since"] or now)) >= UNLOAD_DELAY)
            if p["loaded"]:
                if off:
                    self._freeze(p)
                elif covered != p["paused"]:  # covered but within grace: pause to save CPU
                    p["paused"] = covered
                    _ipc_send(p["pipe"], ["set_property", "pause", covered])
            elif not off:
                self._start_video(p)
