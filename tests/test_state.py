import json
import os
import tempfile
import unittest

import winwall_state


class StateTests(unittest.TestCase):
    def test_invalid_legacy_settings_do_not_block_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            legacy = os.path.join(folder, "old")
            os.makedirs(legacy)
            with open(os.path.join(legacy, "settings.json"), "w", encoding="utf-8") as handle:
                handle.write("invalid json")
            self.assertTrue(os.path.isdir(winwall_state.data_dir(folder, legacy)))

    def test_migration_preserves_wallpaper_and_future_choices(self):
        with tempfile.TemporaryDirectory() as folder:
            legacy = os.path.join(folder, "old")
            library = os.path.join(legacy, "library")
            os.makedirs(library)
            movie = os.path.join(library, "wallpaper.mp4")
            with open(movie, "wb") as handle:
                handle.write(b"wallpaper")
            with open(os.path.join(legacy, "settings.json"), "w", encoding="utf-8") as handle:
                json.dump({"last_wallpaper": movie, "startup_disabled": False}, handle)
            target = winwall_state.data_dir(folder, legacy)
            settings = os.path.join(target, "settings.json")
            with open(settings, encoding="utf-8") as handle:
                restored = json.load(handle)
            self.assertEqual(restored["last_wallpaper"], os.path.join(target, "library", "wallpaper.mp4"))
            self.assertTrue(os.path.isfile(restored["last_wallpaper"]))
            self.assertTrue(os.path.isfile(movie))
            with open(settings, "w", encoding="utf-8") as handle:
                json.dump({"last_wallpaper": "new-choice.mp4"}, handle)
            winwall_state.data_dir(folder, legacy)
            with open(settings, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["last_wallpaper"], "new-choice.mp4")
