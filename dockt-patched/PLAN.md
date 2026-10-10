# Dockt — build plan for the ASUS Local AI track

**Who this is for:** the four of us, and any Claude/Opus session asked to work on this repo. Read this before touching code. If you are an AI session: follow the rules in §2, do only the task you were given, and report what you verified versus what you assumed.

**Deadline:** demo locked Saturday night. Sunday morning is polish only. Devpost due before noon Sunday.

---

## 1. What we are building (one paragraph, memorize it)

Dockt is private work memory for a team. Laptops run a 2 ms screen-change check and send only new frames to an ASUS Ascent GX10 on the local network. The GX10 runs three models at once (vision model for screen summaries and text, embedding model for search, Nemotron for answers, drafts, and commitment extraction) and keeps an index you can ask questions about. Nothing leaves the room. The two lead features are **"Things you promised"** (promises, deadlines, and requests pulled automatically from what you saw) and **a standup or timesheet drafted from your day**. Every answer cites the actual screenshot.

**Judging criteria:** real problem · innovation and use of local hardware · value of local AI · how engaging the demo is.

**What we are not:** a Recall clone. Recall and screenpipe are single-device recorders. Our differentiator is a shared appliance running models a laptop cannot, plus proactive extraction. Say this in every pitch conversation.

---

## 2. Rules (humans and AI sessions)

1. **Hackathon impact over production quality.** A rough feature that lands in the demo beats a clean refactor nobody sees.
2. **No refactors, no restyling, no new abstractions** beyond what a change needs. No new dependencies unless unavoidable.
3. **Nothing goes to a cloud service. Ever.** No cloud APIs, no telemetry, no "just for testing."
4. **Test with the mock first.** `cd tools && uvicorn mock_models:app --port 9000`, then `cd server && VLM_URL=http://localhost:9000/v1 uvicorn app:app --port 8000`, then `python tools/replay.py --server http://localhost:8000 --synthetic 24`. If it doesn't work on the mock, it doesn't go on the GX10.
5. **Don't touch the serving config on the GX10 without telling the serving owner (§4).** One person owns that box.
6. **Claim only what you verified.** Say "ran it, output was X" or "did not run, assumed Y." Never report a feature as working if you only read the code.
7. **Don't fine-tune anything.** Prompt changes only.
8. **Commit small and often.** Branch per task, merge to main when it runs on the mock.

---

## 3. Current state (as of Saturday midday)

**Working, verified end to end on mock models:** capture agent (macOS/Windows change detection, spooling), ingest, background indexing (vision model → embedding), hybrid search (vectors + FTS5 keywords, reciprocal rank fusion), Ask with citations and a time-aware activity log, Draft (email/message/todo/notes), timeline, UI.

**Added in the patched zip (`dockt-patched.zip`, tested on mock; apply it first if not already applied):**
- `store.py`: `commitments` table, `extracted` column migration, dedup, commitment queries.
- `app.py`: timezone pin, background `extractor()` loop (third model loop beside the vision pass), `/api/commitments`, `/api/commitments/{id}/done`, `/api/standup?kind=standup|timesheet`, Ask hardening (prompt cap 20k chars, 60 spans, 500 answer tokens, two captures per window max, empty-answer guard).
- `models.py`: `chat_template_kwargs: {enable_thinking: false}` alongside `/no_think`.
- `index.html`: "Things you promised" panel (polls every 4 s, flashes new rows, checkbox = done, chip opens the screenshot), standup/timesheet buttons, citation chips only for real source ids.
- `mock_models.py`: fake extraction and standup responses so the panel works without a GPU.

**Not yet verified with real models:** everything. Answer quality, extraction JSON reliability, thinking-mode behavior of the preloaded Nemotron, vision throughput on the GX10, real screen capture on the actual demo laptops.

---

## 4. Work split (four people, Saturday)

Owner names are placeholders; fill them in. Each task says what "done" means.

