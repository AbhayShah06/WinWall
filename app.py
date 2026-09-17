"""
WinWall -- desktop app.

A windowed library + shop for live wallpapers:
  * My Library: the videos in ./library (auto thumbnails); click to apply live
  * Browse: genres + search from shop.com, hover to preview, download & apply
  * add your own videos, pause-on-battery toggle, launch-on-startup toggle
  * lives in the system tray; remembers your last wallpaper

Run:  pythonw app.py     (no console)   or   python app.py
"""

import os
import sys
import json
import shutil
import winreg
import subprocess
import urllib.parse

from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

# QtMultimedia (used only for hover previews in Browse) pulls in a heavy media
# backend, so it is imported lazily on first use -- see WinWall._ensure_media.
# Sessions that stay in the tray never load it.

import engine
import shop
import updater

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Wallpapers + settings live in a hidden per-user folder (%LOCALAPPDATA%\WinWall),
# the same whether run from source or as the packaged .exe. Not in the project.
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "WinWall")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass
LIBRARY_DIR = os.path.join(DATA_DIR, "library")
THUMB_DIR = os.path.join(DATA_DIR, "thumbnails")
SETTINGS = os.path.join(DATA_DIR, "settings.json")
VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".gif")
CREATE_NO_WINDOW = 0x08000000
ACCENT = "#7c5cff"
VERSION = "1.0.4"
SELFTEST = "--selftest" in sys.argv or bool(os.environ.get("WINWALL_SELFTEST"))


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def load_settings():
    try:
        with open(SETTINGS, "r", encoding="utf-8-sig") as f:  # tolerate BOM
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    tmp = SETTINGS + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SETTINGS)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


