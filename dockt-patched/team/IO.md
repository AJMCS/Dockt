# Exact inputs and outputs per lane

Base URL for everything local: `http://127.0.0.1:8000`. All timestamps are unix seconds (float). All JSON is UTF-8. Shapes below are what the code on `step0/split-app` produces today; "NEW" marks what a lane still has to build. Adding optional fields is free; renaming or removing anything is a contract change (`TEAM.md` §4).

## Shared shapes

```jsonc
// Capture (store FIELDS). GET /api/capture/{id} adds "error": "". Search results add "score".
{"id": 9, "device": "LAPTOP-A", "ts": 1791663158.158, "app": "slack", "title": "#hackathon - Slack",
 "summary": "Sam confirms dinner at Nopalito at 8 and says he booked for 4.",
 "ocr": "Sam 1:12 PM dinner at Nopalito at 8, I booked for 4", "status": "done", "score": 0.0325}

// Span (activity log entry)
{"device": "LAPTOP-A", "app": "msedge", "title": "Invoice #4471 - Outlook", "start": 1791660000.0,
 "end": 1791660900.0, "n": 12, "id": 42, "summary": "Reading an invoice from ASUS due Oct 30"}

// Commitment
{"id": 3, "capture_id": 9, "device": "LAPTOP-A", "ts": 1791663158.158, "kind": "promise",
 "text": "Send the revised NDA by Monday", "who": "me", "due": "Monday", "done": 0, "created_at": 1791663170.0}
// kind ∈ promise | deadline | request ; done is 0/1 (SQLite int)
```

---

## Lane 1 — Capture + Desktop

### Inputs
| From | What | Exact form |
|---|---|---|
| OS | screen pixels, foreground window | `mss` grab of monitor 1; `(app, title)` from Win32 `GetForegroundWindow` → `("slack", "#hackathon - Slack")`; `app` is the exe name without `.exe` |
| user | pause | file `%USERPROFILE%\.dockt_pause` exists ⇒ no captures |
| config | GX10 address | `gx10.txt` next to the launcher (one line, `192.168.8.10`) or env `DOCKT_GX10` |
| service | health | `GET /api/health` → `{"models":{"vlm":true,"embed":true,"llm":true},"vlm_enabled":true}` (any `false` ⇒ tray shows "GX10 unreachable") |
| service | chat answer (quick-chat) | `POST /api/chat` with `"stream": false` → `{"answer":"Nopalito at 8, Sam booked it [#9]","sources":[Capture…],"cited":[9],"window":{…},"ms":2875}` |
| service | thumbnails | `GET /img/9` → `image/jpeg` |
| service (P2) | transcript | `POST /api/transcribe` → `{"text":"where is dinner tonight","ms":900}` |
| service (P2) | nudges | `GET /api/nudges` → `{"items":[{"id":1,"ts":…,"kind":"deadline","text":"NDA due tomorrow","capture_id":9,"due":"Monday"}]}` |

### Outputs
| To | What | Exact form |
|---|---|---|
| service | every kept frame | `POST /ingest` multipart/form-data: `image` = file `cap.jpg` (`image/jpeg`, ≤1440 px wide, q70), `device` = `"LAPTOP-A"`, `ts` = `"1791663158.158"`, `app` = `"slack"`, `title` = `"#hackathon - Slack"`; header `X-Dockt-Token` only if `DOCKT_TOKEN` is set → `202 {"id": 9}` |
| service | heartbeat every 30 s | `POST /agent/ping` JSON `{"device":"LAPTOP-A","session":"ab12cd34ef56","checks":1200,"sent":37}` → `{"ok":true}` (feeds the "checks → kept → filtered" header) |
| service | quick-chat question | `POST /api/chat` JSON `{"messages":[{"role":"user","content":"where is dinner tonight?"},{"role":"assistant","content":"Nopalito at 8 [#9]"},{"role":"user","content":"draft a text to Sam saying I'm in"}],"stream":false}` (full history each turn) |
| Lane 2 | navigation | opens `msedge --app=http://127.0.0.1:8000`, `http://127.0.0.1:8000/#/capture/9`, `http://127.0.0.1:8000/#/chat?q=where%20is%20dinner%20tonight` |
| service | process env (launcher) | `VLM_URL=http://<gx10>:11434/v1`, `VLM_MODEL=gemma4:26b`, `LLM_MODEL=nemotron3:33b`, `EMBED_MODEL=nomic-embed-text`, `DATA_DIR=%USERPROFILE%\.dockt\data`; starts `python server\app.py`, then `python agent\capture.py --server http://127.0.0.1:8000 --name %COMPUTERNAME%` |
| repo | files | `agent/capture.py` (fixed), `agent/tray.py`, `agent/quickchat.py` (names free), `start-dockt.bat`, `gx10.txt`, `requirements-agent.txt` |
| service (P2) | audio | `POST /api/transcribe` multipart `audio` = `clip.wav` (16 kHz mono PCM) |

