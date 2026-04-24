"""
IR / LiDAR depth sensor abstraction.

Uses Intel RealSense (pyrealsense2) when available;
falls back to a threaded random-walk simulation that slowly
drifts a runner toward and away from the finish line.
"""
import logging
import os
import random
import threading
import time

logger = logging.getLogger(__name__)

try:
    import pyrealsense2 as rs  # type: ignore
    _RS_AVAILABLE = True
except ImportError:
    _RS_AVAILABLE = False

DEPTH_THRESHOLD_MM = float(os.getenv("DEPTH_THRESHOLD_MM", "1000"))


class DepthSensor:
    """Thread-safe depth sensor with RealSense or simulation backend."""

    def __init__(self, threshold_mm: float = DEPTH_THRESHOLD_MM):
        self.threshold_mm = threshold_mm
        self.is_real      = False
        self._pipe        = None
        self._sim_depth   = random.uniform(1200, 2000)
        self._lock        = threading.Lock()
        self._stop        = threading.Event()

        if _RS_AVAILABLE:
            try:
                self._pipe = rs.pipeline()
                cfg = rs.config()
                cfg.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
                self._pipe.start(cfg)
                self.is_real = True
                logger.info("Intel RealSense depth sensor initialised (threshold=%.0f mm)", threshold_mm)
            except Exception as exc:
                logger.warning("RealSense init failed (%s) — falling back to simulation", exc)
                self._pipe = None
        else:
            logger.warning(
                "pyrealsense2 not installed — depth sensor simulated. "
                "Install with: pip install pyrealsense2"
            )

        if not self.is_real:
            t = threading.Thread(target=self._sim_loop, daemon=True, name="depth-sim")
            t.start()

    def _sim_loop(self):
        while not self._stop.is_set():
            with self._lock:
                self._sim_depth = max(
                    200.0,
                    self._sim_depth + random.uniform(-60, 90),
                )
                if self._sim_depth > 2500:
                    self._sim_depth = random.uniform(1200, 2000)
            time.sleep(0.1)

    def read_mm(self) -> float:
        """Return current depth in millimetres. Returns -1.0 on sensor error."""
        if self.is_real and self._pipe:
            try:
                frames = self._pipe.wait_for_frames(timeout_ms=150)
                depth  = frames.get_depth_frame()
                cx = depth.get_width()  // 2
                cy = depth.get_height() // 2
                return round(depth.get_distance(cx, cy) * 1000.0, 1)
            except Exception:
                return -1.0
        with self._lock:
            return round(self._sim_depth, 1)

    def in_zone(self, depth_mm: float) -> bool:
        return 0 < depth_mm <= self.threshold_mm

    def stop(self):
        self._stop.set()
        if self._pipe:
            try:
                self._pipe.stop()
            except Exception:
                pass
