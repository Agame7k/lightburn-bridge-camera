# Updates and rollback

What changed in each version: [CHANGELOG.md](../CHANGELOG.md).

1. Change the code and bump `VERSION` in `lbcam_stream.py`.
2. `python3 make_update.py` → `lbcam-update-<version>.tar.gz` plus
   `lbcam-update-<version>.tar.gz.sha256` (the file's SHA-256, `sha256sum` format).
3. Dashboard → **Software** → choose the file → **Install update**.
4. Optional: compare **Software → File SHA-256** with the `.sha256` file to confirm the
   Bridge got exactly the file you built (`sha256sum -c lbcam-update-*.sha256` checks your copy).

From a script, send the expected hash and the Bridge refuses the file if it doesn't match:

```
curl -X POST -H 'X-LBCam: 1' -H "X-Sha256: $(cut -d' ' -f1 lbcam-update-2.6.tar.gz.sha256)" \
     --data-binary @lbcam-update-2.6.tar.gz http://lightburnbridge.local:8080/api/update
```

What happens:

1. Checks: file list, SHA-256 checksums, version string, and the password if one is set.
2. The new version runs `--selftest` **on the Bridge** (Python version, V4L2 struct
   sizes, assets). If it fails, nothing changes.
3. It's unpacked to `/opt/lbcam/releases/<version>`, `current` is switched, and the app restarts.
4. The launcher waits up to 45 s for the new version to report itself on `/status`. If it
   crashes or doesn't answer, the launcher switches back and the dashboard shows why.
5. **Roll back** returns to the previous version at any time. The last 3 releases are kept.

Only `lbcam_stream.py`, `dashboard.html` and `placeholder.jpg` can be updated by upload.
`launcher.py` and the service change only via SSH or reflash, so an upload can't break rollback.

## Files on the Bridge

```
/opt/lbcam/launcher.py                 stable launcher (health check + rollback)
/opt/lbcam/current -> releases/<ver>   running version
/opt/lbcam/previous -> releases/<ver>  rollback target
/opt/lbcam/releases/<ver>/             lbcam_stream.py, dashboard.html, placeholder.jpg
/opt/lbcam/last_update.json            result of the last update
/etc/systemd/system/lbcam.service      starts the launcher at boot
/boot/lbcam.json                       settings (FAT partition, editable from any computer)
/boot/lbcam.env                        optional env overrides (CAM_WIDTH, CAM_FPS, CAM_PORT, …)
```

```
sudo systemctl status lbcam
sudo journalctl -u lbcam -f
```

---

[← Back to README](../README.md)
