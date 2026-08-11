#!/bin/bash
# Setup Seafire as a systemd service on Raspberry Pi.
# Run this as root (sudo bash setup_service.sh).
# Assumes the seafire project is at /home/pi/seafire.

set -euo pipefail

PROJECT_DIR="/home/pi/seafire"
SERVICE_NAME="seafire"
SSD_MOUNT="/media/rpi/SSD"
SSD_UUID="${1:-}"

echo "=== Seafire systemd service setup ==="

# ── 1. SSD mount ────────────────────────────────────────────────────────────
echo ""
echo "[1/4] Setting up SSD mount at ${SSD_MOUNT}..."

if [ -z "$SSD_UUID" ]; then
    echo "No SSD UUID provided — looking for one..."
    # Try to find an ext4 or exfat USB drive
    SSD_UUID=$(lsblk -o UUID,FSTYPE,MOUNTPOINT -n 2>/dev/null | awk '$2 ~ /^(ext4|vfat|exfat)$/ && $3 == "" {print $1; exit}')
fi

if [ -z "$SSD_UUID" ]; then
    echo "WARNING: No SSD UUID found or provided."
    echo "  1. Find it:  sudo blkid"
    echo "  2. Re-run:    sudo bash setup_service.sh YOUR-UUID"
    echo "  Continuing without SSD fstab entry — the service will wait at boot."
else
    echo "SSD UUID: ${SSD_UUID}"
    mkdir -p "$SSD_MOUNT"

    if grep -q "$SSD_MOUNT" /etc/fstab 2>/dev/null; then
        echo "fstab entry already exists for ${SSD_MOUNT}"
    else
        FS_TYPE=$(blkid -o value -s TYPE "/dev/disk/by-uuid/${SSD_UUID}" 2>/dev/null || echo "ext4")
        echo "UUID=${SSD_UUID} ${SSD_MOUNT} ${FS_TYPE} defaults,nofail,noatime 0 2" >> /etc/fstab
        echo "Added fstab entry."
    fi

    # Try to mount now (won't fail if already mounted)
    mount "$SSD_MOUNT" 2>/dev/null || echo "SSD not currently plugged in — will mount on next connect."
fi

# ── 2. Install dependencies ─────────────────────────────────────────────────
echo ""
echo "[2/4] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv ffmpeg v4l-utils || true

# Python venv
if [ ! -d "${PROJECT_DIR}/venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "${PROJECT_DIR}/venv"
fi
source "${PROJECT_DIR}/venv/bin/activate"
pip install -r "${PROJECT_DIR}/requirements.txt"
deactivate

# ── 3. Install systemd service ──────────────────────────────────────────────
echo ""
echo "[3/4] Installing systemd service..."

cp "${PROJECT_DIR}/seafire.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── 4. Done ─────────────────────────────────────────────────────────────────
echo ""
echo "[4/4] Setup complete."
echo ""
echo "─── Next steps ───"
echo "  Start manually:         sudo systemctl start seafire"
echo "  Check status:            sudo systemctl status seafire"
echo "  View logs:               sudo journalctl -u seafire -f"
echo "  Stop:                    sudo systemctl stop seafire"
echo "  Disable auto-start:      sudo systemctl disable seafire"
echo ""
echo "The service will auto-start on boot once the SSD and cameras are ready."
