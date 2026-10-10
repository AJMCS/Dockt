"""
Dockt desktop app (Lane 2): the dashboard in its own native window, no browser.

  python desktop/dockt_desktop.py                      # real models on the GX10 (gx10.txt or DOCKT_GX10)
  python desktop/dockt_desktop.py --mock               # mock models, no GX10 needed
  python desktop/dockt_desktop.py --no-capture         # window + local service only
  python desktop/dockt_desktop.py --open "#/capture/3" # bring the running window up on that link

On Windows run it with pythonw.exe (Dockt.bat does) so no console shows.

If the local service isn't answering on 127.0.0.1:8000, this starts server/app.py (plus
tools/mock_models.py with --mock) and agent/capture.py, then opens a native window on the
dashboard: WebView2 on Windows (built into Windows 10/11), WebKit on macOS. Closing the
window stops whatever this process started; a service someone else started is left alone.

Only one window runs. A second launch, or `--open`, hands its link to the running window
through http://127.0.0.1:8765/open?hash=<#/...> (Lane 1's tray and quick-chat can call
that URL directly). Logs: ~/.dockt/logs/. Chat history and theme persist in
~/.dockt/webview (the window's own browser profile).
"""
import argparse
import atexit
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]           # dockt-patched/
HOME = Path.home() / ".dockt"
LOGS = HOME / "logs"
PORT = int(os.environ.get("DOCKT_PORT", "8000"))
BASE = f"http://127.0.0.1:{PORT}"
CONTROL_PORT = int(os.environ.get("DOCKT_DESKTOP_PORT", "8765"))
ICON = Path(__file__).with_name("dockt.ico")

children: list[subprocess.Popen] = []


# ---------- helpers ----------
def answers(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def clean_hash(h: str | None) -> str:
    """'#/capture/3', '/#/capture/3' or 'capture/3' -> '#/capture/3'; anything else -> ''."""
    h = (h or "").strip().lstrip("/")
    if h.startswith("#"):
        h = h[1:].lstrip("/")
    return "#/" + h if h.startswith(("capture/", "chat?")) else ""


def spawn(name: str, args: list[str], env: dict) -> subprocess.Popen:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{name}.log", "a", encoding="utf-8")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(args)}\n")
    log.flush()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    p = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=flags)
    children.append(p)
    return p


def stop_children():
    for p in reversed(children):
        if p.poll() is None:
            p.terminate()
    for p in children:
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
    children.clear()


def model_env(mock: bool) -> dict:
    env = dict(os.environ)
    if mock:
        env["VLM_URL"] = "http://127.0.0.1:9000/v1"
        return env
    gx10 = env.get("DOCKT_GX10", "")
    if not gx10 and (ROOT / "gx10.txt").exists():
        gx10 = next(iter((ROOT / "gx10.txt").read_text().split()), "")
    if gx10 and "VLM_URL" not in env:   # same defaults as Lane 1's start-dockt.bat (team/lane1.md P0-b)
        env.update(VLM_URL=f"http://{gx10}:11434/v1", VLM_MODEL="gemma4:26b",
                   LLM_MODEL="nemotron3:33b", EMBED_MODEL="nomic-embed-text")
    return env


def start_backend(mock: bool, capture: bool) -> subprocess.Popen | None:
    """Start what isn't running yet. Returns the service process if this process started it."""
    if answers(BASE + "/api/stats"):
        return None
    env = model_env(mock)
    if mock and not answers("http://127.0.0.1:9000/v1/models"):
        spawn("mock_models", ["-m", "uvicorn", "--app-dir", "tools", "mock_models:app", "--port", "9000"], env)
    server = spawn("server", ["server/app.py"], env)
    if capture:
        spawn("capture", ["agent/capture.py", "--server", BASE, "--name", socket.gethostname()], env)
    return server


