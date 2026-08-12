#!/bin/bash
# Turn the Raspberry Pi into a WiFi access point.
# Connect to "Seafire" network, then open http://192.168.4.1:8080
# for the live preview.  Internet access is NOT shared.
#
# Supports:
#   - NetworkManager (Bookworm) — native AP mode, DHCP built in
#   - dhcpcd + hostapd      (Bullseye) — hostapd AP + dnsmasq DHCP
#   - manual IP + hostapd   (headless Bookworm)
#
# Usage:  sudo bash setup_ap.sh [SSID] [PASSWORD]
#         sudo bash setup_ap.sh --undo

set -euo pipefail

AP_IP="192.168.4.1"
INTERFACE="wlan0"

# Detect networking backend
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    BACKEND="nm"
elif systemctl is-active --quiet dhcpcd 2>/dev/null; then
    BACKEND="dhcpcd"
else
    BACKEND="manual"
fi

# ── Undo ──────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--undo" ]; then
    echo "=== Removing access point ==="
    systemctl stop hostapd dnsmasq 2>/dev/null || true
    systemctl disable hostapd dnsmasq 2>/dev/null || true
    rm -f /etc/hostapd/hostapd.conf /etc/dnsmasq.d/seafire.conf

    if [ "$BACKEND" = "nm" ]; then
        nmcli con delete seafire-ap 2>/dev/null || true
    elif [ "$BACKEND" = "dhcpcd" ]; then
        sed -i '/^interface wlan0/d; /^static ip_address=192\.168\.4/d; /^nohook wpa_supplicant/d' /etc/dhcpcd.conf 2>/dev/null || true
        systemctl restart dhcpcd 2>/dev/null || true
    else
        ip addr del "$AP_IP/24" dev "$INTERFACE" 2>/dev/null || true
    fi

    rfkill unblock wifi
    ip link set "$INTERFACE" up 2>/dev/null || true
    echo "Done. WiFi back to normal client mode."
    exit 0
fi

SSID="${1:-Seafire}"
PASSWORD="${2:-bioluminescence}"

# WPA2 requires 8-63 chars
if [ "${#PASSWORD}" -lt 8 ]; then
    echo "ERROR: password must be at least 8 characters."
    exit 1
fi

echo "=== Seafire WiFi Access Point ==="
echo "  Backend:   $BACKEND"
echo "  SSID:      $SSID"
echo "  Preview:   http://$AP_IP:8080"
echo ""

# ── 1. Install packages ──────────────────────────────────────────────────
echo "[1/3] Installing packages..."
apt-get update -qq

if [ "$BACKEND" = "nm" ]; then
    # NetworkManager shared mode provides DHCP itself — no dnsmasq needed
    :
else
    apt-get install -y -qq hostapd dnsmasq
fi

systemctl stop hostapd dnsmasq 2>/dev/null || true

# ── 2. Configure AP ──────────────────────────────────────────────────────
echo "[2/3] Configuring AP..."

if [ "$BACKEND" = "nm" ]; then
    # ── NetworkManager native AP ──────────────────────────────────────
    nmcli con delete seafire-ap 2>/dev/null || true
    nmcli con add type wifi ifname "$INTERFACE" con-name seafire-ap \
        autoconnect yes ssid "$SSID"
    nmcli con modify seafire-ap \
        connection.interface-name "$INTERFACE" \
        802-11-wireless.mode ap \
        802-11-wireless.band bg \
        802-11-wireless.channel 7 \
        ipv4.method shared ipv4.addresses "$AP_IP/24" \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASSWORD"
else
    # ── hostapd + dnsmasq ─────────────────────────────────────────────
    cat > /etc/hostapd/hostapd.conf <<EOF
interface=$INTERFACE
driver=nl80211
ssid=$SSID
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$PASSWORD
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
EOF

    sed -i 's|^#DAEMON_CONF=.*|DAEMON_CONF=/etc/hostapd/hostapd.conf|' /etc/default/hostapd 2>/dev/null || true
    if ! grep -q '^DAEMON_CONF=' /etc/default/hostapd; then
        echo 'DAEMON_CONF=/etc/hostapd/hostapd.conf' >> /etc/default/hostapd
    fi
    systemctl unmask hostapd 2>/dev/null || true
    systemctl enable hostapd

    # Static IP
    if [ "$BACKEND" = "dhcpcd" ]; then
        sed -i '/^interface wlan0/d; /^static ip_address=192\.168\.4/d; /^nohook wpa_supplicant/d' /etc/dhcpcd.conf 2>/dev/null || true
        cat >> /etc/dhcpcd.conf <<EOF
interface $INTERFACE
    static ip_address=$AP_IP/24
    nohook wpa_supplicant
EOF
    fi

    # dnsmasq DHCP
    if [ ! -f /etc/dnsmasq.conf.orig ]; then
        cp /etc/dnsmasq.conf /etc/dnsmasq.conf.orig 2>/dev/null || true
    fi
    cat > /etc/dnsmasq.d/seafire.conf <<EOF
interface=$INTERFACE
dhcp-range=192.168.4.2,192.168.4.50,255.255.255.0,24h
address=/#/$AP_IP
EOF
    systemctl enable dnsmasq
fi

# ── 3. Start ─────────────────────────────────────────────────────────────
echo "[3/3] Starting..."
rfkill unblock wifi
ip link set "$INTERFACE" up 2>/dev/null || true
sleep 1

if [ "$BACKEND" = "nm" ]; then
    nmcli con up seafire-ap
elif [ "$BACKEND" = "dhcpcd" ]; then
    systemctl restart dhcpcd
    sleep 2
    systemctl start hostapd dnsmasq
else
    ip addr add "$AP_IP/24" dev "$INTERFACE" 2>/dev/null || true
    systemctl start hostapd dnsmasq
fi

echo ""
echo "Done. The Pi is now an access point."
echo ""
echo "  Connect to WiFi:  $SSID"
echo "  Password:         $PASSWORD"
echo "  Preview:          http://$AP_IP:8080"
echo ""
echo "To undo:  sudo bash setup_ap.sh --undo"
