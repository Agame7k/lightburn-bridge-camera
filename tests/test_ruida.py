"""Ruida status reader against a fake controller on localhost (no laser, no namespace)."""
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from helpers import load

app = load("lbcam_stream")


class FakeRuida:
    """Answers DA 00 memory reads like a controller with the given magic key."""

    def __init__(self, magic=0x88, mem=None):
        self.magic = magic
        self.mem = dict(mem or {})
        self.requests = []
        self.commands = []
        self.silent_commands = False
        self.ack_delay = 0.0  # s before answering a command (a slow controller)
        self.move_time = 0.0  # s each D9 move takes; moves queue up like on the controller
        self.queue = []       # (done_at, x, y)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.2)
        self.stop = False
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while not self.stop:
            try:
                data, src = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self.handle(data, src)
            except OSError:  # socket closed by close() mid-reply
                return

    def handle(self, data, src):
        """Answer one request like a controller would."""
        body = data[2:]
        if struct.unpack(">H", data[:2])[0] != sum(body) & 0xFFFF:
            return
        d = app.ruida_unswizzle(body, self.magic)
        self.requests.append(d)
        if self.silent_commands and d[:1] in (b"\xD8", b"\xA5"):
            return
        cmd = d[:1] in (b"\xD8", b"\xA5", b"\xD9", b"\xC9", b"\xC6", b"\xD0")
        if self.ack_delay and cmd:
            time.sleep(self.ack_delay)
        self.sock.sendto(app.ruida_swizzle(b"\xCC", self.magic), src)  # ACK first, like the real one
        self.commands.append(d) if cmd else None
        if d == b"\xD8\x02":
            self.mem[0x0200] = 0x10403
        elif d == b"\xD8\x03":
            self.mem[0x0200] = 0x10401
        elif d == b"\xD8\x01":
            self.mem[0x0200] = 0x10600
        if d[:1] == b"\xD9" and len(d) == 13:
            start = max(time.time(), self.queue[-1][0] if self.queue else 0)
            self.queue.append((start + self.move_time, app.decode35(d[3:8]), app.decode35(d[8:13])))
        while self.queue and self.queue[0][0] <= time.time():  # the head got there
            _, self.mem[0x0221], self.mem[0x0231] = self.queue.pop(0)
        if d[:2] == b"\xDA\x00" and len(d) == 4:
            addr = (d[2] << 7) | d[3]
            if addr in self.mem:
                self.sock.sendto(app.ruida_swizzle(b"\xDA\x01" + d[2:4] + app.encode35(self.mem[addr]),
                                                   self.magic), src)

    def close(self):
        self.stop = True
        self.sock.close()


class FakeLaser:
    iface = "lo"

    def bridge_config(self):
        return {"laser_ip": "127.0.0.1", "device_type": "ruida", "found": True}

    def link(self):
        return True, True, 100


