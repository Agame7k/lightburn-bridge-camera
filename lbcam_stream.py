#!/usr/bin/env python3
"""
lbcam_stream.py - dependency-free MJPEG camera server + dashboard for the LightBurn Bridge.

Runs on the Bridge's stock Raspberry Pi OS (Python 3.7, no OpenCV/ffmpeg). Talks to V4L2
directly (ioctl + mmap) and passes the camera's own MJPEG frames through, so CPU use is tiny.

Endpoints
  /                  dashboard (camera picker, resolution/fps, image controls, status)
  /stream            MJPEG stream  -> LightBurn "network camera" (MJPEG)
  /snapshot[.jpg]    single JPEG   -> LightBurn "network camera" (snapshot)
  /status            short JSON status
  /api/...           JSON API used by the dashboard

When no camera is usable, /stream and /snapshot serve a "Camera not connected" image
(placeholder.jpg) instead of hanging, and switch back to live video automatically.

Settings are saved to CAM_CONFIG (default /boot/lbcam.json, editable from any computer).
Environment variables give the defaults: CAM_DEVICE, CAM_WIDTH, CAM_HEIGHT, CAM_FPS, CAM_PORT.
"""

import ctypes
import errno
import fcntl
import glob
import hashlib
import hmac
import io
import json
import mmap
import os
import re
import secrets
import select
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "2.2"
LBCAM_HOME = os.environ.get("LBCAM_HOME", "/opt/lbcam")

# ======================================================================== V4L2

_IOC_WRITE, _IOC_READ = 1, 2


def _IOC(d, t, nr, size):
    return (d << 30) | (size << 16) | (ord(t) << 8) | nr


def _IOR(t, nr, st):
    return _IOC(_IOC_READ, t, nr, ctypes.sizeof(st))


def _IOW(t, nr, st):
    return _IOC(_IOC_WRITE, t, nr, ctypes.sizeof(st))


def _IOWR(t, nr, st):
    return _IOC(_IOC_READ | _IOC_WRITE, t, nr, ctypes.sizeof(st))


def fourcc(s):
    return ord(s[0]) | (ord(s[1]) << 8) | (ord(s[2]) << 16) | (ord(s[3]) << 24)


def fourcc_str(v):
    return "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4))


BUF_TYPE_CAPTURE = 1
MEMORY_MMAP = 1
CAP_VIDEO_CAPTURE = 0x00000001
CAP_STREAMING = 0x04000000
CAP_DEVICE_CAPS = 0x80000000
PIX_FMT_MJPEG = fourcc("MJPG")
PIX_FMT_JPEG = fourcc("JPEG")

CTRL_TYPE_INTEGER, CTRL_TYPE_BOOLEAN, CTRL_TYPE_MENU, CTRL_TYPE_BUTTON = 1, 2, 3, 4
CTRL_TYPE_CLASS, CTRL_TYPE_INTEGER_MENU = 6, 9
CTRL_FLAG_DISABLED, CTRL_FLAG_READ_ONLY, CTRL_FLAG_INACTIVE = 0x1, 0x4, 0x10
CTRL_FLAG_NEXT_CTRL = 0x80000000

U32 = ctypes.c_uint32
S32 = ctypes.c_int32


class v4l2_capability(ctypes.Structure):
    _fields_ = [("driver", ctypes.c_char * 16), ("card", ctypes.c_char * 32), ("bus_info", ctypes.c_char * 32),
                ("version", U32), ("capabilities", U32), ("device_caps", U32), ("reserved", U32 * 3)]


class v4l2_pix_format(ctypes.Structure):
    _fields_ = [(n, U32) for n in ("width", "height", "pixelformat", "field", "bytesperline", "sizeimage",
                                   "colorspace", "priv", "flags", "ycbcr_enc", "quantization", "xfer_func")]


class _fmt_union(ctypes.Union):
    # kernel union contains pointers (v4l2_window) -> match its alignment
    _fields_ = [("pix", v4l2_pix_format), ("raw_data", ctypes.c_uint8 * 200), ("_align", ctypes.c_void_p)]


class v4l2_format(ctypes.Structure):
    _fields_ = [("type", U32), ("fmt", _fmt_union)]


class v4l2_fract(ctypes.Structure):
    _fields_ = [("numerator", U32), ("denominator", U32)]


class v4l2_captureparm(ctypes.Structure):
    _fields_ = [("capability", U32), ("capturemode", U32), ("timeperframe", v4l2_fract),
                ("extendedmode", U32), ("readbuffers", U32), ("reserved", U32 * 4)]


class _parm_union(ctypes.Union):
    _fields_ = [("capture", v4l2_captureparm), ("raw_data", ctypes.c_uint8 * 200)]


class v4l2_streamparm(ctypes.Structure):
    _fields_ = [("type", U32), ("parm", _parm_union)]


class v4l2_requestbuffers(ctypes.Structure):
    _fields_ = [("count", U32), ("type", U32), ("memory", U32), ("capabilities", U32), ("reserved", U32 * 1)]


class timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class v4l2_timecode(ctypes.Structure):
    _fields_ = [("type", U32), ("flags", U32), ("frames", ctypes.c_uint8), ("seconds", ctypes.c_uint8),
                ("minutes", ctypes.c_uint8), ("hours", ctypes.c_uint8), ("userbits", ctypes.c_uint8 * 4)]


class _buf_m(ctypes.Union):
    _fields_ = [("offset", U32), ("userptr", ctypes.c_ulong), ("planes", ctypes.c_void_p), ("fd", S32)]


