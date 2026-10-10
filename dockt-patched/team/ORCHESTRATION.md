# Dockt — orchestration plan (written Sat Oct 10, 2026, 15:30 PDT)

Source of truth order: this file + `TEAM.md` > `PLAN.md` §2/§6–8 > `README.md`.
Lane cards with ready-to-paste agent prompts: `team/lane1.md` … `team/lane4.md`.

---

## 0. Read-back

**Product.** Dockt is private work memory. A Windows laptop screenshots itself when the screen changes, a local service on `127.0.0.1:8000` indexes each shot (vision summary + screen text + embedding, SQLite FTS5 + numpy vectors), and the user asks questions about their day. Every answer cites `[#id]`, and a click shows the pixels. On top: "Things you promised" (auto-extracted promises/deadlines/requests), drafts, standup/timesheet, and the new layer: tray + hotkey quick-chat, a ChatGPT-style dashboard, a tool-calling agent.

**Architecture (decided).** Everything runs on the laptop except the models. The ASUS GX10 serves `gemma4:26b` (vision), `nemotron3:33b` (chat/agent), `nomic-embed-text` (embeddings) through Ollama's OpenAI-compatible API, and stores nothing. Each laptop has its own index. Pitch line changes from "shared memory for the room" to **"your memory, on hardware in the room"**.

**Critical path** (everything else is parallel or optional):

1. Step 0 (done, see §1) → everyone pulls and runs the mock smoke test **on Windows** (never done yet).
2. Lane 4: Ollama reachable from laptops (`OLLAMA_HOST=0.0.0.0` + firewall, needs sudo). Blocks every real-model test from a laptop.
3. Lane 4: `models.chat_messages()` (tools + streaming) + its mock. Blocks Lane 3's real agent.
4. Lane 3: real `/api/chat` agent with citation discipline, tuned on real captures.
5. Lane 1: two Windows laptops capturing to their local service, one-click launcher.
6. Integration on the demo network, planted moments, two full rehearsals.

Lane 2 is **off** the critical path: the `/api/chat` stub already streams the real event sequence. Voice, nudges, end-of-day summary are off the critical path (P2).

### Verified today (by me, on the GX10, Sat 15:20–15:35)

- Nemotron 3 33B through Ollama **does tool calling**: with `tools` + `reasoning_effort:"none"` it returned a `search_screens` call (0.8 s), and after a `tool` message it answered "Dinner is at Nopalito at 8 PM, and Sam booked it for 4 people. [9]" (0.9 s). Streaming works; a tool call arrives as one complete chunk, not token by token.
- **Citation format risk:** given JSON tool results it cited `[9]`, not `[#9]`. The ask regex `#(\d+)` would miss that. Lane 3 must force the format in the prompt and parse both `[#9]` and `[9]`.
- Ollama on the GX10 listens on `127.0.0.1:11434` only (gap 2 is real). The GX10's address is `169.233.72.21`, a **campus IP**, not a private LAN. Firewall state unknown (sudo needed).
- Three models loaded and pinned (`expires_at` year 2319): nemotron3:33b 30 GB, gemma4:26b 24 GB, nomic 0.6 GB. 60 GB free.
- An old copy of the service is still running **on the GX10** (`uvicorn … --host 0.0.0.0 --port 8000`, pid 136008). Fine for Lane 3 prompt work today; must be stopped before the demo (the story is "the GX10 stores nothing").
- FTS5 works in the GX10's Python 3.12. **Not verified on Windows Python.**
- Whisper: no torch, no docker access without sudo. The `ctranslate2` aarch64 wheel downloads fine, so `pip install faster-whisper` (CPU, 20 cores) is the cheapest path. `nvcc` exists if someone wants whisper.cpp with CUDA. P2.
- `TEAM.md` is **not** in git (untracked). Push it with step 0.

### Questions that change the plan (answer in chat; defaults in bold)

1. ~~Deadline~~ Answered: demo **Sunday Oct 11 afternoon**. Still open: the Devpost submission time (assume before the demo).
2. **Demo network.** Do we have our own router/switch? The GX10 is on campus Wi-Fi today; campus networks often isolate clients, and you cannot "unplug the internet" on them. **Default: bring a travel router, static IPs, WAN cable visibly unplugged.** If there is none, buy/borrow one today; this is a demo-breaker.
3. ~~How many laptops~~ Answered: **one** live laptop; multi-device is a pitch sentence (the index is keyed by `device`).
4. Lane owners' names and who holds the GX10 sudo password (Lane 4 needs it in the next 30 minutes).

