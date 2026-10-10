"""
Replay a folder of screenshots into the server as if a laptop captured them.
Use it to (a) test without a capture agent, and (b) as demo insurance: if live
capture breaks, replay a backup folder you recorded earlier.

  python replay.py --server http://localhost:8000 --folder ./backup --name demo-laptop
  python replay.py --server http://localhost:8000 --synthetic 40

Timestamps come from file modified times (or are spread over the last hour).
Window title = file name unless a sibling .json {"app":..,"title":..} exists.
"""
import argparse
import io
import json
import random
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw

SCENES = [
    ("Slack", "#hackathon — Sam: dinner at Nopalito at 8, I booked for 4"),
    ("Chrome", "United Airlines — SFO to Tokyo NRT, Nov 14, $842 round trip"),
    ("Gmail", "Invoice #4471 from ASUS — due Oct 30 — $1,250.00"),
    ("Visual Studio Code", "store.py — dockt — hybrid search reciprocal rank fusion"),
    ("Chrome", "build.nvidia.com/spark — vLLM playbook — DGX Spark"),
    ("Zoom", "Weekly sync with Kristina — protein binder pipeline results"),
    ("Notion", "Demo script — planted moment #2: Jane's phone number 415-555-0132"),
    ("Chrome", "Devpost — UCSC AI Hackathon — submission due Sun 1:30 PM"),
]


def synth(app, title):
    img = Image.new("RGB", (1280, 800), (245, 246, 248))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1280, 40], fill=(40, 44, 52))
    d.text((16, 12), f"{app}  —  {title}", fill=(255, 255, 255))
    for y in range(80, 760, 26):
        d.text((40, y), title if random.random() < .2 else "lorem ipsum dolor sit amet " * 3, fill=(30, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


def post(server, token, jpeg, meta):
    r = requests.post(server.rstrip("/") + "/ingest", timeout=10,
                      headers={"X-Dockt-Token": token} if token else {},
                      files={"image": ("cap.jpg", jpeg, "image/jpeg")}, data=meta)
    r.raise_for_status()
    return r.json()["id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--name", default="replay")
    ap.add_argument("--token", default="")
    ap.add_argument("--folder")
    ap.add_argument("--synthetic", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between posts")
    a = ap.parse_args()

    now = time.time()
    if a.synthetic:
        for i in range(a.synthetic):
            app, title = SCENES[i % len(SCENES)]
            ts = now - 3600 + i * (3600 / a.synthetic)
            cid = post(a.server, a.token, synth(app, title),
                       {"device": a.name, "ts": ts, "app": app, "title": title})
            print(cid, app, title[:50])
            time.sleep(a.delay)
        return

    files = sorted(p for p in Path(a.folder).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    for i, p in enumerate(files):
        side = p.with_suffix(".json")
        meta = json.loads(side.read_text()) if side.exists() else {}
        buf = io.BytesIO()
        Image.open(p).convert("RGB").save(buf, "JPEG", quality=70)
        ts = meta.get("ts") or p.stat().st_mtime
        cid = post(a.server, a.token, buf.getvalue(),
                   {"device": a.name, "ts": ts, "app": meta.get("app", ""), "title": meta.get("title", p.stem)})
        print(cid, p.name)
        time.sleep(a.delay)


if __name__ == "__main__":
    main()
