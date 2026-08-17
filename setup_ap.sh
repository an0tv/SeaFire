#!/bin/bash
# Turn the Raspberry Pi into a WiFi access point.
# Connect to "Seafire" network, then open http://192.168.4.1:8080
# for the live preview.  Internet access is NOT shared.
#
# Uses hostapd (AP) + dnsmasq (DHCP) — reliable on all RPi OS versions.
# Works whether NetworkManager, dhcpcd, or systemd-networkd is managing WiFi.
#
# Usage:  sudo bash setup_ap.sh [SSID] [PASSWORD] [CHANNEL]
#         sudo bash setup_ap.sh --undo
#
# CHANNEL: a 2.4 GHz channel number (1-11 in the US). Defaults to 6.
#          (Auto channel selection isn't supported by the Pi's WiFi driver.)

set -euo pipefail

AP_IP="192.168.4.1"
INTERFACE="wlan0"

# Detect networking backend (only used to know how to undo/avoid conflicts)
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

    systemctl disable --now seafire-ap-ip.service 2>/dev/null || true
    rm -f /etc/systemd/system/seafire-ap-ip.service
    systemctl daemon-reload 2>/dev/null || true

    ip addr del "$AP_IP/24" dev "$INTERFACE" 2>/dev/null || true

    if [ "$BACKEND" = "nm" ]; then
        nmcli con delete seafire-ap 2>/dev/null || true
        nmcli device set "$INTERFACE" managed yes 2>/dev/null || true
        rm -f /etc/NetworkManager/conf.d/seafire-unmanaged.conf
        nmcli general reload 2>/dev/null || systemctl reload NetworkManager 2>/dev/null || true
    elif [ "$BACKEND" = "dhcpcd" ]; then
        sed -i '/^interface wlan0/d; /^static ip_address=192\.168\.4/d; /^nohook wpa_supplicant/d; /^denyinterfaces wlan0$/d' /etc/dhcpcd.conf 2>/dev/null || true
        systemctl restart dhcpcd 2>/dev/null || true
    fi

    rfkill unblock wifi
    echo "Done. WiFi back to normal client mode."
    exit 0
fi

SSID="${1:-Seafire}"
PASSWORD="${2:-bioluminescence}"
CHANNEL="${3:-6}"

case "$CHANNEL" in
    1|2|3|4|5|6|7|8|9|10|11) ;;
    *) echo "ERROR: channel must be 1-11."; exit 1 ;;
esac

if [ "${#PASSWORD}" -lt 8 ]; then
    echo "ERROR: password must be at least 8 characters."
    exit 1
fi

echo "=== Seafire WiFi Access Point ==="
echo "  SSID:      $SSID"
echo "  Preview:   http://$AP_IP:8080"
echo ""

# ── 1. Install packages ──────────────────────────────────────────────────
echo "[1/4] Installing packages..."
apt-get update -qq
apt-get install -y -qq hostapd dnsmasq
systemctl stop hostapd dnsmasq 2>/dev/null || true

# ── 2. Take wlan0 away from the network manager ──────────────────────────
echo "[2/4] Freeing $INTERFACE from network manager..."

if [ "$BACKEND" = "nm" ]; then
    nmcli device set "$INTERFACE" managed no 2>/dev/null || true
    # `nmcli device set managed no` is only temporary, so persist it here.
    # Otherwise NetworkManager grabs wlan0 on boot and races hostapd for it.
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/seafire-unmanaged.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:$INTERFACE
EOF
    nmcli general reload 2>/dev/null || systemctl reload NetworkManager 2>/dev/null || true
elif [ "$BACKEND" = "dhcpcd" ]; then
    # Stop dhcpcd from running DHCP/wpa_supplicant on the AP interface.
    sed -i '/^interface wlan0/d; /^static ip_address=192\.168\.4/d; /^nohook wpa_supplicant/d' /etc/dhcpcd.conf 2>/dev/null || true
    if ! grep -q '^denyinterfaces wlan0$' /etc/dhcpcd.conf 2>/dev/null; then
        echo "denyinterfaces $INTERFACE" >> /etc/dhcpcd.conf
    fi
    systemctl restart dhcpcd 2>/dev/null || true
fi

# ── 3. Configure hostapd (AP) + dnsmasq (DHCP) ───────────────────────────
echo "[3/4] Configuring hostapd + dnsmasq..."

cat > /etc/hostapd/hostapd.conf <<EOF
interface=$INTERFACE
driver=nl80211
ssid=$SSID
hw_mode=g
channel=$CHANNEL
country_code=US
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$PASSWORD
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF

sed -i 's|^#DAEMON_CONF=.*|DAEMON_CONF=/etc/hostapd/hostapd.conf|' /etc/default/hostapd 2>/dev/null || true
if ! grep -q '^DAEMON_CONF=' /etc/default/hostapd; then
    echo 'DAEMON_CONF=/etc/hostapd/hostapd.conf' >> /etc/default/hostapd
fi
systemctl unmask hostapd 2>/dev/null || true

if [ ! -f /etc/dnsmasq.conf.orig ]; then
    cp /etc/dnsmasq.conf /etc/dnsmasq.conf.orig 2>/dev/null || true
fi
cat > /etc/dnsmasq.d/seafire.conf <<EOF
interface=$INTERFACE
dhcp-range=192.168.4.2,192.168.4.50,255.255.255.0,24h
EOF

systemctl enable hostapd dnsmasq

# ── 4. Bring up the interface and start services ─────────────────────────
echo "[4/4] Starting..."

# A bare `ip addr add` is lost on reboot, so install a small service that
# reassigns the AP IP (and unblocks the radio) on every boot, before
# hostapd/dnsmasq start.  This is what makes the AP actually come up.
cat > /etc/systemd/system/seafire-ap-ip.service <<EOF
[Unit]
Description=Seafire access point interface
Before=hostapd.service dnsmasq.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/rfkill unblock wifi
ExecStart=/usr/sbin/ip link set $INTERFACE up
ExecStart=/usr/sbin/ip addr replace $AP_IP/24 dev $INTERFACE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now seafire-ap-ip.service

systemctl restart hostapd dnsmasq

echo ""
echo "Done. The Pi is now an access point."
echo ""
echo "  Connect to WiFi:  $SSID"
echo "  Password:         $PASSWORD"
echo "  Channel:          $CHANNEL"
echo "  Preview:          http://$AP_IP:8080"
echo ""
echo "To undo:  sudo bash setup_ap.sh --undo"