---

## Lane 2 — App (dashboard)

### Inputs (responses it renders)
```jsonc
POST /api/chat  {"messages":[…],"stream":true}  → text/event-stream, each line "data: <json>\n\n", in this order:
data: {"type":"status","text":"Searching your screens for 'dinner'…"}      // 0..n
data: {"type":"token","text":"Dinner "}                                     // 0..n, concatenate
data: {"type":"sources","items":[Capture, …]}                               // exactly once, before done
data: {"type":"done","answer":"Dinner is at Nopalito at 8; Sam booked it [#9].","cited":[9],"ms":2875}
data: {"type":"error","text":"GX10 unreachable"}                            // instead of done
// no "data: [DONE]" line; the connection closes after done/error

GET /api/capture/9      → Capture + "error":""
GET /img/9              → image/jpeg (Cache-Control max-age=86400); 404 if missing
GET /api/commitments?hours=48&device=  → {"items":[Commitment, …]}   // newest first
POST /api/commitments/3/done {"done":true}  → {"ok":true}
GET /api/standup?kind=standup|timesheet&device=  → {"text":"**Done**\n- …","kind":"standup","spans":14,"captures":212,"ms":4100}
                                                 // when nothing captured: {"text":"No activity captured in this period yet.","kind":"standup","ms":0}
POST /api/draft {"ids":[9,12],"kind":"email|message|todo|notes","instruction":"Context: the user asked \"…\""}  → {"draft":"Subject: …","kind":"email"}
GET /api/search?q=invoice+4471&k=24&device=  → {"results":[Capture+score, …],"ms":31}
GET /api/browse?limit=48&device=&start=&end=  → [Capture, …]            // newest first
GET /api/timeline?bucket=600&device=  → [{"device":"LAPTOP-A","b":1791660000,"n":7,"first_id":31}, …]
GET /api/stats  → {"total":212,"done":210,"queued":2,"errors":0,"indexed_per_min":9.4,"captured_per_min":8.8,
                   "devices":["LAPTOP-A"],"checks":1200,"kept":37,"filtered_pct":96.9}   // filtered_pct null until a ping arrived
GET /api/health → {"models":{"vlm":true,"embed":true,"llm":true},"vlm_enabled":true}
GET /api/nudges → {"items":[…]}  (P2)
URL hash (from Lane 1):  #/capture/9     #/chat?q=<urlencoded question>
```

### Outputs
| What | Exact form |
|---|---|
| the page | `server/static/index.html` served at `/`; any extra files under `server/static/` reachable as `/static/<name>`; no external URLs (demo is offline) |
| requests it sends | exactly the bodies above; `/api/chat` always with the full `messages` history and `"stream": true`; `device` omitted or `null` unless the selector is set |
| deep-link behavior (contract for Lane 1) | `#/capture/{id}` opens that capture's modal on load and on `hashchange`; `#/chat?q=` starts a new chat and sends `q` |
| UI states | `status` → grey hint; `token` → live text; `done` → `[#id]` and bare `[id]` (when id ∈ sources) become chips; `error` → one friendly line; empty states, never raw "Error:" text |

---

## Lane 3 — Agent (`server/brain.py`)