def make_icon():
    """The favicon shape from team/DESIGN.md: a rounded #76B900 square. Needs Pillow (already a requirement)."""
    if ICON.exists():
        return
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle((8, 8, 248, 248), radius=64, fill="#76B900")
        img.save(ICON, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
    except Exception as e:
        print("icon not created:", e)


# ---------- single instance: a tiny control endpoint on 127.0.0.1 ----------
class Control(BaseHTTPRequestHandler):
    window = None

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/open":
            self.send_response(404)
            self.end_headers()
            return
        show(self.window, clean_hash(urllib.parse.parse_qs(u.query).get("hash", [""])[0]))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def show(window, h: str):
    if window is None:
        return
    try:
        window.restore()
        window.show()
    except Exception:
        pass
    if h:   # the dashboard routes on hashchange (index.html, "deep links")
        window.evaluate_js(f"location.hash = {json.dumps(h)}")


def hand_off(h: str) -> bool:
    """If a window is already running, give it the link and return True."""
    try:
        q = urllib.parse.urlencode({"hash": h})
        with urllib.request.urlopen(f"http://127.0.0.1:{CONTROL_PORT}/open?{q}", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


LOADING = """<!doctype html><meta charset="utf-8"><style>
:root{color-scheme:light dark;--bg:#FAF9F5;--fg:#6B6A65}
@media (prefers-color-scheme:dark){:root{--bg:#262624;--fg:#A3A29B}}
body{margin:0;height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:15px -apple-system,"Segoe UI",sans-serif}
i{display:inline-block;width:8px;height:8px;border-radius:50%;background:#76B900;margin-right:8px;
animation:p 1.2s ease-in-out infinite}@keyframes p{50%{opacity:.25}}
</style><body><div><i></i>__TEXT__</div>"""


def boot(window, h: str, server: subprocess.Popen | None):
    """Runs once the window exists: point it at the dashboard as soon as the service answers."""
    if os.name == "nt":   # window icon; the native form only exists once pywebview has shown it
        try:
            from System.Drawing import Icon   # pythonnet, installed with pywebview on Windows
            window.native.Icon = Icon(str(ICON))
        except Exception:
            pass
    deadline = time.time() + 60
    while not answers(BASE + "/api/stats"):
        if time.time() > deadline or (server and server.poll() is not None):
            window.load_html(LOADING.replace("<i></i>", "").replace(
                "__TEXT__", f"Dockt couldn't start. Details are in {LOGS / 'server.log'}"))
            return
        time.sleep(0.4)
    window.load_url(BASE + "/" + h)


def main():
    ap = argparse.ArgumentParser(description="Dockt in a native window")
    ap.add_argument("--mock", action="store_true", help="use tools/mock_models.py instead of the GX10")
    ap.add_argument("--no-capture", action="store_true", help="don't start agent/capture.py")
    ap.add_argument("--open", default="", help='link to show, e.g. "#/capture/3" or "#/chat?q=hello"')
    args = ap.parse_args()
    h = clean_hash(args.open)

    if hand_off(h):   # already running: it shows the link, we're done
        return
    try:
        control = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), Control)
    except OSError:
        control = None   # port taken by something else: run anyway, just without hand-off

    import webview   # pywebview; imported late so --open hand-offs stay instant
    if os.name == "nt":   # own taskbar group and icon instead of Python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Dockt.Desktop")
        except Exception:
            pass
    make_icon()

    server = start_backend(args.mock, not args.no_capture)
    atexit.register(stop_children)

    window = webview.create_window(
        "Dockt", html=LOADING.replace("__TEXT__", "Starting Dockt on this laptop…"),
        width=1366, height=860, min_size=(900, 600), background_color="#FAF9F5", text_select=True)
    if control:
        Control.window = window
        threading.Thread(target=control.serve_forever, daemon=True).start()
    try:
        # private_mode=False + a storage path: recent chats and the theme survive restarts
        webview.start(boot, (window, h, server), private_mode=False, storage_path=str(HOME / "webview"),
                      icon=str(ICON) if ICON.exists() else None)
    finally:
        if control:
            control.shutdown()
        stop_children()


if __name__ == "__main__":
    main()
