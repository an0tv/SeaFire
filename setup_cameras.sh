#!/bin/bash
# Bind Arducam cameras to physical USB ports for stable left/right ordering.
#
# Run as root:
#   sudo bash setup_cameras.sh              # list cameras + their USB ports
#   sudo bash setup_cameras.sh LEFT RIGHT   # bind left/right by USB port
#
# Example:
#   sudo bash setup_cameras.sh 1-1.2 1-1.4
#
# After binding, /dev/seafire-left and /dev/seafire-right always point at
# the same physical cameras regardless of USB enumeration order.

set -euo pipefail

RULE_FILE="/etc/udev/rules.d/99-seafire-cameras.rules"

list_cameras() {
    echo "Arducam cameras and their physical USB ports:"
    echo ""
    local found=0
    for d in /dev/video*; do
        [ -e "$d" ] || continue
        info=$(v4l2-ctl -d "$d" --all 2>/dev/null || true)
        if echo "$info" | grep -qi 'Arducam'; then
            # Physical USB port (e.g. 1-1.2) — the multi-level bus path
            port=$(udevadm info -q path -n "$d" 2>/dev/null \
                   | grep -oE '[0-9]+-[0-9]+(\.[0-9]+)+' | head -1)
            card=$(echo "$info" | grep -i 'Card type' | sed 's/.*: //')
            printf '  %-12s  USB port: %-10s  %s\n' "$d" "${port:-unknown}" "$card"
            found=1
        fi
    done
    [ "$found" -eq 1 ] || echo "  (no Arducam cameras detected)"
    echo ""
}

if [ "${1:-}" = "--undo" ]; then
    rm -f "$RULE_FILE"
    udevadm control --reload-rules
    udevadm trigger
    echo "Removed camera port binding."
    exit 0
fi

if [ $# -eq 2 ]; then
    LEFT_PORT="$1"
    RIGHT_PORT="$2"
else
    list_cameras
    echo "Usage:"
    echo "  sudo bash setup_cameras.sh LEFT_PORT RIGHT_PORT"
    echo ""
    echo "Identify which camera is LEFT and which is RIGHT by covering one lens"
    echo "while watching the preview, then re-run with the two USB ports."
    echo "The port is the value in the 'USB port' column above."
    exit 1
fi

cat > "$RULE_FILE" <<EOF
# Seafire camera USB port binding — stable left/right
SUBSYSTEM=="video4linux", KERNELS=="$LEFT_PORT", SYMLINK+="seafire-left"
SUBSYSTEM=="video4linux", KERNELS=="$RIGHT_PORT", SYMLINK+="seafire-right"
EOF

udevadm control --reload-rules
udevadm trigger

echo "Wrote $RULE_FILE"
echo "  LEFT  -> USB port $LEFT_PORT  -> /dev/seafire-left"
echo "  RIGHT -> USB port $RIGHT_PORT -> /dev/seafire-right"
echo ""
echo "Restart the seafire service to pick up the new mapping:"
echo "  sudo systemctl restart seafire"
