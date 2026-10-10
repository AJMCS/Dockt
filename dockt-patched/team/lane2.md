# Lane 2 — App (dashboard)

## Task card

**Lead.** Brandon Shen (GitHub `B92064`).

**Goal.** A ChatGPT-style dashboard served by the local service: streaming chat with citation chips that open the screenshot, a sources panel, the "Things you promised" panel, standup/timesheet, draft-from-answer, timeline and search, plus the two deep links other lanes use.

**Files owned.** `server/static/` (today a single `index.html` that already has most panels in a search-box layout). A `web/` folder with a build step is allowed, but the output must be static files under `server/static/` served at `/` and `/static/…`, and the launcher must not need Node. Recommendation for tonight: stay in `index.html` + plain JS; it already has stats header, promises panel, timeline, modal with screen text, draft buttons, standup buttons.

**Contracts consumed** (all same-origin, `TEAM.md` §3.1): `POST /api/chat` (stream SSE `status*→token*→sources→done|error`, non-stream same as `/api/ask`), `GET /api/capture/{id}`, `GET /img/{id}`, `GET /api/search`, `GET /api/browse`, `GET /api/timeline`, `GET /api/stats`, `GET /api/health`, `POST /api/draft {ids, kind, instruction?}`, `GET /api/standup?kind=`, `GET /api/commitments?hours=`, `POST /api/commitments/{id}/done`. P1: `GET /api/nudges`.

