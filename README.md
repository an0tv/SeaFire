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

## Setup

### 1. Install dependencies

```bash
cd ~/Documents/SeaFire
sudo apt install ffmpeg v4l-utils ntfs-3g -y
pip install -r requirements.txt
```

### 2. USB power (Pi 5 only)

Add to `/boot/firmware/config.txt` and reboot:

```
[all]
usb_max_current_enable=1
```

Without a 5A PSU this line is ignored.  For battery deployments (IP2369 BMS)
power the Pi via GPIO pins 2/4 (+5V) and 6/9 (GND) to bypass USB-PD
negotiation, and route camera VBUS directly from the 5V rail.

### 3. SSD — auto-mount on boot

The SSD must be formatted (ext4 recommended; NTFS works but has write-cache
risks — see Troubleshooting).  Plug it in, find its device, then add to fstab:

```bash
lsblk                         # identify the SSD (e.g. /dev/sda1)
sudo blkid /dev/sda1          # check filesystem type

# For ext4:
echo '/dev/sda1 /media/rpi/SSD ext4 defaults,nofail 0 0' | sudo tee -a /etc/fstab

# For NTFS:
echo '/dev/sda1 /media/rpi/SSD ntfs-3g defaults,nofail 0 0' | sudo tee -a /etc/fstab

sudo systemctl daemon-reload
sudo mount -a                 # mount now (survives reboots)
```

If `mount -a` fails with NTFS metadata errors, the drive was unplugged
uncleanly from Windows.  Run `sudo ntfsfix /dev/sda1` then try again.

### 4. Bind cameras to left/right (stable across reboots)

USB cameras are enumerated in a different order each boot, which would swap
left and right between runs. Bind each camera to a physical USB port so the
mapping survives reboots:

```bash
# List cameras and their physical USB ports:
sudo bash setup_cameras.sh

# Cover the LEFT lens, note which /dev/videoX it is in the preview, then bind
# LEFT and RIGHT by their USB ports (values from the listing above):
sudo bash setup_cameras.sh 1-1.2 1-1.4

# Restart the service to pick up the new mapping:
sudo systemctl restart seafire
```

This writes `/etc/udev/rules.d/99-seafire-cameras.rules`, which creates
`/dev/seafire-left` and `/dev/seafire-right`. The recorder always assigns
cam0 = left and cam1 = right. Undo with `sudo bash setup_cameras.sh --undo`.

### 5. Verify

```bash
ls /dev/video*                # should see Arducam entries
ls /media/rpi/SSD             # SSD mounted
sudo python3 main.py          # start recording
# Point a browser at http://<pi-ip>:8080 for live preview
```

Output lands in `recordings_local/` as `sbs_YYYYMMDD_HHMMSS.mkv`.
Completed segments auto-transfer to `/media/rpi/SSD/seafire_recordings/`.

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

**SSD auto-mount not working after reboot** — run `sudo systemctl daemon-reload`
then `sudo mount -a`.  If the fstab line was added before daemon-reload, systemd
may have cached the old version.

**NTFS drive won't mount** — the drive was probably unplugged from Windows
without ejecting.  Run `sudo ntfsfix /dev/sda1` then `sudo mount -a`.
Reformatting to ext4 (`sudo mkfs.ext4 -L SSD /dev/sda1`) eliminates this
permanently but erases all data.

**Recordings not appearing on SSD** — the SSD may have dropped during recording.
The transfer copies with `os.sync()` to flush buffers before deleting the local
copy.  If the transfer log says "FAILED: size mismatch", the local copy is
preserved.  Check `recordings_local/` as a fallback.

**"Device or resource busy"** — stale kernel handle:

```bash
echo "2-1" | sudo tee /sys/bus/usb/drivers/usb/unbind
sleep 1
echo "2-1" | sudo tee /sys/bus/usb/drivers/usb/bind
```

**One camera at 0 FPS** — swap USB ports to isolate camera vs. port.

**Cameras show different exposure** — set `CAM_AUTO_EXPOSURE=1` (Manual) with
fixed `CAM_EXPOSURE_ABSOLUTE` so both use identical settings.
