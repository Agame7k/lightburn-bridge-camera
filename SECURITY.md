# Security

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/Agame7k/BridgeDeck/security/advisories/new)
instead. You'll get a reply as soon as possible, and credit in the changelog if you want it.

Especially interesting: anything that lets someone on the network (or a web page in their
browser) start a job, fire the laser, move the head outside the bed, or install an update
without the password.

## Threat model

- Intended for a trusted local network. The stream and camera settings are open to anyone
  on the network. Software updates and resuming a paused job can be protected with the optional
  update password (Pause and Stop never need it).
- Captured jobs (your designs) can be viewed and downloaded by anyone on the network, like
  the camera. Clear them from the monitor page, or turn capture off with `LBCAM_JOBS=0`.
- The API refuses cross-site requests and unknown host names (CSRF and DNS-rebinding
  protection), so a web page you visit can't change the Bridge's settings.
- **The stock Bridge image has SSH enabled with the default Raspberry Pi login and
  passwordless sudo.** Change it with `ssh pi@<bridge-ip> passwd`. That comes from the
  stock image, not from this project.
