"""Ruida status reader against a fake controller on localhost (no laser, no namespace)."""
import socket
import struct
import threading
import time
import unittest

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
        self.sock.sendto(app.ruida_swizzle(b"\xCC", self.magic), src)  # ACK first, like the real one
        self.commands.append(d) if d[:1] in (b"\xD8", b"\xA5") else None
        if d == b"\xD8\x02":
            self.mem[0x0200] = 0x10403
        elif d == b"\xD8\x03":
            self.mem[0x0200] = 0x10401
        elif d == b"\xD8\x01":
            self.mem[0x0200] = 0x10600
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
        self.r.poll_once(self.t)
        self.run_job([(0x10401, 0, None)])
        self.assertFalse(self.r.status()["not_firing"])
        self.r.job["run_s"] = app.NOT_FIRING_AFTER  # 20 s of running later
        self.run_job([(0x10401, 0, None)])
        self.assertTrue(self.r.status()["not_firing"])
        self.ctrl.mem[0x0211] += 1  # the laser fires after all
        self.run_job([(0x10401, 10, None)])
        self.assertFalse(self.r.status()["not_firing"])

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
                                               0x0241: 0, 0x0251: 0, 0x0320: 1909, 0x0321: 0, 0x0203: 5, 0x0208: 0})
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


class ScanTest(CommandTest):
    def test_scan_reads_range_only_answered(self):
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
