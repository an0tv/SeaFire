"""
Continuous recording with 15-minute segments and detection JSON markers.

All camera frames are recorded to rotating segment files.  When motion
detection fires, a JSON sidecar is written so the moment can be located in
the video later.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Dict

from config import FPS, FRAME_BYTES, HEIGHT, REC_DIR, RECORD_CODEC, WIDTH


# ── Codec lookup ────────────────────────────────────────────────────────────


def _codec_params():
    """Return (file_extension, extra_ffmpeg_args) for RECORD_CODEC.

    Mapping:
      h264_v4l2m2m → hardware H.264 via Raspberry Pi VPU (.mkv)
      ffv1         → lossless .mkv, ~5-15× compression on dark-field video
      libx264      → software H.264 .mp4 (fallback)
      copy         → raw passthrough
    """
    codecs = {
        "h264_v4l2m2m": (".mkv", ["-b:v", "5M"]),
        "ffv1": (
            ".mkv",
            ["-level", "3", "-coder", "1", "-context", "1", "-g", "1", "-slices", "24"],
        ),
        "libx264": (".mkv", ["-preset", "ultrafast", "-crf", "30"]),
        "copy": (".raw", []),
    }
    return codecs.get(RECORD_CODEC, (".mkv", []))


# ── ContinuousRecorder ──────────────────────────────────────────────────────


class ContinuousRecorder:
    """Records all camera frames continuously to 15-minute segment files.

    Each camera gets its own FFmpeg process that reads raw gray8 frames from
    stdin and writes them to rotating files under *REC_DIR*.

    On detection events, :meth:`mark_detection` writes a JSON marker that
    records the current segment file and offset so the event can be found
    in the video later.
    """

    SEGMENT_DURATION = 900     # 15 minutes in seconds
    RETRY_DELAY = 10           # seconds before retrying a failed encoder

    def __init__(self):
        self._procs: Dict[int, subprocess.Popen] = {}
        self._segment_paths: Dict[int, str] = {}
        self._segment_start: Dict[int, float] = {}  # cam_id -> time.monotonic()
        self._retry_until: Dict[int, float] = {}     # cam_id -> cooldown expiry
        self._lock = Lock()

    # ── Frame ingestion ────────────────────────────────────────────────────

    def feed(self, cam_id: int, raw: bytes):
        """Pipe one raw gray8 frame into the camera's current segment file."""
        with self._lock:
            proc = self._procs.get(cam_id)

            # If encoder died since last frame, clean up so _rotate_if_needed
            # can try to reopen (subject to retry cooldown).
            if proc and proc.poll() is not None:
                self._close(cam_id)

            self._rotate_if_needed(cam_id)
            proc = self._procs.get(cam_id)

            if proc and proc.stdin:
                try:
                    proc.stdin.write(raw)
                except (BrokenPipeError, OSError):
                    self._handle_encoder_failure(cam_id, proc)

    def _handle_encoder_failure(self, cam_id: int, proc: subprocess.Popen):
        """Log encoder stderr and enter retry cooldown."""
        err = ""
        if proc.stderr:
            try:
                err = proc.stderr.read().decode(errors="replace")[:500]
            except OSError:
                pass
        # Set cooldown BEFORE close so _rotate_if_needed respects it
        self._retry_until[cam_id] = time.monotonic() + self.RETRY_DELAY
        self._close(cam_id)
        if err:
            for line in err.strip().splitlines():
                print(f"[record] cam{cam_id} ffmpeg: {line.strip()}")
        print(
            f"[record] cam{cam_id}: encoder failed, "
            f"retrying in {self.RETRY_DELAY}s"
        )

    # ── Segment management ─────────────────────────────────────────────────

    def _rotate_if_needed(self, cam_id: int):
        now = time.monotonic()

        # Respect retry cooldown so we don't spam opens on a broken encoder
        if now < self._retry_until.get(cam_id, 0):
            return

        start = self._segment_start.get(cam_id)
        if start is None:
            self._open(cam_id)
        elif (now - start) >= self.SEGMENT_DURATION:
            self._close(cam_id)
            self._open(cam_id)

    def _open(self, cam_id: int):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        ext, codec_opts = _codec_params()
        out_path = os.path.join(REC_DIR, f"cam{cam_id}_{timestamp}{ext}")
        os.makedirs(REC_DIR, exist_ok=True)

        cmd = (
            [
                "ffmpeg",
                "-y",
                "-f",
                "rawvideo",
                "-pixel_format",
                "gray",
                "-video_size",
                f"{WIDTH}x{HEIGHT}",
                "-framerate",
                str(FPS),
                "-i",
                "pipe:0",
                "-c:v",
                RECORD_CODEC,
            ]
            + codec_opts
            + [out_path]
        )

        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self._procs[cam_id] = proc
        self._segment_paths[cam_id] = out_path
        self._segment_start[cam_id] = time.monotonic()
        print(f"[record] cam{cam_id}: started 15-min segment -> {out_path}")

    def _close(self, cam_id: int):
        proc = self._procs.pop(cam_id, None)
        if proc and proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._segment_paths.pop(cam_id, None)
        self._segment_start.pop(cam_id, None)

    # ── Detection marker ───────────────────────────────────────────────────

    def mark_detection(self, cam_id: int, ts_ns: int):
        """Write a JSON file recording a detection event.

        Args:
            cam_id: The camera that triggered.
            ts_ns:  Monotonic-raw timestamp from the triggering frame
                    (for cross-referencing, not used in offset calc).
        """
        now = datetime.now(timezone.utc)
        segment_path = self._segment_paths.get(cam_id, "unknown")
        segment_start = self._segment_start.get(cam_id, time.monotonic())
        offset_sec = round(time.monotonic() - segment_start, 3)

        detection = {
            "type": "detection",
            "camera_id": cam_id,
            "timestamp_utc": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "segment_file": os.path.basename(segment_path),
            "segment_offset_sec": offset_sec,
        }

        det_dir = os.path.join(REC_DIR, "detections")
        os.makedirs(det_dir, exist_ok=True)
        det_ts = now.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        json_path = os.path.join(det_dir, f"detection_{det_ts}_cam{cam_id}.json")
        with open(json_path, "w") as f:
            json.dump(detection, f, indent=2)
        print(f"[detect] cam{cam_id} at +{offset_sec:.1f}s -> {json_path}")

    # ── Shutdown ───────────────────────────────────────────────────────────

    def stop(self):
        """Close all segment FFmpeg processes."""
        for cam_id in list(self._procs.keys()):
            self._close(cam_id)
