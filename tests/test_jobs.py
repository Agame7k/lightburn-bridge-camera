"""Job capture: frame parsing, packet assembly into job files, the store, the API, and the
dashboard's Ruida decoder (run with node when it is installed)."""
import http.client
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

from helpers import load

app = load("lbcam_stream")
HERE = os.path.dirname(os.path.abspath(__file__))
MAGIC = 0x88


def a(v):
    return app.encode35(v)


def r(v):
    v &= 0x3FFF
    return bytes(((v >> 7) & 0x7F, v & 0x7F))


def square_job():
    """A 10 x 10 mm square at (10, 20) mm on layer 0 (red), plus a line on layer 1."""
    return (b"\xD8\x12"                                   # header-ish command, ignored
            + b"\xCA\x06\x00" + a(0x0000FF)               # layer 0 colour: red (COLORREF)
            + b"\xCA\x06\x01" + a(0xFF0000)               # layer 1 colour: blue
            + b"\xCA\x02\x00"                             # layer 0
            + b"\x88" + a(10000) + a(20000)               # move to (10, 20) mm
            + b"\xA8" + a(20000) + a(20000)               # cut abs
            + b"\xAB" + r(5000) + b"\xAB" + r(5000)       # cut rel y, twice
            + b"\xAA" + r(-5000) + b"\xAA" + r(-5000)     # cut rel x, twice
            + b"\xA9" + r(0) + r(-8000) + b"\xAB" + r(-2000)  # back to start
            + b"\xCA\x02\x01"
            + b"\x89" + r(1000) + r(1000)                 # move rel
            + b"\xA9" + r(2000) + r(0)                    # one cut on layer 1
            + b"\xD7")                                    # EOF


def packets(data, size=7):
    return [app.ruida_packet(data[i:i + size], MAGIC) for i in range(0, len(data), size)]


