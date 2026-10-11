"""
Clients for the three models, all via OpenAI-compatible APIs (vLLM, Ollama,
llama.cpp server...). Switching laptop -> GX10 is just changing env vars.

  VLM_URL / VLM_MODEL       vision model: describes + OCRs each screenshot
  EMBED_URL / EMBED_MODEL   embedding model for search
  LLM_URL / LLM_MODEL       Nemotron (or any chat model): answers + drafts
  *_URL include the /v1 suffix, e.g. http://localhost:8001/v1
"""
import base64
import json
import mimetypes
import os
import re

import httpx

def _env(name, default):
    return os.environ.get(name, default)

VLM_URL = _env("VLM_URL", "http://localhost:11434/v1")
VLM_MODEL = _env("VLM_MODEL", "gemma4:26b")
EMBED_URL = _env("EMBED_URL", VLM_URL)
EMBED_MODEL = _env("EMBED_MODEL", "nomic-embed-text")
LLM_URL = _env("LLM_URL", VLM_URL)
LLM_MODEL = _env("LLM_MODEL", "nemotron3:33b")
STT_URL = _env("STT_URL", "http://localhost:8001/v1")
STT_MODEL = _env("STT_MODEL", "small")
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
SUMMARY: 1-2 sentences on what the person is doing and what is on screen (app, page, document, people, topics). Don't quote what is typed in an unsent message box.
TEXT: the most important visible text, verbatim — names, emails, numbers, dates, headings. For chats and emails, write each SENT or RECEIVED message on its own line as "Sender: message" (use "Me" for the person at this screen). Max ~150 words. Skip menus and boilerplate.
DRAFT: text typed in a message input or compose box that has NOT been sent yet, verbatim; "none" if there is none."""

# describe() keeps returning (summary, text); unsent compose-box text goes after this marker inside text, so search
# still finds it but the commitment pass can cut it off (a promise typed and deleted was never made)
DRAFT_MARK = "\n[Draft, not sent] "


def split_draft(text: str) -> tuple[str, str]:
    """(sent / received text, unsent draft) from a capture's screen text."""
    sent, _, draft = (text or "").partition(DRAFT_MARK)
    return sent, draft


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
    m = re.search(r"SUMMARY:\s*(.*?)(?:\n\s*(?:TEXT|DRAFT):|$)", out, re.S | re.I)
    t = re.search(r"TEXT:\s*(.*?)(?:\n\s*DRAFT:|$)", out, re.S | re.I)
    d = re.search(r"DRAFT:\s*(.*)", out, re.S | re.I)
    summary = (m.group(1) if m else out).strip()
    text = (t.group(1) if t else "").strip()
    draft = (d.group(1) if d else "").strip()
    if draft and not re.fullmatch(r"[\W_]*(?:none|n/?a|empty|nothing)?[\W_]*", draft, re.I):
        text += DRAFT_MARK + draft
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


def _chat_body(messages, tools, max_tokens, temperature, stream):
    # Copy the messages because callers commonly reuse a history for both the
    # tool-call turn and the final-answer turn.
    outgoing = [dict(message) for message in messages]
    body = {
        "model": LLM_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": outgoing,
        "stream": stream,
    }
    if tools is not None:
        body["tools"] = tools
    if LLM_NO_THINK:
        system_index = next((i for i, msg in enumerate(outgoing)
                             if msg.get("role") == "system"), None)
        if system_index is None:
            outgoing.insert(0, {"role": "system", "content": "/no_think"})
        else:
            content = outgoing[system_index].get("content") or ""
            if isinstance(content, str) and not content.startswith("/no_think"):
                outgoing[system_index] = {
                    **outgoing[system_index], "content": "/no_think\n" + content,
                }
        body["chat_template_kwargs"] = {"enable_thinking": False}
        body["reasoning_effort"] = "none"
    return body


def _append_delta(previous: str, piece: str) -> str:
    """Accept either token deltas or servers that repeat a cumulative value."""
    if not piece or piece == previous:
        return previous
    if previous and piece.startswith(previous):
        return piece
    return previous + piece


def _normalized_tool_calls(calls):
    normalized = []
    for call in calls:
        function = call.get("function") or call
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"model returned invalid arguments for tool {function.get('name', '')!r}"
                ) from exc
        if not isinstance(arguments, dict):
            raise ValueError(
                f"model returned non-object arguments for tool {function.get('name', '')!r}"
            )
        normalized.append({
            "id": call.get("id", ""),
            "name": function.get("name", ""),
            "arguments": arguments,
        })
    return normalized


