"""
OpenCV camera capture with JPEG circular frame buffer and video clip writer.

All public functions are safe no-ops when cv2 is not installed or no camera
device is connected — the rest of the system degrades gracefully.
"""
import collections
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logger.warning("opencv-python not installed — camera module disabled")

import os

if os.getenv("VERCEL"):
    CLIP_DIR = Path("/tmp/uploads")
else:
    CLIP_DIR = Path("uploads")
CLIP_DIR.mkdir(exist_ok=True)
FPS         = int(os.getenv("CAMERA_FPS", "30"))
PRE_ROLL_S  = float(os.getenv("VIDEO_CLIP_PRE_S",  "2"))
POST_ROLL_S = float(os.getenv("VIDEO_CLIP_POST_S", "3"))
CAM_W       = int(os.getenv("CAMERA_WIDTH",  "1280"))
CAM_H       = int(os.getenv("CAMERA_HEIGHT", "720"))

# Circular buffer: stores (timestamp, jpeg_bytes) tuples.
# JPEG at quality 80 ≈ 50 KB/frame → 2 s @ 30 fps ≈ 3 MB — bounded and safe.
_buf: collections.deque = collections.deque(maxlen=int(FPS * PRE_ROLL_S))
_latest_frame  = None     # raw ndarray used by YOLO pipeline
_buf_lock      = threading.Lock()
_stop_ev       = threading.Event()
_cap           = None
_camera_active = False


def start_capture(source: int = int(os.getenv("CAMERA_INDEX_SIDE", "0"))) -> bool:
    """Open camera and start background capture thread. Returns True on success."""
    global _cap, _camera_active
    if not CV2_AVAILABLE:
        return False
    try:
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            logger.warning("Camera %s not accessible — live capture disabled", source)
            return False
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CAM_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
        cap.set(cv2.CAP_PROP_FPS, FPS)
        _cap = cap
        _camera_active = True
        t = threading.Thread(target=_capture_loop, daemon=True, name="camera-capture")
        t.start()
        logger.info("Camera %s opened at %dx%d @ %d fps", source, CAM_W, CAM_H, FPS)
        return True
    except Exception as exc:
        logger.warning("Camera init error: %s — live capture disabled", exc)
        return False


def _capture_loop():
    global _latest_frame
    while not _stop_ev.is_set():
        if _cap is None or not _cap.isOpened():
            time.sleep(0.1)
            continue
        ret, frame = _cap.read()
        if not ret:
            time.sleep(1.0 / FPS)
            continue
        ts = time.time()
        ok, enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with _buf_lock:
                _buf.append((ts, enc.tobytes()))
                _latest_frame = frame


def get_latest_frame():
    """Return the most recent raw frame (ndarray) for YOLO inference, or None."""
    with _buf_lock:
        return _latest_frame


def capture_snapshot() -> Optional[bytes]:
    """Return JPEG bytes of the latest frame, or None."""
    with _buf_lock:
        return _buf[-1][1] if _buf else None


def request_clip(event_id: int) -> str:
    """
    Write a ≈5 s video clip: PRE_ROLL_S of buffered frames + POST_ROLL_S of live frames.
    Returns the /uploads/… path string, or '' if unavailable.

    Blocks for POST_ROLL_S seconds — always call via asyncio.to_thread().
    """
    if not CV2_AVAILABLE or not _camera_active:
        return ""

    with _buf_lock:
        pre = list(_buf)  # snapshot the deque atomically

    if not pre:
        return ""

    CLIP_DIR.mkdir(exist_ok=True)
    out_name = f"clip_{event_id}_{int(time.time())}.mp4"
    out_path = CLIP_DIR / out_name

    try:
        sample = cv2.imdecode(np.frombuffer(pre[0][1], dtype=np.uint8), cv2.IMREAD_COLOR)
        h, w   = sample.shape[:2]
        writer = cv2.VideoWriter(
            str(out_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            FPS, (w, h),
        )

        for _, data in pre:
            frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            writer.write(frame)

        deadline  = time.time() + POST_ROLL_S
        last_seen: Optional[float] = None
        while time.time() < deadline and not _stop_ev.is_set():
            with _buf_lock:
                if _buf:
                    ts, data = _buf[-1]
                    if ts != last_seen:
                        last_seen = ts
                        frame = cv2.imdecode(
                            np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR
                        )
                        writer.write(frame)
            time.sleep(1.0 / FPS)

        writer.release()
        logger.info("Clip saved: %s", out_path)
        return f"/uploads/{out_name}"
    except Exception as exc:
        logger.error("Clip recording failed: %s", exc)
        return ""


def stop_capture():
    _stop_ev.set()
    if _cap:
        try:
            _cap.release()
        except Exception:
            pass
