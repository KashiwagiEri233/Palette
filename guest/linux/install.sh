#!/bin/sh
# Run inside a Debian/X11 guest. No guest disk or host files are modified by Palette.
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run inside the guest with: sudo sh guest/linux/install.sh' >&2
  exit 1
fi
if [ "${1:-}" = '--uninstall' ]; then
  rm -f /etc/xdg/autostart/palette-auto-resize.desktop /etc/udev/rules.d/70-palette-desktop.rules /usr/local/lib/palette/palette-auto-resize.py
  udevadm control --reload-rules
  echo 'Palette session integration removed. Distribution packages remain installed. Log out and in.'
  exit 0
fi
source_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
apt-get update
apt-get install -y qemu-guest-agent spice-vdagent spice-webdavd x11-xserver-utils python3 gvfs-backends
install -d /usr/local/lib/palette /etc/xdg/autostart
install -m 0644 "$source_dir/palette-auto-resize.py" /usr/local/lib/palette/palette-auto-resize.py
install -m 0644 "$source_dir/palette-auto-resize.desktop" /etc/xdg/autostart/palette-auto-resize.desktop
# This port carries only a nonprivileged session-readiness heartbeat, never commands.
printf '%s\n' 'SUBSYSTEM=="virtio-ports", ATTR{name}=="org.palette.desktop.0", MODE="0666"' > /etc/udev/rules.d/70-palette-desktop.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=virtio-ports
systemctl restart qemu-guest-agent.service spice-vdagentd.service spice-webdavd.service || true
echo 'Installed. Log out and in, select virtio-gpu in Palette, then restart the VM.'
