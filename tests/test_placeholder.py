"""The animated "Camera not connected" image (pure-Python JPEG block re-encoding)."""
import io
import os
import re
import shutil
import tempfile
import unittest

from helpers import ROOT, load

app = load("lbcam_stream")

with open(os.path.join(ROOT, "placeholder.jpg"), "rb") as f:
    PLACEHOLDER = f.read()


def mcus(jpeg):
    body = jpeg[jpeg.index(b"\xff\xda"):]
    body = body[4 + int.from_bytes(body[2:4], "big") - 2:jpeg.rindex(b"\xff\xd9")]
    return re.split(rb"\xff[\xd0-\xd7]", body)


class LivePlaceholderTest(unittest.TestCase):
    def setUp(self):
        self.lp = app.LivePlaceholder(PLACEHOLDER)

    def test_shipped_placeholder_animates(self):
        a, b = self.lp.frame(0), self.lp.frame(1, pulse=False)
        self.assertNotEqual(a, b)
        for f in (a, b):
            self.assertEqual(f[:2], b"\xff\xd8")
            self.assertEqual(f[-2:], b"\xff\xd9")
            self.assertNotIn(b"lbcam_live", f)  # the layout COM segment is not resent

    def test_only_the_live_cells_change(self):
        base, frame = mcus(PLACEHOLDER), mcus(self.lp.frame(3725))
        self.assertEqual(len(base), len(frame))
        changed = [i for i, (x, y) in enumerate(zip(base, frame)) if x != y]
        self.assertTrue(changed)
        rows = {i // self.lp.cols * self.lp.mh for i in changed}
        self.assertLessEqual(rows, {self.lp.clock_xy[1], self.lp.clock_xy[1] + self.lp.mh})

    def test_timer_is_capped(self):
        self.assertEqual(self.lp.frame(10 ** 9), self.lp.frame(99 * 3600 + 59 * 60 + 59))

    def test_decodes_with_pillow_when_available(self):
        try:
            from PIL import Image, ImageChops
        except ImportError:
            self.skipTest("Pillow not installed")
        ref = Image.open(io.BytesIO(PLACEHOLDER)).convert("RGB")
        img = Image.open(io.BytesIO(self.lp.frame(42))).convert("RGB")
        box = ImageChops.difference(ref, img).getbbox()
        self.assertIsNotNone(box)
        self.assertGreaterEqual(box[1], 600)
        self.assertLessEqual(box[3], 648)

    def test_rejects_images_without_layout(self):
        for data in (b"", b"\xff\xd8placeholder", PLACEHOLDER.replace(b"lbcam_live", b"other_xxxx")):
            with self.assertRaises(Exception):
                app.LivePlaceholder(data)


class CameraPlaceholderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = app.Config(os.path.join(self.tmp, "c.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_static_placeholder_still_served(self):
        cam = app.Camera(self.cfg, b"\xff\xd8ph")
        self.assertIsNone(cam.animated)
        cam.waiting_since = 0
        self.assertEqual(cam._placeholder_frame(), b"\xff\xd8ph")

    def test_animated_placeholder_ticks(self):
        cam = app.Camera(self.cfg, PLACEHOLDER)
        self.assertIsNotNone(cam.animated)
        cam.waiting_since = app.time.time()
        first = cam._placeholder_frame()
        cam.waiting_since -= 5
        self.assertNotEqual(first, cam._placeholder_frame())


if __name__ == "__main__":
    unittest.main()
