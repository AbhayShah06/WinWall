import subprocess
import sys
import time
import unittest
from unittest import mock

import app


class ShutdownTests(unittest.TestCase):
    def test_cancelled_thumbnail_stops_its_child_process(self):
        original = subprocess.Popen
        children = []

        def start(*args, **kwargs):
            process = original(*args, **kwargs)
            children.append(process)
            return process

        before = time.monotonic()
        with mock.patch.object(app.subprocess, "Popen", side_effect=start):
            result = app._run_thumbnail([sys.executable, "-c", "import time;time.sleep(30)"], cancelled=lambda: True)
        self.assertFalse(result)
        self.assertLess(time.monotonic() - before, 5)
        self.assertIsNotNone(children[0].poll())