class CodecTest(unittest.TestCase):
    def test_swizzle_round_trip_and_known_values(self):
        for m in app.RUIDA_MAGICS:
            data = bytes(range(256))
            self.assertEqual(app.ruida_unswizzle(app.ruida_swizzle(data, m), m), data)
        self.assertEqual(app.ruida_swizzle(b"\xCC", 0x88), b"\xC6")  # ACK as documented
        self.assertEqual(app.ruida_swizzle(b"\xDA\x00\x05\x7E", 0x88), bytes((0xD4, 0x89, 0x0D, 0xF7)))

    def test_packet_checksum(self):
        p = app.ruida_packet(b"\xDA\x00\x04\x00", 0x88)
        self.assertEqual(struct.unpack(">H", p[:2])[0], sum(p[2:]))

    def test_35bit_values(self):
        for v in (0, 1, 123456789, -5000, 0x65106510):
            got = app.decode35(app.encode35(v), signed=v < 0)
            self.assertEqual(got, v)
        self.assertEqual(app.ruida_read_request(0x0221), b"\xDA\x00\x04\x21")


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = FakeRuida(magic=0x11, mem={
            0x02FE: 0x65106510, 0x0200: 0x16, 0x0221: 150500, 0x0231: 80250, 0x0241: 0, 0x0251: 0,
            0x0201: 3600 * 120, 0x0202: 3600 * 40, 0x0203: 512, 0x0205: 7, 0x0208: 95000, 0x0211: 3600 * 30,
            0x0320: 1000, 0x0321: 250})
        self.r = app.Ruida(FakeLaser(), enabled=True,
                           transport_factory=lambda ip: app.RuidaUDP(ip, bind=("127.0.0.1", 0), port=self.ctrl.port))
        self.t = self.r._transport("127.0.0.1")

    def tearDown(self):
        self.t.close()
        self.ctrl.close()

    def test_detects_magic_and_reads_everything(self):
        self.assertTrue(self.r.poll_once(self.t))
        s = self.r.status()
        self.assertTrue(s["available"])
        self.assertEqual(s["magic"], "0x11")
        self.assertEqual(s["model"], "RDC6442S")
        self.assertEqual(s["state"], "idle")
        self.assertEqual(s["position"], {"x": 150.5, "y": 80.25, "z": 0.0, "u": 0.0})
        self.assertEqual(s["counters"]["work_count"], 512)
        self.assertEqual(s["counters"]["open_time"], 432000)
        self.assertIsNone(s["progress"])  # only reported while a job runs
        self.assertEqual(s["raw"]["status"], 0x16)

    def run_job(self, steps):
        """steps: list of (status, progress, work_count) applied one poll at a time."""
        for st, prog, count in steps:
            self.ctrl.mem[0x0200] = st
            if prog is not None:
                self.ctrl.mem[0x0321] = prog
            if count is not None:
                self.ctrl.mem[0x0203] = count
            self.r.poll_once(self.t, slow=False)

    def test_states_seen_on_rdc6442g(self):
        self.r.poll_once(self.t)
        self.run_job([(0x10401, 100, None)])
        s = self.r.status()
        self.assertEqual((s["state"], s["running"], s["paused"], s["progress"]), ("running", True, False, 10.0))
        self.run_job([(0x10403, None, None)])
        self.assertEqual(self.r.status()["state"], "paused")
        self.assertTrue(self.r.status()["running"])

    def test_finished_job(self):
        self.r.poll_once(self.t)
        self.run_job([(0x10401, 500, 512)])
        self.r.job["start"] -= 42  # pretend it ran for 42 s
        self.run_job([(0x10600, 0, 513)])
        s = self.r.status()
        self.assertEqual(s["state"], "idle")
        self.assertEqual((s["last_job"]["result"], s["last_job"]["seconds"]), ("finished", 42))
        self.assertIsNone(s["elapsed"])
        self.assertIsNone(s["progress"])

    def test_stopped_job_then_late_counter_means_finished(self):
        self.r.poll_once(self.t)
        self.run_job([(0x10401, 138, 512), (0x10403, 138, 512), (0x10600, 0, 512)])
        lj = self.r.status()["last_job"]
        self.assertEqual((lj["result"], lj["progress"]), ("stopped", 13.8))
        self.run_job([(0x10600, 0, 513)])  # the counter ticks a moment later
        self.assertEqual(self.r.status()["last_job"]["result"], "finished")

    def test_not_firing_warning(self):
        # recorded with Water Prot1 active: running, progress 0, laser-on time frozen
        self.r.laser_live = True  # this controller's counter is known to tick during jobs
        self.r.poll_once(self.t)
        self.run_job([(0x10401, 0, None)])
        self.assertFalse(self.r.status()["not_firing"])
        self.r.job["run_s"] = app.NOT_FIRING_AFTER  # 20 s of running later
        self.run_job([(0x10401, 0, None)])
        self.assertTrue(self.r.status()["not_firing"])
        self.ctrl.mem[0x0211] += 1  # the laser fires after all
        self.run_job([(0x10401, 10, None)])
        self.assertFalse(self.r.status()["not_firing"])

    def test_no_warning_while_counter_not_known_to_tick(self):
        # An RDC6442G didn't update the laser-on counter during a 388 s job that was cutting:
        # without having seen it tick during a job, never warn.
        self.r.poll_once(self.t)
        self.run_job([(0x10401, 0, None)])
        self.r.job["run_s"] = 300
        self.run_job([(0x10401, 50, None)])
        self.assertFalse(self.r.status()["not_firing"])
        self.assertFalse(self.r.status()["laser_counter_live"])

    def test_counter_ticks_arm_warning_and_set_wait(self):
        self.r.poll_once(self.t)
        self.run_job([(0x10401, 0, None)])
        self.r.job["run_s"] = 5
        self.ctrl.mem[0x0211] += 1
        self.run_job([(0x10401, 10, None)])
        self.r.job["run_s"] = 35  # next tick 30 s later: the counter steps slowly
        self.ctrl.mem[0x0211] += 1
        self.run_job([(0x10401, 20, None)])
        self.assertTrue(self.r.laser_live)
        self.assertGreaterEqual(self.r.laser_gap, 29)
        self.run_job([(0x10600, 100, None)])  # done; a new job that doesn't fire
        self.run_job([(0x10401, 0, None)])
        self.r.job["run_s"] = 40  # past 20 s but not past 2 x 30 s
        self.run_job([(0x10401, 0, None)])
        self.assertFalse(self.r.status()["not_firing"])
        self.r.job["run_s"] = 61
        self.run_job([(0x10401, 0, None)])
        self.assertTrue(self.r.status()["not_firing"])

    def test_paused_time_does_not_count_as_not_firing(self):
        self.r.poll_once(self.t)
        self.run_job([(0x10401, 0, None), (0x10403, 0, None)])
        self.r.job["tick"] -= 60  # a minute paused
        self.run_job([(0x10403, 0, None)])
        self.assertFalse(self.r.status()["not_firing"])
        self.assertLess(self.r.job["run_s"], 5)

    def test_firing_job_never_warns(self):
        self.r.poll_once(self.t)
        self.run_job([(0x10401, 50, None)])
        self.ctrl.mem[0x0211] += 3
        self.r.job["run_s"] = 600
        self.run_job([(0x10401, 100, None)])
        self.assertFalse(self.r.status()["not_firing"])

    def test_jogging_is_moving_not_a_job(self):
        # recorded on an RDC6442G while jogging the head from the panel
        self.ctrl.mem[0x0320] = app.FILE_TOTAL_UNKNOWN
        self.r.poll_once(self.t)
        for st in (0x400603, 0x603, 0x20601):
            self.run_job([(st, 0, None)])
            s = self.r.status()
            self.assertEqual((s["state"], s["running"], s["paused"], s["moving"]), ("moving", False, False, True), hex(st))
        self.run_job([(0x600, 0, None)])
        self.assertEqual(self.r.status()["state"], "idle")
        self.assertIsNone(self.r.status()["last_job"])

    def test_jog_after_a_job_with_stale_file_total(self):
        # the previous job's file total stays readable after the job; bit 0x200 still says "manual"
        self.r.poll_once(self.t)
        self.run_job([(0x20601, 0, None)])
        self.assertEqual(self.r.status()["state"], "moving")

    def test_jogging_from_panel_detected_by_position(self):
        # recorded on an RDC6442G: panel jogging leaves the status at 0x10600 / 0x0
        self.ctrl.mem[0x0200] = 0x10600
        self.r.poll_once(self.t)
        self.assertEqual(self.r.status()["state"], "idle")
        self.ctrl.mem[0x0221] = 282500  # X moved 700 -> 282.5 mm
        self.r.poll_once(self.t, slow=False)
        self.assertEqual(self.r.status()["state"], "moving")
        self.r.moved_at -= app.MOVING_HOLD + 1  # time passes without movement
        self.r.poll_once(self.t, slow=False)
        self.assertEqual(self.r.status()["state"], "idle")
        self.assertIsNone(self.r.status()["last_job"])

    def test_u_axis_shown_only_after_it_moves(self):
        self.r.poll_once(self.t)
        self.assertEqual(self.r.status()["axes"], ["x", "y", "z"])
        self.ctrl.mem[0x0251] = 5000
        self.r.poll_once(self.t, slow=False)
        self.assertEqual(self.r.status()["axes"], ["x", "y", "z", "u"])

    def test_fast_poll_skips_counters(self):
        self.r.poll_once(self.t)
        n = len(self.ctrl.requests)
        self.r.poll_once(self.t, slow=False)
        self.assertEqual(len(self.ctrl.requests) - n, len(app.MEM_FAST))

    def test_silent_controller(self):
        self.ctrl.close()
        self.t.timeout = 0.05
        self.assertFalse(self.r.poll_once(self.t))
        self.assertIsNone(self.r.magic)

    def test_background_thread_end_to_end(self):
        self.r.start()
        deadline = time.time() + 5
        while time.time() < deadline and not self.r.status()["available"]:
            time.sleep(0.05)
        self.assertTrue(self.r.status()["available"], self.r.status())


