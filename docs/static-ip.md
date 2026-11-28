# Static IP

Dashboard → **Network** sets the Bridge's **WiFi** address to DHCP or a static IP. The
laser's Ethernet link isn't touched.

- Input is validated: subnet, gateway inside the subnet, not the laser's subnet, and a ping
  check that the address isn't already in use.
- After **Apply** the page opens the new address. You have **90 seconds** to press
  **Keep settings** there, otherwise the previous settings are restored automatically. This
  also works across a crash or reboot.
- Choose an address outside your router's DHCP range. A DHCP reservation in the router is an
  equally good alternative.
- If the Bridge is later set up for a different WiFi network (LightBurn's `wifi.txt`), it
  goes back to DHCP.

---

[← Back to README](../README.md)
