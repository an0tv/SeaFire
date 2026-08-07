# SeaFire — Underwater Stereo Camera Recorder

Stereo video recording for prolonged underwater deployments. Two cameras are
stitched side-by-side in real time and saved directly to segmented MKV files —
no post-processing, no sync steps, no merge tools. Output is ready to watch the
moment you pull the SSD.

Runs on Raspberry Pi 5 with two USB UVC cameras.

## Hardware

- **Raspberry Pi 5**
- **2× Arducam B0496** (USB3 2MP) — one per **separate** USB controller
- **Storage:** external SSD (`/media/rpi/SSD`) — optional; recordings stay local if
  unmounted
- **Power:** 5V/5A USB-PD supply (official Pi 5 PSU or 25W+ charger)

## Quick Start

```bash
cd ~/Documents/SeaFire
pip install -r requirements.txt
sudo python3 main.py
```

Output lands in `recordings_local/` as `sbs_YYYYMMDD_HHMMSS.mkv` (3840×1080,
cam0 left, cam1 right). Completed segments auto-transfer to SSD if mounted.

## How It Works

```
cam0 ──→ ring buffer ──┐
                        ├─→ stitch ──→ encode ──→ sbs_*.mkv ──→ SSD
cam1 ──→ ring buffer ──┘
```

Both cameras feed raw frames into ring buffers. A stereo muxer thread grabs the
latest frame from each, stitches them side-by-side, and pipes the result to a
single FFmpeg encoder. Because both frames are pulled at the same clock tick,
they're inherently synced — the same logic the live preview uses, just at full
resolution and saved to disk.

Segments rotate every hour (configurable). A live 640×360 MJPEG preview is
served at `http://<pi-ip>:8080`.

## Deployment Workflow

1. **Pre-deployment** — set exposure/gain via env vars, verify cameras + storage
2. **Underwater** — `sudo python3 main.py`, runs unattended
3. **On land** — copy `sbs_*.mkv` off the SSD. Done.

## Configuration

All settings via environment variables.

### Capture

| Variable | Default | Description |
|---|---|---|
| `CAPTURE_WIDTH` | 1920 | Per-camera frame width |
| `CAPTURE_HEIGHT` | 1080 | Per-camera frame height |
| `CAPTURE_FPS` | 15 | 15 for dual-camera, 30 for single |
| `RECORDINGS_DIR` | `./recordings_local` | Local staging directory |
| `SSD_RECORDINGS_DIR` | `/media/rpi/SSD/seafire_recordings` | SSD destination |
| `SEGMENT_DURATION_SEC` | 3600 | Segment length (1 hour) |
| `RECORD_CODEC` | `libx264` | `h264_v4l2m2m` (HW), `ffv1` (lossless), `libx264` |
| `RECORD_CRF` | 30 | libx264 quality (0=lossless, 51=worst) |

### Camera V4L2 Controls

| Variable | Default | Range | Description |
|---|---|---|---|
| `CAM_AUTO_EXPOSURE` | 1 | 0–1 | 0=Auto, 1=Manual |
| `CAM_EXPOSURE_ABSOLUTE` | 5000 | 5–233016 | Exposure in 100µs units |
| `CAM_GAIN` | 100 | 100–3000 | Analog gain |
| `CAM_BRIGHTNESS` | 64 | -64–64 | |
| `CAM_CONTRAST` | 20 | 0–20 | |
| `CAM_SATURATION` | 0 | 0–15 | |
| `CAM_BACKLIGHT_COMPENSATION` | 1 | 0–1 | |
| `CAM_WHITE_BALANCE_AUTOMATIC` | 0 | 0–1 | |

### Preview

| Variable | Default | Description |
|---|---|---|
| `PREVIEW_PORT` | 8080 | MJPEG preview port; `0` to disable |

## Pi 5 USB Power

The Pi 5 PMIC limits USB current to ~768mA unless it detects a 5A PSU. Add to
`/boot/firmware/config.txt`:

```
[all]
usb_max_current_enable=1
```

Then `sudo reboot`. Without a 5A PSU this setting is ignored.

For battery deployments (IP2369 BMS): power via GPIO pins 2/4 (+5V) and 6/9
(GND) to bypass USB-PD negotiation. Route camera VBUS directly from the 5V rail.

## Power Monitoring

```bash
python3 power.py   # Pi 5 PMIC per-rail power draw
```

## Project Structure

```
SeaFire/
├── main.py              # Entry point — discovery, orchestration, preview
├── camera.py            # FFmpeg capture, V4L2 controls, ring buffer
├── stereo_recorder.py   # Side-by-side stitch + encode to MKV
├── config.py            # Environment variable configuration
├── preview.py           # Low-res MJPEG HTTP preview server
├── power.py             # Pi 5 PMIC power readout
├── requirements.txt     # Pillow
└── recordings_local/    # Default output directory
```

## Troubleshooting

**"Device or resource busy"** — stale kernel handle:

```bash
echo "2-1" | sudo tee /sys/bus/usb/drivers/usb/unbind
sleep 1
echo "2-1" | sudo tee /sys/bus/usb/drivers/usb/bind
```

**One camera at 0 FPS** — swap USB ports to isolate camera vs. port.

**SSD not detected** — `lsblk`, check `/media/rpi/SSD`. Files stay local if
unmounted.

**Cameras show different exposure** — set `CAM_AUTO_EXPOSURE=1` (Manual) with
fixed `CAM_EXPOSURE_ABSOLUTE` so both use identical settings.
