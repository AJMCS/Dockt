"""
Dockt desktop app (Lane 2): the dashboard in its own native window, no browser.

  python desktop/dockt_desktop.py                      # real models on the GX10 (gx10.txt or DOCKT_GX10)
  python desktop/dockt_desktop.py --mock               # mock models, no GX10 needed
  python desktop/dockt_desktop.py --no-capture         # window + local service only
  python desktop/dockt_desktop.py --open "#/capture/3" # bring the running window up on that link

On Windows run it with pythonw.exe (Dockt.bat does) so no console shows.

gx10.txt holds either the GX10's address (192.168.8.10: Ollama reached directly on :11434) or
user@address (asus@169.233.72.21: reached through an SSH tunnel this process keeps open on
127.0.0.1:11435, for networks where 11434 isn't reachable, e.g. campus Wi-Fi). The tunnel needs
an SSH key the GX10 accepts; desktop/Setup-GX10.bat sets that up once per laptop.

If the local service isn't answering on 127.0.0.1:8000, this starts server/app.py (plus
tools/mock_models.py with --mock) and agent/capture.py, then opens a native window on the
dashboard: WebView2 on Windows (built into Windows 10/11), WebKit on macOS. Closing the
window hides it so the pet stays available; choosing Quit Dockt from the pet ends the session
and stops whatever services this process started. A service started elsewhere is left alone.

Only one window runs. A second launch, or `--open`, hands its link to the running window
through http://127.0.0.1:8765/open?hash=<#/...> (Lane 1's tray and quick-chat can call
that URL directly). Logs: ~/.dockt/logs/. Shared chat history lives in the local
data directory's conversations.db; the theme persists in ~/.dockt/webview.
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
PORT = int(os.environ.get("DOCKT_PORT", "8000"))   # resolve_port() may change it
BASE = f"http://127.0.0.1:{PORT}"
CONTROL_PORT = int(os.environ.get("DOCKT_DESKTOP_PORT", "8765"))
TUNNEL_PORT = int(os.environ.get("DOCKT_TUNNEL_PORT", "11435"))   # local end of the SSH tunnel to Ollama
ICON = Path(__file__).with_name("dockt.ico")

children: list[subprocess.Popen] = []
shutdown_requested = threading.Event()
stopping = threading.Event()   # set by stop_children so the tunnel watchdog doesn't reconnect
_instance_lock_handle = None
pending_hash = ""
pending_show = False
window_ready = threading.Event()


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
    return "#/" + h if h.startswith(("capture/", "chat?", "conversation/")) else ""


def dashboard_window_options(h: str) -> dict:
    """Keep startup out of the way; explicit deep links request a visible window."""
    return {
        "width": 1040, "height": 700, "x": None, "y": None,
        "min_size": (800, 540), "hidden": not bool(h),
        "background_color": "#FAF9F5", "text_select": True,
    }


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def listening(port: int) -> bool:
    """Something accepts connections on 127.0.0.1:<port>. Unlike port_free, sockets left in
    TIME_WAIT by a tunnel that just closed don't count."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def resolve_port():
    """Same rule as Lane 1's start-dockt.bat (agent/find_port.py): DOCKT_PORT if set, else a Dockt
    already answering in 8000-8099, else the first free port there. Children inherit DOCKT_PORT/DOCKT_URL."""
    global PORT, BASE
    if "DOCKT_PORT" not in os.environ:
        ports = range(8000, 8100)
        running, first_free = None, None
        for p in ports:
            if port_free(p):
                if first_free is None:
                    first_free = p
                continue
            # Only make HTTP requests to ports that actually have a listener.
            if answers(f"http://127.0.0.1:{p}/api/stats", timeout=0.18) and \
                    answers(f"http://127.0.0.1:{p}/api/conversations", timeout=0.18):
                running = p
                break
        PORT = running or first_free or 8000
        BASE = f"http://127.0.0.1:{PORT}"
    os.environ["DOCKT_PORT"], os.environ["DOCKT_URL"] = str(PORT), BASE