class _ThinkFilter:
    """Remove <think> blocks without leaking tags split across stream chunks."""

    def __init__(self):
        self.buffer = ""
        self.inside = False

    def feed(self, text):
        self.buffer += text or ""
        visible = []
        start_tag, end_tag = "<think>", "</think>"
        while self.buffer:
            if self.inside:
                end = self.buffer.find(end_tag)
                if end < 0:
                    self.buffer = self.buffer[-(len(end_tag) - 1):]
                    break
                self.buffer = self.buffer[end + len(end_tag):]
                self.inside = False
                continue
            start = self.buffer.find(start_tag)
            if start >= 0:
                visible.append(self.buffer[:start])
                self.buffer = self.buffer[start + len(start_tag):]
                self.inside = True
                continue
            keep = len(start_tag) - 1
            if len(self.buffer) > keep:
                visible.append(self.buffer[:-keep])
                self.buffer = self.buffer[-keep:]
            break
        return "".join(visible)

    def finish(self):
        if self.inside:
            self.buffer = ""
            return ""
        visible, self.buffer = self.buffer, ""
        return visible


async def _stream_chat_messages(body):
    """Yield visible text deltas, then one normalized completion event."""
    content_parts = []
    tool_parts = {}
    think_filter = _ThinkFilter()
    request_body = dict(body)

    while True:
        retry_without_reasoning = False
        async with _client.stream(
                "POST", f"{LLM_URL}/chat/completions", json=request_body) as response:
            if response.status_code == 400 and "reasoning_effort" in request_body:
                await response.aread()
                request_body = dict(request_body)
                request_body.pop("reasoning_effort", None)
                retry_without_reasoning = True
            else:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content") or ""
                    if text:
                        content_parts.append(text)
                        visible = think_filter.feed(text)
                        if visible:
                            yield {"type": "token", "text": visible}
                    for part in delta.get("tool_calls") or []:
                        index = part.get("index", 0)
                        target = tool_parts.setdefault(index, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if part.get("id"):
                            target["id"] = _append_delta(target["id"], part["id"])
                        function = part.get("function") or {}
                        for key in ("name", "arguments"):
                            if function.get(key) is not None:
                                target["function"][key] = _append_delta(
                                    target["function"][key], str(function[key]))
                break
        if not retry_without_reasoning:
            break

    tail = think_filter.finish()
    if tail:
        yield {"type": "token", "text": tail}
    content = _strip_think("".join(content_parts))
    calls = _normalized_tool_calls([tool_parts[i] for i in sorted(tool_parts)])
    yield {"type": "done", "content": content, "tool_calls": calls}


async def chat_messages(messages: list[dict], tools: list[dict] | None = None,
                        max_tokens: int = 700, temperature: float = 0.2,
                        stream: bool = False):
    """Chat over an OpenAI-style message history, optionally yielding SSE tokens.

    For streaming calls, await this function to get an async iterator. It yields
    token events followed by one done event with the complete content and tools.
    """
    body = _chat_body(messages, tools, max_tokens, temperature, stream)
    if stream:
        return _stream_chat_messages(body)

    request_body = dict(body)
    response = await _client.post(f"{LLM_URL}/chat/completions", json=request_body)
    if response.status_code == 400 and "reasoning_effort" in request_body:
        request_body.pop("reasoning_effort", None)
        response = await _client.post(f"{LLM_URL}/chat/completions", json=request_body)
    response.raise_for_status()
    message = response.json()["choices"][0]["message"]
    return {
        "content": _strip_think(message.get("content") or ""),
        "tool_calls": _normalized_tool_calls(message.get("tool_calls") or []),
    }


async def transcribe(audio: bytes, mime: str) -> str:
    """Send audio bytes to the local OpenAI-compatible Whisper endpoint."""
    content_type = (mime or "application/octet-stream").split(";", 1)[0].strip()
    extension = mimetypes.guess_extension(content_type) or ".bin"
    response = await _client.post(
        f"{STT_URL}/audio/transcriptions",
        data={"model": STT_MODEL},
        files={"file": (f"audio{extension}", audio, content_type)},
    )
    response.raise_for_status()
    return response.json().get("text", "")


async def health() -> dict:
    out = {}
    for name, url in (("vlm", VLM_URL), ("embed", EMBED_URL), ("llm", LLM_URL)):
        try:
            r = await _client.get(f"{url}/models", timeout=3)
            out[name] = r.is_success
        except Exception:
            out[name] = False
    return out
