#!/usr/bin/env python3
"""
Add the lbcam camera dashboard to a LightBurn Bridge SD-card image. No root needed.

  python3 patch_image.py LightBurnBridge-v1.0-RC4.img.xz  LightBurnBridge-camera.img

Needs: debugfs + e2fsck (e2fsprogs) on Linux. The original image is never modified.

Installs (on the image's root filesystem):
  /opt/lbcam/launcher.py                     stable launcher (health check + rollback)
  /opt/lbcam/releases/<version>/...          lbcam_stream.py, dashboard.html, placeholder.jpg
  /opt/lbcam/current -> releases/<version>
  /etc/systemd/system/lbcam.service          enabled at boot
"""
import lzma
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SECTOR = 512
APP_FILES = ("lbcam_stream.py", "dashboard.html", "placeholder.jpg")


def partitions(img):
    with open(img, "rb") as f:
        mbr = f.read(512)
    if mbr[510:512] != b"\x55\xaa":
        sys.exit("not an MBR disk image")
    parts = []
    for i in range(4):
        e = mbr[446 + 16 * i: 462 + 16 * i]
        ptype, start, count = e[4], struct.unpack("<I", e[8:12])[0], struct.unpack("<I", e[12:16])[0]
        if count:
            parts.append((ptype, start, count))
    return parts


def copy_range(src, dst, src_off, length, dst_off=0, mode="r+b"):
    with open(src, "rb") as a, open(dst, mode) as b:
        a.seek(src_off)
        b.seek(dst_off)
        left = length
        while left:
            chunk = a.read(min(left, 8 << 20))
            if not chunk:
                raise IOError("short read")
            b.write(chunk)
            left -= len(chunk)


def debugfs(fsimg, cmds):
    with tempfile.NamedTemporaryFile("w", suffix=".cmds", delete=False) as f:
        f.write("\n".join(cmds) + "\n")
        path = f.name
    try:
        return subprocess.run(["debugfs", "-w", "-f", path, fsimg], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True).stdout
    finally:
        os.unlink(path)


def q(fsimg, cmd, binary=False):
    r = subprocess.run(["debugfs", "-R", cmd, fsimg], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return r.stdout if binary else r.stdout.decode(errors="replace")


def put(path_in_fs, local, mode):
    d, n = os.path.split(path_in_fs)
    return ["cd %s" % d, "rm %s" % n, "write %s %s" % (local, n),
            "sif %s mode 0%o" % (n, mode), "sif %s uid 0" % n, "sif %s gid 0" % n]


def mkdir(path_in_fs):
    return ["mkdir %s" % path_in_fs, "sif %s mode 040755" % path_in_fs,
            "sif %s uid 0" % path_in_fs, "sif %s gid 0" % path_in_fs]


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, out = sys.argv[1], sys.argv[2]
    if os.path.abspath(src) == os.path.abspath(out):
        sys.exit("output must differ from input")

    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', open(os.path.join(HERE, "lbcam_stream.py")).read(), re.M)
    if not m:
        sys.exit("VERSION not found in lbcam_stream.py")
    version = m.group(1)
    local = {n: os.path.join(HERE, n) for n in APP_FILES + ("launcher.py", "lbcam.service")}
    for p in local.values():
        if not os.path.isfile(p):
            sys.exit("missing %s" % p)

    print("Writing", out)
    if src.endswith(".xz"):
        with lzma.open(src) as a, open(out, "wb") as b:
            shutil.copyfileobj(a, b, 8 << 20)
    else:
        shutil.copyfile(src, out)

    linux = [p for p in partitions(out) if p[0] == 0x83]
    if not linux:
        sys.exit("no Linux root partition found")
    _, start, count = linux[0]

    work = tempfile.mkdtemp(prefix="lbcam-")
    root = os.path.join(work, "root.ext4")
    try:
        print("Extracting root filesystem")
        copy_range(out, root, start * SECTOR, count * SECTOR, mode="wb")

        rel = "/opt/lbcam/releases/%s" % version
        cmds = []
        # remove the v2.0 layout if present
        cmds += ["rm /usr/local/bin/lbcam_stream.py", "rm /usr/local/share/lbcam/dashboard.html",
                 "rm /usr/local/share/lbcam/placeholder.jpg", "rmdir /usr/local/share/lbcam"]
        cmds += mkdir("/opt/lbcam") + mkdir("/opt/lbcam/releases") + mkdir(rel)
        for n in APP_FILES:
            cmds += put("%s/%s" % (rel, n), local[n], 0o100755 if n.endswith(".py") else 0o100644)
        cmds += put("/opt/lbcam/launcher.py", local["launcher.py"], 0o100755)
        cmds += ["cd /opt/lbcam", "rm current", "symlink current releases/%s" % version]
        cmds += put("/etc/systemd/system/lbcam.service", local["lbcam.service"], 0o100644)
        cmds += ["cd /etc/systemd/system", "rm multi-user.target.wants/lbcam.service",
                 "symlink multi-user.target.wants/lbcam.service /etc/systemd/system/lbcam.service"]

        print("Installing lbcam", version)
        log = debugfs(root, cmds)
        bad = [l for l in log.splitlines()
               if ("rror" in l or "not found" in l or "File exists" in l)
               and not l.startswith(("rm:", "mkdir:", "rmdir:"))]  # expected on fresh images / re-runs
        if bad:
            print(log)
            sys.exit("debugfs reported errors (see above)")

        # verify (debugfs doesn't follow symlinks, so check links and targets separately)
        if '"/etc/systemd/system/lbcam.service"' not in q(root, "stat /etc/systemd/system/multi-user.target.wants/lbcam.service"):
            sys.exit("verification failed: service not enabled")
        if "/opt/lbcam/launcher.py" not in q(root, "cat /etc/systemd/system/lbcam.service"):
            sys.exit("verification failed: unit file wrong")
        if '"releases/%s"' % version not in q(root, "stat /opt/lbcam/current"):
            sys.exit("verification failed: current link")
        checks = [("%s/%s" % (rel, n), local[n]) for n in APP_FILES] + [("/opt/lbcam/launcher.py", local["launcher.py"])]
        for fs_path, lp in checks:
            with open(lp, "rb") as f:
                if q(root, "dump %s /dev/stdout" % fs_path, binary=True) != f.read():
                    sys.exit("verification failed: %s differs" % fs_path)
        subprocess.run(["e2fsck", "-fy", root], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        print("Writing root filesystem back")
        copy_range(root, out, 0, count * SECTOR, dst_off=start * SECTOR)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("Done. Flash %s to the Bridge's SD card (Raspberry Pi Imager -> Use custom)." % out)


if __name__ == "__main__":
    main()
