# Lane 4 — Models (GX10) + Memory (laptop)

## Task card

**Goal.** Every laptop can reach the three models on the GX10; `models.py` gains `chat_messages()` (tools + streaming) and `transcribe()` with mocks; defaults match the chosen models; the GX10 keeps up with all demo laptops; search and perception quality are measured, not guessed.

**Files owned.** GX10: `/etc/systemd/system/ollama.service.d/dockt.conf`, firewall, any Whisper server. Repo: `server/models.py`, `server/store.py`, `server/memory.py`, `server/indexer.py`, `tools/bench.py`, `tools/mock_models.py`, `contracts/fixtures/`, README sections on env vars. Only Lane 4 changes or restarts anything on the GX10.

**Contracts produced** (`TEAM.md` §3.2/3.3): `models.chat_messages(...)`, `models.transcribe(audio, mime)`, the GX10 endpoints `http://<gx10>:11434/v1/*` and `http://<gx10>:8001/v1/audio/transcriptions`, the local read endpoints (`/ingest`, `/img`, `/api/search`, `/api/stats`, `/api/health`, `/api/transcribe` body), `memory.*`.

**Contracts consumed.** None (bottom of the stack). Lane 3 consumes `chat_messages`; Lane 1 consumes `/api/transcribe`.

**Current GX10 state (verified Sat 15:20).** Ollama 0.23.2 systemd, override has `OLLAMA_CONTEXT_LENGTH=16384 OLLAMA_NUM_PARALLEL=4 OLLAMA_KEEP_ALIVE=-1 OLLAMA_MAX_LOADED_MODELS=3`, **no `OLLAMA_HOST`** → listens on `127.0.0.1:11434` only. Loaded: nemotron3:33b (30 GB), gemma4:26b (24 GB), nomic (0.6 GB); 60 GB free. GX10 IP today `169.233.72.21` (campus network). Sudo requires the password. No torch, no docker group; `ctranslate2` aarch64 wheel installs, `nvcc` present. An old copy of the Dockt service runs on the GX10 on `0.0.0.0:8000` (pid 136008): useful for Lane 3 today, stop it before the demo.

**Priorities.**
- P0 (first 30 min): `OLLAMA_HOST=0.0.0.0` in the override, `daemon-reload`, restart, confirm the three models reload (`ollama ps`; `KEEP_ALIVE=-1` is set but pre-warm with one request each). Open 11434/tcp (`ufw`/`iptables`, whichever is active). From a laptop: `curl http://<gx10>:11434/v1/models`. If campus Wi-Fi blocks it, that is the signal to switch to our own router now.
- P0: `tools/mock_models.py`: handle `tools` in `/v1/chat/completions` (return one fake `tool_calls` for `search_screens` on the first user turn, then a cited answer after a `tool` message), `stream:true` (SSE chunks + `[DONE]`), and `POST /v1/audio/transcriptions` → `{"text":"mock transcript"}`.
- P0: `models.chat_messages(messages, tools, max_tokens, temperature, stream)` per `TEAM.md` §3.2 on `LLM_URL`, with `reasoning_effort:"none"` (+ the 400 retry), `tool_calls` arguments parsed from JSON string to dict, stream yielding `{"type":"token"}` and a final `{"type":"done","content","tool_calls"}` (tool calls arrive as whole chunks; accumulate by `index`). Defaults in `models.py`: `VLM_MODEL=gemma4:26b`, `LLM_MODEL=nemotron3:33b`, `EMBED_MODEL=nomic-embed-text`.
- P0: throughput: two laptops capturing at once + extraction running; `indexed_per_min ≥ captured_per_min` on each; if not, `INGEST_CONCURRENCY`, `EXTRACT_CONCURRENCY=1`, or `--max-width 1280` on the agents. Record numbers in `TEAM.md` §3.3.
- P1: `models.transcribe()` + `/api/transcribe` body + a Whisper server on the GX10: `pip install faster-whisper fastapi uvicorn python-multipart` in a venv, a 40-line FastAPI app exposing `POST /v1/audio/transcriptions` (multipart `file`) → `{"text"}` on `:8001`, model `small` or `base`, CPU int8 (20 cores). Measure: a 5 s clip should transcribe in < 2 s.
- P1: eval set: 10–20 real Windows screenshots (Outlook, Teams, Edge, Excel, VS Code) with 3 expected facts each in `tools/eval/`; a script scoring facts-found and seconds per image for `gemma4:26b` vs `qwen2.5vl:7b`; adjust `VLM_PROMPT` if facts are missed. Keep the `SUMMARY:/TEXT:` format.
- P1: `contracts/fixtures/*.json` with real responses (ask, chat non-stream, commitments, search) for Lanes 1 and 2.
- P2: search quality (RRF weights, `k`), nothing bigger; GX10 reboot test; stop the old service on the GX10.

