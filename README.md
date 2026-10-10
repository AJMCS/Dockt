# Dockt — your workday, remembered. 100% local.

Private screen memory for work, running on the ASUS GX10.

```
laptops (agent/capture.py)  --LAN-->  GX10 (server/app.py)
 screenshot every 3s                    save JPEG -> queue
 skip unchanged frames                  vision model: summary + screen text
 spool to disk if server down           embedding model -> vectors
                                        SQLite FTS5 (keywords) + numpy (vectors)
                                        Nemotron: ask / draft follow-ups
                                        web UI at http://<gx10-ip>:8000
```

All three models are reached through OpenAI-compatible URLs, so moving from a
laptop to the GX10 is just changing env vars.

## 1. Tonight: run everything on one laptop

```bash
pip install -r requirements-server.txt -r requirements-agent.txt

# Option A — no models at all (tests plumbing + UI)
cd tools && uvicorn mock_models:app --port 9000 &
cd ../server && VLM_URL=http://localhost:9000/v1 uvicorn app:app --port 8000

# Option B — real models via Ollama
ollama pull qwen2.5vl:7b && ollama pull nomic-embed-text && ollama pull nemotron-mini
cd server && uvicorn app:app --port 8000     # defaults point at Ollama

# feed it data
python tools/replay.py --server http://localhost:8000 --synthetic 40
python agent/capture.py --server http://localhost:8000 --name me
```
Open http://localhost:8000.

## 2. Saturday: GX10

Serve each model on its own port (vLLM in the NVIDIA container from the Spark
playbook). Memory is unified, so the `--gpu-memory-utilization` values must add
up to well under 1.0 — leave room for the OS and the server.

```bash
# vision (port 8001)
vllm serve Qwen/Qwen2.5-VL-7B-Instruct --port 8001 --gpu-memory-utilization 0.30 \
  --max-model-len 8192 --limit-mm-per-prompt '{"image":1}'
# embeddings (port 8002) — flag is --task embed on older vLLM, --runner pooling on newer
vllm serve BAAI/bge-m3 --port 8002 --task embed --gpu-memory-utilization 0.08
# Nemotron (port 8003) — or whatever NVIDIA preloaded; Ollama is fine too
vllm serve <nemotron-model-id> --port 8003 --gpu-memory-utilization 0.35
```

```bash
export VLM_URL=http://localhost:8001/v1 VLM_MODEL=Qwen/Qwen2.5-VL-7B-Instruct
export EMBED_URL=http://localhost:8002/v1 EMBED_MODEL=BAAI/bge-m3
export LLM_URL=http://localhost:8003/v1   LLM_MODEL=<nemotron-model-id>
export DOCKT_TOKEN=pick-something INGEST_CONCURRENCY=16
cd server && uvicorn app:app --host 0.0.0.0 --port 8000
```

**Benchmark first (by noon):**
```bash
python tools/bench.py --url http://localhost:8001/v1 --model Qwen/Qwen2.5-VL-7B-Instruct
```
Indexed img/min must beat total incoming captures/min (the header shows both).
If it doesn't: raise `--interval` to 5s, capture on fewer laptops, or lower
`--max-width` to 1280.

If the vision model dies, `VLM_ENABLED=0` keeps the app working on window
titles alone.

If you switch embedding models after indexing, old vectors won't match: keyword
search still works, but start from an empty `DATA_DIR` for vector search.

## 3. Laptops

```bash
pip install -r requirements-agent.txt
python agent/capture.py --server http://<gx10-ip>:8000 --name brandon --token pick-something
```
- **macOS:** System Settings → Privacy & Security → *Screen Recording* (required)
  and *Accessibility* (window titles) for your terminal app, then restart it.
- **Windows:** nothing to grant. Use `py` instead of `python` if needed.
- Password managers and private/incognito windows are skipped by default
  (`--exclude` adds more). Pause anytime: create `~/.dockt_pause`.
- Server down? Frames spool to `~/.dockt_spool` and upload when it's back.

**When it captures:** the agent checks a tiny thumbnail every second (~2 ms) and
only sends a frame when the screen settles on something new, right after an
app/window switch, or every 30s while something keeps moving (e.g. video). An
idle screen sends nothing. "New" is relative to how busy the screen normally is.
- Too many captures? Raise `--sensitivity` (default 3) or `--floor` (default 6).
- Missing things? Lower them. Run with `--debug` to watch the numbers live.
- `--min-gap 2` / `--max-gap 30` cap how often/rarely it sends.

## 4. Demo checklist
- Plant 2–3 moments Saturday (a Slack message with a restaurant, a phone number
  in a doc, a flight price) and rehearse the exact questions.
- Ask → click a citation → "Draft follow-up email". That's the differentiator.
- Offline proof: unplug the **router's internet uplink** (WAN), not the
  laptop↔GX10 cable. Everything keeps working.
- Backup: keep a folder of good screenshots; `tools/replay.py --folder` loads them.

## API
| | |
|---|---|
| `POST /ingest` | multipart: image, device, ts, app, title (+ `X-Dockt-Token`) |
| `GET /api/search?q=&k=&device=` | hybrid semantic + keyword search |
| `POST /api/ask {q}` | Nemotron answer with `[#id]` citations |
| `POST /api/draft {ids, kind}` | kind = email / message / todo / notes |
| `GET /api/browse`, `/api/timeline`, `/api/stats`, `/api/health`, `/img/{id}` | |
