#!/usr/bin/env python3
"""

Env vars:
  CAPTURE_WIDTH        Full-res width  (default 1280)
  CAPTURE_HEIGHT       Full-res height (default 720)
  CAPTURE_FPS          Capture framerate (default 30)
  RECORDINGS_DIR       Output directory (default ../recordings)
  PRE_SEC              Seconds of pre-event buffer (default 10)
  POST_SEC             Seconds to record post-event (default 30)
  DELTA_THRESHOLD      Min pixel change 0-255 for a "spike" (default 40)
  DELTA_PIXELS         Min changed-pixel count to trigger event (default 200)
  COOLDOWN_SEC         Minimum seconds between consecutive events (default 10)
  PREVIEW_PORT         HTTP port for MJPEG preview, 0=disabled (default 8080)
  DETECT_ENABLED       Set to "0" to disable event detection (default "1")

  Camera V4L2 controls (dark-field defaults):
  CAM_AUTO_EXPOSURE       0=auto, 1=manual (default 1)
  CAM_EXPOSURE_ABSOLUTE   Exposure in 100us units, 5-233016 (default 5000)
  CAM_GAIN                Analog gain 100-3000 (default 3000)
  CAM_BRIGHTNESS          -64..64 (default -10)
  CAM_CONTRAST            0..100 (default 50)
  CAM_SATURATION          0..100 (default 0)
  CAM_WHITE_BALANCE_AUTOMATIC  0=off, 1=on (default 0)
"""

import os
import signal
import sys
import time
from http.server import HTTPServer
from socket import SOL_SOCKET, SO_REUSEADDR, SO_REUSEPORT
from threading import Event, Thread
from typing import Dict, List, Optional

from camera import Camera, find_cameras
from config import (
    CAM_AUTO_EXPOSURE,
    CAM_BRIGHTNESS,
    CAM_EXPOSURE_ABSOLUTE,
    CAM_GAIN,
    CAM_SATURATION,
    CAM_WHITE_BALANCE_AUTOMATIC,
    COOLDOWN_SEC,
    DELTA_PIXELS,
    DELTA_THRESHOLD,
    DETECT_ENABLED,
    FPS,
    HEIGHT,
    POST_FRAMES,
    POST_SEC,
    PRE_FRAMES,
    PRE_SEC,
    PREVIEW_PORT,
    REC_DIR,
    WIDTH,
)
from preview import _preview_thread, _PreviewHandler
from recorder import EventRecorder


# ── FPS status thread ──────────────────────────────────────────────────────


def _fps_status(cameras: Dict[int, Camera], stop: Event):
    """Print periodic FPS for each running camera."""
    prev: Dict[int, int] = {}
    prev_time = time.monotonic()
    while not stop.is_set():
        if stop.wait(2.0):
            return
        now = time.monotonic()
        elapsed = now - prev_time
        parts = []
        for cam_id, c in list(cameras.items()):
            if cam_id not in prev:
                prev[cam_id] = 0
            if c.alive:
                delta = c.frame_count - prev[cam_id]
                fps_val = delta / elapsed if elapsed > 0 else 0
                c.last_fps = fps_val
                parts.append(f"{delta}f/{fps_val:.1f} fps")
            else:
                parts.append("DEAD")
            prev[cam_id] = c.frame_count
        prev_time = now
        print(f"[FPS] {'  |  '.join(parts)}")


# ── Shutdown helpers ───────────────────────────────────────────────────────


def _kill_ffmpeg(cameras: Dict[int, Camera]):
    for c in cameras.values():
        proc = getattr(c, "_proc", None)
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass


