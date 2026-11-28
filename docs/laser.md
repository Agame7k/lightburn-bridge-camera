# Laser control, status and job capture

How the monitor and control pages talk to the laser, and exactly what they send.

## Laser buttons

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

## How the laser status works

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
without a job file don't count as jobs. With the door open or a water fault the controller holds
a job ("Machine protected" on its panel) at the jog bits `0x10603` with the head still, which is
shown as **Held**; the door and water inputs themselves can't be read. Hour counters are in seconds, the controller's previous job time in milliseconds. Other
models may differ; `/api/laser` includes the raw values under `controller.raw` for checking.

Head moves were recorded from LightBurn on an RDC6442G with **Record laser traffic**. Go to
origin is `C9 02` (speed 100 mm/s), `C6 01`/`C6 21` (power 0), then `D9 10 00 <x> <y>`, a
laser-off rapid move to absolute µm. The origin it goes to is stored in the controller at
`0x0224`/`0x0234` (X/Y) and again at `0x0229`/`0x0239`; the Bridge refuses to move if the two
copies disagree. A frame is the same moves around the outline, at 100 mm/s with all four power
settings at 0. `D8 01` (Stop) cancels a frame that is still being sent. MeerK40t's "Home XY"
(`D8 2A`) is deliberately not used: it reset this controller.

The bed size comes from the axis settings: X travel at `0x0026` and Y travel at `0x0036` (read
700000 / 500000 µm on a 700 × 500 mm machine). Jog and Move to are the same laser-off `D9` moves
at the chosen speed (`C9 02`), never outside 0 to travel; Home is LightBurn's recorded bytes,
sent as-is.

## Job capture

LightBurn's relay sends each job to the controller over the Bridge's Ethernet port. lbcam
copies those packets with a read-only packet socket on `eth0`. It never sends anything, so
neither the relay nor the laser notices. Resent packets are kept once, LightBurn's status
polls are left out, and a job ends at the Ruida end-of-file command (or after 10 s without
data). Streams without a single cut (framing, jogging) aren't kept.

- Jobs are stored in `/opt/lbcam/jobs` (the last 10; change the folder with `LBCAM_JOBS_DIR`)
  as plain Ruida bytes. The dashboard decodes and draws them; the Bridge doesn't.
- A run is matched to the job captured up to 10 minutes before it started. Jobs started from
  files stored on the laser's panel can't be shown unless they were sent while lbcam was running.
- The drawing's done part comes from the controller's progress, so it's approximate. If it's
  mirrored compared to the bed, or the jog arrows go the wrong way, turn on **Flip X** / **Flip Y**
  under *Machine direction* on the Setup page (saved on the Bridge, for every device).
- Turn it off with `LBCAM_JOBS=0` in `/boot/lbcam.env`.
- **Frame** and **Outline frame** trace the shown job's bounding box or the outline around its
  cuts (its convex hull, like LightBurn's rubber-band frame) at 100 mm/s with laser power set
  to 0, then return the head to where it was. Works for LightBurn's *Start From: Absolute Coords*
  (the job header must match its cuts) and *Current Position* (placed from where the head is
  when you press Frame, refused if any of it would leave the bed), only while idle. *User Origin*
  jobs are drawn but can't be framed yet. The job file says which: `D8 10` absolute, `D8 11`
  user origin, `D8 12` current position.
- If a job draws wrong, **Download raw** saves exactly what was captured, for a bug report.
- **Record laser traffic** (setup page) records everything LightBurn and the controller send
  each other for 30 s, decoded into commands. Use it to see what a LightBurn button sends.

---

[← Back to README](../README.md)
