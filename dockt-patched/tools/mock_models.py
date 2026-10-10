"""
Fake OpenAI-compatible model server so you can build/test the pipeline and UI
with no GPU and no model downloads.

  uvicorn mock_models:app --port 9000
  export VLM_URL=http://localhost:9000/v1   (EMBED_URL / LLM_URL default to it)
"""
import hashlib
import json
import re

import numpy as np
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

app = FastAPI()
DIM = 384


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
        out = f"SUMMARY: The user is in {app_} looking at {title}.\nTEXT: {title}"
        message = {"role": "assistant", "content": out}
        finish_reason = "stop"
    elif isinstance(last, str) and "Question:" in last:
        ids = re.findall(r"\[#(\d+)\]", last)[:2]
        out = "Mock answer: this came from " + " and ".join(f"[#{i}]" for i in ids) + "."
        message = {"role": "assistant", "content": out}
        finish_reason = "stop"
    elif isinstance(last, str) and "scan for commitments" in last:  # fake one item when text looks like one
        m = re.search(r"([^\n]*\b(?:due|by|I'll|booked|deadline)\b[^\n]*)", last[last.find("Screen text:"):], re.I)
        out = ('[{"kind": "deadline", "text": "%s", "who": "me", "due": ""}]' % m.group(1).strip()[:120].replace('"', "'")) if m else "[]"
        message = {"role": "assistant", "content": out}
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
