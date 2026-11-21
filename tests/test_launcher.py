"""Launcher: automatic rollback after a failed update."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import load


class LauncherRollbackTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="lbcam-launcher-")
        for v in ("1.0", "2.0", "3.0"):
            d = os.path.join(self.home, "releases", v)
            os.makedirs(d)
            open(os.path.join(d, "lbcam_stream.py"), "w").close()
        self.launcher = load("launcher", {"LBCAM_HOME": self.home})

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def link(self, name):
        p = os.path.join(self.home, name)
        return os.readlink(p) if os.path.islink(p) else None

    def last(self):
        with open(os.path.join(self.home, "last_update.json")) as f:
            return json.load(f)

    def test_rollback_restores_previous_and_its_previous(self):
        L = self.launcher
        L.set_link("current", "releases/3.0")   # the broken one
        L.set_link("previous", "releases/2.0")
        L.write_json(os.path.join(self.home, "pending.json"), {"version": "3.0"})
        L.rollback("3.0", "crashed", restore_previous="releases/1.0")
        self.assertEqual(self.link("current"), "releases/2.0")
        self.assertEqual(self.link("previous"), "releases/1.0")  # never the broken one
        self.assertFalse(os.path.exists(os.path.join(self.home, "pending.json")))
        self.assertEqual(self.last()["result"], "rolled_back")
        self.assertEqual(self.last()["restored"], "2.0")

    def test_rollback_without_older_release_clears_previous(self):
        L = self.launcher
        L.set_link("current", "releases/3.0")
        L.set_link("previous", "releases/2.0")
        L.rollback("3.0", "crashed", restore_previous=None)
        self.assertEqual(self.link("current"), "releases/2.0")
        self.assertIsNone(self.link("previous"))

    def test_rollback_with_nothing_to_restore(self):
        L = self.launcher
        L.set_link("current", "releases/3.0")
        L.rollback("3.0", "crashed")
        self.assertEqual(self.link("current"), "releases/3.0")
        self.assertIn("no previous version", self.last()["reason"])


class LauncherEndToEndTest(unittest.TestCase):
    """Run the real launcher against a release that crashes on start."""

    def test_crashing_update_is_rolled_back(self):
        home = tempfile.mkdtemp(prefix="lbcam-e2e-")
        try:
            for v, body in (("1.0", "import time\nwhile True: time.sleep(1)\n"),
                            ("2.0", "raise SystemExit('boom')\n")):
                d = os.path.join(home, "releases", v)
                os.makedirs(d)
                with open(os.path.join(d, "lbcam_stream.py"), "w") as f:
                    f.write(body)
            os.symlink("releases/2.0", os.path.join(home, "current"))
            os.symlink("releases/1.0", os.path.join(home, "previous"))
            with open(os.path.join(home, "pending.json"), "w") as f:
                json.dump({"version": "2.0", "restore_previous": None}, f)
            env = dict(os.environ, LBCAM_HOME=home, CAM_PORT="1")
            launcher = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launcher.py")
            p = subprocess.Popen([sys.executable, launcher], env=env, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 15
                while time.time() < deadline and not os.path.exists(os.path.join(home, "last_update.json")):
                    time.sleep(0.2)
            finally:
                os.killpg(p.pid, signal.SIGKILL)  # the launcher and the release it started
                p.wait()
            with open(os.path.join(home, "last_update.json")) as f:
                last = json.load(f)
            self.assertEqual(last["result"], "rolled_back")
            self.assertEqual(os.readlink(os.path.join(home, "current")), "releases/1.0")
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
