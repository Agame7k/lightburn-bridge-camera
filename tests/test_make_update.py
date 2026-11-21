"""make_update.py: package plus a matching sha256sum-format checksum file."""
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from helpers import ROOT, load

app = load("lbcam_stream")


class MakeUpdateTest(unittest.TestCase):
    def test_builds_package_and_sha256_file(self):
        out = tempfile.mkdtemp()
        try:
            subprocess.run([sys.executable, os.path.join(ROOT, "make_update.py")], cwd=out, check=True,
                           stdout=subprocess.PIPE)
            pkg = os.path.join(out, "lbcam-update-%s.tar.gz" % app.VERSION)
            with open(pkg, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            with open(pkg + ".sha256") as f:
                self.assertEqual(f.read(), "%s  %s\n" % (digest, os.path.basename(pkg)))
            with tarfile.open(pkg) as tf:
                self.assertEqual(sorted(tf.getnames()),
                                 ["dashboard.html", "lbcam_stream.py", "manifest.json", "placeholder.jpg"])
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