def spawn(name: str, args: list[str], env: dict, program: str | None = None) -> subprocess.Popen:
    """Start a child that logs to ~/.dockt/logs/<name>.log. Python scripts by default, `program` otherwise."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{name}.log", "a", encoding="utf-8")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(args)}\n")
    log.flush()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    p = subprocess.Popen([program or sys.executable, *args], cwd=ROOT, env=env, stdout=log,
                         stderr=subprocess.STDOUT, creationflags=flags)
    children.append(p)
    return p


def stop_children():
    stopping.set()
    for p in reversed(children):
        if p.poll() is None:
            p.terminate()
    for p in children:
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
    children.clear()


def gx10_address(env: dict) -> str:
    """DOCKT_GX10, else the first word of gx10.txt: '192.168.8.10' or 'asus@169.233.72.21'."""
    gx10 = env.get("DOCKT_GX10", "")
    if not gx10 and (ROOT / "gx10.txt").exists():
        gx10 = next(iter((ROOT / "gx10.txt").read_text(encoding="utf-8-sig").split()), "")
    return gx10


def tunnel_target(mock: bool, env: dict) -> str:
    """user@host when the models should go through an SSH tunnel, else ''."""
    gx10 = gx10_address(env)
    return gx10 if not mock and "@" in gx10 and "VLM_URL" not in env else ""


def model_env(mock: bool) -> dict:
    env = dict(os.environ)
    if mock:
        env["VLM_URL"] = "http://127.0.0.1:9000/v1"
        return env
    gx10 = gx10_address(env)
    if gx10 and "VLM_URL" not in env:   # same defaults as Lane 1's start-dockt.bat (team/lane1.md P0-b)
        host = f"127.0.0.1:{TUNNEL_PORT}" if "@" in gx10 else f"{gx10}:11434"
        env.update(VLM_URL=f"http://{host}/v1", VLM_MODEL="gemma4:26b",
                   LLM_MODEL="nemotron3:33b", EMBED_MODEL="nomic-embed-text")
    return env


def ssh_tunnel_args(target: str, local_port: int = TUNNEL_PORT) -> list[str]:
    """Forward 127.0.0.1:<local_port> to Ollama on the GX10. BatchMode: fail instead of prompting
    for a password nobody can see; keepalives so a dropped Wi-Fi link exits and gets reconnected."""
    return ["-N", "-L", f"127.0.0.1:{local_port}:127.0.0.1:11434",
            "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", target]


def start_tunnel(target: str):
    """Keep an SSH tunnel to the GX10 open until Dockt stops, reconnecting when it drops.
    The tray's "GX10 unreachable" state (from /api/health) shows when it is down."""
    if listening(TUNNEL_PORT):   # a tunnel from an earlier run (or a manual one) is already there
        print(f"127.0.0.1:{TUNNEL_PORT} is already in use; assuming it's a tunnel to the GX10")
        return

    def keep_open():
        while not stopping.is_set():
            try:
                p = spawn("tunnel", ssh_tunnel_args(target), dict(os.environ), program="ssh")
            except FileNotFoundError:   # no OpenSSH client (Windows: Settings > Optional features)
                print("ssh not found; can't open the tunnel to the GX10")
                return
            p.wait()
            stopping.wait(5)

    threading.Thread(target=keep_open, daemon=True).start()
    deadline = time.time() + 8   # let the first connection come up before the indexer needs the models
    while not listening(TUNNEL_PORT) and time.time() < deadline and not stopping.is_set():
        time.sleep(0.3)


def start_backend(mock: bool, capture: bool) -> subprocess.Popen | None:
    """Start what isn't running yet. Returns the service process if this process started it."""
    if answers(BASE + "/api/stats"):
        return None
    env = model_env(mock)
    target = tunnel_target(mock, os.environ)
    if target:
        start_tunnel(target)
    if mock and not answers("http://127.0.0.1:9000/v1/models"):
        spawn("mock_models", ["-m", "uvicorn", "--app-dir", "tools", "mock_models:app", "--port", "9000"], env)
    server = spawn("server", ["server/app.py"], env)
    if capture:
        spawn("capture", ["agent/capture.py", "--server", BASE, "--name", socket.gethostname()], env)
    return server


