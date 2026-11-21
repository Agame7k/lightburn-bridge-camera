#!/usr/bin/env python3
"""
lbcam_stream.py - dependency-free MJPEG camera server + dashboard for the LightBurn Bridge.

Runs on the Bridge's stock Raspberry Pi OS (Python 3.7, no OpenCV/ffmpeg). Talks to V4L2
directly (ioctl + mmap) and passes the camera's own MJPEG frames through, so CPU use is tiny.

Endpoints
  /                  dashboard (camera picker, resolution/fps, image controls, status)
  /monitor           monitoring view (live camera, job, laser controller, Bridge health)
  /stream            MJPEG stream  -> LightBurn "network camera" (MJPEG)
  /snapshot[.jpg]    single JPEG   -> LightBurn "network camera" (snapshot)
  /status            short JSON status
  /api/...           JSON API used by the dashboard
                     (POST /api/laser/command {"cmd": pause|resume|stop} commands the laser)

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
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "2.15"
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


import collections

LOG_LINES = collections.deque(maxlen=400)  # recent log for the dashboard


def log(*a):
    line = " ".join([time.strftime("%H:%M:%S")] + [str(x) for x in a])
    LOG_LINES.append(line)
    print(line, flush=True)


def recent_log(n=300):
    """Service log incl. launcher/update messages (journald on the Bridge), else this process's lines."""
    n = max(1, min(int(n), 1000))
    try:
        r = subprocess.run(["journalctl", "-u", "lbcam", "-n", str(n), "--no-pager", "-o", "cat"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5)
        lines = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith("-- ")]
        if r.returncode == 0 and lines:
            return {"source": "journal", "lines": lines}
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"source": "process", "lines": list(LOG_LINES)[-n:]}


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


_cam_cache = {"t": 0.0, "nodes": None, "list": []}
_cam_cache_lock = threading.Lock()
CAMERA_CACHE_SECONDS = 10.0


def cameras_cached(max_age=CAMERA_CACHE_SECONDS):
    """
    list_cameras() for the dashboard's polling, reused for up to max_age seconds.
    Refreshed immediately when /dev/video* nodes appear/disappear (plug/unplug).
    """
    nodes = tuple(sorted(glob.glob("/dev/video*")))
    with _cam_cache_lock:
        c = _cam_cache
        if c["nodes"] == nodes and time.time() - c["t"] < max_age:
            return c["list"]
    fresh = list_cameras()
    with _cam_cache_lock:
        _cam_cache.update(t=time.time(), nodes=nodes, list=fresh)
    return fresh


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
        self._written = None   # last JSON written, to skip identical writes
        self._timer = None     # pending batched save
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
            self._save_later()

    def clear_controls(self, cam_id):
        with self.lock:
            self.data["controls"].pop(cam_id, None)
            self._save_later()

    SAVE_DELAY = 3.0  # seconds; slider drags become one write instead of dozens

    def _save_later(self):
        """Batch frequent changes (image controls) into one write - easier on the SD card."""
        if self._timer is None:
            self._timer = threading.Timer(self.SAVE_DELAY, self.flush)
            self._timer.daemon = True
            self._timer.start()

    def flush(self):
        """Write any batched changes now (also called on shutdown)."""
        with self.lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
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
        text = json.dumps(self.data, indent=2)
        if text == self._written:
            return  # nothing changed - don't touch the SD card
        if self._timer is not None:  # an immediate save covers any batched one
            self._timer.cancel()
            self._timer = None
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self._written = text
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
            "sha256": self._package_sha256(),
            "last_update": last,
            "home": self.home,
        }

    def _package_sha256(self):
        """SHA-256 of the update file the running release was installed from (None if unknown)."""
        try:
            with open(os.path.join(self.home, "current", "manifest.json")) as f:
                return json.load(f).get("package_sha256")
        except (OSError, ValueError, AttributeError):
            return None

    # ------------------------------------------------------------ install
    def install(self, blob, pw, expect_sha256=None):
        self.check_password(pw)
        if not self.managed():
            raise UpdateError("This install can't self-update (not running under the lbcam launcher)")
        digest = hashlib.sha256(blob).hexdigest()
        if expect_sha256 and expect_sha256.strip().lower() != digest:
            raise UpdateError("Update file SHA-256 doesn't match (got %s)" % digest)
        with self.lock:
            files, manifest = self._unpack(blob)
            manifest["package_sha256"] = digest
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
        threading.Timer(1.0, self.restart).start()  # launcher starts + health-checks the new one

    def restart(self):
        """Exit so the launcher starts the new current release (tests replace this)."""
        self.cfg.flush()
        os._exit(0)

    def _prune(self, keep=3):
        rel = os.path.join(self.home, "releases")
        protected = {self._link_version("current"), self._link_version("previous")}
        dirs = [d for d in os.listdir(rel) if not d.startswith(".") and os.path.isdir(os.path.join(rel, d))]
        dirs.sort(key=lambda d: os.path.getmtime(os.path.join(rel, d)), reverse=True)
        for d in dirs[keep:]:
            if d not in protected:
                shutil.rmtree(os.path.join(rel, d), ignore_errors=True)


# ======================================================================== network (WiFi IP)

NET_CONFIRM_SECONDS = int(os.environ.get("LBCAM_NET_CONFIRM", "90"))


class NetError(Exception):
    pass