### A. Serving owner — GX10 models (deadline: 12:30, hard stop 13:00)
- Serve vision (Qwen2.5-VL-7B), embeddings (bge-m3 or nomic-embed-text), and Nemotron on OpenAI-compatible endpoints. vLLM preferred; **if any model isn't up on vLLM by 12:30, run that model on Ollama** with `OLLAMA_MAX_LOADED_MODELS=3 OLLAMA_NUM_PARALLEL=4 OLLAMA_CONTEXT_LENGTH=16384 ollama serve`. The app doesn't care which.
- Nemotron **must** have a 16k context: vLLM `--max-model-len 16384`, or the Ollama env above. The Ask prompt measures 7–9k tokens on a real day; an 8k window silently breaks answers.
- Run `python tools/bench.py --url http://localhost:8001/v1 --model <vlm>` once. Write the img/min number on the whiteboard. If it's under ~20, tell the agent owner to use `--max-width 1280`.
- Export the env vars from the README §2 plus `DOCKT_TZ=America/Los_Angeles`, start the server, confirm `/api/health` shows three green dots **and** that `LLM_URL` is not accidentally the vision server (the defaults fall through to `VLM_URL`).
- **Done when:** `curl -X POST localhost:8000/api/ask -d '{"q":"what did I do in the last hour"}'` returns a real answer with a `[#id]` citation in under 20 s, and one extraction log line (`promise found in #…`) has appeared.

### B. Promises owner — "Things you promised" with real Nemotron (13:00–17:00)
- Feed real captures containing a promise ("I'll send the revised NDA by Monday" in Slack), a deadline (invoice due date in Gmail), and a request ("can you review the redline?" in a Jira comment).
- Read the raw Nemotron output for `EXTRACT_SYSTEM` (add a `print(out)` in `extract_one` temporarily). Tune the prompt until: valid JSON ≥ 9 of 10 times, no items from code editors or menus, kinds are right. Do not change the JSON shape; the UI depends on it.
- If false positives come from code/terminal windows, add an app skip list in `claim_extract` (one `WHERE app NOT IN (...)` clause), nothing fancier.
- **Done when:** the three planted moments appear in the panel within 60 s of being on screen, with the right kind, and nothing embarrassing appears alongside them.

### C. Standup/timesheet + Ask quality owner (13:00–17:00)
- Run `/api/standup?kind=standup` and `kind=timesheet` on a real afternoon of capture. Tune `STANDUP_KINDS` wording until the timesheet is a clean table with plausible durations and the standup is under 150 words and specific.
- Run the five rehearsed Ask questions (§6) against real data. Fix wording in `ASK_SYSTEM` if the model guesses instead of citing. Rehearse one question with no answer in the index and make sure it says so.
- Check timestamps in answers match wall-clock Pacific time (timezone pin).
- **Done when:** all five questions answer correctly with citations three runs in a row, and timesheet/standup output can be pasted into Slack as-is.

### D. Capture + demo owner — laptops, planted moments, fallback (13:00–18:00)
- On each demo Mac: grant Screen Recording, Accessibility, **and the Automation dialog** that appears on the first `osascript` call. Verify cards in the UI show app names and window titles (blank titles mean the activity log is useless).
- Apply the one-line CPU fix in `agent/capture.py`: `Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")` instead of `shot.rgb`.
- If a projector is attached, confirm `--monitor` captures the screen you're showing.
- Record a backup folder of the planted moments with sidecar `.json` files (`{"app":..,"title":..,"ts":..}`) for `tools/replay.py --folder`. Keep one real laptop capturing during the demo even if replay is used, so the "filtered on-device" header stat shows.
- Write the demo script (§6) and run it twice end to end before 20:00.
- **Done when:** the full demo runs on the GX10 with the router's WAN unplugged, twice, with no "Error:" box.

### Evening (all): freeze at 20:00
After 20:00 no feature work. Only: prompt wording, copy, Devpost text, video. Anyone who finds a bug after freeze writes it down; we decide together whether it's demo-breaking.

---

## 5. Do not build (cut list)

- Swapping the vision model (Moondream etc.). The prompt parser depends on the SUMMARY/TEXT format.
- Windows polish unless a teammate demos on Windows. Keep it in the pitch.
- Auth, per-user access control, retention UI. Have the sentence ready (§7); don't build it.
- Streaming answers. Only if A–D are all done and it's before 18:00.
- Vector similarity thresholds, re-ranking, bigger embedding models.
- Draft kinds other than email in the demo.
- Any "nice to have" a judge won't see in four minutes.