def start_quickchat() -> subprocess.Popen:
    """Start Lane 1's popup with the same resolved local service URL as the dashboard."""
    return spawn("quickchat", ["agent/quickchat.py"], dict(os.environ))


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
        if u.path == "/quit":
            shutdown_requested.set()
            self.send_response(200)
            self.end_headers()
            if self.window is not None:
                self.window.destroy()
            return
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
    global pending_hash, pending_show
    pending_hash = h
    pending_show = True
    if window is None or not window_ready.is_set():
        return
    try:
        window.restore()
    except Exception:
        pass
    # Windows can restore a hidden WebView behind the foreground application.
    # Keep the show operation independent so unsupported topmost settings on macOS
    # never prevent the dashboard from opening.
    try:
        window.on_top = True
        threading.Timer(1.2, lambda: setattr(window, "on_top", False)).start()
    except Exception:
        pass
    try:
        window.show()
    except Exception:
        pass
    if h:   # the dashboard routes on hashchange (index.html, "deep links")
        try:
            window.evaluate_js(f"location.hash = {json.dumps(h)}")
        except Exception:
            pass


def hide_dashboard_on_close(window) -> bool:
    """Closing the dashboard hides it; Quit Dockt explicitly ends the session."""
    if shutdown_requested.is_set():
        return True
    window.hide()
    return False  # pywebview interprets False as canceling the close event


def hand_off(h: str) -> bool:
    """If a window is already running, give it the link and return True."""
    try:
        q = urllib.parse.urlencode({"hash": h})
        with urllib.request.urlopen(f"http://127.0.0.1:{CONTROL_PORT}/open?{q}", timeout=0.35) as r:
            return r.status == 200
    except Exception:
        return False


def acquire_single_instance_lock(path: Path | None = None) -> bool:
    """Claim one desktop session before importing WebView or starting child services."""
    global _instance_lock_handle
    lock_path = path or (HOME / "desktop.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "a+b")
    lock.seek(0, os.SEEK_END)
    if lock.tell() == 0:
        lock.write(b"\0")
        lock.flush()
    lock.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        lock.close()
        return False
    _instance_lock_handle = lock
    return True


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
    window_ready.set()
    if shutdown_requested.is_set():
        window.destroy()
        return
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
    window.load_url(BASE + "/" + (pending_hash or h))
    if pending_show or h:
        show(window, pending_hash or h)


def main():
    global pending_hash, pending_show
    ap = argparse.ArgumentParser(description="Dockt in a native window")
    ap.add_argument("--mock", action="store_true", help="use tools/mock_models.py instead of the GX10")
    ap.add_argument("--no-capture", action="store_true", help="don't start agent/capture.py")
    ap.add_argument("--open", default="", help='link to show, e.g. "#/capture/3" or "#/chat?q=hello"')
    args = ap.parse_args()
    h = clean_hash(args.open)
    pending_hash, pending_show = h, bool(h)
    window_ready.clear()

    if hand_off(h):   # already running: it shows the link, we're done
        return
    if not acquire_single_instance_lock():
        # A second launch may race the first one's early startup; wait for its
        # control endpoint to bind before deciding it is already running.
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if hand_off(h):
                return
            time.sleep(0.12)
        return

    try:
        control = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT), Control)
    except OSError:
        control = None   # port taken by something else: run anyway, just without hand-off
    if control:
        threading.Thread(target=control.serve_forever, daemon=True).start()

    resolve_port()
    make_icon()
    # The pet has its own local window and can appear while WebView/server imports warm up.
    start_quickchat()
    # The backend may wait for an SSH tunnel; start it after the pet is already visible.
    server = start_backend(args.mock, not args.no_capture)
    atexit.register(stop_children)
    import webview
    if os.name == "nt":   # own taskbar group and icon instead of Python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Dockt.Desktop")
        except Exception:
            pass
    window = webview.create_window(
        "Dockt", html=LOADING.replace("__TEXT__", "Starting Dockt on this laptop…"),
        **dashboard_window_options(h))
    window.events.closing += lambda: hide_dashboard_on_close(window)
    Control.window = window
    try:
        # Persist the browser profile for theme preferences and legacy chat migration.
        webview.start(boot, (window, h, server), private_mode=False, storage_path=str(HOME / "webview"),
                      icon=str(ICON) if ICON.exists() else None)
    finally:
        if control:
            control.shutdown()
            control.server_close()
        Control.window = None
        window_ready.clear()
        stop_children()


if __name__ == "__main__":
    main()
