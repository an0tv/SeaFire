#!/usr/bin/env python3
"""
Seafire — Stereo Bioluminescence Event Recorder

Usage:  python3 main.py

Env vars: see config.py for full list.
"""

import os
import signal
import sys
import time
from http.server import HTTPServer
from socket import SOL_SOCKET, SO_REUSEADDR, SO_REUSEPORT
from socketserver import ThreadingMixIn
from threading import Event, Thread
from typing import Dict, List, Optional

from camera import Camera, find_cameras
from config import (
    CAM_AUTO_EXPOSURE,
    CAM_BACKLIGHT_COMPENSATION,
    CAM_BRIGHTNESS,
    CAM_CONTRAST,
    CAM_EXPOSURE_ABSOLUTE,
    CAM_GAIN,
    CAM_SATURATION,
    CAM_WHITE_BALANCE_AUTOMATIC,
    FPS,
    HEIGHT,
    PREVIEW_PORT,
    REC_DIR,
    WIDTH,
)
from preview import _preview_thread, _PreviewHandler
from stereo_recorder import stereo_muxer_thread


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ── FPS status ──────────────────────────────────────────────────────────────

def _fps_status(cameras: Dict[int, Camera], stop: Event):
    prev: Dict[int, int] = {}
    prev_time = time.monotonic()
    while not stop.is_set():
        if stop.wait(2.0):
            return
        now = time.monotonic()
        elapsed = now - prev_time
        parts = []
        for cam_id, c in sorted(cameras.items()):
            if cam_id not in prev:
                prev[cam_id] = 0
            if c.alive:
                delta = c.frame_count - prev[cam_id]
                fps_val = delta / elapsed if elapsed > 0 else 0
                c.last_fps = fps_val
                parts.append(f"cam{cam_id}:{fps_val:.1f}")
            else:
                parts.append(f"cam{cam_id}:DEAD")
            prev[cam_id] = c.frame_count
        prev_time = now
        print(f"[FPS] {'  '.join(parts)}")


# ── Shutdown ────────────────────────────────────────────────────────────────

def _kill_camera_procs(cameras: Dict[int, Camera]):
    """Send SIGINT to camera ffmpeg processes for clean exit."""
    for c in cameras.values():
        proc = getattr(c, "_proc", None)
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass


def _stop_cameras(cameras: Dict[int, Camera], timeout: float = 5):
    threads = []
    for c in list(cameras.values()):
        t = Thread(target=c.stop, daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + timeout
    for t in threads:
        remaining = max(0.1, deadline - time.monotonic())
        t.join(timeout=remaining)


def _rescan_cameras(cameras: Dict[int, Camera]):
    try:
        devs = find_cameras()
    except Exception:
        return
    if not devs:
        return
    used = {c.device for c in cameras.values()}
    new_devs = [d for d in devs if d not in used]
    if not new_devs:
        return
    next_id = max(cameras.keys(), default=-1) + 1
    for dev in new_devs:
        cam = Camera(dev, next_id)
        cameras[next_id] = cam
        cam.start()
        print(f"[cam] re-discovered {dev} as cam{next_id}")
        next_id += 1


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("═══ Seafire ═══")
    print(f"  Resolution:   {WIDTH}x{HEIGHT} @ {FPS} fps "
          f"(side-by-side: {WIDTH * 2}x{HEIGHT})")
    print(f"  Recording:    {REC_DIR}/  (→ SSD on rotation)")
    print(f"  Cam ctl:      auto_exp={CAM_AUTO_EXPOSURE}, gain={CAM_GAIN}, "
          f"exp={CAM_EXPOSURE_ABSOLUTE}, bright={CAM_BRIGHTNESS}")
    print(f"  Preview:      http://0.0.0.0:{PREVIEW_PORT}" if PREVIEW_PORT
          else "  Preview:      disabled")

    # ── Find cameras ────────────────────────────────────────────────────────
    devs: List[str] = []
    for attempt in range(10):
        devs = find_cameras()
        if len(devs) >= 1:
            break
        print(f"  Waiting for cameras... ({attempt + 1}/10)")
        time.sleep(2)
    if not devs:
        print("  No cameras found, exiting")
        sys.exit(1)
    print(f"  Cameras:      {devs}")

    # ── Start cameras ───────────────────────────────────────────────────────
    cameras: Dict[int, Camera] = {}
    for i, cam_dev in enumerate(devs):
        cam = Camera(cam_dev, i)
        cameras[i] = cam
        cam.start()
        time.sleep(0.5)

    _stop = Event()

    # Stereo muxer — records side-by-side directly to MKV, no merge needed
    Thread(target=stereo_muxer_thread, args=(cameras, _stop), daemon=True).start()

    # Preview server
    preview_server: Optional[HTTPServer] = None
    if PREVIEW_PORT > 0:
        Thread(target=_preview_thread, args=(cameras, _stop), daemon=True).start()
        preview_server = _ThreadingHTTPServer(
            ("0.0.0.0", PREVIEW_PORT), _PreviewHandler
        )
        preview_server.socket.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        preview_server.socket.setsockopt(SOL_SOCKET, SO_REUSEPORT, 1)
        Thread(target=preview_server.serve_forever, daemon=True).start()

    print(f"{len(cameras)} camera(s) running. Ctrl+C to stop.")
    print("─" * 50)

    # FPS monitor
    Thread(target=_fps_status, args=(cameras, _stop), daemon=True).start()

    # Handlers
    def _handle_term(sig, frame):
        _stop.set()
    signal.signal(signal.SIGTERM, _handle_term)

    # ── Main loop ──────────────────────────────────────────────────────────
    fail_count: Dict[int, int] = {}
    next_restart: Dict[int, float] = {}
    last_rescan = 0.0
    RESCAN_INTERVAL = 15.0

    try:
        while not _stop.is_set():
            _stop.wait(1.0)
            now = time.monotonic()

            # Dead camera restart
            for cam_id, c in list(cameras.items()):
                if c.alive or _stop.is_set():
                    continue
                if now < next_restart.get(cam_id, 0):
                    continue
                fail_count[cam_id] = fail_count.get(cam_id, 0) + 1
                if fail_count[cam_id] > 5:
                    print(f"[cam{cam_id}] failed 5×, giving up")
                    del cameras[cam_id]
                else:
                    delay = min(2 ** fail_count[cam_id], 30)
                    print(f"[cam{cam_id}] dead — restart in {delay}s")
                    next_restart[cam_id] = now + delay
                    time.sleep(0.5)
                    if not _stop.is_set():
                        c.start()

            # Rediscover cameras after USB reset
            if now - last_rescan >= RESCAN_INTERVAL:
                last_rescan = now
                _rescan_cameras(cameras)

    except KeyboardInterrupt:
        print("\nShutting down...")
        _stop.set()
    finally:
        print("  Stopping cameras...")
        _kill_camera_procs(cameras)
        _stop_cameras(cameras, timeout=10)

        if preview_server:
            preview_server.shutdown()

        print("  Done.")


if __name__ == "__main__":
    main()
