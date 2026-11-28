#!/usr/bin/env python3
"""
lbcam launcher - the small, stable part that never gets replaced by an upload.

Runs <LBCAM_HOME>/current/lbcam_stream.py and keeps it running. After an update
(<LBCAM_HOME>/pending.json exists) it waits for the new version to answer /status with
the expected version. If it doesn't within HEALTH_TIMEOUT, it kills it and switches
back to the previous release automatically.

Layout (LBCAM_HOME, default /opt/lbcam):
  launcher.py
  current  -> releases/<version>
  previous -> releases/<version>
  releases/<version>/{lbcam_stream.py, dashboard.html, placeholder.jpg, manifest.json}
  pending.json       written by the app right before it restarts into a new release
  last_update.json   result of the last update, shown in the dashboard
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

HOME = os.environ.get("LBCAM_HOME", "/opt/lbcam")
PORT = int(os.environ.get("CAM_PORT", "8080"))
HEALTH_TIMEOUT = 45


def log(*a):
    print(time.strftime("%H:%M:%S"), "[launcher]", *a, flush=True)


def p(name):
    return os.path.join(HOME, name)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def fsync_dir(path):
    """Make a rename inside `path` reach the disk: the Bridge loses power whenever the laser is switched off."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(os.path.dirname(path) or ".")


def set_link(name, target):
    """Atomically point HOME/name at target (relative path inside HOME)."""
    tmp = p(name + ".tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    os.symlink(target, tmp)
    os.replace(tmp, p(name))
    fsync_dir(HOME)


def link_target(name):
    try:
        return os.readlink(p(name))
    except OSError:
        return None


def healthy(child, want_version):
    """True once the child answers /status with the expected version."""
    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        if child.poll() is not None:
            return False, "new version exited with code %s" % child.returncode
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/status" % PORT, timeout=2) as r:
                st = json.loads(r.read().decode())
            if st.get("version") == want_version:
                return True, ""
            return False, "running version is %r, expected %r" % (st.get("version"), want_version)
        except Exception:
            pass
        time.sleep(1)
    return False, "new version did not respond within %d s" % HEALTH_TIMEOUT


def rollback(bad_version, reason, restore_previous=None):
    prev = link_target("previous")
    log("ROLLBACK:", reason)
    result = {"result": "rolled_back", "version": bad_version, "reason": reason, "time": int(time.time())}
    if prev and os.path.isfile(os.path.join(HOME, prev, "lbcam_stream.py")):
        set_link("current", prev)
        # never leave "previous" pointing at the broken version
        if restore_previous and os.path.isfile(os.path.join(HOME, restore_previous, "lbcam_stream.py")) \
                and restore_previous != prev:
            set_link("previous", restore_previous)
        else:
            try:
                os.unlink(p("previous"))
            except FileNotFoundError:
                pass
        result["restored"] = os.path.basename(prev)
    else:
        result["reason"] += " (no previous version to restore)"
    try:
        os.unlink(p("pending.json"))
    except FileNotFoundError:
        pass
    write_json(p("last_update.json"), result)


def rollback_was_interrupted(pending):
    """True (and tidied up) if a power cut hit a rollback after "current" was already restored.

    pending.json is only written once "current" points at the new release, so if it no
    longer does, the rollback got that far. Checking the restored version against the
    pending one would fail and roll back a second, healthy version.
    """
    want = str(pending.get("version"))
    cur = os.path.realpath(p("current"))
    if os.path.basename(cur) == want or not os.path.isfile(os.path.join(cur, "lbcam_stream.py")):
        return False
    log("finishing a rollback of", want, "that a power cut interrupted")
    try:
        os.unlink(p("pending.json"))
    except FileNotFoundError:
        pass
    last = read_json(p("last_update.json")) or {}
    if last.get("version") != want or last.get("result") != "rolled_back":
        write_json(p("last_update.json"), {
            "result": "rolled_back", "version": want, "time": int(time.time()),
            "reason": "rolled back (the Bridge lost power while it was rolling back)",
            "restored": os.path.basename(cur)})
    else:
        fsync_dir(HOME)
    return True


def main():
    log("starting, home", HOME)
    while True:
        cur = os.path.realpath(p("current"))
        script = os.path.join(cur, "lbcam_stream.py")
        pending = read_json(p("pending.json"))
        if pending and rollback_was_interrupted(pending):
            continue
        if not os.path.isfile(script):
            if pending:
                rollback(pending.get("version"), "release files missing", pending.get("restore_previous"))
                continue
            log("no app at", script, "- retrying in 10 s")
            time.sleep(10)
            continue

        child = subprocess.Popen([sys.executable, script])
        if pending:
            want = pending.get("version")
            log("checking new version", want)
            ok, why = healthy(child, want)
            if ok:
                os.unlink(p("pending.json"))
                write_json(p("last_update.json"), {"result": "installed", "version": want,
                                                   "from": pending.get("from"), "time": int(time.time())})
                log("update to", want, "confirmed")
            else:
                child.kill()
                child.wait()
                rollback(want, why, pending.get("restore_previous"))
                continue

        rc = child.wait()
        log("app exited with code", rc, "- restarting")
        time.sleep(1 if rc == 0 else 3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
