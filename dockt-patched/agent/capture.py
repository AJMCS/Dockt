"""
Dockt capture agent — runs on each laptop (macOS or Windows).

Screenshots every N seconds, skips near-identical frames, and sends each new
frame + active app/window title to the GX10 server. If the server is
unreachable, frames are spooled to disk and sent when it comes back.

Usage:
  python capture.py --server http://192.168.1.50:8000 --name brandon
  python capture.py --server http://... --interval 3 --monitor 1 --token SECRET

macOS: grant your terminal "Screen Recording" (required) and "Accessibility"
(for window titles) in System Settings > Privacy & Security, then restart it.
Pause capture: create a file named .dockt_pause in your home folder.
"""
import argparse
import io
import json
import os
import platform
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import mss
import requests
from PIL import Image, ImageChops

SYSTEM = platform.system()
PAUSE_FILE = Path.home() / ".dockt_pause"
DEFAULT_EXCLUDE = [
    "1password", "bitwarden", "keepass", "lastpass", "keychain access",
    "incognito", "inprivate", "private browsing",
]


# ---------- active window ----------
_MAC_SCRIPT = '''
tell application "System Events"
  set p to first application process whose frontmost is true
  set n to name of p
  set t to ""
  try
    set t to name of front window of p
  end try
end tell
return n & linefeed & t
'''


def active_window():
    """Return (app, title). Never raises."""
    try:
        if SYSTEM == "Darwin":
            out = subprocess.run(["osascript", "-e", _MAC_SCRIPT],
                                 capture_output=True, text=True, timeout=2).stdout
            app, _, title = out.strip().partition("\n")
            return app.strip(), title.strip()
        if SYSTEM == "Windows":
            import ctypes
            from ctypes import wintypes
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            hwnd = user32.GetForegroundWindow()
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            app = ""
            h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
            if h:
                pbuf = ctypes.create_unicode_buffer(260)
                size = wintypes.DWORD(260)
                if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                    app = os.path.splitext(os.path.basename(pbuf.value))[0]
                kernel32.CloseHandle(h)
            return app, title
        # Linux fallback (xdotool)
        title = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                               capture_output=True, text=True, timeout=2).stdout.strip()
        return "", title
    except Exception:
        return "", ""


# ---------- change detection ----------
THUMB = (96, 60)   # tiny grayscale copy of the screen used for all comparisons
GRID = (8, 5)      # tiles; a change concentrated in one tile still counts


def thumbnail(img: Image.Image) -> Image.Image:
    return img.convert("L").resize(THUMB, Image.BOX)


def change(a: Image.Image, b: Image.Image) -> float:
    """How different two thumbnails are, 0-255. Uses the 3 most-changed tiles,
    so a new chat message (big change in one spot) scores high while noise
    spread thinly across the screen (cursor blink, clock) scores low."""
    d = ImageChops.difference(a, b).resize(GRID, Image.BOX)
    top = sorted(d.getdata(), reverse=True)[:3]
    return sum(top) / len(top)


