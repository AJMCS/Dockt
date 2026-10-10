"""
Clients for the three models, all via OpenAI-compatible APIs (vLLM, Ollama,
llama.cpp server...). Switching laptop -> GX10 is just changing env vars.

  VLM_URL / VLM_MODEL       vision model: describes + OCRs each screenshot
  EMBED_URL / EMBED_MODEL   embedding model for search
  LLM_URL / LLM_MODEL       Nemotron (or any chat model): answers + drafts
  *_URL include the /v1 suffix, e.g. http://localhost:8001/v1
"""
import base64
import os
import re

import httpx

def _env(name, default):
    return os.environ.get(name, default)

VLM_URL = _env("VLM_URL", "http://localhost:11434/v1")
VLM_MODEL = _env("VLM_MODEL", "qwen2.5vl:7b")
EMBED_URL = _env("EMBED_URL", VLM_URL)
EMBED_MODEL = _env("EMBED_MODEL", "nomic-embed-text")
LLM_URL = _env("LLM_URL", VLM_URL)
LLM_MODEL = _env("LLM_MODEL", "nemotron-mini")
API_KEY = _env("OPENAI_API_KEY", "local")
# Some Nemotron builds reason by default; "/no_think" in the system prompt turns it off.
LLM_NO_THINK = _env("LLM_NO_THINK", "1") == "1"

_client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5),
                            headers={"Authorization": f"Bearer {API_KEY}"})


async def _complete(url: str, body: dict, no_think: bool) -> str:
    if no_think:
        # Ollama ignores chat_template_kwargs and puts a thinking model's whole reply in
        # "reasoning" (empty "content") unless reasoning_effort is "none".
        body = {**body, "reasoning_effort": "none"}
    r = await _client.post(f"{url}/chat/completions", json=body)
    if r.status_code == 400 and "reasoning_effort" in body:
        # Servers that only accept low/medium/high reject "none"; retry without it.
        body.pop("reasoning_effort")
        r = await _client.post(f"{url}/chat/completions", json=body)
    r.raise_for_status()
    return _strip_think(r.json()["choices"][0]["message"].get("content") or "")

VLM_PROMPT = """You are indexing a screenshot of a person's computer screen so they can search for it later.
Active app: {app}
Window title: {title}

Reply in exactly this format:
SUMMARY: 1-2 sentences on what the person is doing and what is on screen (app, page, document, people, topics).
TEXT: the most important visible text, verbatim — names, emails, numbers, dates, headings, messages. Max ~150 words. Skip menus and boilerplate."""


def _strip_think(s: str) -> str:
    return re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip()


async def describe(jpeg: bytes, app: str, title: str) -> tuple[str, str]:
    b64 = base64.b64encode(jpeg).decode()
    body = {
        "model": VLM_MODEL,
        "temperature": 0,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": VLM_PROMPT.format(app=app or "unknown", title=title or "unknown")},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
    }
    out = await _complete(VLM_URL, body, no_think=True)
    m = re.search(r"SUMMARY:\s*(.*?)(?:\n\s*TEXT:|$)", out, re.S | re.I)
    t = re.search(r"TEXT:\s*(.*)", out, re.S | re.I)
    summary = (m.group(1) if m else out).strip()
    text = (t.group(1) if t else "").strip()
    return summary, text


async def embed(texts: list[str]) -> list[list[float]]:
    r = await _client.post(f"{EMBED_URL}/embeddings",
                           json={"model": EMBED_MODEL, "input": texts})
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


async def chat(system: str, user: str, max_tokens: int = 700, temperature: float = 0.2) -> str:
    body = {
        "model": LLM_MODEL, "temperature": temperature, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    if LLM_NO_THINK:
        # Belt and braces: "/no_think" works for Nemotron Nano 2 style templates,
        # chat_template_kwargs for vLLM-served Nemotron 3 / Qwen3. Ollama ignores unknown fields.
        body["messages"][0]["content"] = "/no_think\n" + system
        body["chat_template_kwargs"] = {"enable_thinking": False}
    return await _complete(LLM_URL, body, no_think=LLM_NO_THINK)


async def health() -> dict:
    out = {}
    for name, url in (("vlm", VLM_URL), ("embed", EMBED_URL), ("llm", LLM_URL)):
        try:
            r = await _client.get(f"{url}/models", timeout=3)
            out[name] = r.is_success
        except Exception:
            out[name] = False
    return out
