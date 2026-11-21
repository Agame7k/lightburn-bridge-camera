"""Update upload: validation, self-test, switching releases, password, rollback."""
import json
import os
import shutil
import unittest

from helpers import app_sources, load, make_home, package

app = load("lbcam_stream")


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(app.VERSION)
        self.cfg = app.Config(os.path.join(self.home, "lbcam.json"))
        self.upd = app.Updater(self.cfg, home=self.home)
        self.restarts = 0

        def fake_restart():
            self.restarts += 1
        self.upd.restart = fake_restart  # don't kill the test process

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def link(self, name):
        return os.readlink(os.path.join(self.home, name))

    def pending(self):
        with open(os.path.join(self.home, "pending.json")) as f:
            return json.load(f)

    def install(self, version, **kw):
        return self.upd.install(package(app_sources(version), version, **kw), kw.pop("pw", None))

    # ---------------------------------------------------------------- happy path
    def test_install_switches_release(self):
        self.assertEqual(self.upd.install(package(app_sources("9.1"), "9.1"), None), "9.1")
        self.assertEqual(self.link("current"), "releases/9.1")
        self.assertEqual(self.link("previous"), "releases/" + app.VERSION)
        p = self.pending()
        self.assertEqual(p["version"], "9.1")
        self.assertEqual(p["from"], app.VERSION)
        self.assertTrue(os.path.isfile(os.path.join(self.home, "releases", "9.1", "lbcam_stream.py")))

    def test_pending_remembers_previous_for_rollback(self):
        self.upd.install(package(app_sources("9.1"), "9.1"), None)
        os.unlink(os.path.join(self.home, "pending.json"))  # launcher confirmed it
        self.upd.install(package(app_sources("9.2"), "9.2"), None)
        self.assertEqual(self.pending()["restore_previous"], "releases/" + app.VERSION)

    def test_manual_rollback(self):
        self.upd.install(package(app_sources("9.1"), "9.1"), None)
        self.assertEqual(self.upd.rollback(None), app.VERSION)
        self.assertEqual(self.link("current"), "releases/" + app.VERSION)

    def test_package_without_assets_reuses_current_ones(self):
        files = {"lbcam_stream.py": app_sources("9.3")["lbcam_stream.py"]}
        self.upd.install(package(files, "9.3"), None)
        self.assertTrue(os.path.isfile(os.path.join(self.home, "releases", "9.3", "dashboard.html")))

    # ---------------------------------------------------------------- rejected uploads
    def assertRejected(self, blob, msg):
        with self.assertRaises(app.UpdateError) as cm:
            self.upd.install(blob, None)
        self.assertIn(msg, str(cm.exception))
        self.assertEqual(self.link("current"), "releases/" + app.VERSION)
        self.assertFalse(os.path.exists(os.path.join(self.home, "pending.json")))

    def test_rejects_non_tar(self):
        self.assertRejected(b"hello", "Not an lbcam update file")

    def test_rejects_tampered_file(self):
        files = app_sources("9.4")
        good = package(files, "9.4")  # noqa: F841 (sums computed from originals below)
        import hashlib
        sums = {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}
        files["dashboard.html"] += b"<!-- changed -->"
        self.assertRejected(package(files, "9.4", manifest_files=sums), "checksum mismatch")

    def test_rejects_path_traversal(self):
        self.assertRejected(package(app_sources("9.5"), "9.5", extra={"../../evil.sh": b"x"}), "Unexpected item")

    def test_rejects_bad_version(self):
        self.assertRejected(package(app_sources("9.6"), "../9.6"), "invalid version")

    def test_rejects_same_version(self):
        self.assertRejected(package(app_sources(app.VERSION), app.VERSION), "already running")

    def test_rejects_version_that_fails_selftest(self):
        self.assertRejected(package(app_sources("9.7", break_python=True), "9.7"), "self-test")
        leftovers = [d for d in os.listdir(os.path.join(self.home, "releases")) if d.startswith(".stage")]
        self.assertEqual(leftovers, [])

    # ---------------------------------------------------------------- password
    def test_password_optional_then_required(self):
        self.upd.set_password("lasercam1")
        with self.assertRaises(PermissionError):
            self.upd.install(package(app_sources("9.8"), "9.8"), None)
        with self.assertRaises(PermissionError):
            self.upd.install(package(app_sources("9.8"), "9.8"), "wrong")
        self.assertEqual(self.upd.install(package(app_sources("9.8"), "9.8"), "lasercam1"), "9.8")

    def test_password_change_needs_current_and_can_be_removed(self):
        self.upd.set_password("lasercam1")
        with self.assertRaises(PermissionError):
            self.upd.set_password("hijacked", current="nope")
        self.upd.set_password("", current="lasercam1")
        self.assertFalse(self.upd.password_set())

    def test_short_password_rejected(self):
        with self.assertRaises(app.UpdateError):
            self.upd.set_password("abc")

    def test_unmanaged_install_refused(self):
        os.unlink(os.path.join(self.home, "launcher.py"))
        with self.assertRaises(app.UpdateError):
            self.upd.install(package(app_sources("9.9"), "9.9"), None)


if __name__ == "__main__":
    unittest.main()
