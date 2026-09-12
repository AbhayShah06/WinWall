import hashlib
import io
import os
import tempfile
import unittest
from unittest import mock

import shop
import updater


class FakeResponse:
    def __init__(self, data):
        self._stream = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._stream.read(size)


class DownloadTests(unittest.TestCase):
    def test_version_comparison(self):
        self.assertTrue(updater.is_newer("1.0.3", "1.0.2"))
        self.assertFalse(updater.is_newer("v1.0.2", "1.0.2"))

    def test_shop_download_streams_to_destination(self):
        data = b"wallpaper" * 1000
        with tempfile.TemporaryDirectory() as folder:
            dest = os.path.join(folder, "wallpaper.mp4")
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse(data)):
                shop.download("https://example.test/wallpaper", dest)
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), data)
            self.assertFalse(os.path.exists(dest + ".part"))

    def test_empty_search_does_not_return_trending_wallpapers(self):
        html = """
            <h1>No Results Found for 'toronto'</h1>
            <img src="/i/c/480x270/media/42/unrelated-anime.jpg">
        """
        with mock.patch("shop._get_text", return_value=html) as get_text:
            self.assertEqual(shop.search("toronto"), [])
        self.assertIn("/search?q=toronto", get_text.call_args.args[0])

    def test_update_accepts_matching_digest(self):
        data = b"x" * 1_100_000
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            dest = os.path.join(folder, "WinWall.exe.new")
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse(data)):
                ok = updater.download("https://example.test/update", dest, len(data), digest)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(dest))

    def test_update_rejects_wrong_digest(self):
        data = b"x" * 1_100_000
        with tempfile.TemporaryDirectory() as folder:
            dest = os.path.join(folder, "WinWall.exe.new")
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse(data)):
                ok = updater.download("https://example.test/update", dest, len(data),
                                      "sha256:" + ("0" * 64))
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(dest))
            self.assertFalse(os.path.exists(dest + ".part"))


if __name__ == "__main__":
    unittest.main()
