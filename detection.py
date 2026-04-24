"""
YOLO26 bib detection simulation.

In production this module would load the YOLO26 model and run inference
on the provided image. Here we simulate plausible detection results so the
rest of the system can be developed and tested end-to-end.
"""
import random
import time

KNOWN_BIBS = [str(b) for b in range(101, 111)]


def simulate_bib_detection(image_path: str, known_bibs: list[str] | None = None) -> dict:
    """Return a simulated YOLO26 detection result for the given image."""
    pool = known_bibs if known_bibs else KNOWN_BIBS

    # Simulate 90% correct detection
    if random.random() < 0.90 and pool:
        bib = random.choice(pool)
        confidence = round(random.uniform(0.87, 0.99), 3)
    else:
        # Simulate an uncertain / wrong read
        bib = str(random.randint(100, 200))
        confidence = round(random.uniform(0.35, 0.72), 3)

    w, h = random.randint(180, 320), random.randint(60, 100)
    x, y = random.randint(80, 400), random.randint(200, 500)

    return {
        "bib": bib,
        "confidence": confidence,
        "model": "YOLO26",
        "inference_ms": round(random.uniform(12, 28), 1),
        "bbox": [x, y, x + w, y + h],
    }
