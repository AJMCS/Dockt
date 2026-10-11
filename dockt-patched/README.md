# Dockt — run guide

What Dockt is and why: see the [project README](../README.md). This page covers how to run, configure, test and demo it. Lane contracts and the architecture live in [`TEAM.md`](TEAM.md); the visual design is in [`team/DESIGN.md`](team/DESIGN.md).

**The setup in one line:** the whole app runs on each Windows laptop (local service on `127.0.0.1`, capture, desktop window, quick-chat). The ASUS GX10 only serves models through Ollama, and it stores nothing.

## Windows desktop launcher

Run `start-dockt.bat` from the `dockt-patched` folder. It starts local capture and the quick-chat pet; the dashboard opens hidden so startup leaves the pet as the only Dockt window on screen. Right-click the pet for the styled controls panel; choose **Open Dashboard** to show the centered 1040×700 dashboard. Closing that window hides it and leaves the pet running. Choose **Quit Dockt** in the controls panel to close the full session. The launcher uses the GX10 address from `gx10.txt` (or `DOCKT_GX10`). Add `--mock` to run against the local mock model without the GX10. Click the pet to open quick chat; `Ctrl+Space` toggles the chat window and `Esc` hides it. The controls panel also lets you pause or resume screen capture; paused state is shared with `agent/capture.py` through `~/.dockt_pause`.

- **First run** creates `.venv` and installs `requirements-server.txt`, `requirements-agent.txt` and `requirements-desktop.txt`. You need Python 3.11+. The window uses WebView2, which ships with Windows 10/11.
- **Desktop shortcut** with the Dockt icon: `powershell -ExecutionPolicy Bypass -File desktop\install-shortcut.ps1`.
- **Port:** the service uses the first free port from 8000 to 8099. **Logs:** `~\.dockt\logs\`. **Screenshots + index:** `~\.dockt\data\` (`DATA_DIR`). **Chat history + theme:** `~\.dockt\webview\`.
- **Opening a specific screenshot or question** from another program: `GET http://127.0.0.1:8765/open?hash=%23%2Fcapture%2F9` (or `#/chat?q=...`). The quick-chat uses this.

## The GX10 (models only)

The GX10 runs Ollama with three models kept loaded:

| Model | Role |
|---|---|
| `gemma4:26b` | vision: screenshot → summary + screen text |
| `nemotron3:33b` | chat agent with tool calling, drafts, standup, promise extraction |
| `nomic-embed-text` | embeddings for search (changing it means re-indexing every laptop) |

Ollama settings: `OLLAMA_HOST=0.0.0.0` (so laptops can reach it), `OLLAMA_CONTEXT_LENGTH=16384`, `OLLAMA_NUM_PARALLEL=4`, `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_MAX_LOADED_MODELS=3`. Port 11434 must be open on the demo LAN. Only Lane 4 changes the GX10.

Check from a laptop: `curl http://<gx10-ip>:11434/v1/models`. Measure vision throughput with `python tools/bench.py --url http://<gx10-ip>:11434/v1 --model gemma4:26b`. The total captures/min from all laptops must stay under what the GX10 can read, about 28 img/min with extraction running.

**GX10 over SSH (campus Wi-Fi):** if the laptop can't reach the GX10's port 11434 directly, double-click `desktop\Setup-GX10.bat` once. It asks for the GX10 login (e.g. `asus@169.233.72.21`), creates an SSH key for this laptop, adds it to the GX10 (asks for the GX10 password once), and writes the login to `gx10.txt`. From then on the launcher sees `user@address` in `gx10.txt` and keeps an SSH tunnel to the GX10's Ollama open on `127.0.0.1:11435` while Dockt runs, reconnecting if the Wi-Fi drops (log: `~/.dockt/logs/tunnel.log`). A plain address in `gx10.txt` (e.g. on our own router) still connects directly.

## Running without the GX10 (mock models)

