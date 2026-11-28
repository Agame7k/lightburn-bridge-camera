# Troubleshooting

| Dashboard says | Fix |
|---|---|
| No USB camera detected | Plug in a UVC camera; try another cable/port; avoid unpowered hubs. |
| … does not support MJPEG | The camera is YUYV-only; use an MJPEG-capable camera. |
| … is in use by another program | Restart camera, or reboot the Bridge. |
| No frames from camera for 3 s | Usually power or cable. Retries automatically. |
| Paused | Nobody is watching; it wakes on the next request. Not an error. |
| Camera chose W×H | The camera doesn't support the requested size; pick one from the list. |

| Monitor says | Fix |
|---|---|
| No cable | Ethernet from the Bridge to the controller is unplugged, or the laser is off. |
| Not answering | Link is up but nothing answers at the laser IP. The controller's IP (set on the laser's panel) must match the Bridge's laser IP, 10.0.3.3 by default. |
| Relay stopped / Relay error | LightBurn's relay service. Restart the Bridge; the message shows what it reported. |
| Controller status: … | The status reader couldn't start or get replies; the text says why. |



Still stuck? Open an [issue](https://github.com/Agame7k/BridgeDeck/issues/new/choose) with the Bridge's log (the log viewer on the Setup page) and, for laser problems, a **Record laser traffic** capture.

---

[← Back to README](../README.md)
