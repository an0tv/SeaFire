#!/bin/bash
# Setup Seafire as a systemd service on Raspberry Pi.
# Run as root:  sudo bash setup_service.sh
# Assumes the project is at /home/pi/seafire.
#
# No UUID or fstab needed — the service auto-detects and mounts
# any unmounted ext4/exfat/vfat/ntfs drive at /media/rpi/SSD on boot.

set -euo pipefail

PROJECT_DIR="/home/pi/seafire"
SERVICE_NAME="seafire"

echo "=== Seafire systemd service setup ==="

# ── 1. Mount point ──────────────────────────────────────────────────────
echo ""
echo "[1/3] Creating mount point..."
mkdir -p /media/rpi/SSD
echo "  /media/rpi/SSD created (service will auto-mount any SSD there)"

# ── 2. Dependencies ─────────────────────────────────────────────────────
echo ""
echo "[2/3] Installing dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv ffmpeg v4l-utils || true

if [ ! -d "${PROJECT_DIR}/venv" ]; then
    echo "  Creating Python venv..."
    python3 -m venv "${PROJECT_DIR}/venv"
fi
source "${PROJECT_DIR}/venv/bin/activate"
pip install -r "${PROJECT_DIR}/requirements.txt"
deactivate

# ── 3. Install & enable service ─────────────────────────────────────────
echo ""
echo "[3/3] Installing systemd service..."

cp "${PROJECT_DIR}/seafire.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo ""
echo "Done."
echo ""
echo "  Start now:      sudo systemctl start seafire"
echo "  Status:         sudo systemctl status seafire"
echo "  Logs:           sudo journalctl -u seafire -f"
echo "  Stop:           sudo systemctl stop seafire"
echo "  Disable:        sudo systemctl disable seafire"
echo ""
echo "At boot the service auto-detects any SSD and cameras, then starts."
