# HTTP API

Everything the web pages do goes through this JSON API on port 8080, so it can be scripted too.

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
| `POST /api/laser/home` | LightBurn's Home, byte for byte (laser off, head to 0, 0; idle only) |
| `POST /api/laser/jog` | `{"dx","dy"}` (relative) or `{"x","y"}` (absolute) in mm, optional `"speed"` mm/s (1–300): laser-off move inside the bed; jogs stop at the edge |
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

---

[← Back to README](../README.md)