class _buf_req(ctypes.Union):
    _fields_ = [("request_fd", S32), ("reserved", U32)]


class v4l2_buffer(ctypes.Structure):
    _fields_ = [("index", U32), ("type", U32), ("bytesused", U32), ("flags", U32), ("field", U32),
                ("timestamp", timeval), ("timecode", v4l2_timecode), ("sequence", U32), ("memory", U32),
                ("m", _buf_m), ("length", U32), ("reserved2", U32), ("u", _buf_req)]


class v4l2_fmtdesc(ctypes.Structure):
    _fields_ = [("index", U32), ("type", U32), ("flags", U32), ("description", ctypes.c_char * 32),
                ("pixelformat", U32), ("mbus_code", U32), ("reserved", U32 * 3)]


class _frmsize_discrete(ctypes.Structure):
    _fields_ = [("width", U32), ("height", U32)]


class _frmsize_stepwise(ctypes.Structure):
    _fields_ = [(n, U32) for n in ("min_width", "max_width", "step_width", "min_height", "max_height", "step_height")]


class _frmsize_union(ctypes.Union):
    _fields_ = [("discrete", _frmsize_discrete), ("stepwise", _frmsize_stepwise)]


class v4l2_frmsizeenum(ctypes.Structure):
    _fields_ = [("index", U32), ("pixel_format", U32), ("type", U32), ("u", _frmsize_union), ("reserved", U32 * 2)]


class _frmival_stepwise(ctypes.Structure):
    _fields_ = [("min", v4l2_fract), ("max", v4l2_fract), ("step", v4l2_fract)]


class _frmival_union(ctypes.Union):
    _fields_ = [("discrete", v4l2_fract), ("stepwise", _frmival_stepwise)]


class v4l2_frmivalenum(ctypes.Structure):
    _fields_ = [("index", U32), ("pixel_format", U32), ("width", U32), ("height", U32), ("type", U32),
                ("u", _frmival_union), ("reserved", U32 * 2)]


class v4l2_queryctrl(ctypes.Structure):
    _fields_ = [("id", U32), ("type", U32), ("name", ctypes.c_char * 32), ("minimum", S32), ("maximum", S32),
                ("step", S32), ("default_value", S32), ("flags", U32), ("reserved", U32 * 2)]


class _qmenu_union(ctypes.Union):
    _pack_ = 1
    _fields_ = [("name", ctypes.c_char * 32), ("value", ctypes.c_int64)]


class v4l2_querymenu(ctypes.Structure):
    _pack_ = 1  # __attribute__((packed)) in the kernel header
    _fields_ = [("id", U32), ("index", U32), ("u", _qmenu_union), ("reserved", U32)]


class v4l2_control(ctypes.Structure):
    _fields_ = [("id", U32), ("value", S32)]


VIDIOC_QUERYCAP = _IOR("V", 0, v4l2_capability)
VIDIOC_ENUM_FMT = _IOWR("V", 2, v4l2_fmtdesc)
VIDIOC_S_FMT = _IOWR("V", 5, v4l2_format)
VIDIOC_REQBUFS = _IOWR("V", 8, v4l2_requestbuffers)
VIDIOC_QUERYBUF = _IOWR("V", 9, v4l2_buffer)
VIDIOC_QBUF = _IOWR("V", 15, v4l2_buffer)
VIDIOC_DQBUF = _IOWR("V", 17, v4l2_buffer)
VIDIOC_STREAMON = _IOW("V", 18, ctypes.c_int)
VIDIOC_STREAMOFF = _IOW("V", 19, ctypes.c_int)
VIDIOC_S_PARM = _IOWR("V", 22, v4l2_streamparm)
VIDIOC_G_CTRL = _IOWR("V", 27, v4l2_control)
VIDIOC_S_CTRL = _IOWR("V", 28, v4l2_control)
VIDIOC_QUERYCTRL = _IOWR("V", 36, v4l2_queryctrl)
VIDIOC_QUERYMENU = _IOWR("V", 37, v4l2_querymenu)
VIDIOC_ENUM_FRAMESIZES = _IOWR("V", 74, v4l2_frmsizeenum)
VIDIOC_ENUM_FRAMEINTERVALS = _IOWR("V", 75, v4l2_frmivalenum)

# ======================================================================== helpers


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def querycap(fd):
    cap = v4l2_capability()
    fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap)
    caps = cap.device_caps if cap.capabilities & CAP_DEVICE_CAPS else cap.capabilities
    return cap, caps


def _video_sort_key(p):
    tail = p[len("/dev/video"):]
    return int(tail) if tail.isdigit() else 9999


def stable_ids():
    """Map /dev/videoN -> /dev/v4l/by-id/... so a saved camera survives re-plugging."""
    out = {}
    for link in sorted(glob.glob("/dev/v4l/by-id/*")):
        try:
            out.setdefault(os.path.realpath(link), link)
        except OSError:
            pass
    return out


def list_cameras():
    """Every UVC node that can stream video (UVC cams also expose metadata-only nodes)."""
    ids = stable_ids()
    cams = []
    for path in sorted(glob.glob("/dev/video*"), key=_video_sort_key):
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            if e.errno in (errno.EBUSY, errno.EACCES):
                cams.append({"id": ids.get(path, path), "path": path, "name": path, "error": os.strerror(e.errno)})
            continue
        try:
            cap, caps = querycap(fd)
            if cap.driver.decode(errors="ignore") != "uvcvideo":
                continue
            if not (caps & CAP_VIDEO_CAPTURE and caps & CAP_STREAMING):
                continue
            cams.append({
                "id": ids.get(path, path),
                "path": path,
                "name": cap.card.decode(errors="ignore"),
                "bus": cap.bus_info.decode(errors="ignore"),
                "mjpeg": supports_mjpeg(fd),
            })
        except OSError:
            pass
        finally:
            os.close(fd)
    return cams


