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
- **Monitor page** at `http://<bridge-ip>:8080/monitor`, made to be left open (e.g. on a
  phone or tablet next to the laser):
  - live camera (the stream pauses while the tab is hidden, so the camera can idle)
  - job status from the controller: running/idle, progress, elapsed time, last job's duration
  - head position, machine counters (power-on, work and laser-on hours, job count), model
  - laser connection: Ethernet link, whether the controller answers, LightBurn relay status
  - Bridge health: CPU temperature, under-voltage/throttling, load
  - laser buttons: **Pause / Resume / Stop** during a job (Resume asks for confirmation, and for
    the update password when one is set, because it makes the laser fire again)
  - **Go to origin** and **Frame** / **Outline frame** while the laser is idle: the same laser-off
    moves LightBurn sends, built on the Bridge. Stop also cancels a frame or move.
  - **"Laser isn't firing" warning** when a job has run 20 s without the laser-on counter
    moving: usually an alarm on the panel such as Water Prot (the water alarm itself can't be
    read from the controller)
  - **job file view**: a drawing of the job LightBurn sent, by layer colour, with the done part
    and the laser head marked while it runs, plus the last 10 jobs and how each run ended
    (see [Job capture](#job-capture))
  - reloads itself after a software update
- **Alerts on the page** (setup and monitor): job started / paused / resumed / finished /
  stopped at N%, laser disconnected / connected, camera unplugged / plugged in, Bridge hot or
  under-voltage. Important ones stay until dismissed; a hidden tab shows a count in its title.
  Optional sound (bell button in the header).
- **Setup page** at `http://<bridge-ip>:8080/`:
  - live preview, status, measured fps, viewer count
  - camera picker, resolution and frame rate dropdowns showing what the camera actually supports
  - image controls (exposure, focus, white balance, …), applied live and saved per camera
  - copyable URLs for LightBurn
  - log viewer (the service journal), static IP settings, software updates
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
| `GET /api/laser` | laser connection check plus controller status under `controller` (`?fresh=1` skips the 5 s cache) |
| `GET /api/jobs` | captured jobs (newest first, with their runs), the job being received (`live`), the job running now (`running`) and the capture state |
| `GET /api/jobs/<id>.rd` | a captured job as plain (unswizzled) Ruida bytes; `live.rd` is the one still being received |
| `POST /api/jobs/clear` | delete all captured jobs |
| `POST /api/laser/record` | `{"seconds": 30}` (5–120): record LightBurn ↔ laser traffic, decoded (diagnostics) |
| `GET /api/laser/record` | the recording: packets with time, direction and commands; status polls and ACKs only counted |
| `GET /api/health` | Bridge CPU temperature, power/throttling flags, load, uptime |
| `POST /api/laser/scan` | `{"start": "0x200", "count": 256}`. Diagnostic: reads a range of controller memory (idle only, read-only, at most 512 addresses; stops early if the laser gets busy) |
| `POST /api/laser/origin` | move the head to the origin stored in the controller (laser off; idle only) |
| `POST /api/laser/frame` | `{"job": "<id>", "kind": "box" \| "outline"}`: trace a captured job's bounding box or outline (laser off; idle only; absolute-placed jobs only), then return |
| `POST /api/laser/command` | `{"cmd": "pause" \| "resume" \| "stop"}`. `resume` also needs `"password"` when the update password is set |

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

## Troubleshooting

| Dashboard says | Fix |
|---|---|
| No USB camera detected | Plug in a UVC camera; try another cable/port; avoid unpowered hubs. |
| … does not support MJPEG | The camera is YUYV-only; use an MJPEG-capable camera. |
| … is in use by another program | Restart camera, or reboot the Bridge. |
| No frames from camera for 3 s | Usually power or cable. Retries automatically. |
| Paused | Nobody is watching; it wakes on the next request. Not an error. |
| Camera chose W×H | The camera doesn't support the requested size; pick one from the list. |

| Monitor says | Fix |
|---|---|
| No cable | Ethernet from the Bridge to the controller is unplugged, or the laser is off. |
| Not answering | Link is up but nothing answers at the laser IP. The controller's IP (set on the laser's panel) must match the Bridge's laser IP, 10.0.3.3 by default. |
| Relay stopped / Relay error | LightBurn's relay service. Restart the Bridge; the message shows what it reported. |
| Controller status: … | The status reader couldn't start or get replies; the text says why. |

### Laser buttons

| Button | Sends | Allowed when |
|---|---|---|
| Pause | `D8 02` | a job is running |
| Resume | `D8 03` | a job is paused |
| Stop | `D8 01` | a job is running or paused (asks first) |

All three are verified on an RDC6442G. Two others were tried and removed: `D8 2A`
("Home XY" in MeerK40t) **reset the controller** (it rebooted and re-homed), and the panel's
Frame key (`A5 53 00`) was acknowledged but ignored on port 50200.

The Bridge enforces these rules itself, not just the page. Each command waits for the
controller's acknowledgement and is never resent if none arrives, so it can't run twice.
Every command is logged with the address it came from. There is deliberately no way to
start a job or fire the laser from the Bridge.

### How the laser status works

Status reading is read-only:

- **Connection:** Ethernet link state, a ping to the controller (or its ARP entry if ping is
  blocked), and LightBurn's relay status files in `/mnt/lbproc`.
- **Controller status** (Ruida): memory reads over the controller's UDP protocol, the same
  kind LightBurn makes constantly. The controller always replies to UDP port 40200, which
  LightBurn's relay also uses, so the reader runs in its own network namespace (`lbcam`)
  with its own address on the laser link (`10.0.3.254`, change with `LBCAM_RUIDA_IP`). Its
  port 40200 is separate, so the relay never sees our traffic. Polls every 2 s while the
  monitor is open, every 20 s otherwise. Turn it off with `LBCAM_RUIDA=0` in `/boot/lbcam.env`.

The protocol details come from the open-source [MeerK40t](https://github.com/meerk40t/meerk40t)
Ruida driver and were checked on an RDC6442G: status bit 0 = job running, bit 1 (with bit 0) =
paused; job progress = file progress ÷ file total; the job counter only goes up for finished
jobs, which is how "finished" and "stopped at N%" are told apart. Jogging from the panel
doesn't change the status bits, so "Moving" comes from the head position changing; moves
without a job file don't count as jobs. Hour counters are in seconds, the controller's previous job time in milliseconds. Other
models may differ; `/api/laser` includes the raw values under `controller.raw` for checking.

Head moves were recorded from LightBurn on an RDC6442G with **Record laser traffic**. Go to
origin is `C9 02` (speed 100 mm/s), `C6 01`/`C6 21` (power 0), then `D9 10 00 <x> <y>`, a
laser-off rapid move to absolute µm. The origin it goes to is stored in the controller at
`0x0224`/`0x0234` (X/Y) and again at `0x0229`/`0x0239`; the Bridge refuses to move if the two
copies disagree. A frame is the same moves around the outline, at 100 mm/s with all four power
settings at 0. `D8 01` (Stop) cancels a frame that is still being sent. MeerK40t's "Home XY"
(`D8 2A`) is deliberately not used: it reset this controller.

### Job capture

LightBurn's relay sends each job to the controller over the Bridge's Ethernet port. lbcam
copies those packets with a read-only packet socket on `eth0`. It never sends anything, so
neither the relay nor the laser notices. Resent packets are kept once, LightBurn's status
polls are left out, and a job ends at the Ruida end-of-file command (or after 10 s without
data). Streams without a single cut (framing, jogging) aren't kept.

- Jobs are stored in `/opt/lbcam/jobs` (the last 10; change the folder with `LBCAM_JOBS_DIR`)
  as plain Ruida bytes. The dashboard decodes and draws them; the Bridge doesn't.
- A run is matched to the job captured up to 10 minutes before it started. Jobs started from
  files stored on the laser's panel can't be shown unless they were sent while lbcam was running.
- The drawing's done part comes from the controller's progress, so it's approximate. Use
  **Mirror** / **Flip** once to match your machine's origin (remembered per browser).
- Turn it off with `LBCAM_JOBS=0` in `/boot/lbcam.env`.
- **Frame** and **Outline frame** trace the shown job's bounding box or the outline around its
  cuts (its convex hull, like LightBurn's rubber-band frame) at 100 mm/s with laser power set
  to 0, then return the head to where it was. Only for jobs placed with LightBurn's
  *Start From: Absolute Coords* (the job header must match its cuts), and only while idle.
- If a job draws wrong, **Download raw** saves exactly what was captured, for a bug report.
- **Record laser traffic** (setup page) records everything LightBurn and the controller send
  each other for 30 s, decoded into commands. Use it to see what a LightBurn button sends.

Laggy or choppy: lower resolution or frame rate. Weak WiFi is the usual cause.

## Security

- Intended for a trusted local network. The stream and camera settings are open to anyone
  on the network. Software updates and resuming a paused job can be protected with the optional
  update password (Pause and Stop never need it).
- Captured jobs (your designs) can be viewed and downloaded by anyone on the network, like
  the camera. Clear them from the monitor page, or turn capture off with `LBCAM_JOBS=0`.
- The API refuses cross-site requests and unknown host names (CSRF and DNS-rebinding
  protection), so a web page you visit can't change the Bridge's settings.
- **The stock Bridge image has SSH enabled with the default Raspberry Pi login and
  passwordless sudo.** Change it with `ssh pi@<bridge-ip> passwd`. That comes from the
  stock image, not from this project.

## License

MIT. See [LICENSE](LICENSE).