def _stop_cameras(cameras: Dict[int, Camera]):
    threads = []
    for c in list(cameras.values()):
        t = Thread(target=c.stop, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=3)


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    print("=== Seafire Stereo Event Recorder ===")
    print(f"Resolution: {WIDTH}x{HEIGHT} @ {FPS} fps  (gray8, raw)")
    print(f"Ring buffer: {PRE_SEC}s ({PRE_FRAMES} frames)")
    print(f"Post-event:  {POST_SEC}s ({POST_FRAMES} frames)")
    print(
        f"Detection:   delta_threshold={DELTA_THRESHOLD}, "
        f"delta_pixels={DELTA_PIXELS}, cooldown={COOLDOWN_SEC}s "
        f"({'ON' if DETECT_ENABLED else 'OFF (pipeline test mode)'})"
    )
    print(f"Output:      {REC_DIR}/")
    print(
        f"Camera ctl:  auto_exp={CAM_AUTO_EXPOSURE}, gain={CAM_GAIN}, "
        f"exp={CAM_EXPOSURE_ABSOLUTE}, bright={CAM_BRIGHTNESS}, "
        f"sat={CAM_SATURATION}, wb_auto={CAM_WHITE_BALANCE_AUTOMATIC}"
    )

    # Find cameras
    devs: List[str] = []
    for attempt in range(10):
        devs = find_cameras()
        if len(devs) >= 1:
            break
        print(f"Waiting for cameras... (attempt {attempt + 1}/10)")
        time.sleep(2)
    if not devs:
        print("No cameras found, exiting")
        sys.exit(1)
    print(f"Found {len(devs)} camera(s): {devs}")

    # Start cameras
    cameras: Dict[int, Camera] = {}
    recorder = EventRecorder(cameras)
    for i, cam_dev in enumerate(devs):
        cam = Camera(cam_dev, i, on_event=recorder.trigger)
        cameras[i] = cam
        cam.start()
        time.sleep(0.5)
    recorder._cameras = cameras

    # Preview server
    _stop = Event()
    preview_server: Optional[HTTPServer] = None
    if PREVIEW_PORT > 0:
        Thread(target=_preview_thread, args=(cameras, _stop), daemon=True).start()
        preview_server = HTTPServer(("0.0.0.0", PREVIEW_PORT), _PreviewHandler)
        # Allow immediate rebind on macOS after a crash
        preview_server.socket.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        preview_server.socket.setsockopt(SOL_SOCKET, SO_REUSEPORT, 1)
        Thread(target=preview_server.serve_forever, daemon=True).start()
        print(f"[preview] HTTP MJPEG at http://0.0.0.0:{PREVIEW_PORT}")

    print(f"{len(cameras)} camera(s) running. Ctrl+C to stop.")

    # FPS status
    Thread(target=_fps_status, args=(cameras, _stop), daemon=True).start()

    # Handle SIGTERM (used by systemd / docker)
    def _sigterm(sig, frame):
        _stop.set()

    signal.signal(signal.SIGTERM, _sigterm)

    # ── Main loop ──────────────────────────────────────────────────────────
    try:
        fail_count: Dict[int, int] = {}
        next_restart: Dict[int, float] = {}
        while not _stop.is_set():
            _stop.wait(1.0)
            now = time.monotonic()
            for cam_id, c in list(cameras.items()):
                if c.alive or _stop.is_set():
                    continue
                if now < next_restart.get(cam_id, 0):
                    continue
                fail_count[cam_id] = fail_count.get(cam_id, 0) + 1
                if fail_count[cam_id] > 5:
                    print(f"[cam{cam_id}] failed too many times, giving up")
                    del cameras[cam_id]
                else:
                    delay = min(2 ** fail_count[cam_id], 30)
                    print(f"[cam{cam_id}] restarting in {delay}s...")
                    next_restart[cam_id] = now + delay
                    time.sleep(0.5)
                    if not _stop.is_set():
                        c.start()

    except KeyboardInterrupt:
        print("\nShutting down...")
        _stop.set()
    finally:
        _kill_ffmpeg(cameras)
        _stop_cameras(cameras)
        if preview_server:
            preview_server.shutdown()
        print("Exit")


if __name__ == "__main__":
    main()
