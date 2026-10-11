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
        if bounds is None:   # the robot is in its own smooth window; the canvas only holds hover controls
            self.assertIsNotNone(app.pet_body)
            bounds = (0, 0, round(quickchat.PET_SIZE * app.ui_scale), round(quickchat.PET_SIZE * app.ui_scale))
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
        self.assertEqual(app.pet_canvas.type(chat_control), "image")
        control_bounds = app.pet_canvas.bbox("pet-control-bar")
        self.assertGreater(control_bounds[1], 100)
        self.assertLessEqual(control_bounds[3], 140)
        self.assertTrue(app.pet_canvas.find_withtag("pet-control-mic"))

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

            app.pet_canvas.event_generate("<ButtonPress-1>", x=48, y=124)
            app.pet_canvas.event_generate("<ButtonRelease-1>", x=48, y=124)
            root.update()
            show.assert_called_once_with()

        with patch.object(app, "_toggle_voice") as voice, patch.object(app, "_show_options_at") as show_options:
            app.pet_canvas.event_generate("<ButtonPress-1>", x=112, y=124)
            app.pet_canvas.event_generate("<ButtonRelease-1>", x=112, y=124)
            root.update()
            voice.assert_not_called()
            show_options.assert_called_once()
            show_options.reset_mock()
            with patch.object(app, "_toggle_conversation") as talk:
                app.pet_canvas.event_generate("<ButtonPress-1>", x=80, y=124)
                app.pet_canvas.event_generate("<ButtonRelease-1>", x=80, y=124)
                root.update()
                talk.assert_called_once_with()
            app.pet_canvas.event_generate("<Button-3>", x=80, y=70)   # options are on right-click
            root.update()
            show_options.assert_called_once()
        app._pet_hint(True)   # opening the menu hides the controls; hovering the pet again brings them back

        app.pet_canvas.event_generate("<Motion>", x=48, y=124)
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

    def test_pet_mic_records_then_fills_the_quick_chat_without_sending(self):
        root, app = self.make_app(96/72)
        app.hide()
        app.pet.geometry("160x140+10000+10000")
        app.pet.deiconify()
        root.update()
        recorder = Mock()
        recorder.stop.return_value = "where is dinner tonight"
        with patch.object(quickchat, "MicRecorder", return_value=recorder),                 patch.object(quickchat, "transcribe_audio", return_value="where is dinner tonight") as upload,                 patch.object(app, "_focus_entry"):
            app._pet_hint(True)
            for action in (app._start_voice, app._stop_voice):
                action()
                root.update()
                if app.voice_state == "recording":
                    recorder.start.assert_called_once_with()
                    self.assertEqual(app.pet_notice, "Listening…")
                    app._pet_hint(False)
                    self.assertTrue(app.pet_hint_visible, "the stop button stays visible while recording")
            for _ in range(50):
                root.update()
                if app.voice_state == "idle":
                    break
                time.sleep(0.02)
        upload.assert_not_called()
        self.assertEqual(app.voice_state, "idle")
        self.assertEqual(app.chat.state(), "normal", "the quick chat opens with the words in it")
        self.assertEqual(app.entry.get("1.0", "end-1c"), "where is dinner tonight")
        self.assertFalse(app.placeholder_visible)
        self.assertEqual(app.messages, [], "voice fills the box; Enter sends it")

    def test_voice_conversation_sends_then_reads_reply(self):
        root, app = self.make_app(96/72)
        app._clear_composer_placeholder()
        app.entry.insert("1.0", "Keep this unsent draft")
        recorder = Mock()
        recorder.stop.return_value = "When is dinner?"
        result = {"answer": "Dinner is at seven. [#3]", "sources": [], "cited": [], "ms": 1}
        with patch.object(quickchat, "MicRecorder", return_value=recorder), \
                patch.object(quickchat, "transcribe_audio", return_value="When is dinner?"), \
                patch.object(quickchat, "chat_request", return_value=result) as chat, \
                patch.object(app.speech, "speak") as speak:
            app._toggle_conversation()
            self.assertTrue(app.voice_conversation)
            app._toggle_conversation()
            for _ in range(100):
                root.update()
                if speak.called:
                    break
                time.sleep(0.02)
            self.assertTrue(speak.called)
            self.assertEqual(chat.call_args.args[0][-1]["content"], "When is dinner?")
            self.assertEqual(speak.call_args.args[0], result["answer"])
            self.assertEqual(app.voice_state, "speaking")
            self.assertEqual(app.entry.get("1.0", "end-1c"), "Keep this unsent draft")
            speak.call_args.args[1](None)
            app._drain_events()
            self.assertFalse(app.voice_conversation)
            self.assertEqual(app.voice_state, "idle")

    def test_browser_auto_stop_sends_question_then_speaks_once(self):
        root, app = self.make_app(96/72)
        app._clear_composer_placeholder()
        app.entry.insert("1.0", "Keep my draft")
        recorder = quickchat.MicRecorder()
        def command(action):
            return {"state": "recording"} if action == "start" else {"state": "done", "text": "When is dinner?"}
        result = {"answer": "Dinner is at eight. [#42]", "sources": [], "cited": [42], "ms": 1}
        with patch.object(recorder, "_command", side_effect=command), \
             patch.object(quickchat, "MicRecorder", return_value=recorder), \
             patch.object(quickchat, "chat_request", return_value=result) as chat, \
             patch.object(quickchat, "load_thumbnail", return_value=None), \
             patch.object(app.speech, "speak") as speak:
            app._toggle_conversation()
            for _ in range(100):
                root.update()
                if speak.called:
                    break
                time.sleep(.02)
            chat.assert_called_once()
            speak.assert_called_once()
            self.assertEqual(app.voice_state, "speaking")
            self.assertEqual(app.entry.get("1.0", "end-1c"), "Keep my draft")
            speak.call_args.args[1](None)
            app._drain_events()
            self.assertEqual(app.voice_state, "idle")

    def test_cancel_voice_ignores_late_transcript_and_stops_speech(self):
        root, app = self.make_app(96/72)
        with patch.object(app.speech, "cancel") as stop:
            app.voice_conversation = True
            app.voice_state = "transcribing"
            generation = app.voice_generation
            app._toggle_conversation()
            stop.assert_called_once_with()
            app.events.put(("voice-text", (generation, "An old question")))
            app._drain_events()
            self.assertTrue(app.placeholder_visible)
            self.assertEqual(app.messages, [])
            self.assertFalse(app.voice_conversation)

    def test_pet_mic_failure_shows_a_short_notice(self):
        root, app = self.make_app(96/72)
        app.hide()
        root.update()
        recorder = Mock()
        recorder.stop.return_value = "where is dinner tonight"
        error = OSError("Voice input isn't set up on this Dockt yet.")
        recorder.stop.side_effect = error
        with patch.object(quickchat, "MicRecorder", return_value=recorder),                 patch.object(quickchat, "transcribe_audio", side_effect=error):
            app._toggle_voice()
            app._toggle_voice()
            for _ in range(50):
                root.update()
                if app.voice_state == "idle":
                    break
                time.sleep(0.02)
        self.assertEqual(app.pet_notice, "Voice input failed")
        self.assertEqual(app.status.get(), "Voice input isn't set up on this Dockt yet.")
        self.assertEqual(app.chat.state(), "normal", "show the voice error in quick chat")

    def _record_with_levels(self, app, root, levels):
        """Start the pet mic on a fake recorder that hears `levels` (one per 100 ms), and run Tk until it's idle."""
        recorder = Mock()
        recorder.stop.return_value = "where is dinner tonight"
        blocks = list(levels)
        recorder.levels.side_effect = lambda: [blocks.pop(0)] if blocks else []
        app._start_voice()
        for _ in range(400):
            root.update()
            if app.voice_state != "recording":
                break
            time.sleep(0.01)
        return recorder

    def test_pet_mic_stops_by_itself_when_the_speaker_is_done(self):
        root, app = self.make_app(96/72)
        app.hide()
        with patch.object(quickchat, "MicRecorder", side_effect=lambda: recorder_holder[0]),                 patch.object(quickchat, "transcribe_audio", return_value="where is dinner tonight"),                 patch.object(quickchat, "VOICE_TICK_MS", 5), patch.object(app, "_focus_entry"):
            speech = [0.001] * 4 + [0.08] * 12 + [0.001] * 20   # room, about a second of speech, then quiet
            recorder_holder = [None]
            recorder = Mock()
            recorder.stop.return_value = "where is dinner tonight"
            recorder.levels.side_effect = lambda: [speech.pop(0)] if speech else []
            recorder_holder[0] = recorder
            app._start_voice()
            for _ in range(400):
                root.update()
                if app.voice_state == "idle" and app.entry.get("1.0", "end-1c") == "where is dinner tonight":
                    break
                time.sleep(0.01)
        recorder.stop.assert_called_once_with()   # no second click needed
        self.assertEqual(app.entry.get("1.0", "end-1c"), "where is dinner tonight")
        self.assertGreaterEqual(len(speech), 7, "it stops after 1.2 s of quiet, not at the end of the input")

    def test_pet_mic_gives_up_when_nobody_speaks(self):
        root, app = self.make_app(96/72)
        recorder = Mock()
        recorder.levels.side_effect = lambda: [0.001]
        with patch.object(quickchat, "MicRecorder", return_value=recorder),                 patch.object(quickchat, "VOICE_TICK_MS", 1):
            app._start_voice()
            for _ in range(2000):
                root.update()
                if app.voice_state == "idle":
                    break
                time.sleep(0.001)
        self.assertEqual(app.voice_state, "idle")
        recorder.cancel.assert_called_once_with()
        recorder.stop.assert_not_called()
        self.assertEqual(app.pet_notice, "Didn't hear anything")

    def test_pet_body_has_smooth_edges_on_windows(self):
        root, app = self.make_app(96/72)
        if app.pet_body is None:
            self.skipTest("per-pixel-alpha body needs Windows")
        app.pet.geometry("160x140+10000+10000")
        app.pet.deiconify()
        root.update()
        app._draw_pet()
        frame = next(iter(app._pet_smooth.values()))
        alpha = bytes(frame[3::4])
        partial = sum(1 for a in alpha if 0 < a < 255)
        self.assertGreater(partial, 100, "edges are blended (partly transparent), not cut to fully on/off")
        self.assertFalse(app.pet_canvas.find_withtag("pet-body"), "the robot isn't also drawn on the keyed canvas")

