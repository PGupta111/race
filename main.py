"""Big Red Command Center — FastAPI application."""
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
)
from detection import YOLO_ACTIVE, detect_bib
from rate_limit import check_trigger_rate
from sensors import DepthSensor
from timing import DepthChecker, LineScanTimer, _fmt
from webhook import post_result

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Big Red Command Center", version="2026.ULTRA-LIGHT")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        self._connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)

    async def broadcast(self, payload: dict):
        text = json.dumps(payload)
        dead: set[WebSocket] = set()
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
    await manager.broadcast({"type": "finish_detected", "event": event})
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


# ── Runner management ─────────────────────────────────────────────────────────

@app.get("/api/runners")
def list_runners():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM runners ORDER BY CAST(bib_number AS INTEGER)"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class RunnerIn(BaseModel):
    bib_number: str
    name: str
    category: str
    email: str = ""


@app.post("/api/runners", status_code=201, dependencies=[Depends(require_token)])
def create_runner(runner: RunnerIn):
    import sqlite3 as _sqlite3
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO runners (bib_number, name, category, email) VALUES (?, ?, ?, ?)",
            (runner.bib_number, runner.name, runner.category, runner.email),
        )
        conn.commit()
    except _sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(400, f"Bib {runner.bib_number} already registered")
    conn.close()
    return {"status": "ok", "bib": runner.bib_number}


# ── Check-in ──────────────────────────────────────────────────────────────────

@app.post("/api/checkin/{bib_number}", dependencies=[Depends(require_token)])
async def checkin(
    bib_number: str,
    tshirt: int                 = Form(0),
    photo: Optional[UploadFile] = File(None),
):
    conn = get_db()
    runner = conn.execute(
        "SELECT * FROM runners WHERE bib_number=?", (bib_number,)
    ).fetchone()
    if not runner:
        conn.close()
        raise HTTPException(404, f"Bib {bib_number} not found")

    photo_path = ""
    if photo and photo.filename:
        data  = await _validated_bytes(photo)
        fname = f"checkin_{bib_number}_{int(time.time())}.jpg"
        (UPLOAD_DIR / fname).write_bytes(data)
        photo_path = f"/uploads/{fname}"

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE runners SET checkin_time=?, tshirt=?, checkin_photo=? WHERE bib_number=?",
        (now, tshirt, photo_path, bib_number),
    )
    conn.commit()
    updated = dict(conn.execute(
        "SELECT * FROM runners WHERE bib_number=?", (bib_number,)
    ).fetchone())
    conn.close()
    await manager.broadcast({"type": "checkin", "runner": updated})
    return updated


# ── YOLO26 bib detection ──────────────────────────────────────────────────────

@app.post("/api/detect", dependencies=[Depends(require_token)])
async def detect(photo: UploadFile = File(...)):
    data  = await _validated_bytes(photo)
    fname = f"detect_{int(time.time()*1000)}.jpg"
    fpath = UPLOAD_DIR / fname
    fpath.write_bytes(data)

    conn  = get_db()
    known = [r["bib_number"] for r in conn.execute("SELECT bib_number FROM runners").fetchall()]
    conn.close()

    result = detect_bib(str(fpath), known)
    result["photo_path"] = f"/uploads/{fname}"
    return result


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
        SELECT r.bib_number, r.name, r.category,
               MIN(fe.timestamp) AS finish_ts, fe.status
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
