"""
Dockt local service — runs on each user's laptop (Windows first), binds 127.0.0.1.
The GX10 only serves models (see models.py / TEAM.md §3.3).

  python app.py                       # binds 127.0.0.1:8000
  uvicorn app:app --host 127.0.0.1 --port 8000

Owned by: everyone (shared glue). Lane code lives in
  indexer.py + memory.py + store.py + models.py   (Lane 4)
  brain.py                                       (Lane 3)
  static/                                        (Lane 2)

Env vars: see models.py for model endpoints, memory.py for DATA_DIR, plus
  DOCKT_TOKEN   if set, capture agents must send the same token
  DOCKT_HOST    bind address when run as `python app.py` (default 127.0.0.1)
  DOCKT_PORT    (default 8000)
"""
import asyncio
import datetime as dt
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

# All "this morning" / "today" / "Sat 14:03" logic uses the local clock. A laptop is
# already on local time; a headless Linux box may be on UTC, so allow pinning it there.
if hasattr(time, "tzset"):  # Unix only; Windows has no tzset and needs none
    # Windows' C runtime can't parse IANA names and would treat this TZ as UTC
    os.environ.setdefault("TZ", os.environ.get("DOCKT_TZ", "America/Los_Angeles"))
    time.tzset()

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import brain
import conversations
import indexer
import memory
import models
from memory import IMG_DIR, store

TOKEN = os.environ.get("DOCKT_TOKEN", "")
STATIC = Path(__file__).parent / "static"
CAPTURE_PAUSE_FILE = Path.home() / ".dockt_pause"


@asynccontextmanager
async def lifespan(_app):
    tasks = [asyncio.create_task(indexer.indexer())]
    if brain.EXTRACT_ENABLED:
        tasks.append(asyncio.create_task(brain.extractor()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Dockt", lifespan=lifespan)
app.include_router(brain.router)
app.include_router(conversations.router)


# ---------- ingest ----------
@app.post("/ingest", status_code=202)
async def ingest(image: UploadFile = File(...), device: str = Form(...), ts: float = Form(...),
                 app_name: str = Form("", alias="app"), title: str = Form(""),
                 x_dockt_token: str = Header("")):
    if TOKEN and x_dockt_token != TOKEN:
        raise HTTPException(401, "bad token")
    safe_dev = re.sub(r"[^\w\-]", "_", device)[:40] or "unknown"
    day = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    folder = IMG_DIR / safe_dev / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ts:.3f}.jpg"
    path.write_bytes(await image.read())
    cid = store.add(device, ts, app_name[:200], title[:500], str(path))
    return {"id": cid}


class Ping(BaseModel):
    device: str
    session: str
    checks: int
    sent: int


@app.post("/agent/ping")
async def agent_ping(p: Ping, x_dockt_token: str = Header("")):
    if TOKEN and x_dockt_token != TOKEN:
        raise HTTPException(401, "bad token")
    store.agent_ping(p.device[:40], p.session[:40], p.checks, p.sent)
    return {"ok": True}


# ---------- read API (memory) ----------
@app.get("/api/stats")
async def stats():
    return store.stats()


class CaptureControl(BaseModel):
    paused: bool


@app.get("/api/capture-control")
async def capture_control_status():
    return {"paused": CAPTURE_PAUSE_FILE.exists()}


@app.post("/api/capture-control")
async def set_capture_control(control: CaptureControl):
    if control.paused:
        CAPTURE_PAUSE_FILE.touch(exist_ok=True)
    else:
        CAPTURE_PAUSE_FILE.unlink(missing_ok=True)
    return {"paused": CAPTURE_PAUSE_FILE.exists()}


@app.get("/api/health")
async def health():
    return {"models": await models.health(), "vlm_enabled": indexer.VLM_ENABLED}


@app.get("/api/timeline")
async def timeline(bucket: int = 600, device: str | None = None):
    return store.timeline(bucket, device)


@app.get("/api/browse")
async def browse(start: float | None = None, end: float | None = None,
                 device: str | None = None, limit: int = 60):
    return store.browse(start, end, device, min(limit, 300))


@app.get("/api/capture/{cid}")
async def capture(cid: int):
    r = store.get(cid)
    if not r:
        raise HTTPException(404)
    r.pop("path", None)
    return r


@app.get("/img/{cid}")
async def img(cid: int):
    r = store.get(cid)
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404)
    return FileResponse(r["path"], media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/api/search")
async def search(q: str, k: int = 24, device: str | None = None,
                 start: float | None = None, end: float | None = None):
    t0 = time.time()
    results = await memory.search(q, min(k, 100), device, start, end)
    return {"results": results, "ms": round((time.time() - t0) * 1000)}


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """STUB (Lane 4): speech-to-text via the GX10 Whisper server. Shape per TEAM.md §3.1."""
    fn = getattr(models, "transcribe", None)
    if fn is None:
        raise HTTPException(503, "transcription not configured yet (models.transcribe missing)")
    t0 = time.time()
    text = await fn(await audio.read(), audio.content_type or "audio/wav")
    return {"text": text, "ms": round((time.time() - t0) * 1000)}


# ---------- dashboard (Lane 2): index.html at /, built assets under /static/ ----------
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("DOCKT_HOST", "127.0.0.1"),
                port=int(os.environ.get("DOCKT_PORT", "8000")))
