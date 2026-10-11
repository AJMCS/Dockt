"""
Fake OpenAI-compatible model server so you can build/test the pipeline and UI
with no GPU and no model downloads.

  uvicorn mock_models:app --port 9000
  export VLM_URL=http://localhost:9000/v1   (EMBED_URL / LLM_URL default to it)
"""
import asyncio
import base64
import hashlib
import io
import json
import os
import re

import numpy as np
from PIL import Image
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

app = FastAPI()
DIM = 384
# Simulate a busy GX10 for starvation tests: every text chat completion takes MOCK_LLM_DELAY seconds and at most
# MOCK_LLM_PARALLEL run at once (Ollama's OLLAMA_NUM_PARALLEL). Defaults: instant, unlimited.
LLM_DELAY = float(os.environ.get("MOCK_LLM_DELAY", "0"))
LLM_SLOTS = asyncio.Semaphore(int(os.environ.get("MOCK_LLM_PARALLEL", "1000")))
VLM_DELAY = float(os.environ.get("MOCK_VLM_DELAY", "0"))   # slow vision: frames finish indexing newest-first
CALLS = {"vision": 0, "extract": 0, "other": 0, "extract_inflight": 0}   # GET /mock/calls, for tests


@app.get("/mock/calls")
async def mock_calls():
    return CALLS


RETRACT = re.compile(r"never ?mind|no need|i'?ve got it|i'?ll do it|don'?t worry about", re.I)
COMMIT = re.compile(r"\b(?:due|by|I'll|booked|deadline|can you|could you)\b", re.I)


def _words(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _scene(prompt_parts):
    """tools/replay.py --scenario puts what's 'on screen' in the JPEG comment as JSON {"text", "draft"},
    because a mock can't read pixels. None for ordinary images."""
    for p in prompt_parts:
        if p.get("type") == "image_url":
            raw = base64.b64decode(p["image_url"]["url"].split(",", 1)[1])
            comment = Image.open(io.BytesIO(raw)).info.get("comment")
            if comment:
                try:
                    return json.loads(comment)
                except ValueError:
                    return None
    return None


def _mock_extract(prompt: str) -> str:
    """Stand-in for Nemotron on EXTRACT_SYSTEM: a retraction line cancels the newest open item; a line that
    looks like a commitment becomes a new item unless it repeats an open or closed one. It can't judge owners."""
    open_items = [(int(i), t) for i, t in re.findall(r"^- id=(\d+) \[[^\]]*\] (.*)$", prompt.split("CLOSED ITEMS")[0], re.M)]
    known = [t for _, t in open_items] + re.findall(r"^- id=\d+ \[[^\]]*\] (.*?)(?: -> .*)?$",
                                                    prompt.split("CLOSED ITEMS")[1].split("SCREEN TEXT")[0], re.M)
    screen = prompt.split("SCREEN TEXT", 1)[1].split("\n", 1)[-1]
    lines = [l.strip() for l in screen.splitlines() if l.strip()]
    new, updates = [], []
    retract = next((l for l in reversed(lines) if RETRACT.search(l)), None)
    if retract and open_items:
        updates.append({"id": open_items[-1][0], "status": "cancelled", "new_text": "", "reason": retract[:120]})
    for l in lines:
        if RETRACT.search(l) or not COMMIT.search(l):
            continue
        text = l.split(":", 1)[1].strip() if re.match(r"^[\w .'-]{1,30}:", l) else l
        w = _words(text)
        if any(w and len(w & _words(k)) / len(w | _words(k)) >= .6 for k in known):
            continue
        kind = "request" if re.search(r"can you|could you", l, re.I) else "promise" if "I'll" in l else "deadline"
        new.append({"kind": kind, "text": text[:120], "owner": "me", "due": ""})
    return json.dumps({"new": new[:4], "updates": updates})


def bow_embed(text: str):
    v = np.zeros(DIM, dtype=np.float32)
    for w in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % DIM] += 1 if (h >> 64) & 1 else -1
    n = np.linalg.norm(v)
    return (v / n if n else v).tolist()


@app.get("/v1/models")
async def models():
    return {"data": [{"id": "mock"}]}