def _nm_split(line):
    """Split one `nmcli -t` line on unescaped ':' and unescape '\\:' / '\\\\'."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    out.append(cur)
    return out


class Network:
    """
    Static IP / DHCP for the Bridge's WiFi connection via NetworkManager (nmcli).

    Never touches the laser's Ethernet link. Every change must be confirmed from the
    new address within NET_CONFIRM_SECONDS, otherwise the previous settings come back
    (also after a crash or reboot, via net_pending.json).
    LBCAM_NET_DRYRUN=1 logs changes instead of applying them (for testing on a PC).
    """

    def __init__(self, home=LBCAM_HOME):
        self.pending_path = os.path.join(home if os.path.isdir(home) else
                                         os.path.dirname(os.path.abspath(__file__)), "net_pending.json")
        self.dry = os.environ.get("LBCAM_NET_DRYRUN") == "1"
        self.lock = threading.Lock()
        self.timer = None
        self.last_error = None
        p = self._pending()
        if p:  # resume the confirm countdown after a restart
            self._arm(max(1.0, p["deadline"] - time.time()))

    # ------------------------------------------------------------ nmcli
    def _run(self, args, write=False, timeout=20):
        if write and self.dry:
            log("[net dry-run] nmcli", " ".join(args))
            return ""
        try:
            r = subprocess.run(["nmcli"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=timeout)
        except FileNotFoundError:
            raise NetError("NetworkManager (nmcli) is not available on this system")
        except subprocess.TimeoutExpired:
            raise NetError("nmcli timed out")
        if r.returncode != 0:
            raise NetError((r.stderr or r.stdout).strip() or "nmcli failed")
        return r.stdout

    def _fields(self, args):
        d = {}
        for line in self._run(args).splitlines():
            parts = _nm_split(line)
            if len(parts) >= 2:
                d.setdefault(parts[0], parts[1])
        return d

    def _wifi_conn(self):
        for line in self._run(["-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show", "--active"]).splitlines():
            name, uuid, ctype, dev = (_nm_split(line) + ["", "", "", ""])[:4]
            if ctype == "802-11-wireless" and dev:
                return {"name": name, "uuid": uuid, "device": dev}
        return None

    def _laser_net(self):
        """The laser's Ethernet subnet (LBBDirect), so WiFi can't be put on it."""
        import ipaddress
        try:
            v = self._fields(["-t", "-f", "ipv4.addresses", "connection", "show", "LBBDirect"]).get("ipv4.addresses", "")
            return ipaddress.ip_interface(v.split(",")[0].strip()).network if v else None
        except (NetError, ValueError):
            return None

    # ------------------------------------------------------------ state
    def _pending(self):
        try:
            with open(self.pending_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def info(self):
        try:
            conn = self._wifi_conn()
        except NetError as e:
            return {"available": False, "error": str(e)}
        if not conn:
            return {"available": False, "error": "No active WiFi connection"}
        cfg = self._fields(["-t", "-f", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
                            "connection", "show", conn["uuid"]])
        live = self._fields(["-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "device", "show", conn["device"]])
        addr = live.get("IP4.ADDRESS[1]", "")
        p = self._pending()
        return {
            "available": True,
            "connection": conn["name"],
            "device": conn["device"],
            "method": "static" if cfg.get("ipv4.method") == "manual" else "dhcp",
            "address": addr.split("/")[0] if addr else "",
            "prefix": int(addr.split("/")[1]) if "/" in addr else 24,
            "gateway": live.get("IP4.GATEWAY", "") or cfg.get("ipv4.gateway", ""),
            "dns": live.get("IP4.DNS[1]", ""),
            "pending": {"deadline": p["deadline"], "remaining": max(0, int(p["deadline"] - time.time())),
                        "target": p.get("target")} if p else None,
            "last_error": self.last_error,
            "dry_run": self.dry,
        }

    # ------------------------------------------------------------ change
    def apply(self, req):
        import ipaddress
        with self.lock:
            if self._pending():
                raise NetError("A network change is waiting for confirmation - confirm or revert it first")
            conn = self._wifi_conn()
            if not conn:
                raise NetError("No active WiFi connection to configure")
            cur = self._fields(["-t", "-f", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
                                "connection", "show", conn["uuid"]])
            live = self._fields(["-t", "-f", "IP4.ADDRESS", "device", "show", conn["device"]])
            cur_ip = live.get("IP4.ADDRESS[1]", "").split("/")[0]

            mode = req.get("mode")
            if mode == "dhcp":
                new = {"ipv4.method": "auto", "ipv4.addresses": "", "ipv4.gateway": "", "ipv4.dns": ""}
                target = None
            elif mode == "static":
                try:
                    prefix = int(req.get("prefix", 24))
                    iface = ipaddress.ip_interface("%s/%d" % (str(req.get("address", "")).strip(), prefix))
                    gw = ipaddress.ip_address(str(req.get("gateway", "")).strip())
                    dns = [ipaddress.ip_address(x.strip()) for x in str(req.get("dns", "")).replace(";", ",").split(",") if x.strip()]
                except ValueError as e:
                    raise NetError("Invalid address: %s" % e)
                if iface.version != 4 or not (8 <= prefix <= 30):
                    raise NetError("Use an IPv4 address with a subnet prefix between /8 and /30")
                ip, net = iface.ip, iface.network
                if ip in (net.network_address, net.broadcast_address):
                    raise NetError("%s is the network/broadcast address of %s" % (ip, net))
                if gw not in net or gw == ip:
                    raise NetError("Gateway %s must be a different address inside %s" % (gw, net))
                laser = self._laser_net()
                if laser and ip in laser:
                    raise NetError("%s is on the laser's Ethernet subnet (%s) - pick an address on your WiFi network" % (ip, laser))
                if str(ip) != cur_ip and self._in_use(str(ip)):
                    raise NetError("%s is already used by another device on the network" % ip)
                new = {"ipv4.method": "manual", "ipv4.addresses": "%s/%d" % (ip, prefix),
                       "ipv4.gateway": str(gw), "ipv4.dns": ",".join(str(d) for d in (dns or [gw]))}
                target = str(ip)
            else:
                raise NetError("mode must be 'dhcp' or 'static'")

            previous = {k: cur.get(k, "") for k in ("ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns")}
            if previous["ipv4.method"] not in ("auto", "manual"):
                previous["ipv4.method"] = "auto"
            token = secrets.token_hex(6)
            pend = {"uuid": conn["uuid"], "device": conn["device"], "previous": previous, "new": new,
                    "target": target, "from_ip": cur_ip, "deadline": time.time() + NET_CONFIRM_SECONDS,
                    "token": token}
            self._write_pending(pend)
            # apply shortly after replying so the browser gets the answer first
            threading.Timer(1.5, self._apply_if_pending, [token]).start()
            self._arm(NET_CONFIRM_SECONDS)
            log("network change to", new, "- waiting for confirmation")
            return {"target": target, "confirm_seconds": NET_CONFIRM_SECONDS}

    def confirm(self):
        with self.lock:
            if not self._pending():
                raise NetError("No network change is waiting for confirmation")
            self._clear()
            log("network change confirmed")

    def revert(self, reason="reverted"):
        with self.lock:
            p = self._pending()
            if not p:
                raise NetError("No network change to revert")
            self._clear()
        log("reverting network change:", reason)
        if p.get("applied"):
            self._set(p["uuid"], p["device"], p["previous"])

    def _apply_if_pending(self, token):
        """Delayed apply - skipped if the change was reverted/replaced in the meantime."""
        with self.lock:
            p = self._pending()
            if not p or p.get("token") != token:
                log("network change was cancelled before it was applied")
                return
            p["applied"] = True
            self._write_pending(p)
        self._set(p["uuid"], p["device"], p["new"])

    def _set(self, uuid, device, settings):
        args = ["connection", "modify", uuid]
        for k in ("ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns"):
            args += [k, settings.get(k, "")]
        # going to DHCP: clear static values first or nmcli rejects 'auto' with addresses set
        try:
            if settings.get("ipv4.method") == "auto":
                self._run(["connection", "modify", uuid, "ipv4.addresses", "", "ipv4.gateway", ""], write=True)
            self._run(args, write=True)
            try:
                self._run(["device", "reapply", device], write=True, timeout=30)
            except NetError:
                self._run(["connection", "up", uuid], write=True, timeout=45)
            self.last_error = None
        except NetError as e:
            self.last_error = "Applying network settings failed: %s" % e
            log(self.last_error)

    def _in_use(self, ip):
        try:
            return subprocess.run(["ping", "-c", "2", "-W", "1", ip], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=6).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _arm(self, seconds):
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self):
        if self._pending():
            try:
                self.revert("not confirmed within %d s" % NET_CONFIRM_SECONDS)
                self.last_error = "The new network settings weren't confirmed in time, so the previous ones were restored."
            except NetError:
                pass

    def _write_pending(self, data):
        tmp = self.pending_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.pending_path)

    def _clear(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        try:
            os.unlink(self.pending_path)
        except FileNotFoundError:
            pass


# ======================================================================== camera


# ---------------------------------------------------------------- laser controller check
RELAY_LEVELS = {1: "none", 2: "ok", 3: "warn", 4: "error"}  # LightBurn's proc_status.STATUS


class Laser:
    """Read-only check of the link to the laser controller.

    The Bridge talks to the controller over eth0 (10.0.3.1/24); LightBurn's relay
    (lbrelay.service) forwards LightBurn's traffic to the controller's IP from
    /boot/bridge.json. Here we only look: cable link, whether the IP answers ping/ARP,
    and the relay's own status files. (Controller status reads are in Ruida below, from
    a separate network namespace so the relay is never disturbed.)
    """

    CACHE_SECONDS = 5.0

    def __init__(self, bridge_json="/boot/bridge.json", iface="eth0", proc_dir="/mnt/lbproc",
                 sys_net="/sys/class/net"):
        self.bridge_json, self.iface, self.proc_dir, self.sys_net = bridge_json, iface, proc_dir, sys_net
        self.lock = threading.Lock()
        self.cached, self.cached_at = None, 0.0
        self.last_seen = None  # time the controller last answered

    # ---- pieces (replaced in tests)
    def bridge_config(self):
        try:
            with open(self.bridge_json) as f:
                d = json.load(f)
            return {"laser_ip": str(d.get("laser_ip") or "10.0.3.3"),
                    "device_type": str(d.get("device_type") or "ruida"), "found": True}
        except (OSError, ValueError, AttributeError):
            return {"laser_ip": "10.0.3.3", "device_type": "ruida", "found": False}

    def _sys(self, name):
        try:
            with open(os.path.join(self.sys_net, self.iface, name)) as f:
                return f.read().strip()
        except OSError:
            return None

    def link(self):
        """(exists, cable_up, speed_mbps)."""
        if not os.path.isdir(os.path.join(self.sys_net, self.iface)):
            return False, False, None
        up = self._sys("carrier") == "1"
        speed = self._sys("speed") if up else None
        try:
            speed = int(speed) if speed and int(speed) > 0 else None
        except ValueError:
            speed = None
        return True, up, speed

    def ping(self, ip):
        try:
            r = subprocess.run(["ping", "-c", "1", "-W", "1", "-I", self.iface, ip],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def arp(self, ip):
        """True if the kernel has a complete ARP entry for ip (answers even with ping blocked)."""
        try:
            with open("/proc/net/arp") as f:
                for line in f.readlines()[1:]:
                    p = line.split()
                    if len(p) >= 6 and p[0] == ip and p[5] == self.iface and int(p[2], 16) & 0x2:
                        return True
        except (OSError, ValueError):
            pass
        return False

    def relay_running(self):
        try:
            r = subprocess.run(["systemctl", "is-active", "--quiet", "lbrelay"], timeout=5)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return None  # unknown (not a Bridge)

    def relay_status(self, name):
        """LightBurn's /mnt/lbproc/<name>.status: level on line 1, message after."""
        try:
            with open(os.path.join(self.proc_dir, name + ".status")) as f:
                lines = f.read().splitlines()
        except OSError:
            return None
        if not lines:
            return None
        try:
            level = RELAY_LEVELS.get(int(lines[0].strip()), "none")
            msg = "\n".join(lines[1:]).strip()
        except ValueError:
            level, msg = "none", "\n".join(lines).strip()
        return {"level": level, "message": msg[:300]}

    # ---- summary
    def check(self, fresh=False):
        with self.lock:
            if not fresh and self.cached and time.time() - self.cached_at < self.CACHE_SECONDS:
                return self.cached
            r = self._check()
            self.cached, self.cached_at = r, time.time()
            return r

    def _check(self):
        cfg = self.bridge_config()
        ip = cfg["laser_ip"]
        exists, cable, speed = self.link()
        answers = bool(cable and (self.ping(ip) or self.arp(ip)))
        if answers:
            self.last_seen = time.time()
        running = self.relay_running()
        relay = self.relay_status("relay")
        details = self.relay_status("relay_details")
        r = {"laser_ip": ip, "device_type": cfg["device_type"], "bridge_config": cfg["found"],
             "interface": self.iface, "interface_found": exists, "cable": cable, "speed": speed,
             "answers": answers, "last_seen": self.last_seen,
             "relay_running": running, "relay": relay, "relay_details": details}
        if not exists:
            r["state"], r["message"] = "unknown", "No %s interface on this device" % self.iface
        elif not cable:
            r["state"], r["message"] = "no_cable", \
                "No Ethernet link: check the cable from the Bridge to the laser and that the laser is on"
        elif not answers:
            r["state"], r["message"] = "no_answer", \
                ("Cable connected, but nothing answers at %s. Check the controller's IP is %s "
                 "(set on the laser's panel) or change it in the LightBurn Bridge settings" % (ip, ip))
        elif running is False:
            r["state"], r["message"] = "relay_down", \
                "Controller found at %s, but LightBurn's relay service isn't running" % ip
        elif any(x and x["level"] == "error" for x in (relay, details)):
            bad = relay if relay and relay["level"] == "error" else details
            r["state"], r["message"] = "relay_error", "Controller found; relay reports: " + bad["message"]
        else:
            r["state"], r["message"] = "ok", "Controller found at %s" % ip
        return r


