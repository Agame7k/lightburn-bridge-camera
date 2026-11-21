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
  - log viewer (the service journal), static IP settings
- **Handles a missing camera**: while no camera is working, LightBurn gets a
  "Camera not connected" image instead of a dead stream. Live video returns automatically
  when one is plugged in. Errors are explained in the dashboard.
- **Starts at boot**, survives crashes and camera unplug/replug.
- **Pauses the camera when nobody is watching** (after 30 s, `CAM_IDLE_SECONDS`; `0`
  turns it off). The next stream or snapshot request wakes it up in about a second.
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

- MJPEG stream: `http://lightburnbridge.local:8080/stream` (recommended)
- Snapshot: `http://lightburnbridge.local:8080/snapshot.jpg`

The `.local` name keeps working if the Bridge's IP changes (macOS supports it out of the
box; on Windows/Linux it needs mDNS/Bonjour). Otherwise use the IP: `http://<bridge-ip>:8080/stream`.

Then run *Calibrate Camera Lens* and *Align Camera*. Redo both after changing camera or
resolution.

## Static IP

Dashboard → **Network** sets the Bridge's **WiFi** address to DHCP or a static IP. The
laser's Ethernet link isn't touched.

- Input is validated: subnet, gateway inside the subnet, not the laser's subnet, and a ping
  check that the address isn't already in use.
- After **Apply** the page opens the new address. You have **90 seconds** to press
  **Keep settings** there, otherwise the previous settings are restored automatically. This
  also works across a crash or reboot.
- Choose an address outside your router's DHCP range. A DHCP reservation in the router is an
  equally good alternative.
- If the Bridge is later set up for a different WiFi network (LightBurn's `wifi.txt`), it
  goes back to DHCP.

## Updating

1. Change the code and bump `VERSION` in `lbcam_stream.py`.
2. `python3 make_update.py` → `lbcam-update-<version>.tar.gz` plus
   `lbcam-update-<version>.tar.gz.sha256` (the file's SHA-256, `sha256sum` format).
3. Dashboard → **Software** → choose the file → **Install update**.
4. Optional: compare **Software → File SHA-256** with the `.sha256` file to confirm the
   Bridge got exactly the file you built (`sha256sum -c lbcam-update-*.sha256` checks your copy).

From a script, send the expected hash and the Bridge refuses the file if it doesn't match:

```
curl -X POST -H 'X-LBCam: 1' -H "X-Sha256: $(cut -d' ' -f1 lbcam-update-2.6.tar.gz.sha256)" \
     --data-binary @lbcam-update-2.6.tar.gz http://lightburnbridge.local:8080/api/update
```

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
| `GET /api/update` | version, previous version, installed file's SHA-256, last update result |
| `POST /api/update` | update file as the body (`X-Password` if a password is set, optional `X-Sha256`) |
| `POST /api/update/rollback` | `{"password"}` |
| `POST /api/update/password` | `{"current","new"}`. An empty `new` removes the password |
| `GET /api/network` | WiFi connection, address, mode, pending change |
| `POST /api/network` | `{"mode":"dhcp"}` or `{"mode":"static","address","prefix","gateway","dns"}` |
| `POST /api/network/confirm` | keep a pending change |
| `POST /api/network/revert` | undo a pending change now |
| `GET /api/logs?n=200` | recent log lines |

Every `POST` needs the header `X-LBCam: 1`, and an `Origin` header (if sent) must match the
Bridge's address. That stops other websites from changing settings through your browser.
Requests must use the Bridge's IP, `localhost` or a `.local` name; add other names to
`CAM_ALLOWED_HOSTS` (comma-separated) in `/boot/lbcam.env`.

```
curl -X POST -H 'X-LBCam: 1' http://lightburnbridge.local:8080/api/restart
```

## Development

Runs on any Linux machine with a UVC webcam:

```
python3 lbcam_stream.py              # http://localhost:8080, settings in ./lbcam.json
python3 lbcam_stream.py --selftest
python3 -m unittest discover -s tests -v   # no camera needed
python3 make_update.py                     # builds lbcam-update-<version>.tar.gz
```

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

## Troubleshooting

| Dashboard says | Fix |
|---|---|
| No USB camera detected | Plug in a UVC camera; try another cable/port; avoid unpowered hubs. |
| … does not support MJPEG | The camera is YUYV-only; use an MJPEG-capable camera. |
| … is in use by another program | Restart camera, or reboot the Bridge. |
| No frames from camera for 3 s | Usually power or cable. Retries automatically. |
| Paused | Nobody is watching; it wakes on the next request. Not an error. |
| Camera chose W×H | The camera doesn't support the requested size; pick one from the list. |

Laggy or choppy: lower resolution or frame rate. Weak WiFi is the usual cause.

## Security

- Intended for a trusted local network. The stream and camera settings are open to anyone
  on the network. Software updates can be protected with the optional update password.
- The API refuses cross-site requests and unknown host names (CSRF and DNS-rebinding
  protection), so a web page you visit can't change the Bridge's settings.
- **The stock Bridge image has SSH enabled with the default Raspberry Pi login and
  passwordless sudo.** Change it with `ssh pi@<bridge-ip> passwd`. That comes from the
  stock image, not from this project.

## License

MIT. See [LICENSE](LICENSE).
