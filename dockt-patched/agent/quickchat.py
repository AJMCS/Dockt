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
COMPOSER_PLACEHOLDER = "Ask anything you saw: where's dinner tonight? find invoice 4471…"
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
CITATION = re.compile(r"\[#(\d+)\]")
MARKDOWN_TOKEN = re.compile(r"(\[#(\d+)\]|\*\*[^*]+\*\*|__.+?__|\*[^*\n]+\*|_[^_\n]+_|`[^`]+`)")
CAPTURE_PAUSE_FILE = Path.home() / ".dockt_pause"
# The dashboard's type: serif answers (--serif falls back to Georgia on Windows), Segoe UI for chrome.
SERIF = ("Georgia", 12)
SANS = ("Segoe UI", 11)
SANS_SMALL = ("Segoe UI", 10)
# the theme the dashboard saves: <DATA_DIR>/theme, same default as server/memory.py; absent = follow the system
THEME_FILE = Path(os.environ.get("DATA_DIR", str(Path.home() / ".dockt" / "data"))).resolve() / "theme"
PALETTES = {
    "light": {
        "bg": "#FAF9F5", "surface": "#FFFFFF", "surface2": "#F5F4EE",
        "recess": "#F0EEE6", "line": "#E8E6DC", "text": "#1F1E1D",
        "muted": "#6B6A65", "accent": "#76B900", "accent_text": "#4D7C0F",
        "accent_soft": "#E8F0D3", "on_accent": "#1F1E1D", "error": "#B91C1C",
        "cite_text": "#3F6212",
    },
    "dark": {
        "bg": "#262624", "surface": "#30302E", "surface2": "#1F1E1D",
        "recess": "#1F1E1D", "line": "#3E3E3A", "text": "#F5F4EE",
        "muted": "#A3A29B", "accent": "#76B900", "accent_text": "#A3E635",
        "accent_soft": "#2D3B22", "on_accent": "#1F1E1D", "error": "#F28B82",
        "cite_text": "#A3E635",
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


def _saved_theme() -> str | None:
    try:
        theme = THEME_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return theme if theme in PALETTES else None


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


def _apply_windows_caption_theme(chat_hwnd: int, theme: str, dwmapi=None, user32=None) -> bool:
    """Tint the native Windows 11 title bar to match the selected Dockt theme."""
    if os.name != "nt" and (dwmapi is None or user32 is None):
        return False
    try:
        user32 = user32 or ctypes.windll.user32
        dwmapi = dwmapi or ctypes.windll.dwmapi
        root = user32.GetAncestor(wintypes.HWND(chat_hwnd), 2) or wintypes.HWND(chat_hwnd)
        colors = PALETTES[theme]

        def colorref(value: str):
            red, green, blue = (int(value[index:index + 2], 16)
                                for index in (1, 3, 5))
            return ctypes.c_uint32(red | (green << 8) | (blue << 16))

        def set_attribute(attribute, value):
            return dwmapi.DwmSetWindowAttribute(
                root, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0

        caption = colorref(colors["bg"])
        title_text = colorref(colors["text"])
        dark = ctypes.c_int(1 if theme == "dark" else 0)
        results = [set_attribute(DWMWA_CAPTION_COLOR, caption),
                   set_attribute(DWMWA_TEXT_COLOR, title_text),
                   set_attribute(DWMWA_USE_IMMERSIVE_DARK_MODE, dark)]
        return any(results)
    except (AttributeError, OSError, TypeError, ValueError):
        # DWM caption colors require Windows 11 22000 or newer.
        return False


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


def _send_button_image(size: int, state: str = "normal") -> Image.Image:
    """The dashboard's send button: a green circle with its up-arrow icon
    (M12 19V5 M5.5 11.5 12 5l6.5 6.5, 2.2 stroke, round caps), 18 of 36 px."""
    from PIL import ImageDraw
    big = size * 8  # draw large, then downsample for smooth edges
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    fill = PALETTES["light"]["accent"] if state != "hover" else "#6AA600"   # same in both themes
    draw.ellipse((0, 0, big - 1, big - 1), fill=fill)
    unit = big / 36 * 18 / 24          # icon units -> pixels: an 18 px icon in a 36 px circle
    offset = big / 36 * 9              # centre the 18 px icon
    width = round(2.2 * unit)
    ink = PALETTES["light"]["on_accent"]
    for line in ([(12, 19), (12, 5)], [(5.5, 11.5), (12, 5), (18.5, 11.5)]):
        line = [(offset + x * unit, offset + y * unit) for x, y in line]
        draw.line(line, fill=ink, width=width, joint="curve")
        for x, y in line:  # round caps
            draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=ink)
    image = image.resize((size, size), Image.Resampling.LANCZOS)
    if state == "disabled":  # the dashboard dims it to 40% while an answer is pending
        image.putalpha(image.getchannel("A").point(lambda alpha: alpha * 2 // 5))
    return image


PET_BOX = 100   # the robot is drawn in a 100 x 100 design box
PET_SIZE = 90   # ...and shown this many logical pixels square in the 160 x 140 pet window


def _pet_image(size: int, blink: bool = False, key: tuple | None = None,
               squash: float = 1.0) -> Image.Image:
    """Dockt's mascot: a chibi Astro Bot-style robot with a round white helmet, glossy visor,
    big sparkly lime eyes and a waving arm. `squash` < 1 flattens it (and > 1 stretches it)
    on its feet for a bouncy, cartoony bob. With `key`, edge pixels are flattened onto the
    dark outline so Windows' transparent-colour keying leaves no pink fringe."""
    from PIL import ImageChops, ImageDraw, ImageFilter
    ss = 4                                   # supersample, then downsample for smooth curves
    px = size * ss
    u = px / PET_BOX                         # design units -> pixels

    def box(x1, y1, x2, y2):
        return (x1 * u, y1 * u, x2 * u, y2 * u)

    ink, white, shade = "#1F1E1D", "#FFFFFF", "#E2E0D6"
    lime, glow_lime = "#A3E635", (163, 230, 53)
    ow = max(1, round(3.6 * u))              # chunky cartoon outline
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def shape(kind, xy, fill, radius=0, target=None):
        d = ImageDraw.Draw(target) if target is not None else draw
        if kind == "ellipse":
            d.ellipse(box(*xy), fill=fill, outline=ink, width=ow)
        else:
            d.rounded_rectangle(box(*xy), radius=radius * u, fill=fill, outline=ink, width=ow)

    def mask(kind, xy, radius=0):
        m = Image.new("L", (px, px), 0)
        d = ImageDraw.Draw(m)
        if kind == "ellipse":
            d.ellipse(box(*xy), fill=255)
        else:
            d.rounded_rectangle(box(*xy), radius=radius * u, fill=255)
        return m

    def tint(color, alpha_mask):
        image.paste(Image.new("RGBA", (px, px), color), (0, 0), alpha_mask)

    def limb(xy, angle, pivot):
        layer = Image.new("RGBA", (px, px), (0, 0, 0, 0))
        shape("ellipse", xy, white, target=layer)
        image.alpha_composite(layer.rotate(angle, resample=Image.Resampling.BICUBIC,
                                           center=(pivot[0] * u, pivot[1] * u)))

    # boots, arms (one waving), tiny body: back to front
    shape("ellipse", (35, 85, 50, 97), white)
    shape("ellipse", (50, 85, 65, 97), white)
    limb((32, 72, 43, 86), -50, (37.5, 73))         # arm out to the side
    limb((57, 72, 68, 86), 50, (62.5, 73))          # and its twin on the other side
    shape("rect", (35, 68, 65, 92), white, 12)
    tint(shade, ImageChops.subtract(mask("rect", (37, 70, 63, 90), 10),
                                    mask("ellipse", (28, 56, 72, 86))))
    draw.rounded_rectangle(box(35, 68, 65, 92), radius=12 * u, outline=ink, width=ow)
    draw.ellipse(box(46.5, 77, 53.5, 84), fill=lime, outline=ink,
                 width=max(1, round(2 * u)))                       # chest light
    draw.ellipse(box(48, 78.5, 50.2, 80.7), fill="#F7FEE7")
    # bendy antenna behind the helmet
    stem = [(50 * u, 18 * u), (52 * u, 11 * u), (56 * u, 5 * u)]
    draw.line(stem, fill=ink, width=max(1, round(6 * u)), joint="curve")
    draw.line(stem, fill="#D6D4CB", width=max(1, round(2.4 * u)), joint="curve")
    # ear pods
    shape("ellipse", (2, 33, 17, 55), lime)
    shape("ellipse", (83, 33, 98, 55), lime)
    draw.ellipse(box(6, 37, 10, 43), fill="#F7FEE7")
    # big round helmet with a soft shade along the bottom
    shape("rect", (8, 12, 92, 74), white, 31)
    tint(shade, ImageChops.subtract(mask("rect", (10, 14, 90, 72), 29),
                                    mask("ellipse", (2, -2, 98, 66))))
    draw.rounded_rectangle(box(8, 12, 92, 74), radius=31 * u, outline=ink, width=ow)
    # visor, its glow, eyes, smile, cheeks and gloss
    shape("rect", (16, 22, 84, 65), "#141614", 21)
    visor = mask("rect", (18, 24, 82, 63), 19)
    eyes = ((36, 43), (64, 43))
    glow = Image.new("L", (px, px), 0)
    gd = ImageDraw.Draw(glow)
    for x, y in eyes:
        gd.ellipse(box(x - 11, y - 12, x + 11, y + 12), fill=130)
    tint(glow_lime, ImageChops.multiply(glow.filter(ImageFilter.GaussianBlur(5 * u)), visor))
    for x, y in eyes:
        if blink:                            # happy closed eyes: upside-down U arcs
            draw.arc(box(x - 7, y - 4, x + 7, y + 8), 200, 340, fill=lime,
                     width=max(1, round(3.6 * u)))
        else:
            draw.ellipse(box(x - 7, y - 9.5, x + 7, y + 9.5), fill=lime)
            draw.ellipse(box(x - 4.8, y - 7, x - 0.4, y - 2.6), fill="#FFFFFF")   # big sparkle
            draw.ellipse(box(x + 1.6, y + 3, x + 3.8, y + 5.2), fill="#F7FEE7")   # small sparkle
    draw.arc(box(46, 51, 54, 58), 20, 160, fill=lime, width=max(1, round(2.4 * u)))   # smile
    blush = Image.new("L", (px, px), 0)
    bd = ImageDraw.Draw(blush)
    for x, y in ((25, 55), (75, 55)):
        bd.ellipse(box(x - 5.5, y - 3, x + 5.5, y + 3), fill=150)
    tint("#FF8FA3", ImageChops.multiply(blush, visor))
    gloss = Image.new("L", (px, px), 0)
    gl = ImageDraw.Draw(gloss)
    gl.polygon([(20 * u, 38 * u), (31 * u, 25 * u), (40 * u, 25 * u), (22 * u, 46 * u)], fill=70)
    gl.polygon([(43 * u, 25 * u), (47 * u, 25 * u), (27 * u, 50 * u), (24 * u, 50 * u)], fill=45)
    tint("#FFFFFF", ImageChops.multiply(gloss, visor))
    # glowing antenna ball with a sparkle
    shape("ellipse", (50.5, 1.5, 62.5, 13.5), lime)
    draw.ellipse(box(53.5, 4, 57.5, 8), fill="#FFFFFF")

    image = image.resize((size, size), Image.Resampling.LANCZOS)
    if squash != 1.0:                        # squash/stretch with the feet planted
        w, h = round(size / squash ** 0.5), round(size * squash)
        stretched = image.resize((w, h), Image.Resampling.LANCZOS)
        left, top = max(0, (w - size) // 2), max(0, h - size)
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        image.alpha_composite(stretched.crop((left, top, left + min(w, size), h)),
                              (max(0, (size - w) // 2), max(0, size - h)))
    if key is not None:
        alpha = image.getchannel("A")
        solid = Image.new("RGBA", image.size, ink)
        solid.alpha_composite(image)
        image = solid
        image.putalpha(alpha.point(lambda a: 255 if a >= 128 else 0))
    return image


class SendButton(tk.Label):
    """A round send button. CTkButton pads its image sideways, which turns a circle into a pill."""

    def __init__(self, master, command, scale: float, bg: str):
        size = round(36 * scale)
        self._images = {state: ImageTk.PhotoImage(_send_button_image(size, state), master=master)
                        for state in ("normal", "hover", "disabled")}
        super().__init__(master, image=self._images["normal"], bg=bg, bd=0,
                         highlightthickness=0, cursor="hand2")
        self._command = command
        self._state = "normal"
        self._hover = False
        self.bind("<Enter>", lambda _event: self._set_hover(True))
        self.bind("<Leave>", lambda _event: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)

    def _set_hover(self, hover: bool):
        self._hover = hover
        self._refresh()

    def _click(self, _event=None):
        if self._state != "disabled" and self._hover:
            self._command()

    def _refresh(self):
        state = "disabled" if self._state == "disabled" else ("hover" if self._hover else "normal")
        super().configure(image=self._images[state],
                          cursor="watch" if state == "disabled" else "hand2")

    def configure(self, cnf=None, **kwargs):
        state = kwargs.pop("state", None)
        if state is not None:
            self._state = str(state)
            self._refresh()
        if cnf or kwargs:
            return super().configure(cnf, **kwargs)

    config = configure

    def cget(self, key):
        return self._state if key == "state" else super().cget(key)


class ChatTranscript(ctk.CTkScrollableFrame):
    """Compact native chat thread with dashboard-style aligned message bubbles."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.rendered_text: list[str] = []
        self.text_labels: list[tuple[str, tk.Widget]] = []
        self.source_buttons: dict[int, ctk.CTkButton] = {}
        self.markdown_widgets: list[tk.Text] = []

    def clear(self):
        for child in self.winfo_children():
            child.destroy()
        self.rendered_text.clear()
        self.text_labels.clear()
        self.source_buttons.clear()
        self.markdown_widgets.clear()

    def add_message(self, role: str, text: str, colors: dict, citations=(), muted=False,
                    show_no_sources=False):
        self.rendered_text.append(text)
        row = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        row.pack(fill="x", padx=8, pady=(0, 22))
        wrap = max(160, int(self.winfo_width() * (0.78 if role == "user" else 0.94)))
        if role == "user":
            bubble = ctk.CTkFrame(row, fg_color=colors["surface2"],
                                  corner_radius=16, border_width=1,
                                  border_color=colors["line"])
            bubble.pack(side="right", anchor="e")
            label = ctk.CTkLabel(bubble, text=text, text_color=colors["text"],
                                 font=("Segoe UI", 11), justify="left", anchor="w",
                                 wraplength=wrap)
            label.pack(padx=16, pady=10)
        else:
            # width=1: let pack size it; Tk's default 80-column request widens the thread.
            label = tk.Text(row, height=1, width=1, wrap="word", relief="flat", bd=0,
                            highlightthickness=0, padx=0, pady=0, takefocus=False,
                            cursor="arrow", background=colors["bg"],
                            foreground=colors["muted"] if muted else colors["text"],
                            font=SERIF, spacing1=0, spacing2=4, spacing3=6)
            label.pack(side="top", fill="x", anchor="w")
            self._insert_markdown(label, text, colors, muted)
            label.configure(state="disabled")
            label.bind("<Configure>", lambda _event, widget=label: self._fit_height(widget), add="+")
            self.markdown_widgets.append(label)
            if citations or show_no_sources:
                chips = ctk.CTkFrame(row, fg_color="transparent")
                chips.pack(side="top", fill="x", anchor="w", pady=(6, 0))
                if show_no_sources and not citations:
                    ctk.CTkLabel(chips, text="No sources cited.",
                                 text_color=colors["muted"],
                                 font=("Segoe UI", 10)).pack(side="left")
                for capture_id, action in citations:
                    button = ctk.CTkButton(
                        chips, text=f"#{capture_id}", width=44, height=30,
                        corner_radius=9, border_width=1, border_color=colors["line"],
                        fg_color=colors["surface"], hover_color=colors["surface2"],
                        text_color=colors["text"], font=SANS_SMALL, command=action)
                    button.pack(side="left", padx=(0, 6))
                    self.source_buttons[capture_id] = button
        self.text_labels.append((role, label))
        self.update_idletasks()
        if role != "user":
            self._fit_height(label)
        self.update_idletasks()
        self._parent_canvas.yview_moveto(1.0)
        # Source chips finish laying out after the answer grows; scroll again once they have.
        self.after_idle(lambda: self._parent_canvas.yview_moveto(1.0))

    @staticmethod
    def _fit_height(widget: tk.Text):
        """Grow the answer to its wrapped line count; a Text otherwise shows only its first line."""
        if getattr(widget, "_fitting", False):
            return
        widget._fitting = True
        try:
            # Start from the wrapped line count, then add lines until nothing scrolls:
            # paragraph spacing and bold runs make lines taller than the base font.
            lines = widget.count("1.0", "end-1c", "displaylines") or 1
            lines = max(1, lines[0] if isinstance(lines, tuple) else lines)
            widget.configure(height=lines)
            widget.update_idletasks()
            while widget.yview()[1] < 1.0 and lines < 400:
                lines += 1
                widget.configure(height=lines)
                widget.update_idletasks()
        except tk.TclError:
            pass
        finally:
            widget._fitting = False

    @staticmethod
    def _insert_markdown(widget: tk.Text, text: str, colors: dict, muted=False):
        widget.tag_configure("bold", font=SERIF + ("bold",))
        widget.tag_configure("italic", font=SERIF + ("italic",))
        widget.tag_configure("code", font=("Consolas", 10),
                             foreground=colors["accent_text"],
                             background=colors["surface2"])
        widget.tag_configure("heading", font=("Segoe UI", 13, "bold"),
                             foreground=colors["muted"] if muted else colors["text"])
        widget.tag_bind("citation", "<Enter>", lambda _event: widget.configure(cursor="hand2"))
        widget.tag_bind("citation", "<Leave>", lambda _event: widget.configure(cursor="arrow"))
        for line in text.splitlines() or [""]:
            heading = re.match(r"^#{1,3}\s+", line)
            if heading:
                widget.insert("end", line[heading.end():] + "\n", "heading")
                continue
            bullet = re.match(r"^\s*[-*]\s+", line)
            if bullet:
                widget.insert("end", "• ")
                line = line[bullet.end():]
            cursor = 0
            for match in MARKDOWN_TOKEN.finditer(line):
                widget.insert("end", line[cursor:match.start()])
                token = match.group(0)
                if token.startswith("[#"):
                    capture_id = int(match.group(2))
                    tag = f"citation_{capture_id}"
                    widget.tag_configure(tag, foreground=colors["cite_text"],
                                         background=colors["accent_soft"],
                                         font=("Segoe UI", 10, "bold"))
                    widget.tag_bind(tag, "<Button-1>",
                                    lambda _event, cid=capture_id:
                                    open_dockt(f"/#/capture/{cid}"))
                    widget.insert("end", f"#{capture_id}", ("citation", tag))
                elif token.startswith("**") or token.startswith("__"):
                    widget.insert("end", token[2:-2], "bold")
                elif token.startswith("*") or token.startswith("_"):
                    widget.insert("end", token[1:-1], "italic")
                else:
                    widget.insert("end", token[1:-1], "code")
                cursor = match.end()
            widget.insert("end", line[cursor:] + "\n")
        widget.delete("end-2c")  # the last line's newline would add a blank line under every answer

    def add_thumbnail(self, capture_id: int, image: Image.Image, colors: dict):
        button = self.source_buttons.get(capture_id)
        if button is None:
            return
        image = image.copy()
        image.thumbnail((46, 29), Image.Resampling.LANCZOS)
        thumbnail = ImageTk.PhotoImage(image, master=button)
        button.configure(image=thumbnail, text=f"#{capture_id}", compound="left",
                         width=110, height=36, corner_radius=9, border_width=1,
                         border_color=colors["line"], fg_color=colors["surface"],
                         hover_color=colors["surface2"], text_color=colors["text"])
        button.image = thumbnail

    def update_theme(self, colors: dict):
        for widget in self.markdown_widgets:
            try:
                widget.configure(background=colors["bg"], foreground=colors["text"])
                widget.tag_configure("code", foreground=colors["accent_text"],
                                     background=colors["surface2"])
                widget.tag_configure("heading", foreground=colors["text"])
                for tag in widget.tag_names():
                    if tag.startswith("citation_"):
                        widget.tag_configure(tag, foreground=colors["cite_text"],
                                             background=colors["accent_soft"])
            except tk.TclError:
                pass

    def get(self, _start="1.0", _end="end"):
        """Keep the small read-only compatibility surface used by widget checks."""
        return "\n".join(self.rendered_text) + ("\n" if self.rendered_text else "")


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
        self.theme_name = _saved_theme() or ("dark" if _system_dark_mode() else "light")
        self.colors = PALETTES[self.theme_name]
        self.chat = ctk.CTkToplevel(self.root, fg_color=self.colors["bg"])
        self.chat.title("Dockt Quick Chat")
        self.chat.geometry("560x480")
        self.chat.minsize(360, 280)
        self.chat.attributes("-topmost", False)
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
        self._composer_resize_job = None
        self._style()
        self._build()
        _apply_windows_caption_theme(self.chat.winfo_id(), self.theme_name)
        self.chat.bind("<Configure>", self._schedule_responsive_layout, add="+")
        self._create_pet()
        self.root.after(80, self._drain_events)
        self.root.after(1000, self._poll_shared_history)
        self.root.after(800, self._poll_theme)

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

    @staticmethod
    def _recolor_tree(widget, previous, current_palette):
        color_options = {
            "fg_color": ("bg", "surface", "surface2", "line", "accent", "accent_soft"),
            "bg_color": ("bg", "surface", "surface2", "line"),
            "text_color": ("text", "muted", "accent_text", "on_accent", "error"),
            "border_color": ("line", "accent", "surface", "surface2"),
            "hover_color": ("line", "surface2", "accent_soft", "accent"),
            "button_color": ("surface2", "line", "accent"),
            "button_hover_color": ("accent", "accent_soft", "line"),
            "bg": ("bg", "surface", "surface2", "accent_soft"),
            "fg": ("text", "muted", "accent_text", "error"),
            "insertbackground": ("text",),
        }
        try:
            for option, keys in color_options.items():
                try:
                    current = widget.cget(option)
                except (tk.TclError, AttributeError, ValueError):
                    continue
                replacement = next((current_palette[key] for key in keys
                                    if previous[key] == current), None)
                if replacement is not None:
                    try:
                        widget.configure(**{option: replacement})
                    except (tk.TclError, TypeError):
                        pass
            children = widget.winfo_children()
        except tk.TclError:
            return
        for child in children:
            QuickChat._recolor_tree(child, previous, current_palette)

    def _apply_theme(self, theme: str):
        if theme not in PALETTES or theme == self.theme_name:
            return
        previous = self.colors
        self.theme_name = theme
        self.colors = PALETTES[theme]
        self._style()
        # Quick Chat, the pet, and its context menu are separate Tk toplevels;
        # recoloring only the hidden root leaves their CustomTk widgets stale.
        for window in (self.chat, getattr(self, "pet", None),
                       getattr(self, "options_panel", None)):
            if window is not None:
                self._recolor_tree(window, previous, self.colors)
        self.transcript.configure(scrollbar_button_color=self.colors["surface2"],
                                  scrollbar_button_hover_color=self.colors["accent"])
        self.entry._textbox.configure(insertbackground=self.colors["text"])
        self.entry._textbox.tag_configure("placeholder", foreground=self.colors["muted"])
        self.transcript.update_theme(self.colors)
        _apply_windows_caption_theme(self.chat.winfo_id(), self.theme_name)

    def _poll_theme(self):
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        theme = _saved_theme()
        if theme:
            self._apply_theme(theme)
        self.root.after(800, self._poll_theme)

    def _build(self):
        frame = ctk.CTkFrame(self.chat, fg_color=self.colors["bg"], corner_radius=22)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        content = ctk.CTkFrame(frame, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=14, pady=12)
        frame = content
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        header = ctk.CTkFrame(frame, fg_color="transparent", height=36)
        header.grid(row=0, column=0, sticky="ew")
        brand = ctk.CTkFrame(header, fg_color="transparent", height=1)
        brand.pack(side="left")
        ctk.CTkLabel(brand, text="Dockt", text_color=self.colors["text"],
                     font=("Segoe UI", 15, "bold")).pack(side="left")
        ctk.CTkLabel(brand, text=".", text_color=self.colors["accent"],
                     font=("Segoe UI", 15, "bold")).pack(side="left")
        # The dashboard's outlined "Powered by local AI" pill.
        badge = ctk.CTkFrame(header, fg_color="transparent", corner_radius=11,
                             border_width=1, border_color=self.colors["accent"])
        badge.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(badge, text="● Powered by local AI", text_color=self.colors["accent_text"],
                     font=("Segoe UI", 9, "bold"), height=18).pack(padx=9, pady=2)
        # Quiet top-right actions, like the dashboard's Sources / Promises / Timeline.
        self.header_buttons = []
        for label, command in (("Hide", self.hide), ("Open dashboard ↗", self.open_in_dockt)):
            button = ctk.CTkButton(
                header, text=label, command=command, width=0, height=28,
                corner_radius=10, fg_color="transparent", hover_color=self.colors["surface2"],
                text_color=self.colors["muted"], font=SANS_SMALL)
            button.pack(side="right", padx=(4, 0))
            self.header_buttons.append(button)

        transcript_card = ctk.CTkFrame(
            frame, fg_color="transparent", corner_radius=0, height=1)
        transcript_card.grid(row=1, column=0, sticky="nsew", pady=(10, 10))
        self.transcript = ChatTranscript(
            transcript_card, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=self.colors["surface2"],
            scrollbar_button_hover_color=self.colors["accent"])
        self.transcript.pack(fill="both", expand=True)
        self.transcript.bind("<Configure>", self._resize_message_margins, add="+")

        composer = ctk.CTkFrame(frame, fg_color=self.colors["surface"], height=1,
                                corner_radius=22, border_width=1,
                                border_color=self.colors["line"])
        composer.grid(row=2, column=0, sticky="ew")
        composer.columnconfigure(0, weight=1)
        self.composer = composer
        self.entry = ctk.CTkTextbox(composer, height=44, wrap="word", activate_scrollbars=False,
                                     corner_radius=0, border_width=0,
                                     border_color=self.colors["line"], fg_color="transparent",
                                     text_color=self.colors["text"], font=SANS,
                                     undo=True)
        self.entry._textbox.configure(insertbackground=self.colors["text"], padx=8, pady=10)
        self.entry._textbox.tag_configure("placeholder", foreground=self.colors["muted"])
        self.placeholder_visible = True
        self.entry.insert("1.0", COMPOSER_PLACEHOLDER, "placeholder")
        self.entry.edit_modified(False)
        self.entry.grid(row=0, column=0, columnspan=3, sticky="ew",
                        padx=(10, 10), pady=(4, 0))
        self.entry.bind("<Return>", self.submit)
        self.entry.bind("<Shift-Return>", lambda _event: None)
        self.entry.bind("<<Modified>>", self._composer_modified, add="+")
        self.entry.bind("<FocusIn>", self._clear_composer_placeholder, add="+")
        self.entry.bind("<FocusOut>", self._show_composer_placeholder, add="+")
        # The dashboard outlines the composer in green while you type.
        self.entry.bind("<FocusIn>", lambda _event: composer.configure(
            border_color=self.colors["accent"]), add="+")
        self.entry.bind("<FocusOut>", lambda _event: composer.configure(
            border_color=self.colors["line"]), add="+")
        self.entry.bind("<Button-1>", self._entry_clicked, add="+")
        self.ask_button = SendButton(composer, self.submit,
                                     ctk.ScalingTracker.get_widget_scaling(composer),
                                     self.colors["surface"])
        # Status sits where the dashboard shows its device picker: quiet, inside the composer.
        self.status = tk.StringVar(value="")
        self.status_label = ctk.CTkLabel(
            composer, textvariable=self.status, text_color=self.colors["muted"],
            font=SANS_SMALL, wraplength=300, justify="left", anchor="w")
        self.status_label.grid(row=1, column=0, sticky="w", padx=(18, 0), pady=(0, 8))
        self.ask_button.grid(row=1, column=2, padx=(4, 8), pady=(0, 8), sticky="e")
        self.retry_button = ctk.CTkButton(
            composer, text="Retry", command=self.retry_latest, width=64, height=30,
            corner_radius=10, fg_color="transparent", border_width=1,
            border_color=self.colors["line"], hover_color=self.colors["surface2"],
            text_color=self.colors["text"], font=SANS_SMALL)
        self.retry_button.grid(row=1, column=1, padx=(0, 2), pady=(0, 8), sticky="e")
        self.retry_button.grid_remove()
        self._layout_composer()

    def _schedule_responsive_layout(self, event=None):
        if event is not None and event.widget is not self.chat:
            return
        if getattr(self, "_responsive_layout_job", None) is None:
            self._responsive_layout_job = self.chat.after_idle(self._layout_composer)

    def _layout_composer(self):
        self._responsive_layout_job = None
        try:
            if not self.chat.winfo_exists():
                return
        except tk.TclError:
            return
        width = self.chat.winfo_width() / max(1.0, self._window_scaling())
        self.status_label.configure(wraplength=max(140, int(width - 150)))
        self._resize_composer()

    def _window_scaling(self):
        try:
            return max(1.0, float(ctk.ScalingTracker.get_window_scaling(self.chat)))
        except (AttributeError, KeyError, tk.TclError):
            return max(1.0, float(getattr(self, "ui_scale", 1.0)))

    def _resize_message_margins(self, _event=None):
        width = max(1, self.transcript.winfo_width())
        for role, label in self.transcript.text_labels:
            if role == "user":
                label.configure(wraplength=max(160, int(width * 0.78)))

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
        y = max(0, self.pet.winfo_screenheight() - height - round(88*self.ui_scale))
        self.pet.geometry(f"{width}x{height}+{x}+{y}")

    def _draw_pet(self):
        canvas = self.pet_canvas
        canvas.delete("all")
        bob = self.pet_bob

        # Astro Bot-style robot, rendered once per eye state at this screen's scale.
        blink = getattr(self, "pet_blink", False)
        squash = 0.94 if bob == 0 else 1.0    # squash when it lands, full height at the top of the hop
        photos = self.__dict__.setdefault("_pet_photos", {})
        if (blink, squash) not in photos:
            key = None if self.pet_background != "#ff00ff" else (255, 0, 255)
            photos[(blink, squash)] = ImageTk.PhotoImage(
                _pet_image(round(PET_SIZE * self.ui_scale), blink, key, squash), master=self.pet)
        # Top-centred, leaving the bottom of the window for the hover controls and tooltip.
        canvas.create_image((160 - PET_SIZE) / 2, 5 - bob, anchor="nw",
                            image=photos[(blink, squash)], tags=("pet-body",))
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
        self._rounded_rect(canvas, 43, 110, 117, 138, 12, fill="#252824",
                           outline="#454A43", width=1, tags=("pet-control-bar",))
        for name, center in (("chat", 64), ("menu", 96)):
            active = self.pet_hover_button == name
            fill = "#555A53" if active else "#252824"
            outline = "#555A53" if active else "#252824"
            self._rounded_rect(
                canvas, center - 12, 113, center + 12, 135, 6,
                fill=fill, outline=outline, width=1.2,
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
        if 52 <= x <= 76 and 112 <= y <= 136:
            return "chat"
        if 84 <= x <= 108 and 112 <= y <= 136:
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
        self.pet_bob = 3 if self.pet_bob == 0 else 0
        self.pet_ticks = getattr(self, "pet_ticks", 0) + 1
        if self.pet_ticks % 8 == 0:   # a quick happy blink every ~4 s
            self.pet_blink = True
            self.pet.after(180, self._end_blink)
        self._draw_pet()
        self.pet.after(520, self._animate_pet)

    def _end_blink(self):
        self.pet_blink = False
        if self.pet.winfo_exists():
            self._draw_pet()

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
        panel = ctk.CTkToplevel(self.pet, fg_color=self.colors["line"])
        self.options_panel = panel
        panel.overrideredirect(True)
        panel.attributes("-topmost", True)

        card = ctk.CTkFrame(panel, width=318, fg_color=self.colors["surface"],
                            corner_radius=18, border_width=1,
                            border_color=self.colors["line"])
        card.pack(fill="both", expand=True, padx=1, pady=1)
        header = ctk.CTkFrame(card, fg_color="transparent", height=48)
        header.pack(fill="x", padx=12, pady=(10, 6))
        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.pack(side="left", fill="y")
        ctk.CTkLabel(title_group, text="Dockt", text_color=self.colors["text"],
                     font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ctk.CTkLabel(title_group, text="Quick actions", text_color=self.colors["muted"],
                     font=("Segoe UI", 11)).pack(anchor="w")
        ctk.CTkButton(header, text="\u00d7", width=30, height=30, corner_radius=10,
                      fg_color="transparent", hover_color=self.colors["surface2"],
                      text_color=self.colors["muted"], font=("Segoe UI", 18),
                      command=self._close_options_panel).pack(side="right", anchor="n")

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=10, pady=(0, 8))
        self._add_option(actions, "Open quick chat", self.show, icon="\u25c9")
        self._add_option(actions, "Open dashboard", lambda: open_dockt(), icon="\u25a6")
        self._add_option(actions, "Hide Dockt pet", self.hide_pet, icon="\u25ce")
        self.capture_option = self._add_option(
            actions, self._capture_menu_label(), self._toggle_capture, icon="\u25cf")
        ctk.CTkFrame(card, height=1, fg_color=self.colors["line"],
                     corner_radius=0).pack(fill="x", padx=18, pady=4)
        self._add_option(card, "Quit Dockt", self.close, danger=True, icon="\u23fb")
        ctk.CTkFrame(card, height=5, fg_color="transparent").pack()

        panel.update_idletasks()
        width, height = panel.winfo_reqwidth(), panel.winfo_reqheight()
        screen_w, screen_h = panel.winfo_screenwidth(), panel.winfo_screenheight()
        x = min(x_root, screen_w - width - 8)
        y = y_root if y_root + height <= screen_h else y_root - height
        panel.geometry(f"+{max(8, x)}+{max(8, y)}")
        panel.bind("<Escape>", lambda _event: self._close_options_panel())
        panel.bind("<FocusOut>", self._options_focus_out, add="+")
        panel.focus_force()

    def _add_option(self, parent, label: str, action, danger: bool = False,
                    icon: str = ""):
        colors = self.colors
        button = ctk.CTkButton(
            parent, text=f"{icon}   {label}" if icon else label,
            anchor="w", height=46, corner_radius=12, border_width=0,
            fg_color="transparent", hover_color=colors["line"],
            text_color=colors["error"] if danger else colors["text"],
            font=("Segoe UI", 12, "bold" if danger else "normal"),
            command=lambda: self._run_option(action))
        button.pack(fill="x", padx=2, pady=2)
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
            self.capture_option.configure(text=f"\u25cf   {self._capture_menu_label()}")
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
        width = min(560, available_width)
        height = min(480, available_height)
        self.chat.minsize(min(360, width), min(280, height))
        pet_x, pet_y = self.pet.winfo_rootx(), self.pet.winfo_rooty()
        pet_width, pet_height = self.pet.winfo_width(), self.pet.winfo_height()
        gap = round(12 * scale)
        # The pet window is always-on-top; keep the chat close without covering it.
        x_px = pet_x + pet_width - round(width * scale)
        y_px = pet_y - round(height * scale) - gap
        if y_px < 8:
            y_px = pet_y + pet_height + gap
        x_px = min(max(8, x_px), max(8, screen_width - round(width * scale) - 8))
        y_px = min(max(8, y_px), max(8, screen_height - round(height * scale) - 8))
        x = round(x_px / scale)
        y = round(y_px / scale)
        self.chat.geometry(f"{width}x{height}+{x}+{y}")
        self.chat.deiconify()
        self.chat.lift()
        self.chat.attributes("-topmost", False)
        _apply_windows_caption_theme(self.chat.winfo_id(), self.theme_name)

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
        try:
            lines = self.entry._textbox.count("1.0", "end-1c", "displaylines") or (1,)
        except tk.TclError:
            return
        if hasattr(self, "chat"):
            scale = self._window_scaling()
            window_height = self.chat.winfo_height() / scale
            max_height = max(44, min(128, int(window_height - 260)))
        else:
            max_height = 128
        if getattr(self, "placeholder_visible", False):
            # Before the window is laid out the box is ~1 px wide and the placeholder wraps
            # into dozens of lines, which used to pin the composer at its maximum height.
            lines = (1,)
        desired_height = max(44, min(max_height, 34 * lines[0]))
        try:
            current_height = int(self.entry.cget("height"))
        except (TypeError, ValueError, tk.TclError):
            current_height = None
        if current_height != desired_height:
            self.entry.configure(height=desired_height)

    def _schedule_composer_resize(self):
        if not hasattr(self, "root"):
            self._resize_composer()
            return
        if getattr(self, "_composer_resize_job", None) is not None:
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
        display_messages = self.messages + [{"role": "user", "content": question}]
        if hasattr(self, "transcript"):
            self._render(display_messages, pending=True)
        else:
            self._render(display_messages)
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
        if hasattr(self, "transcript"):
            self._render(request_messages, pending=True)
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
            cited = result.get("cited")
            if cited is None:
                cited = [int(cid) for cid in CITATION.findall(answer)]
            ids = [int(cid) for cid in cited if str(cid).isdigit()]
            for cid in ids[:6]:
                image = load_thumbnail(cid)
                if image is not None:
                    self.events.put(("thumbnail", (cid, image)))
        except (TypeError, ValueError, KeyError, AttributeError):
            pass

    def _render(self, messages: list[dict], error: str = "", pending: bool = False):
        self.transcript.clear()
        for message in messages:
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = message.get("content", "")
            if "cited" in message:
                citation_ids = [int(cid) for cid in message.get("cited", [])
                                if str(cid).isdigit()]
            else:
                citation_ids = [int(cid) for cid in CITATION.findall(text)]
            citation_ids = list(dict.fromkeys(citation_ids))
            actions = [(cid, lambda capture_id=cid: open_dockt(f"/#/capture/{capture_id}"))
                       for cid in citation_ids]
            self.transcript.add_message(role, text, self.colors, actions,
                                        show_no_sources=(role == "assistant" and
                                                         "sources" in message and
                                                         not citation_ids))
        if error:
            self.transcript.add_message("assistant", error, self.colors, muted=True)
        elif pending:
            self.transcript.add_message("assistant", "Thinking…", self.colors, muted=True)

    def _clear_thumbnails(self):
        # Capture previews belong to transcript rows, which are cleared on rerender.
        pass

    def _add_thumbnail(self, cid: int, image: Image.Image):
        self.transcript.add_thumbnail(cid, image, self.colors)

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
                    self.status.set("Answered · Esc hides this window")
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