# ---------------------------------------------------------------- Ruida controller status (read-only)
# Protocol per the open-source MeerK40t driver: UDP to controller:50200, replies always go to
# port 40200. Every byte is "swizzled" with a model-specific magic key; requests carry a 16-bit
# checksum of the swizzled bytes. Memory reads: DA 00 <addr:2x7bit> -> DA 01 <addr> <value:5x7bit>.
RUIDA_PORT, RUIDA_REPLY_PORT = 50200, 40200
RUIDA_MAGICS = (0x88, 0x11, 0x38)  # 644x/654x/320, 634x, RDL9635
RUIDA_MODELS = {0x65106510: "RDC6442S", 0x65006500: "RDC6442G"}
MEM = {  # address -> key (addresses are 14-bit: byte pair hi*128+lo)
    0x0200: "status", 0x0221: "x", 0x0231: "y", 0x0241: "z", 0x0251: "u",
    0x0201: "open_time", 0x0202: "work_time", 0x0203: "work_count", 0x0205: "doc_count",
    0x0208: "prev_work_time", 0x0211: "laser_time", 0x0320: "file_total", 0x0321: "file_progress",
    0x02FE: "card_id",
}
MEM_FAST = (0x0200, 0x0221, 0x0231, 0x0241, 0x0251, 0x0320, 0x0321, 0x0203, 0x0208, 0x0211)
MEM_SLOW = (0x0201, 0x0202, 0x0205, 0x02FE)
# Status bits as seen on an RDC6442G: bit 0 = busy (job or manual move); during a job bit 9
# (0x200) is clear and bit 1 means paused. Jogging sets bits 0 and 9 (and 1), and the job file
# total reads FILE_TOTAL_UNKNOWN. (MeerK40t calls bit 1 "part end"; on hardware it is paused.)
STATUS_BUSY, STATUS_PAUSED, STATUS_MANUAL = 0x01, 0x02, 0x200
FILE_TOTAL_UNKNOWN = 1000000000  # reported while framing / no job loaded
MOVING_HOLD = 3.0  # s: show "moving" this long after the last position change
# A job that has been running (not paused) this long without the laser-on counter moving is
# probably blocked by an alarm. Water protection, for example, isn't readable from the controller.
NOT_FIRING_AFTER = 20.0
# Control commands (MeerK40t's Ruida driver): sent on 50200 like everything else, answered with ACK.
# name -> (bytes, states it is allowed in). Verified on an RDC6442G.
# Deliberately NOT here: D8 2A ("Home XY" in MeerK40t) reset/rebooted an RDC6442G, and the
# panel Frame key A5 53 00 is acknowledged but ignored on port 50200.
RUIDA_COMMANDS = {
    "pause":  (b"\xD8\x02", ("running",)),
    "resume": (b"\xD8\x03", ("paused",)),
    "stop":   (b"\xD8\x01", ("running", "paused")),
}
RUIDA_ACK, RUIDA_NAK = 0xCC, 0xCF