---

## 1. Step 0 — DONE (branch `step0/split-app`, uncommitted, awaiting your commit)

Mechanical split of `server/app.py`, no behavior change, plus the Windows fixes:

| File | Owner | Contents |
|---|---|---|
| `server/memory.py` | L4 | `DATA_DIR` (default `~/.dockt/data`), the `Store` singleton, `search()` (async, embeds q itself), `get/many/activity/commitments/add_commitments/set_commitment_done` |
| `server/indexer.py` | L4 | `process`, `indexer`, `INGEST_CONCURRENCY`, `VLM_ENABLED` |
| `server/brain.py` | L3 | `APIRouter` with ask/draft/standup/commitments, extractor loop, all prompts, **`/api/chat` stub** (non-stream = ask; stream = status→token*→sources→done over the ask answer), **`/api/nudges` stub** (`{"items":[]}`) |
| `server/app.py` | shared | `tzset` guarded with `hasattr`, lifespan, `/ingest`, `/agent/ping`, read API, **`/api/transcribe` stub** (503 until `models.transcribe` exists), `/static/` mount for built assets, `index.html` read as UTF-8 (the old call crashes on Windows cp1252 because of "—" and "…"), `python app.py` binds `127.0.0.1` (`DOCKT_HOST`/`DOCKT_PORT`) |

Verified: mock smoke test on this machine (ports 8010/9010, scratch `DATA_DIR`): health 3/3, 24/24 indexed, ask cites `[#1] [#5]`, chat non-stream has `answer/sources/cited/window/ms`, chat stream emits `status, token×8, sources, done`, commitments 3, standup, draft, search, `/`, `/img/1`, `/static/index.html` all 200, no errors in the log. OpenAPI diff old→new: nothing removed; added `/api/chat`, `/api/nudges`, `/api/transcribe`. **Not verified on Windows.**

To ship it:
```bash
cd ~/Dokt && git add dockt-patched/server dockt-patched/TEAM.md dockt-patched/team && git commit -m "Step 0: split app.py by lane, Windows fixes, chat/nudges/transcribe stubs" && git push -u origin step0/split-app
# then merge to main (PR or fast-forward) and tell everyone to pull
```

---

## 2. Time-boxed plan (demo Sunday Oct 11 afternoon; Devpost time still to confirm)

Decisions from the user (Sat 16:10): demo is **Sunday afternoon**; **one laptop** demos live (multi-device is a sentence in the pitch: the service already keys every capture by `device`, so several of your machines can feed one memory); demo network still open.

| When | Gate | Lane 1 Capture/Desktop | Lane 2 App | Lane 3 Agent | Lane 4 Models/Memory |
|---|---|---|---|---|---|
| **Sat 16:30–17:30 M0** | step 0 on `main`; everyone pulled | mock smoke test on the **demo Windows laptop**; report pass/fail | chat UI on the `/api/chat` stub | read `brain.py`; agent loop design against the `chat_messages` mock | `OLLAMA_HOST=0.0.0.0` + firewall; laptop curls `/v1/models`; `chat_messages`/`transcribe` mocks merged |
| **Sat 17:30–21:30 M1** | each lane's P0 alone | `capture.py` fixes + titles verified; `start-dockt.bat`; tray + pause | streaming chat, chips → modal, sources panel, promises, standup, draft, deep links | real tool-calling `/api/chat`, `[#id]` discipline, extraction tuned on real captures | real `chat_messages`; model defaults; throughput numbers; fixtures |
| **Sat 21:30 checkpoint** | all P0 PRs merged; `main` green on Windows; the demo laptop capturing to the GX10 overnight is **not** needed, but a 1 h real session tonight is | | | | |
| **Sat 21:30–23:00 (optional)** | P1 only | hotkey quick-chat | polish, 1366×768 and projector check | "write that as an email" via `draft` tool; 6 questions ×3 | Whisper on the GX10 + `transcribe()`; eval set |
| **Sun 08:30–11:00 M2** | on the demo network: launcher → capture 1 h → 3 planted moments → 6 questions | drive the laptop; backup folder + sidecar JSON | no error boxes; empty states | prompt wording only after 10:30 | GX10 reboot test; stop the old service; `ollama ps` |
| **Sun 11:00 FREEZE** | | | | | |
| **Sun 11:00–12:30 M3** | two full rehearsals, WAN unplugged, screen-recorded | | | | Devpost text + video (all) |
| **Sun afternoon** | demo | | | | |