ACK = app.ruida_swizzle(b"\xCC", MAGIC)
NAK = app.ruida_swizzle(b"\xCF", MAGIC)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FrameTest(unittest.TestCase):
    @staticmethod
    def frame(src, sport, dst, dport, payload, frag=0):
        udp = struct.pack(">HHHH", sport, dport, 8 + len(payload), 0) + payload
        ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, frag, 64, 17, 0,
                         socket.inet_aton(src), socket.inet_aton(dst))
        return b"\x00" * 12 + b"\x08\x00" + ip + udp + b"\x00" * 4  # trailing Ethernet padding

    def test_udp(self):
        f = self.frame("10.0.3.1", 40200, "10.0.3.3", 50200, b"hello")
        self.assertEqual(app.parse_udp_frame(f), ("10.0.3.1", 40200, "10.0.3.3", 50200, b"hello"))

    def test_rejects_other_traffic(self):
        f = bytearray(self.frame("10.0.3.1", 1, "10.0.3.3", 2, b"x"))
        self.assertIsNone(app.parse_udp_frame(bytes(f[:30])))
        f[23] = 6  # TCP
        self.assertIsNone(app.parse_udp_frame(bytes(f)))
        self.assertIsNone(app.parse_udp_frame(self.frame("10.0.3.1", 1, "10.0.3.3", 2, b"x", frag=0x2000)))
        arp = b"\x00" * 12 + b"\x08\x06" + b"\x00" * 40
        self.assertIsNone(app.parse_udp_frame(arp))


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = app.JobStore(self.tmp, keep=3)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class AssemblerTest(StoreBase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.asm = app.JobAssembler(self.store, lambda: MAGIC, clock=self.clock)

    def send(self, data, ack=True, size=7):
        for p in packets(data, size):
            self.asm.to_controller(p)
            if ack:
                self.asm.from_controller(ACK)
            self.clock.t += 0.01

    def stored(self):
        return [self.store.read(j["id"]) for j in reversed(self.store.list())]

    def test_job_reassembled_and_stored_at_eof(self):
        self.send(square_job())
        self.assertEqual(self.stored(), [square_job()])
        j = self.store.list()[0]
        self.assertTrue(j["complete"])
        self.assertEqual(j["cuts"], 8)
        self.assertEqual(self.asm.live(), (None, None))

    def test_resend_kept_once_and_nak_dropped(self):
        ps = packets(square_job())
        for i, p in enumerate(ps):
            self.asm.to_controller(p)
            if i == 2:
                self.asm.to_controller(p)  # LightBurn timed out and resent it
            if i == 4:
                self.asm.from_controller(NAK)  # rejected, sent again
                self.asm.to_controller(p)
            self.asm.from_controller(ACK)
        self.assertEqual(self.stored(), [square_job()])
        self.assertEqual(self.asm.stats["resent"], 1)  # after a NAK the next copy is new, not a resend
        self.assertEqual(self.asm.stats["nak"], 1)

    def test_missed_acks_still_assemble(self):
        self.send(square_job(), ack=False)  # the next packet going out commits the previous one
        self.asm.from_controller(ACK)  # ...and the last needs one answer
        self.assertEqual(self.stored(), [square_job()])

    def test_status_polls_left_out(self):
        data = square_job()
        ps = packets(data)
        poll = app.ruida_packet(app.ruida_read_request(0x0200), MAGIC)
        for i, p in enumerate(ps):
            self.asm.to_controller(p)
            self.asm.from_controller(ACK)
            if i % 3 == 0:
                self.asm.to_controller(poll)
                self.asm.from_controller(ACK)
        self.assertEqual(self.stored(), [data])

    def test_bad_checksum_and_unknown_magic_ignored(self):
        bad = bytearray(packets(square_job())[0])
        bad[0] ^= 0xFF
        self.asm.to_controller(bytes(bad))
        self.assertEqual(self.asm.stats["bad_checksum"], 1)
        asm = app.JobAssembler(self.store, lambda: None)
        asm.to_controller(packets(square_job())[0])
        self.assertEqual(asm.stats["no_magic"], 1)

    def test_framing_without_cuts_not_kept(self):
        self.send(b"\x88" + a(0) + a(0) + b"\x89" + r(100) + r(100) + b"\xD7")
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.asm.stats["ignored"], 1)

    def test_two_jobs_back_to_back(self):
        self.send(square_job() + square_job()[:-1] + b"\xAA" + r(1) + b"\xD7", size=50)
        self.assertEqual(len(self.stored()), 2)

    def test_live_then_gap_ends_job(self):
        data = square_job()[:-1]  # no EOF
        self.send(data)
        start, live = self.asm.live()
        self.assertEqual(live, data)
        self.assertEqual(start, 1000.0)
        self.clock.t += app.JOB_GAP + 1
        self.asm.tick()
        self.assertEqual(self.stored(), [data])
        self.assertFalse(self.store.list()[0]["complete"])


class StoreTest(StoreBase):
    def test_keeps_last_n_and_reloads(self):
        ids = [self.store.add(square_job() + bytes([i]), 1000.0 + i * 100, 1001.0 + i * 100, True) for i in range(5)]
        self.assertEqual([j["id"] for j in self.store.list()], ids[:1:-1])
        self.assertEqual(sorted(os.listdir(self.tmp)), sorted([i + ".rd" for i in ids[2:]] + ["index.json"]))
        again = app.JobStore(self.tmp, keep=3)
        self.assertEqual(again.list(), self.store.list())

    def test_read_checks_id(self):
        jid = self.store.add(square_job(), 1000.0, 1001.0, True)
        self.assertEqual(self.store.read(jid), square_job())
        for bad in ("../index", "index", jid[:-1] + "x", "20260101-000000-abcdef"):
            self.assertIsNone(self.store.read(bad), bad)

    def test_runs_attach_to_job_and_corrections_replace(self):
        jid = self.store.add(square_job(), 1000.0, 1002.0, True)
        self.store.attach_run({"start": 1003.0, "seconds": 60, "result": "stopped", "progress": 99.0})
        self.store.attach_run({"start": 1003.0, "seconds": 60, "result": "finished", "progress": 100.0})
        self.store.attach_run({"start": 1200.0, "seconds": 61, "result": "finished", "progress": 100.0})
        self.store.attach_run({"start": 1000.0 + app.RUN_MATCH_BEFORE + 50, "seconds": 5, "result": "stopped",
                               "progress": 3.0})  # too long after: some other file
        runs = self.store.list()[0]["runs"]
        self.assertEqual([(x["start"], x["result"]) for x in runs], [(1003.0, "finished"), (1200.0, "finished")])
        self.assertEqual(self.store.match(1003.0), jid)
        self.assertIsNone(self.store.match(999.0 - 10))

    def test_run_during_capture_waits_for_the_job(self):
        self.store.attach_run({"start": 1001.0, "seconds": 30, "result": "finished", "progress": 100.0},
                              capturing_since=1000.0)
        self.store.add(square_job(), 1000.0, 1040.0, True)
        self.assertEqual(len(self.store.list()[0]["runs"]), 1)

    def test_clear(self):
        self.store.add(square_job(), 1000.0, 1001.0, True)
        self.store.clear()
        self.assertEqual(self.store.list(), [])
        self.assertEqual(os.listdir(self.tmp), ["index.json"])


