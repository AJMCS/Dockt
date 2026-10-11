"""Windows Ctrl+Space quick-chat for the local Dockt service."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from io import BytesIO
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import customtkinter as ctk
from tkinter import ttk
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import webbrowser
import uuid

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
CAPTURE_PAUSE_FILE = Path.home() / ".dockt_pause"
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


def _enable_windows_dpi_awareness(user32=None, shcore=None) -> bool:
    """Opt Tk into crisp per-monitor rendering before creating its first window."""
    if os.name != "nt" and user32 is None:
        return False
    user32 = user32 or ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        pass
    try:
        shcore = shcore or ctypes.windll.shcore
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return True
    except (AttributeError, OSError):
        return False


def capture_is_paused() -> bool:
    """Match the capture agent's shared per-user pause switch."""
    return CAPTURE_PAUSE_FILE.exists()


def toggle_capture() -> bool:
    """Toggle capture and return True when capture is now paused."""
    if capture_is_paused():
        CAPTURE_PAUSE_FILE.unlink(missing_ok=True)
        return False
    CAPTURE_PAUSE_FILE.touch()
    return True


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


def chat_request(messages: list[dict], conversation_id: str | None = None) -> dict:
    payload = {"messages": messages, "stream": False}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    body = json.dumps(payload).encode("utf-8")
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