class CommandTest(unittest.TestCase):
    """Commands go through the background thread, like on the Bridge."""

    def setUp(self):
        self.ctrl = FakeRuida(magic=0x88, mem={0x02FE: 0x65006500, 0x0200: 0x10600, 0x0221: 0, 0x0231: 0,
                                               0x0241: 0, 0x0251: 0, 0x0320: 1909, 0x0321: 0, 0x0203: 5, 0x0208: 0,
                                               # answered like a real controller, so the slow poll is quick
                                               0x0201: 0, 0x0202: 0, 0x0205: 0, 0x0211: 0,
                                               0x0224: 0, 0x0234: 0, 0x0229: 0, 0x0239: 0,
                                               0x0026: 700000, 0x0036: 500000})
        self.r = app.Ruida(FakeLaser(), enabled=True,
                           transport_factory=lambda ip: app.RuidaUDP(ip, bind=("127.0.0.1", 0), port=self.ctrl.port))
        self.r.start()
        self.wait_for(lambda s: s["available"])

    def tearDown(self):
        self.ctrl.close()

    def wait_for(self, cond, secs=5):
        end = time.time() + secs
        while time.time() < end:
            s = self.r.status()
            if cond(s):
                return s
            self.r.want()
            time.sleep(0.05)
        self.fail("timed out: %r" % self.r.status())

    def test_pause_resume_stop(self):
        self.ctrl.mem[0x0200] = 0x10401
        self.wait_for(lambda s: s["state"] == "running")
        self.r.command("pause", who="test")
        self.wait_for(lambda s: s["state"] == "paused")
        self.r.command("resume")
        self.wait_for(lambda s: s["state"] == "running")
        self.r.command("stop")
        self.wait_for(lambda s: s["state"] == "idle")
        self.assertEqual(self.ctrl.commands, [b"\xD8\x02", b"\xD8\x03", b"\xD8\x01"])

    def test_home_and_frame_are_not_offered(self):
        # D8 2A reset an RDC6442G; the Frame key packet was ignored. Neither may be sent.
        for name in ("home", "frame"):
            with self.assertRaises(app.RuidaCommandError) as cm:
                self.r.command(name)
            self.assertIn("unknown", str(cm.exception))
        self.assertEqual(self.ctrl.commands, [])

    def test_refused_in_wrong_state(self):
        for name in ("pause", "resume", "stop"):  # laser is idle
            with self.assertRaises(app.RuidaCommandError):
                self.r.command(name)
        self.ctrl.mem[0x0200] = 0x10401
        self.wait_for(lambda s: s["state"] == "running")
        for name in ("resume",):  # not while running
            with self.assertRaises(app.RuidaCommandError) as cm:
                self.r.command(name)
            self.assertIn("running", str(cm.exception))
        with self.assertRaises(app.RuidaCommandError):
            self.r.command("fire")
        self.assertEqual(self.ctrl.commands, [])

    def test_unanswered_command_is_reported_and_not_resent(self):
        self.ctrl.mem[0x0200] = 0x10401
        self.wait_for(lambda s: s["state"] == "running")
        self.ctrl.silent_commands = True
        with self.assertRaises(app.RuidaCommandError) as cm:
            self.r.command("pause")
        self.assertIn("didn't confirm", str(cm.exception))
        self.assertEqual([d for d in self.ctrl.requests if d[:1] == b"\xD8"], [b"\xD8\x02"])


    def test_slow_ack_after_timeout_is_still_reported_as_done(self):
        # The command is already on the wire when command()'s own timeout runs out: it must
        # wait for the answer instead of telling the user it wasn't sent.
        self.ctrl.mem[0x0200] = 0x10401
        self.wait_for(lambda s: s["state"] == "running")
        # pickup can take one read (<= 0.6 s); the answer comes well after command()'s 1 s timeout
        self.ctrl.ack_delay = 2.0
        with mock.patch.object(app, "SEND_WAIT", 3.0), mock.patch.object(app, "SEND_BUDGET", 6.0):
            self.assertTrue(self.r.command("stop", timeout=1.0))
        self.assertEqual(self.ctrl.commands, [b"\xD8\x01"])

    def test_sent_but_unanswered_is_not_called_unsent(self):
        self.ctrl.mem[0x0200] = 0x10401
        self.wait_for(lambda s: s["state"] == "running")
        self.ctrl.silent_commands = True
        with self.assertRaises(app.RuidaCommandError) as cm:
            self.r.command("pause", timeout=1.0)
        self.assertNotIn("not sent", str(cm.exception))