def resolve_device(dev_id):
    """by-id path or /dev/videoN -> current /dev/videoN, or None if gone."""
    if not dev_id:
        return None
    try:
        real = os.path.realpath(dev_id)
    except OSError:
        return None
    return real if os.path.exists(real) else None


def supports_mjpeg(fd):
    i = 0
    while True:
        d = v4l2_fmtdesc(index=i, type=BUF_TYPE_CAPTURE)
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FMT, d)
        except OSError:
            return False
        if d.pixelformat in (PIX_FMT_MJPEG, PIX_FMT_JPEG):
            return True
        i += 1


def enum_modes(fd, pixfmt=PIX_FMT_MJPEG):
    """[{'width','height','fps':[...]}] for the MJPEG format, largest first."""
    modes = []
    i = 0
    while True:
        fs = v4l2_frmsizeenum(index=i, pixel_format=pixfmt)
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMESIZES, fs)
        except OSError:
            break
        if fs.type == 1:  # discrete
            sizes = [(fs.u.discrete.width, fs.u.discrete.height)]
        else:  # stepwise/continuous: offer common sizes inside the range
            sw = fs.u.stepwise
            common = [(640, 480), (800, 600), (1024, 768), (1280, 720), (1280, 960), (1600, 1200), (1920, 1080),
                      (2048, 1536), (2592, 1944), (3264, 2448)]
            sizes = [(w, h) for w, h in common if sw.min_width <= w <= sw.max_width and sw.min_height <= h <= sw.max_height]
        for w, h in sizes:
            modes.append({"width": w, "height": h, "fps": enum_fps(fd, pixfmt, w, h)})
        if fs.type != 1:
            break
        i += 1
    modes.sort(key=lambda m: (m["width"] * m["height"], m["width"]), reverse=True)
    return modes


def enum_fps(fd, pixfmt, w, h):
    rates = set()
    i = 0
    while True:
        fi = v4l2_frmivalenum(index=i, pixel_format=pixfmt, width=w, height=h)
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, fi)
        except OSError:
            break
        if fi.type == 1:
            f = fi.u.discrete
            if f.numerator:
                rates.add(round(f.denominator / f.numerator))
            i += 1
        else:
            lo, hi = fi.u.stepwise.max, fi.u.stepwise.min  # max interval = min fps
            lo_fps = lo.denominator / lo.numerator if lo.numerator else 1
            hi_fps = hi.denominator / hi.numerator if hi.numerator else 30
            rates.update(r for r in (1, 2, 5, 10, 15, 20, 24, 25, 30, 60) if lo_fps <= r <= hi_fps)
            break
    return sorted(r for r in rates if r > 0)


def list_controls(fd):
    ctrls = []
    qid = CTRL_FLAG_NEXT_CTRL
    while True:
        q = v4l2_queryctrl(id=qid)
        try:
            fcntl.ioctl(fd, VIDIOC_QUERYCTRL, q)
        except OSError:
            break
        qid = q.id | CTRL_FLAG_NEXT_CTRL
        if q.type in (CTRL_TYPE_CLASS, CTRL_TYPE_BUTTON) or q.flags & CTRL_FLAG_DISABLED:
            continue
        if q.type not in (CTRL_TYPE_INTEGER, CTRL_TYPE_BOOLEAN, CTRL_TYPE_MENU, CTRL_TYPE_INTEGER_MENU):
            continue
        c = {
            "id": q.id,
            "name": q.name.decode(errors="ignore"),
            "type": {1: "int", 2: "bool", 3: "menu", 9: "menu"}[q.type],
            "min": q.minimum, "max": q.maximum, "step": q.step or 1, "default": q.default_value,
            "readonly": bool(q.flags & CTRL_FLAG_READ_ONLY),
            "inactive": bool(q.flags & CTRL_FLAG_INACTIVE),
        }
        try:
            c["value"] = get_control(fd, q.id)
        except OSError:
            c["value"] = q.default_value
        if q.type in (CTRL_TYPE_MENU, CTRL_TYPE_INTEGER_MENU):
            items = []
            for idx in range(q.minimum, q.maximum + 1):
                m = v4l2_querymenu(id=q.id, index=idx)
                try:
                    fcntl.ioctl(fd, VIDIOC_QUERYMENU, m)
                except OSError:
                    continue
                label = m.u.name.decode(errors="ignore") if q.type == CTRL_TYPE_MENU else str(m.u.value)
                items.append({"value": idx, "label": label})
            c["menu"] = items
        ctrls.append(c)
    return ctrls


def get_control(fd, cid):
    c = v4l2_control(id=cid)
    fcntl.ioctl(fd, VIDIOC_G_CTRL, c)
    return c.value


def set_control(fd, cid, value):
    fcntl.ioctl(fd, VIDIOC_S_CTRL, v4l2_control(id=int(cid), value=int(value)))


def find_asset(name):
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.environ.get("CAM_ASSETS", ""), here, "/usr/local/share/lbcam"):
        p = os.path.join(d, name) if d else ""
        if p and os.path.isfile(p):
            return p
    return None