class ChangeDetector:
    """Decides when a frame is worth sending. Pure logic, no screen access.

    motion   = change since the previous check (is the screen moving right now?)
    drift    = change since the last frame we sent (is this new content?)
    baseline = median motion over the last ~30 checks (how busy this screen normally
               is; a median so a quick scroll doesn't inflate it)

    Send when the screen has settled and drift clearly exceeds what's normal,
    or right after an app/window switch, or every max_gap seconds if something
    changed (e.g. a video that never settles). An idle screen sends nothing.
    """

    def __init__(self, sensitivity=3.0, floor=6.0, settle=2.5,
                 min_gap=2.0, max_gap=30.0, switch_wait=3.0):
        self.k, self.floor, self.settle = sensitivity, floor, settle
        self.min_gap, self.max_gap, self.switch_wait = min_gap, max_gap, switch_wait
        self.reset()

    def reset(self):
        self.prev = self.sent_thumb = self.sent_window = None
        self.sent_at = 0.0
        self.baseline = 0.0
        self.history = deque(maxlen=30)
        self.switch_since = None
        self.last = {}

    def update(self, thumb, window, now):
        motion = change(thumb, self.prev) if self.prev is not None else 0.0
        drift = change(thumb, self.sent_thumb) if self.sent_thumb is not None else 255.0
        self.prev = thumb
        self.history.append(motion)
        self.baseline = sorted(self.history)[len(self.history) // 2]
        # "settled" is relative: on a page with a looping animation, normal motion isn't 0
        settled = motion < max(self.settle, 1.5 * self.baseline)
        threshold = max(self.floor, self.k * self.baseline)
        since = now - self.sent_at

        if window != self.sent_window:
            self.switch_since = self.switch_since or now
        else:
            self.switch_since = None

        reason = None
        if self.sent_thumb is None:
            reason = "first"
        elif since >= self.max_gap and drift > self.settle:
            reason = "heartbeat"
        elif since >= self.min_gap:
            if self.switch_since and (settled or now - self.switch_since >= self.switch_wait):
                reason = "switch"
            elif settled and drift > threshold:
                reason = "change"

        self.last = {"motion": motion, "drift": drift, "baseline": self.baseline,
                     "threshold": threshold, "settled": settled}
        if reason:
            self.sent_thumb, self.sent_window, self.sent_at = thumb, window, now
            self.switch_since = None
        return reason


def to_jpeg(img: Image.Image, max_width: int, quality: int) -> bytes:
    if img.width > max_width:
        h = round(img.height * max_width / img.width)
        img = img.resize((max_width, h), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def is_excluded(app: str, title: str, patterns) -> bool:
    s = f"{app} {title}".lower()
    return any(p in s for p in patterns)


def is_dockt_window(title: str) -> bool:
    """Dockt's own dashboard, pet and quick chat. Exact title match, not a substring,
    so an editor with this repo open (title contains 'dockt') is still captured."""
    t = (title or "").strip().lower()
    return t == "dockt" or t.startswith("dockt quick chat")


# ---------- sending ----------
class Sender(threading.Thread):
    def __init__(self, server, token, spool_dir):
        super().__init__(daemon=True)
        self.url = server.rstrip("/") + "/ingest"
        self.headers = {"X-Dockt-Token": token} if token else {}
        self.q = queue.Queue(maxsize=200)
        self.spool = Path(spool_dir)
        self.spool.mkdir(parents=True, exist_ok=True)
        self.online = True
        self.sent = 0

    def post(self, jpeg: bytes, meta: dict) -> bool:
        try:
            r = requests.post(self.url, headers=self.headers, timeout=10,
                              files={"image": ("cap.jpg", jpeg, "image/jpeg")},
                              data=meta)
            if r.status_code == 401:
                print("!! server rejected token", file=sys.stderr)
            return r.ok
        except requests.RequestException:
            return False

    def to_spool(self, jpeg, meta):
        stem = self.spool / f"{meta['ts']:.3f}"
        stem.with_suffix(".jpg").write_bytes(jpeg)
        stem.with_suffix(".json").write_text(json.dumps(meta))

    def drain_spool(self, limit=25):
        for js in sorted(self.spool.glob("*.json"))[:limit]:
            jpg = js.with_suffix(".jpg")
            if not jpg.exists():
                js.unlink(missing_ok=True)
                continue
            if not self.post(jpg.read_bytes(), json.loads(js.read_text())):
                self.online = False
                return
            jpg.unlink(missing_ok=True)
            js.unlink(missing_ok=True)
            self.sent += 1

    def run(self):
        last_retry = 0.0
        while True:
            try:
                jpeg, meta = self.q.get(timeout=1)
            except queue.Empty:
                jpeg = None
            if jpeg is not None:
                if self.online and self.post(jpeg, meta):
                    self.sent += 1
                else:
                    if self.online:
                        print("-- server unreachable, spooling locally", file=sys.stderr)
                    self.online = False
                    self.to_spool(jpeg, meta)
            # retry spooled frames every 5s while offline, continuously while online
            if self.online or time.time() - last_retry > 5:
                last_retry = time.time()
                was_offline = not self.online
                self.online = True
                self.drain_spool()
                if was_offline and self.online:
                    print("-- server back, flushing spool", file=sys.stderr)


def start_pinger(server, token, device, counts, every=30):
    url = server.rstrip("/") + "/agent/ping"
    headers = {"X-Dockt-Token": token} if token else {}
    session = uuid.uuid4().hex[:12]

    def loop():
        while True:
            time.sleep(every)
            try:
                requests.post(url, headers=headers, timeout=5,
                              json={"device": device, "session": session,
                                    "checks": counts["checks"], "sent": counts["sent"]})
            except requests.RequestException:
                pass  # offline: the next ping carries the running totals anyway

    threading.Thread(target=loop, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="Dockt capture agent")
    ap.add_argument("--server", required=True, help="e.g. http://192.168.1.50:8000")
    ap.add_argument("--name", default=socket.gethostname(), help="device name shown in UI")
    ap.add_argument("--token", default=os.environ.get("DOCKT_TOKEN", ""))
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between change checks (cheap; frames are only sent on change)")
    ap.add_argument("--monitor", type=int, default=1, help="1 = primary, 0 = all monitors")
    ap.add_argument("--max-width", type=int, default=1440)
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--sensitivity", type=float, default=3.0,
                    help="send when change > this x the screen's normal motion (lower = more captures)")
    ap.add_argument("--floor", type=float, default=6.0,
                    help="minimum change (0-255, top tiles) to count as new content")
    ap.add_argument("--min-gap", type=float, default=2.0, help="never send more often than this (s)")
    ap.add_argument("--max-gap", type=float, default=30.0,
                    help="if the screen keeps changing but never settles, send at least this often (s)")
    ap.add_argument("--debug", action="store_true", help="print change metrics every check (for tuning)")
    ap.add_argument("--exclude", default="", help="extra comma-separated app/title words to skip")
    ap.add_argument("--spool", default=str(Path.home() / ".dockt_spool"))
    args = ap.parse_args()

    exclude = DEFAULT_EXCLUDE + [s.strip().lower() for s in args.exclude.split(",") if s.strip()]
    sender = Sender(args.server, args.token, args.spool)
    sender.start()

    det = ChangeDetector(args.sensitivity, args.floor, min_gap=args.min_gap, max_gap=args.max_gap)
    checks = 0
    counts = {"checks": 0, "sent": 0}
    start_pinger(args.server, args.token, args.name, counts)
    sent_by = {}
    print(f"Capturing as '{args.name}' -> {args.server}  (Ctrl+C to stop, "
          f"touch {PAUSE_FILE} to pause)")

    with mss.mss() as sct:
        mon = sct.monitors[args.monitor] if args.monitor < len(sct.monitors) else sct.monitors[1]
        next_t = time.time()
        while True:
            next_t += args.interval
            try:
                app, title = active_window()
                if PAUSE_FILE.exists() or is_excluded(app, title, exclude) or is_dockt_window(title):
                    det.reset()  # forget the screen; resume with a fresh capture
                else:
                    shot = sct.grab(mon)
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    now = time.time()
                    reason = det.update(thumbnail(img), (app, title), now)
                    checks += 1
                    counts["checks"] = checks
                    if args.debug:
                        m = det.last
                        print(f"motion={m['motion']:5.1f} drift={m['drift']:5.1f} "
                              f"base={m['baseline']:4.1f} thr={m['threshold']:4.1f} "
                              f"{'settled' if m['settled'] else 'moving '} -> {reason or '-'}")
                    if reason:
                        sent_by[reason] = sent_by.get(reason, 0) + 1
                        counts["sent"] += 1
                        jpeg = to_jpeg(img, args.max_width, args.quality)
                        meta = {"device": args.name, "ts": now, "app": app, "title": title}
                        try:
                            sender.q.put_nowait((jpeg, meta))
                        except queue.Full:
                            sender.to_spool(jpeg, meta)
                    if checks % 60 == 0 and not args.debug:
                        print(f"checks={checks} sent={sender.sent} {sent_by} "
                              f"{'online' if sender.online else 'OFFLINE (spooling)'}")
            except KeyboardInterrupt:
                raise
            except Exception as e:  # keep running no matter what
                print(f"capture error: {e}", file=sys.stderr)
            time.sleep(max(0, next_t - time.time()))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