def _swz(b, m):
    b ^= (b >> 7) & 0xFF
    b ^= (b << 7) & 0xFF
    b ^= (b >> 7) & 0xFF
    b ^= m
    return (b + 1) & 0xFF


def _unswz(b, m):
    b = (b - 1) & 0xFF
    b ^= m
    b ^= (b >> 7) & 0xFF
    b ^= (b << 7) & 0xFF
    b ^= (b >> 7) & 0xFF
    return b


_SWZ = {m: bytes(_swz(b, m) for b in range(256)) for m in RUIDA_MAGICS}
_UNSWZ = {m: bytes(_unswz(b, m) for b in range(256)) for m in RUIDA_MAGICS}


def ruida_swizzle(data, magic):
    return bytes(data).translate(_SWZ[magic])


def ruida_unswizzle(data, magic):
    return bytes(data).translate(_UNSWZ[magic])


def ruida_packet(payload, magic):
    sw = ruida_swizzle(payload, magic)
    return struct.pack(">H", sum(sw) & 0xFFFF) + sw


def ruida_read_request(addr):
    return bytes((0xDA, 0x00, (addr >> 7) & 0x7F, addr & 0x7F))


def decode35(b, signed=True):
    v = 0
    for x in b[:5]:
        v = (v << 7) | (x & 0x7F)
    if signed and v & (1 << 34):
        v -= 1 << 35
    return v


def encode35(v):
    v &= (1 << 35) - 1
    return bytes((v >> s) & 0x7F for s in (28, 21, 14, 7, 0))


def ruida_parse_reply(data, magic):
    """(addr, value) from a memory-read reply, or None."""
    d = ruida_unswizzle(data, magic)
    if len(d) >= 9 and d[0] == 0xDA and d[1] == 0x01:
        return ((d[2] & 0x7F) << 7) | (d[3] & 0x7F), decode35(d[4:9])
    return None


class RuidaUDP:
    """Request/reply over UDP. bind=(host, 40200) inside our own network namespace."""

    def __init__(self, laser_ip, bind=("", RUIDA_REPLY_PORT), port=RUIDA_PORT, timeout=0.3):
        self.addr = (laser_ip, port)
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(bind)

    def close(self):
        self.sock.close()

    def read(self, addr, magic, timeout=None, tries=2):
        timeout = timeout or self.timeout
        req = ruida_read_request(addr)
        try:
            while True:  # drop anything stale
                self.sock.setblocking(False)
                self.sock.recv(2048)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            pass
        self.sock.settimeout(timeout)
        for _ in range(tries):
            self.sock.sendto(ruida_packet(req, magic), self.addr)
            end = time.time() + timeout
            while time.time() < end:
                try:
                    self.sock.settimeout(max(0.01, end - time.time()))
                    data, src = self.sock.recvfrom(2048)
                except socket.timeout:
                    break
                if src[0] != self.addr[0]:
                    continue
                r = ruida_parse_reply(data, magic)
                if r and r[0] == addr:
                    return r[1]
        return None


