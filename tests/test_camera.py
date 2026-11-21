"""V4L2 struct layout, idle pause/wake logic, camera list cache (no camera hardware needed)."""
import ctypes
import os
import shutil
import tempfile
import threading
import time
import unittest

from helpers import load

app = load("lbcam_stream")


class V4L2LayoutTest(unittest.TestCase):
    def test_selftest_passes(self):
        self.assertEqual(app.selftest(), 0)

    def test_ioctl_numbers_match_kernel_headers(self):
        ptr64 = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(app.VIDIOC_QUERYCAP, 0x80685600)
        self.assertEqual(app.VIDIOC_QUERYCTRL, 0xc0445624)
        self.assertEqual(app.VIDIOC_QUERYMENU, 0xc02c5625)
        self.assertEqual(app.VIDIOC_ENUM_FRAMESIZES, 0xc02c564a)
        self.assertEqual(app.VIDIOC_ENUM_FRAMEINTERVALS, 0xc034564b)
        self.assertEqual(app.VIDIOC_S_FMT, 0xc0d05605 if ptr64 else 0xc0cc5605)
        self.assertEqual(app.VIDIOC_QBUF, 0xc058560f if ptr64 else 0xc044560f)


class IdleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cam = app.Camera(app.Config(os.path.join(self.tmp, "c.json")), b"\xff\xd8ph")
        self.cam.idle_after = 0.2

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idle_after_timeout_without_viewers(self):
        self.cam.touch()
        self.assertFalse(self.cam.is_idle())
        time.sleep(0.3)
        self.assertTrue(self.cam.is_idle())

    def test_viewer_keeps_it_awake(self):
        self.cam.client(+1)
        time.sleep(0.3)
        self.assertFalse(self.cam.is_idle())
        self.cam.client(-1)
        self.assertFalse(self.cam.is_idle())  # leaving counts as recent use
        time.sleep(0.3)
        self.assertTrue(self.cam.is_idle())

    def test_disabled_with_zero(self):
        self.cam.idle_after = 0
        self.cam.last_use = 0
        self.assertFalse(self.cam.is_idle())

    def test_snapshot_waits_for_fresh_frame(self):
        self.cam.frame = None  # as after going idle
        threading.Timer(0.2, self.cam._publish, [b"\xff\xd8fresh", True]).start()
        self.assertEqual(self.cam.latest_frame(2), b"\xff\xd8fresh")

    def test_snapshot_gives_up(self):
        self.cam.frame = None
        self.assertIsNone(self.cam.latest_frame(0.2))


class CameraCacheTest(unittest.TestCase):
    def test_cached_until_device_nodes_change(self):
        calls = []
        real = app.list_cameras
        app.list_cameras = lambda: calls.append(1) or []
        nodes = ["/dev/video0"]
        real_glob = app.glob.glob
        app.glob.glob = lambda pat: list(nodes) if pat == "/dev/video*" else real_glob(pat)
        try:
            app._cam_cache.update(t=0.0, nodes=None, list=[])
            app.cameras_cached()
            app.cameras_cached()
            self.assertEqual(len(calls), 1)
            nodes.append("/dev/video1")  # camera plugged in
            app.cameras_cached()
            self.assertEqual(len(calls), 2)
            app.cameras_cached(max_age=0)
            self.assertEqual(len(calls), 3)
        finally:
            app.list_cameras = real
            app.glob.glob = real_glob


if __name__ == "__main__":
    unittest.main()
