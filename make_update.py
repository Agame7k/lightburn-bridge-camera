#!/usr/bin/env python3
"""
Build an update file for the Bridge camera dashboard.

  python3 make_update.py            -> lbcam-update-<version>.tar.gz

Upload it on the Bridge at http://<bridge-ip>:8080 -> Software -> Install update.
The version comes from VERSION in lbcam_stream.py - bump it for every update.
"""
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ("lbcam_stream.py", "dashboard.html", "placeholder.jpg")


def main():
    src = open(os.path.join(HERE, "lbcam_stream.py")).read()
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        sys.exit("VERSION not found in lbcam_stream.py")
    version = m.group(1)

    blobs = {}
    for name in FILES:
        with open(os.path.join(HERE, name), "rb") as f:
            blobs[name] = f.read()
    manifest = {
        "name": "lbcam",
        "version": version,
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": {n: hashlib.sha256(b).hexdigest() for n, b in blobs.items()},
    }
    blobs["manifest.json"] = json.dumps(manifest, indent=2).encode()

    out = os.path.join(os.getcwd(), "lbcam-update-%s.tar.gz" % version)
    with tarfile.open(out, "w:gz") as tf:
        for name in ["manifest.json"] + list(FILES):
            info = tarfile.TarInfo(name)
            info.size = len(blobs[name])
            info.mtime = int(time.time())
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(blobs[name]))
    print("built", out, "(%d KB)" % (os.path.getsize(out) // 1024))


if __name__ == "__main__":
    main()
