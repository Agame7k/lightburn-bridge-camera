"""Shared test helpers. Standard library only, so tests run on the Bridge's Python 3.7 too."""
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_FILES = ("lbcam_stream.py", "dashboard.html", "placeholder.jpg")


def load(name, env=None):
    """Import a top-level script (lbcam_stream / launcher) as a fresh module."""
    old = {}
    for k, v in (env or {}).items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location(name + "_under_test", os.path.join(ROOT, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def app_sources(version=None, break_python=False):
    """The app files as {name: bytes}, optionally with a different VERSION / a syntax error."""
    files = {}
    for n in APP_FILES:
        with open(os.path.join(ROOT, n), "rb") as f:
            files[n] = f.read()
    if version:
        src = files["lbcam_stream.py"].decode()
        src = re.sub(r'^VERSION = "[^"]+"', 'VERSION = "%s"' % version, src, count=1, flags=re.M)
        files["lbcam_stream.py"] = src.encode()
    if break_python:
        files["lbcam_stream.py"] += b"\ndef broken(:\n"
    return files


def package(files, version, manifest_files=None, extra=None):
    """Build an update .tar.gz in memory, like make_update.py."""
    manifest = {"name": "lbcam", "version": version,
                "files": manifest_files or {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}
    items = dict(files)
    items["manifest.json"] = json.dumps(manifest).encode()
    items.update(extra or {})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for n, b in items.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(b)
            tf.addfile(ti, io.BytesIO(b))
    return buf.getvalue()


def make_home(version):
    """A temp LBCAM_HOME laid out like the Bridge: launcher.py, releases/<v>, current link."""
    home = tempfile.mkdtemp(prefix="lbcam-test-")
    rel = os.path.join(home, "releases", version)
    os.makedirs(rel)
    for n, b in app_sources(version).items():
        with open(os.path.join(rel, n), "wb") as f:
            f.write(b)
    shutil.copy(os.path.join(ROOT, "launcher.py"), home)
    os.symlink("releases/" + version, os.path.join(home, "current"))
    return home
