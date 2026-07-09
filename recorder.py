"""
Event-triggered recording: when a camera detects a spike, grab ring buffers from
ALL cameras and encode PRE_SEC + POST_SEC of frames to disk via FFmpeg.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Dict, List, Tuple

from camera import Camera
from config import (
    FPS,
    FRAME_BYTES,
    HEIGHT,
    POST_SEC,
    PRE_SEC,
    REC_DIR,
    RECORD_CODEC,
    WIDTH,
)

# ── Codec lookup ────────────────────────────────────────────────────────────


def _codec_params():
    """Return (file_extension, extra_ffmpeg_args) for RECORD_CODEC.

    Mapping:
      ffv1     → lossless .mkv, ~5–15× compression on dark-field video
      libx264  → near-lossless .mp4, ~50–200× compression on dark-field video
      copy     → raw passthrough (same as old .raw behavior)
    """
    codecs = {
        "ffv1": (
            ".mkv",
            ["-level", "3", "-coder", "1", "-context", "1", "-g", "1", "-slices", "24"],
        ),
        "libx264": (".mp4", ["-preset", "ultrafast", "-crf", "18"]),
        "copy": (".raw", []),
    }
    return codecs.get(RECORD_CODEC, (".mkv", []))


# ── EventRecorder ───────────────────────────────────────────────────────────


class EventRecorder:
    """When any camera detects a spike, grab ring buffers from ALL cameras
    and record PRE_SEC + POST_SEC of raw frames to disk.
    """

    def __init__(self, cameras: Dict[int, Camera]):
        self._cameras = cameras
        self._recording = Lock()
        self._stop = Event()

    def trigger(self, source_cam_id: int, trigger_ts_ns: int):
        """Called from Camera's detection loop. Dispatches to background thread
        so the camera thread is never blocked."""
        if not self._recording.acquire(blocking=False):
            return  # already handling an event
        t = Thread(
            target=self._record_event,
            args=(source_cam_id, trigger_ts_ns),
            daemon=True,
        )
        t.start()

    def _record_event(self, source_cam_id: int, trigger_ts_ns: int):
        try:
            self._do_record(source_cam_id, trigger_ts_ns)
        finally:
            self._recording.release()

    def _do_record(self, source_cam_id: int, trigger_ts_ns: int):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        event_dir = os.path.join(REC_DIR, f"event_{timestamp}")
        os.makedirs(event_dir, exist_ok=True)

        print(f"[record] Event triggered by cam{source_cam_id} -> {event_dir}/")

        # ── Snapshot ring buffers (pre-event frames) ────────────────────
        pre_buffers: Dict[int, List[Tuple[int, bytes]]] = {}
        for cam_id, cam in self._cameras.items():
            pre_buffers[cam_id] = cam.snapshot_ring()

        # ── Start post-event collection on all cameras ──────────────────
        for cam in self._cameras.values():
            cam.start_post_buffer()

        # Wait for the post-event window (check _stop so Ctrl+C works)
        deadline = time.monotonic() + POST_SEC
        while time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.5)

        # ── Stop post-event collection ──────────────────────────────────
        post_buffers: Dict[int, List[Tuple[int, bytes]]] = {}
        for cam_id, cam in self._cameras.items():
            post_buffers[cam_id] = cam.stop_post_buffer()

        # ── Encode frames to disk via FFmpeg ────────────────────────────
        ext, codec_opts = _codec_params()

        event_meta: dict = {
            "event_timestamp": timestamp,
            "source_camera": source_cam_id,
            "trigger_ts_ns": trigger_ts_ns,
            "config": {
                "width": WIDTH,
                "height": HEIGHT,
                "fps": FPS,
                "pix_fmt": "gray",
                "frame_bytes": FRAME_BYTES,
                "pre_sec": PRE_SEC,
                "post_sec": POST_SEC,
            },
            "cameras": {},
        }

        for cam_id in sorted(pre_buffers.keys()):
            pre = pre_buffers.get(cam_id, [])
            post = post_buffers.get(cam_id, [])
            all_frames = pre + post

            if not all_frames:
                continue

            total_mb_raw = len(all_frames) * FRAME_BYTES / 1e6
            print(
                f"[record]   cam{cam_id}: {len(pre)} pre + {len(post)} post "
                f"= {len(all_frames)} frames ({total_mb_raw:.1f} MB raw)"
            )

            # Pipe raw frames through FFmpeg for encoding
            out_path = os.path.join(event_dir, f"cam{cam_id}{ext}")
            encode_cmd = (
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

            proc = subprocess.Popen(
                encode_cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _, raw in all_frames:
                try:
                    proc.stdin.write(raw)
                except BrokenPipeError:
                    break
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass

            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

            if proc.returncode != 0:
                err = proc.stderr.read().decode(errors="replace")[-500:]
                print(f"[record]   cam{cam_id}: FFmpeg error:\n{err}")
            else:
                size_mb = os.path.getsize(out_path) / 1e6
                ratio = size_mb / total_mb_raw * 100 if total_mb_raw > 0 else 0
                print(
                    f"[record]   cam{cam_id}: encoded {size_mb:.1f} MB "
                    f"({ratio:.0f}% of raw)"
                )

            # Per-camera metadata sidecar
            cam_meta = {
                "camera_id": cam_id,
                "device": self._cameras[cam_id].device,
                "total_frames": len(all_frames),
                "pre_frames": len(pre),
                "post_frames": len(post),
                "codec": RECORD_CODEC,
            }
            meta_path = os.path.join(event_dir, f"cam{cam_id}.json")
            with open(meta_path, "w") as f:
                json.dump(cam_meta, f, indent=2)

            event_meta["cameras"][str(cam_id)] = cam_meta

        # Write event-level JSON
        event_json_path = os.path.join(event_dir, "event.json")
        with open(event_json_path, "w") as f:
            json.dump(event_meta, f, indent=2)

        print(f"[record] Event saved to {event_dir}/")
