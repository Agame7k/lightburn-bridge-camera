#!/bin/sh
# Install/upgrade lbcam on a running Bridge over SSH (no reflash). You'll be asked for
# the Bridge's SSH password once.
#
#   ./upgrade_over_ssh.sh <bridge-ip>             (user defaults to pi)
#   ./upgrade_over_ssh.sh <bridge-ip> pi
#
# Settings in /boot/lbcam.json are kept. After this, future updates go through the
# dashboard (Software -> Install update).
set -e
HOST="$1"; USER_="${2:-pi}"
[ -n "$HOST" ] || { echo "usage: $0 <bridge-ip> [user]"; exit 1; }
cd "$(dirname "$0")"
VER=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' lbcam_stream.py)
[ -n "$VER" ] || { echo "VERSION not found in lbcam_stream.py"; exit 1; }

remote_script() {
  echo "set -e"
  echo "VER='$VER'"
  echo "ROOT='${ROOT:-}'"
  echo 'T=$(mktemp -d)'
  echo "base64 -d > \"\$T/b.tgz\" <<'LBCAM_EOF'"
  tar czf - lbcam_stream.py dashboard.html placeholder.jpg launcher.py lbcam.service | base64
  echo "LBCAM_EOF"
  cat <<'EOS'
tar xzf "$T/b.tgz" -C "$T"
python3 "$T/lbcam_stream.py" --selftest
R="$ROOT/opt/lbcam/releases/$VER"
mkdir -p "$R"
install -m 755 "$T/lbcam_stream.py" "$R/"
install -m 644 "$T/dashboard.html" "$T/placeholder.jpg" "$R/"
install -m 755 "$T/launcher.py" "$ROOT/opt/lbcam/launcher.py"
ln -sfn "releases/$VER" "$ROOT/opt/lbcam/current"
rm -f "$ROOT/opt/lbcam/pending.json"
mkdir -p "$ROOT/etc/systemd/system"
install -m 644 "$T/lbcam.service" "$ROOT/etc/systemd/system/lbcam.service"
rm -f "$ROOT/usr/local/bin/lbcam_stream.py"
rm -rf "$ROOT/usr/local/share/lbcam"
rm -rf "$T"
if [ -z "$ROOT" ]; then
  systemctl daemon-reload
  systemctl enable lbcam >/dev/null 2>&1
  systemctl restart lbcam
  sleep 4
  systemctl is-active lbcam >/dev/null && echo "lbcam $VER installed and running" || { echo "lbcam failed to start:"; journalctl -u lbcam -n 20 --no-pager; exit 1; }
else
  echo "lbcam $VER staged into $ROOT"
fi
EOS
}

if [ -n "$ROOT" ]; then      # local dry run for testing: ROOT=/tmp/fakeroot ./upgrade_over_ssh.sh x
  remote_script | sh -s
else
  remote_script | ssh "$USER_@$HOST" 'sudo sh -s'
fi
