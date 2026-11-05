"""FastAPI server: upload/run invoices, stream live stage events, serve dashboard data."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, data, db, pipeline

app = FastAPI(title="AP Invoice Agent")
STATIC = config.ROOT / "static"


@app.on_event("startup")
def _startup():
    db.conn()
    # Warm the local OCR models in the background so the first scanned invoice isn't slow.
    import threading
    from . import ocr
    if ocr.available():
        threading.Thread(target=ocr._engine, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    from .extract import LAST_CLAUDE_ERROR
    from . import ocr
    return {"ok": True, "mode": config.extraction_mode(), "model": config.CLAUDE_MODEL,
            "claude_error": LAST_CLAUDE_ERROR.get("reason"), "local_ocr": ocr.available()}


@app.get("/api/samples")
def samples():
    manifest = config.SAMPLES_DIR / "manifest.json"
    return json.loads(manifest.read_text()) if manifest.exists() else []


@app.get("/api/samples/{name}")
def sample_file(name: str):
    p = (config.SAMPLES_DIR / name).resolve()
    if p.parent != config.SAMPLES_DIR.resolve() or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


@app.post("/api/runs")
async def create_run(file: UploadFile | None = File(None), sample: str | None = Form(None)):
    if sample:
        src = (config.SAMPLES_DIR / sample).resolve()
        if src.parent != config.SAMPLES_DIR.resolve() or not src.exists():
            raise HTTPException(404, "Unknown sample")
        filename, source = src.name, "sample"
        dest = config.UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{src.name}"
        shutil.copy(src, dest)
    elif file:
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "invoice.pdf")
        content = await file.read()
        if not content.startswith(b"%PDF"):
            raise HTTPException(400, "Please upload a PDF invoice.")
        if len(content) > 20 * 1024 * 1024:
            raise HTTPException(400, "PDF is larger than 20 MB.")
        dest = config.UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{filename}"
        dest.write_bytes(content)
        source = "upload"
    else:
        raise HTTPException(400, "Provide a file or a sample name.")
    run_id = pipeline.start_run(dest, filename, source)
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str):
    if not db.get_run(run_id):
        raise HTTPException(404)

    async def gen():
        sent = 0
        idle = 0
        while True:
            events = pipeline.EVENTS.get(run_id)
            if events is None:  # server restarted: replay from DB
                run = db.get_run(run_id)
                for st in run.get("stages") or []:
                    yield f"data: {json.dumps({'type': 'stage_done', **st}, default=str)}\n\n"
                yield f"data: {json.dumps({'type': 'run_done', 'decision': run.get('decision')})}\n\n"
                return
            while sent < len(events):
                ev = events[sent]
                sent += 1
                yield f"data: {json.dumps(ev, default=str)}\n\n"
                if ev["type"] == "run_done":
                    return
            idle += 1
            if idle % 40 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def list_runs():
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404)
    run.pop("file_path", None)
    return run


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404)
    return FileResponse(run["file_path"], media_type="application/pdf")


@app.post("/api/runs/{run_id}/rerun")
def rerun(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404)
    return {"run_id": pipeline.start_run(Path(run["file_path"]), run["filename"], "rerun")}


class Action(BaseModel):
    action: str
    note: str = ""


@app.post("/api/runs/{run_id}/action")
def run_action(run_id: str, body: Action):
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    try:
        return pipeline.human_action(run_id, body.action, body.note)
    except KeyError:
        raise HTTPException(404)


@app.get("/api/stats")
def stats():
    runs = [r for r in db.list_runs(10000) if r["status"] != "running"]
    by = {}
    for r in runs:
        by[r["final_status"] or r["decision"] or "ERROR"] = by.get(r["final_status"] or r["decision"] or "ERROR", 0) + 1
    auto = sum(1 for r in runs if r["decision"] == "APPROVE")
    durations = [r["duration_ms"] for r in runs if r["duration_ms"]]
    return {
        "total_runs": len(runs),
        "by_status": by,
        "auto_approval_rate": round(auto / len(runs) * 100) if runs else 0,
        "value_processed": round(sum(r["total"] or 0 for r in runs), 2),
        "value_approved": round(sum(r["total"] or 0 for r in runs if (r["final_status"] or r["decision"]) == "APPROVE"), 2),
        "value_blocked": round(sum(r["total"] or 0 for r in runs if (r["final_status"] or r["decision"]) in ("HOLD", "REJECT")), 2),
        "open_exceptions": sum(1 for r in runs if (r["final_status"] or r["decision"]) in ("REVIEW", "HOLD")),
        "avg_seconds": round(sum(durations) / len(durations) / 1000, 1) if durations else 0,
    }


@app.get("/api/reference")
def reference():
    pos = []
    for po in data.purchase_orders().values():
        billed = db.po_billed(po["po_number"])
        v = data.vendor_by_id(po["vendor_id"])
        pos.append({**{k: po[k] for k in ("po_number", "po_date", "status", "amount", "buyer", "description", "lines")},
                    "vendor": v["name"] if v else po["vendor_id"], "billed": billed,
                    "remaining": round(po["amount"] - billed, 2),
                    "invoices": [{k: r[k] for k in ("invoice_number", "invoice_date", "subtotal", "status", "source")}
                                 for r in db.ledger_rows(po_number=po["po_number"])]})
    return {"vendors": data.vendors(), "purchase_orders": pos, "policy": config.POLICY}


@app.post("/api/reset")
def reset():
    db.reset()
    pipeline.EVENTS.clear()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