### Inputs
```python
# from memory.py (Lane 4)
await memory.search(q: str, k=24, device=None, start=None, end=None) -> list[Capture]   # ranked, each has "score"; embeds q itself; keyword-only if embed fails
memory.get(id) -> Capture | None            # includes "path" and "error"
memory.many(ids: list[int]) -> list[Capture]  # keeps the order of ids, drops unknown ids
memory.activity(start, end, device=None, max_spans=120) -> (spans: list[Span], omitted: int, total: int)
memory.commitments(start=None, end=None, device=None, include_done=True, limit=200) -> list[Commitment]
memory.add_commitments(capture_row: dict, items: list[{"kind","text","who","due"}]) -> list[{"id","text"}]  # dedups near-duplicates (24 h)
memory.set_commitment_done(id, done: bool) -> None
memory.store.claim_extract(n) -> list[{"id","device","ts","app","title","summary","ocr"}]   # extractor queue, newest first
memory.store.mark_extracted(id, ok=True) -> None

# from models.py (Lane 4)
await models.chat(system: str, user: str, max_tokens=700, temperature=0.2) -> str          # <think> stripped, "" possible
await models.chat_messages(messages, tools=None, max_tokens=700, temperature=0.2, stream=False)
    # stream=False -> {"content": "…", "tool_calls": [{"id": "call_x", "name": "search_screens", "arguments": {"q": "dinner"}}]}
    # stream=True  -> async iterator: {"type":"token","text":"Din"} … {"type":"done","content":"…","tool_calls":[…]}
    # messages: OpenAI roles; an assistant message that called tools is
    #   {"role":"assistant","content":"","tool_calls":[{"id":"call_x","type":"function","function":{"name":"search_screens","arguments":"{\"q\":\"dinner\"}"}}]}
    #   followed by {"role":"tool","tool_call_id":"call_x","content":"[#9] Sat 13:12 · slack — #hackathon: Sam: dinner at Nopalito at 8, I booked for 4"}

# HTTP requests it receives
POST /api/chat   {"messages":[{"role":"user|assistant","content":"…"}], "stream": bool, "device": str|null}
POST /api/ask    {"q":"…","k":10,"device":null}
POST /api/draft  {"ids":[9,12],"kind":"email","instruction":""}
GET  /api/standup?kind=standup|timesheet&device=&hours=
GET  /api/commitments?hours=48&device=&include_done=true
POST /api/commitments/{id}/done {"done":true}
GET  /api/nudges?since=
```

### Outputs
```jsonc
// POST /api/chat, stream:true  — exact lines, in this order (NEW: real agent behind it, same events)
data: {"type":"status","text":"Searching your screens for 'dinner'…"}
data: {"type":"token","text":"Dinner is at "}
data: {"type":"sources","items":[Capture…]}          // every capture any tool returned, cited ones first, max 12
data: {"type":"done","answer":"Dinner is at Nopalito at 8; Sam booked it [#9].","cited":[9],"ms":2875}
// cited ⊆ ids in sources; answer contains "[#9]" literally; on failure: data: {"type":"error","text":"…"}

// POST /api/chat, stream:false  and  POST /api/ask
{"answer":"…[#9]…","sources":[Capture…],"cited":[9],"window":{"start":1791655200.0,"end":1791680400.0,"explicit":true,"spans":14},"ms":2875}
// nothing indexed: {"answer":"Nothing indexed matches that yet.","sources":[],"cited":[]}
// no evidence: answer text "I didn't see that on your screens." with "cited": []

// POST /api/draft  → {"draft":"Subject: Dinner tonight\n\nHi Sam, …","kind":"email"}
// GET /api/standup → {"text":"…","kind":"timesheet","spans":14,"captures":212,"ms":4100}
// GET /api/commitments → {"items":[Commitment…]}     POST …/done → {"ok":true}
// GET /api/nudges → {"items":[{"id":3,"ts":1791663158.1,"kind":"deadline","text":"NDA due Monday — seen Sat 13:12","capture_id":9,"due":"Monday"}]}   (NEW, P2; stub returns [])

// stdout (Lane 4 and the orchestrator watch for these)
promise found in #9: Send the revised NDA by Monday
extract error id=9: …
```
Tool schemas Lane 3 defines (names are the contract with the prompt only, not with other lanes): `search_screens(q, start?, end?)`, `get_activity(start, end)`, `get_captures(ids)`, `list_commitments(hours?, include_done?)`, `draft(ids, kind, instruction?)`.

---

## Lane 4 — Models (GX10) + Memory (laptop)

### Inputs
| From | What | Exact form |
|---|---|---|
| Lane 1 | frames | `POST /ingest` fields `image` (jpeg bytes), `device`, `ts`, `app`, `title` (see Lane 1 outputs) |
| Lane 1 (P2) | audio | `POST /api/transcribe` multipart `audio` (wav/webm/m4a bytes + content-type) |
| Lane 3 | calls | the `memory.*` and `models.*` signatures listed under Lane 3 inputs |
| env | config | `VLM_URL` (default `http://localhost:11434/v1`), `VLM_MODEL` (→ `gemma4:26b`), `EMBED_URL` (= VLM_URL), `EMBED_MODEL` (`nomic-embed-text`), `LLM_URL` (= VLM_URL), `LLM_MODEL` (→ `nemotron3:33b`), `STT_URL` (NEW, `http://<gx10>:8001/v1`), `STT_MODEL` (NEW, `small`), `DATA_DIR` (`~/.dockt/data`), `INGEST_CONCURRENCY` (8), `VLM_ENABLED` (1), `LLM_NO_THINK` (1), `OPENAI_API_KEY` (`local`) |