def read_asset(name, default=b""):
    p = find_asset(name)
    if not p:
        return default
    with open(p, "rb") as f:
        return f.read()


# ======================================================================== config


class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {
            "device": os.environ.get("CAM_DEVICE") or "auto",
            "width": int(os.environ.get("CAM_WIDTH", "1280")),
            "height": int(os.environ.get("CAM_HEIGHT", "720")),
            "fps": int(os.environ.get("CAM_FPS", "15")),
            "controls": {},  # {camera id: {control id (str): value}}
            "update_auth": None,  # {"salt": hex, "hash": hex} - password for software updates
        }
        self.load_error = None
        self.save_error = None
        try:
            with open(path) as f:
                saved = json.load(f)
            for k in ("device", "width", "height", "fps", "controls", "update_auth"):
                if k in saved:
                    self.data[k] = saved[k]
        except FileNotFoundError:
            pass
        except Exception as e:
            self.load_error = "could not read %s: %s (using defaults)" % (path, e)
            log(self.load_error)

    def get(self, k):
        with self.lock:
            return self.data[k]

    def update(self, **kw):
        with self.lock:
            self.data.update(kw)
            self._save()

    def set_control(self, cam_id, cid, value):
        with self.lock:
            self.data["controls"].setdefault(cam_id, {})[str(cid)] = int(value)
            self._save()

    def clear_controls(self, cam_id):
        with self.lock:
            self.data["controls"].pop(cam_id, None)
            self._save()

    def controls_for(self, cam_id):
        with self.lock:
            return dict(self.data["controls"].get(cam_id, {}))

    def public(self):
        with self.lock:
            d = {k: self.data[k] for k in ("device", "width", "height", "fps")}
        d["path"] = self.path
        d["load_error"] = self.load_error
        d["save_error"] = self.save_error
        return d

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self.save_error = None
        except Exception as e:
            self.save_error = "could not save settings to %s: %s" % (self.path, e)
            log(self.save_error)


# ======================================================================== updates

UPDATE_FILES = ("lbcam_stream.py", "dashboard.html", "placeholder.jpg")
MAX_UPDATE_BYTES = 10 * 1024 * 1024
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$")


class UpdateError(Exception):
    pass


