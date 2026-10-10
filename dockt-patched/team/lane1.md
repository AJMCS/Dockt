# Lane 1 — Capture + Desktop client (Windows)

## Task card

**Goal.** Two Windows laptops capture their screens into their own local Dockt service with one click, show the dashboard as an app window, and (P1) give the user a tray icon and a hotkey quick-chat.

**Files owned.** `agent/` (everything), `start-dockt.bat` / `start-dockt.ps1` at the repo root, `requirements-agent.txt`.

**Contracts consumed.** `POST /ingest` (multipart `image, device, ts, app, title`), `POST /agent/ping`, `POST /api/chat` (`{"messages":[{"role","content"}],"stream":false}` → `{answer, sources:[Capture], cited:[id], ms}`), `GET /img/{id}`, deep links `/#/capture/{id}` and `/#/chat?q=` (Lane 2), `POST /api/transcribe` (P2). All on `http://127.0.0.1:8000`.

**Contracts produced.** None new. The launcher is the "contract" everyone runs the demo through.

**Priorities.**
- P0: `capture.py` on Windows: replace `shot.rgb` with `Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")`; verify app + window titles are non-empty for Edge, Teams/Slack, Outlook, VS Code; `--server` default `http://127.0.0.1:8000`.
- P0: `start-dockt.bat`: creates/uses `.venv`, installs requirements, sets `VLM_URL=http://<gx10-ip>:11434/v1 VLM_MODEL=gemma4:26b LLM_MODEL=nemotron3:33b EMBED_MODEL=nomic-embed-text DATA_DIR=%USERPROFILE%\.dockt\data`, starts `python server\app.py`, waits for `/api/health`, starts `python agent\capture.py --name %COMPUTERNAME%`, opens `msedge --app=http://127.0.0.1:8000`. GX10 IP read from a `gx10.txt` or env var so it can change on demo day.
- P0: run the mock smoke test on Windows after every merge window (TEAM.md §6 A) and report pass/fail.
- P1: tray icon (`pystray` + `Pillow`): status capturing / paused / GX10 unreachable (poll `/api/health` every 10 s), pause toggle (creates/removes `~/.dockt_pause`), "Open Dockt" (Edge app window), "Quit".
- P1: global hotkey (e.g. Ctrl+Space via `keyboard` or `pynput`) → small always-on-top `tkinter` window: text box, sends `/api/chat` with `stream:false`, shows the answer, renders `[#id]` as buttons that open `http://127.0.0.1:8000/#/capture/{id}` in the Edge app window, shows up to 3 thumbnails from `/img/{id}`, "Open in Dockt" → `/#/chat?q=<question>`.
- P2: push-to-talk (record WAV with `sounddevice`, POST to `/api/transcribe`, put text in the box); Windows toast for `GET /api/nudges` items (`win10toast` or `plyer`).

**Definition of done (P0).** On a clean Windows laptop: double-click `start-dockt.bat` → within 30 s the dashboard opens, header shows checks → kept and three green dots, captures appear with real app names and titles, a planted Slack message is indexed within 60 s. Mock smoke test passes on Windows.

**How to verify.**
```powershell
# mock pipeline (no GX10): terminal 1
.venv\Scripts\uvicorn --app-dir tools mock_models:app --port 9000
# terminal 2
$env:VLM_URL="http://127.0.0.1:9000/v1"; .venv\Scripts\python server\app.py
# terminal 3
.venv\Scripts\python tools\replay.py --server http://127.0.0.1:8000 --synthetic 24
.venv\Scripts\python agent\capture.py --server http://127.0.0.1:8000 --name test --debug
# checks: http://127.0.0.1:8000/api/health all true; /api/stats done == total; cards show app + title
```

---

## Ready-to-paste agent prompt

