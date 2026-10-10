"""
Dockt server — runs on the GX10 (or a laptop for development).

  uvicorn app:app --host 0.0.0.0 --port 8000

Env vars: see models.py for model endpoints, plus
  DATA_DIR             where images + DB live (default ./data)
  DOCKT_TOKEN         if set, capture agents must send the same token
  INGEST_CONCURRENCY   parallel vision-model requests (default 8; raise on vLLM)
  VLM_ENABLED          set 0 to skip the vision model (index titles only)
"""
import asyncio
import datetime as dt
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

# All "this morning" / "today" / "Sat 14:03" logic uses the server's local clock.
# A fresh GX10 is usually on UTC, which is 7 hours off from the room. Pin it.
os.environ.setdefault("TZ", os.environ.get("DOCKT_TZ", "America/Los_Angeles"))
time.tzset()

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import models
from store import Store

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
IMG_DIR = DATA_DIR / "img"
IMG_DIR.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ.get("DOCKT_TOKEN", "")
CONCURRENCY = int(os.environ.get("INGEST_CONCURRENCY", "8"))
VLM_ENABLED = os.environ.get("VLM_ENABLED", "1") == "1"
STATIC = Path(__file__).parent / "static"

store = Store(str(DATA_DIR / "dockt.db"))


# ---------- background indexing ----------
async def process(row, sem):
    async with sem:
        try:
            jpeg = Path(row["path"]).read_bytes()
            if VLM_ENABLED:
                summary, ocr = await models.describe(jpeg, row["app"], row["title"])
            else:
                summary, ocr = "", ""
            doc = f"{row['app']} — {row['title']}\n{summary}\n{ocr}"[:4000]
            (emb,) = await models.embed([doc])
            store.complete(row["id"], summary, ocr, emb)
        except Exception as e:
            print(f"index error id={row['id']}: {e!r}")
            store.fail(row["id"], repr(e))


async def indexer():
    sem = asyncio.Semaphore(CONCURRENCY)
    inflight: set[asyncio.Task] = set()
    while True:
        free = CONCURRENCY * 2 - len(inflight)
        rows = store.claim(free) if free > 0 else []
        for r in rows:
            t = asyncio.create_task(process(r, sem))
            inflight.add(t)
            t.add_done_callback(inflight.discard)
        await asyncio.sleep(0.2 if rows else 1.0)


# ---------- background commitment extraction (Nemotron, runs beside the vision pass) ----------
EXTRACT_ENABLED = os.environ.get("EXTRACT_ENABLED", "1") == "1"
EXTRACT_CONCURRENCY = int(os.environ.get("EXTRACT_CONCURRENCY", "4"))

EXTRACT_SYSTEM = """You read text captured from one person's computer screen and pull out COMMITMENTS:
- promise: something the person said they will do ("I'll send the deck by Friday", "will follow up tomorrow")
- deadline: a due date or time that applies to them ("due Oct 30", "submission closes Sun 1:30 PM")
- request: something someone else asked them to do that is still open ("can you review the redline?")
Return ONLY a JSON array, no prose. Each item: {"kind": "promise"|"deadline"|"request", "text": "<one specific line, keep names/numbers/dates>", "who": "<who owes it: 'me' or a name>", "due": "<date/time as written, or ''>"}
Rules: only concrete actions or dates; skip menus, navigation, ads, boilerplate, code, and anything marked done. At most 4 items. If nothing qualifies, return []."""


def _parse_items(text: str) -> list[dict]:
    text = text or ""
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        data = json.loads(text[i:j + 1])
    except json.JSONDecodeError:
        return []
    out = []
    for it in data if isinstance(data, list) else []:
        if isinstance(it, dict) and isinstance(it.get("text"), str) and it["text"].strip():
            out.append(it)
    return out[:4]