def load_conversation(conversation_id: str) -> dict | None:
    try:
        with urlopen(BASE_URL + "/api/conversations/" + quote(conversation_id, safe=""), timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None


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
        self.root.withdraw()
        self.ui_scale = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
        self.colors = PALETTES["dark" if _system_dark_mode() else "light"]
        self.chat = ctk.CTkToplevel(self.root, fg_color=self.colors["bg"])
        self.chat.title("Dockt Quick Chat")
        self.chat.geometry("720x600")
        self.chat.minsize(360, 280)
        self.chat.attributes("-topmost", True)
        self.chat.withdraw()
        self.chat.protocol("WM_DELETE_WINDOW", self.hide)
        self.chat.bind("<Escape>", lambda _event: self.hide())
        self.events: queue.Queue = queue.Queue()
        self.messages: list[dict[str, str]] = []
        self.conversation_id = str(uuid.uuid4())
        self.history_loading = False
        self.history_ts = 0
        self.busy = False
        self.latest_question = ""
        self.retry_payload = None
        self.photos: list[ImageTk.PhotoImage] = []
        self._composer_resize_job = None
        self._style()
        self._build()
        self.chat.bind("<Configure>", self._schedule_responsive_layout, add="+")
        self._create_pet()
        self.root.after(80, self._drain_events)
        self.root.after(1000, self._poll_shared_history)

        self.hotkey = None
        if os.name == "nt":
            self.hotkey = HotkeyListener(self.events)
            self.hotkey.start()
        else:
            self.status.set("The global Ctrl+Space shortcut is available on Windows.")

    def _style(self):
        colors = self.colors
        self.chat.configure(fg_color=colors["bg"])
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=colors["bg"])
        style.configure("MenuCard.TFrame", background=colors["surface"])
        style.configure("MenuCard.TLabel", background=colors["surface"],
                        foreground=colors["text"], font=("Segoe UI", 12, "bold"))
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
        style.configure("Menu.TButton", background=colors["surface"],
                        foreground=colors["text"], bordercolor=colors["surface"],
                        anchor="w", padding=(12, 9), font=("Segoe UI", 10))
        style.map("Menu.TButton", background=[("active", colors["accent_soft"])],
                  foreground=[("active", colors["text"])])
        style.configure("MenuDanger.TButton", background=colors["surface"],
                        foreground=colors["error"], bordercolor=colors["surface"],
                        anchor="w", padding=(12, 9), font=("Segoe UI", 10))
        style.map("MenuDanger.TButton", background=[("active", colors["accent_soft"])])
        style.configure("Citation.TButton", background=colors["accent_soft"],
                        foreground=colors["accent_text"], bordercolor=colors["accent_soft"],
                        padding=(5, 2), font=("Segoe UI", 9, "bold"))
        style.map("Citation.TButton", background=[("active", colors["accent"])],
                  foreground=[("active", colors["on_accent"])])

    def _build(self):
        frame = ctk.CTkFrame(self.chat, fg_color=self.colors["bg"], corner_radius=20)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        content = ctk.CTkFrame(frame, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=10, pady=8)
        frame = content
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        header = ctk.CTkFrame(frame, fg_color="transparent", height=32)
        header.grid(row=0, column=0, sticky="ew")
        brand = ctk.CTkFrame(header, fg_color="transparent", height=1)
        brand.pack(side="left")
        ctk.CTkLabel(brand, text="Dockt", text_color=self.colors["text"],
                     font=("Segoe UI", 19, "bold")).pack(side="left")
        ctk.CTkLabel(brand, text=".", text_color=self.colors["accent"],
                     font=("Segoe UI", 19, "bold")).pack(side="left")
        ctk.CTkLabel(header, text="Quick chat", text_color=self.colors["muted"],
                     font=("Segoe UI", 11)).pack(
            side="left", padx=(12, 0), pady=(7, 0))
        ctk.CTkLabel(header, text="Ctrl+Space", text_color=self.colors["muted"],
                     font=("Segoe UI", 10)).pack(
            side="right", pady=(7, 0))

        transcript_card = ctk.CTkFrame(
            frame, fg_color=self.colors["surface"], corner_radius=16, height=1)
        transcript_card.grid(row=1, column=0, sticky="nsew", pady=(12, 8))
        self.transcript = tk.Text(transcript_card, wrap="word", height=1, state="disabled",
                                  bg=self.colors["surface"], fg=self.colors["text"],
                                  insertbackground=self.colors["text"],
                                  relief="flat", padx=10, pady=10, font=("Segoe UI", 12),
                                  spacing1=2, spacing3=10, highlightthickness=0,
                                  takefocus=False)
        self.transcript.pack(side="left", fill="both", expand=True, padx=(9, 0), pady=8)
        transcript_scrollbar = ttk.Scrollbar(transcript_card, orient="vertical",
                                             command=self.transcript.yview)
        transcript_scrollbar.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self.transcript.configure(yscrollcommand=transcript_scrollbar.set)
        self.transcript.tag_configure("user", foreground=self.colors["text"],
                                      font=("Segoe UI", 10, "bold"), justify="right",
                                      spacing1=10)
        self.transcript.tag_configure("user_body", foreground=self.colors["text"],
                                      background=self.colors["accent_soft"],
                                      font=("Segoe UI", 11), justify="right",
                                      lmargin1=72, lmargin2=72, rmargin=4,
                                      spacing1=3, spacing3=12)
        self.transcript.tag_configure("assistant", foreground=self.colors["accent_text"],
                                      font=("Segoe UI", 10, "bold"), spacing1=8)
        self.transcript.tag_configure("assistant_body", foreground=self.colors["text"],
                                      font=("Segoe UI", 12), lmargin1=2, lmargin2=2,
                                      rmargin=28, spacing1=2, spacing3=10)
        self.transcript.tag_configure("error", foreground=self.colors["error"])

        self.thumbnail_row = ctk.CTkFrame(frame, fg_color="transparent", height=1)
        self.thumbnail_row.grid(row=2, column=0, sticky="ew", pady=(0, 7))

        composer = ctk.CTkFrame(frame, fg_color="transparent", height=1)
        composer.grid(row=3, column=0, sticky="ew")
        composer.columnconfigure(0, weight=1)
        self.entry = ctk.CTkTextbox(composer, height=44, wrap="word", activate_scrollbars=False,
                                     corner_radius=14, border_width=1,
                                     border_color=self.colors["line"], fg_color=self.colors["surface"],
                                     text_color=self.colors["text"], font=("Segoe UI", 11),
                                     undo=True)
        self.entry._textbox.configure(insertbackground=self.colors["text"], padx=12, pady=9)
        self.entry._textbox.tag_configure("placeholder", foreground=self.colors["muted"])
        self.placeholder_visible = True
        self.entry.insert("1.0", COMPOSER_PLACEHOLDER, "placeholder")
        self.entry.edit_modified(False)
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<Return>", self.submit)
        self.entry.bind("<Shift-Return>", lambda _event: None)
        self.entry.bind("<<Modified>>", self._composer_modified, add="+")
        self.entry.bind("<FocusIn>", self._clear_composer_placeholder, add="+")
        self.entry.bind("<FocusOut>", self._show_composer_placeholder, add="+")
        self.entry.bind("<Button-1>", self._entry_clicked, add="+")
        self.ask_button = ctk.CTkButton(composer, text="Send", width=78, height=42,
                                        corner_radius=13, fg_color=self.colors["accent"],
                                        hover_color="#6AA600", text_color=self.colors["on_accent"],
                                        font=("Segoe UI", 11, "bold"), command=self.submit)
        self.ask_button.grid(row=0, column=1, padx=(8, 0), sticky="s")
        self.retry_button = ctk.CTkButton(
            composer, text="Retry", command=self.retry_latest, width=76, height=42,
            corner_radius=13, fg_color=self.colors["surface2"],
            hover_color=self.colors["line"], text_color=self.colors["text"],
            font=("Segoe UI", 10, "bold"))
        self.retry_button.grid(row=0, column=2, padx=(6, 0), sticky="s")
        self.retry_button.grid_remove()

        footer = ctk.CTkFrame(frame, fg_color="transparent", height=1)
        footer.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="Ask about something you saw on this computer.")
        self.status_label = ctk.CTkLabel(
            footer, textvariable=self.status, text_color=self.colors["muted"],
            font=("Segoe UI", 10), wraplength=210, justify="left", anchor="w")
        self.footer_buttons = []
        for column, (label, command) in enumerate(
                (("Hide", self.hide), ("Open dashboard", self.open_in_dockt),
                 ("Quit", self.close)), start=1):
            button = ctk.CTkButton(
                footer, text=label, command=command, width=96, height=32,
                corner_radius=11, fg_color=self.colors["surface2"],
                hover_color=self.colors["line"], text_color=self.colors["text"],
                font=("Segoe UI", 10))
            button.grid(row=0, column=column, padx=(6, 0), sticky="e")
            self.footer_buttons.append(button)
        self.footer = footer
        self._layout_footer()

    def _schedule_responsive_layout(self, event=None):
        if event is not None and event.widget is not self.chat:
            return
        if getattr(self, "_responsive_layout_job", None) is None:
            self._responsive_layout_job = self.chat.after_idle(self._layout_footer)

    def _layout_footer(self):
        self._responsive_layout_job = None
        if not hasattr(self, "footer"):
            return
        scale = ctk.ScalingTracker.get_window_scaling(self.chat)
        width = self.chat.winfo_width() / max(1.0, scale)
        compact = width < 620
        narrow = width < 430
        if compact:
            self.status_label.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 6))
            for column, button in enumerate(self.footer_buttons, start=1):
                button.grid_configure(row=1, column=column,
                                      padx=(0 if column == 1 else 6, 0))
            self.footer.columnconfigure(1, weight=1, uniform="footer-actions")
            self.footer.columnconfigure(2, weight=1, uniform="footer-actions")
            self.footer.columnconfigure(3, weight=1, uniform="footer-actions")
            for button, text in zip(self.footer_buttons,
                                    ("Hide", "Dash" if narrow else "Dashboard", "Quit")):
                button.configure(text=text, width=64 if narrow else 96)
        else:
            self.status_label.grid(row=0, column=0, sticky="ew", pady=0)
            self.status_label.configure(wraplength=max(120, int(width - 390)))
            for column, button in enumerate(self.footer_buttons, start=1):
                button.grid_configure(row=0, column=column, padx=(6, 0))
                button.configure(width=96)
            for column in (1, 2, 3):
                self.footer.columnconfigure(column, weight=0, uniform="")
        self._resize_composer()

    def _create_pet(self):
        self.pet = tk.Toplevel(self.root)
        self.pet.overrideredirect(True)
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

        self.pet_canvas = tk.Canvas(self.pet, width=round(160*self.ui_scale),
                                    height=round(140*self.ui_scale), bg=self.pet_background,
                                    highlightthickness=0, bd=0, cursor="hand2")
        self.pet_canvas.pack(fill="both", expand=True)
        self.pet_canvas.bind("<ButtonPress-1>", self._pet_press)
        self.pet_canvas.bind("<B1-Motion>", self._pet_drag)
        self.pet_canvas.bind("<ButtonRelease-1>", self._pet_release)
        self.pet_canvas.bind("<Button-3>", self._pet_menu)
        self.pet_canvas.bind("<Enter>", lambda _event: self._pet_hint(True))
        self.pet_canvas.bind("<Leave>", self._schedule_pet_hint_hide)
        self.pet_canvas.bind("<Motion>", self._pet_motion)
        self.pet_xy = (0, 0)
        self.pet_dragged = False
        self.pet_bob = 0
        self.pet_hint_visible = False
        self.pet_hover_button = None
        self._hint_hide_job = None
        self.options_panel = None
        self._place_pet()
        self._draw_pet()
        self.pet.after(520, self._animate_pet)

    def _place_pet(self):
        self.pet.update_idletasks()
        width, height = round(160*self.ui_scale), round(140*self.ui_scale)
        x = max(0, self.pet.winfo_screenwidth() - width - 24)
        y = max(0, self.pet.winfo_screenheight() - height - 56)
        self.pet.geometry(f"{width}x{height}+{x}+{y}")

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
        size, left, top = 10, 5, -10 + bob
        for col, row in blocks:
            color = "#90B87B" if (col + row) % 4 else "#739662"
            canvas.create_rectangle(left + col * size, top + row * size,
                                    left + (col + 1) * size, top + (row + 1) * size,
                                    fill=color, outline=color)

        face = (left + 5 * size, top + 8 * size, left + 10 * size, top + 10 * size)
        face_color = "#303A32"
        canvas.create_rectangle(*face, fill=face_color, outline="")
        eye_y = top + 8 * size + 5
        canvas.create_rectangle(left + 6 * size, eye_y, left + 7 * size, eye_y + 5,
                                fill="#ECF2E9", outline="")
        canvas.create_rectangle(left + 9 * size, eye_y, left + 10 * size, eye_y + 5,
                                fill="#ECF2E9", outline="")
        canvas.create_rectangle(left + 7 * size, eye_y + 10, left + 9 * size,
                                eye_y + 12, fill="#90B87B", outline="")
        if self.pet_hint_visible:
            self._draw_pet_controls(canvas)
        canvas.scale("all", 0, 0, self.ui_scale, self.ui_scale)

    @staticmethod
    def _rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
        points = [x1 + radius, y1, x2 - radius, y1, x2, y1,
                  x2, y1 + radius, x2, y2 - radius, x2, y2,
                  x2 - radius, y2, x1 + radius, y2, x1, y2,
                  x1, y2 - radius, x1, y1 + radius, x1, y1]
        return canvas.create_polygon(*points, smooth=True, splinesteps=12, **kwargs)

    def _draw_pet_controls(self, canvas):
        self._rounded_rect(canvas, 43, 110, 117, 138, 14, fill="#252824",
                           outline="#454A43", width=1, tags=("pet-control-bar",))
        for name, center in (("chat", 64), ("menu", 96)):
            active = self.pet_hover_button == name
            fill = "#41493B" if active else "#30342F"
            outline = "#90B87B" if active else "#50564D"
            canvas.create_oval(center - 10, 114, center + 10, 134, fill=fill,
                               outline=outline, width=1.3,
                               tags=("pet-control", f"pet-control-{name}"))
            if name == "chat":
                canvas.create_rectangle(center - 5, 119, center + 5, 127,
                                        outline="#F1F3EE", width=1.5,
                                        tags=("pet-control", f"pet-control-{name}"))
                canvas.create_line(center - 2, 127, center - 3, 130, center + 1, 127,
                                   fill="#F1F3EE", width=1.5,
                                   tags=("pet-control", f"pet-control-{name}"))
            else:
                for y in (120, 124, 128):
                    canvas.create_oval(center - 1.5, y - 1.5, center + 1.5, y + 1.5,
                                       fill="#F1F3EE", outline="",
                                       tags=("pet-control", f"pet-control-{name}"))

            if active:
                title = "Open quick chat" if name == "chat" else "More options"
                tooltip_width = 82 if name == "chat" else 68
                left = max(6, min(154 - tooltip_width, center - tooltip_width / 2))
                self._rounded_rect(canvas, left, 89, left + tooltip_width, 106, 8,
                                   fill="#252824", outline="#454A43", width=1,
                                   tags=("pet-tooltip",))
                canvas.create_text(left + tooltip_width / 2, 97.5, text=title,
                                   fill="#F1F3EE", font=("Segoe UI", 8),
                                   tags=("pet-tooltip",))

    def _pet_button_at(self, x, y):
        x, y = x / self.ui_scale, y / self.ui_scale
        if 54 <= x <= 74 and 114 <= y <= 134:
            return "chat"
        if 86 <= x <= 106 and 114 <= y <= 134:
            return "menu"
        return None

    def _pet_motion(self, event):
        button = self._pet_button_at(event.x, event.y) if self.pet_hint_visible else None
        if button != self.pet_hover_button:
            self.pet_hover_button = button
            self._draw_pet()
    def _animate_pet(self):
        if not self.pet.winfo_exists():
            return
        self.pet_bob = 2 if self.pet_bob == 0 else 0
        self._draw_pet()
        self.pet.after(520, self._animate_pet)

    def _pet_hint(self, visible: bool):
        if self._hint_hide_job is not None:
            try:
                self.root.after_cancel(self._hint_hide_job)
            except tk.TclError:
                pass
            self._hint_hide_job = None
        self.pet_hint_visible = visible
        if not visible:
            self.pet_hover_button = None
        self._draw_pet()

    def _schedule_pet_hint_hide(self, _event=None):
        if self._hint_hide_job is not None:
            try:
                self.root.after_cancel(self._hint_hide_job)
            except tk.TclError:
                pass
        self._hint_hide_job = self.root.after(320, lambda: self._pet_hint(False))

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
        if self.pet_dragged:
            return
        button = self._pet_button_at(_event.x, _event.y)
        if button == "chat":
            self.show()
        elif button == "menu":
            self._pet_menu(_event)

    def _pet_menu(self, event):
        self._pet_hint(False)
        self._show_options_at(event.x_root, event.y_root)

    def _show_options_at(self, x_root, y_root):
        self._pet_hint(False)
        self._close_options_panel()
        panel = tk.Toplevel(self.pet)
        self.options_panel = panel
        panel.overrideredirect(True)
        panel.attributes("-topmost", True)
        panel.configure(bg=self.colors["line"])

        card = ttk.Frame(panel, padding=(8, 8), style="MenuCard.TFrame")
        card.pack(fill="both", expand=True, padx=1, pady=1)
        header = ttk.Frame(card, style="MenuCard.TFrame")
        header.pack(fill="x", padx=6, pady=(4, 8))
        ttk.Label(header, text="Dockt controls", font=("Segoe UI", 12, "bold"),
                  style="MenuCard.TLabel").pack(side="left")
        ttk.Button(header, text="×", width=3, style="Menu.TButton",
                   command=self._close_options_panel).pack(side="right")

        self._add_option(card, "Open Quick Chat", self.show)
        self._add_option(card, "Open Dashboard", lambda: open_dockt())
        self._add_option(card, "Hide Dockt Pet", self.hide_pet)
        self.capture_option = self._add_option(card, self._capture_menu_label(), self._toggle_capture)
        ttk.Separator(card).pack(fill="x", padx=6, pady=6)
        self._add_option(card, "Quit Dockt", self.close, danger=True)

        panel.update_idletasks()
        width, height = panel.winfo_reqwidth(), panel.winfo_reqheight()
        screen_w, screen_h = panel.winfo_screenwidth(), panel.winfo_screenheight()
        x = min(x_root, screen_w - width - 8)
        y = y_root if y_root + height <= screen_h else y_root - height
        panel.geometry(f"+{max(8, x)}+{max(8, y)}")
        panel.bind("<Escape>", lambda _event: self._close_options_panel())
        panel.bind("<FocusOut>", self._options_focus_out, add="+")
        panel.focus_force()

    def _add_option(self, parent, label: str, action, danger: bool = False):
        style = "MenuDanger.TButton" if danger else "Menu.TButton"
        button = ttk.Button(parent, text=label, style=style,
                            command=lambda: self._run_option(action))
        button.pack(fill="x", padx=2, pady=1)
        return button

    def _run_option(self, action):
        self._close_options_panel()
        action()

    def _close_options_panel(self):
        panel = getattr(self, "options_panel", None)
        self.options_panel = None
        if panel is not None:
            try:
                panel.destroy()
            except tk.TclError:
                pass

    def _options_focus_out(self, _event=None):
        panel = self.options_panel
        if panel is not None:
            panel.after(120, lambda: self._dismiss_options_if_focus_left(panel))

    @staticmethod
    def _focus_is_inside_options(panel, focus) -> bool:
        return focus is not None and (str(focus) == str(panel) or str(focus).startswith(str(panel) + "."))

    def _dismiss_options_if_focus_left(self, panel):
        if self.options_panel is not panel:
            return
        try:
            focus = self.root.focus_get()
        except tk.TclError:
            focus = None
        if not self._focus_is_inside_options(panel, focus):
            self._close_options_panel()

    @staticmethod
    def _capture_menu_label() -> str:
        return "Resume screen capture" if capture_is_paused() else "Pause screen capture"

    def _toggle_capture(self):
        paused = toggle_capture()
        if self.options_panel is not None and hasattr(self, "capture_option"):
            self.capture_option.configure(text=self._capture_menu_label())
        self.status.set("Screen capture paused" if paused else "Screen capture resumed")

    def toggle(self):
        """Toggle pet visibility for the global keyboard shortcut."""
        if self.pet.state() == "withdrawn":
            self.pet.deiconify()
            self.pet.lift()
            self.pet.attributes("-topmost", True)
        else:
            self.hide_pet()

    def hide_pet(self):
        self.hide()
        self._pet_hint(False)
        self._close_options_panel()
        self.pet.withdraw()

    def show(self):
        self._pet_hint(False)
        self.pet.deiconify()
        self.pet.lift()
        self.pet.attributes("-topmost", True)
        self.chat.update_idletasks()
        scale = ctk.ScalingTracker.get_window_scaling(self.chat)
        screen_width, screen_height = self.chat.winfo_screenwidth(), self.chat.winfo_screenheight()
        # CTkToplevel geometry and minsize arguments are logical units; the
        # screen dimensions and winfo sizes are physical pixels. Convert the
        # available monitor area before sizing, or HiDPI scaling can force the
        # footer and composer below the visible edge.
        available_width = max(1, round((screen_width - 32) / scale))
        available_height = max(1, round((screen_height - 80) / scale))
        width = min(720, available_width)
        height = min(600, available_height)
        self.chat.minsize(min(360, width), min(280, height))
        x = max(0, round((screen_width / scale - width) / 2))
        y = max(0, round((screen_height / scale - height) / 2))
        self.chat.geometry(f"{width}x{height}+{x}+{y}")
        self.chat.deiconify()
        self.chat.lift()
        self.chat.attributes("-topmost", True)
        self.chat.after_idle(self._focus_entry)
        self.chat.after(120, self._focus_entry_if_unfocused)

    def _poll_shared_history(self):
        # A fresh chat gets a UUID before it has a saved row. Polling that
        # nonexistent conversation every 3 s produces a stream of 404s until
        # the first message is stored.
        if (self.history_ts > 0 and not self.busy and not self.history_loading and
                self.chat.state() != "withdrawn"):
            self.history_loading = True
            cid = self.conversation_id

            def fetch():
                self.events.put(("history", (cid, load_conversation(cid))))

            threading.Thread(target=fetch, daemon=True).start()
        self.root.after(3000, self._poll_shared_history)

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
        # show() maps and raises the window before scheduling this focus helper.
        # Keep this method from remapping an already visible CTkToplevel.
        self.chat.lift()
        if os.name == "nt":
            try:
                user32 = ctypes.windll.user32
                _focus_windows_entry(self.chat.winfo_id(), self.entry.winfo_id(),
                                     user32, ctypes.windll.kernel32)
            except (AttributeError, OSError, tk.TclError, TypeError):
                pass
        self.entry.focus_force()
        if move_cursor:
            self.entry.mark_set("insert", "end-1c")

    def _resize_composer(self, _event=None):
        self._composer_resize_job = None
        lines = self.entry._textbox.count("1.0", "end-1c", "displaylines") or (1,)
        scale = ctk.ScalingTracker.get_window_scaling(self.chat)
        window_height = self.chat.winfo_height() / max(1.0, scale)
        max_height = max(44, min(128, int(window_height - 260)))
        desired_height = max(44, min(max_height, 34 * lines[0]))
        if int(self.entry.cget("height")) != desired_height:
            self.entry.configure(height=desired_height)

    def _schedule_composer_resize(self):
        if self._composer_resize_job is not None:
            try:
                self.root.after_cancel(self._composer_resize_job)
            except tk.TclError:
                pass
        self._composer_resize_job = self.root.after(140, self._resize_composer)

    def _composer_modified(self, _event=None):
        if self.entry.edit_modified():
            self.entry.edit_modified(False)
            self._schedule_composer_resize()

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
        self.ask_button.configure(state="disabled")
        self.latest_question = question
        self.retry_payload = None
        self.retry_button.grid_remove()
        self.entry.delete("1.0", "end")
        self._resize_composer()
        self.status.set("Searching your screen memories...")
        self._render(self.messages + [{"role": "user", "content": question}])
        request_messages = self.messages + [{"role": "user", "content": question}]
        threading.Thread(target=self._ask, args=(question, request_messages), daemon=True).start()
        return "break"

    def retry_latest(self):
        if self.busy or not self.retry_payload:
            return "break"
        request_messages = self.retry_payload
        self.busy = True
        self.ask_button.configure(state="disabled")
        self.retry_button.configure(state="disabled")
        self.status.set("Retrying your question...")
        question = next((m["content"] for m in reversed(request_messages) if m.get("role") == "user"), "")
        threading.Thread(target=self._ask, args=(question, request_messages), daemon=True).start()
        return "break"

    def _ask(self, question: str, request_messages: list[dict]):
        try:
            result = chat_request(request_messages, self.conversation_id)
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
        if not self.messages:
            open_dockt()
            return
        open_dockt("/#/conversation/" + quote(self.conversation_id, safe=""))

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
                    self.ask_button.configure(state="normal")
                    self.retry_button.configure(state="normal")
                    self.retry_button.grid()
                    if not self.messages or self.messages[-1].get("role") != "user" or \
                            self.messages[-1].get("content") != self.latest_question:
                        self.messages.append({"role": "user", "content": self.latest_question})
                    self.retry_payload = list(self.messages)
                    self.status.set("Dockt could not answer. Check that the local service is running.")
                    self._render(self.messages, "I couldn't reach Dockt. Check that it is running and try again.")
                elif kind == "history":
                    cid, row = value
                    self.history_loading = False
                    if (not self.busy and row and cid == self.conversation_id and
                            row["ts"] > self.history_ts):
                        self.history_ts = row["ts"]
                        self.messages = row["messages"]
                        self._render(self.messages)
                elif kind == "thumbnail":
                    capture_id, image = value
                    self._add_thumbnail(capture_id, image)
                elif kind == "answer":
                    question, answer, result = value
                    self.history_ts = result.get("conversation_updated_at", self.history_ts)
                    if not self.messages or self.messages[-1].get("role") != "user" or \
                            self.messages[-1].get("content") != question:
                        self.messages.append({"role": "user", "content": question})
                    self.messages.append(
                        {"role": "assistant", "content": answer, "sources": result.get("sources", []),
                         "cited": result.get("cited", []), "ms": result.get("ms", 0)})
                    self.busy = False
                    self.ask_button.configure(state="normal")
                    self.retry_button.grid_remove()
                    self.retry_payload = None
                    self.status.set(f"Answered in {result.get('ms', 0)} ms | Esc hides this window")
                    self._render(self.messages)
                    self._clear_thumbnails()
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def close(self):
        if self.hotkey:
            self.hotkey.stop()
        try:
            with urlopen(f"http://127.0.0.1:{DESKTOP_CONTROL_PORT}/quit", timeout=1):
                pass
        except Exception:
            pass  # The dashboard may already be closed (for example, during shutdown).
        self.root.destroy()


def main():
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    app = QuickChat(root)
    root.mainloop()


if __name__ == "__main__":
    main()
