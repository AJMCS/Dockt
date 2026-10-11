"""
Indexer (Lane 4): background loop that turns each new capture into
summary + screen text (vision model on the GX10) + an embedding, and stores
them in this laptop's memory.

  INGEST_CONCURRENCY   parallel vision-model requests (default 8)
  VLM_ENABLED          set 0 to skip the vision model (index titles only)
"""
import asyncio
import os
from pathlib import Path

import httpx

import models
from memory import store

CONCURRENCY = int(os.environ.get("INGEST_CONCURRENCY", "8"))
VLM_ENABLED = os.environ.get("VLM_ENABLED", "1") == "1"
models_down = False  # set by a transport error; the loop backs off before claiming again


async def process(row, sem):
    global models_down
    async with sem:
        try:
            jpeg = Path(row["path"]).read_bytes()
            if VLM_ENABLED:
                summary, ocr = await models.describe(jpeg, row["app"], row["title"])
            else:
                summary, ocr = "", ""
            doc = f"{row['app']} — {row['title']}\n{summary}\n{ocr}"[:4000]
            (emb,) = await models.embed([doc])
            store.complete(row["id"], summary, ocr, emb)
        except httpx.TransportError as e:
            # GX10 down / network blip: not this capture's fault, so it costs no attempt
            models_down = True
            print(f"models unreachable, will retry id={row['id']}: {e!r}")
            store.requeue(row["id"], repr(e))
        except Exception as e:
            print(f"index error id={row['id']}: {e!r}")
            store.fail(row["id"], repr(e))


async def indexer():
    sem = asyncio.Semaphore(CONCURRENCY)
    inflight: set[asyncio.Task] = set()
    global models_down
    while True:
        if models_down:  # back off instead of re-claiming every second while the models are down
            models_down = False
            await asyncio.sleep(5)
        free = CONCURRENCY * 2 - len(inflight)
        rows = store.claim(free) if free > 0 else []
        for r in rows:
            t = asyncio.create_task(process(r, sem))
            inflight.add(t)
            t.add_done_callback(inflight.discard)
        await asyncio.sleep(0.2 if rows else 1.0)
