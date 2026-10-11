"""Windows Ctrl+Space quick-chat for the local Dockt service."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from io import BytesIO
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import webbrowser

from PIL import Image, ImageTk

BASE_URL = os.environ.get("DOCKT_URL", "http://127.0.0.1:8000").rstrip("/")
DESKTOP_CONTROL_PORT = int(os.environ.get("DOCKT_DESKTOP_PORT", "8765"))
HOTKEY_ID = 1
COMPOSER_PLACEHOLDER = "Ask anything you saw…"
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
CITATION = re.compile(r"\[#(\d+)\]")
PALETTES = {
    "light": {
        "bg": "#FAF9F5", "surface": "#FFFFFF", "surface2": "#F5F4EE",
        "recess": "#F0EEE6", "line": "#E8E6DC", "text": "#1F1E1D",
        "muted": "#6B6A65", "accent": "#76B900", "accent_text": "#4D7C0F",
        "accent_soft": "#E8F0D3", "on_accent": "#1F1E1D", "error": "#B91C1C",
    },
    "dark": {
        "bg": "#262624", "surface": "#30302E", "surface2": "#1F1E1D",
        "recess": "#1F1E1D", "line": "#3E3E3A", "text": "#F5F4EE",
        "muted": "#A3A29B", "accent": "#76B900", "accent_text": "#A3E635",
        "accent_soft": "#2D3B22", "on_accent": "#1F1E1D", "error": "#F28B82",
    },
}


def _system_dark_mode() -> bool:
    if os.name == "nt":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
        except (ImportError, OSError):
            return False
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                    capture_output=True, text=True, timeout=2, check=False)
            return result.stdout.strip().lower() == "dark"
        except (OSError, subprocess.SubprocessError):
            return False
    return "dark" in os.environ.get("GTK_THEME", "").lower()


def _edge_path() -> str | None:
    found = shutil.which("msedge")
    if found:
        return found
    for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(key)
        if base:
            candidate = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def _focus_windows_entry(chat_hwnd: int, entry_hwnd: int, user32=None, kernel32=None) -> None:
    """Raise the owning chat window and give its input control keyboard focus."""
    user32 = user32 or ctypes.windll.user32
    kernel32 = kernel32 or ctypes.windll.kernel32
    hwnd = wintypes.HWND(chat_hwnd)
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    hwnd = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE

    user32.GetForegroundWindow.restype = wintypes.HWND
    foreground = user32.GetForegroundWindow()
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD))
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
    current_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetActiveWindow.argtypes = (wintypes.HWND,)
    user32.SetFocus.argtypes = (wintypes.HWND,)

    attached = bool(foreground_thread and foreground_thread != current_thread and
                    user32.AttachThreadInput(current_thread, foreground_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(wintypes.HWND(entry_hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


def open_dockt(path: str = "") -> None:
    # The native desktop shell owns one window and exposes a local hand-off endpoint.
    # Prefer it so citations and "Open in Dockt" don't spawn duplicate Edge windows.
    try:
        query = urlencode({"hash": path})
        with urlopen(f"http://127.0.0.1:{DESKTOP_CONTROL_PORT}/open?{query}", timeout=1) as response:
            if response.status == 200:
                return
    except (HTTPError, URLError, TimeoutError, OSError):
        pass

    url = BASE_URL + path
    edge = _edge_path()
    if edge:
        subprocess.Popen([edge, "--app=" + url], close_fds=True)
    else:
        webbrowser.open(url)


def chat_request(messages: list[dict]) -> dict:
    body = json.dumps({"messages": messages, "stream": False}).encode("utf-8")
    req = Request(BASE_URL + "/api/chat", data=body,
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Dockt returned HTTP {exc.code}. {detail[:180]}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Can't reach Dockt on this laptop. Start the Dockt service and try again.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
        raise RuntimeError("Dockt returned an unexpected chat response.")
    return result


def load_thumbnail(capture_id: int):
    try:
        with urlopen(f"{BASE_URL}/img/{int(capture_id)}", timeout=12) as response:
            image = Image.open(BytesIO(response.read())).convert("RGB")
        image.thumbnail((170, 100), Image.Resampling.LANCZOS)
        return image
    except (HTTPError, URLError, OSError, ValueError):
        return None


class HotkeyListener(threading.Thread):
    """Use RegisterHotKey: no keyboard hook and no administrator access needed."""

    def __init__(self, events: queue.Queue):
        super().__init__(name="dockt-hotkey", daemon=True)
        self.events = events
        self.thread_id = 0
        self.ready = threading.Event()

    def run(self):
        user32 = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        ok = user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_NOREPEAT, VK_SPACE)
        if not ok:
            ok = user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL, VK_SPACE)
        self.ready.set()
        if not ok:
            self.events.put(("hotkey-error", "Ctrl+Space is already in use. Close the app using it and restart Dockt."))
            return

        message = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result <= 0 or message.message == WM_QUIT:
                break
            if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
                self.events.put(("toggle", None))
        user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self):
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


class QuickChat:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Dockt")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry("220x168")
        self.chat = tk.Toplevel(self.root)
        self.chat.title("Dockt Quick Chat")
        self.chat.geometry("560x420")
        self.chat.minsize(480, 360)
        self.chat.attributes("-topmost", True)
        self.chat.withdraw()
        self.chat.protocol("WM_DELETE_WINDOW", self.hide)
        self.chat.bind("<Escape>", lambda _event: self.hide())
        self.events: queue.Queue = queue.Queue()
        self.messages: list[dict[str, str]] = []
        self.busy = False
        self.latest_question = ""
        self.photos: list[ImageTk.PhotoImage] = []
        self.colors = PALETTES["dark" if _system_dark_mode() else "light"]
        self._style()
        self._build()
        self._create_pet()
        self.chat.bind("<Map>", self._chat_mapped, add="+")
        self.root.after(80, self._drain_events)

        self.hotkey = None
        if os.name == "nt":
            self.hotkey = HotkeyListener(self.events)
            self.hotkey.start()
        else:
            self.status.set("The global Ctrl+Space shortcut is available on Windows.")

    def _style(self):
        colors = self.colors
        self.chat.configure(bg=colors["bg"])
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=colors["bg"])
        style.configure("TLabel", background=colors["bg"], foreground=colors["text"],
                        font=("Segoe UI", 11))
        style.configure("Muted.TLabel", background=colors["bg"], foreground=colors["muted"],
                        font=("Segoe UI", 10))
        style.configure("TButton", background=colors["surface2"], foreground=colors["text"],
                        bordercolor=colors["line"], padding=(10, 7), font=("Segoe UI", 10))
        style.map("TButton", background=[("active", colors["line"])])
        style.configure("Accent.TButton", background=colors["accent"],
                        foreground=colors["on_accent"], bordercolor=colors["accent"],
                        padding=(12, 8), font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#6AA600")])
        style.configure("Citation.TButton", background=colors["accent_soft"],
                        foreground=colors["accent_text"], bordercolor=colors["accent_soft"],
                        padding=(5, 2), font=("Segoe UI", 9, "bold"))
        style.map("Citation.TButton", background=[("active", colors["accent"])],
                  foreground=[("active", colors["on_accent"])])

    def _build(self):
        frame = ttk.Frame(self.chat, padding=(20, 18, 20, 16))
        frame.pack(fill="both", expand=True)
        header = ttk.Frame(frame)
        header.pack(fill="x")
        brand = ttk.Frame(header)
        brand.pack(side="left")
        ttk.Label(brand, text="Dockt", foreground=self.colors["text"],
                  font=("Segoe UI", 20, "bold")).pack(side="left")
        ttk.Label(brand, text=".", foreground=self.colors["accent"],
                  font=("Segoe UI", 20, "bold")).pack(side="left")
        ttk.Label(header, text="Quick chat", style="Muted.TLabel").pack(
            side="left", padx=(12, 0), pady=(7, 0))
        ttk.Label(header, text="Ctrl+Space", style="Muted.TLabel").pack(
            side="right", pady=(7, 0))

        self.transcript = tk.Text(frame, wrap="word", height=11, state="disabled",
                                  bg=self.colors["bg"], fg=self.colors["text"],
                                  insertbackground=self.colors["text"],
                                  relief="flat", padx=2, pady=10, font=("Georgia", 13),
                                  spacing1=2, spacing3=10, highlightthickness=0,
                                  takefocus=False)
        self.transcript.pack(fill="both", expand=True, pady=(12, 8))
        self.transcript.tag_configure("user", foreground=self.colors["text"],
                                      font=("Segoe UI", 10, "bold"), justify="right",
                                      spacing1=10)
        self.transcript.tag_configure("user_body", foreground=self.colors["text"],
                                      background=self.colors["surface2"],
                                      font=("Segoe UI", 11), justify="right",
                                      lmargin1=72, lmargin2=72, rmargin=4,
                                      spacing1=3, spacing3=12)
        self.transcript.tag_configure("assistant", foreground=self.colors["accent_text"],
                                      font=("Segoe UI", 10, "bold"), spacing1=8)
        self.transcript.tag_configure("assistant_body", foreground=self.colors["text"],
                                      font=("Georgia", 13), lmargin1=2, lmargin2=2,
                                      rmargin=28, spacing1=2, spacing3=10)
        self.transcript.tag_configure("error", foreground=self.colors["error"])

        self.thumbnail_row = ttk.Frame(frame)
        self.thumbnail_row.pack(fill="x", pady=(0, 7))

        composer = ttk.Frame(frame)
        composer.pack(fill="x")
        self.entry = tk.Text(composer, height=1, wrap="word", takefocus=True,
                             bg=self.colors["surface"], fg=self.colors["text"],
                             insertbackground=self.colors["text"],
                             highlightthickness=1, highlightbackground=self.colors["line"],
                             highlightcolor=self.colors["accent"], relief="flat",
                             padx=12, pady=9, font=("Segoe UI", 11), undo=True)
        self.entry.tag_configure("placeholder", foreground=self.colors["muted"])
        self.placeholder_visible = True
        self.entry.insert("1.0", COMPOSER_PLACEHOLDER, "placeholder")
        self.entry.edit_modified(False)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self.submit)
        self.entry.bind("<Shift-Return>", lambda _event: None)
        self.entry.bind("<KeyRelease>", self._resize_composer, add="+")
        self.entry.bind("<<Modified>>", self._composer_modified, add="+")
        self.entry.bind("<FocusIn>", self._clear_composer_placeholder, add="+")
        self.entry.bind("<FocusOut>", self._show_composer_placeholder, add="+")
        self.entry.bind("<Button-1>", self._entry_clicked, add="+")
        self.ask_button = ttk.Button(composer, text="Ask", style="Accent.TButton",
                                     command=self.submit)
        self.ask_button.pack(side="left", padx=(8, 0), anchor="s")

        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(8, 0))
        self.status = tk.StringVar(value="Ask about something you saw on this computer.")
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel",
                  wraplength=210, justify="left").pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="Quit", command=self.close).pack(side="right")
        ttk.Button(footer, text="Open in Dockt", command=self.open_in_dockt).pack(side="right")
        ttk.Button(footer, text="Hide", command=self.hide).pack(side="right", padx=(0, 7))

    def _create_pet(self):
        self.pet = self.root
        self.pet.title("Dockt")
        self.pet.attributes("-topmost", True)
        self.pet_background = "#ff00ff"
        if os.name == "nt":
            try:
                self.pet.attributes("-transparentcolor", self.pet_background)
            except tk.TclError:
                self.pet_background = "#101318"
        else:
            self.pet_background = "#101318"
        self.pet.configure(bg=self.pet_background)

        self.pet_canvas = tk.Canvas(self.pet, width=220, height=168, bg=self.pet_background,
                                    highlightthickness=0, bd=0, cursor="hand2")
        self.pet_canvas.pack(fill="both", expand=True)
        self.pet_canvas.bind("<ButtonPress-1>", self._pet_press)
        self.pet_canvas.bind("<B1-Motion>", self._pet_drag)
        self.pet_canvas.bind("<ButtonRelease-1>", self._pet_release)
        self.pet_canvas.bind("<Button-3>", self._pet_menu)
        self.pet_canvas.bind("<Enter>", lambda _event: self._pet_hint(True))
        self.pet_canvas.bind("<Leave>", lambda _event: self._pet_hint(False))
        self.pet_xy = (0, 0)
        self.pet_dragged = False
        self.pet_bob = 0
        self.pet_hint_visible = False
        self.pet_menu = tk.Menu(self.pet, tearoff=0, bg=self.colors["surface"],
                                fg=self.colors["text"], activebackground=self.colors["accent_soft"],
                                activeforeground=self.colors["text"])
        self.pet_menu.add_command(label="Open Quick Chat", command=self.show)
        self.pet_menu.add_command(label="Hide Dockt Pet", command=self.hide_pet)
        self.pet_menu.add_separator()
        self.pet_menu.add_command(label="Quit Dockt", command=self.close)
        self._place_pet()
        self._draw_pet()
        self.pet.after(520, self._animate_pet)

    def _place_pet(self):
        self.pet.update_idletasks()
        x = max(0, self.pet.winfo_screenwidth() - 236)
        y = max(0, self.pet.winfo_screenheight() - 205)
        self.pet.geometry(f"220x168+{x}+{y}")

    def _draw_pet(self):
        canvas = self.pet_canvas
        canvas.delete("all")
        bob = self.pet_bob

        # Pixel-like Dockt critter: leaf antenna, rounded lime body, dark faceplate.
        blocks = [
            (7, 4), (8, 4), (8, 3),
            (5, 6), (6, 6), (7, 6), (8, 6), (9, 6),
            (4, 7), (5, 7), (6, 7), (7, 7), (8, 7), (9, 7), (10, 7),
            (3, 8), (4, 8), (5, 8), (6, 8), (7, 8), (8, 8), (9, 8), (10, 8), (11, 8),
            (3, 9), (4, 9), (5, 9), (6, 9), (7, 9), (8, 9), (9, 9), (10, 9), (11, 9),
            (4, 10), (5, 10), (6, 10), (7, 10), (8, 10), (9, 10), (10, 10),
            (4, 11), (5, 11), (9, 11), (10, 11),
        ]
        size, left, top = 7, 58, 16 + bob
        for col, row in blocks:
            color = self.colors["accent"] if (col + row) % 4 else self.colors["accent_text"]
            canvas.create_rectangle(left + col * size, top + row * size,
                                    left + (col + 1) * size, top + (row + 1) * size,
                                    fill=color, outline=color)

        face = (left + 5 * size, top + 8 * size, left + 10 * size, top + 10 * size)
        face_color = self.colors["surface2"]
        canvas.create_rectangle(*face, fill=face_color, outline="")
        eye_y = top + 8 * size + 5
        canvas.create_rectangle(left + 6 * size, eye_y, left + 7 * size, eye_y + 5,
                                fill=self.colors["text"], outline="")
        canvas.create_rectangle(left + 9 * size, eye_y, left + 10 * size, eye_y + 5,
                                fill=self.colors["text"], outline="")
        canvas.create_rectangle(left + 7 * size, eye_y + 10, left + 9 * size,
                                eye_y + 12, fill=self.colors["accent_text"], outline="")
        # Put the wordmark on a solid background so Windows' transparent-color
        # keying cannot leave a magenta fringe around anti-aliased text.
        canvas.create_rectangle(69, 132, 151, 157, fill=self.colors["surface"],
                                outline=self.colors["line"])
        canvas.create_text(110, 144, text="Dockt", fill=self.colors["text"],
                           font=("Segoe UI", 10, "bold"))
        if self.pet_hint_visible:
            canvas.create_rectangle(5, 4, 215, 27, fill=self.colors["surface"],
                                    outline=self.colors["line"])
            canvas.create_text(110, 15, text="Click to chat  |  Ctrl+Space hides",
                               fill=self.colors["text"], font=("Segoe UI", 9))
    def _animate_pet(self):
        if not self.pet.winfo_exists():
            return
        self.pet_bob = 2 if self.pet_bob == 0 else 0
        self._draw_pet()
        self.pet.after(520, self._animate_pet)

    def _pet_hint(self, visible: bool):
        self.pet_hint_visible = visible
        self._draw_pet()

    def _pet_press(self, event):
        self.pet_xy = (event.x_root - self.pet.winfo_x(),
                       event.y_root - self.pet.winfo_y())
        self.pet_dragged = False

    def _pet_drag(self, event):
        if abs(event.x_root - self.pet.winfo_x() - self.pet_xy[0]) + \
                abs(event.y_root - self.pet.winfo_y() - self.pet_xy[1]) > 4:
            self.pet_dragged = True
        if self.pet_dragged:
            self.pet.geometry(f"+{event.x_root - self.pet_xy[0]}+{event.y_root - self.pet_xy[1]}")

    def _pet_release(self, _event):
        if not self.pet_dragged:
            self.show()

    def _pet_menu(self, event):
        self.pet_menu.tk_popup(event.x_root, event.y_root)

    def toggle(self):
        if self.chat.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    def hide_pet(self):
        self.hide()
        self.pet.withdraw()

    def show(self):
        self.pet.deiconify()
        self.pet.lift()
        self.pet.attributes("-topmost", True)
        self.chat.update_idletasks()
        width, height = self.chat.winfo_width(), self.chat.winfo_height()
        screen_width, screen_height = self.chat.winfo_screenwidth(), self.chat.winfo_screenheight()
        x = max(0, min(self.pet.winfo_x() - width + 30, screen_width - width))
        y = max(0, min(self.pet.winfo_y() - height + 15, screen_height - height))
        self.chat.geometry(f"{width}x{height}+{x}+{y}")
        self.chat.deiconify()
        self.chat.lift()
        self.chat.attributes("-topmost", True)
        self.chat.after_idle(self._focus_entry)
        self.chat.after(120, self._focus_entry_if_unfocused)

    def _chat_mapped(self, _event=None):
        if self.chat.state() != "withdrawn":
            self.chat.after_idle(self._focus_entry)

    def _entry_clicked(self, _event=None):
        # Let Tk place the insertion cursor from the click before raising/focusing.
        self.chat.after_idle(lambda: self._focus_entry(move_cursor=False))

    def _focus_entry_if_unfocused(self):
        if self.chat.state() == "withdrawn":
            return
        focused = self.chat.focus_get()
        if focused is None or focused == self.chat:
            self._focus_entry()

    def _focus_entry(self, move_cursor: bool = True):
        if self.chat.state() == "withdrawn":
            return
        self.chat.deiconify()
        self.chat.lift()
        if os.name == "nt":
            try:
                user32 = ctypes.windll.user32
                _focus_windows_entry(self.chat.winfo_id(), self.entry.winfo_id(),
                                     user32, ctypes.windll.kernel32)
            except (AttributeError, OSError, tk.TclError, TypeError):
                pass
        self.chat.focus_force()
        self.entry.focus_set()
        if move_cursor:
            self.entry.mark_set("insert", "end-1c")

    def _resize_composer(self, _event=None):
        lines = self.entry.count("1.0", "end-1c", "displaylines") or (1,)
        self.entry.configure(height=max(1, min(5, lines[0])))

    def _composer_modified(self, _event=None):
        if self.entry.edit_modified():
            self.entry.edit_modified(False)
            self._resize_composer()

    def _clear_composer_placeholder(self, _event=None):
        if self.placeholder_visible:
            self.entry.delete("1.0", "end")
            self.placeholder_visible = False
            self.entry.edit_modified(False)
            self._resize_composer()

    def _show_composer_placeholder(self, _event=None):
        if not self.entry.get("1.0", "end-1c").strip():
            self.placeholder_visible = True
            self.entry.insert("1.0", COMPOSER_PLACEHOLDER, "placeholder")
            self.entry.edit_modified(False)
            self._resize_composer()

    def hide(self):
        self.chat.withdraw()

    def submit(self, _event=None):
        if self.placeholder_visible:
            return "break"
        question = self.entry.get("1.0", "end-1c").strip()
        if not question or self.busy:
            return "break"
        self.busy = True
        self.latest_question = question
        self.entry.delete("1.0", "end")
        self._resize_composer()
        self.status.set("Searching your screen memories...")
        self._render(self.messages + [{"role": "user", "content": question}])
        request_messages = self.messages + [{"role": "user", "content": question}]
        threading.Thread(target=self._ask, args=(question, request_messages), daemon=True).start()
        return "break"

    def _ask(self, question: str, request_messages: list[dict]):
        try:
            result = chat_request(request_messages)
            answer = result["answer"]
            self.events.put(("answer", (question, answer, result)))
        except Exception as exc:
            self.events.put(("error", str(exc)))
            return

        # A thumbnail failure must not turn a successful answer into a chat error.
        try:
            sources = result.get("sources") or []
            cited = result.get("cited") or [int(cid) for cid in CITATION.findall(answer)]
            ids = [int(cid) for cid in cited if str(cid).isdigit()]
            if not ids:
                ids = [int(row["id"]) for row in sources[:3] if row.get("id") is not None]
            for cid in ids[:3]:
                image = load_thumbnail(cid)
                if image is not None:
                    self.events.put(("thumbnail", (cid, image)))
        except (TypeError, ValueError, KeyError, AttributeError):
            pass

    def _render(self, messages: list[dict], error: str = ""):
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        for message in messages:
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            self.transcript.insert("end", "You\n" if role == "user" else "Dockt\n",
                                   role)
            if role == "assistant":
                self._insert_citations(message.get("content", ""), "assistant_body")
            else:
                self.transcript.insert("end", message.get("content", "") + "\n\n",
                                       "user_body")
        if error:
            self.transcript.insert("end", "Dockt\n", "error")
            self.transcript.insert("end", error + "\n\n", "error")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _insert_citations(self, text: str, tag: str = "assistant_body"):
        start = 0
        for match in CITATION.finditer(text):
            self.transcript.insert("end", text[start:match.start()], tag)
            cid = int(match.group(1))
            button = ttk.Button(self.transcript, text=f"#{cid}", style="Citation.TButton",
                                command=lambda capture_id=cid: open_dockt(f"/#/capture/{capture_id}"))
            self.transcript.window_create("end", window=button)
            start = match.end()
        self.transcript.insert("end", text[start:] + "\n\n", tag)

    def _clear_thumbnails(self):
        for child in self.thumbnail_row.winfo_children():
            child.destroy()
        self.photos.clear()

    def _add_thumbnail(self, cid: int, image: Image.Image):
        photo = ImageTk.PhotoImage(image)
        self.photos.append(photo)
        button = ttk.Button(self.thumbnail_row, image=photo, text=f"Capture #{cid}",
                            compound="top",
                            command=lambda capture_id=cid: open_dockt(f"/#/capture/{capture_id}"))
        button.pack(side="left", padx=(0, 8))

    def open_in_dockt(self):
        if not self.latest_question:
            open_dockt()
            return
        open_dockt("/#/chat?q=" + quote(self.latest_question, safe=""))

    def _drain_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "toggle":
                    self.toggle()
                elif kind == "hotkey-error":
                    self.status.set(value)
                    self.show()
                elif kind == "error":
                    self.busy = False
                    self.status.set("Dockt could not answer. Check that the local service is running.")
                    self._render(self.messages, "I couldn't reach Dockt. Check that it is running and try again.")
                elif kind == "thumbnail":
                    capture_id, image = value
                    self._add_thumbnail(capture_id, image)
                elif kind == "answer":
                    question, answer, result = value
                    self.messages.extend([
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": answer},
                    ])
                    self.busy = False
                    self.status.set(f"Answered in {result.get('ms', 0)} ms | Esc hides this window")
                    self._render(self.messages)
                    self._clear_thumbnails()
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def close(self):
        if self.hotkey:
            self.hotkey.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = QuickChat(root)
    root.mainloop()


if __name__ == "__main__":
    main()
