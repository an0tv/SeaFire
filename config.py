"""
Seafire configuration — all constants from environment variables.
"""

import os
import sys

# ── Core capture settings ─────────────────────────────────────────────────────
WIDTH = int(os.environ.get("CAPTURE_WIDTH", 1920))
HEIGHT = int(os.environ.get("CAPTURE_HEIGHT", 1080))
FPS = int(os.environ.get("CAPTURE_FPS", 15))
REC_DIR = os.environ.get(
    "RECORDINGS_DIR", os.path.join(os.path.dirname(__file__), "recordings_local")
)
SSD_DIR = os.environ.get("SSD_RECORDINGS_DIR", "/media/rpi/SSD/seafire_recordings")

# ── Startup checks ───────────────────────────────────────────────────────────

def _check_dir(path: str, name: str):
    try:
        os.makedirs(path, exist_ok=True)
        test = os.path.join(path, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        print(f"[check] {name}: {path} — OK")
    except Exception as e:
        print(f"[check] {name}: {path} — FAILED ({e})")
        return False
    return True


def _is_mount(path: str) -> bool:
    """Check if path or its parent is a mount point (i.e. SSD actually plugged in)."""
    p = os.path.realpath(path)
    while p != "/":
        if os.path.ismount(p):
            return True
        p = os.path.dirname(p)
    return False


if not _check_dir(REC_DIR, "Local recordings"):
    print("ERROR: Cannot write to local recording directory.")
    sys.exit(1)

if not _is_mount(SSD_DIR):
    print(f"[check] SSD: {SSD_DIR} — NOT MOUNTED (recordings will stay local)")
elif not _check_dir(SSD_DIR, "SSD transfer"):
    print("WARNING: SSD directory not writable — recordings will stay local.")

PREVIEW_PORT = int(os.environ.get("PREVIEW_PORT", 8080))
RECORD_CODEC = os.environ.get("RECORD_CODEC", "libx264")
RECORD_CRF = int(os.environ.get("RECORD_CRF", 30))
SEGMENT_DUR_SEC = int(os.environ.get("SEGMENT_DURATION_SEC", 120))  # 1 hour

# ── Camera V4L2 controls (applied before FFmpeg starts) ───────────────────────
CAM_AUTO_EXPOSURE = int(os.environ.get("CAM_AUTO_EXPOSURE", 1))   # 0=Auto, 1=Manual
# Exposure in 100µs units.  5000 = 0.5s.  Only active when auto_exposure=1.
CAM_EXPOSURE_ABSOLUTE = int(os.environ.get("CAM_EXPOSURE_ABSOLUTE", 5000))
CAM_GAIN = int(os.environ.get("CAM_GAIN", 100))               # 100-3000
CAM_BRIGHTNESS = int(os.environ.get("CAM_BRIGHTNESS", 64))    # -64 to 64
CAM_CONTRAST = int(os.environ.get("CAM_CONTRAST", 20))        # 0-20
CAM_SATURATION = int(os.environ.get("CAM_SATURATION", 0))     # 0-15
CAM_WHITE_BALANCE_AUTOMATIC = int(os.environ.get("CAM_WHITE_BALANCE_AUTOMATIC", 0))
CAM_BACKLIGHT_COMPENSATION = int(os.environ.get("CAM_BACKLIGHT_COMPENSATION", 1))  # 0 or 1

FRAME_BYTES = WIDTH * HEIGHT
PRE_FRAMES = int(float(os.environ.get("PRE_SEC", 10)) * FPS)  # ring buffer for preview