---

## 6. Demo script (four minutes, rehearse it)

Setup before judges arrive: two laptops capturing for at least an hour, three planted moments already indexed, Promises panel showing them, router WAN already unplugged with the cable visible.

1. **(20 s) The problem.** "If you're a lawyer or under NDA you can't send your screen to a cloud AI. So your workday has no memory." Point at the unplugged WAN cable.
2. **(30 s) The hardware.** Point at the header: checks → kept → filtered on-device; three model dots; indexed/min. "Laptops do a 2 ms check. The GX10 holds three models in 128 GB and does everything else."
3. **(60 s) Things you promised.** Type a new promise in Slack on the live laptop ("I'll send the deck by Friday"). Keep talking. When it appears in the panel, click the chip to show the exact screenshot. "Nobody asked. It was watching."
4. **(60 s) Ask + cite + act.** "Where is dinner tonight and who booked it?" → answer with citation → click the chip → "Draft follow-up email." Copy it.
5. **(40 s) Standup / timesheet.** Click "Draft timesheet." "Billable hours, from what you actually did, without typing anything."
6. **(10 s) Close.** "Private work memory. Three models, one box, nothing leaves the room."

Fallback if live capture fails: `python tools/replay.py --folder ./backup --name demo-laptop` reproduces steps 3–5 from the recorded folder.

---

## 7. Judge objections and the answers

- **"This is Recall."** Recall is one device, Windows only, Microsoft's model. Dockt is shared memory for a room: Mac and Windows, one index, models no laptop can host, and it extracts commitments and writes your timesheet. Recall doesn't.
- **"Screenshots over the network — isn't that worse?"** Local LAN only, token-authenticated, no route out; we unplugged the WAN. Three models resident need 40+ GB; that's an appliance, like the office NAS.
- **"My boss sees my screen?"** Per-device scoping, exclusion list (password managers, private windows), pause file. Access control is a product decision, not a 24-hour feature. Primary user: a solo lawyer or a consenting small team.
- **"I can't trust an LLM summary."** Every claim carries a citation to the actual pixels. Click it. The screenshot is ground truth; the model is the index. It says "not found" when the material isn't there.
- **"How accurate is the screen text?"** It's a vision-model transcription of the important text, not full OCR. Show the screen-text panel.
- **"Why not screenpipe?"** Per-machine recorder. We'd happily ingest its captures. Our differentiator is the server-side team memory plus proactive extraction.
- **"Storage and retention?"** ~150 KB per frame, ~450 MB per 3,000-capture day, stored by device and day, so retention is deleting a folder.
- **"What did you build this weekend?"** Tell the truth.

---

## 8. Known fragile spots (don't rediscover these)

- Server time logic uses the local clock; the patch pins `TZ`. If timestamps look wrong, check `DOCKT_TZ`.
- Ask prompt is 7–9k tokens on a real day. Nemotron context must be ≥16k.
- If Nemotron reasons despite `/no_think` and `chat_template_kwargs`, answers come back as the diagnostic line "The model returned an empty answer…". Set `LLM_NO_THINK=0` and rely on `<think>` stripping, or raise `ASK_MAX_TOKENS`.
- Health dots only prove `/models` responds, not that the named model is there. Check env vars by hand.
- Indexing is newest-first on purpose; the queue count grows under load and old frames wait. Say "we index the present first."
- If Ask gets slow with the extractor running, `EXTRACT_CONCURRENCY=1`. Don't disable it during the demo; the panel is the pitch.
- If the embedding model changes, wipe `DATA_DIR` (vectors won't match; keyword search still works).

---

## 9. For AI sessions: how to take a task from this file

1. Read `README.md` and this file. Read every file you will change before changing it.
2. State which task (A–D) you're doing and what "done" means for it.
3. Make the smallest change that achieves it. Run the mock pipeline (§2 rule 4) before reporting.
4. Report: what you changed (file and function), what you ran, what you saw, what you did not verify.
5. Do not add features from the cut list, do not refactor, do not add cloud calls, do not edit this file's scope without the team asking.