async def extract_one(row, sem):
    async with sem:
        try:
            user = (f"Screen capture to scan for commitments.\n"
                    f"App: {row['app']} — {row['title']}\nSeen: {_fmt_time(row['ts'])}\n"
                    f"Summary: {row['summary']}\nScreen text:\n{(row['ocr'] or '')[:1500]}")
            out = await models.chat(EXTRACT_SYSTEM, user, max_tokens=300, temperature=0)
            added = store.add_commitments(row, _parse_items(out))
            store.mark_extracted(row["id"], ok=True)
            for a in added:
                print(f"promise found in #{row['id']}: {a['text']}")
        except Exception as e:
            print(f"extract error id={row['id']}: {e!r}")
            store.mark_extracted(row["id"], ok=True)  # don't retry forever; the capture is still searchable


async def extractor():
    sem = asyncio.Semaphore(EXTRACT_CONCURRENCY)
    inflight: set[asyncio.Task] = set()
    while True:
        free = EXTRACT_CONCURRENCY * 2 - len(inflight)
        rows = store.claim_extract(free) if free > 0 else []
        for r in rows:
            t = asyncio.create_task(extract_one(r, sem))
            inflight.add(t)
            t.add_done_callback(inflight.discard)
        await asyncio.sleep(0.3 if rows else 1.0)


