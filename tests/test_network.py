"""Static IP: validation, confirm/revert, and the apply/revert race - with a fake nmcli."""
import json
import os
import shutil
import tempfile
import unittest

from helpers import load

app = load("lbcam_stream")


class FakeNmcli:
    """Answers the read queries Network makes; records write commands."""

    def __init__(self, method="auto", addr="192.168.1.118/24", gw="192.168.1.1"):
        self.method, self.addr, self.gw = method, addr, gw
        self.writes = []

    def __call__(self, args, write=False, timeout=20):
        if write:
            self.writes.append(args)
            return ""
        if args[:4] == ["-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection"]:
            return "lo:1111:loopback:lo\nMy\\:Wifi:abcd-uuid:802-11-wireless:wlan0\nLBBDirect:eeee:802-3-ethernet:eth0\n"
        if "LBBDirect" in args:
            return "ipv4.addresses:10.0.3.1/24\n"
        if args[-3:] == ["connection", "show", "abcd-uuid"]:
            return ("ipv4.method:%s\nipv4.addresses:%s\nipv4.gateway:%s\nipv4.dns:%s\n"
                    % (self.method, self.addr if self.method == "manual" else "",
                       self.gw if self.method == "manual" else "", ""))
        if args[-3:] == ["device", "show", "wlan0"]:
            return "IP4.ADDRESS[1]:%s\nIP4.GATEWAY:%s\nIP4.DNS[1]:%s\n" % (self.addr, self.gw, self.gw)
        raise AssertionError("unexpected nmcli call: %r" % (args,))


class NetworkTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        app.NET_CONFIRM_SECONDS = 300  # timers must not fire during tests
        self.net = app.Network(home=self.home)
        self.nm = FakeNmcli()
        self.net._run = self.nm
        self.net._in_use = lambda ip: ip == "192.168.1.77"

    def tearDown(self):
        self.net._clear()
        shutil.rmtree(self.home, ignore_errors=True)

    def static(self, address, **kw):
        req = {"mode": "static", "address": address, "prefix": 24, "gateway": "192.168.1.1"}
        req.update(kw)
        return self.net.apply(req)

    def assertNetError(self, msg, fn, *a, **kw):
        with self.assertRaises(app.NetError) as cm:
            fn(*a, **kw)
        self.assertIn(msg, str(cm.exception))

    def test_nmcli_escaping(self):
        self.assertEqual(app._nm_split("My\\:Wifi:abcd:802-11-wireless:wlan0"),
                         ["My:Wifi", "abcd", "802-11-wireless", "wlan0"])

    def test_info(self):
        i = self.net.info()
        self.assertTrue(i["available"])
        self.assertEqual((i["connection"], i["device"], i["method"], i["address"], i["prefix"]),
                         ("My:Wifi", "wlan0", "dhcp", "192.168.1.118", 24))

    def test_validation(self):
        self.assertNetError("Invalid address", self.static, "192.168.1.300")
        self.assertNetError("Gateway", self.static, "192.168.1.50", gateway="10.0.0.1")
        self.assertNetError("network/broadcast", self.static, "192.168.1.255")
        self.assertNetError("between /8 and /30", self.static, "192.168.1.50", prefix=31)
        self.assertNetError("laser", self.net.apply,
                            {"mode": "static", "address": "10.0.3.50", "prefix": 24, "gateway": "10.0.3.1"})
        self.assertNetError("already used", self.static, "192.168.1.77")
        self.assertNetError("mode must be", self.net.apply, {"mode": "nope"})
        self.assertEqual(self.nm.writes, [])

    def test_keeping_current_address_skips_in_use_check(self):
        self.net._in_use = lambda ip: True
        self.assertEqual(self.static("192.168.1.118")["target"], "192.168.1.118")

    def test_apply_confirm(self):
        r = self.static("192.168.1.50", dns="1.1.1.1")
        self.assertEqual(r["target"], "192.168.1.50")
        self.assertNetError("waiting for confirmation", self.static, "192.168.1.51")
        token = json.load(open(self.net.pending_path))["token"]
        self.net._apply_if_pending(token)
        modify = [w for w in self.nm.writes if w[:2] == ["connection", "modify"]][-1]
        self.assertIn("192.168.1.50/24", modify)
        self.assertIn("1.1.1.1", modify)
        self.net.confirm()
        self.assertFalse(os.path.exists(self.net.pending_path))

    def test_revert_before_apply_changes_nothing(self):
        self.static("192.168.1.50")
        token = json.load(open(self.net.pending_path))["token"]
        self.net.revert("test")
        self.net._apply_if_pending(token)  # the delayed apply fires late
        self.assertEqual(self.nm.writes, [])

    def test_expiry_restores_previous_settings(self):
        self.static("192.168.1.50")
        self.net._apply_if_pending(json.load(open(self.net.pending_path))["token"])
        self.nm.writes.clear()
        self.net._expire()
        modify = [w for w in self.nm.writes if "ipv4.method" in w][-1]
        self.assertEqual(modify[modify.index("ipv4.method") + 1], "auto")
        self.assertIn("weren't confirmed", self.net.last_error)

    def test_pending_survives_restart(self):
        self.static("192.168.1.50")
        again = app.Network(home=self.home)
        try:
            self.assertIsNotNone(again._pending())
            self.assertIsNotNone(again.timer)
        finally:
            again._clear()


if __name__ == "__main__":
    unittest.main()
