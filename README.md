# WinWall

A lightweight live-wallpaper app for Windows. Plays looping videos as your
desktop wallpaper on all monitors, with a built-in shop to browse and download
new ones. Built to stay light on the battery.

## Features

- **Live video wallpaper** on all monitors, behind your desktop icons.
- **Easy on battery.** Hardware-accelerated decode, and it pauses when a window
  covers the desktop (nothing to show, so nothing to render). Optional
  pause-on-battery too.
- **My Library.** Your saved wallpapers as a grid; click one to apply it live.
  Add your own `.mp4` / `.webm` / etc. with the file picker or drag and drop.
- **Browse.** Genre chips (anime, nature, city, space, cyberpunk, cars, games,
  abstract, animals) plus free-text search. Browse visual thumbnails, then click
  to download the size that matches your screen and apply it. Handy links
  to a few wallpaper sites are included too.
- **System tray.** Closing the window keeps it running in the tray. Remembers
  your last wallpaper.
- **Launch on startup** (opens straight to the tray and restores your last
  wallpaper, no window).
- **Automatic display recovery.** Rebuilds the wallpaper when a monitor is
  connected, disconnected, rotated, or changes resolution.

## How it works

Windows draws the desktop wallpaper in a hidden window. WinWall embeds a video
window behind the desktop icons (the WorkerW / Progman layer) and loops your
video there, pausing and resuming for power saving.

## Requirements

- Windows 10/11
- [Python](https://www.python.org/) 3.9+
- [mpv](https://mpv.io/) (`winget install shinchiro.mpv`)
- Python packages: `pip install -r requirements.txt`

## Run

```bash
pythonw app.py
```

Or the command-line engine only (no UI):

```bash
python winwall.py "C:\path\to\video.mp4"
```

## Build a standalone .exe

```bash
python build_exe.py
```

Produces `dist/WinWall.exe`, a single self-contained file (Python and mpv
bundled) that runs on a Windows PC with nothing installed.

## Project layout

- `app.py` - the desktop app (library, browse, tray, settings)
- `engine.py` - the wallpaper engine (window embedding, playback, pause logic)
- `shop.py` - the Browse tab source (genres, search, downloads)
- `winwall.py` - minimal CLI wrapper around the engine
- `build_exe.py` - builds the standalone executable

## Made by

Abhay Shah - [@AbhayShah06 on X](https://x.com/AbhayShah06) - [LinkedIn](https://www.linkedin.com/in/abhay-shah1/)

## License

Personal project.
