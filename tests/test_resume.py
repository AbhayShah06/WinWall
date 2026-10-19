import unittest
from unittest import mock

import engine


class ResumeTests(unittest.TestCase):
    def test_sleep_gap_recreates_even_a_healthy_player(self):
        wallpaper = engine.WallpaperEngine(log=lambda message: None)
        wallpaper._stop = mock.Mock()
        wallpaper._stop.is_set.side_effect = [False, False, True]
        players = [object()]
        with mock.patch.object(engine, "_set_dpi_awareness"), \
                mock.patch.object(engine, "_register_class"), \
                mock.patch.object(engine, "find_mpv", return_value="mpv.exe"), \
                mock.patch.object(engine.win32gui, "PumpWaitingMessages"), \
                mock.patch.object(engine.time, "time", return_value=100), \
                mock.patch.object(engine.time, "monotonic", side_effect=[100, 101, 125]), \
                mock.patch.object(engine.time, "sleep"), \
                mock.patch.object(wallpaper, "_spawn", return_value=(123, players)) as spawn, \
                mock.patch.object(wallpaper, "_update_pause"), \
                mock.patch.object(wallpaper, "_layout_changed", return_value=False), \
                mock.patch.object(wallpaper, "_healthy", return_value=True), \
                mock.patch.object(wallpaper, "_teardown"):
            wallpaper._run("wallpaper.mp4")
        self.assertEqual(spawn.call_count, 2)
