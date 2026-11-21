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
