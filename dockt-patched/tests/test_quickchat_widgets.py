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

    def test_quickchat_renders_dashboard_style_user_bubble_and_theme_updates_menu(self):
        root, app = self.make_app(96/72)
        app._render([{"role": "user", "content": "Where did I save the notes?"},
                     {"role": "assistant", "content": "They are in the project folder."}])
        self.assertEqual(app.transcript.get("1.0", "end-1c").splitlines(), [
            "Where did I save the notes?", "They are in the project folder."])
        row = app.transcript.winfo_children()[0]
        bubble = row.winfo_children()[0]
        self.assertEqual(bubble.cget("corner_radius"), 16)

        original_theme = app.theme_name
        target_theme = "dark" if original_theme == "light" else "light"
        app._show_options_at(200, 200)
        app._apply_theme(target_theme)
        self.assertEqual(app.options_panel.cget("fg_color"), quickchat._corner_key(app.colors))
        self.assertEqual(app.options_panel.winfo_children()[0].cget("fg_color"), app.colors["surface"])
        self.assertEqual(app.theme_name, target_theme)
        self.assertEqual(app.options_panel.winfo_children()[0].cget("width"), 318)
        self.assertIn("12", str(app.capture_option.cget("font")))

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

    def test_pet_click_does_not_open_chat_but_bottom_icon_buttons_do(self):
        root, app = self.make_app(96/72)
        app.pet.geometry("160x140+10000+10000")
        app.pet.deiconify()
        app._pet_hint(True)
        root.update()
        self.assertEqual(len(app.pet_canvas.find_withtag("pet-control-bar")), 1)
        chat_control = app.pet_canvas.find_withtag("pet-control-chat")[0]
        self.assertEqual(app.pet_canvas.type(chat_control), "polygon")
        control_bounds = app.pet_canvas.bbox("pet-control-bar")
        self.assertGreater(control_bounds[1], 100)
        self.assertLessEqual(control_bounds[3], 140)

        app._schedule_pet_hint_hide()
        pending_hide = app._hint_hide_job
        app._pet_hint(True)
        self.assertIsNone(app._hint_hide_job)
        self.assertIsNotNone(pending_hide)

        with patch.object(app, "show") as show:
            app.pet_canvas.event_generate("<ButtonPress-1>", x=80, y=70)
            app.pet_canvas.event_generate("<ButtonRelease-1>", x=80, y=70)
            root.update()
            show.assert_not_called()

            app.pet_canvas.event_generate("<ButtonPress-1>", x=64, y=124)
            app.pet_canvas.event_generate("<ButtonRelease-1>", x=64, y=124)
            root.update()
            show.assert_called_once_with()

        with patch.object(app, "_pet_menu") as show_options:
            app.pet_canvas.event_generate("<ButtonPress-1>", x=96, y=124)
            app.pet_canvas.event_generate("<ButtonRelease-1>", x=96, y=124)
            root.update()
            show_options.assert_called_once()

        app.pet_canvas.event_generate("<Motion>", x=64, y=124)
        root.update()
        titles = [app.pet_canvas.itemcget(item, "text")
                  for item in app.pet_canvas.find_withtag("pet-tooltip")
                  if app.pet_canvas.type(item) == "text"]
        self.assertEqual(titles, ["Open quick chat"])

    def test_quick_chat_opens_near_pet_without_staying_on_top_or_stealing_focus(self):
        for scaling in (96/72, 144/72, 192/72):
            with self.subTest(scaling=scaling):
                root, app = self.make_app(scaling)
                app.hide()
                root.update()
                app.show()
                root.update()
                geometry = app.chat.geometry()
                match = __import__("re").search(r"\+(\d+)\+(\d+)$", geometry)
                self.assertIsNotNone(match)
                x, y = map(int, match.groups())
                scale = ctk.ScalingTracker.get_window_scaling(app.chat)
                pet_x, pet_y = app.pet.winfo_rootx(), app.pet.winfo_rooty()
                pet_w, pet_h = app.pet.winfo_width(), app.pet.winfo_height()
                chat_top = y * scale
                chat_bottom = chat_top + app.chat.winfo_height()
                self.assertLessEqual(abs(chat_bottom - pet_y), 40 * scale,
                                     "Quick chat should sit just above the pet")
                self.assertFalse(app.chat.attributes("-topmost"))
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(pet_x + pet_w - (x * scale + app.chat.winfo_width()), 0)
                self.destroy(root)

    def test_opening_chat_does_not_force_input_focus(self):
        root, app = self.make_app(96/72)
        app.hide()
        root.update()
        with patch.object(app, "_focus_entry") as focus_entry:
            app.show()
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)

        self.assertEqual(app.chat.state(), "normal")
        focus_entry.assert_not_called()

    def test_assistant_markdown_is_rendered_with_bold_tags(self):
        root, app = self.make_app(96/72)
        app._render([{"role": "assistant", "content": "This is **bold** text and `code`."}])
        widget = app.transcript.markdown_widgets[0]
        self.assertIn("bold", widget.tag_names("1.9"))
        self.assertIn("code", widget.tag_names("1.24"))
        self.assertIn("This is bold text and code.", widget.get("1.0", "end-1c"))

    def test_quickchat_source_strip_only_shows_latest_cited_captures(self):
        root, app = self.make_app(96/72)
        app._render([{"role": "assistant", "content": "Saved in [#2].",
                      "sources": [{"id": 1}, {"id": 2}], "cited": [2]}])
        self.assertEqual(list(app.transcript.source_buttons), [2])
        widget = app.transcript.markdown_widgets[0]
        self.assertIn("citation_2", widget.tag_names("1.10"))
        self.assertIn("Saved in #2.", widget.get("1.0", "end-1c"))