P0 = must. P1 = tonight after 21:30 if P0 is green. P2 (nudges toasts, end-of-day summary, framework migration) is cut unless everything else is done by Sunday 09:30.

## 3. Integration plan

**Branches and merges.** `laneN/<feature>` → PR into `main`. I review every PR for contract drift (shapes in `TEAM.md` §3) and for edits outside the lane's files. Contract changes: PR to `TEAM.md` §3 first, consuming lane approves; optional fields are free. Merge windows at **16:45, 17:45, 18:45, 19:30** (freeze). Merging only at the window keeps `main` stable between tests; a PR that misses a window waits for the next.

**After every merge window:**
- Lane 1 runs the mock smoke test on a Windows laptop against `main` (TEAM.md §6 A, using `python app.py`). Red = the merge that broke it is reverted, not fixed forward.
- Lane 4 checks the GX10: `ollama ps` shows the three models, `curl http://<gx10>:11434/v1/models` from a laptop, `free -g` has > 20 GB free, and the capture rate across laptops (`/api/stats` `captured_per_min` summed) stays under ~28/min.
- Lane 3 runs the 5 rehearsed questions against a laptop that has real captures (below) and posts answers + citations in chat.

**Shared files** (`app.py`, `README.md`, `TEAM.md`, requirements): separate small PRs, announced in chat.

**Who verifies what at M2 (18:00):**
- Lane 1: both demo laptops run `start-dockt.bat`, dashboard opens as an Edge app window, header shows checks → kept → filtered, three green model dots.
- Lane 4: throughput with both laptops capturing; `indexed_per_min ≥ captured_per_min` in each laptop's header.
- Lane 3 + Lane 2: the 5 questions, citations clickable, promises panel shows the 3 planted moments, timesheet renders.

**Rollback rule after freeze:** a bug found after 19:30 is written down; the four of us decide together whether it is demo-breaking. Only demo-breaking bugs get a fix, and the fix is re-rehearsed.

### Demo-day checklist

*Network*
- [ ] Own router/switch; laptops + GX10 on it with static or reserved IPs; GX10 IP written on the whiteboard.
- [ ] WAN cable unplugged, visibly, before judges arrive. Laptops' Wi-Fi off (so "offline" is provable: open a browser tab to any website, it fails).
- [ ] `curl http://<gx10>:11434/v1/models` from each laptop.

*GX10*
- [ ] `ollama ps`: `gemma4:26b`, `nemotron3:33b`, `nomic-embed-text` loaded, `expires_at` far future (`OLLAMA_KEEP_ALIVE=-1`).
- [ ] Systemd override has `OLLAMA_HOST=0.0.0.0`, `OLLAMA_CONTEXT_LENGTH=16384`, `OLLAMA_NUM_PARALLEL=4`, `OLLAMA_MAX_LOADED_MODELS=3`; reboot test done once.
- [ ] Old `uvicorn app:app` on the GX10 stopped. Nothing on the GX10 stores captures.
- [ ] A terminal on the GX10 screen showing `watch -n2 'ollama ps'` or `nvtop` (judges love it).

*Demo Windows laptop (one live; a second machine only if someone wants to show multi-device ingest)*
- [ ] `start-dockt.bat` starts service + capture + tray and opens the dashboard; env vars baked in (`VLM_URL=http://<gx10>:11434/v1`, `VLM_MODEL=gemma4:26b`, `LLM_MODEL=nemotron3:33b`, `EMBED_MODEL=nomic-embed-text`).
- [ ] `DATA_DIR` for the demo is separate from test data; at least 1 h of real capture Sunday morning before judges; planted moments indexed and visible in Promises.
- [ ] Notifications/Focus Assist on, Teams/Slack desktop logged in for the live promise, screen scale/zoom readable from 2 m, dark mode consistent.
- [ ] Power plugged in, sleep disabled, no password-manager windows open (they are excluded anyway).

