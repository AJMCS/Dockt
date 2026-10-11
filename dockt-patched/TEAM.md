# Dockt — team split: 4 lanes, black-box contracts

**Who this is for:** the four of us and each person's AI agent. Every lane is a black box: you own its files and its insides; everyone else only sees the **contracts** in §3 (HTTP endpoints, Python functions, JSON shapes). Build against the contracts and mocks, not against someone else's code.

This file replaces the work split in `PLAN.md` §4. The rules in `PLAN.md` §2 still apply (nothing to the cloud, test on the mock first, claim only what you verified), with one exception: the step-0 changes in §5 below are allowed.

---

## 1. Architecture: the app runs on each laptop, only the models run on the GX10

The **whole application** runs on each user's own computer (the demo is on **Windows**): capture, the local Dockt service (index, search, agent, API), the dashboard, and the tray / quick-chat. The **GX10 is a model server and nothing else.** It runs the vision, embedding, chat and speech models and is reached over the local network through OpenAI-compatible HTTP. Screenshots and the index are stored **only on the laptop**; the GX10 sees an image only for the second it takes to describe it, and stores nothing.

```
 ┌──────────────────────── USER LAPTOP (Windows) ──────────────────────────┐
 │                                                                          │
 │  LANE 1: CAPTURE + DESKTOP          LANE 2: APP (dashboard UI)           │
 │  capture.py, tray icon, hotkey,     chat, sources, screenshots,          │
 │  quick-chat, voice, launcher        timeline, promises                   │
 │        │  POST /ingest, /api/chat          │  GET/POST /api/*  (§3.1)    │
 │        ▼                                   ▼                              │
 │  ═══════════ local Dockt service: http://127.0.0.1:8000 (FastAPI) ══════  │
 │  LANE 3: AGENTIC LAYER              LANE 4 (laptop half): MEMORY         │
 │  chat agent + tools, ask, drafts,   SQLite + FTS5 + vectors, indexer,    │
 │  standup, promises, nudges  ──────▶ models.py client           (§3.2)   │
 │                                            │  screenshots + DB live here │
 └────────────────────────────────────────────┼─────────────────────────────┘
                                              │ OpenAI-compatible HTTP over the LAN (§3.3)
                                              ▼
 ┌──────────────── GX10 (models only) — LANE 4 (GX10 half) ─────────────────┐
 │  Ollama :11434/v1  vision (gemma4:26b) · embeddings (nomic-embed-text)   │
 │                    chat (nemotron3:33b)                                  │
 │  speech-to-text :8001/v1 (local Whisper)        nothing stored here      │
 └──────────────────────────────────────────────────────────────────────────┘
```

**Data flow:** screenshot → local `/ingest` → saved on the laptop → the laptop's indexer sends it to the GX10 vision model → summary + screen text → GX10 embedding model → vector → laptop SQLite. Questions: dashboard or quick-chat → local agent → local search + GX10 chat model → answer with citations to screenshots on the laptop.

**Consequences everyone should know:**
- Each laptop has its **own** memory (index of its own screens). There is no shared team index unless we add one later.
- The GX10 is shared by every laptop. Total screenshots/min from all laptops must stay under its throughput (≈28 img/min measured with gemma4 + Nemotron extraction running, see §3.3).
- The local service listens on `127.0.0.1` only, so laptop-internal calls need no token. The GX10's model ports are open to the LAN; that's acceptable on the demo network only.

---

## 2. The four lanes

### Lane 1 — Capture + Desktop client (laptop)
**Owns:** `agent/` and the Windows launcher/packaging.
**Builds:**
- Keep `capture.py` working on Windows (change detection, spool, exclusions, pause file); it posts to the **local** service (`http://127.0.0.1:8000/ingest`).
- Tray icon with status (capturing / paused / GX10 unreachable) and a pause toggle.
- Global hotkey (e.g. Ctrl+Space) → small quick-chat window that sends `POST /api/chat`, shows the answer with source thumbnails (`GET /img/{id}`), and has an "Open in Dockt" link to Lane 2's deep links (§3.1).
- Opening the dashboard: **Lane 2 ships a native desktop window** (`desktop/dockt_desktop.py`, see §3.1 "Desktop window"). The launcher and tray open Dockt by running it (or by calling its `/open` URL), not `msedge --app`.
- One-click start for the demo: a launcher that starts the local service, capture and tray, and points them at the GX10 (`start-dockt.bat` or a packaged exe).
- Voice (stretch): push-to-talk → `POST /api/transcribe` → text to `/api/chat`. Spoken answer (optional) with Windows' built-in speech (SAPI). No cloud speech.
- Show Lane 3's nudges as Windows notifications (stretch, `GET /api/nudges`).

