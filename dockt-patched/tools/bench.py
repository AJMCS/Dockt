"""
Vision-model throughput benchmark — run this on the GX10 Saturday morning.
Tells you how many screenshots/min you can index at each concurrency level,
so you know how many laptops / what capture interval you can afford.

  python bench.py --url http://localhost:8001/v1 --model Qwen/Qwen2.5-VL-7B-Instruct \
                  --images ./sample_screens --n 32

With no --images folder it generates synthetic text-heavy screenshots.
Need: incoming captures/min (all laptops) < indexed/min, or the queue grows.
"""
import argparse
import asyncio
import base64
import io
import random
import statistics
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

WORDS = ("meeting invoice flight tokyo deadline slack email budget roadmap demo "
         "nvidia protein binder customer quarterly review python error stack trace").split()


def synthetic(w=1440, h=900):
    img = Image.new("RGB", (w, h), (250, 250, 250))
    d = ImageDraw.Draw(img)
    for y in range(20, h - 20, 22):
        d.text((30, y), " ".join(random.choices(WORDS, k=random.randint(5, 18))), fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


async def one(client, url, model, jpeg):
    b64 = base64.b64encode(jpeg).decode()
    t = time.time()
    r = await client.post(f"{url}/chat/completions", json={
        "model": model, "max_tokens": 400, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "SUMMARY: describe this screen in 1-2 sentences.\nTEXT: transcribe key visible text (max 150 words)."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]})
    r.raise_for_status()
    toks = r.json().get("usage", {}).get("completion_tokens", 0)
    return time.time() - t, toks


async def level(url, model, images, conc, n):
    sem = asyncio.Semaphore(conc)
    async with httpx.AsyncClient(timeout=300) as client:
        async def run(i):
            async with sem:
                return await one(client, url, model, images[i % len(images)])
        t0 = time.time()
        res = await asyncio.gather(*[run(i) for i in range(n)])
        wall = time.time() - t0
    lat = [r[0] for r in res]
    toks = sum(r[1] for r in res)
    print(f"concurrency {conc:>3}: {n / wall * 60:7.1f} img/min | p50 {statistics.median(lat):5.1f}s "
          f"| max {max(lat):5.1f}s | {toks / wall:6.0f} out tok/s")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--images", help="folder of .jpg/.png screenshots")
    ap.add_argument("--n", type=int, default=32, help="requests per concurrency level")
    ap.add_argument("--levels", default="1,4,8,16,32")
    a = ap.parse_args()
    if a.images:
        paths = sorted(p for p in Path(a.images).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
        images = []
        for p in paths[:64]:
            buf = io.BytesIO()
            Image.open(p).convert("RGB").save(buf, "JPEG", quality=70)
            images.append(buf.getvalue())
    else:
        images = [synthetic() for _ in range(8)]
    print(f"{len(images)} images, {a.n} requests per level\n")
    await one(httpx.AsyncClient(timeout=300), a.url, a.model, images[0])  # warm-up
    for c in map(int, a.levels.split(",")):
        await level(a.url, a.model, images, c, max(a.n, c))


if __name__ == "__main__":
    asyncio.run(main())