```text
You are working on Dockt, a private "work memory" desktop app built this weekend for the ASUS Local AI track of the UCSC AI Hackathon. The whole app runs on each user's own Windows laptop: a capture agent saves a screenshot whenever the screen changes, a local FastAPI service on http://127.0.0.1:8000 indexes them (SQLite FTS5 + vectors), and a dashboard + tray quick-chat let the user ask questions about what they saw, with every answer citing the exact screenshot as [#id]. The only thing not on the laptop is the AI models: an ASUS Ascent GX10 (NVIDIA GB10, 128 GB unified memory) on the local network serves gemma4:26b (vision), nemotron3:33b (chat agent) and nomic-embed-text (embeddings) through Ollama's OpenAI-compatible API at http://<gx10-ip>:11434/v1. Screenshots and the index stay on the laptop; the GX10 stores nothing. Hard rule: nothing ever leaves the local network. No cloud APIs, no telemetry, no hosted models, not even for testing.

The team works in 4 lanes, each a black box with fixed contracts in TEAM.md §3:
1 Capture + Desktop client (agent/: capture, tray icon, hotkey quick-chat, voice, Windows launcher)   <- ME
2 App (dashboard UI served by the local service at /, built assets under /static/)
3 Agentic layer (server/brain.py: /api/chat tool-calling agent, ask, drafts, standup, promise extraction, nudges)
4 Models + Memory (GX10 model serving, server/models.py, store.py, memory.py, indexer.py, mocks)

I am [NAME], LANE 1. Demo: about 4 minutes, live, on two Windows laptops, probably tonight (feature freeze 19:30, if the deadline is confirmed), so optimize for the demo, not production quality.

MY FILES: agent/ (everything), start-dockt.bat / start-dockt.ps1 at the repo root, requirements-agent.txt. Do not edit anything else. If you need something from another lane, write the proposed contract addition to TEAM.md §3 and stop.

CONTRACTS I CONSUME (all on http://127.0.0.1:8000; full spec TEAM.md §3.1):
- POST /ingest  multipart image (jpeg), device, ts (unix s), app, title -> {"id"} (202). Already used by capture.py.
- POST /agent/ping {"device","session","checks","sent"} -> {"ok":true}. Already used.
- POST /api/chat {"messages":[{"role":"user","content":"..."}], "stream":false} -> {"answer","sources":[Capture],"cited":[id],"ms"}.
  Capture = {id, device, ts, app, title, summary, ocr, status}. Image bytes at GET /img/{id}. (A stub of /api/chat exists today and answers via ask(); Lane 3 replaces the insides, the shape stays.)
- Deep links owned by Lane 2: /#/capture/{id} opens one capture, /#/chat?q=<urlencoded question> opens the chat with the question asked.
- GET /api/health -> {"models":{"vlm":bool,"embed":bool,"llm":bool}} = "can this laptop reach the GX10 models".
- P2 only: POST /api/transcribe multipart audio (wav) -> {"text","ms"}; GET /api/nudges -> {"items":[{"id","ts","kind","text","capture_id","due"}]}.

TASKS, in this order. Stop after each and report before moving on.
P0-a  agent/capture.py Windows fixes: (1) replace `Image.frombytes("RGB", shot.size, shot.rgb)` with `Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")` (shot.rgb is slow). (2) make --server default to http://127.0.0.1:8000. (3) run it with --debug on this Windows laptop and confirm app and title are non-empty for Edge, Teams or Slack, Outlook/Gmail in a browser, VS Code; if titles are empty, fix active_window() (ctypes GetForegroundWindow path) and tell me what was wrong. (4) check that the pause file (~/.dockt_pause) and the exclusion list still work.
P0-b  start-dockt.bat (and the same as start-dockt.ps1 if easier): read the GX10 address from gx10.txt next to the script (one line, e.g. 192.168.8.10) or env DOCKT_GX10; create .venv with `py -m venv .venv` if missing and pip install -r requirements-server.txt -r requirements-agent.txt; set VLM_URL=http://<gx10>:11434/v1, VLM_MODEL=gemma4:26b, LLM_MODEL=nemotron3:33b, EMBED_MODEL=nomic-embed-text, DATA_DIR=%USERPROFILE%\.dockt\data; start `python server\app.py` in its own window; wait until http://127.0.0.1:8000/api/health answers; start `python agent\capture.py --name %COMPUTERNAME%` in its own window; open `msedge --app=http://127.0.0.1:8000`. Add a --mock flag/variant that starts tools/mock_models.py on :9000 and points VLM_URL at it instead (for the smoke test). Everything must work from a fresh clone with only Python 3.11+ installed.
P0-c  Run the mock smoke test on this Windows laptop (commands below) and report: health all true, /api/stats done == total, /api/ask returns an answer with [#id], and `python server\app.py` starts without a tzset or encoding error. This is the first time the service runs on Windows; report anything that fails verbatim, it may be another lane's bug (then tell me, don't fix their files).
P1-a  Tray icon (pystray + Pillow, add to requirements-agent.txt): states capturing (green) / paused (grey) / GX10 unreachable (red, from /api/health polled every 10 s); menu: Pause/Resume (create/delete ~/.dockt_pause), Open Dockt (msedge --app=http://127.0.0.1:8000), Quit. Run it from the launcher. Keep capture.py's logic untouched; the tray can be a separate process or import capture.py's main in a thread, your call, but the pause file is the only coupling.
P1-b  Global hotkey (Ctrl+Space, via `keyboard` or `pynput`; pick what works without admin) opening a small always-on-top tkinter window: an input box; Enter sends POST /api/chat with stream:false and the conversation so far (keep messages in memory while the window is open); show the answer; turn each [#id] in the answer into a button that opens http://127.0.0.1:8000/#/capture/{id} in the Edge app window; show up to 3 thumbnails from /img/{id} for the cited ids (fall back to the first 3 sources); an "Open in Dockt" button that opens /#/chat?q=<question>. Esc hides it. It must open in under 300 ms (create once, hide/show).
P2    Push-to-talk: hold a key in the quick-chat window to record WAV (sounddevice), POST to /api/transcribe, put the text in the box. Windows toast notifications for new /api/nudges items (poll every 60 s). Only if P0 and P1 are done and it is before the freeze.

RULES
- Windows first: pathlib, no Unix-only calls, PowerShell/.bat commands. Keep changes small and in the style of the surrounding code. New dependencies only when the task needs them (pystray, keyboard/pynput, sounddevice are allowed; say why for anything else).
- Verify on the mock pipeline before claiming anything works; use the real GX10 when it is reachable (ask me for the IP). Never test against a cloud model. Do not change anything on the GX10.
- Branch lane1/<feature>, small commits. Don't push to main or open PRs unless I ask.
- Before writing code, read README.md, TEAM.md (source of truth; where it disagrees with README/PLAN.md, TEAM.md wins), PLAN.md §2 and §8, then every file you will change. Then tell me in a few lines: what my lane owns, which contracts my task consumes, what "done" means, how you will verify.

MOCK PIPELINE (PowerShell, from the dockt-patched folder):
  py -m venv .venv; .venv\Scripts\pip install -r requirements-server.txt -r requirements-agent.txt
  terminal 1:  .venv\Scripts\uvicorn --app-dir tools mock_models:app --port 9000
  terminal 2:  $env:VLM_URL="http://127.0.0.1:9000/v1"; .venv\Scripts\python server\app.py
  terminal 3:  .venv\Scripts\python tools\replay.py --server http://127.0.0.1:8000 --synthetic 24
               .venv\Scripts\python agent\capture.py --server http://127.0.0.1:8000 --name test --debug
  then open http://127.0.0.1:8000, check /api/health, /api/stats, and POST /api/ask {"q":"where is dinner tonight"}.

When you finish each task, report: what you changed (files/functions), exactly what you ran and what you saw, what you did not verify, and anything another lane needs to know.
```