**Inputs:** screen pixels, active app + window title, hotkey, mic. **Outputs:** `/ingest` + `/agent/ping` calls; the desktop UI and launcher.
**Develop alone with:** the local service on mock models (§6 A). Until `/api/chat` exists, call `/api/ask`. It returns the same `answer / sources / cited` fields.

### Lane 2 — App (dashboard)
**Lead:** Brandon Shen (GitHub `B92064`).
**Owns:** `server/static/`, or a new `web/` folder if you move to a framework.
**Tech:** your choice (plain JS, React/Vite, …). Node is fine on dev machines. For the demo, build to static files that the local service serves at `http://127.0.0.1:8000/`, so the launcher only has to start one thing. If you'd rather ship Electron/Tauri, agree it with Lane 1 first, since they own the window/tray shell.
**Builds:** the main page: a ChatGPT-style conversation with streaming answers, citation chips that open the screenshot + its summary/text, a sources side panel, search, a timeline of the day, the "Things you promised" panel, standup/timesheet buttons, and a draft (email/message) action on any answer.
**Inputs:** only the HTTP API in §3.1 (same origin, no CORS needed). **Outputs:** the UI, plus deep links other lanes use:
- `/#/capture/{id}`: open one capture
- `/#/chat?q=<text>`: open the chat with a question asked

**Develop alone with:** the local service on mock models + `tools/replay.py --synthetic 40` (§6 A), and the fixtures in `contracts/fixtures/` once they exist.

### Lane 3 — Agentic layer ("the brain", laptop)
**Owns:** the reasoning: `server/brain.py` after step 0 (today: the ask / draft / standup / commitments / extractor sections of `app.py`), and all reasoning prompts (`ASK_SYSTEM`, `DRAFT_SYSTEM`, `STANDUP_*`, `EXTRACT_SYSTEM`).
**What "agentic" means here:**
1. **Chat agent** (`POST /api/chat`): multi-turn conversation with history. The model can call tools over memory instead of the one-shot "search, then answer" of `/api/ask`. Tools to give it: `search_screens(q, start, end)`, `get_activity(start, end)`, `get_captures(ids)`, `list_commitments(...)`, `draft(ids, kind)`. It streams status ("searching your screens…"), tokens, then sources.
2. **Background agents:** promise extraction (exists), plus proactive nudges ("you told Priya you'd send the redline by Wed — due tomorrow"), and an end-of-day summary.
3. **Actions:** drafts, standup, timesheet (exist). Make them reachable from chat ("write that as an email").
4. **Quality:** every claim cites `[#id]`, it says "not found" instead of guessing, and it resolves times correctly ("this morning", "yesterday").

**Inputs:** `memory.*` and `models.chat*` (§3.2). **Outputs:** the agent endpoints in §3.1.
**Develop alone with:** the real GX10 models (§6 B) for prompt quality, the mock for plumbing.

### Lane 4 — Models (GX10) + Memory (laptop)
**Owns, GX10 half:** the model server: which models, Ollama settings (`/etc/systemd/system/ollama.service.d/`), speech-to-text server, throughput. Only Lane 4 changes or restarts anything on the GX10.
**Owns, laptop half:** `server/models.py` (the client for the GX10), `server/store.py`, the indexer loop (`server/indexer.py` after step 0), `tools/bench.py`, `tools/mock_models.py`.
**Builds:**
- GX10: expose the models on the LAN (§3.3), keep all of them loaded, add a local Whisper server for speech, measure img/min with several laptops at once.
- Perception: screenshot → `summary` + `text` (VLM prompt, or VLM + a dedicated OCR model if it beats it). Owns `VLM_PROMPT` and the SUMMARY/TEXT parser.
- Embeddings + hybrid search quality (vector + FTS5 + RRF).
- `chat_messages(...)` with tool calling + streaming for Lane 3, and `transcribe(...)` for Lane 1's voice.
- An **eval set**: ~20 real screenshots (including Windows apps) with expected facts, scored automatically (facts found / time per image). Pick models by these numbers, not by feel.
- Keep `mock_models.py` in sync with every model function so the other lanes can run without the GX10.