class FakeLaser:
    iface = "eth0"

    def bridge_config(self):
        return {"laser_ip": "10.0.3.3"}


class FakeNS:
    ip = "10.0.3.254"


class FakeRuida:
    magic = MAGIC
    ns = FakeNS()
    on_run_end = None
    job_started = None


class CaptureTest(StoreBase):
    def setUp(self):
        super().setUp()
        self.ruida = FakeRuida()
        self.cap = app.JobCapture(FakeLaser(), self.ruida, self.store, enabled=False)

    def feed(self, src, dst, payload, out=True):
        if out:
            self.cap.handle((src, 40200, dst, 50200, payload))
        else:
            self.cap.handle((src, 50200, dst, 40200, payload))

    def test_only_relay_traffic_to_the_controller(self):
        for p in packets(square_job()):
            self.feed("10.0.3.254", "10.0.3.3", p)  # our own status reader: ignored
            self.feed("10.0.3.1", "10.0.3.9", p)    # not the controller
            self.feed("10.0.3.1", "10.0.3.3", p)    # LightBurn's relay
            self.feed("10.0.3.3", "10.0.3.254", ACK, out=False)  # ACK to us: not the relay's
            self.feed("10.0.3.3", "10.0.3.1", ACK, out=False)
        self.assertEqual([self.store.read(j["id"]) for j in self.store.list()], [square_job()])

    def test_flows_listed_for_diagnostics(self):
        self.feed("10.0.3.1", "10.0.3.3", b"\x00\x00x")
        self.cap.handle(("10.0.3.1", 5000, "10.0.3.3", 6000, b"x"))
        st = self.cap.status()
        self.assertEqual(st["seen"]["udp"], 2)
        self.assertEqual(st["flows"], {"10.0.3.1:40200 > 10.0.3.3:50200": 1, "10.0.3.1:5000 > 10.0.3.3:6000": 1})
        self.assertEqual((st["laser_ip"], st["own_ip"]), ("10.0.3.3", "10.0.3.254"))

    def test_run_end_hook_and_running_job(self):
        self.assertEqual(self.ruida.on_run_end, self.cap._run_ended)
        data = square_job()[:-1]
        self.cap.asm.clock = lambda: 1000.0
        for p in packets(data):
            self.feed("10.0.3.1", "10.0.3.3", p)
            self.feed("10.0.3.3", "10.0.3.1", ACK, out=False)
        self.ruida.job_started = 1001.0
        self.assertEqual(self.cap.running_job(), "live")
        self.ruida.on_run_end({"start": 1001.0, "seconds": 9, "result": "finished", "progress": 100.0})
        self.cap.asm.clock = lambda: 1000.0 + app.JOB_GAP + 1
        self.cap.asm.tick()
        jid = self.store.list()[0]["id"]
        self.assertEqual(self.cap.running_job(), jid)
        self.assertEqual(self.store.list()[0]["runs"][0]["result"], "finished")


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.rec = app.PacketRecorder(clock=self.clock)

    def test_records_only_while_active_and_decodes(self):
        home = app.ruida_packet(b"\xD8\x2C", MAGIC)
        self.rec.add("out", home, MAGIC)  # not started: ignored
        self.assertEqual(self.rec.start(1), 5)  # at least 5 s
        self.clock.t += 0.5
        self.rec.add("out", home, MAGIC)
        self.rec.add("in", ACK, MAGIC)
        self.rec.add("out", app.ruida_packet(app.ruida_read_request(0x0200), MAGIC), MAGIC)
        self.rec.add("in", app.ruida_swizzle(b"\xDA\x01\x04\x00" + a(5), MAGIC), MAGIC)
        self.rec.add("out", app.ruida_packet(b"\x88" + a(1000) + a(2000) + b"\xD7", MAGIC), MAGIC)
        self.rec.add("in", NAK, MAGIC)
        st = self.rec.status()
        self.assertTrue(st["active"])
        self.assertEqual([(p["t"], p["dir"], p["cmds"]) for p in st["packets"]], [
            (0.5, "out", ["D8 2C"]),
            (0.5, "out", ["88 00 00 00 07 68 00 00 00 0F 50", "D7"]),
            (0.5, "in", ["CF"])])
        self.assertEqual((st["counts"]["polls"], st["counts"]["acks"]), (1, 1))
        self.clock.t += 10
        self.rec.add("out", home, MAGIC)
        self.assertFalse(self.rec.status()["active"])
        self.assertEqual(len(self.rec.status()["packets"]), 3)

    def test_long_packets_shortened_and_unknown_magic_kept_raw(self):
        self.rec.start(30)
        self.rec.add("out", app.ruida_packet(b"\xA9\x00\x01" * 200, MAGIC), MAGIC)
        cmds = self.rec.status()["packets"][0]["cmds"]
        self.assertEqual(cmds[-1], "... (600 bytes in all)")
        self.rec.add("out", b"\x01\x02\x03", None)
        self.assertEqual(self.rec.status()["packets"][1]["note"], "swizzled (magic not known yet)")