def trim_memory():
    """Ask Windows to trim our working set back to its minimum. Called when the
    app goes idle in the tray so the footprint drops (pages fault back in on the
    next interaction). This is what well-behaved background apps do."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetProcessWorkingSetSize(k32.GetCurrentProcess(),
                                     ctypes.c_size_t(-1), ctypes.c_size_t(-1))
    except Exception:
        pass


def migrate_old_data():
    """One time: move data from an older in-project folder into DATA_DIR."""
    old = APP_DIR
    if os.path.abspath(old) == os.path.abspath(DATA_DIR):
        return
    try:
        old_lib = os.path.join(old, "library")
        if os.path.isdir(old_lib):
            os.makedirs(LIBRARY_DIR, exist_ok=True)
            for f in os.listdir(old_lib):
                dst = os.path.join(LIBRARY_DIR, f)
                if os.path.exists(dst):
                    continue
                try:
                    shutil.move(os.path.join(old_lib, f), dst)
                except Exception:
                    try:
                        shutil.copy2(os.path.join(old_lib, f), dst)
                    except Exception:
                        pass
        old_thumbs = os.path.join(old, "thumbnails")
        if os.path.isdir(old_thumbs):
            os.makedirs(THUMB_DIR, exist_ok=True)
            for f in os.listdir(old_thumbs):
                dst = os.path.join(THUMB_DIR, f)
                if not os.path.exists(dst):
                    try:
                        shutil.copy2(os.path.join(old_thumbs, f), dst)
                    except Exception:
                        pass
        old_set = os.path.join(old, "settings.json")
        if os.path.isfile(old_set) and not os.path.isfile(SETTINGS):
            with open(old_set, encoding="utf-8-sig") as f:
                s = json.load(f)
            if s.get("last_wallpaper"):
                s["last_wallpaper"] = os.path.join(LIBRARY_DIR,
                                                   os.path.basename(s["last_wallpaper"]))
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
    except Exception as e:
        print("migrate error:", e)


# --------------------------------------------------------------------------- #
# launch on Windows startup (HKCU Run key)
# --------------------------------------------------------------------------- #
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup\WinWall.lnk")


def _startup_target():
    if getattr(sys, "frozen", False):
        return sys.executable, "--tray"
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return exe, '"%s" --tray' % os.path.join(APP_DIR, "app.py")


def startup_enabled():
    return os.path.exists(STARTUP_LNK)


def set_startup(enable):
    try:
        # remove any legacy Run-key entry so there is only one startup method
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "WinWall")
        except Exception:
            pass
        if enable:
            import win32com.client
            target, args = _startup_target()
            sh = win32com.client.Dispatch("WScript.Shell")
            lnk = sh.CreateShortcut(STARTUP_LNK)
            lnk.TargetPath = target
            lnk.Arguments = args
            lnk.WorkingDirectory = os.path.dirname(target)
            lnk.IconLocation = target + ",0"
            lnk.Save()
        elif os.path.exists(STARTUP_LNK):
            os.remove(STARTUP_LNK)
    except Exception as e:
        print("startup error:", e)


# --------------------------------------------------------------------------- #
# thumbnails
# --------------------------------------------------------------------------- #
def thumb_path(video):
    stem = os.path.splitext(os.path.basename(video))[0]
    return os.path.join(THUMB_DIR, stem + ".jpg")


def make_thumbnail(mpv, video, out):
    os.makedirs(THUMB_DIR, exist_ok=True)
    base = ["--no-config", "--really-quiet", "--no-audio", "--start=10%",
            "--frames=1", "--vf=scale=520:-2"]
    try:
        subprocess.run([mpv, video, *base, f"--o={out}"],
                       timeout=25, creationflags=CREATE_NO_WINDOW)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return True
    except Exception:
        pass
    try:
        d = out + "_d"
        os.makedirs(d, exist_ok=True)
        subprocess.run([mpv, video, *base, "--vo=image", "--vo-image-format=jpg",
                        f"--vo-image-outdir={d}"], timeout=25, creationflags=CREATE_NO_WINDOW)
        imgs = [f for f in os.listdir(d) if f.lower().endswith((".jpg", ".png"))]
        if imgs:
            shutil.move(os.path.join(d, imgs[0]), out)
            shutil.rmtree(d, ignore_errors=True)
            return True
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
    return False


class ThumbWorker(QtCore.QThread):
    done = QtCore.Signal(str, str)

    def __init__(self, mpv, videos):
        super().__init__()
        self.mpv = mpv
        self.videos = videos

    def run(self):
        for v in self.videos:
            out = thumb_path(v)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                self.done.emit(v, out)
                continue
            ok = make_thumbnail(self.mpv, v, out) if self.mpv else False
            self.done.emit(v, out if ok else "")


class ApplyWorker(QtCore.QThread):
    finished_apply = QtCore.Signal(str)

    def __init__(self, eng, video):
        super().__init__()
        self.eng = eng
        self.video = video

    def run(self):
        try:
            self.eng.start(self.video)
        except Exception as e:
            print("apply error:", e)
        self.finished_apply.emit(self.video)


# --------------------------------------------------------------------------- #
# screen size + filename helpers
# --------------------------------------------------------------------------- #
SCREEN_W = 1920  # replaced at startup with the real primary-monitor width


def detect_screen_width():
    global SCREEN_W
    # EnumDisplaySettings reports the true physical mode regardless of DPI.
    try:
        import win32api
        import win32con
        s = win32api.EnumDisplaySettings(None, win32con.ENUM_CURRENT_SETTINGS)
        if s and s.PelsWidth:
            SCREEN_W = int(s.PelsWidth)
            return
    except Exception:
        pass
    try:
        import win32api
        mons = win32api.EnumDisplayMonitors()
        for _h, _d, r in mons:
            if r[0] <= 0 < r[2] and r[1] <= 0 < r[3]:
                SCREEN_W = r[2] - r[0]
                return
    except Exception:
        pass


def safe_filename(name):
    out = "".join(c for c in name if c not in '<>:"/\\|?*').strip()
    return out[:80] or "wallpaper"


# --------------------------------------------------------------------------- #
# workers: download a wallpaper, and browse shop (the shop)
# --------------------------------------------------------------------------- #
class DownloadWorker(QtCore.QThread):
    done = QtCore.Signal(str)
    error = QtCore.Signal(str)
    progress = QtCore.Signal(int)

    def __init__(self, url, dest):
        super().__init__()
        self.url = url
        self.dest = dest

    def run(self):
        try:
            shop.download(self.url, self.dest,
                          lambda g, t: self.progress.emit(int(g * 100 / t)))
            self.done.emit(self.dest)
        except Exception as e:
            self.error.emit(str(e))


class MotionWorker(QtCore.QThread):
    result = QtCore.Signal(dict, str)   # item, thumb_path ("" if none)
    error = QtCore.Signal(str)
    done = QtCore.Signal(int)

    def __init__(self, kind, arg):
        super().__init__()
        self.kind = kind   # "genre" or "search"
        self.arg = arg

    def run(self):
        try:
            items = (shop.genre(self.arg) if self.kind == "genre"
                     else shop.search(self.arg))
        except Exception as e:
            self.error.emit("Could not reach the wallpaper service: %s" % e)
            return
        os.makedirs(THUMB_DIR, exist_ok=True)
        for it in items:
            thumb = os.path.join(THUMB_DIR, "mbg_%s.jpg" % it["id"])
            if not (os.path.exists(thumb) and os.path.getsize(thumb) > 0):
                try:
                    with open(thumb, "wb") as fp:
                        fp.write(shop.http_bytes(it["thumb"]))
                except Exception:
                    thumb = ""
            self.result.emit(it, thumb)
        self.done.emit(len(items))


class UpdateCheckWorker(QtCore.QThread):
    found = QtCore.Signal(dict)

    def run(self):
        try:
            info = updater.check(VERSION)
            if info:
                self.found.emit(info)
        except Exception as e:
            print("update check:", e)


class UpdateDownloadWorker(QtCore.QThread):
    progress = QtCore.Signal(int)
    phase = QtCore.Signal(str)
    done = QtCore.Signal(str)
    error = QtCore.Signal(str)

    def __init__(self, url, dest, size, digest=""):
        super().__init__()
        self.url = url
        self.dest = dest
        self.size = size
        self.digest = digest

    def run(self):
        try:
            ok = updater.download(self.url, self.dest, self.size, self.digest,
                                  lambda g, t: self.progress.emit(int(g * 100 / t)),
                                  self.phase.emit)
            if ok:
                self.done.emit(self.dest)
            else:
                self.error.emit("Downloaded file failed verification.")
        except Exception as e:
            self.error.emit(str(e))


# --------------------------------------------------------------------------- #
# icons
# --------------------------------------------------------------------------- #
def play_badge_icon(thumb):
    if thumb and os.path.exists(thumb):
        pm = QtGui.QPixmap(thumb)
    else:
        pm = QtGui.QPixmap()
    if pm.isNull():
        pm = QtGui.QPixmap(260, 150)
        pm.fill(QtGui.QColor("#20222b"))
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    w, h = pm.width(), pm.height()
    cx, cy = w / 2, h / 2
    r = min(w, h) * 0.16
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor(0, 0, 0, 120))
    p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
    p.setBrush(QtGui.QColor(255, 255, 255, 235))
    tri = QtGui.QPolygonF([QtCore.QPointF(cx - r * 0.32, cy - r * 0.5),
                           QtCore.QPointF(cx - r * 0.32, cy + r * 0.5),
                           QtCore.QPointF(cx + r * 0.52, cy)])
    p.drawPolygon(tri)
    p.end()
    return QtGui.QIcon(pm)


def placeholder_icon(name):
    pm = QtGui.QPixmap(260, 150)
    pm.fill(QtGui.QColor("#20222b"))
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtGui.QColor("#8a8f9c"))
    p.setFont(QtGui.QFont("Segoe UI", 10))
    p.drawText(pm.rect().adjusted(10, 10, -10, -10),
               QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap, name)
    p.end()
    return QtGui.QIcon(pm)


def _res(name):
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(base, "assets", name)


def app_icon():
    p = _res("icon.png")
    if os.path.exists(p):
        ic = QtGui.QIcon(p)
        if not ic.isNull():
            return ic
    pm = QtGui.QPixmap(64, 64)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setBrush(QtGui.QColor(ACCENT))
    p.setPen(QtCore.Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QtGui.QColor("white"))
    p.setFont(QtGui.QFont("Segoe UI", 30, QtGui.QFont.Black))
    p.drawText(pm.rect(), QtCore.Qt.AlignCenter, "W")
    p.end()
    return QtGui.QIcon(pm)


def make_grid():
    g = QtWidgets.QListWidget()
    g.setViewMode(QtWidgets.QListWidget.IconMode)
    g.setIconSize(QtCore.QSize(260, 150))
    g.setGridSize(QtCore.QSize(288, 200))
    g.setResizeMode(QtWidgets.QListWidget.Adjust)
    g.setMovement(QtWidgets.QListWidget.Static)
    g.setSpacing(10)
    g.setUniformItemSizes(True)
    return g


# --------------------------------------------------------------------------- #
# main window
# --------------------------------------------------------------------------- #
class WinWall(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        detect_screen_width()
        migrate_old_data()
        self.settings = load_settings()
        # Self-configure launch-on-startup in the user's real context, unless
        # they explicitly turned it off (heals sandboxed/virtualized setups).
        if not SELFTEST and not self.settings.get("startup_disabled"):
            set_startup(True)
        self.mpv = engine.find_mpv()
        self.engine = engine.WallpaperEngine(
            pause_on_battery=self.settings.get("pause_on_battery", False),
            log=lambda m: print(m))
        self._apply_worker = None
        self._thumb_worker = None
        self._search_worker = None
        self._browse_workers = set()
        self._browse_request = 0
        self._dl_worker = None
        self._browse_items = {}
        self._browse_loaded = False
        self._media_ok = None        # None = QtMultimedia not probed yet
        self._library_loaded = False  # library grid built lazily on first show

        self.setWindowTitle("WinWall")
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.resize(980, 660)
        self._build_ui()
        self._apply_style()
        self._build_tray()

        # The library grid (and its thumbnails) is only built when the window is
        # actually shown -- a startup/tray session never pays for it.

        last = self.settings.get("last_wallpaper")
        if not SELFTEST and last and os.path.isfile(last):
            self.apply(last)

        # check for a newer release (packaged builds only)
        if getattr(sys, "frozen", False) and not SELFTEST:
            self._upd_check = UpdateCheckWorker()
            self._upd_check.found.connect(self._show_update_banner)
            self._upd_check.start()

    # ---- UI ----
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)

        # update banner (hidden until a newer release is found)
        self.banner = QtWidgets.QFrame()
        self.banner.setObjectName("banner")
        bl = QtWidgets.QGridLayout(self.banner)
        bl.setContentsMargins(14, 8, 10, 8)
        self.banner_label = QtWidgets.QLabel()
        self.banner_label.setObjectName("bannerlabel")
        self.update_progress = QtWidgets.QProgressBar()
        self.update_progress.setObjectName("updateprogress")
        self.update_progress.setRange(0, 100)
        self.update_progress.setValue(0)
        self.update_progress.setTextVisible(True)
        self.update_progress.hide()
        self.update_btn = QtWidgets.QPushButton("Update now")
        self.update_btn.setObjectName("primary")
        self.update_btn.clicked.connect(self._do_update)
        self.banner_dismiss = QtWidgets.QPushButton("Later")
        self.banner_dismiss.clicked.connect(self.banner.hide)
        bl.addWidget(self.banner_label, 0, 0)
        bl.addWidget(self.update_btn, 0, 1)
        bl.addWidget(self.banner_dismiss, 0, 2)
        bl.addWidget(self.update_progress, 1, 0, 1, 3)
        bl.setColumnStretch(0, 1)
        self.banner.hide()
        root.addWidget(self.banner)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("WinWall")
        title.setObjectName("title")
        sub = QtWidgets.QLabel("Live wallpapers, light on battery")
        sub.setObjectName("subtitle")
        tbox = QtWidgets.QVBoxLayout()
        tbox.setSpacing(0)
        tbox.addWidget(title)
        tbox.addWidget(sub)
        header.addLayout(tbox)
        header.addStretch(1)
        credit = QtWidgets.QLabel(
            'Built by <b>Abhay Shah</b> &nbsp; '
            '<a href="https://x.com/AbhayShah06"><font color="#8a8f9c">X</font></a> · '
            '<a href="https://www.linkedin.com/in/abhay-shah1/"><font color="#8a8f9c">LinkedIn</font></a>')
        credit.setObjectName("links")
        credit.setToolTip("Made by Abhay Shah")
        credit.setOpenExternalLinks(True)
        header.addWidget(credit)
        header.addSpacing(12)
        self.stop_btn = QtWidgets.QPushButton("■  Stop")
        self.stop_btn.clicked.connect(self.stop_wallpaper)
        header.addWidget(self.stop_btn)
        root.addLayout(header)

        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs, 1)

        # My Library tab
        lib = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(lib)
        lv.setContentsMargins(0, 10, 0, 0)
        self.grid = make_grid()
        self.grid.itemClicked.connect(self._library_clicked)
        self.grid.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._library_menu)
        lv.addWidget(self.grid, 1)
        libbar = QtWidgets.QHBoxLayout()
        self.add_btn = QtWidgets.QPushButton("＋  Add your own video")
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(self.add_wallpaper)
        libbar.addWidget(self.add_btn)
        self.clear_btn = QtWidgets.QPushButton("Clear storage")
        self.clear_btn.clicked.connect(self.clear_storage)
        libbar.addWidget(self.clear_btn)
        hint = QtWidgets.QLabel("Right-click a wallpaper to delete it")
        hint.setObjectName("links")
        libbar.addWidget(hint)
        libbar.addStretch(1)
        lv.addLayout(libbar)
        self.tabs.addTab(lib, "My Library")

        # Browse tab
        br = QtWidgets.QWidget()
        bv = QtWidgets.QVBoxLayout(br)
        bv.setContentsMargins(0, 10, 0, 0)
        grow = QtWidgets.QHBoxLayout()
        grow.setSpacing(6)
        for g in shop.GENRES:
            chip = QtWidgets.QPushButton(g.title())
            chip.setObjectName("chip")
            chip.setCursor(QtCore.Qt.PointingHandCursor)
            chip.clicked.connect(lambda _=False, name=g: self._load_genre(name))
            grow.addWidget(chip)
        grow.addStretch(1)
        bv.addLayout(grow)
        srow = QtWidgets.QHBoxLayout()
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Search: cyberpunk city, rain, mountains, sunset, lofi...")
        self.search_edit.returnPressed.connect(self.do_search)
        self.search_btn = QtWidgets.QPushButton("Search")
        self.search_btn.setObjectName("primary")
        self.search_btn.clicked.connect(self.do_search)
        self.web_search_btn = QtWidgets.QPushButton("Search Pexels")
        self.web_search_btn.setToolTip("Find downloadable stock videos on Pexels")
        self.web_search_btn.clicked.connect(self._search_web)
        srow.addWidget(self.search_edit, 1)
        srow.addWidget(self.search_btn)
        srow.addWidget(self.web_search_btn)
        bv.addLayout(srow)
        links = QtWidgets.QLabel(
            'Or browse the web: '
            '<a href="https://moewalls.com"><font color="' + ACCENT + '">MoeWalls</font></a> &nbsp; '
            '<a href="https://desktophut.com"><font color="' + ACCENT + '">DesktopHut</font></a> &nbsp; '
            '<a href="https://mylivewallpapers.com"><font color="' + ACCENT + '">MyLiveWallpapers</font></a>')
        links.setObjectName("links")
        links.setOpenExternalLinks(True)
        bv.addWidget(links)
        self.browse_grid = make_grid()
        self.browse_grid.itemClicked.connect(self._browse_clicked)
        bv.addWidget(self.browse_grid, 1)
        self.tabs.addTab(br, "Browse")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Hover preview: wire the cheap signals now, but build the (heavy)
        # QtMultimedia player lazily on first hover -- see _ensure_media.
        self._preview = None
        self._player = None
        self._preview_item = None
        self._pending_preview_item = None
        self._preview_timer = QtCore.QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(450)
        self._preview_timer.timeout.connect(self._start_hover_preview)
        self.browse_grid.setMouseTracking(True)
        self.browse_grid.viewport().setMouseTracking(True)
        self.browse_grid.itemEntered.connect(self._hover_preview)
        self.browse_grid.viewport().installEventFilter(self)
        self.browse_grid.verticalScrollBar().valueChanged.connect(
            lambda _: self._hide_preview())

        # bottom bar
        bottom = QtWidgets.QHBoxLayout()
        self.batt_chk = QtWidgets.QCheckBox("Pause on battery")
        self.batt_chk.setChecked(self.settings.get("pause_on_battery", False))
        self.batt_chk.toggled.connect(self._toggle_battery)
        bottom.addWidget(self.batt_chk)
        self.startup_chk = QtWidgets.QCheckBox("Launch on startup")
        self.startup_chk.setChecked(startup_enabled())
        self.startup_chk.toggled.connect(self._toggle_startup)
        bottom.addWidget(self.startup_chk)
        bottom.addStretch(1)
        self.status = QtWidgets.QLabel("No wallpaper running")
        self.status.setObjectName("status")
        bottom.addWidget(self.status)
        root.addLayout(bottom)

    def _apply_style(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: #14151b; color: #e8e9ee; }}
            QLabel#title {{ font-size: 22px; font-weight: 800; }}
            QLabel#subtitle {{ color: #8a8f9c; font-size: 12px; }}
            QLabel#status {{ color: #8a8f9c; font-size: 12px; }}
            QLabel#links {{ color: #8a8f9c; font-size: 12px; }}
            QFrame#banner {{ background: #23202f; border: 1px solid {ACCENT}; border-radius: 10px; }}
            QLabel#bannerlabel {{ background: transparent; color: #e8e9ee; font-weight: 600; }}
            QProgressBar#updateprogress {{
                background: #101117; color: white; border: 1px solid #343746;
                border-radius: 5px; height: 10px; text-align: center;
                font-size: 10px; font-weight: 700;
            }}
            QProgressBar#updateprogress::chunk {{ background: {ACCENT}; border-radius: 4px; }}
            QPushButton {{
                background: #262833; color: #e8e9ee; border: none;
                padding: 9px 16px; border-radius: 9px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #30323f; }}
            QPushButton:disabled {{ background: #34303f; color: #9a96a8; }}
            QPushButton#primary {{ background: {ACCENT}; color: white; }}
            QPushButton#primary:hover {{ background: #8f72ff; }}
            QPushButton#primary:disabled {{ background: #4a416d; color: #b8b2c8; }}
            QPushButton#chip {{ background: #1b1d26; color: #cfd2db; padding: 6px 13px;
                                border-radius: 14px; font-weight: 600; }}
            QPushButton#chip:hover {{ background: {ACCENT}; color: white; }}
            QLineEdit {{
                background: #101117; border: 1px solid #23252f; border-radius: 9px;
                padding: 9px 12px; color: #e8e9ee;
            }}
            QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
            QTabWidget::pane {{ border: none; }}
            QTabBar::tab {{
                background: transparent; color: #8a8f9c; padding: 8px 16px;
                border-radius: 8px; font-weight: 600; margin-right: 4px;
            }}
            QTabBar::tab:selected {{ background: #23202f; color: white; }}
            QListWidget {{
                background: #101117; border: 1px solid #23252f; border-radius: 12px;
                padding: 8px; outline: none;
            }}
            QListWidget::item {{ color: #cfd2db; border-radius: 10px; padding-bottom: 6px; }}
            QListWidget::item:selected {{ background: #23202f; border: 1px solid {ACCENT}; color: white; }}
            QCheckBox {{ color: #cfd2db; }}
        """)

    def _build_tray(self):
        self.tray = QtWidgets.QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("WinWall")
        menu = QtWidgets.QMenu()
        menu.addAction("Show", self.showNormal)
        menu.addAction("Stop wallpaper", self.stop_wallpaper)
        menu.addSeparator()
        menu.addAction("Quit", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.showNormal() if r == QtWidgets.QSystemTrayIcon.Trigger else None)
        self.tray.show()

    # ---- library ----
    def list_videos(self):
        if not os.path.isdir(LIBRARY_DIR):
            return []
        return [os.path.join(LIBRARY_DIR, f) for f in sorted(os.listdir(LIBRARY_DIR))
                if f.lower().endswith(VIDEO_EXTS)]

    def reload_library(self):
        self.grid.clear()
        self._items = {}
        videos = self.list_videos()
        for v in videos:
            name = os.path.splitext(os.path.basename(v))[0]
            it = QtWidgets.QListWidgetItem(name)
            it.setData(QtCore.Qt.UserRole, v)
            it.setTextAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignBottom)
            tp = thumb_path(v)
            it.setIcon(QtGui.QIcon(tp) if os.path.exists(tp) else placeholder_icon(name))
            self.grid.addItem(it)
            self._items[v] = it
        self._start_thumbs(videos)
        self._highlight(self.engine.current)

    def _start_thumbs(self, videos):
        missing = [v for v in videos
                   if not (os.path.exists(thumb_path(v)) and os.path.getsize(thumb_path(v)) > 0)]
        if not missing or not self.mpv:
            return
        self._thumb_worker = ThumbWorker(self.mpv, missing)
        self._thumb_worker.done.connect(self._thumb_ready)
        self._thumb_worker.start()

    def _thumb_ready(self, video, thumb):
        it = self._items.get(video)
        if it and thumb and os.path.exists(thumb):
            it.setIcon(QtGui.QIcon(thumb))

    def _library_clicked(self, it):
        v = it.data(QtCore.Qt.UserRole)
        if v:
            self.apply(v)

    # ---- apply / stop ----
    def apply(self, video):
        if not self.mpv:
            QtWidgets.QMessageBox.warning(
                self, "mpv missing",
                "mpv.exe was not found.\nInstall it with:  winget install shinchiro.mpv")
            return
        self.status.setText("Applying %s..." % os.path.basename(video))
        self.grid.setEnabled(False)
        self._apply_worker = ApplyWorker(self.engine, video)
        self._apply_worker.finished_apply.connect(self._applied)
        self._apply_worker.start()

    def _applied(self, video):
        self.grid.setEnabled(True)
        self.status.setText("Live: %s" % os.path.basename(video))
        self.settings["last_wallpaper"] = video
        save_settings(self.settings)
        self._highlight(video)

    def _highlight(self, video):
        for v, it in getattr(self, "_items", {}).items():
            it.setSelected(v == video)

    def stop_wallpaper(self):
        self.status.setText("Stopping...")
        QtWidgets.QApplication.processEvents()
        self.engine.stop()
        self.status.setText("No wallpaper running")
        self._highlight(None)
        QtCore.QTimer.singleShot(1000, trim_memory)

    def add_wallpaper(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add wallpaper videos", os.path.expanduser("~"),
            "Videos (*.mp4 *.webm *.mkv *.mov *.avi *.m4v *.gif)")
        if not files:
            return
        self._import_wallpapers(files)

    def _import_wallpapers(self, files):
        os.makedirs(LIBRARY_DIR, exist_ok=True)
        last = None
        failed = []
        for f in files:
            if not f.lower().endswith(VIDEO_EXTS):
                continue
            dst = os.path.join(LIBRARY_DIR, os.path.basename(f))
            try:
                if os.path.abspath(f) != os.path.abspath(dst):
                    stem, ext = os.path.splitext(dst)
                    n = 2
                    while os.path.exists(dst):
                        dst = "%s (%d)%s" % (stem, n, ext)
                        n += 1
                    shutil.copy2(f, dst)
                last = dst
            except Exception as e:
                print("copy failed:", e)
                failed.append(os.path.basename(f))
        self.reload_library()
        if last:
            self.apply(last)
        if failed:
            QtWidgets.QMessageBox.warning(
                self, "Some files were not added", "\n".join(failed))

    def _library_menu(self, pos):
        it = self.grid.itemAt(pos)
        if not it:
            return
        v = it.data(QtCore.Qt.UserRole)
        menu = QtWidgets.QMenu(self)
        menu.addAction("Apply", lambda: self.apply(v))
        menu.addAction("Delete", lambda: self._delete_video(v))
        menu.exec(self.grid.mapToGlobal(pos))

    def _delete_video(self, video):
        if self.engine.current and os.path.abspath(self.engine.current) == os.path.abspath(video):
            self.engine.stop()
            self.status.setText("No wallpaper running")
        try:
            os.remove(video)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Delete failed", str(e))
            return
        tp = thumb_path(video)
        if os.path.exists(tp):
            try:
                os.remove(tp)
            except Exception:
                pass
        if (self.settings.get("last_wallpaper")
                and os.path.abspath(self.settings["last_wallpaper"]) == os.path.abspath(video)):
            self.settings["last_wallpaper"] = ""
            save_settings(self.settings)
        self.reload_library()

    def clear_storage(self):
        vids = self.list_videos()
        if not vids:
            self.status.setText("Storage is already empty.")
            return
        if QtWidgets.QMessageBox.question(
                self, "Clear storage",
                "Delete all %d saved wallpapers?" % len(vids)) \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.engine.stop()
        for d in (LIBRARY_DIR, THUMB_DIR):
            if os.path.isdir(d):
                for f in os.listdir(d):
                    try:
                        os.remove(os.path.join(d, f))
                    except Exception:
                        pass
        self.settings["last_wallpaper"] = ""
        save_settings(self.settings)
        self.reload_library()
        self.status.setText("Storage cleared.")

    def _toggle_battery(self, on):
        self.engine.pause_on_battery = on
        self.settings["pause_on_battery"] = on
        save_settings(self.settings)

    def _toggle_startup(self, on):
        set_startup(on)
        self.settings["startup_disabled"] = not on
        save_settings(self.settings)

    # ---- one-click self-update ----
    def _show_update_banner(self, info):
        self._update_info = info
        self.banner_label.setText("A new version of WinWall (%s) is available." % info["version"])
        self.update_progress.hide()
        self.update_progress.setRange(0, 100)
        self.update_progress.setValue(0)
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Update now")
        self.banner_dismiss.show()
        self.banner.show()

    def _do_update(self):
        info = getattr(self, "_update_info", None)
        if not info:
            return
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Updating...")
        self.banner_dismiss.hide()
        self.banner_label.setText("Downloading WinWall %s..." % info["version"])
        self.update_progress.setRange(0, 100)
        self.update_progress.setValue(0)
        self.update_progress.setFormat("Downloading  %p%")
        self.update_progress.show()
        dest = sys.executable + ".new"
        self._dlu = UpdateDownloadWorker(info["url"], dest, info.get("size", 0),
                                         info.get("digest", ""))
        self._dlu.progress.connect(self._update_progress_changed)
        self._dlu.phase.connect(self._update_phase_changed)
        self._dlu.done.connect(self._install_update)
        self._dlu.error.connect(self._update_failed)
        self._dlu.start()

    def _update_progress_changed(self, progress):
        self.update_progress.setRange(0, 100)
        self.update_progress.setValue(progress)
        self.update_progress.setFormat("Downloading  %d%%" % progress)

    def _update_phase_changed(self, phase):
        version = getattr(self, "_update_info", {}).get("version", "")
        if phase == "verifying":
            self.banner_label.setText("Verifying WinWall %s..." % version)
            self.update_progress.setRange(0, 0)
            self.update_progress.setFormat("")
        elif phase == "ready":
            self.banner_label.setText("Update verified. Preparing to install...")
            self.update_progress.setRange(0, 100)
            self.update_progress.setValue(100)
            self.update_progress.setFormat("Verified")

    def _install_update(self, new_exe):
        self.banner_label.setText("Installing update... WinWall will restart.")
        self.update_btn.setText("Restarting...")
        self.update_progress.setRange(0, 0)
        self.update_progress.setFormat("")
        QtWidgets.QApplication.processEvents()
        try:
            updater.apply_and_relaunch(new_exe, sys.executable, os.getpid())
        except Exception as e:
            self._update_failed(str(e))
            return
        self.engine.stop()
        self.tray.hide()
        QtCore.QTimer.singleShot(400, QtWidgets.QApplication.quit)

    def _update_failed(self, msg):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Retry")
        self.banner_dismiss.show()
        self.update_progress.hide()
        self.banner_label.setText("Update failed (%s). You can retry." % msg)

    # ---- browse (the shop) ----
    def do_search(self):
        q = self.search_edit.text().strip()
        if not q:
            return
        self._start_browse("search", q, 'Searching "%s"...' % q)

    def _search_web(self):
        q = self.search_edit.text().strip() or "live wallpaper"
        url = "https://www.pexels.com/search/videos/%s/" % urllib.parse.quote(q)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def _load_genre(self, name):
        self._start_browse("genre", name, "Loading %s..." % name.title())

    def _start_browse(self, kind, arg, status_text):
        self._hide_preview()
        self.browse_grid.clear()
        self._browse_items = {}
        self.search_btn.setEnabled(False)
        self.status.setText(status_text)
        self._browse_request += 1
        request = self._browse_request
        worker = MotionWorker(kind, arg)
        self._search_worker = worker
        self._browse_workers.add(worker)
        worker.result.connect(
            lambda item, thumb, r=request: self._search_result(item, thumb)
            if r == self._browse_request else None)
        worker.error.connect(
            lambda msg, r=request: self._search_error(msg)
            if r == self._browse_request else None)
        worker.done.connect(
            lambda n, r=request: self._search_done(n)
            if r == self._browse_request else None)
        worker.finished.connect(lambda w=worker: self._browse_workers.discard(w))
        worker.start()

    def _on_tab_changed(self, idx):
        self._hide_preview()
        if idx == 1 and not self._browse_loaded:  # first time opening Browse
            self._browse_loaded = True
            self._load_genre("nature")

    def _search_result(self, item, thumb):
        it = QtWidgets.QListWidgetItem(item.get("desc") or item["title"])
        it.setData(QtCore.Qt.UserRole, item)
        it.setTextAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignBottom)
        it.setIcon(play_badge_icon(thumb))
        it.setToolTip((item.get("desc") or "") + "\nClick to download and set as live wallpaper")
        self.browse_grid.addItem(it)
        self._browse_items[item.get("id") or item["title"]] = it

    def _search_done(self, n):
        self.search_btn.setEnabled(True)
        self.status.setText("%d results, click one to download & apply" % n if n
                            else "No built-in results. Try Search Pexels.")

    def _search_error(self, msg):
        self.search_btn.setEnabled(True)
        self.status.setText("Search failed.")
        QtWidgets.QMessageBox.warning(self, "Search failed", msg)

    def _browse_clicked(self, it):
        self._hide_preview()
        item = it.data(QtCore.Qt.UserRole)
        if not item:
            return
        name = safe_filename(item.get("desc") or item["title"])
        if item.get("id"):
            name += " - " + safe_filename(str(item["id"]))
        dest = os.path.join(LIBRARY_DIR, name + ".mp4")
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            self._apply_downloaded(dest)
            return
        self.status.setText("Downloading...")
        self.browse_grid.setEnabled(False)
        self._dl_worker = DownloadWorker(item["video_url"], dest)
        self._dl_worker.progress.connect(
            lambda p: self.status.setText("Downloading... %d%%" % p))
        self._dl_worker.done.connect(self._apply_downloaded)
        self._dl_worker.error.connect(self._download_error)
        self._dl_worker.start()

    def _apply_downloaded(self, dest):
        self.browse_grid.setEnabled(True)
        self.reload_library()
        self.tabs.setCurrentIndex(0)
        self.apply(dest)

    def _download_error(self, msg):
        self.browse_grid.setEnabled(True)
        self.status.setText("Download failed.")
        QtWidgets.QMessageBox.warning(self, "Download failed", msg)

    # ---- hover preview ----
    def _ensure_media(self):
        """Import QtMultimedia and build the preview player on first hover.
        Deferred so tray/startup sessions never load the media backend."""
        if self._player is not None:
            return True
        if self._media_ok is False:
            return False
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget
        except Exception:
            self._media_ok = False
            return False
        self._preview = QVideoWidget(self.browse_grid.viewport())
        self._preview.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._preview.hide()
        self._player = QMediaPlayer(self)
        self._player.setVideoOutput(self._preview)
        self._player.setLoops(QMediaPlayer.Infinite)
        self._media_ok = True
        return True

    def _hover_preview(self, item):
        if item is None or item is self._preview_item:
            return
        self._pending_preview_item = item
        self._preview_timer.start()

    def _start_hover_preview(self):
        item = self._pending_preview_item
        if item is None:
            return
        data = item.data(QtCore.Qt.UserRole) or {}
        url = data.get("preview_url") or data.get("video_url")
        if not url or not self._ensure_media():
            return
        self._preview_item = item
        rect = self.browse_grid.visualItemRect(item)
        self._preview.setGeometry(rect.left() + 8, rect.top() + 8,
                                  rect.width() - 16, rect.height() - 42)
        self._preview.raise_()
        self._preview.show()
        self._player.setSource(QtCore.QUrl(url))
        self._player.play()

    def _hide_preview(self):
        self._preview_timer.stop()
        self._pending_preview_item = None
        if not self._player:
            return
        self._preview_item = None
        self._player.stop()
        self._preview.hide()

    def eventFilter(self, obj, ev):
        if (self._player and obj is self.browse_grid.viewport()
                and ev.type() == QtCore.QEvent.Leave):
            self._hide_preview()
        return super().eventFilter(obj, ev)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(
                u.isLocalFile() and u.toLocalFile().lower().endswith(VIDEO_EXTS)
                for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls()
                 if u.isLocalFile() and u.toLocalFile().lower().endswith(VIDEO_EXTS)]
        if files:
            self._import_wallpapers(files)
            e.acceptProposedAction()

    # ---- lifecycle ----
    def showEvent(self, e):
        # Build the library grid the first time the window is actually shown.
        if not self._library_loaded:
            self._library_loaded = True
            self.reload_library()
        super().showEvent(e)

    def closeEvent(self, e):
        # Hiding to the tray: stop any preview playback so nothing decodes while
        # the window is not visible.
        self._hide_preview()
        if self.tray.isVisible():
            e.ignore()
            self.hide()
            self.tray.showMessage("WinWall", "Still running in the tray.",
                                  QtWidgets.QSystemTrayIcon.Information, 2000)
            # give RAM back now that the window is gone
            QtCore.QTimer.singleShot(1500, trim_memory)

    def _quit(self):
        self.engine.stop()
        self.tray.hide()
        QtWidgets.QApplication.quit()


