#!/bin/bash
# Turn the Raspberry Pi into a WiFi access point.
# Connect to "Seafire" network, then open http://192.168.4.1:8080
# for the live preview.  Internet access is NOT shared.
#
# Run as root:  sudo bash setup_ap.sh
# To remove:    sudo bash setup_ap.sh --undo

set -euo pipefail

SSID="${SEAFIRE_SSID:-Seafire}"
PASSWORD="${SEAFIRE_PASS:-bioluminescence}"
AP_IP="192.168.4.1"
DHCP_RANGE_START="192.168.4.2"
DHCP_RANGE_END="192.168.4.50"
INTERFACE="wlan0"

if [ "${1:-}" = "--undo" ]; then
    echo "=== Removing access point ==="

    systemctl stop hostapd dnsmasq 2>/dev/null || true
    systemctl disable hostapd dnsmasq 2>/dev/null || true
    rm -f /etc/hostapd/hostapd.conf /etc/dnsmasq.d/seafire.conf
    sed -i '/^interface wlan0/d; /^static ip_address=192\.168\.4/d; /^nohook wpa_supplicant/d' /etc/dhcpcd.conf 2>/dev/null || true
    rfkill unblock wifi
    systemctl restart dhcpcd 2>/dev/null || true
    echo "Done. WiFi back to normal client mode."
    exit 0
fi

echo "=== Seafire WiFi Access Point ==="
echo "  SSID:      $SSID"
echo "  Password:  $PASSWORD"
echo "  Preview:   http://$AP_IP:8080"
echo ""

# ── 1. Install packages ──────────────────────────────────────────────────
echo "[1/4] Installing packages..."
apt-get update -qq
apt-get install -y -qq hostapd dnsmasq

systemctl stop hostapd dnsmasq 2>/dev/null || true

# ── 2. Static IP ─────────────────────────────────────────────────────────
echo "[2/4] Setting static IP on $INTERFACE..."
# Remove old entries first
sed -i '/^interface wlan0/d; /^static ip_address=192\.168\.4/d; /^nohook wpa_supplicant/d' /etc/dhcpcd.conf 2>/dev/null || true

cat >> /etc/dhcpcd.conf <<EOF
interface $INTERFACE
    static ip_address=$AP_IP/24
    nohook wpa_supplicant
EOF

# ── 3. dnsmasq (DHCP) ────────────────────────────────────────────────────
echo "[3/4] Configuring DHCP..."

# Back up original if not already done
if [ ! -f /etc/dnsmasq.conf.orig ]; then
    cp /etc/dnsmasq.conf /etc/dnsmasq.conf.orig 2>/dev/null || true
fi

cat > /etc/dnsmasq.d/seafire.conf <<EOF
interface=$INTERFACE
dhcp-range=$DHCP_RANGE_START,$DHCP_RANGE_END,255.255.255.0,24h
# Redirect all DNS to the Pi (captive portal friendly)
address=/#/$AP_IP
EOF

# ── 4. hostapd (WiFi AP) ────────────────────────────────────────────────
echo "[4/4] Configuring WiFi AP..."

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

# Point hostapd to the config
sed -i 's|^#DAEMON_CONF=.*|DAEMON_CONF=/etc/hostapd/hostapd.conf|' /etc/default/hostapd 2>/dev/null || true
if ! grep -q '^DAEMON_CONF=' /etc/default/hostapd; then
    echo 'DAEMON_CONF=/etc/hostapd/hostapd.conf' >> /etc/default/hostapd
fi

# Unmask and enable
systemctl unmask hostapd 2>/dev/null || true
systemctl enable hostapd dnsmasq

# ── Start ────────────────────────────────────────────────────────────────
echo ""
echo "Starting services..."
rfkill unblock wifi
systemctl restart dhcpcd
sleep 2
systemctl start hostapd dnsmasq

echo ""
echo "Done. The Pi is now an access point."
echo ""
echo "  Connect to WiFi:  $SSID"
echo "  Password:         $PASSWORD"
echo "  Preview:          http://$AP_IP:8080"
echo ""
echo "To undo:  sudo bash setup_ap.sh --undo"
