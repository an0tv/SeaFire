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

import cv2
import numpy as np

from config import (
    BASELINE_LEAK_SEC,
    CAM_AUTO_EXPOSURE,
    CAM_BRIGHTNESS,
    CAM_CONTRAST,
    CAM_EXPOSURE_ABSOLUTE,
    CAM_GAIN,
    CAM_SATURATION,
    CAM_WHITE_BALANCE_AUTOMATIC,
    COOLDOWN_SEC,
    DELTA_PIXELS,
    DELTA_THRESHOLD,
    DETECT_ENABLED,
    DETECT_MODE,
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
    ]
    args = ["v4l2-ctl", "--device", device]
    for name, val in ctrls:
        args += ["-c", f"{name}={val}"]
    try:
        subprocess.run(args, capture_output=True, timeout=5, check=True)
        print(f"[ctl] {device}: controls applied")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace").strip() if e.stderr else ""
        print(f"[ctl] {device}: some controls failed — {stderr}")
    except Exception as e:
        print(f"[ctl] {device}: v4l2-ctl error — {e}")


# ── Delta spike detector ────────────────────────────────────────────────────


def _baseline_delta(
    current_bytes: bytes,
    baseline: "np.ndarray",
    delta: int,
    width: int,
    height: int,
) -> int:
    """Count pixels where the current frame is brighter than the
    running-minimum baseline by more than *delta*.

    Only positive-going changes (brightening) are counted — this filters
    out static light sources and objects occluding light.
    """
    cur = np.frombuffer(current_bytes, dtype=np.uint8).reshape((height, width))
    # int16 so subtraction keeps negative values for cv2.threshold
    bright = cv2.subtract(cur.astype(np.int16), baseline.astype(np.int16))
    _, thresh = cv2.threshold(bright, delta, 255, cv2.THRESH_BINARY)
    return int(cv2.countNonZero(thresh))


def _baseline_update(
    current_bytes: bytes,
    baseline: "np.ndarray",
    width: int,
    height: int,
) -> None:
    """Update the running-minimum baseline in-place."""
    cur = np.frombuffer(current_bytes, dtype=np.uint8).reshape((height, width))
    cv2.min(cur, baseline, dst=baseline)


def _baseline_leak(baseline: "np.ndarray", step: int = 1) -> None:
    """Slowly raise the baseline so it forgets old dark values.

    cv2.add saturates at 255 for uint8, so no explicit clip needed.
    """
    cv2.add(baseline, step, dst=baseline)


# ── Original absdiff detector (kept for A/B comparison) ───────────────────


def _absdiff_delta(
    current: bytes,
    previous: bytes,
    delta: int,
    width: int,
    height: int,
) -> int:
    """Original frame-to-frame absolute-difference detector.

    Counts pixels where |current[i] - previous[i]| > delta.
    Triggers on ANY change — brightening, darkening, static-light flicker.
    """
    a = np.frombuffer(current, dtype=np.uint8).reshape((height, width))
    b = np.frombuffer(previous, dtype=np.uint8).reshape((height, width))
    diff = cv2.absdiff(a, b)
    _, thresh = cv2.threshold(diff, delta, 255, cv2.THRESH_BINARY)
    return int(cv2.countNonZero(thresh))


# ── Camera class ────────────────────────────────────────────────────────────


class Camera:
    """One FFmpeg process per camera. Outputs full-res gray8 rawvideo to pipe.

    Frames are fed to a ContinuousRecorder for disk archiving and kept in a
    ring buffer for live preview.  Detection events write JSON markers via
    the recorder.
    """

    def __init__(
        self,
        device: str,
        cam_id: int,
        recorder: "ContinuousRecorder",  # noqa: F821
    ):
        self.device = device
        self.cam_id = cam_id
        self._recorder = recorder
        self._proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]
        self._stop = Event()
        self._thread: Optional[Thread] = None
        self.frame_count = 0
        self.last_fps = 0.0
        # Ring buffer for preview
        self._ring: deque = deque(maxlen=PRE_FRAMES)
        # Running-minimum baseline for spike detection.
        # Initialized from the first frame; updated every frame with
        # per-pixel minimum; slowly leaked upward to forget old dark values.
        self._baseline: Optional["np.ndarray"] = None
        self._frame_index = 0
        # Leak the baseline by 1 unit every _leak_interval frames.
        # This causes a full reset from 0→255 in ~BASELINE_LEAK_SEC seconds.
        self._leak_interval = max(1, int(BASELINE_LEAK_SEC * FPS / 256))
        # Previous frame for absdiff mode
        self._prev_frame: Optional[bytes] = None
        # Detection state
        self._last_event_ns = 0

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
                    ts_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)

                    # Always feed to continuous recording
                    self._recorder.feed(self.cam_id, raw)

                    # Preview ring buffer
                    self._ring.append((ts_ns, raw))
                    self.frame_count += 1

                    # Delta-based spike detection.
                    # Mode is selected via DETECT_MODE env var:
                    #   "baseline" = running-minimum (filters static light)
                    #   "absdiff"  = frame-to-frame absolute difference
                    if DETECT_ENABLED:
                        changed = 0

                        if DETECT_MODE == "absdiff":
                            # ── Original absdiff path ────────────────────
                            if self._prev_frame is not None:
                                changed = _absdiff_delta(
                                    raw,
                                    self._prev_frame,
                                    DELTA_THRESHOLD,
                                    WIDTH,
                                    HEIGHT,
                                )
                            self._prev_frame = raw

                        elif DETECT_MODE == "baseline":
                            # ── Running-minimum baseline path ────────────
                            if self._baseline is None:
                                self._baseline = (
                                    np.frombuffer(raw, dtype=np.uint8)
                                    .reshape((HEIGHT, WIDTH))
                                    .copy()
                                )
                            else:
                                changed = _baseline_delta(
                                    raw,
                                    self._baseline,
                                    DELTA_THRESHOLD,
                                    WIDTH,
                                    HEIGHT,
                                )
                                _baseline_update(
                                    raw,
                                    self._baseline,
                                    WIDTH,
                                    HEIGHT,
                                )
                                self._frame_index += 1
                                if self._frame_index % self._leak_interval == 0:
                                    _baseline_leak(self._baseline)

                        else:
                            print(
                                f"[cam{self.cam_id}] Unknown DETECT_MODE="
                                f"{DETECT_MODE!r}, detection disabled"
                            )

                        if changed >= DELTA_PIXELS:
                            now_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
                            cooldown_ns = int(COOLDOWN_SEC * 1e9)
                            if now_ns - self._last_event_ns >= cooldown_ns:
                                self._last_event_ns = now_ns
                                print(
                                    f"[cam{self.cam_id}] \N{FIRE} EVENT  "
                                    f"delta_px={changed}  ts={now_ns / 1e9:.3f}s"
                                )
                                self._recorder.mark_detection(self.cam_id, ts_ns)
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
