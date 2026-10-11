"""Real Tk widget regression tests. No screen capture or OS input injection.

The test creates its own offscreen windows and sends Tk events to its own widgets.
It exercises geometry and Text bindings that mocks cannot verify.
"""
import sys
import time
import tkinter as tk
import customtkinter as ctk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import quickchat


class QuickChatWidgetTests(unittest.TestCase):
    @staticmethod
    def layout(widget):
        return [(str(child), child.winfo_y(), child.winfo_height(), child.winfo_reqheight(),
                 QuickChatWidgetTests.layout(child)) for child in widget.winfo_children()]
    @staticmethod
    def destroy(root):
        try:
            for job in root.tk.call("after", "info"):
                root.after_cancel(job)
            root.destroy()
        except tk.TclError:
            pass

    @classmethod
    def setUpClass(cls):
        quickchat._enable_windows_dpi_awareness()

    def make_app(self, scaling, pet_dpi=False):
        ctk.ScalingTracker.window_widgets_dict.clear()
        ctk.ScalingTracker.window_dpi_scaling_dict.clear()
        ctk.ScalingTracker.deactivate_automatic_dpi_awareness = True
        factor = 1.0 if pet_dpi else max(1.0, scaling * 72 / 96)
        ctk.set_widget_scaling(factor)
        ctk.set_window_scaling(factor)
        root = tk.Tk()
        root.withdraw()
        root.tk.call("tk", "scaling", scaling if pet_dpi else 96/72)
        self.addCleanup(self.destroy, root)
        with patch.object(quickchat.HotkeyListener, "start"):
            app = quickchat.QuickChat(root)
        app.pet.withdraw()
        # Keep this test's widgets offscreen; all input is via Tk's own event API.
        app.chat.geometry(f"{round(560*app.ui_scale)}x{round(440*app.ui_scale)}+10000+10000")
        with patch.object(app, "_focus_entry"), patch.object(app, "_focus_entry_if_unfocused"):
            app.chat.deiconify()
            root.update()
        return root, app

    def test_composer_is_visible_at_100_150_and_200_percent_scaling(self):
        for scaling in (96/72, 144/72, 192/72):
            with self.subTest(scaling=scaling):
                root, app = self.make_app(scaling)
                self.assertEqual(str(app.entry.cget("state")), "normal")
                self.assertTrue(app.entry.winfo_ismapped(), "Composer was clipped out by transcript")
                self.assertGreater(app.entry.winfo_height(), 20)
                self.assertGreater(app.entry.winfo_width(), 150)
                self.assertTrue(app.ask_button.winfo_ismapped())
                self.assertLess(app.entry.winfo_rooty() + app.entry.winfo_height(),
                                app.chat.winfo_rooty() + app.chat.winfo_height(),
                                f"entry={app.entry.winfo_rooty()}+{app.entry.winfo_height()}, "
                                f"chat={app.chat.winfo_rooty()}+{app.chat.winfo_height()}, "
                                f"children={self.layout(app.chat)}, "
                                f"content={self.layout(app.chat.winfo_children()[0])}")
                self.destroy(root)

    def test_real_text_bindings_type_multiline_submit_and_display_the_reply(self):
        root, app = self.make_app(144/72)
        app.entry.focus_force()
        root.update()
        self.assertFalse(app.placeholder_visible)
        for key in ("h", "i"):
            app.entry._textbox.event_generate("<KeyPress>", keysym=key)
        app.entry._textbox.event_generate("<Shift-KeyPress-Return>")
        app.entry._textbox.event_generate("<KeyPress>", keysym="a")
        root.update()
        self.assertEqual(app.entry.get("1.0", "end-1c"), "hi\na")
        answer = {"answer": "Saved reply", "sources": [], "cited": [], "ms": 1}
        with patch.object(quickchat, "chat_request", return_value=answer) as request:
            app.entry._textbox.event_generate("<KeyPress-Return>")
            deadline = time.monotonic() + 2
            while app.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            request.assert_called_once_with([{"role": "user", "content": "hi\na"}], app.conversation_id)
        self.assertFalse(app.busy)
        self.assertIn("Saved reply", app.transcript.get("1.0", "end"))
        self.assertEqual(app.entry.get("1.0", "end-1c"), "")
        self.assertEqual(app.chat.state(), "normal")
        app.hide()
        root.update()
        self.assertEqual(app.chat.state(), "withdrawn")

    def test_compact_window_at_200_percent_does_not_clip_the_composer(self):
        root, app = self.make_app(192/72, pet_dpi=True)
        app.chat.minsize(480, 360)
        app.chat.geometry("560x420+10000+10000")
        root.update()
        self.assertTrue(app.entry.winfo_ismapped())
        self.assertGreater(app.entry.winfo_height(), 20)
        self.assertGreater(app.entry.winfo_width(), 100)

    def test_pet_has_no_wordmark_and_its_drawing_fits_at_high_dpi(self):
        root, app = self.make_app(192/72)
        app._draw_pet()
        self.assertFalse(any(app.pet_canvas.type(item) == "text" for item in app.pet_canvas.find_all()))
        bounds = app.pet_canvas.bbox("all")
        self.assertGreaterEqual(bounds[0], 0)
        self.assertGreaterEqual(bounds[1], 0)
        self.assertLessEqual(bounds[2], 160*app.ui_scale)
        self.assertLessEqual(bounds[3], 140*app.ui_scale)

    def test_pet_hint_stays_open_across_pointer_handoff_and_actions_are_clickable(self):
        root, app = self.make_app(96/72)
        app._pet_hint(True)
        root.update()
        hint = app.hint_panel
        rows = hint.winfo_children()
        self.assertEqual(len(rows), 2)

        app._schedule_pet_hint_hide()
        pending_hide = app._hint_hide_job
        app._pet_hint(True)  # entering the tooltip cancels the pet's delayed leave
        self.assertIsNone(app._hint_hide_job)
        self.assertEqual(app.hint_panel, hint)
        self.assertIsNotNone(pending_hide)

        with patch.object(app, "show") as show:
            rows[0].event_generate("<Button-1>")
            root.update()
            show.assert_called_once_with()

        with patch.object(app, "_show_options_at") as show_options:
            rows[1].event_generate("<Button-1>", x=8, y=8)
            root.update()
            show_options.assert_called_once()
