"""Laser controller check: cable, ping/ARP, relay status files (all faked, nothing sent)."""
import json
import os
import shutil
import tempfile
import unittest

from helpers import load

app = load("lbcam_stream")


class LaserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sys = os.path.join(self.tmp, "net")
        self.proc = os.path.join(self.tmp, "lbproc")
        os.makedirs(os.path.join(self.sys, "eth0"))
        os.makedirs(self.proc)
        self.bj = os.path.join(self.tmp, "bridge.json")
        with open(self.bj, "w") as f:
            json.dump({"device_type": "ruida", "laser_ip": "10.0.3.7", "ap_name": "x"}, f)
        self.laser = app.Laser(bridge_json=self.bj, proc_dir=self.proc, sys_net=self.sys)
        self.pinged = []
        self.answer = True
        self.laser.ping = lambda ip: self.pinged.append(ip) or self.answer
        self.laser.arp = lambda ip: False
        self.laser.relay_running = lambda: True
        self.cable(True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cable(self, up, speed="100"):
        for name, val in (("carrier", "1" if up else "0"), ("speed", speed if up else "-1")):
            with open(os.path.join(self.sys, "eth0", name), "w") as f:
                f.write(val + "\n")

    def status_file(self, name, level, msg):
        with open(os.path.join(self.proc, name + ".status"), "w") as f:
            f.write("%d\n%s\n" % (level, msg))

    def test_ok_uses_ip_from_bridge_json(self):
        self.status_file("relay", 2, "Ruida relay started, laser IP: 10.0.3.7")
        self.status_file("relay_details", 2, "Ruida command complete")
        r = self.laser.check()
        self.assertEqual(r["state"], "ok")
        self.assertEqual(self.pinged, ["10.0.3.7"])
        self.assertEqual(r["speed"], 100)
        self.assertEqual(r["relay"], {"level": "ok", "message": "Ruida relay started, laser IP: 10.0.3.7"})
        self.assertIsNotNone(r["last_seen"])

    def test_no_cable_skips_ping(self):
        self.cable(False)
        r = self.laser.check()
        self.assertEqual(r["state"], "no_cable")
        self.assertEqual(self.pinged, [])

    def test_no_answer(self):
        self.answer = False
        r = self.laser.check()
        self.assertEqual(r["state"], "no_answer")
        self.assertIn("10.0.3.7", r["message"])

    def test_arp_counts_when_ping_blocked(self):
        self.answer = False
        self.laser.arp = lambda ip: ip == "10.0.3.7"
        self.assertEqual(self.laser.check()["state"], "ok")

    def test_relay_down_and_error(self):
        self.laser.relay_running = lambda: False
        self.assertEqual(self.laser.check(fresh=True)["state"], "relay_down")
        self.laser.relay_running = lambda: True
        self.status_file("relay_details", 4, "Laser timeout error")
        r = self.laser.check(fresh=True)
        self.assertEqual(r["state"], "relay_error")
        self.assertIn("Laser timeout error", r["message"])

    def test_defaults_without_bridge_json_or_interface(self):
        os.unlink(self.bj)
        self.assertEqual(self.laser.bridge_config(), {"laser_ip": "10.0.3.3", "device_type": "ruida", "found": False})
        shutil.rmtree(os.path.join(self.sys, "eth0"))
        self.assertEqual(self.laser.check(fresh=True)["state"], "unknown")

    def test_results_are_cached(self):
        self.laser.check()
        self.laser.check()
        self.assertEqual(len(self.pinged), 1)
        self.laser.check(fresh=True)
        self.assertEqual(len(self.pinged), 2)

    def test_garbled_status_file(self):
        with open(os.path.join(self.proc, "relay.status"), "w") as f:
            f.write("hello there")
        self.assertEqual(self.laser.relay_status("relay"), {"level": "none", "message": "hello there"})
        self.assertIsNone(self.laser.relay_status("missing"))


if __name__ == "__main__":
    unittest.main()
