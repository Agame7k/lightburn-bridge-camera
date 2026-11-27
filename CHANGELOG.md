# Changelog

Versions are the `VERSION` in `lbcam_stream.py` and the name of the update file
(`lbcam-update-<version>.tar.gz`). Newest first.

## 3.1 (2026-11-27)

- **Flip X / Flip Y** on the Setup page (*Machine direction*), for machines whose X or Y runs the
  other way. Saved on the Bridge, so every phone and computer follows it: the job drawing, the
  head marker and the Control page's jog arrows all react within a couple of seconds. Replaces the
  per-browser Mirror / Flip buttons on the job drawing. Changing it doesn't restart the camera.

## 3.0 (2026-11-27)

### New: Control page (`/control`)

A page for driving the laser from a phone standing next to the machine.

- Big status (Idle / Running / Paused / Moving), progress, elapsed time, head X/Y and the bed size.
- **Jog pad** with diagonals, like LightBurn's Move window: distance presets (0.1 / 1 / 10 / 50 /
  100 mm) or any value, and a jog speed (1–300 mm/s). Both are remembered on the device.
  Quick taps add up while the head is still moving.
- **Move to** X/Y, **Home** and **Go to origin** (in the middle of the jog pad).
- The job drawing with **Frame** / **Outline frame**, and the recent jobs.
- **Stop** and **Pause / Resume** always at the bottom of the screen. Stop vibrates the phone
  where supported.
- Phone details: large touch targets, no double-tap zoom, polls every second while visible and
  not at all while hidden, an optional camera (off by default, to save data), keeps the screen on
  during a job where the browser allows it, and can be added to the home screen.
- New **Control** tab in the header. The header fits small phones.

### New: Home, jog and move to

- **Home**: LightBurn's own Home, recorded on an RDC6442G and sent byte for byte.
- **Jog / Move to** (`POST /api/laser/jog`): laser-off moves, power forced to 0, at the chosen
  speed. Never outside the bed: jogs stop at the edge, a Move to outside it is refused.
- The bed size is read from the controller's axis settings (X travel `0x0026`, Y travel `0x0036`)
  and shown in the status.

## 2.19 (not released; folded into 3.0)

- **Stop now really stops a frame or move.** The controller keeps running moves it already has
  after Stop, so moves are now handed over a few at a time while watching the head (at most two
  ahead, long sides split into 50 mm pieces). Stop ends a frame within about 100 mm. The head
  marker moves live during frames.
- Stop pressed just after Frame, before it started, cancels the frame instead of being refused
  or followed by the whole frame.
- **No more false "Laser isn't firing" warnings.** An RDC6442G doesn't update its laser-on counter
  during a job. The warning only turns on once the counter has been seen to tick during a job, and
  then waits at least twice the longest gap between ticks.
- Home button (LightBurn's recorded bytes).

## 2.18 (2026-11-27)

- **Go to origin**: reads the origin stored in the controller (`0x0224`/`0x0234`, with a second
  copy at `0x0229`/`0x0239` that must agree) and moves there like LightBurn.
- **Frame** and **Outline frame** for a captured job: its bounding box or the outline around its
  cuts (like LightBurn's rubber-band frame), laser-off at 100 mm/s, then back to where the head
  was. Only for jobs placed with *Start From: Absolute Coords*.
- **Stop** also shows while the head is moving.
- Capture diagnostics in `/api/jobs`: frames and UDP flows seen, the laser and own address,
  capture errors (logged instead of stopping the capture).

## 2.17 (2026-11-27)

- Fixed: job capture never saw what LightBurn sends. A packet socket bound to IPv4 only doesn't
  get frames the Bridge itself sends, so it now listens to all protocols and filters IPv4 itself.
- Capture status counts the laser's replies too.

## 2.16 (2026-11-27)

- **Job file view** on the monitor page: jobs LightBurn sends through the Bridge are captured
  (passively, from `eth0`; nothing is sent to the laser) and drawn in their layer colours. The done
  part is shown solid and the laser head marked while a job runs. The last 10 jobs are kept,
  with how each run ended. Mirror / Flip to match the machine, Download raw, Clear history.
- Jobs started later from the laser's panel are linked to the file sent within 10 minutes before.
- **Record laser traffic** (setup page): records and decodes what LightBurn and the controller
  send each other for 30 s. Used to work out Home, Go to origin and both frame types.
- Laser commands: a command that may already have been sent is never reported as failed (the
  result is "unknown, check the machine" instead). Stop no longer waits behind status reads or a
  memory scan.
- The memory scan is a `POST` (so other websites can't start it), at most 512 addresses, and stops
  when the laser gets busy.
- **Resume** asks for confirmation, and needs the update password when one is set. Pause and
  Stop never do.

## 2.15 (2026-11-20)

- Laser buttons: **Pause / Resume / Stop** from the monitor page (`POST /api/laser/command`).
- Diagnostic read of a range of controller memory.
- "Moving" state when the head is jogged from the panel. "Laser isn't firing" warning.

## 2.9 (2026-11-20)

- Laser controller monitoring: connection, relay status, and Ruida status read from a separate
  network namespace (job state, progress, head position, counters, model). Bridge health
  (temperature, power, load).

## 2.6 (2026-11-20)

- Updates carry a SHA-256 checksum (`.sha256` file), checked on install.

## 2.5 (2026-11-20)

- CI, unit tests, update package builder.

## 2.4 (2026-11-20)

- Static IP settings, with automatic revert if the Bridge can't be reached.

## 2.2 (2026-11-20)

- First release: a USB camera on a LightBurn Bridge as a LightBurn network camera (MJPEG stream
  and snapshot), setup page, camera settings, software updates with rollback.