**Inputs:** JPEG + app + title (from local `/ingest`), audio bytes. **Outputs:** the `memory.*` and `models.*` functions in §3.2, the GX10 endpoints in §3.3, and the local endpoints `/ingest`, `/img/{id}`, `/api/search`, `/api/stats`, `/api/health`, `/api/transcribe`.
**Develop alone with:** `tools/bench.py`, the eval set, `tools/replay.py --folder`.

---

## 3. Contracts (the only things other lanes may rely on)

### 3.0 Shared JSON shapes
```jsonc
// Capture: one indexed screenshot. Image bytes at GET /img/{id}.
{"id": 13, "device": "brandon-laptop", "ts": 1791663158.158,   // unix seconds
 "app": "chrome.exe", "title": "Gmail", "summary": "…", "ocr": "…",  // ocr = screen text
 "status": "pending|processing|done|error", "score": 0.0333}         // score only in search results

// Span: a stretch of time on one window (activity log)
{"device": "…", "app": "…", "title": "…", "start": 1791660000.0, "end": 1791660900.0,
 "n": 12, "id": 42, "summary": "…"}                 // id = a representative capture

// Commitment: a promise / deadline / request pulled from a capture
{"id": 3, "capture_id": 13, "device": "…", "ts": 1791663158.1, "kind": "promise|deadline|request",
 "text": "Send the redline back by Wed Oct 15", "who": "me", "due": "Oct 15", "done": 0, "created_at": 1791663170.0}
```
Additive changes (new optional fields) are fine without asking. Renaming or removing a field, or changing a type, is a **contract change** (§4).

### 3.1 Local HTTP API (on each laptop, base `http://127.0.0.1:8000`)
| Endpoint | Owner | Status | In → Out |
|---|---|---|---|
| `POST /ingest` | L4 | exists | multipart `image` (jpeg), `device`, `ts`, `app`, `title` → `{"id"}` (202) |
| `POST /agent/ping` | L4 | exists | `{"device","session","checks","sent"}` → `{"ok":true}` |
| `GET /img/{id}` | L4 | exists | → `image/jpeg` |
| `GET /api/capture/{id}` | L4 | exists | → Capture |
| `GET /api/search?q=&k=&device=&start=&end=` | L4 | exists | → `{"results":[Capture], "ms"}` |
| `GET /api/browse?start=&end=&device=&limit=` | L4 | exists | → `[Capture]` |
| `GET /api/timeline?bucket=&device=` | L4 | exists | → `[{"device","b","n","first_id"}]` (b = bucket start, unix s) |
| `GET /api/stats`, `GET /api/health` | L4 | exists | counters; `{"models":{"vlm","embed","llm"}, "vlm_enabled"}`: health = "can this laptop reach the GX10 models" |
| `POST /api/transcribe` | L4 | **stub on main** (503 until `models.transcribe` exists) | multipart `audio` (wav/webm/m4a) → `{"text", "ms"}` |
| `POST /api/ask` | L3 | exists | `{"q","k"?,"device"?}` → `{"answer","sources":[Capture],"cited":[id],"window","ms"}` |
| `POST /api/chat` | L3 | **stub on main** (answers via ask, fakes the stream) | see below |
| `POST /api/draft` | L3 | exists | `{"ids":[id],"kind":"email|message|todo|notes","instruction"?}` → `{"draft","kind"}` |
| `GET /api/standup?kind=standup|timesheet&hours=&device=` | L3 | exists | → `{"text","kind","spans","captures","ms"}` |
| `GET /api/commitments?hours=&device=&include_done=` | L3 | exists | → `{"items":[Commitment]}` |
| `POST /api/commitments/{id}/done` | L3 | exists | `{"done":true}` → `{"ok":true}` |
| `GET /api/nudges?since=` | L3 | **stub on main** (empty list) | → `{"items":[{"id","ts","kind","text","capture_id","due"}]}` |

