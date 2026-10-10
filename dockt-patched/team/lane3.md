# Lane 3 — Agentic layer ("the brain")

## Task card

**Goal.** `POST /api/chat` becomes a real multi-turn, tool-calling agent over the laptop's memory with streaming, strict `[#id]` citations and "not found" discipline; promise extraction and standup/timesheet produce demo-quality output on real captures; the 5 rehearsed questions answer correctly 3× in a row.

**Files owned.** `server/brain.py` (all of it: ask, chat, draft, standup, commitments endpoints, extractor loop, every prompt). Nothing else. Memory is reached only through `memory.*`, models only through `models.chat` / `models.chat_messages`.

**Contracts consumed** (`TEAM.md` §3.2): `memory.search(q,k,device,start,end)` (async), `memory.get/many/activity/commitments/add_commitments/set_commitment_done`, `models.chat(system,user,…)`, **`models.chat_messages(messages, tools, max_tokens, temperature, stream)`** → non-stream `{"content", "tool_calls":[{"id","name","arguments":dict}]}`; stream → async iterator of `{"type":"token","text"}` … `{"type":"done","content","tool_calls"}`. Lane 4 ships the mock first (`tools/mock_models.py` + `models.py`); until then build against the mock.

**Contracts produced** (`TEAM.md` §3.1): `POST /api/chat` (stream `status*→token*→sources→done|error`; non-stream = ask shape), `GET /api/nudges`, plus the existing `/api/ask`, `/api/draft`, `/api/standup`, `/api/commitments*`. Shapes are frozen; adding optional fields is fine.

**Verified facts to build on** (GX10, Sat 15:25): Nemotron 3 33B via Ollama returns proper `tool_calls` with `reasoning_effort:"none"` (0.8 s for a short prompt), answers after a `tool` message, and streams. It cited `[9]` instead of `[#9]` when the tool result was JSON: force the format in the system prompt and parse both. A tool call arrives as a single chunk in streaming mode, so stream only the final answer turn.