### Outputs — GX10 endpoints (only `models.py` and the mock speak these)
```jsonc
POST http://<gx10>:11434/v1/chat/completions      // vision
{"model":"gemma4:26b","temperature":0,"max_tokens":400,"reasoning_effort":"none",
 "messages":[{"role":"user","content":[{"type":"text","text":"<VLM_PROMPT with app/title>"},
                                        {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…"}}]}]}
→ choices[0].message.content = "SUMMARY: …\nTEXT: …"      // parsed into (summary, text)

POST …/v1/chat/completions                        // chat + tools
{"model":"nemotron3:33b","temperature":0.2,"max_tokens":700,"reasoning_effort":"none","messages":[…],
 "tools":[{"type":"function","function":{"name":"search_screens","description":"…","parameters":{"type":"object","properties":{"q":{"type":"string"}},"required":["q"]}}}],
 "stream": false|true}
→ non-stream: choices[0].message = {"content":"","tool_calls":[{"id":"call_bt9nm1vu","type":"function","function":{"name":"search_screens","arguments":"{\"q\":\"dinner\"}"}}]}, finish_reason "tool_calls"
   or {"content":"Dinner is at Nopalito at 8 PM… [#9]"}, finish_reason "stop"
→ stream: "data: {…delta:{content:\"Din\"}…}" chunks; a tool call arrives as ONE chunk with delta.tool_calls[…]; ends with "data: [DONE]"
   (verified on the GX10 Sat 15:25; HTTP 400 on reasoning_effort ⇒ retry without it)

POST …/v1/embeddings  {"model":"nomic-embed-text","input":["text", …]}  → {"data":[{"index":0,"embedding":[768 floats]}]}
GET  …/v1/models  → 200 (health check only)
POST http://<gx10>:8001/v1/audio/transcriptions   (NEW, Whisper server)  multipart file=clip.wav, model=small  → {"text":"where is dinner tonight"}
```

### Outputs — Python (consumed by Lane 3 and `app.py`)
```python
await models.describe(jpeg: bytes, app: str, title: str) -> (summary: str, screen_text: str)
await models.embed(texts: list[str]) -> list[list[float]]           # 768-dim for nomic
await models.chat(system, user, max_tokens=700, temperature=0.2) -> str
await models.chat_messages(messages, tools=None, max_tokens=700, temperature=0.2, stream=False)  # NEW, exact return shapes under Lane 3
await models.transcribe(audio: bytes, mime: str) -> str              # NEW
await models.health() -> {"vlm": bool, "embed": bool, "llm": bool}   # may add "stt"
memory.search / get / many / activity / commitments / add_commitments / set_commitment_done  # as under Lane 3
```

### Outputs — local endpoints and files
```jsonc
POST /ingest → 202 {"id": 9}          GET /img/9 → image/jpeg          GET /api/capture/9 → Capture + "error"
GET /api/search?q=&k=&device=&start=&end= → {"results":[Capture+score…],"ms":31}
GET /api/browse → [Capture…]          GET /api/timeline?bucket=600 → [{"device","b","n","first_id"}]
GET /api/stats → {"total","done","queued","errors","indexed_per_min","captured_per_min","devices","checks","kept","filtered_pct"}
GET /api/health → {"models":{"vlm","embed","llm"},"vlm_enabled"}
POST /api/transcribe → {"text":"…","ms":900}      // NEW; today 503 until models.transcribe exists
```
Mock (`tools/mock_models.py`, NEW parts): first user turn with `tools` → one `tool_calls` for `search_screens` with `{"q": <last user text>}`; after a `tool` message → `"… [#<first id in tool content>]"`; `stream:true` → SSE chunks + `[DONE]`; `POST /v1/audio/transcriptions` → `{"text":"mock transcript"}`. Files: `tools/chat_smoke.py`, `tools/eval/` + `tools/eval.py`, `contracts/fixtures/{ask,chat,commitments,search}.json`, GX10 `ollama.service.d/dockt.conf` with `OLLAMA_HOST=0.0.0.0`, Whisper unit on `:8001`. Numbers to publish in `TEAM.md` §3.3: img/min at concurrency 1/4/8, Ask latency, laptop capture rate.