**`POST /api/chat`** (the main new contract, used by Lanes 1 and 2):
```jsonc
// request
{"messages": [{"role": "user", "content": "where is dinner tonight?"},
              {"role": "assistant", "content": "Nopalito at 8 [#9]"},
              {"role": "user", "content": "draft a text to Sam saying I'm in"}],
 "stream": true}
// stream: true → text/event-stream, one JSON object per `data:` line, in this order:
{"type": "status",  "text": "Searching your screens…"}     // 0..n, show as a hint
{"type": "token",   "text": "Dinner is at "}                 // 0..n, append to the answer
{"type": "sources", "items": [Capture]}                      // once, before done
{"type": "done",    "answer": "…full text…", "cited": [9, 1], "ms": 2875}
{"type": "error",   "text": "…"}                             // instead of done, on failure
// stream: false → one JSON body: {"answer", "sources", "cited", "ms"}  (same as /api/ask)
```
Until L3 ships it, consumers call `/api/ask` with the last user message. The non-stream shape is the same.

**Personalized next-question buttons (L2 + L3).**
Replace the fixed action buttons beneath chat answers with up to three next steps
generated by the local GX10 chat model from the conversation, including the latest
assistant answer. For example, a conversation about an NDA deadline might offer
"Draft Priya's NDA follow-up" rather than a generic "Draft message".

- L3 provides `POST /api/chat/suggestions`: request
  `{"messages":[{"role":"user|assistant","content":"..."}]}`; response
  `{"suggestions":[{"label":"Draft Priya's NDA follow-up","prompt":"Draft a follow-up to Priya about the NDA deadline we just discussed."}]}`.
- Generate zero to three distinct, short labels with complete prompts. Use only
  details present in the chat; suggestions may be questions or actions, and must
  not imply an email was sent or another action already performed. Use the existing
  local `models.chat` client; no cloud calls or new dependencies.
- L2 requests suggestions after an answer finishes, without delaying answer
  streaming. Clicking a button sends its prompt through the normal chat flow,
  with conversation history, so the existing agent can search or draft as needed.
- Save suggestions with their assistant turn for chat-history rendering. Ignore
  late responses after switching chats or starting another turn. Render labels as
  text, validate the response, and hide the buttons on failure or empty output;
  do not substitute generic actions. Screenshot-modal drafting is unchanged.
- Verify with a mocked model: different chats produce different suggestions;
  clicks include the conversation history; malformed responses, model outages,
  reloads and chat switches do not break chat or show stale suggestions. Review
  relevance separately on the GX10 before claiming model quality.

**Desktop window** (L2, `desktop/dockt_desktop.py`, needs `requirements-desktop.txt` = pywebview; WebView2 on Windows):
- `pythonw desktop\dockt_desktop.py [--mock] [--no-capture] [--open "#/capture/9"]`, or double-click `desktop\Dockt.bat` (sets up `.venv` on first run). `desktop\install-shortcut.ps1` adds Desktop + Start menu shortcuts with the Dockt icon.
- Port: `DOCKT_PORT` if set (start-dockt.bat sets it), else a Dockt already answering in 8000–8099, else the first free port there (same rule as `agent/find_port.py`). If no Dockt answers, it starts `server/app.py` and `agent/capture.py` itself (GX10 from `gx10.txt` / `DOCKT_GX10`, same env defaults as `start-dockt.bat`), and stops them when the window closes. If the service is already running (e.g. started by the launcher), it only opens the window.
- One window only. To show it, optionally on a deep link: `GET http://127.0.0.1:8765/open?hash=%23%2Fcapture%2F9` → `ok` (brings the window up and navigates), or run it again with `--open`. This is what the tray's "Open Dockt" and the quick-chat's chips / "Open in Dockt" should call. Suggested for `agent/quickchat.py` `open_dockt()`: try this URL first, fall back to `msedge --app` when it doesn't answer; and in `start-dockt.bat`, replace `start "" msedge --app=%DOCKT_URL%` with `start "" .venv\Scripts\pythonw.exe desktop\dockt_desktop.py` (it reuses the service the launcher started).
- Logs in `~/.dockt/logs/`; chat history + theme persist in `~/.dockt/webview`.

