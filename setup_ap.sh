#!/bin/bash
# Turn the Raspberry Pi into a WiFi access point.
# Connect to "Seafire" network, then open http://192.168.4.1:8080
# for the live preview.  Internet access is NOT shared.
#
# Works on RPi OS Bullseye (dhcpcd) and Bookworm (NetworkManager).
#
# Run as root:  sudo bash setup_ap.sh
# To remove:    sudo bash setup_ap.sh --undo

set -euo pipefail

SSID="${SEAFIRE_SSID:-Seafire}"
PASSWORD="${SEAFIRE_PASS:-bioluminescence}"
AP_IP="192.168.4.1"
INTERFACE="wlan0"

# Detect networking backend
if systemctl list-units --all 2>/dev/null | grep -q NetworkManager; then
    BACKEND="nm"
else
    BACKEND="dhcpcd"
fi

if [ "${1:-}" = "--undo" ]; then
    echo "=== Removing access point ==="

    if [ "$BACKEND" = "nm" ]; then
        nmcli con delete seafire-ap 2>/dev/null || true
        systemctl stop dnsmasq 2>/dev/null || true
        systemctl disable dnsmasq 2>/dev/null || true
        rm -f /etc/dnsmasq.d/seafire.conf
        # Re-enable normal WiFi client
        nmcli radio wifi on
    else
        systemctl stop hostapd dnsmasq 2>/dev/null || true
        systemctl disable hostapd dnsmasq 2>/dev/null || true
        rm -f /etc/hostapd/hostapd.conf /etc/dnsmasq.d/seafire.conf
        sed -i '/^interface wlan0/d; /^static ip_address=192\.168\.4/d; /^nohook wpa_supplicant/d' /etc/dhcpcd.conf 2>/dev/null || true
        systemctl restart dhcpcd 2>/dev/null || true
    fi
    rfkill unblock wifi
    echo "Done. WiFi back to normal client mode."
    exit 0
fi

echo "=== Seafire WiFi Access Point ==="
echo "  Backend:   $BACKEND"
echo "  SSID:      $SSID"
echo "  Password:  $PASSWORD"
echo "  Preview:   http://$AP_IP:8080"
echo ""

# ── 1. Install packages ──────────────────────────────────────────────────
echo "[1/4] Installing packages..."
apt-get update -qq

if [ "$BACKEND" = "nm" ]; then
    apt-get install -y -qq dnsmasq
else
    apt-get install -y -qq hostapd dnsmasq
fi

systemctl stop hostapd dnsmasq 2>/dev/null || true
systemctl stop NetworkManager 2>/dev/null || true  # brief stop for clean config

# ── 2. WiFi AP ───────────────────────────────────────────────────────────
echo "[2/4] Configuring WiFi..."

if [ "$BACKEND" = "nm" ]; then
    # Bookworm: NetworkManager AP + dnsmasq for DHCP
    nmcli con delete seafire-ap 2>/dev/null || true
    nmcli con add type wifi ifname "$INTERFACE" con-name seafire-ap \
        autoconnect yes ssid "$SSID"
    nmcli con modify seafire-ap \
        802-11-wireless.mode ap \
        802-11-wireless.band bg \
        802-11-wireless.channel 7 \
        ipv4.method shared ipv4.addresses "$AP_IP/24" \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASSWORD"
else
    # Bullseye: hostapd for AP, dhcpcd for static IP
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

    # Static IP via dhcpcd
    sed -i '/^interface wlan0/d; /^static ip_address=192\.168\.4/d; /^nohook wpa_supplicant/d' /etc/dhcpcd.conf 2>/dev/null || true
    cat >> /etc/dhcpcd.conf <<EOF
interface $INTERFACE
    static ip_address=$AP_IP/24
    nohook wpa_supplicant
EOF
fi

# ── 3. dnsmasq (DHCP server) ─────────────────────────────────────────────
echo "[3/4] Configuring DHCP..."
if [ ! -f /etc/dnsmasq.conf.orig ]; then
    cp /etc/dnsmasq.conf /etc/dnsmasq.conf.orig 2>/dev/null || true
fi

cat > /etc/dnsmasq.d/seafire.conf <<EOF
interface=$INTERFACE
dhcp-range=192.168.4.2,192.168.4.50,255.255.255.0,24h
address=/#/$AP_IP
EOF

# On Bookworm, prevent NM from touching dnsmasq's interface
if [ "$BACKEND" = "nm" ]; then
    nmcli device set "$INTERFACE" managed no 2>/dev/null || true
fi

systemctl enable dnsmasq

# ── 4. Start ─────────────────────────────────────────────────────────────
echo "[4/4] Starting services..."
rfkill unblock wifi

if [ "$BACKEND" = "nm" ]; then
    nmcli con up seafire-ap
else
    systemctl restart dhcpcd
    sleep 2
    systemctl start hostapd
fi

systemctl start dnsmasq

echo ""
echo "Done. The Pi is now an access point."
echo ""
echo "  Connect to WiFi:  $SSID"
echo "  Password:         $PASSWORD"
echo "  Preview:          http://$AP_IP:8080"
echo ""
echo "To undo:  sudo bash setup_ap.sh --undo"