class Updater:
    """Installs uploaded update packages into LBCAM_HOME/releases and hands over to the launcher."""

    def __init__(self, cfg, home=LBCAM_HOME):
        self.cfg = cfg
        self.home = home
        self.lock = threading.Lock()
        self.fail_time = 0.0

    # ------------------------------------------------------------ password
    @staticmethod
    def _hash(pw, salt):
        return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 60000).hex()

    def password_set(self):
        return bool(self.cfg.get("update_auth"))

    def check_password(self, pw):
        auth = self.cfg.get("update_auth")
        if not auth:
            return  # no password set: updates are open (fine on a trusted home network)
        ok = hmac.compare_digest(self._hash(pw or "", auth["salt"]), auth["hash"])
        if not ok:
            time.sleep(1.0)  # slow down guessing
            raise PermissionError("Wrong password")

    def set_password(self, new, current=None):
        """Set/change the optional update password. An empty new password removes it."""
        if self.password_set():
            self.check_password(current)
        if not new:
            self.cfg.update(update_auth=None)
        elif len(new) < 6:
            raise UpdateError("Password must be at least 6 characters")
        else:
            salt = secrets.token_hex(16)
            self.cfg.update(update_auth={"salt": salt, "hash": self._hash(new, salt)})
        if self.cfg.save_error:
            raise UpdateError(self.cfg.save_error)

    # ------------------------------------------------------------ info
    def managed(self):
        """True when running from the launcher layout (updates possible)."""
        return os.path.islink(os.path.join(self.home, "current")) and \
            os.path.isfile(os.path.join(self.home, "launcher.py"))

    def _link_version(self, name):
        try:
            return os.path.basename(os.readlink(os.path.join(self.home, name)).rstrip("/"))
        except OSError:
            return None

    def info(self):
        last = None
        try:
            with open(os.path.join(self.home, "last_update.json")) as f:
                last = json.load(f)
        except (OSError, ValueError):
            pass
        return {
            "version": VERSION,
            "managed": self.managed(),
            "previous": self._link_version("previous") if self.managed() else None,
            "password_set": self.password_set(),
            "last_update": last,
            "home": self.home,
        }

    # ------------------------------------------------------------ install
    def install(self, blob, pw):
        self.check_password(pw)
        if not self.managed():
            raise UpdateError("This install can't self-update (not running under the lbcam launcher)")
        with self.lock:
            files, manifest = self._unpack(blob)
            version = manifest["version"]
            if version == VERSION:
                raise UpdateError("Version %s is already running" % version)
            releases = os.path.join(self.home, "releases")
            os.makedirs(releases, exist_ok=True)
            stage = os.path.join(releases, ".stage-%s" % secrets.token_hex(4))
            os.makedirs(stage)
            try:
                for name, data in files.items():
                    with open(os.path.join(stage, name), "wb") as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                with open(os.path.join(stage, "manifest.json"), "w") as f:
                    json.dump(manifest, f, indent=2)
                self._selftest(stage, version)
                final = os.path.join(releases, version)
                if os.path.exists(final):
                    shutil.rmtree(final)
                os.rename(stage, final)
            except Exception:
                shutil.rmtree(stage, ignore_errors=True)
                raise
            self._switch("releases/" + version, version)
            self._prune()
            return version

    def rollback(self, pw):
        self.check_password(pw)
        if not self.managed():
            raise UpdateError("This install can't self-update")
        with self.lock:
            prev = self._link_version("previous")
            if not prev or not os.path.isfile(os.path.join(self.home, "releases", prev, "lbcam_stream.py")):
                raise UpdateError("No previous version to roll back to")
            self._switch("releases/" + prev, prev)
            return prev

    def _unpack(self, blob):
        if len(blob) > MAX_UPDATE_BYTES:
            raise UpdateError("Update file is too large")
        try:
            tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")
        except (tarfile.TarError, OSError, EOFError):
            raise UpdateError("Not an lbcam update file (expected .tar.gz from make_update.py)")
        files, manifest = {}, None
        with tf:
            for m in tf.getmembers():
                name = m.name[2:] if m.name.startswith("./") else m.name
                if not m.isfile() or "/" in name or name not in UPDATE_FILES + ("manifest.json",):
                    raise UpdateError("Unexpected item in update file: %s" % m.name)
                data = tf.extractfile(m).read()
                if name == "manifest.json":
                    try:
                        manifest = json.loads(data.decode())
                    except ValueError:
                        raise UpdateError("Update manifest is not valid JSON")
                else:
                    files[name] = data
        if not manifest or manifest.get("name") != "lbcam":
            raise UpdateError("Update file has no lbcam manifest")
        version = str(manifest.get("version", ""))
        if not VERSION_RE.match(version):
            raise UpdateError("Update has an invalid version: %r" % version)
        if "lbcam_stream.py" not in files:
            raise UpdateError("Update is missing lbcam_stream.py")
        sums = manifest.get("files") or {}
        if set(sums) != set(files):
            raise UpdateError("Update manifest doesn't match its files")
        for name, data in files.items():
            if hashlib.sha256(data).hexdigest() != sums[name]:
                raise UpdateError("%s is corrupted (checksum mismatch)" % name)
        # carry over assets the package didn't include
        cur = os.path.join(self.home, "current")
        for name in UPDATE_FILES:
            if name not in files and os.path.isfile(os.path.join(cur, name)):
                with open(os.path.join(cur, name), "rb") as f:
                    files[name] = f.read()
        manifest["version"] = version
        return files, manifest

    def _selftest(self, stage, version):
        try:
            r = subprocess.run([sys.executable, os.path.join(stage, "lbcam_stream.py"), "--selftest"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
                               universal_newlines=True)
        except subprocess.TimeoutExpired:
            raise UpdateError("New version's self-test timed out")
        out = (r.stdout or "").strip()
        if r.returncode != 0 or ("selftest ok " + version) not in out:
            tail = "\n".join(out.splitlines()[-6:])
            raise UpdateError("New version failed its self-test on this Bridge:\n" + tail)

    def _switch(self, target, version):
        home = self.home
        cur = os.readlink(os.path.join(home, "current"))
        try:
            old_prev = os.readlink(os.path.join(home, "previous"))
        except OSError:
            old_prev = None

        def link(name, tgt):
            tmp = os.path.join(home, name + ".tmp")
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            os.symlink(tgt, tmp)
            os.replace(tmp, os.path.join(home, name))

        link("previous", cur)
        link("current", target)
        tmp = os.path.join(home, "pending.json.tmp")
        with open(tmp, "w") as f:
            # restore_previous: what "previous" should point at again if this version gets rolled back
            json.dump({"version": version, "from": VERSION, "time": int(time.time()),
                       "restore_previous": old_prev}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(home, "pending.json"))
        log("switching to", version, "- restarting")
        threading.Timer(1.0, os._exit, [0]).start()  # launcher starts + health-checks the new one

    def _prune(self, keep=3):
        rel = os.path.join(self.home, "releases")
        protected = {self._link_version("current"), self._link_version("previous")}
        dirs = [d for d in os.listdir(rel) if not d.startswith(".") and os.path.isdir(os.path.join(rel, d))]
        dirs.sort(key=lambda d: os.path.getmtime(os.path.join(rel, d)), reverse=True)
        for d in dirs[keep:]:
            if d not in protected:
                shutil.rmtree(os.path.join(rel, d), ignore_errors=True)


# ======================================================================== camera


class Camera:
    """Owns the capture device. Publishes live frames, or the placeholder when no camera is usable."""

    def __init__(self, cfg, placeholder):
        self.cfg = cfg
        self.placeholder = placeholder
        self.state = "starting"  # starting | searching | streaming | error
        self.message = ""
        self.info = {}
        self.cam_id = None
        self.fd = None
        self.fd_lock = threading.RLock()
        self.frame = None
        self.frame_id = 0
        self.live = False
        self.cond = threading.Condition()
        self.reopen = threading.Event()
        self.running = True
        self.fps_measured = 0.0
        self.clients = 0
        self.clients_lock = threading.Lock()
        self.started = time.time()
        self.last_error_time = None

    # ------------------------------------------------------------ state

    def _set(self, state, message=""):
        if state != self.state or message != self.message:
            log("state:", state, message)
            if state == "error":
                self.last_error_time = time.time()
        self.state, self.message = state, message

    def _publish(self, data, live):
        with self.cond:
            self.frame = data
            self.frame_id += 1
            self.live = live
            self.cond.notify_all()

    def wait_frame(self, last_id, timeout=5.0):
        with self.cond:
            if self.frame_id == last_id:
                self.cond.wait(timeout)
            return self.frame_id, self.frame

    def _idle(self, seconds):
        """Sleep while serving the placeholder about once a second; return early on reopen."""
        end = time.time() + seconds
        while self.running and not self.reopen.is_set() and time.time() < end:
            if self.placeholder:
                self._publish(self.placeholder, False)
            self.reopen.wait(min(1.0, max(0.0, end - time.time())))

    # ------------------------------------------------------------ choose device

    def _pick(self):
        want = self.cfg.get("device")
        cams = list_cameras()
        usable = [c for c in cams if c.get("mjpeg")]
        if want and want != "auto":
            path = resolve_device(want)
            if path:
                return want, path, None
            if usable:
                c = usable[0]
                return c["id"], c["path"], "Saved camera not found - using %s instead" % c["name"]
            return None, None, "Saved camera is not plugged in"
        if usable:
            return usable[0]["id"], usable[0]["path"], None
        if cams:
            bad = cams[0]
            if bad.get("error"):
                return None, None, "%s is busy or not accessible (%s)" % (bad["path"], bad["error"])
            return None, None, "%s does not support MJPEG - use an MJPEG-capable UVC camera" % bad["name"]
        return None, None, "No USB camera detected"

    # ------------------------------------------------------------ open / close

    def _open(self, path, cam_id):
        w, h, fps = self.cfg.get("width"), self.cfg.get("height"), self.cfg.get("fps")
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        maps = []
        try:
            cap, _ = querycap(fd)
            fmt = v4l2_format(type=BUF_TYPE_CAPTURE)
            fmt.fmt.pix.width, fmt.fmt.pix.height = w, h
            fmt.fmt.pix.pixelformat = PIX_FMT_MJPEG
            fcntl.ioctl(fd, VIDIOC_S_FMT, fmt)
            if fmt.fmt.pix.pixelformat not in (PIX_FMT_MJPEG, PIX_FMT_JPEG):
                raise RuntimeError("camera does not support MJPEG output (offers %s)" % fourcc_str(fmt.fmt.pix.pixelformat))

            parm = v4l2_streamparm(type=BUF_TYPE_CAPTURE)
            parm.parm.capture.timeperframe.numerator = 1
            parm.parm.capture.timeperframe.denominator = max(1, int(fps))
            try:
                fcntl.ioctl(fd, VIDIOC_S_PARM, parm)
            except OSError:
                pass

            self._apply_saved_controls(fd, cam_id)

            req = v4l2_requestbuffers(count=4, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP)
            fcntl.ioctl(fd, VIDIOC_REQBUFS, req)
            if req.count < 2:
                raise RuntimeError("camera gave too few buffers")
            for i in range(req.count):
                buf = v4l2_buffer(index=i, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP)
                fcntl.ioctl(fd, VIDIOC_QUERYBUF, buf)
                maps.append(mmap.mmap(fd, buf.length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                                      offset=buf.m.offset))
                fcntl.ioctl(fd, VIDIOC_QBUF, buf)
            fcntl.ioctl(fd, VIDIOC_STREAMON, ctypes.c_int(BUF_TYPE_CAPTURE))
        except Exception:
            self._close(fd, maps, streaming=False)
            raise

        tpf = parm.parm.capture.timeperframe
        actual = (fmt.fmt.pix.width, fmt.fmt.pix.height)
        self.info = {
            "device": path,
            "id": cam_id,
            "name": cap.card.decode(errors="ignore"),
            "bus": cap.bus_info.decode(errors="ignore"),
            "width": actual[0],
            "height": actual[1],
            "fps": round(tpf.denominator / tpf.numerator, 2) if tpf.numerator else None,
            "requested": {"width": w, "height": h, "fps": fps},
        }
        return fd, maps

    def _apply_saved_controls(self, fd, cam_id):
        saved = self.cfg.controls_for(cam_id)
        if not saved:
            return
        # auto/mode switches (menus, booleans) first so the manual values they unlock stick
        try:
            kinds = {c["id"]: c["type"] for c in list_controls(fd)}
        except OSError:
            kinds = {}
        order = sorted(saved.items(), key=lambda kv: 0 if kinds.get(int(kv[0])) in ("menu", "bool") else 1)
        for cid, val in order:
            try:
                set_control(fd, int(cid), val)
            except OSError as e:
                log("could not restore control", cid, "=", val, ":", e)

    def _close(self, fd, maps, streaming=True):
        if streaming:
            try:
                fcntl.ioctl(fd, VIDIOC_STREAMOFF, ctypes.c_int(BUF_TYPE_CAPTURE))
            except OSError:
                pass
        for m in maps:
            try:
                m.close()
            except Exception:
                pass
        try:
            os.close(fd)
        except OSError:
            pass

    # ------------------------------------------------------------ main loop

    def run(self):
        while self.running:
            self.reopen.clear()
            cam_id, path, note = self._pick()
            if not path:
                self.info = {}
                self.cam_id = None
                self._set("searching", note)
                self._idle(2)
                continue
            try:
                fd, maps = self._open(path, cam_id)
            except Exception as e:
                msg = os.strerror(e.errno) if isinstance(e, OSError) and e.errno else str(e)
                if isinstance(e, OSError) and e.errno == errno.EBUSY:
                    msg = "%s is in use by another program" % path
                self._set("error", "Could not start camera: %s" % msg)
                self._idle(3)
                continue
            with self.fd_lock:
                self.fd, self.cam_id = fd, cam_id
            self._set("streaming", note or "")
            log("streaming", self.info)
            try:
                self._loop(fd, maps)
                if self.reopen.is_set():
                    self._set("starting", "Applying new settings")
            except Exception as e:
                gone = not os.path.exists(path)
                self._set("searching" if gone else "error",
                          "Camera unplugged" if gone else "Capture stopped: %s" % e)
            finally:
                with self.fd_lock:
                    self.fd = None
                    self._close(fd, maps)
                self.fps_measured = 0.0
            if not self.reopen.is_set():
                self._idle(1)

    def _loop(self, fd, maps):
        count, t0 = 0, time.time()
        bad = 0
        while self.running and not self.reopen.is_set():
            r, _, _ = select.select([fd], [], [], 3.0)
            if not r:
                raise RuntimeError("no frames from camera for 3 s")
            buf = v4l2_buffer(type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP)
            fcntl.ioctl(fd, VIDIOC_DQBUF, buf)
            data = maps[buf.index][: buf.bytesused]
            fcntl.ioctl(fd, VIDIOC_QBUF, buf)
            if len(data) < 128 or data[:2] != b"\xff\xd8":
                bad += 1
                if bad > 50:
                    raise RuntimeError("camera is sending corrupt frames")
                continue
            bad = 0
            self._publish(data, True)
            count += 1
            now = time.time()
            if now - t0 >= 2.0:
                self.fps_measured = round(count / (now - t0), 1)
                count, t0 = 0, now

    # ------------------------------------------------------------ controls

    def controls(self):
        with self.fd_lock:
            if self.fd is None:
                return []
            return list_controls(self.fd)

    def modes(self, dev_id=None):
        """Supported MJPEG modes for a camera (current one if streaming and dev_id matches)."""
        with self.fd_lock:
            if self.fd is not None and (dev_id is None or dev_id == self.cam_id):
                return enum_modes(self.fd)
        path = resolve_device(dev_id) if dev_id else None
        if not path:
            return []
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            return []
        try:
            return enum_modes(fd)
        finally:
            os.close(fd)

    def set_control(self, cid, value):
        with self.fd_lock:
            if self.fd is None:
                raise RuntimeError("camera is not streaming")
            set_control(self.fd, cid, value)
            actual = get_control(self.fd, cid)
            cam_id = self.cam_id
        self.cfg.set_control(cam_id, cid, value)
        return actual

    def reset_controls(self):
        with self.fd_lock:
            if self.fd is None:
                raise RuntimeError("camera is not streaming")
            ctrls = list_controls(self.fd)
            for c in sorted(ctrls, key=lambda c: 0 if c["type"] in ("menu", "bool") else 1):
                if not c["readonly"]:
                    try:
                        set_control(self.fd, c["id"], c["default"])
                    except OSError:
                        pass
            cam_id = self.cam_id
        self.cfg.clear_controls(cam_id)

    def client(self, delta):
        with self.clients_lock:
            self.clients += delta


# ======================================================================== HTTP

BOUNDARY = "lbcamframe"


def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return ips


def make_handler(cam, cfg, dashboard, updater):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "lbcam/" + VERSION

        def log_message(self, fmt, *args):
            pass

        # ---------------------------------------------------- utils
        def _send(self, code, ctype, body, head=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, "application/json", json.dumps(obj).encode())

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 65536:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode() or "{}")
            except ValueError:
                return None

        # ---------------------------------------------------- routes
        def do_HEAD(self):
            self._route(head=True)

        def do_GET(self):
            self._route(head=False)

        def _route(self, head):
            u = urlparse(self.path)
            p = u.path.rstrip("/") or "/"
            if p in ("/", "/index.html"):
                return self._send(200, "text/html; charset=utf-8", dashboard, head)
            if p in ("/stream", "/stream.mjpg", "/video", "/mjpeg"):
                if head:
                    return self._send(200, "multipart/x-mixed-replace; boundary=" + BOUNDARY, b"", True)
                return self._stream()
            if p in ("/snapshot", "/snapshot.jpg", "/image.jpg", "/jpg"):
                _, frame = cam.wait_frame(-1, 0)
                if frame is None:
                    _, frame = cam.wait_frame(0, 3)
                if frame is None:
                    return self._send(503, "text/plain", b"camera starting", head)
                return self._send(200, "image/jpeg", frame, head)
            if p == "/status":
                return self._json(self._status())
            if p == "/api/state":
                return self._json(self._state())
            if p == "/api/cameras":
                return self._json(list_cameras())
            if p == "/api/modes":
                dev = (parse_qs(u.query).get("device") or [None])[0]
                return self._json(cam.modes(dev if dev and dev != "auto" else None))
            if p == "/api/controls":
                return self._json(cam.controls())
            if p == "/api/update":
                return self._json(updater.info())
            self._send(404, "text/plain", b"not found", head)

        def do_POST(self):
            p = urlparse(self.path).path.rstrip("/")
            if p == "/api/update":
                return self._upload()
            body = self._body()
            if body is None:
                return self._json({"error": "invalid JSON"}, 400)
            try:
                if p == "/api/update/password":
                    updater.set_password(str(body.get("new") or ""), body.get("current"))
                    return self._json({"ok": True})
                if p == "/api/update/rollback":
                    v = updater.rollback(body.get("password"))
                    return self._json({"ok": True, "version": v, "restarting": True})
                if p == "/api/config":
                    upd = {}
                    if "device" in body:
                        upd["device"] = str(body["device"]) or "auto"
                    for k in ("width", "height", "fps"):
                        if k in body:
                            v = int(body[k])
                            if not (1 <= v <= 10000):
                                raise ValueError("%s out of range" % k)
                            upd[k] = v
                    cfg.update(**upd)
                    cam.reopen.set()
                    return self._json({"ok": True, "config": cfg.public()})
                if p == "/api/control":
                    actual = cam.set_control(int(body["id"]), int(body["value"]))
                    return self._json({"ok": True, "value": actual, "controls": cam.controls()})
                if p == "/api/controls/reset":
                    cam.reset_controls()
                    return self._json({"ok": True, "controls": cam.controls()})
                if p == "/api/restart":
                    cam.reopen.set()
                    return self._json({"ok": True})
            except UpdateError as e:
                return self._json({"error": str(e)}, 400)
            except PermissionError as e:
                return self._json({"error": str(e)}, 403)
            except (KeyError, ValueError, TypeError) as e:
                return self._json({"error": "bad request: %s" % e}, 400)
            except OSError as e:
                return self._json({"error": "camera rejected it: %s" % os.strerror(e.errno or 0)}, 409)
            except RuntimeError as e:
                return self._json({"error": str(e)}, 409)
            self._json({"error": "not found"}, 404)

        def _upload(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return self._json({"error": "no file received"}, 400)
            if n > MAX_UPDATE_BYTES:
                return self._json({"error": "update file is too large"}, 413)
            blob = b""
            while len(blob) < n:
                chunk = self.rfile.read(min(65536, n - len(blob)))
                if not chunk:
                    break
                blob += chunk
            if len(blob) != n:
                return self._json({"error": "upload was cut off"}, 400)
            try:
                v = updater.install(blob, self.headers.get("X-Password"))
            except PermissionError as e:
                return self._json({"error": str(e)}, 403)
            except UpdateError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"error": "could not install: %s" % e}, 500)
            log("installed update", v)
            self._json({"ok": True, "version": v, "restarting": True})

        # ---------------------------------------------------- payloads
        def _status(self):
            return {"version": VERSION, "state": cam.state, "message": cam.message, "live": cam.live, "camera": cam.info,
                    "fps": cam.fps_measured, "clients": cam.clients}

        def _state(self):
            s = self._status()
            s.update({
                "version": VERSION,
                "config": cfg.public(),
                "cameras": list_cameras(),
                "uptime": int(time.time() - cam.started),
                "ips": local_ips(),
                "placeholder": bool(cam.placeholder),
            })
            return s

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + BOUNDARY)
            self.send_header("Cache-Control", "no-cache, no-store, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            cam.client(+1)
            log("client connected", self.client_address[0])
            last = 0
            try:
                while True:
                    fid, frame = cam.wait_frame(last)
                    if frame is None or fid == last:
                        continue
                    last = fid
                    self.wfile.write(
                        b"--" + BOUNDARY.encode() + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                cam.client(-1)
                log("client left", self.client_address[0])

    return Handler


def selftest():
    """Run by the updater on the Bridge before switching to this version."""
    problems = []
    expect = {v4l2_capability: 104, v4l2_requestbuffers: 20, v4l2_queryctrl: 68, v4l2_querymenu: 44,
              v4l2_control: 8, v4l2_fmtdesc: 64, v4l2_frmsizeenum: 44, v4l2_frmivalenum: 52}
    for st, size in expect.items():
        if ctypes.sizeof(st) != size:
            problems.append("%s is %d bytes, expected %d" % (st.__name__, ctypes.sizeof(st), size))
    if ctypes.sizeof(v4l2_buffer) not in (68, 88):
        problems.append("v4l2_buffer has unexpected size %d" % ctypes.sizeof(v4l2_buffer))
    if ctypes.sizeof(v4l2_format) not in (204, 208):
        problems.append("v4l2_format has unexpected size %d" % ctypes.sizeof(v4l2_format))
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("dashboard.html", "placeholder.jpg"):
        if not os.path.isfile(os.path.join(here, name)):
            problems.append("missing " + name)
    make_handler(None, None, b"", None)  # builds the request handler class
    if problems:
        print("selftest FAILED:", "; ".join(problems))
        return 1
    print("selftest ok", VERSION)
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    port = int(os.environ.get("CAM_PORT", "8080"))
    if os.environ.get("CAM_CONFIG"):
        cfg_path = os.environ["CAM_CONFIG"]
    elif os.path.isdir("/boot/overlays"):  # Raspberry Pi: FAT boot partition, editable from any computer
        cfg_path = "/boot/lbcam.json"
    elif os.path.isdir(LBCAM_HOME):  # survives updates (release dirs are replaced)
        cfg_path = os.path.join(LBCAM_HOME, "lbcam.json")
    else:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lbcam.json")
    cfg = Config(cfg_path)
    placeholder = read_asset("placeholder.jpg")
    if not placeholder:
        log("warning: placeholder.jpg not found - stream will pause while no camera is connected")
    dashboard = read_asset("dashboard.html", b"<h1>lbcam</h1><p>dashboard.html missing</p><img src='/stream'>")

    cam = Camera(cfg, placeholder)
    updater = Updater(cfg)
    threading.Thread(target=cam.run, daemon=True).start()

    srv = None
    for attempt in range(30):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(cam, cfg, dashboard, updater))
            break
        except OSError as e:
            log("port %d unavailable (%s), retrying..." % (port, e))
            time.sleep(2)
    if srv is None:
        log("giving up: port %d stays busy" % port)
        return 1
    srv.daemon_threads = True
    log("lbcam %s on port %d, settings: %s" % (VERSION, port, cfg_path))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    cam.running = False
    return 0


if __name__ == "__main__":
    sys.exit(main())
