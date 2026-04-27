"""
Precision timing primitives.

LineScanTimer simulates a 1-pixel vertical line-scan camera that delivers
millisecond-accurate finish timestamps.

DepthChecker gates bib processing: only accepted when depth ≤ threshold.
"""
import time
from typing import Optional

FINISH_DEPTH_THRESHOLD_MM = 1000.0  # 1 metre


class LineScanTimer:
    """Monotonic high-resolution clock anchored to race start."""

    def __init__(self):
        self._race_start: Optional[float] = None

    @property
    def race_start(self) -> Optional[float]:
        return self._race_start

    def start(self, ts: Optional[float] = None) -> float:
        self._race_start = ts if ts is not None else time.time()
        return self._race_start

    def reset(self) -> None:
        self._race_start = None

    def record_crossing(self) -> dict:
        wall    = time.time()
        elapsed = wall - self._race_start if self._race_start else 0.0
        return {
            "wall_timestamp":  wall,
            "elapsed_s":       round(elapsed, 4),
            "elapsed_ms":      round(elapsed * 1000, 1),
            "formatted":       _fmt(elapsed),
        }


class DepthChecker:
    """Gates finish detection based on IR/LiDAR depth reading."""

    def __init__(self, threshold_mm: float = FINISH_DEPTH_THRESHOLD_MM):
        self.threshold_mm = threshold_mm

    def check(self, depth_mm: float) -> bool:
        return 0 < depth_mm <= self.threshold_mm


def _fmt(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}" if h else f"{m}:{s:06.3f}"
