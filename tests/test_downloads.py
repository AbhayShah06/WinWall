import hashlib
import io
import json
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
    def test_updater_uses_portable_app_not_setup(self):
        release = {"tag_name": "v1.0.6", "assets": [
            {"name": "WinWall-Setup-1.0.6.exe", "browser_download_url": "https://example.test/setup"},
            {"name": "WinWall.exe", "browser_download_url": "https://example.test/app", "size": 1234567},
        ]}
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(release).encode())):
            self.assertEqual(updater.check("1.0.5")["url"], "https://example.test/app")

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
        phases = []
        with tempfile.TemporaryDirectory() as folder:
            dest = os.path.join(folder, "WinWall.exe.new")
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse(data)):
                ok = updater.download("https://example.test/update", dest, len(data), digest,
                                      status=phases.append)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(dest))
            self.assertEqual(phases, ["downloading", "verifying", "ready"])

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

    def test_update_cleans_partial_file_after_network_error(self):
        class BrokenResponse(FakeResponse):
            def read(self, size=-1):
                if self._stream.tell():
                    raise OSError("connection lost")
                return self._stream.read(min(size, 262144))

        data = b"x" * 1_100_000
        with tempfile.TemporaryDirectory() as folder:
            dest = os.path.join(folder, "WinWall.exe.new")
            with mock.patch("urllib.request.urlopen", return_value=BrokenResponse(data)):
                with self.assertRaises(OSError):
                    updater.download("https://example.test/update", dest)
            self.assertFalse(os.path.exists(dest + ".part"))

    @unittest.skipUnless(os.name == "nt", "update helper is Windows-only")
    def test_relaunch_resets_pyinstaller_environment_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "WinWall.exe")
            new = target + ".new"
            with open(target, "wb") as f:
                f.write(b"old")
            with open(new, "wb") as f:
                f.write(b"new")
            with mock.patch("updater.subprocess.Popen") as popen:
                updater.apply_and_relaunch(new, target, process_id=4321)

            args, kwargs = popen.call_args
            helper = args[0][2]
            try:
                with open(helper, encoding="mbcs") as f:
                    script = f.read()
                self.assertEqual(kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
                self.assertIn('set "PYINSTALLER_RESET_ENVIRONMENT=1"', script)
                self.assertIn('tasklist /fi "pid eq 4321"', script)
                self.assertIn('start ""', script)
                # The old executable is removed only before the swap. There is
                # no post-launch backup deletion; startup performs that cleanup.
                self.assertLess(script.find("del \"%s.bak\"" % target),
                                script.find('start ""'))
            finally:
                if os.path.exists(helper):
                    os.remove(helper)


if __name__ == "__main__":
    unittest.main()
