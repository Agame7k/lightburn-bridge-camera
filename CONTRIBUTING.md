# Contributing

Thanks for helping out! Bug reports, hardware reports ("works on my controller / camera") and
pull requests are all welcome.

## Ground rules

- **Standard library only, Python 3.7.** The code has to run on the stock Bridge image as-is.
  No `pip install`, no f-string features newer than 3.7, no new dependencies.
- **Never make the laser fire.** Anything that talks to the controller must be laser-off,
  idle-only where it moves the head, and enforced on the Bridge, not just in the page.
- **`dashboard.html` stays one file** with no build step.
- Add or update a test in `tests/` for behaviour changes, and a line in
  [CHANGELOG.md](CHANGELOG.md) for anything users will notice.

## Running it locally

Runs on any Linux machine with a UVC webcam:

```
python3 lbcam_stream.py              # http://localhost:8080, settings in ./lbcam.json
python3 lbcam_stream.py --selftest
python3 -m unittest discover -s tests -v   # no camera needed
python3 make_update.py                     # builds lbcam-update-<version>.tar.gz
```

Without a laser, point the controller reader at a Ruida emulator (MeerK40t has one) with
`LBCAM_RUIDA_DIRECT=host:port`; that skips the network namespace.

GitHub Actions runs the tests on Python 3.7 (the Bridge's version) and 3.12. Pushing a tag
`v<VERSION>` (e.g. `v2.5`) builds the update file and attaches it to a GitHub release.

## Project layout

| File | Purpose |
|---|---|
| `lbcam_stream.py` | camera server, dashboard API, updater |
| `dashboard.html` | dashboard UI (single file, no build step) |
| `placeholder.jpg` | "Camera not connected" image |
| `launcher.py` | runs the current release, health-checks updates, rolls back |
| `lbcam.service` | systemd unit |
| `patch_image.py` | adds lbcam to a Bridge `.img`/`.img.xz` offline |
| `make_update.py` | builds an update file |
| `upgrade_over_ssh.sh` | installs onto a running Bridge over SSH |
| `tests/` | unit tests (standard library `unittest`) |

## Pull requests

1. Fork, branch from `main`, make the change.
2. `python3 -m unittest discover -s tests -v` passes (CI also runs it on Python 3.7 and 3.12).
3. If you changed what gets installed, bump `VERSION` in `lbcam_stream.py`.
4. Describe what you tested on real hardware, if anything (Bridge image, controller model, camera).