@app.post("/v1/embeddings")
async def embeddings(req: Request):
    body = await req.json()
    inp = body["input"] if isinstance(body["input"], list) else [body["input"]]
    return {"data": [{"index": i, "embedding": bow_embed(t)} for i, t in enumerate(inp)]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    content = body["messages"][-1]["content"]
    kind = "vision" if isinstance(content, list) else "extract" if "OPEN ITEMS" in str(content) else "other"
    CALLS[kind] += 1
    if VLM_DELAY and kind == "vision":
        await asyncio.sleep(VLM_DELAY)
    if LLM_DELAY and kind != "vision":   # text model only
        CALLS["extract_inflight"] += kind == "extract"
        try:
            async with LLM_SLOTS:
                await asyncio.sleep(LLM_DELAY)
        finally:
            CALLS["extract_inflight"] -= kind == "extract"
    msgs = body["messages"]
    last_message = msgs[-1]
    last = last_message["content"]
    tools = body.get("tools") or []

    if last_message.get("role") == "user" and tools:
        functions = [tool.get("function", {}) for tool in tools]
        search_tool = next((fn for fn in functions if fn.get("name") == "search_screens"), None)
        if search_tool:
            question = last if isinstance(last, str) else ""
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_mock_search",
                    "type": "function",
                    "function": {
                        "name": "search_screens",
                        "arguments": json.dumps({"q": question}),
                    },
                }],
            }
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "Mock response."}
            finish_reason = "stop"
    elif last_message.get("role") == "tool":
        ids = re.findall(r"\[#(\d+)\]", str(last))
        citation = f" [#{ids[0]}]" if ids else ""
        message = {
            "role": "assistant",
            "content": f"Mock answer based on the supplied memory result.{citation}",
        }
        finish_reason = "stop"
    elif isinstance(last, list):  # vision request
        prompt = next(p["text"] for p in last if p["type"] == "text")
        app_ = re.search(r"Active app: (.*)", prompt).group(1)
        title = re.search(r"Window title: (.*)", prompt).group(1)
        scene = _scene(last)
        if scene:
            out = (f"SUMMARY: The user is in {app_} looking at {title}.\nTEXT: {scene.get('text') or title}\n"
                   f"DRAFT: {scene.get('draft') or 'none'}")
        else:
            out = f"SUMMARY: The user is in {app_} looking at {title}.\nTEXT: {title}"
        message = {"role": "assistant", "content": out}
        finish_reason = "stop"
    elif isinstance(last, str) and "Question:" in last:
        ids = re.findall(r"\[#(\d+)\]", last)[:2]
        out = "Mock answer: this came from " + " and ".join(f"[#{i}]" for i in ids) + "."
        message = {"role": "assistant", "content": out}
        finish_reason = "stop"
    elif isinstance(last, str) and "OPEN ITEMS" in last and "SCREEN TEXT" in last:  # commitment extraction
        message = {"role": "assistant", "content": _mock_extract(last)}
        finish_reason = "stop"
    elif isinstance(last, str) and "ACTIVITY LOG" in last and "OPEN COMMITMENTS" in last:  # standup / timesheet
        n = len(re.findall(r"\[#\d+\]", last))
        out = f"**Done**\n- Mock standup built from {n} activity spans\n\n**Next**\n- Ship the demo\n\n**Blocked**\n- nothing"
        message = {"role": "assistant", "content": out}
        finish_reason = "stop"
    else:
        ids = re.findall(r"\[#(\d+)\]", str(last))
        out = f"Subject: Follow-up\n\nHi [name],\n\nMock draft based on captures {', '.join(ids)}.\n\nBest,\n[you]"
        message = {"role": "assistant", "content": out}
        finish_reason = "stop"

    if body.get("stream"):
        async def events():
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                chunk = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                         "choices": [{"index": 0, "delta": {"tool_calls": [
                             {"index": i, **call} for i, call in enumerate(tool_calls)
                         ]}, "finish_reason": None}]}
                yield "data: " + json.dumps(chunk) + "\n\n"
            else:
                for start in range(0, len(content), 12):
                    chunk = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                             "choices": [{"index": 0,
                                          "delta": {"content": content[start:start + 12]},
                                          "finish_reason": None}]}
                    yield "data: " + json.dumps(chunk) + "\n\n"
            final = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
            yield "data: " + json.dumps(final) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return {"choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]}


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: str = Form("small")):
    # Consume the upload just like the local Whisper endpoint; the model name and
    # audio content intentionally do not affect this deterministic mock result.
    await file.read()
    return {"text": "mock transcript"}
