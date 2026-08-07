"""
Camera capture via FFmpeg with ring buffer and delta spike detection.

Platform support:
  - Linux:   V4L2 input  (via ffmpeg -f v4l2)
  - macOS:   AVFoundation input (via ffmpeg -f avfoundation)
"""

import os
import platform
import re
import select
import signal
import subprocess
import time
from collections import deque
from threading import Event, Thread
from typing import Callable, List, Optional, Tuple

# ── Platform detection ────────────────────────────────────────────────────────

IS_MACOS = platform.system() == "Darwin"

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
    FRAME_BYTES,
    HEIGHT,
    PRE_FRAMES,
    WIDTH,
)

# ── Camera discovery ────────────────────────────────────────────────────────


def _find_cameras_macos() -> List[str]:
    """Find Arducam / USB camera devices via FFmpeg AVFoundation on macOS."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            timeout=10,
            text=True,
        )
        stderr = out.stderr  # ffmpeg lists devices on stderr
    except Exception as e:
        print(f"ffmpeg AVFoundation not available: {e}")
        return []

    devices: List[str] = []
    # Lines look like: "[AVFoundation indev @ 0x...] [0] FaceTime HD Camera"
    in_video_section = False
    for line in stderr.splitlines():
        if "AVFoundation video devices:" in line:
            in_video_section = True
            continue
        if "AVFoundation audio devices:" in line:
            in_video_section = False
            continue
        if in_video_section:
            m = re.search(r"\[(\d+)\]\s+(.+)", line)
            if m:
                idx = m.group(1)
                name = m.group(2)
                # Match Arducam or USB cameras (same filter as Linux version)
                if "Arducam" in name or ("USB" in name and "Camera" in name):
                    devices.append(idx)
    return devices[:2]


def _find_cameras_linux() -> List[str]:
    """Find USB camera video capture devices via v4l2-ctl."""
    try:
        out = subprocess.run(
            ["v4l2-ctl", "--list-devices"], capture_output=True, timeout=5
        ).stdout.decode(errors="replace")
    except Exception:
        print("v4l2-ctl not available")
        return []
    devices: List[str] = []
    in_usb_section = False
    for line in out.splitlines():
        stripped = line.strip()
        if "Arducam" in stripped or ("USB" in stripped and "Camera" in stripped):
            in_usb_section = True
            continue
        if in_usb_section and stripped.startswith("/dev/video"):
            dev = stripped.split()[0]
            if dev not in devices:
                devices.append(dev)
            in_usb_section = False
    return [d for d in devices if os.path.exists(d)][:2]


def find_cameras() -> List[str]:
    """Find camera devices for the current platform."""
    if IS_MACOS:
        return _find_cameras_macos()
    return _find_cameras_linux()


# ── Camera controls ──────────────────────────────────────────────────────────


def apply_camera_settings(device: str):
    """Apply camera controls.

    On Linux this uses v4l2-ctl for dark-field imaging.
    On macOS, V4L2 controls are not available — prints a notice.
    """
    if IS_MACOS:
        print(f"[ctl] {device}: camera controls not available on macOS (V4L2)")
        return

    ctrls = [
        ("auto_exposure", CAM_AUTO_EXPOSURE),
        ("exposure_time_absolute", CAM_EXPOSURE_ABSOLUTE),
        ("gain", CAM_GAIN),
        ("brightness", CAM_BRIGHTNESS),
        ("contrast", CAM_CONTRAST),
        ("saturation", CAM_SATURATION),
        ("white_balance_automatic", CAM_WHITE_BALANCE_AUTOMATIC),
        ("backlight_compensation", CAM_BACKLIGHT_COMPENSATION),
    ]
    applied = []
    failed = []
    for name, val in ctrls:
        try:
            subprocess.run(
                ["v4l2-ctl", "--device", device, "-c", f"{name}={val}"],
                capture_output=True, timeout=3, check=True,
            )
            applied.append(name)
        except subprocess.CalledProcessError:
            failed.append(name)
    if applied:
        print(f"[ctl] {device}: applied {applied}")
    if failed:
        print(f"[ctl] {device}: skipped unsupported {failed}")


# ── Camera class ────────────────────────────────────────────────────────────


class Camera:
    """One FFmpeg process per camera. Outputs full-res gray8 rawvideo to pipe.

    Frames are kept in a ring buffer for live preview and stereo recording.
    The stereo muxer thread pulls from this buffer.
    """

    def __init__(
        self,
        device: str,
        cam_id: int,
    ):
        self.device = device
        self.cam_id = cam_id
        self._proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]
        self._stop = Event()
        self._thread: Optional[Thread] = None
        self.frame_count = 0
        self.last_fps = 0.0
        self._ring: deque = deque(maxlen=PRE_FRAMES)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.alive:
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def snapshot_ring(self) -> List[Tuple[int, bytes]]:
        """Return a copy of the ring buffer (latest frames)."""
        return list(self._ring)

    def _run(self):
        # Get camera label for logging
        label = self.device
        if IS_MACOS:
            # On macOS we don't have v4l2-ctl; just use the index as label
            print(f"[cam{self.cam_id}] AVFoundation device index {self.device}")
        else:
            try:
                cam_label = subprocess.check_output(
                    ["v4l2-ctl", "--device", self.device, "--all"],
                    stderr=subprocess.DEVNULL,
                ).decode(errors="replace")
                for line in cam_label.splitlines():
                    if "Card" in line:
                        label = line.strip()
            except Exception:
                pass
            print(f"[cam{self.cam_id}] {self.device}: {label}")

        # Apply camera controls
        apply_camera_settings(self.device)

        # FFmpeg: camera → full-res gray8 rawvideo → pipe
        if IS_MACOS:
            # macOS: AVFoundation input
            cmd = [
                "ffmpeg",
                "-f",
                "avfoundation",
                "-video_size",
                f"{WIDTH}x{HEIGHT}",
                "-framerate",
                str(FPS),
                "-i",
                self.device,
                "-vf",
                "format=gray",
                "-c:v",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-f",
                "rawvideo",
                "pipe:1",
            ]
        else:
            # Linux: V4L2 input
            cmd = [
                "ffmpeg",
                "-f",
                "v4l2",
                "-video_size",
                f"{WIDTH}x{HEIGHT}",
                "-framerate",
                str(FPS),
                "-i",
                self.device,
                "-vf",
                "format=gray",
                "-c:v",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-f",
                "rawvideo",
                "pipe:1",
            ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )

        buf = bytearray()
        try:
            while not self._stop.is_set():
                # select with 0.5s timeout so we check _stop frequently.
                # Under load, select returns as soon as data is ready.
                ready, _, _ = select.select([self._proc.stdout], [], [], 0.5)
                if not ready:
                    continue
                chunk = self._proc.stdout.read(FRAME_BYTES)
                if not chunk:
                    break
                buf.extend(chunk)
                # Drain any remaining buffered data into buf until we have
                # at least one full frame.
                while len(buf) >= FRAME_BYTES:
                    raw = bytes(buf[:FRAME_BYTES])
                    del buf[:FRAME_BYTES]

                    # Feed to ring buffer (stereo muxer pulls from here)
                    ts_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
                    self._ring.append((ts_ns, raw))
                    self.frame_count += 1
        finally:
            # Kill process group (select loop already exited cleanly)
            if self._proc and self._proc.poll() is None:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            # Drain stderr non-blocking via communicate
            if self._proc:
                try:
                    _, stderr = self._proc.communicate(timeout=1)
                    if stderr:
                        for line in (
                            stderr.decode(errors="replace").strip().splitlines()[-3:]
                        ):
                            print(f"[cam{self.cam_id} ffmpeg] {line.strip()}")
                except Exception:
                    pass

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
