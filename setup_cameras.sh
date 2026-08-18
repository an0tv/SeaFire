#!/bin/bash
# Bind USB cameras to physical USB ports for stable left/right ordering.
#
# Run as root:
#   sudo bash setup_cameras.sh              # list cameras + their USB ports
#   sudo bash setup_cameras.sh LEFT RIGHT   # bind left/right by USB port
#   sudo bash setup_cameras.sh --undo       # remove the binding
#
# Example:
#   sudo bash setup_cameras.sh 1-1.2 1-1.4
#
# After binding, /dev/seafire-left and /dev/seafire-right always point at
# the same physical cameras regardless of USB enumeration order, so cam0/left
# and cam1/right stay consistent across reboots.

set -euo pipefail

RULE_FILE="/etc/udev/rules.d/99-seafire-cameras.rules"

list_cameras() {
    echo "USB video cameras and their physical USB ports:"
    echo ""
    local found=0 card="" node=""
    while IFS= read -r line; do
        # Card header: a non-indented line ending in ':'.
        if printf '%s' "$line" | grep -qE '^[^[:space:]].*:$'; then
            card=$(printf '%s' "$line" | sed -E 's/ \(.*//; s/[[:space:]]*:[[:space:]]*$//')
            node=""
        elif printf '%s' "$line" | grep -qE '^[[:space:]]+/dev/video'; then
            # The first /dev/videoN under a card is the capture device
            # (any later video nodes are UVC metadata).
            if [ -z "$node" ]; then
                node=$(printf '%s' "$line" | awk '{print $1}')
                # The deepest USB device in the sysfs path is the physical
                # port. Handles root ports (2-1) and hub chains (1-1.3.2).
                # Platform video devices (codecs, CSI) have no such component
                # and are skipped. `|| true` keeps set -e from aborting when
                # grep finds no USB port.
                port=$(udevadm info -q path -n "$node" 2>/dev/null \
                       | grep -oE '[0-9]+-[0-9]+(\.[0-9]+)*' | tail -1 || true)
                if [ -n "$port" ]; then
                    printf '  %-12s  USB port: %-10s  %s\n' "$node" "$port" "${card:-unknown}"
                    found=1
                fi
            fi
        fi
    done < <(v4l2-ctl --list-devices 2>/dev/null)

    if [ "$found" -eq 0 ]; then
        echo "  (no USB video cameras detected)"
        echo ""
        echo "  Diagnostics — run these and share the output:"
        echo "    ls -l /dev/video*"
        echo "    v4l2-ctl --list-devices"
        echo "    dmesg | tail -30"
    fi
    echo ""
}

if [ "${1:-}" = "--undo" ]; then
    rm -f "$RULE_FILE"
    udevadm control --reload-rules
    udevadm trigger
    echo "Removed camera port binding."
    exit 0
fi

if [ $# -ne 2 ]; then
    list_cameras
    echo "Usage:"
    echo "  sudo bash setup_cameras.sh LEFT_PORT RIGHT_PORT"
    echo "  sudo bash setup_cameras.sh --undo"
    echo ""
    echo "Identify which camera is LEFT and which is RIGHT by covering one lens"
    echo "while watching the preview (http://<pi-ip>:8080), then re-run with the"
    echo "two USB ports shown above."
    exit 1
fi

LEFT_PORT="$1"
RIGHT_PORT="$2"

if [ "$LEFT_PORT" = "$RIGHT_PORT" ]; then
    echo "ERROR: LEFT and RIGHT must be different USB ports."
    exit 1
fi

cat > "$RULE_FILE" <<EOF
# Seafire camera USB port binding — stable left/right.
# The ID_V4L_CAPABILITIES filter keeps the symlink on the video capture
# device, not a UVC metadata node that may share the same USB port.
SUBSYSTEM=="video4linux", KERNELS=="$LEFT_PORT", ENV{ID_V4L_CAPABILITIES}=="*capture*", ENV{ID_V4L_CAPABILITIES}!="*meta*", SYMLINK+="seafire-left"
SUBSYSTEM=="video4linux", KERNELS=="$RIGHT_PORT", ENV{ID_V4L_CAPABILITIES}=="*capture*", ENV{ID_V4L_CAPABILITIES}!="*meta*", SYMLINK+="seafire-right"
EOF

udevadm control --reload-rules
udevadm trigger

echo "Wrote $RULE_FILE"
echo "  LEFT  -> USB port $LEFT_PORT  -> /dev/seafire-left"
echo "  RIGHT -> USB port $RIGHT_PORT -> /dev/seafire-right"
echo ""

# Verify the symlinks resolved (udev may take a moment).
sleep 1
for side in left right; do
    link="/dev/seafire-$side"
    if [ -L "$link" ]; then
        echo "  OK:   $link -> $(readlink -f "$link")"
    elif [ -e "$link" ]; then
        echo "  WARN: $link exists but is not a symlink"
    else
        echo "  WARN: $link not created — check the USB port value or udev/v4l_id"
    fi
done

echo ""
echo "Restart the seafire service to pick up the new mapping:"
echo "  sudo systemctl restart seafire"
