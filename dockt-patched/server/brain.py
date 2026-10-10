"""
Brain (Lane 3): everything that reasons over memory with the chat model.

  ask / chat / draft / standup / commitments endpoints (FastAPI APIRouter),
  the background commitment extractor, and all reasoning prompts.

Reads memory only through memory.py (TEAM.md §3.2) and the GX10 only through
models.chat* — never store.py or httpx directly.

  EXTRACT_ENABLED / EXTRACT_CONCURRENCY   promise extraction loop
  ASK_MAX_SPANS / ASK_MAX_CHARS / ASK_MAX_TOKENS
"""
import asyncio
import datetime as dt
import json
import os
import re
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import memory
import models

router = APIRouter()


# ---------- shared helpers ----------
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


def _diverse(rows, per_window=2):
    """Real capture streams repeat the same window many times; keep at most N per window."""
    seen, out = {}, []
    for r in rows:
        key = (r["device"], r["app"], r["title"])
        if seen.get(key, 0) < per_window:
            seen[key] = seen.get(key, 0) + 1
            out.append(r)
    return out


# ---------- background commitment extraction (runs beside the vision pass) ----------
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
            added = memory.add_commitments(row, _parse_items(out))
            memory.store.mark_extracted(row["id"], ok=True)
            for a in added:
                print(f"promise found in #{row['id']}: {a['text']}")
        except Exception as e:
            print(f"extract error id={row['id']}: {e!r}")
            memory.store.mark_extracted(row["id"], ok=True)  # don't retry forever; the capture is still searchable


async def extractor():
    sem = asyncio.Semaphore(EXTRACT_CONCURRENCY)
    inflight: set[asyncio.Task] = set()
    while True:
        free = EXTRACT_CONCURRENCY * 2 - len(inflight)
        rows = memory.store.claim_extract(free) if free > 0 else []
        for r in rows:
            t = asyncio.create_task(extract_one(r, sem))
            inflight.add(t)
            t.add_done_callback(inflight.discard)
        await asyncio.sleep(0.3 if rows else 1.0)


# ---------- ask ----------
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


class AskReq(BaseModel):
    q: str
    k: int = 10
    device: str | None = None


@router.post("/api/ask")
async def ask(req: AskReq):
    t0 = time.time()
    start, end, explicit = time_window(req.q)
    # the time window only shapes the activity log; the detailed search covers all time,
    # because "where is dinner tonight" was probably *seen* this morning
    rows = _diverse(await memory.search(req.q, req.k * 2, req.device))[:req.k]
    spans, omitted, total = memory.activity(start, end, req.device, MAX_SPANS)
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
    sources = rows + memory.many([c for c in cited if c not in have])
    return {"answer": answer, "sources": sources, "cited": cited,
            "window": {"start": start, "end": end, "explicit": explicit, "spans": len(spans)},
            "ms": round((time.time() - t0) * 1000)}


# ---------- chat (TEAM.md §3.1) — STUB: Lane 3 replaces the body with the tool-calling agent ----------
# Until then it answers the last user message with ask(), and fakes the stream so Lanes 1 and 2
# can build against the real event sequence: status* → token* → sources → done | error.
class ChatMsg(BaseModel):
    role: str
    content: str


class ChatReq(BaseModel):
    messages: list[ChatMsg]
    stream: bool = False
    device: str | None = None


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@router.post("/api/chat")
async def chat(req: ChatReq):
    users = [m.content for m in req.messages if m.role == "user"]
    if not users:
        raise HTTPException(400, "no user message")
    ask_req = AskReq(q=users[-1], device=req.device)
    if not req.stream:
        return await ask(ask_req)

    async def gen():
        yield _sse({"type": "status", "text": "Searching your screens…"})
        try:
            r = await ask(ask_req)
        except Exception as e:
            yield _sse({"type": "error", "text": repr(e)})
            return
        for piece in re.findall(r"\S+\s*", r["answer"]):
            yield _sse({"type": "token", "text": piece})
            await asyncio.sleep(0.015)
        yield _sse({"type": "sources", "items": r["sources"]})
        yield _sse({"type": "done", "answer": r["answer"], "cited": r["cited"], "ms": r.get("ms", 0)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/nudges")
async def nudges(since: float | None = None):
    """STUB (Lane 3): proactive nudges. Shape per TEAM.md §3.1."""
    return {"items": []}


# ---------- draft ----------
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


@router.post("/api/draft")
async def draft(req: DraftReq):
    rows = memory.many(req.ids[:12])
    if not rows:
        raise HTTPException(404, "no captures")
    kind = DRAFT_KINDS.get(req.kind, DRAFT_KINDS["email"])
    user = f"Screen captures:\n\n{_context(rows, 1500)}\n\n"
    user += f"Instruction: {req.instruction}" if req.instruction else "Write the draft."
    text = await models.chat(DRAFT_SYSTEM.format(kind=kind), user, max_tokens=900, temperature=0.4)
    return {"draft": text, "kind": req.kind}


# ---------- things you promised + standup / timesheet ----------
@router.get("/api/commitments")
async def commitments(device: str | None = None, hours: float = 48, include_done: bool = True):
    now = time.time()
    return {"items": memory.commitments(now - hours * 3600, now + 60, device, include_done)}


class DoneReq(BaseModel):
    done: bool = True


@router.post("/api/commitments/{cid}/done")
async def commitment_done(cid: int, req: DoneReq):
    memory.set_commitment_done(cid, req.done)
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


@router.get("/api/standup")
async def standup(kind: str = "standup", device: str | None = None, hours: float | None = None):
    t0 = time.time()
    now = dt.datetime.now()
    if hours:
        start = now.timestamp() - hours * 3600
    else:  # the workday so far; before 6am, treat it as yesterday's day
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = (day - dt.timedelta(days=1) if now.hour < 6 else day).timestamp()
    end = now.timestamp() + 60
    spans, omitted, total = memory.activity(start, end, device, 80)
    if not spans:
        return {"text": "No activity captured in this period yet.", "kind": kind, "ms": 0}
    commits = memory.commitments(start, end, device, include_done=False, limit=40)
    clist = "\n".join(f"- [{c['kind']}] {c['text']}" + (f" (due {c['due']})" if c["due"] else "")
                      for c in commits) or "(none found)"
    user = (f"ACTIVITY LOG\n{_activity_text(spans, omitted, total)[:ASK_MAX_CHARS]}\n\n"
            f"OPEN COMMITMENTS\n{clist}\n\nPeriod: {_fmt_time(start)} to {_fmt_time(end)}.")
    text = await models.chat(STANDUP_SYSTEM.format(kind=STANDUP_KINDS.get(kind, STANDUP_KINDS["standup"])),
                             user, max_tokens=600, temperature=0.3)
    return {"text": text, "kind": kind, "spans": len(spans), "captures": total,
            "ms": round((time.time() - t0) * 1000)}
