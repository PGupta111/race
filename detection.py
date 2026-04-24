"""
YOLO26 bib detection.

Loads a trained YOLO model when YOLO_MODEL_PATH exists and ultralytics is
installed; falls back to a plausible simulation otherwise.

To enable real inference:
  1. pip install ultralytics
  2. Train or obtain a bib-detection model (YOLOv8/YOLO11 format).
  3. Set YOLO_MODEL_PATH=/path/to/bib_detector.pt in .env
"""
import logging
import os
import random
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_model      = None
YOLO_ACTIVE = False
_MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "models/bib_detector.pt")


def _init_model():
    global _model, YOLO_ACTIVE
    try:
        from ultralytics import YOLO  # type: ignore
        p = Path(_MODEL_PATH)
        if not p.exists():
            logger.warning(
                "YOLO model not found at %s — place a trained bib-detection model "
                "there and restart. Using simulation for now.",
                _MODEL_PATH,
            )
            return
        _model = YOLO(str(p))
        YOLO_ACTIVE = True
        logger.info("YOLO model loaded from %s", _MODEL_PATH)
    except ImportError:
        logger.warning("ultralytics not installed — bib detection is simulated. pip install ultralytics")
    except Exception as exc:
        logger.warning("YOLO init error (%s) — using simulation", exc)


_init_model()


def detect_bib(image_path: str, known_bibs: list[str] | None = None) -> dict:
    """Detect a bib number in an image. Returns detection metadata dict."""
    if YOLO_ACTIVE and _model is not None:
        return _run_yolo(image_path)
    return _simulate(known_bibs)


def _run_yolo(image_path: str) -> dict:
    t0      = time.time()
    results = _model(image_path, verbose=False)[0]
    ms      = round((time.time() - t0) * 1000, 1)

    if not results.boxes or len(results.boxes) == 0:
        return {"bib": None, "confidence": 0.0, "model": "YOLO26", "inference_ms": ms, "bbox": []}

    idx  = int(results.boxes.conf.argmax())
    conf = float(results.boxes.conf[idx])
    bbox = [round(x) for x in results.boxes.xyxy[idx].tolist()]
    # In a trained bib-detection model the class name encodes the bib number
    label = results.names[int(results.boxes.cls[idx])]

    return {
        "bib":          label,
        "confidence":   round(conf, 3),
        "model":        "YOLO26",
        "inference_ms": ms,
        "bbox":         bbox,
    }


def _simulate(known_bibs: list[str] | None) -> dict:
    pool = known_bibs or [str(b) for b in range(101, 111)]
    if random.random() < 0.90 and pool:
        bib  = random.choice(pool)
        conf = round(random.uniform(0.87, 0.99), 3)
    else:
        bib  = str(random.randint(100, 200))
        conf = round(random.uniform(0.35, 0.72), 3)
    w, h = random.randint(180, 320), random.randint(60, 100)
    x, y = random.randint(80, 400), random.randint(200, 500)
    return {
        "bib":          bib,
        "confidence":   conf,
        "model":        "YOLO26-sim",
        "inference_ms": round(random.uniform(12, 28), 1),
        "bbox":         [x, y, x + w, y + h],
    }
