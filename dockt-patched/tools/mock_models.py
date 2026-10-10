"""
Fake OpenAI-compatible model server so you can build/test the pipeline and UI
with no GPU and no model downloads.

  uvicorn mock_models:app --port 9000
  export VLM_URL=http://localhost:9000/v1   (EMBED_URL / LLM_URL default to it)
"""
import hashlib
import re

import numpy as np
from fastapi import FastAPI, Request

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
    last = msgs[-1]["content"]
    if isinstance(last, list):  # vision request
        prompt = next(p["text"] for p in last if p["type"] == "text")
        app_ = re.search(r"Active app: (.*)", prompt).group(1)
        title = re.search(r"Window title: (.*)", prompt).group(1)
        out = f"SUMMARY: The user is in {app_} looking at {title}.\nTEXT: {title}"
    elif "Question:" in last:
        ids = re.findall(r"\[#(\d+)\]", last)[:2]
        out = "Mock answer: this came from " + " and ".join(f"[#{i}]" for i in ids) + "."
    elif "scan for commitments" in last:  # commitment extraction: fake one item when the text looks like one
        m = re.search(r"([^\n]*\b(?:due|by|I'll|booked|deadline)\b[^\n]*)", last[last.find("Screen text:"):], re.I)
        out = ('[{"kind": "deadline", "text": "%s", "who": "me", "due": ""}]' % m.group(1).strip()[:120].replace('"', "'")) if m else "[]"
    elif "ACTIVITY LOG" in last and "OPEN COMMITMENTS" in last:  # standup / timesheet
        n = len(re.findall(r"\[#\d+\]", last))
        out = f"**Done**\n- Mock standup built from {n} activity spans\n\n**Next**\n- Ship the demo\n\n**Blocked**\n- nothing"
    else:
        ids = re.findall(r"\[#(\d+)\]", last)
        out = f"Subject: Follow-up\n\nHi [name],\n\nMock draft based on captures {', '.join(ids)}.\n\nBest,\n[you]"
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": out}}]}