@asynccontextmanager
async def lifespan(_app):
    tasks = [asyncio.create_task(indexer())]
    if EXTRACT_ENABLED:
        tasks.append(asyncio.create_task(extractor()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Dockt", lifespan=lifespan)


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


# ---------- read API ----------
@app.get("/api/stats")
async def stats():
    return store.stats()


@app.get("/api/health")
async def health():
    return {"models": await models.health(), "vlm_enabled": VLM_ENABLED}


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


async def _search(q, k, device=None, start=None, end=None):
    try:
        (qvec,) = await models.embed([q])
    except Exception as e:
        print(f"embed failed, keyword-only search: {e!r}")
        qvec = None
    return store.hybrid(q, qvec, k, device, start, end)


@app.get("/api/search")
async def search(q: str, k: int = 24, device: str | None = None,
                 start: float | None = None, end: float | None = None):
    t0 = time.time()
    results = await _search(q, min(k, 100), device, start, end)
    return {"results": results, "ms": round((time.time() - t0) * 1000)}


# ---------- ask + act (Nemotron) ----------
def _fmt_time(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%a %H:%M")


def _context(rows, max_chars=700):
    blocks = []
    for r in sorted(rows, key=lambda r: r["ts"]):
        blocks.append(
            f"[#{r['id']}] {_fmt_time(r['ts'])} · {r['device']} · {r['app']} — {r['title']}\n"
            f"Summary: {r['summary']}\nScreen text: {(r['ocr'] or '')[:max_chars]}")
    return "\n\n".join(blocks)


def time_window(q: str, now: dt.datetime | None = None):
    """Pull a time range out of the question. Returns (start, end, explicit)."""
    now = now or dt.datetime.now()
    ql = q.lower()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.search(r"(?:last|past)\s+(\d+)\s*(hour|hr|minute|min)", ql)
    if m:
        n = int(m.group(1)) * (3600 if m.group(2).startswith("h") else 60)
        return now.timestamp() - n, now.timestamp(), True
    if re.search(r"(?:last|past)\s+(?:an?\s+)?hour", ql):
        return now.timestamp() - 3600, now.timestamp(), True
    if "yesterday" in ql:
        return (day - dt.timedelta(days=1)).timestamp(), day.timestamp(), True
    for words, (h0, h1) in ((("this morning", "morning"), (5, 12)),
                            (("this afternoon", "afternoon", "lunch"), (12, 17)),
                            (("tonight", "this evening", "evening"), (17, 24))):
        if any(w in ql for w in words):
            return (day + dt.timedelta(hours=h0)).timestamp(), (day + dt.timedelta(hours=h1)).timestamp(), True
    if "today" in ql:
        return day.timestamp(), now.timestamp(), True
    return now.timestamp() - 12 * 3600, now.timestamp() + 60, False


def _activity_text(spans, omitted, total):
    lines = []
    for sp in spans:
        t0 = dt.datetime.fromtimestamp(sp["start"]).strftime("%H:%M")
        t1 = dt.datetime.fromtimestamp(sp["end"]).strftime("%H:%M")
        line = f"{t0}–{t1} · {sp['device']} · {sp['app']} — {sp['title']} ({sp['n']} capture{'s' if sp['n'] != 1 else ''}) [#{sp['id']}]"
        if sp["summary"]:
            line += f"\n    {sp['summary']}"
        lines.append(line)
    head = f"{total} captures in this period, merged into {len(spans)} spans"
    if omitted:
        head += f" ({omitted} brief spans omitted)"
    return head + ":\n" + "\n".join(lines)


ASK_SYSTEM = """You answer questions about what the user saw and did on their own screens, using ONLY the material provided.
You get two things:
1. ACTIVITY LOG: a timeline of which app/window was open when. Use it for "what did I do / when / how long" questions.
2. DETAILED CAPTURES: the screens most relevant to the question, with their text. Use them for specific facts.
Cite sources inline like [#123]. Be concise and specific (names, numbers, times, durations).
If the material doesn't contain the answer, say so plainly — never guess.
The current time is {now}."""

MAX_SPANS = int(os.environ.get("ASK_MAX_SPANS", "60"))
# Hard cap on prompt size so a long day can never push Nemotron past its context window.
# ~4 chars/token: 20k chars ≈ 5k tokens of context, leaving room for the answer in an 8k window.
ASK_MAX_CHARS = int(os.environ.get("ASK_MAX_CHARS", "20000"))
ASK_MAX_TOKENS = int(os.environ.get("ASK_MAX_TOKENS", "500"))


def _diverse(rows, per_window=2):
    """Real capture streams repeat the same window many times; keep at most N per window."""
    seen, out = {}, []
    for r in rows:
        key = (r["device"], r["app"], r["title"])
        if seen.get(key, 0) < per_window:
            seen[key] = seen.get(key, 0) + 1
            out.append(r)
    return out


class AskReq(BaseModel):
    q: str
    k: int = 10
    device: str | None = None


@app.post("/api/ask")
async def ask(req: AskReq):
    t0 = time.time()
    start, end, explicit = time_window(req.q)
    # the time window only shapes the activity log; the detailed search covers all time,
    # because "where is dinner tonight" was probably *seen* this morning
    rows = _diverse(await _search(req.q, req.k * 2, req.device))[:req.k]
    spans, omitted, total = store.activity(start, end, req.device, MAX_SPANS)
    if not rows and not spans:
        return {"answer": "Nothing indexed matches that yet.", "sources": [], "cited": []}
    system = ASK_SYSTEM.format(now=dt.datetime.now().strftime("%A %H:%M"))
    ctx = _context(rows) or "(none matched)"
    act = _activity_text(spans, omitted, total)
    if len(ctx) + len(act) > ASK_MAX_CHARS:  # detailed captures matter more than the log's tail
        act = act[:max(2000, ASK_MAX_CHARS - len(ctx))] + "\n(... log truncated)"
    user = f"ACTIVITY LOG\n{act}\n\nDETAILED CAPTURES\n\n{ctx}\n\nQuestion: {req.q}"
    answer = await models.chat(system, user, max_tokens=ASK_MAX_TOKENS)
    if not answer.strip():  # a reasoning model that spent its whole budget thinking
        answer = "The model returned an empty answer (likely reasoning mode). Check LLM_NO_THINK / context length."
    cited = list(dict.fromkeys(int(x) for x in re.findall(r"#(\d+)", answer)))
    # make sure every cited capture can be shown, even if it came from the activity log
    have = {r["id"] for r in rows}
    sources = rows + store.many([c for c in cited if c not in have])
    return {"answer": answer, "sources": sources, "cited": cited,
            "window": {"start": start, "end": end, "explicit": explicit, "spans": len(spans)},
            "ms": round((time.time() - t0) * 1000)}


DRAFT_KINDS = {
    "email": "a follow-up email (include a Subject: line)",
    "message": "a short Slack/text message",
    "todo": "a checklist of concrete next steps",
    "notes": "concise meeting/reading notes with key facts",
}

DRAFT_SYSTEM = """You turn what the user saw on their screen into useful next actions.
Write {kind} based ONLY on the screen captures provided. Use real names, numbers, links and dates from the captures.
If something needed is missing, leave a clear [placeholder]. Output only the draft, no preamble."""


class DraftReq(BaseModel):
    ids: list[int]
    kind: str = "email"
    instruction: str = ""


@app.post("/api/draft")
async def draft(req: DraftReq):
    rows = store.many(req.ids[:12])
    if not rows:
        raise HTTPException(404, "no captures")
    kind = DRAFT_KINDS.get(req.kind, DRAFT_KINDS["email"])
    user = f"Screen captures:\n\n{_context(rows, 1500)}\n\n"
    user += f"Instruction: {req.instruction}" if req.instruction else "Write the draft."
    text = await models.chat(DRAFT_SYSTEM.format(kind=kind), user, max_tokens=900, temperature=0.4)
    return {"draft": text, "kind": req.kind}


# ---------- things you promised + standup / timesheet ----------
@app.get("/api/commitments")
async def commitments(device: str | None = None, hours: float = 48, include_done: bool = True):
    now = time.time()
    return {"items": store.commitments(now - hours * 3600, now + 60, device, include_done)}


class DoneReq(BaseModel):
    done: bool = True


@app.post("/api/commitments/{cid}/done")
async def commitment_done(cid: int, req: DoneReq):
    store.set_commitment_done(cid, req.done)
    return {"ok": True}


STANDUP_KINDS = {
    "standup": ("a standup update with three short sections: **Done**, **Next**, **Blocked / waiting on**. "
                "Bullets, specific (names, tickets, documents), rough durations where the log shows them. "
                "Under 150 words."),
    "timesheet": ("a timesheet for the period: a markdown table with columns Time | Client or project | "
                  "What was done | Duration. Merge consecutive work on the same thing. Guess the client/project "
                  "from window titles and say [unclear] when you can't. End with the total hours."),
}

STANDUP_SYSTEM = """You write {kind}
Use ONLY the activity log and the commitments below, which were captured from the user's own screens.
Never invent work that isn't in the log. Output only the result, no preamble."""


@app.get("/api/standup")
async def standup(kind: str = "standup", device: str | None = None, hours: float | None = None):
    t0 = time.time()
    now = dt.datetime.now()
    if hours:
        start = now.timestamp() - hours * 3600
    else:  # the workday so far; before 6am, treat it as yesterday's day
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = (day - dt.timedelta(days=1) if now.hour < 6 else day).timestamp()
    end = now.timestamp() + 60
    spans, omitted, total = store.activity(start, end, device, 80)
    if not spans:
        return {"text": "No activity captured in this period yet.", "kind": kind, "ms": 0}
    commits = store.commitments(start, end, device, include_done=False, limit=40)
    clist = "\n".join(f"- [{c['kind']}] {c['text']}" + (f" (due {c['due']})" if c["due"] else "")
                      for c in commits) or "(none found)"
    user = (f"ACTIVITY LOG\n{_activity_text(spans, omitted, total)[:ASK_MAX_CHARS]}\n\n"
            f"OPEN COMMITMENTS\n{clist}\n\nPeriod: {_fmt_time(start)} to {_fmt_time(end)}.")
    text = await models.chat(STANDUP_SYSTEM.format(kind=STANDUP_KINDS.get(kind, STANDUP_KINDS["standup"])),
                             user, max_tokens=600, temperature=0.3)
    return {"text": text, "kind": kind, "spans": len(spans), "captures": total,
            "ms": round((time.time() - t0) * 1000)}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC / "index.html").read_text()