*Backup*
- [ ] `backup/` folder with the planted screenshots + sidecar `.json` (`{"app","title","ts"}`), tested with `python tools/replay.py --server http://127.0.0.1:8000 --folder backup --name demo-laptop`.
- [ ] A screen recording of a full good run (also the Devpost video).

*Rehearsed questions (5 + 1 negative), each must answer correctly 3× in a row on the demo data*
1. "Where is dinner tonight and who booked it?" → planted Slack/Teams message.
2. "What's the invoice number and when is it due?" → planted Gmail/Outlook.
3. "What did I work on this afternoon?" → activity log answer with times.
4. "Did anyone ask me to review something?" → planted request.
5. Follow-up turn: "Draft a message to Sam saying I'm in." → multi-turn + draft.
6. Negative: "What seat am I in on the flight?" → "not in your screens".

---

## 4. Four-minute demo script (draft, rehearse it)

*Before judges arrive:* router WAN unplugged and visible, GX10 screen showing the three loaded models, both laptops captured for ≥ 1 h, three planted moments in the Promises panel, Slack/Teams open on laptop A.

1. **(20 s) Problem.** "If you're a lawyer, in finance, under NDA, you can't paste your screen into ChatGPT. So your workday has no memory. Dockt gives you one, and nothing leaves this table." Point at the unplugged cable.
2. **(30 s) The hardware.** "The laptop keeps every screenshot and the index. It only sends a frame to *that* box (point at the GX10) for the second it takes a 26-billion-parameter vision model to read it. The GX10 holds three models in memory at once and stores nothing." Show the header: checks → kept → filtered on-device, three green dots.
3. **(55 s) Things you promised.** On laptop A type into Slack/Teams: "I'll send the revised NDA by Monday". Keep talking about privacy. When the row appears in Promises, click the chip → the screenshot. "Nobody asked. It was reading along, locally."
4. **(75 s) Ask, cite, act.** Press the hotkey (or use the dashboard): "Where is dinner tonight and who booked it?" Tokens stream; answer cites `[#9]`; click it → the Slack screenshot. Follow up in the same chat: "Draft a message to Sam saying I'm in." Copy it. "Multi-turn, tool-using, and every claim points at pixels."
5. **(30 s) Timesheet.** Click "Draft timesheet". "Billable hours from what you actually did, without typing a thing."
6. **(10 s) Close.** "Private work memory. Your laptop remembers, the box in the room thinks, the cloud never hears about it." Offer: "Ask it something."

*Fallback:* if live capture misbehaves, `tools/replay.py --folder backup` reproduces steps 3–5 in 30 s; keep one laptop capturing live anyway so the header stats move.

---

## 5. Devpost outline (hits each criterion)

- **Title / tagline:** Dockt — your workday, remembered. 100% local.
- **Inspiration (real problem):** people who legally cannot send their screen to a cloud model: lawyers, finance, healthcare, anyone under NDA. Recall-style memory exists only as a cloud-adjacent, single-vendor feature.
- **What it does:** capture on change → vision summary + screen text → searchable memory → questions with clickable citations → promises/deadlines extracted automatically → drafts, standup, timesheet → hotkey quick-chat.
- **Use of local hardware (innovation):** three models resident on one ASUS GX10 (gemma4 26B vision, Nemotron 3 33B agent with tool calling, nomic embeddings), ~54 GB of a 128 GB unified pool, 28 screenshots/min end to end, 3 s answers; laptops do a 2 ms change check and keep all data; the GX10 is a stateless model appliance for the whole room.
- **Value of local AI:** nothing leaves the LAN, demonstrated with the uplink unplugged; the screenshot is the ground truth and the model only indexes it; "not found" instead of guessing.
- **How we built it:** FastAPI + SQLite FTS5 + numpy vectors on the laptop; Ollama OpenAI-compatible API on the GX10; tool-calling agent; four lanes with black-box contracts (`TEAM.md`).
- **Challenges:** thinking models returning empty content through Ollama (`reasoning_effort:"none"`), Windows-first details (tzset, encodings), keeping total capture under GX10 throughput, citation discipline.
- **Accomplishments / what we learned / what's next:** per-user memory, retention UI, consent and access control, macOS, local speech.
- **Video:** the rehearsal recording (≤ 3 min), with the unplugged cable in frame.
