"""Cross-site request (CSRF) and DNS-rebinding protection of the HTTP API."""
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from helpers import load

app = load("lbcam_stream")


class HostAllowedTest(unittest.TestCase):
    def test_accepts_local_addresses(self):
        me = app.socket.gethostname()
        for h in ("192.168.1.118", "192.168.1.118:8080", "127.0.0.1", "[::1]:8080", "localhost:8080",
                  "lightburnbridge.local", "lightburnbridge.local:8080", me, me + ".local:8080", None, ""):
            self.assertTrue(app.host_allowed(h), h)

    def test_rejects_foreign_names(self):
        for h in ("evil.example", "evil.example:8080", "lightburnbridge.local.evil.example", "localhost.evil.example"):
            self.assertFalse(app.host_allowed(h), h)

    def test_extra_allowed_hosts(self):
        self.assertTrue(app.host_allowed("cam.home.arpa", extra="cam.home.arpa"))
        self.assertFalse(app.host_allowed("other.home.arpa", extra="cam.home.arpa"))


class HttpGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cfg = app.Config(os.path.join(cls.tmp, "cfg.json"))
        cls.cam = app.Camera(cfg, b"\xff\xd8placeholder")  # not started: no hardware needed
        upd = app.Updater(cfg, home=cls.tmp)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(cls.cam, cfg, b"<html>", upd, None))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_get_ok_and_no_cors(self):
        r, data = self.req("GET", "/status")
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(data.decode())["version"], app.VERSION)
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_dns_rebinding_host_rejected(self):
        r, _ = self.req("GET", "/status", {"Host": "evil.example:%d" % self.port})
        self.assertEqual(r.status, 403)

    def test_post_without_header_rejected(self):
        # what a cross-site <form> or no-cors fetch could send
        self.cam.reopen.clear()
        r, data = self.req("POST", "/api/restart", {"Content-Type": "text/plain"}, b"{}")
        self.assertEqual(r.status, 403)
        self.assertFalse(self.cam.reopen.is_set())

    def test_update_upload_without_header_rejected(self):
        r, _ = self.req("POST", "/api/update", {"Content-Type": "text/plain"}, b"x" * 100)
        self.assertEqual(r.status, 403)

    def test_post_with_foreign_origin_rejected(self):
        r, _ = self.req("POST", "/api/restart", {"X-LBCam": "1", "Origin": "http://evil.example"}, b"{}")
        self.assertEqual(r.status, 403)

    def test_post_from_dashboard_allowed(self):
        self.cam.reopen.clear()
        r, data = self.req("POST", "/api/restart",
                           {"X-LBCam": "1", "Origin": "http://127.0.0.1:%d" % self.port,
                            "Content-Type": "application/json"}, b"{}")
        self.assertEqual(r.status, 200, data)
        self.assertTrue(self.cam.reopen.is_set())

    def test_preflight_refused(self):
        r, _ = self.req("OPTIONS", "/api/restart", {"Origin": "http://evil.example",
                                                    "Access-Control-Request-Method": "POST"})
        self.assertEqual(r.status, 403)
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_snapshot_head_never_fails(self):
        r, _ = self.req("HEAD", "/snapshot.jpg")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "image/jpeg")


if __name__ == "__main__":
    unittest.main()


class StubRuida:
    """Records what the HTTP layer asks of the controller."""

    def __init__(self):
        self.calls = []

    def command(self, name, who="?"):
        self.calls.append(("command", name))

    def scan(self, start, count, who="?"):
        self.calls.append(("scan", start, count))
        return {"start": "0x%04X" % start, "count": count, "values": {}}

    def want(self):
        pass

    def status(self):
        return {"available": False}


class HttpLaserTest(unittest.TestCase):
    """Laser control and the memory scan go through the same CSRF guard as other changes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = app.Config(os.path.join(self.tmp, "cfg.json"))
        cam = app.Camera(cfg, b"\xff\xd8placeholder")
        self.upd = app.Updater(cfg, home=self.tmp)
        self.ruida = StubRuida()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                       app.make_handler(cam, cfg, b"<html>", self.upd, None, ruida=self.ruida))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    req = HttpGuardTest.req

    def post(self, path, body, headers=None):
        h = {"X-LBCam": "1", "Content-Type": "application/json"}
        h.update(headers or {})
        r, data = self.req("POST", path, h, json.dumps(body).encode())
        return r.status, json.loads(data.decode() or "{}")

    def test_scan_is_not_a_get(self):
        # a cross-site <img src=".../api/laser/scan"> must not tie up the controller
        r, _ = self.req("GET", "/api/laser/scan?start=0x200&count=512")
        self.assertNotEqual(r.status, 200)
        self.assertEqual(self.ruida.calls, [])

    def test_scan_post_needs_header(self):
        r, _ = self.req("POST", "/api/laser/scan", {"Content-Type": "text/plain"}, b"{}")
        self.assertEqual(r.status, 403)
        self.assertEqual(self.ruida.calls, [])

    def test_scan_post(self):
        status, j = self.post("/api/laser/scan", {"start": "0x210", "count": 16})
        self.assertEqual(status, 200, j)
        self.assertEqual(self.ruida.calls, [("scan", 0x210, 16)])
        status, j = self.post("/api/laser/scan", {"start": "nope"})
        self.assertEqual(status, 400)

    def test_resume_without_password_set(self):
        status, j = self.post("/api/laser/command", {"cmd": "resume"})
        self.assertEqual(status, 200, j)
        self.assertEqual(self.ruida.calls, [("command", "resume")])

    def test_resume_needs_password_when_set(self):
        self.upd.set_password("s3cret")
        status, j = self.post("/api/laser/command", {"cmd": "resume"})
        self.assertEqual(status, 403)
        self.assertIn("password", j["error"])
        status, j = self.post("/api/laser/command", {"cmd": "resume", "password": "wrong"})
        self.assertEqual(status, 403)
        self.assertEqual(self.ruida.calls, [])
        status, j = self.post("/api/laser/command", {"cmd": "resume", "password": "s3cret"})
        self.assertEqual(status, 200, j)
        self.assertEqual(self.ruida.calls, [("command", "resume")])

    def test_pause_and_stop_never_need_password(self):
        self.upd.set_password("s3cret")
        for cmd in ("pause", "stop"):
            status, j = self.post("/api/laser/command", {"cmd": cmd})
            self.assertEqual(status, 200, j)
        self.assertEqual(self.ruida.calls, [("command", "pause"), ("command", "stop")])
