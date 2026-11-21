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
            body = data[2:]
            if struct.unpack(">H", data[:2])[0] != sum(body) & 0xFFFF:
                continue
            d = app.ruida_unswizzle(body, self.magic)
            self.requests.append(d)
            self.sock.sendto(app.ruida_swizzle(b"\xCC", self.magic), src)  # ACK first, like the real one
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

    def test_framing_is_not_a_job(self):
        self.ctrl.mem[0x0320] = app.FILE_TOTAL_UNKNOWN
        self.r.poll_once(self.t)
        self.run_job([(0x400603, 0, None), (0x20601, 0, None), (0x600, 0, None)])
        self.assertIsNone(self.r.status()["last_job"])

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


class HealthTest(unittest.TestCase):
    def test_runs_anywhere(self):
        h = app.pi_health()
        self.assertIsInstance(h, dict)
        self.assertIn("uptime", h)


if __name__ == "__main__":
    unittest.main()
