"""
Visual Fingerprinting — Bib Detection & Matching.

Three modes (in priority order):
  1. Visual Match: ORB feature matching against stored check-in bib photos (OpenCV)
  2. OCR Fallback: Read digits from bib region (EasyOCR / pytesseract)
  3. Simulation: Returns a plausible random match for dev/testing

Install for real matching:
    pip install opencv-python
    pip install easyocr  (optional, for OCR fallback)
"""
import logging
import os
import random
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Detection engine flags ──
CV2_AVAILABLE = False
OCR_AVAILABLE = False
DETECTION_MODE = "simulation"  # "visual_match" | "ocr" | "simulation"

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
    DETECTION_MODE = "visual_match"
    logger.info("OpenCV loaded — visual fingerprinting active")
except ImportError:
    logger.warning("opencv-python not installed — using simulated bib detection. pip install opencv-python")

try:
    import easyocr
    _ocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
    OCR_AVAILABLE = True
    logger.info("EasyOCR loaded — OCR fallback available")
except ImportError:
    _ocr_reader = None
except Exception as exc:
    _ocr_reader = None
    logger.warning("EasyOCR init error (%s)", exc)

# Keep YOLO_ACTIVE for backward compat with main.py references
YOLO_ACTIVE = CV2_AVAILABLE


# ── ORB Feature Matching ─────────────────────────────────────────────────────

def _orb_match_score(img1, img2) -> float:
    """
    Compare two images using ORB feature matching.
    Returns a score 0.0–1.0 (higher = better match).
    """
    if not CV2_AVAILABLE:
        return 0.0

    orb = cv2.ORB_create(nfeatures=500)

    # Convert to grayscale
    g1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY) if len(img1.shape) == 3 else img1
    g2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY) if len(img2.shape) == 3 else img2

    # Resize to similar dimensions for fair comparison
    h1, w1 = g1.shape[:2]
    h2, w2 = g2.shape[:2]
    target_w = 300
    if w1 > 0:
        g1 = cv2.resize(g1, (target_w, int(h1 * target_w / w1)))
    if w2 > 0:
        g2 = cv2.resize(g2, (target_w, int(h2 * target_w / w2)))

    kp1, des1 = orb.detectAndCompute(g1, None)
    kp2, des2 = orb.detectAndCompute(g2, None)

    if des1 is None or des2 is None or len(des1) < 5 or len(des2) < 5:
        return 0.0

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    matches = bf.knnMatch(des1, des2, k=2)

    # Lowe's ratio test
    good = []
    for pair in matches:
        if len(pair) == 2:
            m, n = pair
            if m.distance < 0.75 * n.distance:
                good.append(m)

    # Score = ratio of good matches to total keypoints
    max_kp = max(len(kp1), len(kp2), 1)
    score = len(good) / max_kp
    return min(score, 1.0)


