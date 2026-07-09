"""
Live preview via MJPEG HTTP server. Grabs latest frames from cameras,
stitches side-by-side, and serves as MJPEG stream and snapshot.
"""

import io
import time
from http.server import BaseHTTPRequestHandler
from threading import Event, Lock
from typing import Dict

from camera import Camera
from config import FPS, HEIGHT, WIDTH

# ── Shared preview state ───────────────────────────────────────────────────

_latest_preview: Dict[str, bytes] = {}  # key -> JPEG bytes
_preview_lock = Lock()


def set_preview_frame(key: str, jpeg_bytes: bytes):
    with _preview_lock:
        _latest_preview[key] = jpeg_bytes


# ── HTTP handler ───────────────────────────────────────────────────────────


class _PreviewHandler(BaseHTTPRequestHandler):
    """Minimal MJPEG / snapshot HTTP server (like an IP camera)."""

    def do_GET(self):
        path = self.path.lstrip("/")
        if path in ("", "index.html"):
            self._send_index()
        elif path == "stream.mjpeg":
            self._send_mjpeg("sidebyside")
        elif path == "snapshot":
            self._send_snapshot()
        else:
            self.send_error(404)

    def _send_index(self):
        html = """<!DOCTYPE html>
<html><head><title>Seafire Preview</title>
<meta charset="utf-8"><style>
body{font-family:monospace;background:#111;color:#0f0;margin:20px}
h1{font-size:16px} a{color:#0f0}
img{max-width:100%%}
</style></head><body>
<h1>Seafire Stereo Preview</h1>
<img src="/stream.mjpeg">
<p><a href="/snapshot">Latest snapshot</a></p>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _send_mjpeg(self, key: str):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            while True:
                with _preview_lock:
                    jpeg = _latest_preview.get(key)
                if jpeg:
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                    )
                time.sleep(1.0 / max(FPS, 1))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_snapshot(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        with _preview_lock:
            jpeg = _latest_preview.get("sidebyside")
        if jpeg:
            import base64

            b64 = base64.b64encode(jpeg).decode()
            html = (
                "<html><body><h2>Stereo Snapshot</h2>"
                f"<img src='data:image/jpeg;base64,{b64}'/></body></html>"
            )
            self.wfile.write(html.encode())
        else:
            self.wfile.write(b"<html><body>No frames yet</body></html>")

    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        return  # suppress request logs


# ── Preview thread ─────────────────────────────────────────────────────────


def _preview_thread(cameras: Dict[int, Camera], stop: Event):
    """Grab latest frames from both cameras, stitch side-by-side, encode as JPEG."""
    try:
        from PIL import Image
    except ImportError:
        print("[preview] Pillow not installed, preview disabled")
        return

    pw, ph = 640, 360  # per-camera preview size (half of 640-wide pair)
    print(f"[preview] Encoding {pw * 2}x{ph} side-by-side preview")
    while not stop.is_set():
        frames = {}
        for cam_id, cam in sorted(cameras.items()):
            ring = cam.snapshot_ring()
            if not ring:
                continue
            _, raw = ring[-1]
            try:
                img = Image.frombytes("L", (WIDTH, HEIGHT), raw)
                frames[cam_id] = img.resize((pw, ph), Image.Resampling.NEAREST)
            except Exception as e:
                print(f"[preview] cam{cam_id} encode error: {e}")

        if frames:
            # Stitch side-by-side: cam0 | cam1
            paired = Image.new("L", (pw * len(frames), ph))
            for i, cam_id in enumerate(sorted(frames)):
                paired.paste(frames[cam_id], (i * pw, 0))
            buf = io.BytesIO()
            paired.save(buf, format="JPEG", quality=60)
            set_preview_frame("sidebyside", buf.getvalue())

        time.sleep(1.0 / max(FPS, 1))
