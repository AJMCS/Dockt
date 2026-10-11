# Dockt — your workday, remembered. 100% local.

If you're a lawyer, work in finance or healthcare, or are under an NDA, you can't paste your screen into a cloud AI. So your workday has no memory: the address someone mentioned in a chat, the invoice you glanced at, the thing you promised to send by Monday.

**Dockt gives you that memory, and none of it leaves the room.** Your laptop keeps every screenshot and the search index. An ASUS Ascent GX10 on the local network reads each screenshot for a second or two, answers your questions, and stores nothing. The cloud never hears about it.

> Your laptop remembers. The box in the room thinks. The cloud never hears about it.

Built in one weekend for the **ASUS Local AI track of the UCSC AI Hackathon** (October 2026).

---

## What it does

- **Remembers what you saw.** A capture agent takes a screenshot only when your screen changes to something new. The check costs about 2 ms on a tiny thumbnail, so an idle screen sends nothing. Password managers and private or incognito windows are always skipped, and you can pause capture at any time.
- **Answers with proof.** Ask "Where is dinner tonight and who booked it?" The answer streams in, and every claim cites the screenshot it came from as a chip like `#9`. Click the chip to see the exact pixels, the model's summary and the text on screen. If it isn't on your screens, it says so instead of guessing.
- **Keeps track of what you promised.** While you work, Dockt pulls promises, deadlines and requests from your screen ("I'll send the revised NDA by Monday") into a checklist, each linked to its screenshot.
- **Acts on it.** Draft a follow-up email or message from any answer or screenshot. Write your standup, or draft a timesheet from what you actually did today.
- **Feels like an app, not a website.** A desktop window with chat history in a Claude-style sidebar, light and dark themes, and a quick-chat pet on your desktop (`Ctrl+Space` from anywhere).

## How it works

```
 ┌──────────────── your laptop (Windows) ─────────────────┐        ┌──────── ASUS GX10 (in the room) ────────┐
 │                                                        │        │                                         │
 │  capture agent ── screenshot on change ──► local Dockt │  LAN   │  Ollama, three models always loaded:    │
 │                                             service    │ ─────► │   gemma4:26b       reads screenshots    │
 │  desktop app + quick-chat ◄── answers ──── 127.0.0.1   │ ◄───── │   nemotron3:33b    tool-calling agent   │
 │                                              :8000     │        │   nomic-embed-text search vectors       │
 │  screenshots + SQLite index live here, nowhere else    │        │  stores nothing                         │
 └────────────────────────────────────────────────────────┘        └─────────────────────────────────────────┘
```

1. **Capture.** The agent posts a JPEG to the local service on the laptop, which saves it there.
2. **Understand.** The laptop sends the image to the GX10's vision model, which returns a summary and the screen text. The embedding model turns that into a vector. Both are stored in the laptop's SQLite database, which uses FTS5 for keyword search.
3. **Ask.** A tool-calling agent running Nemotron 3 on the GX10 decides what to look up: it can search your screens, read your activity for a time range, open specific captures, list your promises or write a draft. It answers only from what it found, citing every claim as `[#id]`.
4. **Watch for promises.** A background extractor reads new captures for promises, deadlines and requests.

**Measured on the GX10:**
- About 1.5 s to read a simple screenshot, about 5.6 s for a dense one.
- About 28 screenshots/min end to end, with promise extraction running.
- Answers in about 3 s.
- All three models stay resident in about 54 GB of the 128 GB unified memory.

**Privacy, by design:**
- The local service only listens on `127.0.0.1`.
- Screenshots and the index never leave the laptop. The GX10 sees an image only while describing it.
- The dashboard loads no fonts, scripts or icons from the internet. A regression test fails if it makes any request off the laptop.
- To prove it in the demo, we unplug the router's internet uplink and everything keeps working.

## Run it

**Windows (the demo setup).** You need Python 3.11+. From `dockt-patched/`:

```powershell
echo 192.168.8.10> gx10.txt        # your GX10's address (or set DOCKT_GX10)
start-dockt.bat                     # first run sets up .venv; add --mock to run without a GX10
```

On campus Wi-Fi, where the GX10's port can't be reached directly, run `desktop\Setup-GX10.bat` once instead; Dockt then reaches the GX10 through an SSH tunnel.

This starts the local service, screen capture and the quick-chat pet. Right-click the pet for controls: **Open Dashboard**, pause or resume capture, and **Quit Dockt**. Click the pet or press `Ctrl+Space` to ask a quick question. Closing the dashboard window hides it; Quit ends everything.

**Developing on macOS/Linux (mock models, no GX10 needed):**

```bash
cd dockt-patched
python3 -m venv .venv && .venv/bin/pip install -r requirements-server.txt -r requirements-agent.txt
.venv/bin/uvicorn --app-dir tools mock_models:app --port 9000 &                 # mock models
VLM_URL=http://127.0.0.1:9000/v1 .venv/bin/python server/app.py &               # local service on :8000
.venv/bin/python tools/replay.py --server http://127.0.0.1:8000 --synthetic 24   # fake screenshots
open http://127.0.0.1:8000                                                       # the dashboard
```

More detail, including env vars, capture tuning and the demo checklist, is in [`dockt-patched/README.md`](dockt-patched/README.md).

## Tests

```bash
cd dockt-patched
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest tests -q
```

The suite has 89 tests. It starts its own mock models and Dockt service on free ports with a throwaway data folder, then drives the dashboard in a real browser. It covers:
- streaming answers and citation rules
- failure paths
- drafts and the screenshot modal
- deep links
- promises, standup and timesheet
- the timeline
- chat history, including with the GX10 down
- the on-device badge
- both themes, including contrast checks
- layout from phone to projector size
- the desktop app and the local API

See [`dockt-patched/tests/dashboard/README.md`](dockt-patched/tests/dashboard/README.md).

## How we built it

Four people, four lanes, black-box contracts. Each lane owned its files and talked to the others only through the HTTP and Python interfaces in [`TEAM.md`](dockt-patched/TEAM.md) §3, so all four could build in parallel against mocks.

| Lane | Owner | What | Where |
|---|---|---|---|
| 1. Capture + desktop client | Rik Ros | capture agent, quick-chat pet, Windows launcher | `agent/`, `start-dockt.bat` |
| 2. App | Brandon Shen | dashboard, desktop window, design system, regression tests | `server/static/`, `desktop/`, `tests/dashboard/`, [`team/DESIGN.md`](dockt-patched/team/DESIGN.md) |
| 3. Agent | cyshello | tool-calling `/api/chat` agent, drafts, standup, promise extraction | `server/brain.py` |
| 4. Models + memory | AJ McDaniel | GX10 model serving, model client, search, indexer, mocks | `server/models.py`, `store.py`, `memory.py`, `indexer.py`, `tools/` |

**Stack:**
- **Laptop:** FastAPI, SQLite FTS5 and NumPy vectors, pywebview (WebView2 on Windows), plain HTML/JS for the dashboard.
- **GX10:** Ollama's OpenAI-compatible API.

**Challenges:**
- Thinking models returned empty answers through Ollama until we sent `reasoning_effort: "none"`.
- Nemotron cited `[9]` instead of `[#9]`, so both the agent and the UI accept either form.
- Windows-first details: no `tzset`, and cp1252 encodings.
- Keeping capture from every laptop under what the GX10 can read per minute.

**What's next:**
- Voice questions: local Whisper transcription is wired on the server but not in the app yet.
- Proactive nudges ("the redline is due tomorrow").
- An end-of-day summary.
- Retention controls.
- Several devices feeding one memory: the index already keys every capture by device.