**Definition of done (P0).** A laptop on the demo network gets `/v1/models` from the GX10; the mock supports tools + streaming + transcription; `chat_messages` works against the real Nemotron (unit script: 2 rounds with a fake tool result, prints the cited answer and streams it) and against the mock; two laptops capturing concurrently stay under capacity for 10 minutes with no queue growth.

**How to verify.** `tools/bench.py --url http://127.0.0.1:11434/v1 --model gemma4:26b --levels 1,4,8` on the GX10; mock pipeline for shapes (`TEAM.md` §6 A); the real pipeline from a laptop (§6 B).

---

## Ready-to-paste agent prompt

```text
You are working on Dockt, a private "work memory" desktop app built this weekend for the ASUS Local AI track of the UCSC AI Hackathon. The whole app runs on each user's own Windows laptop: a capture agent saves a screenshot whenever the screen changes, a local FastAPI service on http://127.0.0.1:8000 indexes them (SQLite FTS5 + numpy vectors), and a dashboard + tray quick-chat let the user ask questions with every answer citing the exact screenshot as [#id]. The only thing not on the laptop is the AI models: an ASUS Ascent GX10 (NVIDIA GB10, ARM64, 128 GB unified memory) on the local network serves them with Ollama 0.23.2 through its OpenAI-compatible API: gemma4:26b (vision, summary + screen text per screenshot), nemotron3:33b (chat + tool calling, 16k context), nomic-embed-text (embeddings). Screenshots and the index stay on the laptop; the GX10 stores nothing. Hard rule: nothing ever leaves the local network. No cloud APIs, no telemetry, no hosted models, not even for testing.

The team works in 4 lanes with fixed contracts in TEAM.md §3:
1 Capture + Desktop client (agent/, tray, hotkey quick-chat, voice via POST /api/transcribe)
2 App / dashboard (server/static/)
3 Agentic layer (server/brain.py: /api/chat tool-calling agent; consumes models.chat_messages and memory.*)
4 Models + Memory   <- ME

I am [NAME], LANE 4. Demo: about 4 minutes, live, two Windows laptops, probably tonight (feature freeze 19:30 if confirmed). I own the bottom of the stack, so my P0 items block other lanes: do them first and report fast.

MY FILES. GX10: /etc/systemd/system/ollama.service.d/dockt.conf, the firewall, a Whisper server. Repo: server/models.py, server/store.py, server/memory.py, server/indexer.py, tools/bench.py, tools/mock_models.py, contracts/fixtures/, the env-var section of README.md. Not mine: server/brain.py (Lane 3), server/app.py (shared; small separate PR if a route body must change), agent/, server/static/.

CURRENT GX10 STATE (verified by the orchestrator today 15:20): Ollama systemd override has OLLAMA_CONTEXT_LENGTH=16384 OLLAMA_NUM_PARALLEL=4 OLLAMA_KEEP_ALIVE=-1 OLLAMA_MAX_LOADED_MODELS=3 and NO OLLAMA_HOST, so it listens on 127.0.0.1:11434 only; laptops cannot reach it yet. Loaded and pinned: nemotron3:33b (30 GB), gemma4:26b (24 GB), nomic-embed-text; ~60 GB free. Also on disk: qwen2.5vl:7b, qwen3.6:35b, nemotron-3-super (86 GB, do not load, it would evict the others). GX10 IP today 169.233.72.21 (campus network; we may move to our own router). sudo needs the password (ask me). No torch, no docker access; the ctranslate2 aarch64 wheel installs fine; nvcc exists. Ollama ignores chat_template_kwargs; thinking models return empty content unless the request has reasoning_effort:"none" (models.py already does this with a retry without it on HTTP 400). Verified today: nemotron3:33b returns proper OpenAI tool_calls with reasoning_effort:"none", answers after a `tool` message, and streams (a tool call arrives as ONE chunk). Measured: vision ~1.5 s simple / 5.6 s dense screenshot, ~28 img/min end to end with extraction, Ask ~3 s.

CONTRACTS I PRODUCE (TEAM.md §3.2/3.3; Lane 3 is coding against chat_messages right now, Lane 1 against /api/transcribe):
  await models.chat_messages(messages: list[dict], tools: list[dict] | None = None, max_tokens=700, temperature=0.2, stream=False)
    stream=False -> {"content": str, "tool_calls": [{"id": str, "name": str, "arguments": dict}]}   # arguments parsed from the JSON string; [] when none
    stream=True  -> async iterator yielding {"type":"token","text": str} ... then {"type":"done","content": str,"tool_calls":[...]}
    messages are OpenAI-style (system/user/assistant with tool_calls/tool with tool_call_id); tools are OpenAI function schemas. Always send reasoning_effort:"none" (retry without on 400). Strip <think> like chat() does.
  await models.transcribe(audio: bytes, mime: str) -> str   # POST {STT_URL}/audio/transcriptions multipart file, model STT_MODEL; env STT_URL (default http://<gx10>:8001/v1), STT_MODEL (default "small")
  await models.chat / embed / describe / health: unchanged signatures. health may add "stt".
  Mock: tools/mock_models.py must support everything models.py calls: tools (first user turn -> one tool_call for search_screens with {"q": <last user text>}; after a `tool` message -> an answer citing the first [#id] found in that tool content), stream:true (SSE chunks, then data: [DONE]), POST /v1/audio/transcriptions -> {"text":"mock transcript"}.
  Local endpoints (bodies live in app.py, shared): POST /api/transcribe multipart audio -> {"text","ms"} (the stub calls models.transcribe if it exists; you only need to add the function), /ingest, /img/{id}, /api/search, /api/stats, /api/health unchanged.
  memory.py: search(q,k,device,start,end) async; get, many, activity, commitments, add_commitments, set_commitment_done. Don't rename anything; adding functions is fine.
  Env defaults: VLM_MODEL=gemma4:26b, LLM_MODEL=nemotron3:33b, EMBED_MODEL=nomic-embed-text (change the defaults in models.py; README env section accordingly).

TASKS, in order; stop and report after each.
P0-a  (GX10, needs sudo, 15 min) Add Environment=OLLAMA_HOST=0.0.0.0 to /etc/systemd/system/ollama.service.d/dockt.conf, systemctl daemon-reload && systemctl restart ollama, then warm all three models with one request each and confirm `ollama ps` shows them with far-future expiry. Open tcp/11434 (check `sudo ufw status`; if inactive, check iptables/nftables; open only 11434 and later 8001). From a laptop: curl http://<gx10-ip>:11434/v1/models. Report the result and the exact IP. If a laptop on the campus Wi-Fi cannot connect, say so immediately: we switch to our own router.
P0-b  tools/mock_models.py: tools + streaming + /v1/audio/transcriptions as specified above. Keep the existing behaviors (vision, ask, extraction, standup, draft) intact. Test with curl.
P0-c  server/models.py: chat_messages() and transcribe() as specified; change the model defaults. Write a tiny script tools/chat_smoke.py that calls chat_messages against LLM_URL with a search_screens tool, feeds back a fake tool result ("[#9] Sat 13:12 · Slack — #hackathon: Sam: dinner at Nopalito at 8, I booked for 4"), prints the final answer, then repeats with stream=True printing tokens. Run it against the mock and against the real GX10; paste both outputs.
P0-d  Throughput: with two laptops capturing (coordinate with Lane 1) and extraction on, watch each laptop's /api/stats for 10 minutes: indexed_per_min must be >= captured_per_min and queued must not grow. Run tools/bench.py --url http://127.0.0.1:11434/v1 --model gemma4:26b --levels 1,4,8 on the GX10 once and write the numbers into TEAM.md §3.3. If capacity is short: EXTRACT_CONCURRENCY=1 first, then --max-width 1280 on the agents.
P1-a  Whisper on the GX10: python3 -m venv ~/stt && ~/stt/bin/pip install faster-whisper fastapi uvicorn python-multipart; a small FastAPI app (~40 lines) exposing POST /v1/audio/transcriptions (multipart `file`, optional `model`) -> {"text"} using WhisperModel("small", device="cpu", compute_type="int8") loaded once; run on 0.0.0.0:8001 (systemd unit or nohup). Open tcp/8001. Measure a 5 s wav. Then models.transcribe() against it; /api/transcribe on a laptop returns text.
P1-b  Eval set: tools/eval/ with 10–20 real Windows screenshots (get them from Lane 1's laptop: Outlook, Teams, Edge, Excel, VS Code) and a facts.json ({"file": ["fact1","fact2","fact3"]}); tools/eval.py runs models.describe on each and reports facts found (case-insensitive substring) and seconds/image for the current VLM_MODEL. Run for gemma4:26b and qwen2.5vl:7b; if gemma4 misses facts, tune VLM_PROMPT (keep the SUMMARY:/TEXT: format) and re-run. Paste the table.
P1-c  contracts/fixtures/: real responses from the GX10 pipeline for /api/ask, /api/chat (non-stream), /api/commitments, /api/search, saved as JSON for Lanes 1 and 2.
P2    Stop the old service on the GX10 (uvicorn app:app on :8000, pid 136008) once Lane 3 no longer needs it, and before the demo. GX10 reboot test: after reboot the three models are loaded and reachable within 3 minutes without anyone typing anything (write a one-shot systemd unit that curls each model once after ollama starts, or document the manual warm-up). Search-quality tweaks only if everything else is done.

RULES
- Nothing on the GX10 but models (and the Whisper server). No app code or data lives there. Changing the embedding model means every laptop re-indexes: don't.
- Verify on the mock before claiming anything; measure on the real GX10 and paste numbers. Never test against a cloud model.
- Windows first for repo code: pathlib, no Unix-only calls; PowerShell commands in docs. Keep changes small and in the style of the surrounding code; no new dependencies in the laptop service (faster-whisper lives only on the GX10).
- Branch lane4/<feature>, small commits. Don't push to main or open PRs unless I ask.
- Before writing code, read README.md, TEAM.md (source of truth; where it disagrees with README/PLAN.md, TEAM.md wins), PLAN.md §2 and §8, then server/models.py, server/memory.py, server/indexer.py, server/store.py, tools/mock_models.py fully. Then tell me in a few lines: what my lane owns, which contracts my task produces, what "done" means, how you will verify.

MOCK PIPELINE (PowerShell, from the dockt-patched folder; Linux/macOS same with .venv/bin and export):
  terminal 1:  .venv\Scripts\uvicorn --app-dir tools mock_models:app --port 9000
  terminal 2:  $env:VLM_URL="http://127.0.0.1:9000/v1"; .venv\Scripts\python server\app.py
  terminal 3:  .venv\Scripts\python tools\replay.py --server http://127.0.0.1:8000 --synthetic 24
REAL: terminal 2 with $env:VLM_URL="http://<gx10-ip>:11434/v1" (VLM_MODEL/LLM_MODEL/EMBED_MODEL defaults after your change), own $env:DATA_DIR for test data.

When you finish each task, report: what you changed (files/functions, GX10 config lines), exactly what you ran and what you saw (paste numbers), what you did not verify, and anything another lane needs to know (new env vars, fixtures, the GX10 IP).
```
