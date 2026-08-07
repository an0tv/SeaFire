"""
Stereo side-by-side recorder — stitches frames from two cameras into a single
MKV in real time.  No post-processing merge needed.

The recorder pulls the latest frame from each camera's ring buffer every
1/FPS seconds, stitches them side-by-side (3840×1080), and pipes the
combined frame to a single FFmpeg encoder.  This is the same logic the
live preview uses, but at full resolution and saved to disk.

Flow:
  cam0 ring buffer ─┐
                    ├─→ stitch_sbs() ─→ FFmpeg encoder ─→ .mkv ─→ SSD
  cam1 ring buffer ─┘
"""

import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from queue import Queue
from threading import Event, Lock, Thread
from typing import Dict, Optional

from camera import Camera
from config import FPS, HEIGHT, REC_DIR, RECORD_CODEC, RECORD_CRF, SEGMENT_DUR_SEC, SSD_DIR, WIDTH

LOCAL_DIR = REC_DIR
SEGMENT_DURATION = SEGMENT_DUR_SEC
SBS_WIDTH = WIDTH * 2
SBS_HEIGHT = HEIGHT


def _ssd_ready():
    p = os.path.realpath(SSD_DIR)
    while p != "/":
        if os.path.ismount(p):
            return True
        p = os.path.dirname(p)
    return False


def _codec_params():
    codecs = {
        "h264_v4l2m2m": (".mkv", ["-b:v", "8M"]),
        "ffv1": (".mkv", ["-level", "3", "-coder", "1", "-context", "1", "-g", "1", "-slices", "24"]),
        "libx264": (".mkv", ["-preset", "ultrafast", "-crf", str(RECORD_CRF)]),
    }
    return codecs.get(RECORD_CODEC, (".mkv", []))


def stitch_sbs(frame0: bytes, frame1: bytes) -> bytes:
    """Stitch two gray8 frames side-by-side into one 3840×1080 frame.

    Each input is WIDTH × HEIGHT bytes (gray8, one byte per pixel).
    The output interleaves rows:  [cam0_row0][cam1_row0][cam0_row1][cam1_row1]...
    """
    result = bytearray(SBS_WIDTH * SBS_HEIGHT)
    for y in range(HEIGHT):
        src_off = y * WIDTH
        dst_off = y * SBS_WIDTH
        result[dst_off : dst_off + WIDTH] = frame0[src_off : src_off + WIDTH]
        result[dst_off + WIDTH : dst_off + SBS_WIDTH] = frame1[src_off : src_off + WIDTH]
    return bytes(result)


class StereoRecorder:
    """Records side-by-side video continuously to segmented MKV files."""

    RETRY_DELAY = 10

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._segment_path: Optional[str] = None
        self._segment_start: float = 0.0
        self._retry_until: float = 0.0
        self._lock = Lock()

        self._ssd_ready = _ssd_ready()
        self._transfer_q: Queue = Queue()
        self._transfer_thread = Thread(target=self._transfer_loop, daemon=True)
        self._transfer_thread.start()

    # ── Transfer thread ─────────────────────────────────────────────────────

    def _transfer_loop(self):
        while True:
            src = self._transfer_q.get()
            if src is None:
                break
            if not os.path.exists(src) or os.path.getsize(src) < 10000:
                continue
            if not self._ssd_ready:
                continue
            dst = os.path.join(SSD_DIR, os.path.basename(src))
            try:
                size_mb = os.path.getsize(src) / 1_000_000
                print(f"[transfer] {os.path.basename(src)} ({size_mb:.0f} MB) -> SSD ...")
                shutil.move(src, dst)
                print(f"[transfer] done: {os.path.basename(dst)}")
            except Exception as e:
                print(f"[transfer] FAILED: {src} — {e}")

    # ── Rotation ────────────────────────────────────────────────────────────

    def rotate(self):
        """Close current segment, queue for transfer, open new one."""
        with self._lock:
            old_path = self._close()
            if old_path:
                self._transfer_q.put(old_path)
            self._open()

    # ── Segment open/close ──────────────────────────────────────────────────

    def _open(self):
        if time.monotonic() < self._retry_until:
            return
        if self._proc is not None:
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        ext, codec_opts = _codec_params()
        out_path = os.path.join(LOCAL_DIR, f"sbs_{ts}{ext}")

        cmd = (
            ["ffmpeg", "-y",
             "-f", "rawvideo", "-pixel_format", "gray",
             "-video_size", f"{SBS_WIDTH}x{SBS_HEIGHT}",
             "-framerate", str(FPS),
             "-i", "pipe:0",
             "-c:v", RECORD_CODEC]
            + codec_opts
            + [out_path]
        )
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self._segment_path = out_path
        self._segment_start = time.monotonic()
        print(f"[record] opened -> {os.path.basename(out_path)}")

    def _close(self) -> Optional[str]:
        proc, self._proc = self._proc, None
        if proc and proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        path, self._segment_path = self._segment_path, None
        self._segment_start = 0.0
        if path and os.path.exists(path):
            size = os.path.getsize(path) / 1_000_000
            print(f"[record] closed ({size:.0f} MB)")
        return path

    # ── Frame write ────────────────────────────────────────────────────────

    def write(self, sbs_frame: bytes):
        """Write a stitched side-by-side frame to the encoder."""
        with self._lock:
            proc = self._proc
            if proc and proc.poll() is not None:
                self._close()
                self._open()
                proc = self._proc

            if proc is None:
                self._open()
                proc = self._proc

            if proc and proc.stdin:
                try:
                    proc.stdin.write(sbs_frame)
                except (BrokenPipeError, OSError):
                    self._retry_until = time.monotonic() + self.RETRY_DELAY
                    self._close()
                    print(f"[record] encoder failed, retrying in {self.RETRY_DELAY}s")

    # ── Shutdown ───────────────────────────────────────────────────────────

    def stop(self):
        with self._lock:
            old = self._close()
            if old:
                self._transfer_q.put(old)
        self._transfer_q.put(None)
        self._transfer_thread.join(timeout=30)


def stereo_muxer_thread(cameras: Dict[int, Camera], stop: Event):
    """Continuously stitch and record side-by-side video.

    Grabs the latest frame from each camera's ring buffer every 1/FPS
    seconds, stitches them, and writes to the StereoRecorder.
    """
    recorder = StereoRecorder()
    frame_interval = 1.0 / FPS
    last_rotation = time.monotonic()

    print(f"[stereo] muxer started — {SBS_WIDTH}x{SBS_HEIGHT} @ {FPS} fps")

    while not stop.is_set():
        loop_start = time.monotonic()

        # Grab latest frame from each ring buffer
        ring0 = cameras[0].snapshot_ring()
        ring1 = cameras[1].snapshot_ring()
        if ring0 and ring1:
            _, raw0 = ring0[-1]
            _, raw1 = ring1[-1]
            sbs = stitch_sbs(raw0, raw1)
            recorder.write(sbs)

        # Segment rotation
        if loop_start - last_rotation >= SEGMENT_DURATION:
            recorder.rotate()
            last_rotation = loop_start

        # Maintain target frame rate
        elapsed = time.monotonic() - loop_start
        if elapsed < frame_interval:
            stop.wait(frame_interval - elapsed)

    recorder.stop()
    print("[stereo] muxer stopped")
