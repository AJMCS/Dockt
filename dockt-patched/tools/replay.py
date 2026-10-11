"""
Replay a folder of screenshots into the server as if a laptop captured them.
Use it to (a) test without a capture agent, and (b) as demo insurance: if live
capture breaks, replay a backup folder you recorded earlier.

  python replay.py --server http://localhost:8000 --folder ./backup --name demo-laptop
  python replay.py --server http://localhost:8000 --synthetic 40
  python replay.py --server http://localhost:8000 --scenario all     # retraction + deleted-draft test threads

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


# Commitment test threads: (seconds from start, app, title, sent/received messages, unsent text in the compose box).
# Frames < 8 s apart (EXTRACT_SETTLE_S) are mid-typing and should be skipped; repeats should cost no extraction.
SCENARIOS = {
    "retraction": [  # a request, repeated frames of it, then the requester takes it back
        (0, "Slack", "#design | Acme — Slack", "Alex: can you send the deck to Sarah by Friday?", ""),
        (3, "Slack", "#design | Acme — Slack", "Alex: can you send the deck to Sarah by Friday?", ""),
        (20, "Slack", "(1) #design | Acme — Slack", "Alex: can you send the deck to Sarah by Friday?", ""),
        (62, "Slack", "#design | Acme — Slack",
         "Alex: can you send the deck to Sarah by Friday?\nAlex: actually nevermind, I've got it", ""),
        (65, "Slack", "#design | Acme — Slack",
         "Alex: can you send the deck to Sarah by Friday?\nAlex: actually nevermind, I've got it", ""),
        (90, "Slack", "#design | Acme — Slack",
         "Alex: can you send the deck to Sarah by Friday?\nAlex: actually nevermind, I've got it", ""),
    ],
    "draft": [  # a promise typed in the compose box, left there a while, then deleted without sending
        (200, "Slack", "#launch | Acme — Slack", "Jordan: are we still on for the launch review tomorrow?",
         "I'll send the launch checklist to Jordan by tomorrow"),
        (215, "Slack", "#launch | Acme — Slack", "Jordan: are we still on for the launch review tomorrow?",
         "I'll send the launch checklist to Jordan by tomorrow"),
        (217, "Slack", "#launch | Acme — Slack", "Jordan: are we still on for the launch review tomorrow?",
         "I'll send the launch checklist"),
        (219, "Slack", "#launch | Acme — Slack", "Jordan: are we still on for the launch review tomorrow?", "I'll send"),
        (240, "Slack", "#launch | Acme — Slack", "Jordan: are we still on for the launch review tomorrow?", ""),
    ],
}


def chat_frame(app, title, text, draft):
    """A chat window: messages drawn as pixels for a real vision model, plus the same content as JSON in the
    JPEG comment for tools/mock_models.py (which can't read pixels)."""
    img = Image.new("RGB", (1280, 800), (245, 246, 248))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1280, 40], fill=(40, 44, 52))
    d.text((16, 12), f"{app}  —  {title}", fill=(255, 255, 255))
    for i, line in enumerate(text.splitlines()):
        d.text((40, 80 + 40 * i), line, fill=(30, 30, 30))
    d.rectangle([30, 700, 1250, 770], outline=(120, 120, 120), width=2)   # the message box
    d.text((44, 728), draft or "Message #channel", fill=(30, 30, 30) if draft else (150, 150, 150))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80, comment=json.dumps({"text": text, "draft": draft}))
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
    ap.add_argument("--scenario", choices=[*SCENARIOS, "all"], help="post commitment test threads")
    a = ap.parse_args()

    now = time.time()
    if a.scenario:   # placed in the recent past so every frame has already settled
        frames = sorted(f for k, v in SCENARIOS.items() if a.scenario in (k, "all") for f in v)
        start = now - 60 - max(f[0] for f in frames)
        for dt, app, title, text, draft in frames:
            cid = post(a.server, a.token, chat_frame(app, title, text, draft),
                       {"device": a.name, "ts": start + dt, "app": app, "title": title})
            print(cid, f"+{dt}s", title, "|", text.splitlines()[-1][:50], "| draft:", draft[:30] or "-")
            time.sleep(a.delay)
        return
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
