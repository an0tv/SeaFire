"""
Seafire configuration — all constants from environment variables.
"""

import os

# ── Core capture settings ─────────────────────────────────────────────────────
WIDTH = int(os.environ.get("CAPTURE_WIDTH", 1920))
HEIGHT = int(os.environ.get("CAPTURE_HEIGHT", 1080))
FPS = int(os.environ.get("CAPTURE_FPS", 30))
REC_DIR = os.environ.get(
    "RECORDINGS_DIR", os.path.join(os.path.dirname(__file__), "..", "recordings")
)
PRE_SEC = float(os.environ.get("PRE_SEC", 10))
POST_SEC = float(os.environ.get("POST_SEC", 10))
DELTA_THRESHOLD = int(os.environ.get("DELTA_THRESHOLD", 40))
DELTA_PIXELS = int(os.environ.get("DELTA_PIXELS", 200))
COOLDOWN_SEC = float(os.environ.get("COOLDOWN_SEC", 10))
# Seconds for the running-minimum baseline to fully "forget" a dark pixel.
# A static light that turns on will stop triggering after this duration.
BASELINE_LEAK_SEC = float(os.environ.get("BASELINE_LEAK_SEC", 30))
PREVIEW_PORT = int(os.environ.get("PREVIEW_PORT", 8080))
CAMERA_INTERFACE = os.environ.get("CAMERA_INTERFACE", "usb")  # "usb" | "mipi"
DETECT_ENABLED = os.environ.get("DETECT_ENABLED", "1") != "0"
# "baseline" = running-minimum baseline (filters static light)
# "absdiff"  = frame-to-frame absolute difference (original)
DETECT_MODE = os.environ.get("DETECT_MODE", "baseline")
RECORD_CODEC = os.environ.get("RECORD_CODEC", "libx264")  # libx264 | ffv1 | h264_v4l2m2m | copy

# ── Camera V4L2 controls (applied before FFmpeg starts) ───────────────────────
CAM_AUTO_EXPOSURE = int(os.environ.get("CAM_AUTO_EXPOSURE", 0))
# Exposure in 100µs units.  300 = 30ms ~ fits 30 fps.
# Raise for sensitivity (slower fps), lower for less motion blur.
CAM_EXPOSURE_ABSOLUTE = int(os.environ.get("CAM_EXPOSURE_ABSOLUTE", 3000))
CAM_GAIN = int(os.environ.get("CAM_GAIN", 3000))
CAM_BRIGHTNESS = int(os.environ.get("CAM_BRIGHTNESS", -50))
CAM_CONTRAST = int(os.environ.get("CAM_CONTRAST", 50))
CAM_SATURATION = int(os.environ.get("CAM_SATURATION", 0))
CAM_WHITE_BALANCE_AUTOMATIC = int(os.environ.get("CAM_WHITE_BALANCE_AUTOMATIC", 0))

FRAME_BYTES = WIDTH * HEIGHT  # single-channel (gray8) rawvideo
PRE_FRAMES = int(PRE_SEC * FPS)
POST_FRAMES = int(POST_SEC * FPS)

os.makedirs(REC_DIR, exist_ok=True)
