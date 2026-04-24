"""
Precision timing primitives.

LineScanTimer simulates a 1-pixel vertical line-scan camera that delivers
millisecond-accurate finish timestamps.

DepthChecker simulates the IR/LiDAR sensor that gates bib processing:
a bib is only accepted when the runner is within 1 m of the finish line.
"""
import time

FINISH_DEPTH_THRESHOLD_MM = 1000  # 1 metre


class LineScanTimer:
    """Monotonic high-resolution clock anchored to race start."""

    def __init__(self):
        self._race_start: float | None = None

    @property
    def race_start(self) -> float | None:
        return self._race_start

    def start(self, ts: float | None = None) -> float:
        self._race_start = ts if ts is not None else time.time()
        return self._race_start

    def record_crossing(self) -> dict:
        wall = time.time()
        elapsed = wall - self._race_start if self._race_start else 0.0
        return {
            "wall_timestamp": wall,
            "elapsed_s": round(elapsed, 4),
            "elapsed_ms": round(elapsed * 1000, 1),
            "formatted": _fmt(elapsed),
        }


class DepthChecker:
    """Gates finish detection based on IR/LiDAR depth reading."""

    def __init__(self, threshold_mm: float = FINISH_DEPTH_THRESHOLD_MM):
        self.threshold_mm = threshold_mm

    def check(self, depth_mm: float) -> bool:
        return 0 < depth_mm <= self.threshold_mm

    def reading_label(self, depth_mm: float) -> str:
        return "OK" if self.check(depth_mm) else "TOO FAR"


def _fmt(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}" if h else f"{m}:{s:06.3f}"
