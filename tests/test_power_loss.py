"""The Bridge loses power whenever the laser is switched off: files must survive a cut at any moment."""
import json
import os
import shutil
import tempfile
import unittest

from helpers import load
from test_jobs import square_job

app = load("lbcam_stream")


class WriteDurableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_replaces_whole_file_and_leaves_no_temp(self):
        p = os.path.join(self.tmp, "x.json")
        app.write_durable(p, "old")
        app.write_durable(p, b"new")
        with open(p) as f:
            self.assertEqual(f.read(), "new")
        self.assertEqual(os.listdir(self.tmp), ["x.json"])


class ConfigRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "lbcam.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write(text)

    def test_cut_during_rename_recovers_from_temp(self):
        self.write("lbcam.json.tmp", json.dumps({"fps": 7, "update_auth": {"salt": "a", "hash": "b"}}))
        c = app.Config(self.path)  # lbcam.json itself is gone
        self.assertEqual(c.get("fps"), 7)
        self.assertEqual(c.get("update_auth"), {"salt": "a", "hash": "b"})
        self.assertIsNone(c.load_error)

    def test_corrupt_file_recovers_from_temp(self):
        self.write("lbcam.json", "")
        self.write("lbcam.json.tmp", json.dumps({"fps": 9}))
        self.assertEqual(app.Config(self.path).get("fps"), 9)

    def test_torn_temp_is_ignored_when_file_is_fine(self):
        self.write("lbcam.json", json.dumps({"fps": 5}))
        self.write("lbcam.json.tmp", '{"fps": 1')
        c = app.Config(self.path)
        self.assertEqual(c.get("fps"), 5)
        self.assertIsNone(c.load_error)


class JobStoreRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_job_stored_but_index_not_saved(self):
        store = app.JobStore(self.tmp, keep=3)
        first = store.add(square_job(), 1000.0, 1001.0, True)
        with open(os.path.join(self.tmp, "index.json")) as f:
            before = f.read()
        second = store.add(square_job() + b"\x01", 2000.0, 2001.0, False)
        with open(os.path.join(self.tmp, "index.json"), "w") as f:
            f.write(before)  # the index save never made it
        jobs = {j["id"]: j for j in app.JobStore(self.tmp, keep=3).list()}
        self.assertEqual(set(jobs), {first, second})
        self.assertFalse(jobs[second]["complete"])  # the file doesn't end with Ruida's EOF
        self.assertEqual(jobs[second]["cuts"], app.job_cuts(square_job()))

    def test_lost_index_is_rebuilt_and_extra_files_pruned(self):
        store = app.JobStore(self.tmp, keep=10)
        ids = [store.add(square_job() + bytes([i]), 1000.0 + i * 100, 1001.0 + i * 100, True) for i in range(5)]
        os.unlink(os.path.join(self.tmp, "index.json"))
        again = app.JobStore(self.tmp, keep=3)
        self.assertEqual([j["id"] for j in again.list()], ids[:1:-1])
        self.assertEqual(sorted(f for f in os.listdir(self.tmp) if f.endswith(".rd")),
                         sorted(i + ".rd" for i in ids[2:]))

    def test_torn_job_file_and_temp_files_are_dropped(self):
        open(os.path.join(self.tmp, "20260101-120000-abcdef.rd"), "wb").close()  # zero length after a cut
        open(os.path.join(self.tmp, "index.json.tmp"), "w").close()
        self.assertEqual(app.JobStore(self.tmp).list(), [])
        self.assertEqual(os.listdir(self.tmp), [])


class LauncherInterruptedRollbackTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="lbcam-launcher-")
        for v in ("1.0", "2.0", "3.0"):
            d = os.path.join(self.home, "releases", v)
            os.makedirs(d)
            open(os.path.join(d, "lbcam_stream.py"), "w").close()
        self.L = load("launcher", {"LBCAM_HOME": self.home})

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_cut_after_current_was_restored_does_not_roll_back_again(self):
        L = self.L
        L.set_link("current", "releases/2.0")    # rollback of 3.0 already restored 2.0 ...
        L.set_link("previous", "releases/1.0")
        L.write_json(os.path.join(self.home, "pending.json"), {"version": "3.0"})  # ... then power went
        self.assertTrue(L.rollback_was_interrupted(L.read_json(os.path.join(self.home, "pending.json"))))
        self.assertEqual(os.readlink(os.path.join(self.home, "current")), "releases/2.0")
        self.assertEqual(os.readlink(os.path.join(self.home, "previous")), "releases/1.0")
        self.assertFalse(os.path.exists(os.path.join(self.home, "pending.json")))
        last = L.read_json(os.path.join(self.home, "last_update.json"))
        self.assertEqual((last["result"], last["version"], last["restored"]), ("rolled_back", "3.0", "2.0"))

    def test_update_still_on_trial_is_checked_normally(self):
        L = self.L
        L.set_link("current", "releases/3.0")
        self.assertFalse(L.rollback_was_interrupted({"version": "3.0"}))
        L.set_link("current", "releases/9.9")   # files missing: main() rolls back as before
        self.assertFalse(L.rollback_was_interrupted({"version": "9.9"}))


if __name__ == "__main__":
    unittest.main()