class CaptureSocketTest(unittest.TestCase):
    def test_listens_to_all_protocols(self):
        # A packet socket bound to IPv4 only never sees frames the Bridge sends, i.e. the
        # relay's packets to the laser. It must use ETH_P_ALL and filter IPv4 itself.
        cap = app.JobCapture(FakeLaser(), FakeRuida(), None, enabled=False)
        with mock.patch.object(app.socket, "socket") as sock:
            cap._open()
        self.assertEqual(sock.call_args[0][2], socket.htons(0x0003))
        sock.return_value.bind.assert_called_with(("eth0", 0))

    def test_replies_counted(self):
        asm = app.JobAssembler(None, lambda: MAGIC)
        asm.from_controller(ACK)
        self.assertEqual(asm.stats["replies"], 1)


def with_header(job, x0, y0, x1, y1):
    return b"\xE7\x03" + a(x0) + a(y0) + b"\xE7\x07" + a(x1) + a(y1) + job


class GeometryTest(unittest.TestCase):
    def test_absolute_job(self):
        g = app.ruida_job_geometry(with_header(square_job(), 10000, 20000, 20000, 30000))
        self.assertEqual(g["bounds"], (10000, 20000, 20000, 30000))
        self.assertTrue(g["absolute"])
        g = app.ruida_job_geometry(with_header(square_job(), 0, 0, 5000, 5000))
        self.assertFalse(g["absolute"])  # the header's box doesn't match the cuts
        line_free = square_job().split(b"\xCA\x02\x01")[0] + b"\xD7"
        g = app.ruida_job_geometry(with_header(line_free, 10000, 20000, 20000, 30000))
        self.assertTrue(g["absolute"])
        self.assertEqual(app.frame_outline(g, "box"),
                         [(10000, 20000), (10000, 30000), (20000, 30000), (20000, 20000), (10000, 20000)])
        hull = app.frame_outline(g, "outline")
        self.assertEqual(hull[0], hull[-1])
        self.assertEqual(sorted(set(hull)), [(10000, 20000), (10000, 30000), (20000, 20000), (20000, 30000)])

    def test_relative_only_or_headerless_jobs_are_not_framed(self):
        rel = b"\xA9" + r(1000) + r(0) + b"\xAB" + r(1000) + b"\xD7"  # "current position" style
        for data in (rel, with_header(rel, 0, 0, 1000, 1000), square_job()):
            g = app.ruida_job_geometry(data)
            self.assertFalse(g["absolute"])
            with self.assertRaises(app.RuidaCommandError):
                app.frame_outline(g, "box")
        with self.assertRaises(app.RuidaCommandError):
            app.frame_outline(app.ruida_job_geometry(b"\xD7"), "box")

    def test_hull(self):
        pts = [(0, 0), (10, 0), (10, 10), (0, 10), (5, 5), (5, 0)]
        self.assertEqual(app.convex_hull(pts), [(0, 0), (10, 0), (10, 10), (0, 10)])

    def test_rapid_refuses_silly_positions(self):
        for x, y in ((-1, 0), (0, app.MAX_COORD + 1)):
            with self.assertRaises(app.RuidaCommandError):
                app.ruida_rapid(x, y)

    def test_store_records_placement(self):
        tmp = tempfile.mkdtemp()
        try:
            st = app.JobStore(tmp)
            line_free = square_job().split(b"\xCA\x02\x01")[0] + b"\xD7"
            st.add(with_header(line_free, 10000, 20000, 20000, 30000), 1000.0, 1001.0, True)
            j = st.list()[0]
            self.assertEqual((j["absolute"], j["bounds"]), (True, [10000, 20000, 20000, 30000]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RuidaHookTest(unittest.TestCase):
    def test_job_end_calls_hook(self):
        ru = app.Ruida(FakeLaser(), enabled=False)
        got = []
        ru.on_run_end = got.append
        ru._job_ended({"start": 100.0, "count0": 1, "progress": 40.0}, 160.0, 1)
        self.assertEqual(got[0]["start"], 100.0)
        self.assertEqual(got[0]["result"], "stopped")


class HttpJobsTest(StoreBase):
    def setUp(self):
        super().setUp()
        cfg = app.Config(os.path.join(self.tmp, "cfg.json"))
        cam = app.Camera(cfg, b"\xff\xd8placeholder")
        self.cap = app.JobCapture(FakeLaser(), FakeRuida(), self.store, enabled=False)
        self.moves = []
        test = self

        class StubRuida:
            def frame(self, outline, who="?", label="frame"):
                test.moves.append((label, outline))
                return {"packets": len(outline) + 8, "sent": len(outline) + 8, "seconds": 3.0}

            def go_to_origin(self, who="?"):
                test.moves.append(("origin",))
                raise app.RuidaCommandError("only while the laser is idle (it is running)")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(
            cam, cfg, b"<html>", app.Updater(cfg, home=self.tmp), None, ruida=StubRuida(), jobs=self.cap))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        resp = c.getresponse()
        data = resp.read()
        c.close()
        return resp.status, data

    def test_list_download_clear(self):
        jid = self.store.add(square_job(), 1000.0, 1001.0, True)
        st, data = self.req("GET", "/api/jobs")
        j = json.loads(data.decode())
        self.assertEqual(st, 200)
        self.assertEqual([x["id"] for x in j["jobs"]], [jid])
        self.assertIsNone(j["live"])
        self.assertIsNone(j["running"])
        st, data = self.req("GET", "/api/jobs/%s.rd" % jid)
        self.assertEqual((st, data), (200, square_job()))
        self.assertEqual(self.req("GET", "/api/jobs/nope.rd")[0], 404)
        self.assertEqual(self.req("GET", "/api/jobs/live.rd")[0], 404)
        self.assertEqual(self.req("POST", "/api/jobs/clear", {"Content-Type": "text/plain"}, b"{}")[0], 403)
        self.assertEqual(len(self.store.list()), 1)
        st, _ = self.req("POST", "/api/jobs/clear", {"X-LBCam": "1", "Content-Type": "application/json"}, b"{}")
        self.assertEqual(st, 200)
        self.assertEqual(self.store.list(), [])

    def test_record_api(self):
        self.assertEqual(self.req("POST", "/api/laser/record", {"Content-Type": "text/plain"}, b"{}")[0], 403)
        self.cap.enabled = True
        st, data = self.req("POST", "/api/laser/record", {"X-LBCam": "1", "Content-Type": "application/json"},
                            b'{"seconds": 999}')
        self.assertEqual((st, json.loads(data.decode())["seconds"]), (200, 120))
        self.cap.handle(("10.0.3.1", 40200, "10.0.3.3", 50200, app.ruida_packet(b"\xD8\x2C", MAGIC)))
        j = json.loads(self.req("GET", "/api/laser/record")[1].decode())
        self.assertTrue(j["active"])
        self.assertEqual(j["packets"][0]["cmds"], ["D8 2C"])

    def post(self, path, body, extra=None):
        h = {"X-LBCam": "1", "Content-Type": "application/json"}
        h.update(extra or {})
        st, data = self.req("POST", path, h, json.dumps(body).encode())
        return st, json.loads(data.decode())

    def test_frame_api(self):
        line_free = square_job().split(b"\xCA\x02\x01")[0] + b"\xD7"
        jid = self.store.add(with_header(line_free, 10000, 20000, 20000, 30000), 1000.0, 1001.0, True)
        rel = self.store.add(b"\xA9" + r(1000) + r(0) + b"\xD7", 2000.0, 2001.0, True)
        self.assertEqual(self.req("POST", "/api/laser/frame", {"Content-Type": "text/plain"}, b"{}")[0], 403)
        st, j = self.post("/api/laser/frame", {"job": jid, "kind": "box"})
        self.assertEqual(st, 200, j)
        self.assertEqual(self.moves[-1][0], "box frame")
        self.assertEqual(len(self.moves[-1][1]), 5)
        st, j = self.post("/api/laser/frame", {"job": jid, "kind": "outline"})
        self.assertEqual((st, self.moves[-1][0]), (200, "outline frame"))
        for body in ({"job": rel}, {"job": "nope"}, {"job": jid, "kind": "circle"}):
            st, j = self.post("/api/laser/frame", body)
            self.assertEqual(st, 409, body)
            self.assertIn("Couldn't frame", j["error"])
        self.assertEqual(len(self.moves), 2)
        st, j = self.post("/api/laser/origin", {})
        self.assertEqual(st, 409)
        self.assertIn("idle", j["error"])

    def test_live(self):
        for p in packets(square_job()[:-1]):
            self.cap.asm.to_controller(p)
            self.cap.asm.from_controller(ACK)
        j = json.loads(self.req("GET", "/api/jobs")[1].decode())
        self.assertEqual(j["live"]["bytes"], len(square_job()) - 1)
        self.assertEqual(self.req("GET", "/api/jobs/live.rd"), (200, square_job()[:-1]))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class DashboardParserTest(unittest.TestCase):
    """The dashboard's parseRuida() against the same synthetic job."""

    def run_parser(self, data):
        with open(os.path.join(HERE, "..", "dashboard.html")) as f:
            src = re.search(r"/\* ruida-parser:start \*/(.*?)/\* ruida-parser:end \*/", f.read(), re.S).group(1)
        js = src + "\nconst P = parseRuida(new Uint8Array(%s));\nconsole.log(JSON.stringify(P));" % list(data)
        out = subprocess.run(["node", "-e", js], stdout=subprocess.PIPE, check=True, timeout=30).stdout
        return json.loads(out.decode())

    def test_square(self):
        P = self.run_parser(square_job())
        self.assertEqual(P["cuts"], 8)
        self.assertEqual(P["bounds"], {"minX": 10000, "minY": 20000, "maxX": 20000, "maxY": 30000})
        self.assertEqual(P["layers"], [0, 1])
        self.assertEqual(P["colors"], {"0": 0x0000FF, "1": 0xFF0000})
        self.assertTrue(P["eof"])
        segs = [P["segs"][i:i + 5] for i in range(0, len(P["segs"]), 5)]
        self.assertEqual(segs[0], [10000, 20000, 20000, 20000, 0])
        self.assertEqual(segs[5], [10000, 30000, 10000, 22000, 0])  # A9 with a negative dy
        self.assertEqual(segs[7], [11000, 21000, 13000, 21000, 1])
        self.assertAlmostEqual(P["cutLen"], 40000 + 2000)  # square + the layer-1 line

    def test_garbage_does_not_throw(self):
        P = self.run_parser(bytes(range(256)) * 4)
        self.assertIsInstance(P["cuts"], int)


if __name__ == "__main__":
    unittest.main()
