"""Settings file: batched writes (SD card wear) and flushing."""
import json
import os
import shutil
import tempfile
import time
import unittest

from helpers import load

app = load("lbcam_stream")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "lbcam.json")
        self.cfg = app.Config(self.path)
        self.cfg.SAVE_DELAY = 0.3

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def saved(self):
        with open(self.path) as f:
            return json.load(f)

    def test_control_changes_are_batched(self):
        for v in range(20):  # a slider drag
            self.cfg.set_control("/dev/video0", 123, v)
        self.assertFalse(os.path.exists(self.path), "should not write on every change")
        time.sleep(0.6)
        self.assertEqual(self.saved()["controls"]["/dev/video0"]["123"], 19)

    def test_flush_writes_now(self):
        self.cfg.set_control("cam", 1, 5)
        self.cfg.flush()
        self.assertEqual(self.saved()["controls"]["cam"]["1"], 5)

    def test_update_writes_immediately(self):
        self.cfg.update(width=640, height=480)
        self.assertEqual(self.saved()["width"], 640)

    def test_identical_save_is_skipped(self):
        self.cfg.update(fps=10)
        m1 = os.stat(self.path).st_mtime_ns
        time.sleep(0.05)
        self.cfg.update(fps=10)  # no change
        self.assertEqual(os.stat(self.path).st_mtime_ns, m1)

    def test_reload_keeps_values(self):
        self.cfg.update(device="/dev/v4l/by-id/x", fps=5)
        self.cfg.set_control("x", 7, 3)
        self.cfg.flush()
        again = app.Config(self.path)
        self.assertEqual(again.get("fps"), 5)
        self.assertEqual(again.controls_for("x"), {"7": 3})

    def test_corrupt_file_falls_back_to_defaults(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        c = app.Config(self.path)
        self.assertIsNotNone(c.load_error)
        self.assertEqual(c.get("device"), "auto")


if __name__ == "__main__":
    unittest.main()