### 3.2 Python interfaces (inside the local service; L4 provides, L3 consumes)
```python
# memory: today these are Store methods (store.py); after step 0, server/memory.py re-exports them
search(q: str, k=24, device=None, start=None, end=None) -> list[Capture]   # hybrid; embeds q itself
get(id: int) -> Capture | None
many(ids: list[int]) -> list[Capture]                                       # keeps order
activity(start: float, end: float, device=None, max_spans=120) -> tuple[list[Span], int, int]  # spans, omitted, total
commitments(start=None, end=None, device=None, include_done=True, limit=200) -> list[Commitment]
add_commitments(capture_row: dict, items: list[dict]) -> list[dict]         # dedups; returns the new ones
set_commitment_done(id: int, done: bool) -> None

# models (models.py): async clients for the GX10, all OpenAI-compatible underneath
await chat(system: str, user: str, max_tokens=700, temperature=0.2) -> str          # thinking already stripped
await chat_messages(messages: list[dict], tools: list[dict] | None = None,
                    max_tokens=700, temperature=0.2, stream=False)
      -> dict  # {"content": str, "tool_calls": [{"id","name","arguments": dict}]}   (stream=False)
      -> AsyncIterator[dict]  # {"type":"token","text"} … then {"type":"done","content","tool_calls"}  (stream=True)
await embed(texts: list[str]) -> list[list[float]]
await describe(jpeg: bytes, app: str, title: str) -> tuple[str, str]               # (summary, screen_text); L4-internal
await transcribe(audio: bytes, mime: str) -> str                                    # new, L4
```
`chat_messages` and `transcribe` are **new**. L4 adds them, plus mock versions in `tools/mock_models.py`, first. L3 builds against the mock until then.

### 3.3 GX10 model endpoints (L4 owns; only `models.py` calls these)
| Endpoint | Model | Notes |
|---|---|---|
| `http://<gx10-ip>:11434/v1/chat/completions` | `gemma4:26b` (vision, images as data URLs), `nemotron3:33b` (chat) | send `reasoning_effort: "none"`, or thinking models return empty `content` (handled in `models.py`) |
| `http://<gx10-ip>:11434/v1/embeddings` | `nomic-embed-text` | changing it means re-indexing every laptop |
| `http://<gx10-ip>:11434/v1/models` | — | used by `/api/health` |
| `http://<gx10-ip>:8001/v1/audio/transcriptions` | local Whisper (**new**) | OpenAI-style multipart `file` → `{"text"}` |

Current Ollama settings on the GX10: `OLLAMA_CONTEXT_LENGTH=16384`, `OLLAMA_NUM_PARALLEL=4`, `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_MAX_LOADED_MODELS=3`. **Still needed:** `OLLAMA_HOST=0.0.0.0` (today Ollama only answers on the GX10 itself) and an open firewall port 11434 on the LAN.
Measured on the GX10: vision ≈ 1.5 s per simple screenshot, ≈ 5.6 s for a dense one; ≈ 28 img/min end to end with extraction running; Ask ≈ 3 s.