def _ruida_send(self, payload, magic):
    """Send one command and wait for its 1-byte answer: "ack", "nak" or None (no answer).
    Never retried here: a command that may have run must not run twice."""
    try:
        while True:  # drop stale replies
            self.sock.setblocking(False)
            self.sock.recv(2048)
    except (BlockingIOError, InterruptedError, OSError):
        pass
    self.sock.sendto(ruida_packet(payload, magic), self.addr)
    end = time.time() + 1.0
    while time.time() < end:
        try:
            self.sock.settimeout(max(0.01, end - time.time()))
            data, src = self.sock.recvfrom(2048)
        except socket.timeout:
            break
        if src[0] != self.addr[0] or len(data) != 1:
            continue
        b = ruida_unswizzle(data, magic)[0]
        if b == RUIDA_ACK:
            return "ack"
        if b == RUIDA_NAK:
            return "nak"
    return None


RuidaUDP.send = _ruida_send


class NetNS:
    """Our own network namespace with a macvlan on eth0, so we can use port 40200
    without touching the one LightBurn's relay listens on."""

    CLONE_NEWNET = 0x40000000

    def __init__(self, name="lbcam", parent="eth0", link="lbcamlaser"):
        self.name, self.parent, self.link = name, parent, link
        self.ip = None

    def _ip(self, *args, check=True):
        r = subprocess.run(["ip"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=10)
        if check and r.returncode != 0:
            raise OSError((r.stderr or r.stdout).strip() or "ip %s failed" % " ".join(args))
        return r.stdout

    def setup(self, laser_ip):
        if os.geteuid() != 0:
            raise OSError("needs root")
        prefix = ".".join(laser_ip.split(".")[:3])
        want = os.environ.get("LBCAM_RUIDA_IP") or prefix + ".254"
        if self.name not in self._ip("netns", "list", check=False).split():
            self._ip("netns", "add", self.name)
        ns = ["netns", "exec", self.name, "ip"]
        have = subprocess.run(["ip"] + ns + ["-o", "-4", "addr", "show", "dev", self.link],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout
        if want + "/" not in have:
            subprocess.run(["ip"] + ns + ["link", "del", self.link], stderr=subprocess.DEVNULL)
            # created straight into the namespace so NetworkManager never sees it
            self._ip("link", "add", self.link, "link", self.parent, "netns", self.name, "type", "macvlan", "mode", "bridge")
            self._ip("netns", "exec", self.name, "ip", "addr", "add", want + "/24", "dev", self.link)
            self._ip("netns", "exec", self.name, "ip", "link", "set", self.link, "up")
            self._ip("netns", "exec", self.name, "ip", "link", "set", "lo", "up")
        self.ip = want

    home_fd = None

    def _setns(self, fd):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.setns(fd, self.CLONE_NEWNET) != 0:
            raise OSError(ctypes.get_errno(), "setns failed")

    def enter(self):
        """Move the calling thread (only) into the namespace."""
        if self.home_fd is None:
            self.home_fd = os.open("/proc/thread-self/ns/net", os.O_RDONLY)
        fd = os.open("/var/run/netns/" + self.name, os.O_RDONLY)
        try:
            self._setns(fd)
        finally:
            os.close(fd)

    def leave(self):
        """Back to the normal namespace (needed to see eth0 when re-creating the link)."""
        if self.home_fd is not None:
            self._setns(self.home_fd)


class RuidaCommandError(Exception):
    pass


class Ruida:
    """Polls the controller's status in the background (read-only memory reads)."""

    def __init__(self, laser, enabled=None, transport_factory=None):
        self.laser = laser
        self.enabled = (os.environ.get("LBCAM_RUIDA", "1") != "0") if enabled is None else enabled
        self.transport_factory = transport_factory  # tests: (laser_ip) -> transport
        self.lock = threading.Lock()
        self.wanted_until = 0.0
        self.wake = threading.Event()
        self.state = {"available": False, "reason": "starting" if self.enabled else "turned off (LBCAM_RUIDA=0)"}
        self.values, self.raw = {}, {}
        self.magic = None
        self.job = None       # the run in progress: {"start", "count0", "progress"}
        self.last_job = None  # {"seconds", "ended", "result": finished|stopped, "progress"}
        self.recheck_until = 0.0
        self.first_u, self.u_moved = None, False
        self.last_pos, self.moved_at = None, 0.0
        self.cmd = None  # {"name", "payload", "who", "done": Event, "result"}
        self.scan_job = None  # {"start", "count", "done": Event, "result"}
        self.cmd_lock = threading.Lock()
        self.updated = None
        self.ns = None

    def want(self):
        """The dashboard is looking: poll fast for a while."""
        self.wanted_until = time.time() + 30
        self.wake.set()

    def start(self):
        if self.enabled:
            threading.Thread(target=self.run, daemon=True).start()

    # ---- polling
    def _transport(self, laser_ip):
        if self.transport_factory:
            return self.transport_factory(laser_ip)
        direct = os.environ.get("LBCAM_RUIDA_DIRECT")  # development: host:port of an emulator
        if direct:
            host, _, port = direct.rpartition(":")
            return RuidaUDP(host, bind=("", 0), port=int(port))
        if self.ns is None:
            self.ns = NetNS(parent=self.laser.iface)
        self.ns.leave()
        self.ns.setup(laser_ip)
        self.ns.enter()  # this thread lives in the namespace from now on
        return RuidaUDP(laser_ip)

    def run(self):
        transport, laser_ip, fails, slow_at = None, None, 0, 0.0
        while True:
            ip = self.laser.bridge_config()["laser_ip"]
            try:
                if transport is None or ip != laser_ip:
                    if transport:
                        transport.close()
                    transport, laser_ip, self.magic = self._transport(ip), ip, None
                link = self.laser.link()[1]
                if link and self.cmd:
                    self._do_command(transport)
                if link and self.scan_job:
                    self._do_scan(transport)
                if not link:
                    self._set(False, "no Ethernet link")
                elif self.poll_once(transport, slow=time.time() >= slow_at):
                    fails = 0
                    if time.time() >= slow_at:
                        slow_at = time.time() + 60
                else:
                    fails += 1
                    self._set(False, "controller doesn't answer status reads" if self.magic is None
                              else "no reply from controller")
                    if fails >= 3:
                        self.magic = None
            except Exception as e:  # setup problems: report and retry later
                transport = None
                self._set(False, "can't open a reader: %s" % e)
                self.wake.wait(60)
                self.wake.clear()
                continue
            fast = time.time() < self.wanted_until or self._running()
            self.wake.wait(2 if fast else 20)
            self.wake.clear()

    # ---- control commands
    def command(self, name, who="?", timeout=5.0):
        """Run a control command from the dashboard. Raises RuidaCommandError with a reason."""
        if name not in RUIDA_COMMANDS:
            raise RuidaCommandError("unknown command")
        payload, allowed = RUIDA_COMMANDS[name]
        with self.lock:
            available, state = self.state.get("available"), self.values.get("state")
        if not self.enabled or not available or self.magic is None:
            raise RuidaCommandError("the controller isn't connected")
        if state not in allowed:
            raise RuidaCommandError("can't %s while the laser is %s" % (name, state or "unknown"))
        job = {"name": name, "payload": payload, "who": who, "done": threading.Event(), "result": None}
        with self.cmd_lock:
            if self.cmd:
                raise RuidaCommandError("another command is still being sent")
            self.cmd = job
        self.wake.set()
        if not job["done"].wait(timeout):
            with self.cmd_lock:
                if self.cmd is job:
                    self.cmd = None
            raise RuidaCommandError("the Bridge couldn't send it in time")
        if job["result"] != "ack":
            raise RuidaCommandError("the laser didn't confirm it (%s) - check the machine"
                                    % (job["result"] or "no answer"))
        return True

    # ---- diagnostics: read a range of controller memory (read-only, same reads as status)
    SCAN_MAX = 2048

    def scan(self, start, count, who="?"):
        """{address: value} for every address in range that answers. Only while idle."""
        count = max(1, min(int(count), self.SCAN_MAX))
        start = max(0, min(int(start), 0x3FFF))
        count = min(count, 0x4000 - start)
        with self.lock:
            available, state = self.state.get("available"), self.values.get("state")
        if not self.enabled or not available or self.magic is None:
            raise RuidaCommandError("the controller isn't connected")
        if state not in ("idle", None):
            raise RuidaCommandError("only while the laser is idle (it is %s)" % state)
        job = {"start": start, "count": count, "who": who, "done": threading.Event(), "result": None}
        with self.cmd_lock:
            if self.scan_job or self.cmd:
                raise RuidaCommandError("busy, try again in a moment")
            self.scan_job = job
        self.wake.set()
        if not job["done"].wait(10 + count * 0.12):
            raise RuidaCommandError("scan timed out")
        return job["result"]

    def _do_scan(self, transport):
        job = self.scan_job
        t0, out, missing = time.time(), {}, 0
        for addr in range(job["start"], job["start"] + job["count"]):
            v = transport.read(addr, self.magic, timeout=0.06, tries=1)
            if v is None:
                missing += 1
            else:
                out["0x%04X" % addr] = v
        job["result"] = {"start": "0x%04X" % job["start"], "count": job["count"], "answered": len(out),
                         "missing": missing, "seconds": round(time.time() - t0, 1), "values": out}
        log("controller memory scan 0x%04X+%d from %s: %d answered in %.1fs"
            % (job["start"], job["count"], job["who"], len(out), time.time() - t0))
        with self.cmd_lock:
            self.scan_job = None
        job["done"].set()

    def _do_command(self, transport):
        with self.cmd_lock:
            job = self.cmd
        if not job:
            return
        try:
            res = transport.send(job["payload"], self.magic)
            if res == "nak":  # checksum rejected: it did not run, safe to send once more
                res = transport.send(job["payload"], self.magic)
        except OSError as e:
            res = "error: %s" % e
        job["result"] = res
        log("laser command: %s from %s -> %s" % (job["name"], job["who"], res or "no answer"))
        with self.cmd_lock:
            self.cmd = None
        job["done"].set()
        self.want()  # poll fast so the dashboard shows the result

    def _detect_magic(self, t):
        for m in ((self.magic,) if self.magic else RUIDA_MAGICS):
            v = t.read(0x02FE, m)
            if v is not None:
                return m, v
        return None, None

    def poll_once(self, t, slow=True):
        if self.magic is None:
            m, card = self._detect_magic(t)
            if m is None:
                return False
            self.magic = m
            self.raw["card_id"] = card
            slow = True
        got = {}
        for addr in MEM_FAST + (MEM_SLOW if slow else ()):
            v = t.read(addr, self.magic)
            if v is None and addr == 0x0200:
                return False
            if v is not None:
                got[MEM[addr]] = v
        with self.lock:
            self.raw.update(got)
            self._interpret()
            self.updated = time.time()
            self.state = {"available": True, "reason": ""}
        return True

    def _set(self, ok, reason):
        with self.lock:
            self.state = {"available": ok, "reason": reason}

    def _running(self):
        return bool(self.values.get("running"))

    @property
    def job_started(self):
        return self.job["start"] if self.job else None

    def _interpret(self):
        r, v, now = self.raw, {}, time.time()
        tot, prog = r.get("file_total"), r.get("file_progress")
        progress = (round(100.0 * prog / tot, 1)
                    if tot and 0 < tot < FILE_TOTAL_UNKNOWN and prog is not None and 0 <= prog <= tot else None)
        count = r.get("work_count")
        lt = r.get("laser_time")
        st = r.get("status")
        if st is not None:
            busy = bool(st & STATUS_BUSY)
            has_file = bool(tot) and 0 < tot < FILE_TOTAL_UNKNOWN
            running = busy and not (st & STATUS_MANUAL) and has_file
            paused = running and bool(st & STATUS_PAUSED)
            # jogging from the panel doesn't touch the status bits at all, so also watch the position
            pos = tuple(r.get(k) for k in ("x", "y", "z", "u"))
            if self.last_pos and any(a is not None and b is not None and abs(a - b) > 50  # µm
                                     for a, b in zip(pos, self.last_pos)):
                self.moved_at = now
            self.last_pos = pos
            moving = not running and (busy or now - self.moved_at < MOVING_HOLD)
            v["running"], v["paused"], v["moving"] = running, paused, moving
            v["state"] = "paused" if paused else "running" if running else "moving" if moving else "idle"
            if running:
                if not self.job:
                    self.job = {"start": now, "count0": count, "progress": None,
                                "laser0": lt, "run_s": 0.0, "tick": now, "fired": False}
                job = self.job
                if progress is not None:
                    job["progress"] = progress
                if not paused:
                    job["run_s"] += now - job["tick"]
                job["tick"] = now
                if job["laser0"] is None:
                    job["laser0"] = lt
                elif lt is not None and lt > job["laser0"]:
                    job["fired"] = True
            elif self.job:
                self._job_ended(self.job, now, count)
                self.job = None
        # the job counter can tick up a moment after the status goes idle
        lj = self.last_job
        if lj and lj["result"] == "stopped" and now < self.recheck_until and self._counted(lj.get("count0"), count):
            lj["result"], lj["progress"] = "finished", 100.0
        v["elapsed"] = round(now - self.job["start"]) if self.job else None
        j = self.job
        v["not_firing"] = bool(j and not j["fired"] and j["laser0"] is not None
                               and j["run_s"] >= NOT_FIRING_AFTER)
        if v["not_firing"] and not j.get("warned"):
            j["warned"] = True
            log("laser job has run %ds without firing - alarm on the panel?" % j["run_s"])
        v["progress"] = progress if self.job else None
        v["position"] = {k: round(r[k] / 1000.0, 2) for k in ("x", "y", "z", "u") if k in r}
        # U often reads a fixed value on machines without a rotary: show it once it has moved
        if "u" in r:
            self.first_u = r["u"] if self.first_u is None else self.first_u
            self.u_moved = self.u_moved or r["u"] != self.first_u
        v["axes"] = ["x", "y", "z"] + (["u"] if self.u_moved else [])
        v["counters"] = {k: r[k] for k in ("open_time", "work_time", "laser_time", "work_count",
                                             "doc_count", "prev_work_time") if k in r}
        cid = r.get("card_id")
        if cid is not None:
            v["model"] = RUIDA_MODELS.get(cid & 0xFFFFFFFF, "Ruida (card %08X)" % (cid & 0xFFFFFFFF))
        self.values = v

    @staticmethod
    def _counted(before, after):
        return before is not None and after is not None and after > before

    def _job_ended(self, job, now, count):
        last = job["progress"]
        if self._counted(job["count0"], count) or (last is not None and last >= 98):
            result = "finished"
        elif last is None:
            return  # no job file progress at all: framing or a move, not a job
        else:
            result = "stopped"
        self.last_job = {"seconds": round(now - job["start"]), "ended": now, "result": result,
                         "progress": 100.0 if result == "finished" else last, "count0": job["count0"]}
        self.recheck_until = now + 15
        log("laser job %s after %ds%s" % (result, self.last_job["seconds"],
                                          "" if result == "finished" else " at %.0f%%" % last))

    def status(self):
        with self.lock:
            out = dict(self.state)
            out.update(self.values)
            out["last_job"] = self.last_job
            out["updated"] = self.updated
            out["magic"] = "0x%02X" % self.magic if self.magic else None
            out["raw"] = dict(self.raw)
            return out


def pi_health():
    """CPU temperature and Raspberry Pi power/throttling flags."""
    h = {}
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            h["temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=3).stdout
        bits = int(out.strip().split("=")[1], 16)
        h["throttled"] = bits
        names = {0: "under-voltage now", 1: "CPU speed capped now", 2: "throttled now", 3: "hot now",
                 16: "under-voltage since boot", 17: "speed capped since boot", 18: "throttled since boot",
                 19: "hot since boot"}
        h["power_issues"] = [n for b, n in names.items() if bits & (1 << b)]
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    try:
        with open("/proc/loadavg") as f:
            h["load"] = float(f.read().split()[0])
        with open("/proc/uptime") as f:
            h["uptime"] = int(float(f.read().split()[0]))
    except (OSError, ValueError):
        pass
    return h


class Camera:
    """Owns the capture device. Publishes live frames, or the placeholder when no camera is usable."""

    def __init__(self, cfg, placeholder):
        self.cfg = cfg
        self.placeholder = placeholder
        self.state = "starting"  # starting | searching | streaming | idle | error
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
        # pause the camera when nobody watches (0 disables)
        self.idle_after = float(os.environ.get("CAM_IDLE_SECONDS", "30"))
        self.last_use = time.time()
        self.wake = threading.Event()

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

    def touch(self):
        """Someone wants frames: keep the camera running / wake it up."""
        self.last_use = time.time()
        self.wake.set()

    def is_idle(self):
        return (self.idle_after > 0 and self.clients == 0
                and time.time() - self.last_use > self.idle_after)

    def latest_frame(self, timeout=5.0):
        """Current frame, waiting for a fresh one if the camera is just waking up."""
        self.touch()
        fid, frame = self.wait_frame(-1, 0)
        deadline = time.time() + timeout
        while frame is None and time.time() < deadline:
            fid, frame = self.wait_frame(fid, max(0.05, deadline - time.time()))
        return frame

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
            went_idle = False
            try:
                went_idle = self._loop(fd, maps)
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
            if went_idle:
                self._sleep_until_needed()
                continue
            if not self.reopen.is_set():
                self._idle(1)

    def _sleep_until_needed(self):
        """Camera closed because nobody is watching; reopen when a viewer/snapshot arrives."""
        self._set("idle", "Paused - nobody is watching (wakes up on the next viewer)")
        with self.cond:
            self.frame, self.live = None, False  # don't hand out a stale frame later
        while self.running and not self.reopen.is_set() and self.is_idle():
            self.wake.wait(1.0)
            self.wake.clear()
        self._set("starting", "Waking up")

    def _loop(self, fd, maps):
        """Capture until stopped. Returns True if it stopped because nobody is watching."""
        count, t0 = 0, time.time()
        bad = 0
        while self.running and not self.reopen.is_set():
            if self.is_idle():
                return True
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
        return False

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
        self.touch()


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


def host_allowed(host_header, extra=None):
    """
    Accept only requests addressed to this device: an IP, localhost, a .local name, or this
    host's name. Blocks DNS-rebinding (evil.example resolving to the Bridge's IP).
    """
    import ipaddress
    if not host_header:
        return True  # HTTP/1.0 clients without Host
    h = host_header.strip().lower()
    if h.startswith("["):  # [IPv6]:port
        h = h[1:h.find("]")] if "]" in h else h
    elif h.count(":") == 1:
        h = h.split(":")[0]
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    me = socket.gethostname().lower()
    allowed = {"localhost", me, me + ".local"}
    allowed.update(x.strip().lower() for x in (extra or os.environ.get("CAM_ALLOWED_HOSTS", "")).split(",") if x.strip())
    return h in allowed or h.endswith(".local")


def make_handler(cam, cfg, dashboard, updater, net=None, laser=None, ruida=None):
    health_cache = {"t": 0.0, "v": {}}

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
            self.send_header("X-Content-Type-Options", "nosniff")
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

        def _guard(self, changing):
            """None if the request may proceed, else an error message (sent as 403)."""
            if not host_allowed(self.headers.get("Host")):
                return "Host not allowed"
            if changing:
                # Custom header => browsers need a CORS preflight for cross-site requests,
                # which this server never approves. Blocks CSRF from other websites.
                if self.headers.get("X-LBCam") != "1":
                    return "Missing X-LBCam header (requests must come from the dashboard)"
                origin = self.headers.get("Origin")
                if origin and origin != "null":
                    if urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
                        return "Cross-site request blocked"
            return None

        # ---------------------------------------------------- routes
        def do_OPTIONS(self):
            self._send(403, "text/plain", b"cross-origin requests are not allowed")

        def do_HEAD(self):
            self._route(head=True)

        def do_GET(self):
            self._route(head=False)

        def _route(self, head):
            err = self._guard(False)
            if err:
                return self._send(403, "text/plain", err.encode(), head)
            u = urlparse(self.path)
            p = u.path.rstrip("/") or "/"
            if p in ("/", "/index.html", "/monitor"):  # one page, two views
                return self._send(200, "text/html; charset=utf-8", dashboard, head)
            if p in ("/stream", "/stream.mjpg", "/video", "/mjpeg"):
                if head:
                    return self._send(200, "multipart/x-mixed-replace; boundary=" + BOUNDARY, b"", True)
                return self._stream()
            if p in ("/snapshot", "/snapshot.jpg", "/image.jpg", "/jpg"):
                if head:  # URL checks (LightBurn) shouldn't wake the camera or ever fail
                    return self._send(200, "image/jpeg", b"", True)
                frame = cam.latest_frame(5)
                if frame is None:
                    return self._send(503, "text/plain", b"camera starting", head)
                return self._send(200, "image/jpeg", frame, head)
            if p == "/status":
                return self._json(self._status())
            if p == "/api/state":
                return self._json(self._state())
            if p == "/api/cameras":
                return self._json(cameras_cached())
            if p == "/api/modes":
                dev = (parse_qs(u.query).get("device") or [None])[0]
                return self._json(cam.modes(dev if dev and dev != "auto" else None))
            if p == "/api/controls":
                return self._json(cam.controls())
            if p == "/api/update":
                return self._json(updater.info())
            if p == "/api/network":
                return self._json(net.info() if net else {"available": False, "error": "disabled"})
            if p == "/api/laser":
                if not laser:
                    return self._json({"state": "unknown", "message": "Laser check not available"})
                out = dict(laser.check(fresh="fresh" in parse_qs(u.query)))
                if ruida:
                    ruida.want()
                    out["controller"] = ruida.status()
                return self._json(out)
            if p == "/api/laser/scan":
                if not ruida:
                    return self._json({"error": "not available"}, 409)
                q = parse_qs(u.query)
                try:
                    start = int((q.get("start") or ["0x200"])[0], 0)
                    count = int((q.get("count") or ["256"])[0], 0)
                    return self._json(ruida.scan(start, count, who=self.client_address[0]))
                except ValueError:
                    return self._json({"error": "start/count must be numbers"}, 400)
                except RuidaCommandError as e:
                    return self._json({"error": str(e)}, 409)
            if p == "/api/health":
                if time.time() - health_cache["t"] > 5:
                    health_cache["v"], health_cache["t"] = pi_health(), time.time()
                return self._json(health_cache["v"])
            if p == "/api/logs":
                try:
                    n = int((parse_qs(u.query).get("n") or ["300"])[0])
                except ValueError:
                    n = 300
                return self._json(recent_log(n))
            self._send(404, "text/plain", b"not found", head)

        def do_POST(self):
            err = self._guard(True)
            if err:
                return self._json({"error": err}, 403)
            p = urlparse(self.path).path.rstrip("/")
            if p == "/api/update":
                return self._upload()
            body = self._body()
            if body is None:
                return self._json({"error": "invalid JSON"}, 400)
            try:
                if p == "/api/laser/command":
                    if not ruida:
                        return self._json({"error": "laser control isn't available"}, 409)
                    name = str(body.get("cmd") or "")
                    try:
                        ruida.command(name, who=self.client_address[0])
                    except RuidaCommandError as e:
                        return self._json({"error": "Couldn't %s: %s" % (name, e)}, 409)
                    return self._json({"ok": True, "cmd": name})
                if p == "/api/network":
                    return self._json(dict(ok=True, **net.apply(body)))
                if p == "/api/network/confirm":
                    net.confirm()
                    return self._json({"ok": True})
                if p == "/api/network/revert":
                    net.revert("reverted from dashboard")
                    return self._json({"ok": True})
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
            except (UpdateError, NetError) as e:
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
                v = updater.install(blob, self.headers.get("X-Password"), self.headers.get("X-Sha256"))
            except PermissionError as e:
                return self._json({"error": str(e)}, 403)
            except UpdateError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"error": "could not install: %s" % e}, 500)
            digest = hashlib.sha256(blob).hexdigest()
            log("installed update", v, "sha256", digest)
            self._json({"ok": True, "version": v, "sha256": digest, "restarting": True})

        # ---------------------------------------------------- payloads
        def _status(self):
            return {"version": VERSION, "state": cam.state, "message": cam.message, "live": cam.live, "camera": cam.info,
                    "fps": cam.fps_measured, "clients": cam.clients}

        def _state(self):
            s = self._status()
            s.update({
                "version": VERSION,
                "config": cfg.public(),
                "cameras": cameras_cached(),
                "uptime": int(time.time() - cam.started),
                "ips": local_ips(),
                "hostname": socket.gethostname(),
                "placeholder": bool(cam.placeholder),
            })
            return s

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + BOUNDARY)
            self.send_header("Cache-Control", "no-cache, no-store, private")
            self.send_header("Pragma", "no-cache")
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
    net = Network()
    laser = Laser(iface=os.environ.get("LBCAM_LASER_IFACE", "eth0"))
    ruida = Ruida(laser)
    ruida.start()
    threading.Thread(target=cam.run, daemon=True).start()

    srv = None
    for attempt in range(30):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(cam, cfg, dashboard, updater, net, laser, ruida))
            break
        except OSError as e:
            log("port %d unavailable (%s), retrying..." % (port, e))
            time.sleep(2)
    if srv is None:
        log("giving up: port %d stays busy" % port)
        return 1
    srv.daemon_threads = True

    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)  # systemd stop -> write batched settings first

    log("lbcam %s on port %d, settings: %s" % (VERSION, port, cfg_path))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    cam.running = False
    cfg.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
