"""Big Red Command Center — FastAPI application."""
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set

from fastapi import (
    Depends, FastAPI, File, Form, HTTPException,
    UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth import require_token
from backup import run as run_backup
from camera import CV2_AVAILABLE, get_latest_frame, request_clip, start_capture, stop_capture
from database import (
    get_db, get_finish_event, get_race_start,
    init_db, seed_runners, set_race_start,
    register_runner, lookup_by_registration,
    search_runners, assign_bib, get_all_bib_photos,
)
from detection import YOLO_ACTIVE, DETECTION_MODE
from rate_limit import check_trigger_rate
from sensors import DepthSensor
from timing import DepthChecker, LineScanTimer, _fmt
from webhook import post_result

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Big Red Command Center", version="2026.ULTRA-ELITE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

import os

if os.getenv("VERCEL"):
    UPLOAD_DIR = Path("/tmp/uploads")
else:
    UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path("static")

app.mount("/static",  StaticFiles(directory="static"),  name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

timer        = LineScanTimer()
depth_sensor = DepthSensor()
depth_check  = DepthChecker(threshold_mm=depth_sensor.threshold_mm)

# ── File validation ───────────────────────────────────────────────────────────

_ALLOWED_MAGIC  = (b"\xff\xd8\xff", b"\x89PNG")
_MAX_UPLOAD     = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024


async def _validated_bytes(upload: UploadFile, max_bytes: int = _MAX_UPLOAD) -> bytes:
    data = await upload.read()
    if len(data) > max_bytes:
        raise HTTPException(413, f"Upload exceeds {max_bytes // 1024 // 1024} MB limit")
    if not any(data[:4].startswith(sig) for sig in _ALLOWED_MAGIC):
        raise HTTPException(415, "Only JPEG and PNG images are accepted")
    return data


# ── WebSocket manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)

    async def broadcast(self, payload: dict):
        text = json.dumps(payload)
        dead: Set[WebSocket] = set()
        for ws in self._connections:
            try:
                await ws.send_text(text)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._connections.discard(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()

# ── Pipeline: camera thread → async event loop ───────────────────────────────

_pipeline_queue: asyncio.Queue = asyncio.Queue()


async def _pipeline_consumer():
    """Drain the camera-thread queue and record each finish event."""
    while True:
        item = await _pipeline_queue.get()
        try:
            await _record_finish(**item)
        except Exception as exc:
            logger.error("Pipeline consumer error: %s", exc)


# ── Shared finish-recording helper ────────────────────────────────────────────

async def _record_finish(
    bib: str,
    detected_bib: str,
    depth_mm: float,
    depth_ok: bool,
    photo_path: str = "",
) -> dict:
    """
    Insert a finish_events row, record a video clip, broadcast via WS.
    Single source of truth used by both the HTTP endpoint and the camera pipeline.
    """
    ts = time.time()
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO finish_events
           (bib_number, detected_bib, timestamp, depth_mm, depth_ok, photo_path, video_path, status)
           VALUES (?, ?, ?, ?, ?, ?, '', 'pending')""",
        (bib, detected_bib, ts, depth_mm, 1 if depth_ok else 0, photo_path),
    )
    event_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Video clip: runs in a thread for POST_ROLL_S seconds — must not block event loop
    video_path = ""
    if CV2_AVAILABLE:
        video_path = await asyncio.to_thread(request_clip, event_id)
        if video_path:
            conn = get_db()
            conn.execute(
                "UPDATE finish_events SET video_path=? WHERE id=?",
                (video_path, event_id),
            )
            conn.commit()
            conn.close()

    event = get_finish_event(event_id)
    # Enrich broadcast with runner info for announcer
    conn2 = get_db()
    runner_row = conn2.execute(
        "SELECT name, category FROM runners WHERE bib_number=?", (bib,)
    ).fetchone()
    conn2.close()
    broadcast_event = {**event}
    if runner_row:
        broadcast_event["runner_name"] = runner_row["name"]
        broadcast_event["runner_category"] = runner_row["category"]
    await manager.broadcast({"type": "finish_detected", "event": broadcast_event})
    return event


# ── Startup / shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    seed_runners()
    rs = get_race_start()
    if rs:
        timer.start(rs)
    start_capture()
    asyncio.create_task(_pipeline_consumer())


@app.on_event("shutdown")
async def shutdown():
    stop_capture()
    depth_sensor.stop()


# ── HTML page routes ──────────────────────────────────────────────────────────

def _html(name: str) -> HTMLResponse:
    path = STATIC_DIR / name
    if not path.exists():
        raise HTTPException(404, f"{name} not found")
    return HTMLResponse(path.read_text())


@app.get("/",                response_class=HTMLResponse)
def page_index():   return _html("index.html")

@app.get("/checkin",         response_class=HTMLResponse)
def page_checkin():  return _html("checkin.html")

@app.get("/var",             response_class=HTMLResponse)
def page_var():      return _html("var.html")

@app.get("/results",         response_class=HTMLResponse)
def page_results():  return _html("results.html")

@app.get("/receipt/{event_id}", response_class=HTMLResponse)
def page_receipt(event_id: int):  return _html("receipt.html")

@app.get("/announcer",       response_class=HTMLResponse)
def page_announcer(): return _html("announcer.html")

@app.get("/overlay",         response_class=HTMLResponse)
def page_overlay():   return _html("overlay.html")

@app.get("/phone/side",      response_class=HTMLResponse)
def page_phone_side():    return _html("phone_side.html")

@app.get("/phone/front",     response_class=HTMLResponse)
def page_phone_front():   return _html("phone_front.html")

@app.get("/phone/starter",   response_class=HTMLResponse)
def page_phone_starter(): return _html("phone_starter.html")

@app.get("/phone/calibrate", response_class=HTMLResponse)
def page_phone_calibrate(): return _html("phone_calibrate.html")

@app.get("/register",        response_class=HTMLResponse)
def page_register():  return _html("register.html")

@app.get("/livestream",      response_class=HTMLResponse)
def page_livestream(): return _html("livestream.html")


# ── Registration ─────────────────────────────────────────────────────────────

class RegisterIn(BaseModel):
    name: str
    email: str = ""
    category: str


@app.post("/api/register", status_code=201)
def api_register(runner: RegisterIn):
    """Public registration — no auth required. Returns runner with QR registration_id."""
    r = register_runner(runner.name, runner.email, runner.category)
    return r


@app.get("/api/register/{reg_id}")
def api_lookup_registration(reg_id: str):
    """Lookup a runner by their QR registration UUID."""
    r = lookup_by_registration(reg_id)
    if not r:
        raise HTTPException(404, "Registration not found")
    return r


@app.get("/api/runners/search")
def api_search_runners(q: str = ""):
    """Search runners by name for check-in."""
    if len(q) < 2:
        return []
    return search_runners(q)


# ── Runner list ──────────────────────────────────────────────────────────────

@app.get("/api/runners")
def list_runners():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM runners ORDER BY name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Check-in (new flow: find runner → assign bib → check in) ─────────────────

@app.post("/api/checkin/{runner_id}", dependencies=[Depends(require_token)])
async def checkin(
    runner_id: int,
    bib_number: str             = Form(...),
    tshirt: int                 = Form(0),
    bib_photo: Optional[UploadFile] = File(None),
):
    """
    Check in a runner after assigning a bib.
    The volunteer has already found the runner (via QR or name search),
    scanned a physical bib, and now submits the assignment.
    """
    conn = get_db()
    runner = conn.execute(
        "SELECT * FROM runners WHERE id=?", (runner_id,)
    ).fetchone()
    if not runner:
        conn.close()
        raise HTTPException(404, "Runner not found")

    # Save bib photo (the reference image for visual fingerprinting)
    bib_photo_path = ""
    if bib_photo and bib_photo.filename:
        data = await _validated_bytes(bib_photo)
        fname = f"bib_{bib_number}_{int(time.time())}.jpg"
        (UPLOAD_DIR / fname).write_bytes(data)
        bib_photo_path = f"/uploads/{fname}"

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE runners
           SET bib_number=?, bib_photo=?, checkin_time=?, tshirt=?
           WHERE id=?""",
        (bib_number, bib_photo_path, now, tshirt, runner_id),
    )
    conn.commit()
    updated = dict(conn.execute(
        "SELECT * FROM runners WHERE id=?", (runner_id,)
    ).fetchone())
    conn.close()
    await manager.broadcast({"type": "checkin", "runner": updated})
    return updated



# ── Visual Fingerprinting — Bib Detection ─────────────────────────────────────

from detection import match_bib_visual, compare_bibs

@app.post("/api/detect", dependencies=[Depends(require_token)])
async def detect(photo: UploadFile = File(...)):
    """
    Detect a bib in a photo by visual matching against stored check-in bib photos.
    Used by the front-view iPhone for both approach detection and finish capture.
    """
    data = await _validated_bytes(photo)
    fname = f"detect_{int(time.time()*1000)}.jpg"
    fpath = UPLOAD_DIR / fname
    fpath.write_bytes(data)

    bib_photos = get_all_bib_photos()
    result = match_bib_visual(data, bib_photos)
    result["photo_path"] = f"/uploads/{fname}"
    return result


@app.post("/api/detect/approach", dependencies=[Depends(require_token)])
async def detect_approach(photo: UploadFile = File(...)):
    """
    Detect an approaching runner's bib. Same as /api/detect but also
    broadcasts approaching_runner to announcer and livestream.
    """
    data = await _validated_bytes(photo)
    fname = f"approach_{int(time.time()*1000)}.jpg"
    fpath = UPLOAD_DIR / fname
    fpath.write_bytes(data)

    bib_photos = get_all_bib_photos()
    result = match_bib_visual(data, bib_photos)
    result["photo_path"] = f"/uploads/{fname}"

    if result.get("bib"):
        # Look up runner info
        conn = get_db()
        runner = conn.execute(
            "SELECT name, category FROM runners WHERE bib_number=?",
            (result["bib"],),
        ).fetchone()
        conn.close()

        if runner:
            await manager.broadcast({
                "type": "approaching_runner",
                "bib_number": result["bib"],
                "name": runner["name"],
                "category": runner["category"],
                "confidence": result.get("confidence", 0),
            })

    return result


@app.post("/api/detect/compare")
async def detect_compare(
    image1: UploadFile = File(...),
    image2: UploadFile = File(...),
):
    """Compare two bib images for similarity. Used by VAR for manual verification."""
    data1 = await _validated_bytes(image1)
    data2 = await _validated_bytes(image2)
    return compare_bibs(data1, data2)



# ── Race clock ────────────────────────────────────────────────────────────────

@app.post("/api/race/start", dependencies=[Depends(require_token)])
async def start_race():
    ts = time.time()
    timer.start(ts)
    set_race_start(ts)
    await manager.broadcast({"type": "race_started", "timestamp": ts})
    return {"status": "started", "timestamp": ts}


@app.post("/api/race/reset", dependencies=[Depends(require_token)])
async def reset_race():
    conn = get_db()
    conn.execute("DELETE FROM finish_events")
    conn.execute("DELETE FROM race_meta WHERE key='race_start'")
    conn.commit()
    conn.close()
    timer.reset()
    await manager.broadcast({"type": "race_reset"})
    return {"status": "reset"}


@app.get("/api/race/status")
def race_status():
    rs = timer.race_start
    if rs is None:
        return {"started": False, "elapsed_s": 0, "elapsed_formatted": "0:00.000"}
    elapsed = time.time() - rs
    return {
        "started":          True,
        "race_start":       rs,
        "elapsed_s":        round(elapsed, 4),
        "elapsed_formatted": _fmt(elapsed),
    }


# ── Finish-line timing trigger ────────────────────────────────────────────────

@app.post(
    "/api/timing/trigger",
    dependencies=[Depends(require_token), Depends(check_trigger_rate)],
)
async def timing_trigger(
    detected_bib: str             = Form(...),
    depth_mm:     float           = Form(...),
    photo: Optional[UploadFile]   = File(None),
):
    depth_ok   = depth_check.check(depth_mm)
    photo_path = ""
    if photo and photo.filename:
        data  = await _validated_bytes(photo)
        fname = f"finish_{detected_bib}_{int(time.time()*1000)}.jpg"
        (UPLOAD_DIR / fname).write_bytes(data)
        photo_path = f"/uploads/{fname}"

    event = await _record_finish(
        bib=detected_bib, detected_bib=detected_bib,
        depth_mm=depth_mm, depth_ok=depth_ok,
        photo_path=photo_path,
    )
    return {
        "event_id":    event["id"],
        "timestamp":   event["timestamp"],
        "formatted":   _fmt(event["timestamp"] - timer.race_start) if timer.race_start else "---",
        "depth_ok":    depth_ok,
        "depth_mm":    depth_mm,
        "detected_bib": detected_bib,
    }


# ── iPhone Timing Pipeline — Crossing + Matching Engine ──────────────────────

MATCH_WINDOW_S = 3.0  # Side-view crossing must match front-view within ±3s


@app.post("/api/timing/crossing", dependencies=[Depends(require_token)])
async def timing_crossing(
    timestamp: float = Form(...),
    ribbon_crop: Optional[UploadFile] = File(None),
):
    """
    Called by the Side-View iPhone when a runner crosses the finish line.
    Stores the crossing event and broadcasts to front-view for bib capture.
    """
    crop_path = ""
    if ribbon_crop and ribbon_crop.filename:
        data = await _validated_bytes(ribbon_crop)
        fname = f"ribbon_{int(timestamp * 1000)}.jpg"
        (UPLOAD_DIR / fname).write_bytes(data)
        crop_path = f"/uploads/{fname}"

    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO crossing_events (timestamp, ribbon_crop, created_at)
           VALUES (?, ?, ?)""",
        (timestamp, crop_path, now),
    )
    crossing_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Broadcast to front-view phone to capture bib NOW
    elapsed = _fmt(timestamp - timer.race_start) if timer.race_start else "---"
    await manager.broadcast({
        "type": "crossing_detected",
        "crossing_id": crossing_id,
        "timestamp": timestamp,
        "elapsed": elapsed,
        "ribbon_crop": crop_path,
    })

    logger.info("Crossing #%d at %s", crossing_id, elapsed)
    return {"crossing_id": crossing_id, "timestamp": timestamp, "elapsed": elapsed}


@app.post("/api/timing/match", dependencies=[Depends(require_token)])
async def timing_match(
    crossing_id: int = Form(0),
    bib_number: str = Form(...),
    confidence: float = Form(0.0),
    depth_mm: float = Form(0.0),
    photo: Optional[UploadFile] = File(None),
):
    """
    Called by the Front-View iPhone after detecting a bib.
    Matches the bib detection to the nearest unmatched crossing event,
    then creates a finish_event for VAR review.
    """
    conn = get_db()

    # Find the crossing to match
    if crossing_id > 0:
        # Direct match by ID (best case — front-view knows which crossing)
        crossing = conn.execute(
            "SELECT * FROM crossing_events WHERE id=? AND matched=0",
            (crossing_id,),
        ).fetchone()
    else:
        # Auto-match: find nearest unmatched crossing within ±MATCH_WINDOW_S
        now_ts = time.time()
        crossing = conn.execute(
            """SELECT * FROM crossing_events
               WHERE matched=0 AND ABS(timestamp - ?) < ?
               ORDER BY ABS(timestamp - ?) LIMIT 1""",
            (now_ts, MATCH_WINDOW_S, now_ts),
        ).fetchone()

    if not crossing:
        conn.close()
        raise HTTPException(404, "No unmatched crossing found within time window")

    crossing_ts = crossing["timestamp"]
    cx_id = crossing["id"]

    # Save front-view photo
    photo_path = ""
    if photo and photo.filename:
        data = await _validated_bytes(photo)
        fname = f"front_{bib_number}_{int(crossing_ts * 1000)}.jpg"
        (UPLOAD_DIR / fname).write_bytes(data)
        photo_path = f"/uploads/{fname}"

    # Depth check
    depth_ok = depth_check.check(depth_mm) if depth_mm > 0 else True

    conn.close()

    # Create the finish event using the crossing timestamp (the accurate one)
    event = await _record_finish(
        bib=bib_number,
        detected_bib=bib_number,
        depth_mm=depth_mm,
        depth_ok=depth_ok,
        photo_path=photo_path,
    )

    # Update the finish_event timestamp to the side-view crossing time (more accurate)
    conn = get_db()
    conn.execute(
        "UPDATE finish_events SET timestamp=? WHERE id=?",
        (crossing_ts, event["id"]),
    )
    # Mark crossing as matched
    conn.execute(
        "UPDATE crossing_events SET matched=1, matched_bib=?, finish_event_id=? WHERE id=?",
        (bib_number, event["id"], cx_id),
    )
    conn.commit()
    conn.close()

    elapsed = _fmt(crossing_ts - timer.race_start) if timer.race_start else "---"
    logger.info(
        "MATCH: Crossing #%d → Bib %s at %s (conf=%.1f%%, depth=%s)",
        cx_id, bib_number, elapsed, confidence * 100, fmtDepth(depth_mm),
    )

    return {
        "event_id": event["id"],
        "crossing_id": cx_id,
        "bib_number": bib_number,
        "timestamp": crossing_ts,
        "elapsed": elapsed,
        "confidence": confidence,
        "depth_mm": depth_mm,
        "depth_ok": depth_ok,
    }


def fmtDepth(mm):
    """Format depth for logging."""
    if not mm or mm < 0:
        return "—"
    return f"{mm:.0f}mm" if mm < 1000 else f"{mm/1000:.2f}m"


@app.get("/api/timing/crossings")
def list_crossings(unmatched_only: bool = True):
    """List recent crossing events. Used for manual matching fallback."""
    conn = get_db()
    where = "WHERE matched=0" if unmatched_only else ""
    rows = conn.execute(f"""
        SELECT * FROM crossing_events {where}
        ORDER BY timestamp DESC LIMIT 30
    """).fetchall()
    conn.close()
    rs = get_race_start()
    result = []
    for row in rows:
        r = dict(row)
        r["elapsed"] = _fmt(r["timestamp"] - rs) if rs else "---"
        result.append(r)
    return result


# ── Depth sensor ──────────────────────────────────────────────────────────────

@app.get("/api/sensor/depth")
def sensor_depth():
    mm = depth_sensor.read_mm()
    return {"depth_mm": mm, "in_zone": depth_sensor.in_zone(mm)}


# ── VAR Dashboard ─────────────────────────────────────────────────────────────

@app.get("/api/var/queue")
def var_queue():
    conn = get_db()
    rows = conn.execute("""
        SELECT fe.*, r.name, r.category, r.checkin_photo
        FROM   finish_events fe
        LEFT JOIN runners r ON fe.bib_number = r.bib_number
        WHERE  fe.status = 'pending'
        ORDER BY fe.timestamp
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/var/{event_id}/accept", dependencies=[Depends(require_token)])
async def var_accept(event_id: int):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE finish_events SET status='accepted', validated_at=? WHERE id=?",
        (now, event_id),
    )
    conn.commit()
    conn.close()
    event = get_finish_event(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    await manager.broadcast({"type": "var_accepted", "event": event})
    await post_result(event)
    return event


@app.post("/api/var/{event_id}/override", dependencies=[Depends(require_token)])
async def var_override(
    event_id:    int,
    correct_bib: str = Form(...),
    notes:       str = Form(""),
):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        """UPDATE finish_events
           SET status='overridden', bib_number=?, override_bib=?, validated_at=?, notes=?
           WHERE id=?""",
        (correct_bib, correct_bib, now, notes, event_id),
    )
    conn.commit()
    conn.close()
    event = get_finish_event(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    await manager.broadcast({"type": "var_overridden", "event": event, "correct_bib": correct_bib})
    await post_result(event)
    return event


@app.post("/api/var/{event_id}/reject", dependencies=[Depends(require_token)])
async def var_reject(event_id: int, notes: str = Form("")):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        "UPDATE finish_events SET status='rejected', validated_at=?, notes=? WHERE id=?",
        (now, notes, event_id),
    )
    conn.commit()
    conn.close()
    event = get_finish_event(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    await manager.broadcast({"type": "var_rejected", "event": event})
    return event


# ── Finish event (receipt data) ───────────────────────────────────────────────

@app.get("/api/finish_events/{event_id}")
def finish_event_detail(event_id: int):
    event = get_finish_event(event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    conn   = get_db()
    runner = conn.execute(
        "SELECT * FROM runners WHERE bib_number=?", (event["bib_number"],)
    ).fetchone()
    # Compute rank among accepted/overridden finishes
    rs = get_race_start()
    elapsed = event["timestamp"] - rs if rs else 0.0
    rank_row = conn.execute("""
        SELECT COUNT(*) + 1 AS rank
        FROM finish_events
        WHERE status IN ('accepted','overridden')
          AND timestamp < ?
    """, (event["timestamp"],)).fetchone()
    conn.close()
    return {
        **event,
        "runner":      dict(runner) if runner else None,
        "elapsed_s":   round(elapsed, 4),
        "finish_time": _fmt(elapsed),
        "rank":        rank_row["rank"] if rank_row else None,
    }


# ── Results & standings ───────────────────────────────────────────────────────

@app.get("/api/results")
def get_results(category: str = ""):
    conn   = get_db()
    rs     = get_race_start()
    where  = "AND r.category = ?" if category else ""
    params = (category,) if category else ()
    rows = conn.execute(f"""
        SELECT r.bib_number, r.name, r.category, r.bib_photo,
               MIN(fe.timestamp) AS finish_ts, fe.status, fe.photo_path
        FROM runners r
        JOIN finish_events fe ON r.bib_number = fe.bib_number
        WHERE fe.status IN ('accepted', 'overridden') {where}
        GROUP BY r.bib_number
        ORDER BY finish_ts
    """, params).fetchall()
    conn.close()
    results = []
    for i, row in enumerate(rows):
        r = dict(row)
        r["rank"]        = i + 1
        elapsed          = r["finish_ts"] - rs if rs else 0.0
        r["elapsed_s"]   = round(elapsed, 4)
        r["finish_time"] = _fmt(elapsed)
        results.append(r)
    return results


@app.get("/api/results/all")
def results_all():
    return {
        "Students": get_results("Students"),
        "Alumni":   get_results("Alumni"),
        "Parents":  get_results("Parents"),
    }


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats():
    conn     = get_db()
    total    = conn.execute("SELECT COUNT(*) FROM runners").fetchone()[0]
    checked  = conn.execute("SELECT COUNT(*) FROM runners WHERE checkin_time != ''").fetchone()[0]
    tshirts  = conn.execute("SELECT COUNT(*) FROM runners WHERE tshirt=1").fetchone()[0]
    finished = conn.execute(
        "SELECT COUNT(DISTINCT bib_number) FROM finish_events WHERE status IN ('accepted','overridden')"
    ).fetchone()[0]
    pending  = conn.execute(
        "SELECT COUNT(*) FROM finish_events WHERE status='pending'"
    ).fetchone()[0]
    conn.close()
    rs = timer.race_start
    return {
        "total_runners":     total,
        "checked_in":        checked,
        "tshirt_orders":     tshirts,
        "tshirt_revenue":    tshirts * 5.0,
        "finished":          finished,
        "pending_var":       pending,
        "race_started":      rs is not None,
        "elapsed_formatted": _fmt(time.time() - rs) if rs else "0:00.000",
        "yolo_active":       YOLO_ACTIVE,
        "camera_active":     CV2_AVAILABLE,
        "sensor_real":       depth_sensor.is_real,
    }


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.post("/api/admin/backup", dependencies=[Depends(require_token)])
def admin_backup():
    path = run_backup()
    return {"backup": str(path), "status": "ok"}


@app.get("/api/admin/status", dependencies=[Depends(require_token)])
def admin_status():
    return {
        "camera_active":    CV2_AVAILABLE,
        "sensor_real":      depth_sensor.is_real,
        "yolo_active":      YOLO_ACTIVE,
        "pipeline_queue":   _pipeline_queue.qsize(),
        "ws_connections":   manager.count,
    }


# ── Announcer feed ────────────────────────────────────────────────────────

@app.get("/api/announcer/feed")
def announcer_feed():
    """Last 15 finishers with full runner details for the announcer tablet."""
    conn = get_db()
    rs = get_race_start()
    rows = conn.execute("""
        SELECT fe.id, fe.bib_number, fe.timestamp, fe.status, fe.depth_ok,
               r.name, r.category
        FROM   finish_events fe
        LEFT JOIN runners r ON fe.bib_number = r.bib_number
        ORDER BY fe.timestamp DESC
        LIMIT 15
    """).fetchall()
    conn.close()
    feed = []
    for row in rows:
        r = dict(row)
        elapsed = r["timestamp"] - rs if rs else 0.0
        r["finish_time"] = _fmt(elapsed)
        r["elapsed_s"] = round(elapsed, 4)
        feed.append(r)
    return feed


# ── Demo simulation ───────────────────────────────────────────────────────────

@app.post("/api/demo/simulate_finish", dependencies=[Depends(require_token)])
async def demo_simulate():
    conn   = get_db()
    runner = conn.execute("SELECT bib_number FROM runners ORDER BY RANDOM() LIMIT 1").fetchone()
    conn.close()
    if not runner:
        raise HTTPException(400, "No runners in database")
    bib      = runner["bib_number"]
    depth_mm = round(random.uniform(300, 1400), 1)
    depth_ok = depth_check.check(depth_mm)
    event    = await _record_finish(
        bib=bib, detected_bib=bib, depth_mm=depth_mm, depth_ok=depth_ok,
    )
    return {
        "event_id": event["id"],
        "bib":      bib,
        "depth_mm": depth_mm,
        "depth_ok": depth_ok,
        "formatted": _fmt(event["timestamp"] - timer.race_start) if timer.race_start else "---",
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        manager.disconnect(ws)
