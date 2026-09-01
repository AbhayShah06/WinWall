"""
shop.py -- fetches live wallpapers for the Browse tab (genres + search).

Parses listing pages into items with a thumbnail and a direct HD mp4 URL, and
downloads them on demand. For personal use.
"""

import re
import urllib.parse
import urllib.request

_BASE = "https://motionbgs.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# genre chips shown in the Browse tab
GENRES = ["anime", "nature", "city", "space", "car", "games", "cyberpunk",
          "abstract", "superhero", "fantasy", "animal", "technology"]

_THUMB = re.compile(r'/i/c/\d+x\d+/media/(\d+)/([^"\']+?\.jpg)')


def _get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def http_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": _BASE + "/"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def _parse(html, cap=48):
    seen = set()
    out = []
    for m in _THUMB.finditer(html):
        mid, fname = m.group(1), m.group(2)
        if mid in seen:
            continue
        seen.add(mid)
        stem = re.sub(r"\.\d+x\d+$", "", fname[:-4])
        title = stem.replace("-", " ").strip().title() or ("Wallpaper %s" % mid)
        out.append({
            "id": mid,
            "title": title,
            "desc": title,
            "thumb": "%s/i/c/480x270/media/%s/%s" % (_BASE, mid, fname),
            "video_url": "%s/dl/hd/%s" % (_BASE, mid),
            "preview_url": "%s/dl/hd/%s" % (_BASE, mid),
        })
        if len(out) >= cap:
            break
    return out


def genre(name):
    return _parse(_get_text("%s/tag:%s/" % (_BASE, urllib.parse.quote(name))))


def search(query):
    return _parse(_get_text("%s/?s=%s" % (_BASE, urllib.parse.quote(query))))
