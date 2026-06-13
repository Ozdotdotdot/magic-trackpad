#!/usr/bin/env bash
# Magic Music installer. Wires up driver persistence, the daemon, and the service.
#
#   sudo ./deploy/install.sh
#
# Assumes the nexustar driver (hid-magicmouse-nexustar-dkms) is already built/installed
# (see README). Re-run after editing magicmusic.py to update the installed copy.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root:  sudo $0"; exit 1; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_USER="${SUDO_USER:-$(logname)}"
U=$(id -u "$TARGET_USER"); G=$(id -g "$TARGET_USER")
echo "Installing Magic Music for ${TARGET_USER} (${U}:${G})"

# 1. driver persistence (loads the fork with host_click on boot)
install -Dm644 "$REPO_DIR/deploy/magic-music.modprobe.conf"     /etc/modprobe.d/magic-music.conf
install -Dm644 "$REPO_DIR/deploy/magic-music.modules-load.conf" /etc/modules-load.d/magic-music.conf
echo "  + driver: /etc/modprobe.d/ + /etc/modules-load.d/ drop-ins"

# 2. the daemon
install -Dm755 "$REPO_DIR/magicmusic.py" /usr/local/lib/magic-music/magicmusic.py
echo "  + daemon: /usr/local/lib/magic-music/magicmusic.py"

# 3. config (never clobber an existing one)
if [ -f /etc/magicmusic.toml ]; then
  echo "  = config: /etc/magicmusic.toml already exists, left untouched"
else
  install -Dm644 "$REPO_DIR/deploy/magicmusic.toml" /etc/magicmusic.toml
  echo "  + config: /etc/magicmusic.toml (edit to taste)"
fi

# 4. systemd service (inject the target uid/gid)
sed -e "s/__UID__/$U/" -e "s/__GID__/$G/" \
    "$REPO_DIR/deploy/magicmusic.service" > /etc/systemd/system/magicmusic.service
echo "  + service: /etc/systemd/system/magicmusic.service"

systemctl daemon-reload
systemctl enable --now magicmusic.service

echo
echo "Done. The service is enabled and running now."
echo "  check:   systemctl status magicmusic"
echo "  logs:    journalctl -u magicmusic -f"
echo "REBOOT once to confirm the driver loads with host_click automatically."
