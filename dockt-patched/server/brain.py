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


async def _warmup():
    """Load the chat model into memory at startup so the first real /api/chat
    isn't a 40-60s cold start. Best effort: a tiny tool-less turn, errors ignored."""
    try:
        t = time.time()
        await _chat_messages([{"role": "user", "content": "ready?"}], None, max_tokens=1, temperature=0)
        print(f"chat model warmed in {time.time() - t:.1f}s")
    except Exception as e:
        print(f"warmup skipped: {e!r}")


async def extractor():
    asyncio.create_task(_warmup())  # don't block the extractor loop on it
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


# ---------- chat agent (TEAM.md §3.1) ----------
# Multi-turn, tool-calling agent over this laptop's memory.
# Events (stream): status* → token* → sources → done | error.  Non-stream: the /api/ask shape.
class ChatMsg(BaseModel):
    role: str
    content: str


class ChatReq(BaseModel):
    messages: list[ChatMsg]
    stream: bool = False
    device: str | None = None
    conversation_id: str | None = None


CHAT_MAX_ROUNDS = int(os.environ.get("CHAT_MAX_ROUNDS", "4"))
CHAT_MAX_TOKENS = int(os.environ.get("CHAT_MAX_TOKENS", "700"))
CHAT_HISTORY = int(os.environ.get("CHAT_HISTORY", "8"))  # past messages sent to the model
TOOL_MAX_CHARS = 6000  # per tool result, keeps a 4-round conversation inside Nemotron's 16k

CHAT_SYSTEM = """You answer questions about what the user saw and did on their own computer screens.
You cannot see the screens directly: use the tools to look things up before answering.
- search_screens: find specific facts (names, numbers, places, messages). Leave start/end empty unless the user asks what they saw during a specific period: plans for "tonight" or "Friday" were usually seen earlier. Try a second, different query if the first finds nothing useful.
- get_activity: what the user worked on / when / for how long.
- list_commitments: promises, deadlines and requests found on screen.
- get_captures: re-read captures already cited earlier in the conversation.
- draft: write an email/message/todo/notes from captures. Return the draft text as your answer.
Rules:
- Every fact must cite the capture it came from as [#id] (one id per bracket, e.g. [#3] [#7]), using ONLY ids that appear in tool results.
- If the tools return nothing relevant, say "I didn't see that on your screens." Never guess.
- Be concise and specific: names, numbers, times, durations.
Times are local, written as YYYY-MM-DD HH:MM. Now is {now}."""

_TIME_PARAM = {"type": "string", "description": "local time, YYYY-MM-DD HH:MM"}
TOOLS = [
    {"type": "function", "function": {
        "name": "search_screens",
        "description": "Search the user's screen history (text and summaries). start/end filter by when the "
                       "screen was seen; omit them to search everything.",
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string", "description": "keywords, e.g. 'dinner reservation'"},
            "start": _TIME_PARAM, "end": _TIME_PARAM}, "required": ["q"]}}},
    {"type": "function", "function": {
        "name": "get_activity",
        "description": "Timeline of which apps and windows were open between start and end.",
        "parameters": {"type": "object", "properties": {"start": _TIME_PARAM, "end": _TIME_PARAM},
                       "required": ["start", "end"]}}},
    {"type": "function", "function": {
        "name": "get_captures",
        "description": "Full text of specific captures by id.",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}}},
    {"type": "function", "function": {
        "name": "list_commitments",
        "description": "Promises, deadlines and requests found on the user's screens.",
        "parameters": {"type": "object", "properties": {
            "hours": {"type": "number", "description": "look back this many hours (default 48)"},
            "include_done": {"type": "boolean"}}}}},
    {"type": "function", "function": {
        "name": "draft",
        "description": "Write a draft from captures. kind: email | message | todo | notes.",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "integer"}},
            "kind": {"type": "string", "enum": ["email", "message", "todo", "notes"]},  # = DRAFT_KINDS
            "instruction": {"type": "string"}}, "required": ["ids", "kind"]}}},
]