class UnsentCommandTest(unittest.TestCase):
    def test_worker_never_picks_it_up(self):
        r = app.Ruida(FakeLaser(), enabled=True)  # no worker thread running
        r.state, r.values, r.magic = {"available": True}, {"state": "running"}, 0x88
        with self.assertRaises(app.RuidaCommandError) as cm:
            r.command("stop", timeout=0.2)
        self.assertIn("not sent", str(cm.exception))
        self.assertIsNone(r.cmd)  # withdrawn: a late worker won't send it


ORIGIN = {0x0224: 276113, 0x0234: 435380, 0x0229: 276113, 0x0239: 435380,
          0x0026: 700000, 0x0036: 500000}  # and the bed size (axis travel), as read on a 700 x 500 machine


class MotionTest(CommandTest):
    """Go to origin and Frame, as LightBurn sends them (recorded on an RDC6442G)."""

    def setUp(self):
        super().setUp()
        self.ctrl.mem.update(ORIGIN)
        self.ctrl.mem.update({0x0221: 148845, 0x0231: 186540})  # head jogged away from the origin

    def test_go_to_origin_matches_lightburn(self):
        r = self.r.go_to_origin(who="test")
        c = self.ctrl.commands
        self.assertEqual(c[:3], [
            bytes.fromhex("C9 02 00 00 06 0D 20"),           # 100 mm/s
            bytes.fromhex("C6 01 00 00"), bytes.fromhex("C6 21 00 00")])
        self.assertEqual(c[-1], bytes.fromhex("D9 10 00 00 00 10 6D 11 00 00 1A 49 34"))  # LightBurn's move
        steps = c[3:]  # ~281 mm in 50 mm pieces, all on the straight line from the head
        self.assertEqual(len(steps), 6)
        self.assertTrue(all(s[:3] == b"\xD9\x10\x00" for s in steps))
        self.assertEqual(r["sent"], r["packets"])

    def test_home_is_lightburns_bytes(self):
        self.r.home(who="test")
        self.assertEqual([c.hex(" ").upper() for c in self.ctrl.commands], [
            "C9 02 00 00 06 0D 20", "C6 01 00 00", "C6 21 00 00",
            "D0 29 89 89 D9 10 00 00 00 00 00 00 00 00 00 00 00"])  # one packet, as recorded

    def test_origin_copies_must_agree(self):
        self.ctrl.mem[0x0229] = 0
        with self.assertRaises(app.RuidaCommandError) as cm:
            self.r.go_to_origin()
        self.assertIn("disagree", str(cm.exception))
        self.assertEqual(self.ctrl.commands, [])

    def test_origin_shown_in_status(self):
        with self.r.lock:  # the origin is a slow (60 s) read: interpret it directly
            self.r.raw.update(origin_x=276113, origin_y=435380, origin_x2=276113, origin_y2=435380)
            self.r._interpret()
        self.assertEqual(self.r.status()["origin"], {"x": 276.11, "y": 435.38})
        with self.r.lock:
            self.r.raw["origin_x2"] = 0
            self.r._interpret()
        self.assertNotIn("origin", self.r.status())  # copies disagree: not shown

    def test_only_while_idle(self):
        self.ctrl.mem[0x0200] = 0x10401
        self.wait_for(lambda s: s["state"] == "running")
        for call in (self.r.go_to_origin, lambda: self.r.frame([(0, 0), (1000, 0), (1000, 1000)])):
            with self.assertRaises(app.RuidaCommandError) as cm:
                call()
            self.assertIn("idle", str(cm.exception))
        self.assertEqual(self.ctrl.commands, [])

    def test_box_frame_matches_lightburn_and_returns(self):
        box = [(306000, 40000), (306000, 332000), (626000, 332000), (626000, 40000), (306000, 40000)]
        self.r.frame(box)
        hx = lambda b: b.hex(" ").upper()
        c = [hx(x) for x in self.ctrl.commands]
        self.assertEqual(c[:5], ["C9 02 00 00 00 00 00", "C6 01 00 00", "C6 02 00 00", "C6 21 00 00", "C6 22 00 00"])
        self.assertIn("C9 02 00 00 06 0D 20", c)  # 100 mm/s for the frame itself
        last_speed = len(c) - 1 - c[::-1].index("C9 02 00 00 00 00 00")
        self.assertTrue(all(x.startswith("D9 10 00") for x in c[last_speed + 1:]))
        self.assertEqual(c[-1], hx(app.ruida_rapid(148845, 186540)))  # back to where the head was
        targets = [(app.decode35(x[3:8]), app.decode35(x[8:13])) for x in self.ctrl.commands if x[:1] == b"\xD9"]
        corners = [t for t in targets if t in box]  # LightBurn's corners, in LightBurn's order
        self.assertEqual(corners[:5], box)
        frame_part = targets[targets.index(box[0]):len(targets) - 0]
        for a_, b_ in zip(frame_part, frame_part[1:]):  # no piece longer than MOVE_CHUNK
            self.assertLessEqual(((a_[0] - b_[0]) ** 2 + (a_[1] - b_[1]) ** 2) ** 0.5, app.MOVE_CHUNK + 1)

    def test_moves_are_paced(self):
        # The controller keeps running moves it already has, so never hand it more than a few.
        self.ctrl.move_time = 0.15
        outline = [(150000 + i * 2000, 190000 + (i % 2) * 3000) for i in range(12)]  # zigzag
        ahead = []
        orig = self.ctrl.handle

        def spy(data, src):
            orig(data, src)
            d = app.ruida_unswizzle(data[2:], self.ctrl.magic)
            if d[:1] == b"\xD9":
                ahead.append(len(self.ctrl.queue))
        self.ctrl.handle = spy
        self.r.frame(outline)
        self.assertLessEqual(max(ahead), app.MOVES_AHEAD)

    def test_stop_interrupts_a_frame(self):
        self.ctrl.move_time = 0.1
        outline = [(100000 + i * 1000, 100000 + (i % 2) * 2000) for i in range(60)]
        res = {}

        def run():
            try:
                self.r.frame(outline)
            except app.RuidaCommandError as e:
                res["err"] = str(e)
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.6)
        self.assertTrue(self.r.command("stop"))  # allowed: the head is moving (status may still say idle)
        t.join(20)
        self.assertRegex(res.get("err", ""), "(interrupted|cancelled) by stop")  # depends on timing
        self.assertIn(b"\xD8\x01", self.ctrl.commands)
        d9 = [i for i, c in enumerate(self.ctrl.commands) if c[:1] == b"\xD9"]
        stop = self.ctrl.commands.index(b"\xD8\x01")
        self.assertLess(len(d9), 20)  # stopped early, not all 60+ moves
        self.assertTrue(all(i < stop for i in d9))  # and nothing was sent after the Stop

    def test_stop_cancels_a_queued_frame(self):
        # Stop before the worker started the frame: the frame must not run after the Stop.
        r = app.Ruida(FakeLaser(), enabled=True)  # no worker thread: we play the worker
        r.state, r.values, r.magic = {"available": True}, {"state": "idle"}, 0x88
        job = {"name": "frame", "build": None, "who": "t", "done": threading.Event(), "error": None, "result": None}
        r.motion = job  # queued, not started
        threading.Thread(target=lambda: self.assertRaises(app.RuidaCommandError, r.command, "stop", "t", 0.3)).start()
        time.sleep(0.1)
        self.assertTrue(job.get("cancel"))
        r._do_motion(None)  # what the worker does next: builds and sends nothing
        self.assertEqual(job["error"], "cancelled by stop before it started")
        self.assertEqual(job["result"]["sent"], 0)

    def d9_targets(self):
        return [(app.decode35(c[3:8]), app.decode35(c[8:13])) for c in self.ctrl.commands if c[:1] == b"\xD9"]

    def test_jog_relative_laser_off(self):
        self.r.jog(dx=10, dy=-5, speed=50)
        c = self.ctrl.commands
        self.assertEqual(c[:3], [app.ruida_speed(50), bytes.fromhex("C6 01 00 00"), bytes.fromhex("C6 21 00 00")])
        self.assertEqual(self.d9_targets(), [(158845, 181540)])

    def test_jog_stops_at_the_bed_edge(self):
        self.r.jog(dx=-1000, dy=1000)  # far past both edges
        self.assertEqual(self.d9_targets()[-1], (0, 500000))
        self.ctrl.commands.clear()
        with self.assertRaises(app.RuidaCommandError) as cm:
            self.r.jog(dx=-10)  # already at X 0
        self.assertIn("edge", str(cm.exception))
        self.assertEqual(self.ctrl.commands, [])

    def test_move_to_inside_bed_only(self):
        self.r.jog(x=300, y=100)
        self.assertEqual(self.d9_targets()[-1], (300000, 100000))
        self.ctrl.commands.clear()
        for x, y in ((701, 10), (10, -1)):
            with self.assertRaises(app.RuidaCommandError) as cm:
                self.r.jog(x=x, y=y)
            self.assertIn("outside the bed", str(cm.exception))
        self.assertEqual(self.ctrl.commands, [])

    def test_jog_needs_a_sensible_bed_size(self):
        self.ctrl.mem[0x0026] = 0
        with self.assertRaises(app.RuidaCommandError) as cm:
            self.r.jog(dx=1)
        self.assertIn("bed size", str(cm.exception))

    def test_quick_jogs_add_up(self):
        self.ctrl.move_time = 0.5  # the first jog is still moving when the second comes
        self.r.jog(dx=10)
        self.r.want()
        self.wait_for(lambda s: s["state"] == "moving" or s.get("motion"))
        with self.r.lock:  # the status reader sees the head moving
            self.r.values["state"] = "moving"
        self.r.jog(dx=10)
        self.assertEqual(self.d9_targets()[-1], (168845, 186540))  # 20 mm from the start, not 10 from mid-move

    def test_speed_limited(self):
        self.r.jog(dx=1, speed=5000)
        self.assertEqual(self.ctrl.commands[0], app.ruida_speed(app.JOG_SPEED_MAX))

    def test_stop_still_refused_when_nothing_moves(self):
        with self.assertRaises(app.RuidaCommandError):
            self.r.command("stop")


