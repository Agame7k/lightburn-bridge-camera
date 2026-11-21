# LightBurn Bridge Camera

Use a USB camera plugged into a **LightBurn Bridge** as a network camera in LightBurn.
That covers camera overlay, Print and Cut, alignment, and the rest.

```
USB camera ─▶ LightBurn Bridge (lbcam, port 8080) ─WiFi─▶ LightBurn "network camera"
```

> **Unofficial.** Not affiliated with or endorsed by LightBurn Software. It adds a
> separate service next to the Bridge's own software and doesn't modify LightBurn's code.
> LightBurn's Bridge image is not included. You patch your own copy.

## Features

- **MJPEG stream + snapshot URLs** that LightBurn's network camera option accepts.
- **Web dashboard** at `http://<bridge-ip>:8080/`:
  - live preview, status, measured fps, viewer count
  - camera picker, resolution and frame rate dropdowns showing what the camera actually supports
  - image controls (exposure, focus, white balance, …), applied live and saved per camera
  - copyable URLs for LightBurn
- **Handles a missing camera**: while no camera is working, LightBurn gets a
  "Camera not connected" image instead of a dead stream. Live video returns automatically
  when one is plugged in. Errors are explained in the dashboard.
- **Starts at boot**, survives crashes and camera unplug/replug.
- **Updates in the field**: upload an update file in the dashboard. Each update is
  self-tested on the Bridge, health-checked after restart, and **rolled back automatically**
  if it fails. Optional update password.
- **No dependencies**: pure Python 3.7 standard library (V4L2 via `ioctl` + `mmap`), so it
  runs on the stock Bridge image as-is. The camera's MJPEG frames are passed through, so CPU
  use is tiny.

## Requirements

- A LightBurn Bridge (Raspberry Pi based). Tested with the **v1.0-RC4** image.
- A USB **UVC camera with MJPEG output**. Nearly all cameras sold for LightBurn qualify.
- LightBurn with network camera support.
- To build an image: Linux with `debugfs` / `e2fsck` (package `e2fsprogs`) and Python 3.

## Install

### Option A: patch the Bridge image and flash it

1. Download the Bridge image from LightBurn (e.g. `LightBurnBridge-v1.0-RC4.img.xz`).
2. Patch it (no root needed, the original is left untouched):
   ```
   python3 patch_image.py LightBurnBridge-v1.0-RC4.img.xz LightBurnBridge-camera.img
   ```
3. Flash `LightBurnBridge-camera.img` with Raspberry Pi Imager (*Use custom*), then set the
   Bridge up as usual.

### Option B: install on a running Bridge over SSH

```
./upgrade_over_ssh.sh <bridge-ip>
```

Asks for the Bridge's SSH password once (stock image: user `pi`, see [Security](#security)).

## Connect LightBurn

In LightBurn's camera settings choose a network camera and enter:

- MJPEG stream: `http://<bridge-ip>:8080/stream` (recommended)
- Snapshot: `http://<bridge-ip>:8080/snapshot.jpg`

Then run *Calibrate Camera Lens* and *Align Camera*. Redo both after changing camera or
resolution. Reserve the Bridge's IP in your router so the URL doesn't change.

## Updating

1. Change the code and bump `VERSION` in `lbcam_stream.py`.
2. `python3 make_update.py` → `lbcam-update-<version>.tar.gz`
3. Dashboard → **Software** → choose the file → **Install update**.

What happens:

1. Checks: file list, SHA-256 checksums, version string, and the password if one is set.
2. The new version runs `--selftest` **on the Bridge** (Python version, V4L2 struct
   sizes, assets). If it fails, nothing changes.
3. It's unpacked to `/opt/lbcam/releases/<version>`, `current` is switched, and the app restarts.
4. The launcher waits up to 45 s for the new version to report itself on `/status`. If it
   crashes or doesn't answer, the launcher switches back and the dashboard shows why.
5. **Roll back** returns to the previous version at any time. The last 3 releases are kept.

Only `lbcam_stream.py`, `dashboard.html` and `placeholder.jpg` can be updated by upload.
`launcher.py` and the service change only via SSH or reflash, so an upload can't break rollback.

## On the Bridge

```
/opt/lbcam/launcher.py                 stable launcher (health check + rollback)
/opt/lbcam/current -> releases/<ver>   running version
/opt/lbcam/previous -> releases/<ver>  rollback target
/opt/lbcam/releases/<ver>/             lbcam_stream.py, dashboard.html, placeholder.jpg
/opt/lbcam/last_update.json            result of the last update
/etc/systemd/system/lbcam.service      starts the launcher at boot
/boot/lbcam.json                       settings (FAT partition, editable from any computer)
/boot/lbcam.env                        optional env overrides (CAM_WIDTH, CAM_FPS, CAM_PORT, …)
```

```
sudo systemctl status lbcam
sudo journalctl -u lbcam -f
```

## API

| | |
|---|---|
| `GET /stream` | MJPEG stream |
| `GET /snapshot.jpg` | single JPEG |
| `GET /status` | version, state, message, camera, fps, viewers |
| `GET /api/state` | everything the dashboard shows |
| `GET /api/cameras` | detected cameras |
| `GET /api/modes?device=<id>` | MJPEG resolutions + frame rates |
| `GET /api/controls` | image controls with ranges and current values |
| `POST /api/config` | `{"device","width","height","fps"}`: saves and restarts the camera |
| `POST /api/control` | `{"id","value"}` |
| `POST /api/controls/reset` | camera defaults, clears saved values |
| `POST /api/restart` | reopen the camera |
| `GET /api/update` | version, previous version, last update result |
| `POST /api/update` | update file as the body (`X-Password` header if a password is set) |
| `POST /api/update/rollback` | `{"password"}` |
| `POST /api/update/password` | `{"current","new"}`. An empty `new` removes the password |

## Development

Runs on any Linux machine with a UVC webcam:

```
python3 lbcam_stream.py              # http://localhost:8080, settings in ./lbcam.json
python3 lbcam_stream.py --selftest
```

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

## Troubleshooting

| Dashboard says | Fix |
|---|---|
| No USB camera detected | Plug in a UVC camera; try another cable/port; avoid unpowered hubs. |
| … does not support MJPEG | The camera is YUYV-only; use an MJPEG-capable camera. |
| … is in use by another program | Restart camera, or reboot the Bridge. |
| No frames from camera for 3 s | Usually power or cable. Retries automatically. |
| Camera chose W×H | The camera doesn't support the requested size; pick one from the list. |

Laggy or choppy: lower resolution or frame rate. Weak WiFi is the usual cause.

## Security

- Intended for a trusted local network. The stream and camera settings are open to anyone
  on the network. Software updates can be protected with the optional update password.
- **The stock Bridge image has SSH enabled with the default Raspberry Pi login and
  passwordless sudo.** Change it with `ssh pi@<bridge-ip> passwd`. That comes from the
  stock image, not from this project.