def _fmt_full(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _parse_time(v):
    """Tool time argument -> unix seconds. Accepts 'YYYY-MM-DD HH:MM', 'HH:MM' (today) or a number."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip()
    try:
        return float(v)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(v[:19], fmt).timestamp()
        except ValueError:
            pass
    if v.startswith("24:00") or v.endswith(" 24:00"):  # models like "…24:00" for midnight
        day = v[:10] if len(v) > 5 else dt.date.today().isoformat()
        return (dt.datetime.strptime(day, "%Y-%m-%d") + dt.timedelta(days=1)).timestamp()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", v)
    if m:
        return dt.datetime.now().replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                         second=0, microsecond=0).timestamp()
    raise ValueError(f"can't read time {v!r}, use YYYY-MM-DD HH:MM")


def _capture_line(r, max_chars=400):
    text = " ".join((r["ocr"] or "").split())[:max_chars]
    return f"[#{r['id']}] {_fmt_time(r['ts'])} · {r['app']} — {r['title']}: {r['summary']} | {text}"


def _tool_status(name, args):
    if name == "search_screens":
        return f"Searching your screens for '{args.get('q', '')}'…"
    if name == "get_activity":
        return "Reading your activity log…"
    if name == "get_captures":
        return "Opening " + ", ".join(f"#{i}" for i in args.get("ids", [])[:6]) + "…"
    if name == "list_commitments":
        return "Checking your commitments…"
    if name == "draft":
        return f"Drafting a {args.get('kind', 'draft')}…"
    return f"Running {name}…"


async def _run_tool(name, args, device, sources):
    """Execute one tool call. Returns compact text for the model; adds every capture it shows to sources."""
    def keep(rows):
        for r in rows:
            sources.setdefault(r["id"], r)
        return rows

    if name == "search_screens":
        q = str(args.get("q") or "").strip()
        if not q:
            return "error: q is required"
        rows = keep(_diverse(await memory.search(q, 24, device, _parse_time(args.get("start")),
                                                 _parse_time(args.get("end"))))[:12])
        return "\n".join(_capture_line(r) for r in rows) or "No matching captures."
    if name == "get_activity":
        start, end = _parse_time(args.get("start")), _parse_time(args.get("end"))
        if start is None or end is None:
            return "error: start and end are required"
        spans, omitted, total = memory.activity(start, end, device, MAX_SPANS)
        if not spans:
            return ("No activity captured in that period. If the user asked about a plan or event "
                    "(dinner, meeting, flight, deadline), call search_screens without start/end.")
        keep(memory.many([sp["id"] for sp in spans]))
        return _activity_text(spans, omitted, total)
    if name == "get_captures":
        ids = [int(i) for i in (args.get("ids") or [])][:12]
        rows = keep(memory.many(ids))
        return "\n\n".join(_capture_line(r, 1500) for r in rows) or "No such captures."
    if name == "list_commitments":
        now = time.time()
        hours = float(args.get("hours") or 48)
        items = memory.commitments(now - hours * 3600, now + 60, device, bool(args.get("include_done", False)))
        if not items:
            return "No commitments found."
        keep(memory.many(list(dict.fromkeys(c["capture_id"] for c in items))))
        return "\n".join(f"[#{c['capture_id']}] {_fmt_time(c['ts'])} · {c['kind']} · {c['who']}: {c['text']}"
                         + (f" (due {c['due']})" if c["due"] else "") + (" [done]" if c["done"] else "")
                         for c in items[:40])
    if name == "draft":
        ids = [int(i) for i in (args.get("ids") or [])][:12]
        rows = keep(memory.many(ids))
        if not rows:
            return "error: draft needs ids of captures returned by an earlier tool call"
        text = await _draft_text(rows, args.get("kind") or "message", args.get("instruction") or "")
        refs = " ".join(f"[#{r['id']}]" for r in rows)
        return f"Draft built from {refs}:\n\n{text}"
    return f"error: unknown tool {name}"


async def _chat_messages(messages, tools=None, max_tokens=700, temperature=0.2):
    """Non-stream chat_messages per TEAM.md §3.2. Uses Lane 4's models.chat_messages when it exists;
    TEMPORARY fallback until then: same OpenAI-compatible call through models' client and LLM_URL."""
    if hasattr(models, "chat_messages"):
        return await models.chat_messages(messages, tools=tools, max_tokens=max_tokens,
                                          temperature=temperature, stream=False)
    body = {"model": models.LLM_MODEL, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "reasoning_effort": "none"}
    if tools:
        body["tools"] = tools
    r = await models._client.post(f"{models.LLM_URL}/chat/completions", json=body)
    if r.status_code == 400:  # servers that reject reasoning_effort "none"
        body.pop("reasoning_effort")
        r = await models._client.post(f"{models.LLM_URL}/chat/completions", json=body)
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        a = fn.get("arguments") or {}
        if isinstance(a, str):
            try:
                a = json.loads(a) if a.strip() else {}
            except json.JSONDecodeError:
                a = {}
        calls.append({"id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""), "arguments": a})
    return {"content": models._strip_think(msg.get("content") or ""), "tool_calls": calls}


def _time_hint(q):
    start, end, explicit = time_window(q)
    if not explicit:
        return ""
    return (f"\n(Time hint: the period in this question is {_fmt_full(start)} to {_fmt_full(end)}. "
            f"Plans and events for this period were usually seen earlier: search for them without start/end.)")


_NEGATIVE = re.compile(r"didn'?t see|not(?:hing)? (?:found|on your screens)|"
                       r"no (?:requests?|results?|matching|record|information)", re.I)


def _finish(answer, sources):
    """Normalize [9] -> [#9] for ids we actually returned, drop [#id]s no tool returned,
    then cited = ids in the answer ∩ sources."""
    answer = re.sub(r"\[(#?\d+(?:\s*,\s*#?\d+)+)\]",  # [#1, #9] -> [#1] [#9]
                    lambda m: " ".join(f"[#{x.strip().lstrip('#')}]" for x in m.group(1).split(",")), answer)
    answer = re.sub(r"\[(\d+)\]", lambda m: f"[#{m.group(1)}]" if int(m.group(1)) in sources else m.group(0),
                    answer)
    answer = re.sub(r"\s?\[#(\d+)\]", lambda m: m.group(0) if int(m.group(1)) in sources else "", answer)
    # a "didn't see / nothing found" answer shouldn't carry citations, even if the model tacked some on
    if _NEGATIVE.search(answer):
        answer = re.sub(r"\s?\[#\d+\]", "", answer)
    cited = [i for i in dict.fromkeys(int(x) for x in re.findall(r"\[#(\d+)\]", answer)) if i in sources]
    ordered = [sources[i] for i in cited] + [r for i, r in sources.items() if i not in cited]
    return answer, cited, ordered[:max(12, len(cited))]


async def _agent(req: ChatReq):
    """The agent loop as an event stream (dicts). Both /api/chat modes consume it."""
    t0 = time.time()
    history = [m for m in req.messages if m.role in ("user", "assistant") and m.content.strip()]
    if not history or history[-1].role != "user":
        raise HTTPException(400, "the last message must be from the user")
    history = history[-CHAT_HISTORY:]
    msgs = [{"role": "system", "content": CHAT_SYSTEM.format(now=dt.datetime.now().strftime("%A %Y-%m-%d %H:%M"))}]
    msgs += [{"role": m.role, "content": m.content} for m in history]
    msgs[-1]["content"] += _time_hint(history[-1].content)

    sources: dict[int, dict] = {}
    drafted: list[int] = []  # captures a draft was built from; the model tends to drop their citations
    searched = False         # did the model ever actually search the screens?
    forced = False           # have we already forced one fallback search?
    answer = ""
    for rnd in range(CHAT_MAX_ROUNDS + 1):
        last = rnd == CHAT_MAX_ROUNDS
        if last:  # out of tool rounds: force an answer from what was found
            msgs.append({"role": "user", "content": "Answer now using only the tool results above."})
        out = await _chat_messages(msgs, None if last else TOOLS, CHAT_MAX_TOKENS, 0.1)
        calls = out.get("tool_calls") or []
        if not calls:
            answer = (out.get("content") or "").strip()
            # the model gave up without ever searching — do one keyword search for it, then let it answer again
            if not last and not forced and not searched and _NEGATIVE.search(answer):
                forced = True
                q = history[-1].content
                yield {"type": "status", "text": _tool_status("search_screens", {"q": q})}
                result = await _run_tool("search_screens", {"q": q}, req.device, sources)
                searched = True
                print(f"chat tool search_screens(fallback {q!r}) -> {len(result)} chars")
                msgs.append({"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_fallback", "type": "function",
                    "function": {"name": "search_screens", "arguments": json.dumps({"q": q})}}]})
                msgs.append({"role": "tool", "tool_call_id": "call_fallback", "content": result[:TOOL_MAX_CHARS]})
                continue
            break
        msgs.append({"role": "assistant", "content": out.get("content") or "", "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]})
        for c in calls:
            if c["name"] == "search_screens":
                searched = True
            yield {"type": "status", "text": _tool_status(c["name"], c["arguments"])}
            try:
                result = await _run_tool(c["name"], c["arguments"], req.device, sources)
            except Exception as e:  # a bad argument shouldn't kill the turn; let the model retry
                result = f"error: {e}"
            if c["name"] == "draft" and result.startswith("Draft built from"):
                drafted += [int(i) for i in re.findall(r"\[#(\d+)\]", result.split("\n", 1)[0])]
            print(f"chat tool {c['name']}({json.dumps(c['arguments'], ensure_ascii=False)}) -> {len(result)} chars")
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result[:TOOL_MAX_CHARS]})

    if not answer:
        answer = "I didn't see that on your screens."
    if drafted and not any(f"[#{i}]" in answer for i in drafted):
        answer += "\n\n" + " ".join(f"[#{i}]" for i in dict.fromkeys(drafted))
    answer, cited, items = _finish(answer, sources)
    # the model's answer arrives whole (tool rounds are non-stream); chunk it for the UI
    for piece in re.findall(r"\S+\s*", answer):
        yield {"type": "token", "text": piece}
    yield {"type": "sources", "items": items}
    yield {"type": "done", "answer": answer, "cited": cited, "ms": round((time.time() - t0) * 1000)}


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@router.post("/api/chat")
async def chat(req: ChatReq):
    import conversations
    users = [m.content for m in req.messages if m.role == "user"]
    if not users:
        raise HTTPException(400, "no user message")
    ask_req = AskReq(q=users[-1], device=req.device)
    history = [m.model_dump() for m in req.messages]
    previous = conversations.get(req.conversation_id) if req.conversation_id else None
    if previous:
        for i, message in enumerate(history):
            saved = previous["messages"][i] if i < len(previous["messages"]) else {}
            if saved.get("role") == message["role"] and saved.get("content") == message["content"]:
                history[i] = {**saved, **message}
    cid = conversations.save(history, req.conversation_id)

    def remember(result):
        conversations.save(history + [{"role": "assistant", "content": result["answer"],
                                      "sources": result.get("sources", []),
                                      "cited": result.get("cited", []), "ms": result.get("ms", 0)}], cid)
        return {**result, "conversation_id": cid, "conversation_updated_at": conversations.get(cid)["ts"]}

    if not req.stream:
        result = {"sources": []}
        try:
            async for event in _agent(req):
                if event["type"] == "sources":
                    result["sources"] = event["items"]
                elif event["type"] == "done":
                    result.update(answer=event["answer"], cited=event["cited"], ms=event["ms"])
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, repr(e))
        return remember(result)

    agent = _agent(req)
    try:  # surface a bad request as HTTP 400 before the stream starts
        first = await agent.__anext__()
    except StopAsyncIteration:
        first = None
    except HTTPException:
        raise
    except Exception as e:
        first = {"type": "error", "text": repr(e)}

    async def gen():
        try:
            sources = first["items"] if first and first["type"] == "sources" else []
            if first and first["type"] == "done":
                done = remember({"answer": first["answer"], "cited": first["cited"],
                                 "ms": first["ms"], "sources": sources})
                first.update(conversation_id=cid,
                             conversation_updated_at=done["conversation_updated_at"])
            if first is not None:
                yield _sse(first)
                if first["type"] == "error":
                    return
            async for ev in agent:
                if ev["type"] == "sources":
                    sources = ev["items"]
                elif ev["type"] == "done":
                    done = remember({"answer": ev["answer"], "cited": ev["cited"],
                                     "ms": ev["ms"], "sources": sources})
                    ev.update(conversation_id=cid,
                              conversation_updated_at=done["conversation_updated_at"])
                yield _sse(ev)
                if ev["type"] == "token":
                    await asyncio.sleep(0.01)
        except Exception as e:
            yield _sse({"type": "error", "text": repr(e)})

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
    return {"draft": await _draft_text(rows, req.kind, req.instruction), "kind": req.kind}


async def _draft_text(rows, kind, instruction=""):
    """Shared by /api/draft and the chat agent's draft tool."""
    user = f"Screen captures:\n\n{_context(rows, 1500)}\n\n"
    user += f"Instruction: {instruction}" if instruction else "Write the draft."
    return await models.chat(DRAFT_SYSTEM.format(kind=DRAFT_KINDS.get(kind, DRAFT_KINDS["email"])),
                             user, max_tokens=900, temperature=0.4)


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