class ScanTest(CommandTest):
    def test_command_is_sent_during_a_scan(self):
        res = {}
        t = threading.Thread(target=lambda: res.update(r=self.r.scan(0x1000, 512)))
        t.start()
        time.sleep(0.5)  # scan in progress (these addresses don't answer: 0.06 s each)
        job = {"name": "stop", "payload": b"\xD8\x01", "who": "test", "done": threading.Event(),
               "result": None, "sent": False}
        with self.r.cmd_lock:
            self.r.cmd = job
        self.assertTrue(job["done"].wait(1.5), "command waited behind the scan")
        self.assertEqual(job["result"], "ack")
        t.join(40)

    def test_scan_stops_when_laser_gets_busy(self):
        res = {}
        t = threading.Thread(target=lambda: res.update(r=self.r.scan(0x1000, 512)))
        t.start()
        time.sleep(0.3)
        self.ctrl.mem[0x0200] = 0x10401  # a job started from LightBurn
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertEqual(res["r"]["aborted"], "the laser became busy")
        self.assertLess(res["r"]["seconds"], 10)
        self.wait_for(lambda s: s["state"] == "running")


    def test_scan_reads_range_only_answered(self):
        for a in (0x0201, 0x0202):  # two addresses that don't answer
            del self.ctrl.mem[a]
        r = self.r.scan(0x0200, 4)
        self.assertEqual(r["values"], {"0x0200": 0x10600, "0x0203": 5})  # 0x201/0x202 don't answer
        self.assertEqual((r["count"], r["answered"], r["missing"]), (4, 2, 2))
        self.assertTrue(all(d[:2] == b"\xDA\x00" for d in self.ctrl.requests))  # reads only

    def test_scan_refused_during_job(self):
        self.ctrl.mem[0x0200] = 0x10401
        self.wait_for(lambda s: s["state"] == "running")
        with self.assertRaises(app.RuidaCommandError):
            self.r.scan(0x200, 16)

    def test_scan_is_capped(self):
        r = self.r.scan(0x3FFE, 5000)
        self.assertEqual(r["count"], 2)


class HealthTest(unittest.TestCase):
    def test_runs_anywhere(self):
        h = app.pi_health()
        self.assertIsInstance(h, dict)
        self.assertIn("uptime", h)


if __name__ == "__main__":
    unittest.main()
