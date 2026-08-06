# SeaFire — Stereo Bioluminescence Event Recorder

Low-light stereo camera system for detecting and recording bioluminescent
emissions in the field. Runs on Raspberry Pi 5 with two USB UVC cameras.

## Hardware

- Raspberry Pi 5
- 2× Arducam B0496 (USB3 2MP) — one per **separate** USB controller
- Storage: external SSD recommended (`/media/rpi/SSD`)

## Quick Start

```bash
cd ~/Documents/SeaFire
pip install -r requirements.txt

# Default settings (1080p, 15fps, baseline detection, H.264 hardware encoding)
python3 main.py

# Low-light / dark-field setup:
CAM_AUTO_EXPOSURE=1 CAM_EXPOSURE_ABSOLUTE=5000 CAM_GAIN=100 \
  CAM_BRIGHTNESS=64 CAM_CONTRAST=20 CAM_BACKLIGHT_COMPENSATION=1 \
  python3 main.py
```

## Pi 5 USB Power Fix

The Pi 5 PMIC limits USB current to ~768mA unless it detects a 5A-capable PSU via USB-PD. If cameras drop out under load, add to `/boot/firmware/config.txt`:

```
# /boot/firmware/config.txt
[all]
usb_max_current_enable=1
```

Then **reboot**:

```bash
sudo reboot
```

If the Pi still reports undervoltage (`vcgencmd get_throttled` shows `0x50000`),
you need a **5V/5A USB-PD power supply** (official Pi 5 PSU or equivalent 25W+ charger).
Without it, `usb_max_current_enable` is silently ignored.

For battery-powered field deployments with an IP2369 PMIC, **power the Pi via
GPIO pins 2/4 (+5V) and 6/9 (GND)** to bypass USB-PD negotiation entirely.
Route camera VBUS directly from the 5V rail — not through the Pi's USB ports —
so the PMIC never sees current draw on the USB bus.

## Environment Variables

### Capture

| Variable | Default | Description |
|---|---|---|
| `CAPTURE_WIDTH` | 1920 | Full-res width |
| `CAPTURE_HEIGHT` | 1080 | Full-res height |
| `CAPTURE_FPS` | 15 | Capture framerate (15 for dual-camera, 30 for single) |
| `RECORDINGS_DIR` | `../recordings` | Output directory |
| `RECORD_CODEC` | `libx264` | Encoder: `h264_v4l2m2m` (HW), `ffv1` (lossless), `libx264` |

### Detection

| Variable | Default | Description |
|---|---|---|
| `DETECT_ENABLED` | `1` | Set to `0` for pipeline test mode (no detection) |
| `DETECT_MODE` | `baseline` | `baseline` (filters static light) or `absdiff` (frame-to-frame) |
| `DELTA_THRESHOLD` | 40 | Min pixel intensity change (0–255) to count as a "spike" |
| `DELTA_PIXELS` | 200 | Min changed-pixel count to trigger an event |
| `COOLDOWN_SEC` | 10 | Minimum seconds between consecutive events |
| `BASELINE_LEAK_SEC` | 30 | How fast the baseline forgets a static light |

### Camera V4L2 Controls

| Variable | Default | Range | Description |
|---|---|---|---|
| `CAM_AUTO_EXPOSURE` | 1 | 0–3 | 0=Auto, 1=Manual |
| `CAM_EXPOSURE_ABSOLUTE` | 5000 | 5–233016 | Exposure in 100µs units (0.5s default) |
| `CAM_GAIN` | 100 | 100–3000 | Analog gain |
| `CAM_BRIGHTNESS` | 64 | -64–64 | |
| `CAM_CONTRAST` | 20 | 0–20 | |
| `CAM_SATURATION` | 0 | 0–15 | |
| `CAM_BACKLIGHT_COMPENSATION` | 1 | 0–1 | |
| `CAM_WHITE_BALANCE_AUTOMATIC` | 0 | 0–1 | |

### Preview

| Variable | Default | Description |
|---|---|---|
| `PREVIEW_PORT` | 8080 | HTTP MJPEG preview port, 0 to disable |

## Live Preview

Open `http://<pi-ip>:8080` — shows side-by-side stereo view with MJPEG stream.

## Monitoring

The capture service prints FPS every 2 seconds:

```
[FPS] 149f/14.9 fps  |  151f/15.1 fps
```

Detection events are logged to `recordings/detections/` as JSON files:

```json
{
  "type": "detection",
  "camera_id": 0,
  "timestamp_utc": "2026-08-06T21:48:52.123Z",
  "segment_file": "cam0_20260806_214852.mkv",
  "segment_offset_sec": 42.5
}
```

## Power Monitoring

```bash
python3 power.py   # shows per-rail power draw on Pi 5 PMIC
```

## Camera Setup

Lock camera settings before deployment:

```bash
# Verify cameras on separate USB buses
v4l2-ctl --list-devices | grep -B1 -A2 Arducam

# Apply locked settings
v4l2-ctl --device=/dev/video0 -c auto_exposure=1 -c exposure_time_absolute=5000 \
         -c gain=100 -c brightness=64 -c contrast=20 -c backlight_compensation=1
v4l2-ctl --device=/dev/video2 -c auto_exposure=1 -c exposure_time_absolute=5000 \
         -c gain=100 -c brightness=64 -c contrast=20 -c backlight_compensation=1
```

## Troubleshooting

**"Device or resource busy"** — Stale kernel handle after crash:
```bash
# Find and reset the stuck USB bus
echo "2-1" | sudo tee /sys/bus/usb/drivers/usb/unbind
sleep 1
echo "2-1" | sudo tee /sys/bus/usb/drivers/usb/bind
```

**Cameras show different exposures** — Set `CAM_AUTO_EXPOSURE=1` (Manual). In Auto
mode each camera meters independently and will diverge.

**USB drops under load** — See Pi 5 USB Power Fix above. If the issue persists,
use a powered USB 3.0 hub or route camera power from an external 5V rail.

**One camera refuses to stream** — Try swapping USB ports to isolate whether
it's a camera, cable, or port issue.