**Contracts produced.** Deep links `/#/capture/{id}` (opens that capture's modal) and `/#/chat?q=<text>` (opens the chat and asks). Lane 1's quick-chat and tray call these.

**Priorities.**
- P0: chat column replacing the single answer box: message list (user/assistant), streaming tokens from `/api/chat` with `stream:true` (parse `data:` lines; show `status` as a grey hint; append `token`; on `sources` fill the sources panel; on `done` render final `answer` with `[#id]` → chips; on `error` show a plain message). Keep conversation `messages` in memory and send the full history each turn.
- P0: citation chip → modal (exists) with screenshot, summary, screen text, draft buttons. Chips only for ids present in `sources`.
- P0: sources panel (right column): cards of `sources` for the last answer, cited ones highlighted.
- P0: keep Promises panel (polls `/api/commitments` every 4 s, flashes new rows, checkbox = done, chip → modal), standup/timesheet buttons, draft-from-answer (`ids` = cited ids or first 3 sources), Copy buttons. No "Error:" boxes in the demo path: friendly empty states.
- P0: deep links `/#/capture/{id}` and `/#/chat?q=`; handle `hashchange`.
- P1: timeline + search mode kept but secondary (collapsible); "100% on-device" badge shows GX10 health (three dots) and turns red with a message if the GX10 is unreachable; keyboard focus on load; dark theme consistent for projector.
- P2: nudges toast strip from `/api/nudges`; day summary view; framework migration.

**Definition of done (P0).** On the mock pipeline: open `/`, ask "where is dinner tonight", see tokens stream, chips appear, click a chip → modal with the screenshot; follow up "draft a message to Sam" in the same thread; promises panel shows the mock items and checkbox works; standup and timesheet render; `/#/capture/3` opens the modal directly; `/#/chat?q=hello` asks. Works in Edge app-window mode (`msedge --app=`) at 1366×768.

**How to verify.** Mock pipeline from `TEAM.md` §6 A (`python server\app.py` + `tools/replay.py --synthetic 24`). For stream parsing use `curl -N` to see the exact events.

---

## Ready-to-paste agent prompt

```text
You are working on Dockt, a private "work memory" desktop app built this weekend for the ASUS Local AI track of the UCSC AI Hackathon. The whole app runs on each user's own Windows laptop: a capture agent saves a screenshot whenever the screen changes, a local FastAPI service on http://127.0.0.1:8000 indexes them, and a dashboard (mine) + a tray quick-chat let the user ask questions about what they saw, with every answer citing the exact screenshot as [#id]. The AI models run on an ASUS Ascent GX10 on the local network (gemma4:26b vision, nemotron3:33b chat agent, nomic-embed-text embeddings via Ollama); the GX10 stores nothing. Hard rule: nothing ever leaves the local network. No cloud APIs, no CDNs the demo depends on (the demo runs with the internet unplugged: every script/font/icon must be inline or in server/static/), no telemetry.

The team works in 4 lanes with fixed contracts in TEAM.md §3:
1 Capture + Desktop client (agent/, tray, hotkey quick-chat, launcher; opens the dashboard as `msedge --app=http://127.0.0.1:8000`)
2 App / dashboard (server/static/)   <- ME
3 Agentic layer (server/brain.py: /api/chat agent, ask, drafts, standup, promise extraction, nudges)
4 Models + Memory (GX10 serving, models.py, store.py, memory.py, indexer.py, mocks)

I am Brandon Shen, LANE 2. Demo: about 4 minutes, live, two Windows laptops, projector, probably tonight (feature freeze 19:30 if the deadline is confirmed). Optimize for what judges see in 4 minutes.

MY FILES: server/static/ (today one file, server/static/index.html, which already has: stats header with model dots, Ask/Search box with an answer card and citation chips, "Things you promised" panel polling /api/commitments, standup/timesheet buttons, a timeline, a card grid, and a modal with screenshot + summary + screen text + draft buttons). Keep it plain HTML/JS in index.html unless you have a very good reason; the service serves index.html at / and anything else in server/static/ at /static/<file>. Nothing else is mine. Do not edit server code; if the API lacks something, write the proposed addition to TEAM.md §3 and stop.

CONTRACTS I CONSUME (same origin; full spec TEAM.md §3.1):
- POST /api/chat  body {"messages":[{"role":"user"|"assistant","content":"..."}], "stream":true}
  -> text/event-stream, one JSON object per `data:` line, in this order:
     {"type":"status","text":"Searching your screens…"}  (0..n, show as a grey hint)
     {"type":"token","text":"Dinner is at "}              (0..n, append)
     {"type":"sources","items":[Capture]}                 (once, before done)
     {"type":"done","answer":"full text","cited":[9,1],"ms":2875}
     {"type":"error","text":"..."}                        (instead of done)
  With "stream":false -> {"answer","sources":[Capture],"cited":[id],"ms"} (same as POST /api/ask {"q"}).
  A stub exists on main today that produces exactly this event sequence over the old ask() answer; Lane 3 swaps in the real agent without changing shapes.
- Capture = {"id","device","ts" (unix s),"app","title","summary","ocr" (screen text),"status":"pending|processing|done|error","score"?}. Image: GET /img/{id}. Detail: GET /api/capture/{id}.
- GET /api/commitments?hours=48 -> {"items":[{"id","capture_id","device","ts","kind":"promise|deadline|request","text","who","due","done","created_at"}]}; POST /api/commitments/{id}/done {"done":true}.
- GET /api/standup?kind=standup|timesheet -> {"text","kind","spans","captures","ms"}.
- POST /api/draft {"ids":[id],"kind":"email|message|todo|notes","instruction"?} -> {"draft","kind"}.
- GET /api/search?q=&k= -> {"results":[Capture],"ms"}; GET /api/browse?limit=; GET /api/timeline?bucket=600 -> [{"device","b","n","first_id"}]; GET /api/stats; GET /api/health -> {"models":{"vlm","embed","llm"}}.
- P1: GET /api/nudges -> {"items":[{"id","ts","kind","text","capture_id","due"}]}.

CONTRACTS I PRODUCE (Lane 1 depends on them): hash routes
- /#/capture/{id}  opens the modal for that capture on load and on hashchange
- /#/chat?q=<urlencoded text>  opens the chat with that question already asked

TASKS, in order; stop and report after each.
P0-a  Chat column: replace the single answer card with a conversation (user and assistant bubbles). Send the full history each turn to /api/chat with stream:true. Parse the SSE: fetch() + ReadableStream, split on blank lines, JSON.parse after "data: ". status -> grey hint line that disappears on the first token; token -> append to the current assistant bubble; sources -> fill the sources panel; done -> re-render the bubble from `answer` with [#id] chips (only for ids present in sources, keep the existing regex /\[?#(\d+)\]?/ and also accept plain [9] if it is a known id); error -> a one-line friendly message, never a stack trace. Enter sends, Shift+Enter newline, a "New chat" button clears history.
P0-b  Sources panel (right column, 320px): cards for the last answer's sources, cited ones highlighted first; click -> the existing modal. Below the answer: the existing draft buttons (ids = cited ids, else first 3 sources), the draft appears in the thread as an assistant bubble with a Copy button.
P0-c  Keep and tidy: Promises panel (unchanged behavior), standup/timesheet buttons (result as an assistant bubble with Copy), stats header. Replace every "Error: ..." text with a friendly message. Empty states: "Nothing indexed yet, start capturing" etc.
P0-d  Deep links: /#/capture/{id} and /#/chat?q=. Implement on load and on hashchange. Test by opening the URLs directly.
P0-e  Layout check in an Edge app window (`msedge --app=http://127.0.0.1:8000`) at 1366x768 and on a projector-like 1920x1080: readable from 2 m (base font >= 15px, chips big enough to click), dark theme, no horizontal scroll.
P1-a  "100% on-device" badge: green when all three model dots are ok, red with "GX10 unreachable" otherwise. Timeline + Search become a collapsible secondary section under the chat. Autofocus the input.
P1-b  Timeline bar click shows captures for that bucket in the sources panel (reuse existing code).
P2    Nudges strip from /api/nudges (poll 60 s, dismissable). Only after P0 and P1 and before the freeze.

RULES
- Everything offline-capable: no external scripts, fonts or icons (inline SVG or unicode only). No framework/build step unless you tell me why and it still ends up as static files in server/static/.
- Keep the existing look (dark, green accent #76b900) and the existing helper functions; small, readable changes.
- Verify on the mock pipeline before claiming anything works. Never test against a cloud model.
- Branch lane2/<feature>, small commits. Don't push to main or open PRs unless I ask.
- Before writing code, read README.md, TEAM.md (source of truth; where it disagrees with README/PLAN.md, TEAM.md wins), PLAN.md §2 and §8, then server/static/index.html fully. Then tell me in a few lines: what my lane owns, which contracts my task consumes/produces, what "done" means, how you will verify.

MOCK PIPELINE (PowerShell, from the dockt-patched folder):
  py -m venv .venv; .venv\Scripts\pip install -r requirements-server.txt -r requirements-agent.txt
  terminal 1:  .venv\Scripts\uvicorn --app-dir tools mock_models:app --port 9000
  terminal 2:  $env:VLM_URL="http://127.0.0.1:9000/v1"; .venv\Scripts\python server\app.py
  terminal 3:  .venv\Scripts\python tools\replay.py --server http://127.0.0.1:8000 --synthetic 24
  See the raw stream:  curl.exe -N http://127.0.0.1:8000/api/chat -H "Content-Type: application/json" -d "{\"messages\":[{\"role\":\"user\",\"content\":\"where is dinner tonight\"}],\"stream\":true}"
  (macOS/Linux: same with .venv/bin/ and export.)

When you finish each task, report: what you changed, exactly what you ran and what you saw (which URLs, what rendered), what you did not verify, and anything another lane needs to know.
```