def _read_image(path_or_bytes) -> "np.ndarray":
    """Read an image from a file path or bytes."""
    if isinstance(path_or_bytes, bytes):
        arr = np.frombuffer(path_or_bytes, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return cv2.imread(str(path_or_bytes))


# ── Main Detection Functions ─────────────────────────────────────────────────

def detect_bib(image_path: str, known_bibs: Optional[List[str]] = None) -> dict:
    """
    Detect a bib number in an image.
    In visual_match mode, this just returns a simulated result since
    matching requires the stored bib photos (use match_bib_visual instead).
    """
    if DETECTION_MODE == "simulation" or not CV2_AVAILABLE:
        return _simulate(known_bibs)

    # Basic detection: try OCR on the image
    if OCR_AVAILABLE and _ocr_reader:
        return _detect_ocr(image_path, known_bibs)

    return _simulate(known_bibs)


def match_bib_visual(query_image_bytes: bytes, bib_photos: List[Dict]) -> dict:
    """
    Match a query image (from finish-line camera) against stored bib photos.

    Args:
        query_image_bytes: JPEG bytes of the finish-line frame
        bib_photos: List of dicts with {bib_number, bib_photo, name, category}
                    where bib_photo is a relative URL path like /uploads/bib_101_xxx.jpg

    Returns:
        dict with {bib, confidence, model, inference_ms, bbox, method}
    """
    t0 = time.time()

    if not CV2_AVAILABLE or not bib_photos:
        # Simulate matching against the known bibs
        known = [bp["bib_number"] for bp in bib_photos] if bib_photos else None
        result = _simulate(known)
        result["method"] = "simulation"
        return result

    query_img = _read_image(query_image_bytes)
    if query_img is None:
        return {"bib": None, "confidence": 0.0, "model": "visual", "inference_ms": 0, "bbox": [], "method": "error"}

    best_score = 0.0
    best_bib = None
    best_name = None

    upload_dir = Path("uploads")

    for bp in bib_photos:
        bib_path = bp.get("bib_photo", "")
        if not bib_path:
            continue

        # Resolve path: /uploads/xxx.jpg → uploads/xxx.jpg
        local_path = bib_path.lstrip("/")
        if not Path(local_path).exists():
            continue

        ref_img = cv2.imread(local_path)
        if ref_img is None:
            continue

        score = _orb_match_score(query_img, ref_img)
        if score > best_score:
            best_score = score
            best_bib = bp["bib_number"]
            best_name = bp.get("name", "")

    ms = round((time.time() - t0) * 1000, 1)

    if best_bib and best_score > 0.05:  # Minimum threshold
        return {
            "bib": best_bib,
            "confidence": round(min(best_score * 2.5, 0.99), 3),  # Scale for display
            "model": "ORB-visual",
            "inference_ms": ms,
            "bbox": [],
            "method": "visual_match",
            "matched_name": best_name,
        }

    # Fallback: try OCR
    if OCR_AVAILABLE and _ocr_reader:
        result = _detect_ocr_bytes(query_image_bytes, [bp["bib_number"] for bp in bib_photos])
        result["inference_ms"] = ms
        return result

    return {
        "bib": None,
        "confidence": 0.0,
        "model": "ORB-visual",
        "inference_ms": ms,
        "bbox": [],
        "method": "no_match",
    }


def compare_bibs(image1_bytes: bytes, image2_bytes: bytes) -> dict:
    """
    Compare two bib images directly. Used by VAR dashboard for manual verification.
    Returns similarity score.
    """
    if not CV2_AVAILABLE:
        return {"score": random.uniform(0.6, 0.95), "method": "simulation"}

    img1 = _read_image(image1_bytes)
    img2 = _read_image(image2_bytes)
    if img1 is None or img2 is None:
        return {"score": 0.0, "method": "error"}

    score = _orb_match_score(img1, img2)
    return {"score": round(score, 4), "method": "orb"}


# ── OCR Fallback ─────────────────────────────────────────────────────────────

def _detect_ocr(image_path: str, known_bibs: Optional[List[str]] = None) -> dict:
    t0 = time.time()
    try:
        results = _ocr_reader.readtext(image_path)
        ms = round((time.time() - t0) * 1000, 1)

        for (bbox, text, conf) in results:
            digits = ''.join(c for c in text if c.isdigit())
            if digits and (not known_bibs or digits in known_bibs):
                return {
                    "bib": digits,
                    "confidence": round(conf, 3),
                    "model": "EasyOCR",
                    "inference_ms": ms,
                    "bbox": [int(bbox[0][0]), int(bbox[0][1]), int(bbox[2][0]), int(bbox[2][1])],
                    "method": "ocr",
                }

        return {"bib": None, "confidence": 0.0, "model": "EasyOCR", "inference_ms": ms, "bbox": [], "method": "ocr_no_match"}
    except Exception as exc:
        logger.warning("OCR error: %s", exc)
        return _simulate(known_bibs)


def _detect_ocr_bytes(image_bytes: bytes, known_bibs: Optional[List[str]] = None) -> dict:
    """OCR on raw bytes."""
    if not OCR_AVAILABLE or not _ocr_reader:
        return _simulate(known_bibs)
    # Save temp file for easyocr
    tmp = Path("uploads/_temp_ocr.jpg")
    tmp.write_bytes(image_bytes)
    result = _detect_ocr(str(tmp), known_bibs)
    try:
        tmp.unlink()
    except:
        pass
    return result


# ── Simulation ───────────────────────────────────────────────────────────────

def _simulate(known_bibs: Optional[List[str]] = None) -> dict:
    pool = known_bibs or [str(b) for b in range(101, 111)]
    if random.random() < 0.90 and pool:
        bib = random.choice(pool)
        conf = round(random.uniform(0.87, 0.99), 3)
    else:
        bib = str(random.randint(100, 200))
        conf = round(random.uniform(0.35, 0.72), 3)
    w, h = random.randint(180, 320), random.randint(60, 100)
    x, y = random.randint(80, 400), random.randint(200, 500)
    return {
        "bib": bib,
        "confidence": conf,
        "model": "visual-sim",
        "inference_ms": round(random.uniform(12, 28), 1),
        "bbox": [x, y, x + w, y + h],
        "method": "simulation",
    }