**Windows:** `start-dockt.bat --mock`. **By hand** (any OS; on Windows use `.venv\Scripts\` and `$env:VLM_URL=...`):

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-server.txt -r requirements-agent.txt
.venv/bin/uvicorn --app-dir tools mock_models:app --port 9000              # terminal 1
VLM_URL=http://127.0.0.1:9000/v1 .venv/bin/python server/app.py           # terminal 2 (binds 127.0.0.1:8000)
.venv/bin/python tools/replay.py --server http://127.0.0.1:8000 --synthetic 24   # fake screenshots
```

Open http://127.0.0.1:8000. Use your own `DATA_DIR` for test data so it never mixes with your demo index. `python tools/chat_smoke.py` exercises the chat/tool interface against the mock or a real model.

## Model client defaults

The laptop keeps screenshots and the SQLite/FTS5 plus NumPy search index in
`DATA_DIR` (default `~/.dockt/data`). It calls OpenAI-compatible model services
through these settings:

```text
VLM_URL=http://localhost:11434/v1       VLM_MODEL=gemma4:26b
EMBED_URL=$VLM_URL                      EMBED_MODEL=nomic-embed-text
LLM_URL=$VLM_URL                        LLM_MODEL=nemotron3:33b
STT_URL=http://localhost:8001/v1        STT_MODEL=small
```

`EMBED_URL` and `LLM_URL` default to `VLM_URL`. For the local mock, point
`VLM_URL` at `http://127.0.0.1:9000/v1`; set `STT_URL` to the same URL when
testing transcription. Lane 3's message/tool/stream interface is in
`server/models.py`; `python tools/chat_smoke.py` exercises it against the mock
or the configured chat model.

Other settings:
- `INGEST_CONCURRENCY`: parallel vision requests, default 8.
- `VLM_ENABLED=0`: if the vision model dies, the app keeps working on window titles alone.
- `EXTRACT_ENABLED` / `EXTRACT_CONCURRENCY`: promise extraction. Lower the concurrency to 1 if answers get slow; don't turn extraction off for the demo.
- `DOCKT_TOKEN`: if set, the capture agent must send the same token.
- `DOCKT_TZ`: time zone for "this morning" / "yesterday", default America/Los_Angeles.

## Capture agent

The launcher starts it. By hand: `python agent/capture.py --server http://127.0.0.1:8000 --name <device>`.

- **Windows:** nothing to grant. **macOS:** allow *Screen Recording* and *Accessibility* (for window titles) for your terminal app, then restart the terminal.
- Password managers (1Password, Bitwarden, KeePass, LastPass, Keychain Access) and private/incognito windows are always skipped; `--exclude` adds more. Pause anytime: create `~/.dockt_pause`, or use the pet's controls.
- Service down? Frames spool to `~/.dockt_spool` and upload when it's back.

**When it captures:** the agent checks a tiny thumbnail every second (~2 ms) and
only sends a frame when the screen settles on something new, right after an
app/window switch, or every 30s while something keeps moving (e.g. video). An
idle screen sends nothing. "New" is relative to how busy the screen normally is.
- Too many captures? Raise `--sensitivity` (default 3) or `--floor` (default 6).
- Missing things? Lower them. Run with `--debug` to watch the numbers live.
- `--min-gap 2` / `--max-gap 30` cap how often/rarely it sends.

## Tests

```powershell
.venv\Scripts\pip install -r requirements-test.txt
.venv\Scripts\python -m pytest tests -q
```

There are 84 tests: the dashboard in a real browser (Edge on Windows, Chrome elsewhere), the desktop helpers, the local API and the quick-chat. The 25 quick-chat tests need tkinter, which Windows Python includes. The suite starts its own mock stack on free ports with a temporary `DATA_DIR`, so it never touches your index or a running Dockt. Run it before pushing; details are in [`tests/dashboard/README.md`](tests/dashboard/README.md).

## Demo checklist

- **Network:** your own router. Its internet uplink (WAN) is visibly unplugged; the laptop↔GX10 link stays up. Everything keeps working.
- **Before judges arrive:** the GX10 shows the three loaded models (`ollama ps`). The demo laptop has captured for at least 1 hour, the planted moments are in "Things you promised", and the badge reads "● 100% on-device".
- **Planted moments + rehearsed questions** (each correct 3× in a row):
  1. "Where is dinner tonight and who booked it?"
  2. "What's the invoice number and when is it due?"
  3. "What did I work on this afternoon?"
  4. "Did anyone ask me to review something?"
  5. Follow-up: "Draft a message to Sam saying I'm in."
  6. Negative: "What seat am I in on the flight?" → not found.
- **The flow that sells it:** ask → click the citation → the screenshot → "Draft follow-up email" → Copy.
- **Backup:** keep a folder of good screenshots; `python tools/replay.py --server http://127.0.0.1:8000 --folder <dir>` loads them in seconds.

## Local API

The full shapes are in [`TEAM.md`](TEAM.md) §3.1. Everything is on `http://127.0.0.1:<port>`.

| Endpoint | What |
|---|---|
| `POST /api/chat` | `{messages, stream, device?}` → tool-calling agent; streamed as `status* → token* → sources → done \| error` |
| `POST /api/ask` | `{q}` → one-shot answer with `[#id]` citations |
| `POST /api/draft` | `{ids, kind}`, kind = email / message / todo / notes |
| `GET /api/standup?kind=standup\|timesheet` | standup or timesheet from today's activity |
| `GET /api/commitments`, `POST /api/commitments/{id}/done` | things you promised |
| `GET /api/search`, `/api/browse`, `/api/timeline`, `/api/capture/{id}`, `/img/{id}` | memory |
| `GET /api/stats`, `/api/health` | counters; can this laptop reach the GX10 models |
| `POST /ingest`, `/agent/ping` | capture agent |
| `POST /api/transcribe` | speech-to-text via the GX10 (not used by the app yet) |