**Priorities.**
- P0: agent loop in `/api/chat`: system prompt (who/what, current time, rules), tools `search_screens(q, start?, end?)`, `get_activity(start, end)`, `get_captures(ids)`, `list_commitments(hours?, include_done?)`, `draft(ids, kind, instruction?)`; max 4 tool rounds; collect every capture id returned by tools into `sources`; final turn streamed as `token` events; `done` with `answer`, `cited` (ids in the answer that exist in sources), `ms`. Non-stream path returns the same fields. `status` events name the tool ("Searching your screens for 'dinner'…"). Resolve relative times with the existing `time_window()` on the latest user message, and pass `start/end` hints to the model.
- P0: citation and honesty discipline: every factual claim cites `[#id]`; if tools return nothing relevant, answer "I didn't see that on your screens" (no guessing); never cite ids that weren't returned by a tool; tool results are compact text (`[#id] time · app — title: summary / screen text ≤ 400 chars`), not raw JSON.
- P0: tune `EXTRACT_SYSTEM` on real captures (Lane 4 can give you a laptop with real data, or run the service on your own laptop against the GX10): valid JSON ≥ 9/10, no items from code editors, terminals, menus; kinds right. Add an app skip list in the claim if needed (one `WHERE` clause in `memory.store.claim_extract` is Lane 4's; ask them, or filter in `extract_one` before the model call).
- P0: run the 5 rehearsed questions + the negative one (ORCHESTRATION.md §3) on real data, 3× each; fix wording in prompts until stable.
- P1: "write that as an email/message" works from chat via the `draft` tool, the draft comes back as the answer text with the cited ids kept.
- P1: `STANDUP_KINDS` wording so the timesheet is a clean table with plausible durations and the standup is < 150 words.
- P2: `GET /api/nudges` real: commitments with a due date within 24 h or older than 24 h and not done → nudge items; end-of-day summary endpoint (`GET /api/standup?kind=eod`) only if time remains.

**Definition of done (P0).** On the mock: `curl -N /api/chat` with a 3-message history produces the event sequence and `done.cited ⊆ sources ids`. On the GX10: the 6 rehearsed questions answer correctly 3× in a row with clickable citations, the negative question says "not found", `promise found in #…` log lines appear for the 3 planted moments and nothing embarrassing, Ask/chat latency ≤ 6 s on a real afternoon of data.

**How to verify.** Mock pipeline (`TEAM.md` §6 A) for shapes; real pipeline (§6 B, `VLM_URL=http://<gx10-ip>:11434/v1 VLM_MODEL=gemma4:26b LLM_MODEL=nemotron3:33b`) for quality. Until Ollama is open on the LAN, Lane 4 can run your branch on the GX10 itself against `localhost:11434` (temporary; the old service already runs there on :8000).

---

## Ready-to-paste agent prompt

```text
You are working on Dockt, a private "work memory" desktop app built this weekend for the ASUS Local AI track of the UCSC AI Hackathon. The whole app runs on each user's own Windows laptop: a capture agent saves a screenshot whenever the screen changes, a local FastAPI service on http://127.0.0.1:8000 indexes them (SQLite FTS5 + numpy vectors), and a dashboard + tray quick-chat let the user ask questions about what they saw, with every answer citing the exact screenshot as [#id]. The AI models run on an ASUS Ascent GX10 (NVIDIA GB10, 128 GB) on the local network through Ollama's OpenAI-compatible API: gemma4:26b (vision), nemotron3:33b (chat + tool calling, 16k context), nomic-embed-text (embeddings). The GX10 stores nothing. Hard rule: nothing ever leaves the local network. No cloud APIs, no telemetry, no hosted models, not even for testing.

The team works in 4 lanes with fixed contracts in TEAM.md §3:
1 Capture + Desktop client (agent/, tray, hotkey quick-chat calling POST /api/chat non-stream)
2 App / dashboard (server/static/, calls POST /api/chat with stream:true, renders [#id] chips)
3 Agentic layer (server/brain.py)   <- ME
4 Models + Memory (GX10 serving, server/models.py incl. chat_messages(), store.py, memory.py, indexer.py, tools/mock_models.py)

I am [NAME], LANE 3. Demo: about 4 minutes, live, probably tonight (feature freeze 19:30 if the deadline is confirmed). Judges see: ask → streamed cited answer → click citation → follow-up "draft a message" → the Promises panel filling itself → a timesheet. Quality of those outputs is my job.

MY FILE: server/brain.py only (a FastAPI APIRouter with /api/ask, /api/chat [stub today], /api/nudges [stub], /api/draft, /api/standup, /api/commitments, /api/commitments/{id}/done, the background extractor loop, and all prompts: ASK_SYSTEM, DRAFT_SYSTEM, STANDUP_*, EXTRACT_SYSTEM). I read memory only through `import memory` (memory.search is async and embeds the query itself; memory.get/many/activity/commitments/add_commitments/set_commitment_done are sync; memory.store.claim_extract / mark_extracted are the extractor's queue) and models only through `import models` (models.chat(system, user, max_tokens, temperature) -> str, and the NEW models.chat_messages below). Do not edit memory.py, store.py, models.py, app.py or static files. If a contract is missing, write the proposed change to TEAM.md §3 and stop.

CONTRACT I CONSUME FROM LANE 4 (TEAM.md §3.2; Lane 4 ships a mock first, then the real one; build against whichever exists):
  await models.chat_messages(messages: list[dict], tools: list[dict] | None = None, max_tokens=700, temperature=0.2, stream=False)
    stream=False -> {"content": str, "tool_calls": [{"id": str, "name": str, "arguments": dict}]}
    stream=True  -> async iterator of {"type":"token","text":str} ... then {"type":"done","content":str,"tool_calls":[...]}
  messages use OpenAI roles: system / user / assistant (with "tool_calls": [{"id","type":"function","function":{"name","arguments": json string}}]) / tool (with "tool_call_id" and "content"). tools use the OpenAI {"type":"function","function":{"name","description","parameters"}} schema.
  Until Lane 4 merges it, if `models.chat_messages` does not exist, fall back to the current stub behavior so main never breaks.

CONTRACT I PRODUCE (TEAM.md §3.1; shapes frozen, Lanes 1 and 2 are building against them right now):
  POST /api/chat {"messages":[{"role","content"}], "stream": bool, "device"?: str}
    stream:true -> text/event-stream, one JSON per `data:` line, in order:
      {"type":"status","text":"Searching your screens for 'dinner'…"}   0..n
      {"type":"token","text":"..."}                                      0..n (final answer only)
      {"type":"sources","items":[Capture]}                               once
      {"type":"done","answer":"...","cited":[9,1],"ms":2875}             or {"type":"error","text":"..."}
    stream:false -> {"answer","sources":[Capture],"cited":[id],"ms"} (same as /api/ask)
  GET /api/nudges?since= -> {"items":[{"id","ts","kind","text","capture_id","due"}]}
  Existing: POST /api/ask, POST /api/draft {ids, kind, instruction?} -> {draft, kind}, GET /api/standup?kind=standup|timesheet -> {text, kind, spans, captures, ms}, GET /api/commitments -> {items:[Commitment]}, POST /api/commitments/{id}/done.
  Capture = {id, device, ts, app, title, summary, ocr, status}. Commitment = {id, capture_id, device, ts, kind: promise|deadline|request, text, who, due, done, created_at}.

VERIFIED FACTS (orchestrator, on the GX10, today 15:25): Nemotron 3 33B through Ollama returns proper tool_calls when given `tools` and `reasoning_effort:"none"` (0.8 s short prompt), answers correctly after a `tool` message, and streams; a tool call arrives as ONE chunk when streaming. It cited "[9]" instead of "[#9]" when the tool result was JSON, so: force "[#id]" in the system prompt, give tool results as compact text lines starting with "[#id]", and parse both "[#9]" and bare "[9]" when the number is a known source id.

TASKS, in order; stop and report after each.
P0-a  Agent loop in /api/chat. System prompt: you answer questions about the user's own screen history; current local time; rules (use tools before answering, cite every fact as [#id] with ids from tool results only, say "I didn't see that on your screens" when nothing relevant comes back, be concise and specific: names, numbers, times). Tools: search_screens(q: str, start?: number, end?: number) -> memory.search(q, 12, device, start, end) rendered as "[#id] Sat 13:12 · app — title: summary | screen text ≤ 400 chars" lines; get_activity(start, end) -> memory.activity(...) rendered with the existing _activity_text; get_captures(ids) -> memory.many; list_commitments(hours?, include_done?) -> memory.commitments; draft(ids, kind, instruction?) -> the existing draft logic, returned as text. Pass a time hint computed with the existing time_window() on the last user message ("hint: 'tonight' = 17:00–24:00 today, now is …"). Max 4 tool rounds, then force a final answer. Every capture id returned by any tool goes into a sources dict (id -> Capture). Final turn: stream tokens; done.cited = ids in the answer ∩ sources; done.sources = cited first, then the rest (cap 12). Non-stream path returns the same fields without events. status events before each tool call. Any exception -> {"type":"error","text"} (stream) or HTTP 500 with the text (non-stream). Keep /api/ask as is (Lane 1 fallback).
P0-b  Make it right on the mock first: `curl -N` with a 3-message history must produce status*, token*, sources, done; done.cited ⊆ source ids; a question with no matches ends in a "didn't see" answer with empty cited. Then on the real GX10 (ask me for the address or run on the GX10 against localhost:11434): the six rehearsed questions below, 3 runs each, and post the answers.
P0-c  EXTRACT_SYSTEM on real captures: read raw model output for ~20 captures (temporary print), tune until JSON is valid ≥ 9/10, nothing from code editors/terminals/menus/ads, kinds right, text keeps names/dates/numbers. Keep the JSON shape (the UI depends on it). If false positives come from specific apps, skip them in extract_one before the model call (app name match), nothing fancier.
P1-a  "Write that as an email / message to X" from chat uses the draft tool and returns the draft as the answer (keep [#id] of the captures it was built from). STANDUP_KINDS wording: timesheet = clean markdown table with plausible merged durations and a total; standup < 150 words, specific.
P2    /api/nudges real: open commitments whose `due` parses to within 24 h, or seen > 24 h ago and still open -> items with a one-line nudge text. End-of-day summary only if everything above is done and it is before the freeze.

REHEARSED QUESTIONS (must be right 3x in a row on the demo data; planted moments come from Lane 1/4):
  1 "Where is dinner tonight and who booked it?"  2 "What's the invoice number and when is it due?"  3 "What did I work on this afternoon?"  4 "Did anyone ask me to review something?"  5 follow-up turn: "Draft a message to Sam saying I'm in."  6 negative: "What seat am I in on the flight?" -> not found.

RULES
- Prompt changes only, no fine-tuning, no new models, no new dependencies. Keep the Ask prompt under ASK_MAX_CHARS; Nemotron context is 16k. Temperature 0–0.2 for answers.
- Verify on the mock before claiming anything; use the real GX10 for quality. Never test against a cloud model. Don't change anything on the GX10 (Lane 4 owns it).
- Windows first: no Unix-only calls. Branch lane3/<feature>, small commits. Don't push to main or open PRs unless I ask.
- Before writing code, read README.md, TEAM.md (source of truth; where it disagrees with README/PLAN.md, TEAM.md wins), PLAN.md §2 and §8, then server/brain.py, server/memory.py and server/models.py fully (read-only for the last two). Then tell me in a few lines: what my lane owns, which contracts my task produces/consumes, what "done" means, how you will verify.

MOCK PIPELINE (PowerShell, from the dockt-patched folder; macOS/Linux same with .venv/bin and export):
  terminal 1:  .venv\Scripts\uvicorn --app-dir tools mock_models:app --port 9000
  terminal 2:  $env:VLM_URL="http://127.0.0.1:9000/v1"; .venv\Scripts\python server\app.py
  terminal 3:  .venv\Scripts\python tools\replay.py --server http://127.0.0.1:8000 --synthetic 24
  stream test: curl.exe -N http://127.0.0.1:8000/api/chat -H "Content-Type: application/json" -d "{\"messages\":[{\"role\":\"user\",\"content\":\"where is dinner tonight\"}],\"stream\":true}"
REAL MODELS: terminal 2 with $env:VLM_URL="http://<gx10-ip>:11434/v1"; $env:VLM_MODEL="gemma4:26b"; $env:LLM_MODEL="nemotron3:33b"; $env:EMBED_MODEL="nomic-embed-text" and a separate $env:DATA_DIR for test data.

When you finish each task, report: what you changed (functions/prompts), exactly what you ran and what you saw (paste the answers and citations), what you did not verify, and anything another lane needs to know.
```