### 3.4 Environment variables (the local service reads these; L4 documents defaults in README)
`VLM_URL/VLM_MODEL`, `EMBED_URL/EMBED_MODEL`, `LLM_URL/LLM_MODEL`, `STT_URL/STT_MODEL` (new), `DATA_DIR` (where this laptop's screenshots + DB live), `INGEST_CONCURRENCY`, `EXTRACT_ENABLED`. On a laptop: `VLM_URL=http://<gx10-ip>:11434/v1`, `VLM_MODEL=gemma4:26b`, `LLM_MODEL=nemotron3:33b`, `EMBED_MODEL=nomic-embed-text` (`EMBED_URL` and `LLM_URL` default to `VLM_URL`).

---

## 4. Working together

- **Branches:** `lane1/<feature>`, `lane2/<feature>`, `lane3/<feature>`, `lane4/<feature>`. Small PRs into `main`. `main` must always start and pass the mock smoke test (§6 A) **on Windows**.
- **Stay in your files.** If you need something from another lane, don't edit their code. Open a PR (or message) that changes the contract in this file first. The consuming lane's owner approves contract changes; additive optional fields don't need approval.
- **Ship the stub first.** When you add a new endpoint/function, merge a stub that returns a correctly shaped fake response on day one (and a mock in `mock_models.py` if it's a model call). Then other lanes can integrate immediately.
- **Fixtures:** put example responses in `contracts/fixtures/<endpoint>.json` (real outputs with the GX10 models). Lanes 1 and 2 build their UI from these.
- **Shared files** (`server/app.py` after step 0, `README.md`, this file, requirements): change them only in small, separate PRs, and say so in the team chat.
- **GX10 is shared and models-only:** nobody runs app code on it, and only Lane 4 changes its config. Everyone else just points `VLM_URL` at it.
- **Windows first:** use `pathlib`, no Unix-only calls (`time.tzset`, `fcntl`, shell scripts without a `.bat`/PowerShell equivalent).

---

## 5. Step 0 (one person, ~30 min, before lanes start) — DONE Sat Oct 10 15:40 on branch `step0/split-app`

Result: `server/memory.py`, `server/indexer.py`, `server/brain.py` (APIRouter, plus stubs for `/api/chat`, `/api/nudges`), thin `server/app.py` (tzset guarded, `/api/transcribe` stub, `/static/` mount, UTF-8 index.html, `python app.py` binds 127.0.0.1). Mock smoke test passed on Linux; **not yet run on Windows**. Plan and lane cards: `team/ORCHESTRATION.md`, `team/lane1..4.md`.

1. **Windows fix:** `server/app.py` calls `time.tzset()`, which doesn't exist on Windows, so the service crashes at startup there. Guard it (`if hasattr(time, "tzset")`); a laptop's clock is already in local time.
2. **Split `server/app.py` by owner** (it mixes Lane 3 and Lane 4 code, which guarantees merge conflicts). One mechanical PR, no behavior change:
   - `server/indexer.py` (L4): `process`, `indexer`
   - `server/brain.py` (L3): ask / chat / draft / standup / commitments / extractor + their prompts, as a FastAPI `APIRouter`
   - `server/memory.py` (L4): thin re-export of the `Store` methods in §3.2
   - `server/app.py` (shared): app creation, lifespan (starts the indexer + extractor), `/ingest`, `/agent/ping`, read API, `include_router(...)`, static files
3. **Default bind + data dir for laptops:** service on `127.0.0.1`, `DATA_DIR` default under the user's home (e.g. `~/.dockt/data`) instead of the current folder.
4. Verify with the mock smoke test on a Windows machine before merging (including that SQLite FTS5 works in that Python build).

---

## 6. Running things (on your own laptop, Windows PowerShell)

```powershell
cd dockt-patched
py -m venv .venv
.venv\Scripts\pip install -r requirements-server.txt -r requirements-agent.txt

# A) No GX10 needed: mock models (plumbing, UI work). Terminal 1:
.venv\Scripts\uvicorn --app-dir tools mock_models:app --port 9000
# Terminal 2:
$env:VLM_URL="http://127.0.0.1:9000/v1"; .venv\Scripts\python server\app.py    # binds 127.0.0.1:8000; DATA_DIR defaults to ~\.dockt\data
# Terminal 3: fake data, then open http://127.0.0.1:8000
.venv\Scripts\python tools\replay.py --server http://127.0.0.1:8000 --synthetic 24
# smoke test: /api/health all true, /api/stats done == total, /api/ask returns an answer with [#id]

# B) Real models on the GX10 (Terminal 2 instead of the mock):
$env:VLM_URL="http://<gx10-ip>:11434/v1"; $env:VLM_MODEL="gemma4:26b"; $env:LLM_MODEL="nemotron3:33b"; $env:EMBED_MODEL="nomic-embed-text"
.venv\Scripts\python server\app.py
# then real capture:
.venv\Scripts\python agent\capture.py --server http://127.0.0.1:8000 --name <you>
```
Use your own `DATA_DIR` for test data so it never mixes with your demo index. macOS/Linux: same commands with `.venv/bin/` and `export`.

---

## 7. Milestones

| | Lane 1 Capture/Desktop | Lane 2 App | Lane 3 Agentic | Lane 4 Models/Memory |
|---|---|---|---|---|
| **M0 contracts** | tray icon + hotkey opens a window on Windows | chat page calling `/api/ask` | `/api/chat` stub (correct shapes, fake stream) | step 0; GX10 reachable on LAN; mocks for `chat_messages`, `transcribe` |
| **M1 works alone** | quick chat works on mock; pause/status; launcher | streaming chat, citation → screenshot, promises panel | real tool-calling agent on GX10 models | eval set + chosen models; `chat_messages` real; Whisper on GX10 |
| **M2 integrated** | voice → transcribe → chat; nudges | deep links; draft from answer; timeline | nudges + end-of-day summary | GX10 keeps up with all demo laptops at once; search quality numbers |
| **M3 demo** | one-click start on the demo Windows laptops | polished, no error boxes | 5 rehearsed questions correct 3× in a row | GX10 boots straight into the demo model config |

---

## 8. Agent kickoff prompt

Each person pastes this into their agent (Claude Fable or similar) at the start of a session, after filling in the brackets.

```text
You are working on Dockt, a private "work memory" desktop app. The whole app runs on each user's own computer (the demo is on Windows): a capture agent saves a screenshot whenever the screen changes, a local FastAPI service on 127.0.0.1:8000 indexes them, and a dashboard + tray quick-chat let the user ask questions about what they saw. The only thing not on the laptop is the AI models: an ASUS Ascent GX10 (NVIDIA GB10, 128 GB unified memory) on the local network serves them through Ollama's OpenAI-compatible API: a vision model turns each screenshot into a summary + screen text, an embedding model makes it searchable (with SQLite FTS5 on the laptop), and Nemotron answers questions with citations to the exact screenshots, drafts follow-ups, writes standups/timesheets, and pulls out "things you promised". Screenshots and the index stay on the laptop; the GX10 stores nothing. Hard rule: nothing ever leaves the local network — no cloud APIs, no telemetry, no hosted models.

The team works in 4 lanes, each a black box with fixed contracts:
1 Capture + Desktop client (agent/: capture, tray icon, hotkey quick-chat, voice, Windows launcher)
2 App (dashboard UI served by the local service: ChatGPT-style chat with sources, screenshots, timeline, promises)
3 Agentic layer (reasoning in the local service: /api/chat tool-calling agent, ask, drafts, standup, promise extraction, nudges, reasoning prompts)
4 Models + Memory (GX10 model serving, models.py client, store.py/search, indexer, perception prompts, evals, transcription)

I am [NAME], working on LANE [N]. My task this session: [TASK, e.g. "build the hotkey quick-chat window against /api/ask"].

Before writing code:
1. Read README.md, TEAM.md (the architecture, lane split + contracts, it is the source of truth; where it disagrees with README/PLAN.md, TEAM.md wins), and PLAN.md §2 and §8 (rules, known fragile spots). Then read every file you will change.
2. Tell me in a few lines: what my lane owns, which contracts in TEAM.md §3 my task produces or consumes, what "done" means, and how you will verify it.

While working:
- Only edit files my lane owns (TEAM.md §2). Treat other lanes as black boxes: use them only through TEAM.md §3 contracts, and use the mocks/fixtures when the real thing doesn't exist yet. If my task needs a contract change, stop and write the proposed change to TEAM.md §3 for me to agree with the owning lane. Don't work around it in another lane's code.
- Don't break existing contract shapes. Adding optional fields is fine.
- Everything must run on Windows (no Unix-only calls; give PowerShell commands). Keep changes small and in the style of the surrounding code. No new dependencies unless the task needs one; say why.
- Verify on the mock pipeline (TEAM.md §6 A) before claiming anything works; use the real GX10 models (§6 B) when quality matters. Never test against a cloud model. Don't change anything on the GX10 unless I'm Lane 4.
- Work on a branch named lane[N]/<short-feature>. Commit in small steps. Don't push to main or open PRs unless I ask.

When you finish, report: what you changed (files/functions), what you ran and what you saw, what you did not verify, and anything another lane needs to know (contract additions, new env vars, new fixtures).
```
