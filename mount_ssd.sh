#!/bin/sh
# Wait for and mount an SSD at /media/rpi/SSD.
# Handles both unmounted drives and drives already auto-mounted
# by the Pi desktop (e.g. at /media/pi/LABEL).

MOUNT_POINT=/media/rpi/SSD
mkdir -p "$MOUNT_POINT"

echo "[seafire] looking for SSD..."

while true; do

    # 1. Already mounted at our target — we're done
    if mountpoint -q "$MOUNT_POINT"; then
        echo "[seafire]   $MOUNT_POINT already mounted"
        exit 0
    fi

    # 2. Try to find an unmounted ext4/exfat/vfat/ntfs drive
    DEV=$(lsblk -nro NAME,FSTYPE,MOUNTPOINT 2>/dev/null | \
          awk '$2 ~ /^(ext4|exfat|vfat|ntfs)$/ && $3 == "" {print "/dev/"$1; exit}')
    if [ -n "$DEV" ]; then
        mount "$DEV" "$MOUNT_POINT" 2>/dev/null && \
            echo "[seafire]   mounted $DEV -> $MOUNT_POINT" && exit 0
    fi

    # 3. Try to find a drive already mounted elsewhere
    #    (desktop automounter may have mounted it at /media/pi/...)
    ALT_MOUNT=$(lsblk -nro MOUNTPOINT 2>/dev/null | \
                awk '$1 ~ /^\/media\// && $1 != "'$MOUNT_POINT'" {print $1; exit}')
    if [ -n "$ALT_MOUNT" ] && mountpoint -q "$ALT_MOUNT"; then
        mount --bind "$ALT_MOUNT" "$MOUNT_POINT" 2>/dev/null && \
            echo "[seafire]   bind-mounted $ALT_MOUNT -> $MOUNT_POINT" && exit 0
    fi

    echo "[seafire]   no drive found, retrying in 5s..."
    sleep 5
done