SINGLETON = "WinWall_singleton_selftest" if SELFTEST else "WinWall_singleton"


def main():
    try:  # so Windows uses our icon on the taskbar (not a generic one)
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AbhayShah.WinWall")
    except Exception:
        pass
    app = QtWidgets.QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)

    # Single instance: if WinWall is already running, tell it to show its window
    # and exit, so two copies never fight over the wallpaper.
    probe = QtNetwork.QLocalSocket()
    probe.connectToServer(SINGLETON)
    if probe.waitForConnected(200):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return 0
    QtNetwork.QLocalServer.removeServer(SINGLETON)  # clear any stale socket
    server = QtNetwork.QLocalServer()
    server.listen(SINGLETON)

    w = WinWall()
    w._singleton_server = server  # keep a reference alive
    if getattr(sys, "frozen", False):
        # Reaching this point proves the updated executable completed its
        # bootloader/Python/UI startup, so its rollback copy is no longer needed.
        updater.cleanup_backup(sys.executable)

    def _on_second_launch():
        conn = server.nextPendingConnection()
        if conn:
            conn.readAll()
            w.showNormal()
            w.raise_()
            w.activateWindow()
    server.newConnection.connect(_on_second_launch)

    if SELFTEST:
        w.show()
        QtCore.QTimer.singleShot(1500, w._quit)
        return app.exec()
    # Launched at startup with --tray (and a saved wallpaper): stay in the tray
    # and just run the last wallpaper. Otherwise show the window.
    start_tray = "--tray" in sys.argv
    if not start_tray:
        w.show()
    else:
        # Started hidden in the tray: once the wallpaper is up, trim the working
        # set so an idle startup session sits small.
        QtCore.QTimer.singleShot(6000, trim_memory)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
