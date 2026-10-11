"""Regression tests for the Dockt popup's input, handoff, and API contract."""
from __future__ import annotations

import json
import ctypes
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import quickchat  # noqa: E402
from desktop import dockt_desktop  # noqa: E402


class TextareaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.composer = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "textarea" and attributes.get("id") == "q":
            self.composer = attributes


class FakeWinCall:
    def __init__(self, name: str, calls: list[tuple], result=None):
        self.name = name
        self.calls = calls
        self.result = result
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append((self.name, *args))
        return self.result


class QuickChatTests(unittest.TestCase):
    def test_service_selection_skips_backends_without_shared_history(self):
        old_port, old_base = dockt_desktop.PORT, dockt_desktop.BASE
        try:
            with patch.dict(dockt_desktop.os.environ, {}, clear=True), \
                 patch.object(dockt_desktop, "answers", side_effect=lambda url, **kw:
                              url.endswith("/api/stats") or url == "http://127.0.0.1:8002/api/conversations"), \
                 patch.object(dockt_desktop, "port_free",
                              side_effect=lambda p: p not in (8000, 8001, 8002)) as free:
                dockt_desktop.resolve_port()
                self.assertEqual(dockt_desktop.PORT, 8002)
                self.assertEqual(dockt_desktop.os.environ["DOCKT_URL"], "http://127.0.0.1:8002")
                self.assertEqual(free.call_count, 3)
        finally:
            dockt_desktop.PORT, dockt_desktop.BASE = old_port, old_base

    def test_port_resolution_checks_only_listening_ports_and_uses_first_free(self):
        old_port, old_base = dockt_desktop.PORT, dockt_desktop.BASE
        try:
            with patch.dict(dockt_desktop.os.environ, {}, clear=True), \
                 patch.object(dockt_desktop, "port_free", side_effect=lambda p: p not in (8000, 8001)), \
                 patch.object(dockt_desktop, "answers", return_value=False) as answers:
                dockt_desktop.resolve_port()
                self.assertEqual(dockt_desktop.PORT, 8002)
                self.assertEqual(answers.call_count, 2)  # the one occupied port gets two API checks
        finally:
            dockt_desktop.PORT, dockt_desktop.BASE = old_port, old_base

    def test_dashboard_is_temporarily_raised_when_opened(self):
        window = Mock()
        dockt_desktop.window_ready.set()
        try:
            dockt_desktop.show(window, "")
        finally:
            dockt_desktop.window_ready.clear()
        window.restore.assert_called_once_with()
        window.show.assert_called_once_with()
        self.assertTrue(window.on_top)

    def test_dashboard_still_opens_when_platform_has_no_on_top_support(self):
        class Window:
            def restore(self): pass
            def show(self): self.was_shown = True
            def evaluate_js(self, _script): pass
            @property
            def on_top(self): return False
            @on_top.setter
            def on_top(self, _value): raise NotImplementedError("unsupported here")
        window = Window()
        dockt_desktop.window_ready.set()
        try:
            dockt_desktop.show(window, "")
        finally:
            dockt_desktop.window_ready.clear()
        self.assertTrue(window.was_shown)

    def test_dashboard_open_is_queued_while_webview_starts(self):
        previous = dockt_desktop.Control.window
        dockt_desktop.Control.window = Mock()
        dockt_desktop.window_ready.clear()
        dockt_desktop.pending_show = False
        server = ThreadingHTTPServer(("127.0.0.1", 0), dockt_desktop.Control)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/open?hash=%23%2Fconversation%2Fabc",
                         timeout=1) as response:
                self.assertEqual(response.status, 200)
            dockt_desktop.Control.window.restore.assert_not_called()
            self.assertTrue(dockt_desktop.pending_show)
            self.assertEqual(dockt_desktop.pending_hash, "#/conversation/abc")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            dockt_desktop.Control.window = previous
            dockt_desktop.pending_hash = ""
            dockt_desktop.pending_show = False

    def test_desktop_lock_rejects_a_second_process_and_releases_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "desktop.lock"
            self.assertTrue(dockt_desktop.acquire_single_instance_lock(lock_path))
            script = ("import sys; from pathlib import Path; "
                      "sys.path.insert(0, sys.argv[1]); "
                      "from desktop.dockt_desktop import acquire_single_instance_lock; "
                      "print(acquire_single_instance_lock(Path(sys.argv[2])))")
            result = quickchat.subprocess.run(
                [sys.executable, "-c", script, str(ROOT), str(lock_path)],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.stdout.strip(), "False")
            dockt_desktop._instance_lock_handle.close()
            dockt_desktop._instance_lock_handle = None
            result = quickchat.subprocess.run(
                [sys.executable, "-c", script, str(ROOT), str(lock_path)],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.stdout.strip(), "True")

    def test_capture_toggle_creates_and_removes_the_shared_pause_file(self):
        with tempfile.TemporaryDirectory() as directory:
            pause_file = Path(directory) / ".dockt_pause"
            with patch.object(quickchat, "CAPTURE_PAUSE_FILE", pause_file):
                self.assertFalse(quickchat.capture_is_paused())
                self.assertTrue(quickchat.toggle_capture())
                self.assertTrue(pause_file.exists())
                self.assertTrue(quickchat.capture_is_paused())
                self.assertFalse(quickchat.toggle_capture())
                self.assertFalse(pause_file.exists())

    def test_global_toggle_hides_and_restores_the_pet_only(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.pet = Mock()
        app.pet.state.return_value = "normal"
        app.hide_pet = Mock()
        app.toggle()
        app.hide_pet.assert_called_once_with()
        app.pet.deiconify.assert_not_called()

        app.pet.reset_mock()
        app.pet.state.return_value = "withdrawn"
        app.toggle()
        app.pet.deiconify.assert_called_once_with()
        app.pet.lift.assert_called_once_with()
        app.pet.attributes.assert_called_once_with("-topmost", True)
        app.hide_pet.assert_called_once_with()

    def test_options_panel_dismissal_does_not_run_quit_action(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        panel = Mock()
        app.options_panel = panel
        action = Mock()
        app._run_option(action)
        panel.destroy.assert_called_once_with()
        action.assert_called_once_with()
        self.assertIsNone(app.options_panel)

    def test_clicking_outside_options_only_dismisses_the_panel(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        panel = Mock()
        app.options_panel = panel
        app.root = Mock()
        app.root.focus_get.return_value = None
        app._dismiss_options_if_focus_left(panel)
        panel.destroy.assert_called_once_with()
        app.root.destroy.assert_not_called()
        self.assertIsNone(app.options_panel)

    def test_focus_detection_only_keeps_options_open_for_its_own_widgets(self):
        panel = ".!toplevel"
        self.assertTrue(quickchat.QuickChat._focus_is_inside_options(
            panel, ".!toplevel.!frame.!button"))
        self.assertFalse(quickchat.QuickChat._focus_is_inside_options(panel, None))
        self.assertFalse(quickchat.QuickChat._focus_is_inside_options(
            panel, ".!other"))

    def test_quit_closes_the_dashboard_and_quickchat(self):
        root = Mock()
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.root = root
        app.hotkey = None
        with patch.object(quickchat, "DESKTOP_CONTROL_PORT", 18765), \
             patch.object(quickchat, "urlopen") as open_url:
            app.close()
        open_url.assert_called_once_with("http://127.0.0.1:18765/quit", timeout=1)
        root.destroy.assert_called_once_with()

    def test_chat_request_sends_full_history_to_local_chat_api(self):
        history = [
            {"role": "user", "content": "Where is dinner?"},
            {"role": "assistant", "content": "Nopalito at 8 [#9]"},
            {"role": "user", "content": "Who booked it?"},
        ]
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"answer":"Sam booked it [#9]","sources":[],"cited":[9]}'

        with patch.object(quickchat, "BASE_URL", "http://127.0.0.1:8123"), \
             patch.object(quickchat, "urlopen", return_value=response) as open_url:
            result = quickchat.chat_request(history)

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8123/api/chat")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"messages": history, "stream": False})
        self.assertEqual(result["cited"], [9])

    def test_chat_request_turns_http_failure_into_friendly_exception(self):
        response = Mock()
        response.read.return_value = b"model details should stay private"
        failure = HTTPError("http://127.0.0.1/api/chat", 503, "unavailable", {}, response)
        with patch.object(quickchat, "urlopen", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                quickchat.chat_request([{"role": "user", "content": "hello"}])

    def test_submit_reads_multiline_text_and_clears_composer(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.entry = Mock()
        app.entry.get.return_value = "first line\nsecond line"
        app.question = None  # The composer is a Text widget, not a StringVar.
        app.busy = False
        app.placeholder_visible = False
        app.latest_question = ""
        app.status = Mock()
        app.ask_button = Mock()
        app.retry_button = Mock()
        app.messages = []
        app._render = Mock()
        app._resize_composer = Mock()
        thread = Mock()

        with patch.object(quickchat.threading, "Thread", return_value=thread):
            result = app.submit()

        self.assertEqual(result, "break")
        self.assertEqual(app.latest_question, "first line\nsecond line")
        app.entry.delete.assert_called_once_with("1.0", "end")
        app.retry_button.grid_remove.assert_called_once_with()
        app._resize_composer.assert_called_once_with()
        thread.start.assert_called_once_with()

    def test_failed_question_stays_in_history_without_repopulating_composer(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.events = __import__("queue").Queue()
        app.events.put(("error", "service unavailable"))
        app.busy = True
        app.ask_button = Mock()
        app.retry_button = Mock()
        app.retry_payload = None
        app.messages = []
        app.latest_question = "hiii"
        app.status = Mock()
        app.entry = Mock()
        app.entry.get.return_value = ""
        app._render = Mock()
        app.root = Mock()
        app.root.after = Mock()
        app.events.put(("stop", None))
        # Drain one event only; queue.Empty ends the handler after its event.
        with patch.object(app.events, "get_nowait", side_effect=[
                ("error", "service unavailable"), __import__("queue").Empty]):
            app._drain_events()
        app.entry.insert.assert_not_called()
        app.retry_button.grid.assert_called_once_with()
        expected = [{"role": "user", "content": "hiii"}]
        self.assertEqual(app.messages, expected)
        self.assertEqual(app.retry_payload, expected)
        app._render.assert_called_once_with(
            expected, "I couldn't reach Dockt. Check that it is running and try again.")

    def test_retry_success_does_not_duplicate_question_already_in_history(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.events = __import__("queue").Queue()
        app.events.put(("answer", ("hiii", "hello", {"answer":"hello", "sources":[], "cited":[], "ms":4})))
        app.history_ts = 0
        app.messages = [{"role":"user", "content":"hiii"}]
        app.busy = True
        app.ask_button = Mock()
        app.retry_button = Mock()
        app.retry_payload = [{"role":"user", "content":"hiii"}]
        app.status = Mock()
        app._render = Mock()
        app._clear_thumbnails = Mock()
        app.root = Mock()
        app.root.after = Mock()
        with patch.object(app.events, "get_nowait", side_effect=[
                ("answer", ("hiii", "hello", {"answer":"hello", "sources":[], "cited":[], "ms":4})),
                __import__("queue").Empty]):
            app._drain_events()
        self.assertEqual([m["role"] for m in app.messages], ["user", "assistant"])

    def test_retry_resends_the_same_question_without_adding_a_duplicate(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.busy = False
        app.retry_payload = [{"role": "user", "content": "hiii"}]
        app.ask_button = Mock()
        app.retry_button = Mock()
        app.status = Mock()
        thread = Mock()
        with patch.object(quickchat.threading, "Thread", return_value=thread) as factory:
            self.assertEqual(app.retry_latest(), "break")
        self.assertEqual(factory.call_args.kwargs["args"][1], app.retry_payload)
        thread.start.assert_called_once_with()

    def test_show_opens_quickchat_at_the_compact_default_size(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app._pet_hint = Mock()
        app.theme_name = "light"
        app.pet = Mock()
        app.chat = Mock()
        app.chat.winfo_screenwidth.return_value = 1920
        app.chat.winfo_screenheight.return_value = 1080
        app.chat.winfo_id.return_value = 123
        app.pet.winfo_rootx.return_value = 1700
        app.pet.winfo_rooty.return_value = 700
        app.pet.winfo_width.return_value = 160
        app.pet.winfo_height.return_value = 140
        with patch.object(quickchat.ctk.ScalingTracker, "get_window_scaling", return_value=1.0), \
             patch.object(quickchat, "_apply_windows_caption_theme"):
            app.show()
        app.chat.geometry.assert_called_once_with("560x480+1300+208")
        app.chat.minsize.assert_called_once_with(360, 280)

    def test_history_poll_skips_a_new_unsaved_conversation(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.history_ts = 0
        app.history_loading = False
        app.busy = False
        app.conversation_id = "new-conversation"
        app.chat = Mock()
        app.chat.state.return_value = "normal"
        app.root = Mock()
        app.events = __import__("queue").Queue()

        with patch.object(quickchat.threading, "Thread") as thread:
            app._poll_shared_history()
        thread.assert_not_called()
        app.root.after.assert_called_once_with(3000, app._poll_shared_history)

        app.history_ts = 1
        worker = Mock()
        with patch.object(quickchat.threading, "Thread", return_value=worker) as thread:
            app._poll_shared_history()
        thread.assert_called_once()
        worker.start.assert_called_once_with()

    def test_placeholder_is_not_submitted_as_a_user_question(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.placeholder_visible = True
        app.entry = Mock()
        app.busy = False
        with patch.object(quickchat.threading, "Thread") as thread:
            self.assertEqual(app.submit(), "break")
        app.entry.delete.assert_not_called()
        thread.assert_not_called()

    def test_placeholder_clears_on_focus_and_returns_when_empty(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.entry = Mock()
        app.entry.get.return_value = ""
        app.placeholder_visible = True
        app._resize_composer = Mock()
        app._clear_composer_placeholder()
        app.entry.delete.assert_called_once_with("1.0", "end")
        self.assertFalse(app.placeholder_visible)

        app._show_composer_placeholder()
        app.entry.insert.assert_called_once_with("1.0", quickchat.COMPOSER_PLACEHOLDER,
                                                "placeholder")
        self.assertTrue(app.placeholder_visible)

    def test_composer_grows_to_five_lines_without_expanding_forever(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.entry = Mock()
        app.entry._textbox = Mock()
        app.entry._textbox.count.return_value = (8,)
        app.entry.cget.return_value = 44
        app.chat = Mock()
        app.chat.winfo_height.return_value = 700   # tall enough that the 128 px cap applies
        with patch.object(quickchat.ctk.ScalingTracker, "get_window_scaling", return_value=1.0):
            app._resize_composer()
        app.entry.configure.assert_called_once_with(height=128)

    def test_paste_or_other_programmatic_edits_resize_the_composer(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.entry = Mock()
        app.entry.edit_modified.return_value = True
        app._schedule_composer_resize = Mock()
        app._composer_modified()
        app.entry.edit_modified.assert_any_call(False)
        self.assertEqual(app.entry.edit_modified.call_count, 2)
        app._schedule_composer_resize.assert_called_once_with()

    def test_windows_focus_raises_top_level_and_focuses_input_child(self):
        calls: list[tuple] = []
        user32 = SimpleNamespace(**{
            "GetAncestor": FakeWinCall("GetAncestor", calls, 200),
            "ShowWindow": FakeWinCall("ShowWindow", calls),
            "GetForegroundWindow": FakeWinCall("GetForegroundWindow", calls, 300),
            "GetWindowThreadProcessId": FakeWinCall("GetWindowThreadProcessId", calls, 11),
            "AttachThreadInput": FakeWinCall("AttachThreadInput", calls, True),
            "BringWindowToTop": FakeWinCall("BringWindowToTop", calls),
            "SetForegroundWindow": FakeWinCall("SetForegroundWindow", calls, True),
            "SetActiveWindow": FakeWinCall("SetActiveWindow", calls, 200),
            "SetFocus": FakeWinCall("SetFocus", calls, 100),
        })
        kernel32 = SimpleNamespace(GetCurrentThreadId=FakeWinCall("GetCurrentThreadId", calls, 22))

        quickchat._focus_windows_entry(100, 400, user32, kernel32)

        def args_for(name):
            return [call[1:] for call in calls if call[0] == name]

        self.assertEqual(args_for("SetForegroundWindow")[0][0], 200)
        self.assertEqual(args_for("SetFocus")[0][0].value, 400)
        self.assertEqual(args_for("AttachThreadInput"), [(22, 11, True), (22, 11, False)])

    def test_refocusing_after_a_click_preserves_the_clicked_caret_position(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.chat = Mock()
        app.chat.state.return_value = "normal"
        app.entry = Mock()
        with patch.object(quickchat.os, "name", "posix"):
            app._focus_entry(move_cursor=False)
        app.entry.focus_force.assert_called_once_with()
        app.entry.mark_set.assert_not_called()

    def test_open_in_dockt_hands_off_to_existing_desktop_window(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        path = "/#/chat?q=where%20is%20dinner"
        with patch.object(quickchat, "DESKTOP_CONTROL_PORT", 18765), \
             patch.object(quickchat, "urlopen", return_value=response) as open_url, \
             patch.object(quickchat, "_edge_path") as edge_path, \
             patch.object(quickchat.subprocess, "Popen") as popen:
            quickchat.open_dockt(path)

        request = open_url.call_args.args[0]
        self.assertEqual(request, "http://127.0.0.1:18765/open?hash=%2F%23%2Fchat%3Fq%3Dwhere%2520is%2520dinner")
        edge_path.assert_not_called()
        popen.assert_not_called()

    def test_open_in_dockt_falls_back_to_browser_when_desktop_is_not_running(self):
        with patch.object(quickchat, "urlopen", side_effect=OSError("closed")), \
             patch.object(quickchat, "_edge_path", return_value="edge.exe"), \
             patch.object(quickchat.subprocess, "Popen") as popen:
            quickchat.open_dockt("/#/capture/42")
        popen.assert_called_once_with(["edge.exe", "--app=" + quickchat.BASE_URL + "/#/capture/42"],
                                      close_fds=True)

    def test_dashboard_composer_remains_an_editable_multiline_control(self):
        parser = TextareaParser()
        parser.feed((ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8"))
        self.assertIsNotNone(parser.composer)
        self.assertEqual(parser.composer.get("rows"), "1")
        self.assertNotIn("disabled", parser.composer)
        self.assertNotIn("readonly", parser.composer)
        self.assertTrue(parser.composer.get("placeholder"))

    def test_dashboard_keeps_enter_send_and_shift_enter_multiline_contract(self):
        html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('e.key === "Enter" && !e.shiftKey && !e.isComposing', html)
        self.assertIn('e.preventDefault(); run();', html)

    def test_desktop_deep_link_sanitizes_routes(self):
        self.assertEqual(dockt_desktop.clean_hash("/capture/9"), "#/capture/9")
        self.assertEqual(dockt_desktop.clean_hash("#/chat?q=hello"), "#/chat?q=hello")
        self.assertEqual(dockt_desktop.clean_hash("/settings"), "")

    def test_dashboard_starts_hidden_and_centered_at_a_compact_size(self):
        options = dockt_desktop.dashboard_window_options("")
        self.assertTrue(options["hidden"])
        self.assertEqual((options["width"], options["height"]), (1200, 760))
        self.assertIsNone(options["x"])
        self.assertIsNone(options["y"])
        self.assertEqual(options["min_size"], (960, 600))
        self.assertFalse(dockt_desktop.dashboard_window_options("#/capture/9")["hidden"])

    def test_desktop_quit_control_closes_the_dashboard_window(self):
        window = Mock()
        previous = dockt_desktop.Control.window
        dockt_desktop.Control.window = window
        dockt_desktop.shutdown_requested.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), dockt_desktop.Control)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/quit", timeout=2) as response:
                self.assertEqual(response.status, 200)
            window.destroy.assert_called_once_with()
            self.assertTrue(dockt_desktop.shutdown_requested.is_set())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            dockt_desktop.Control.window = previous
            dockt_desktop.shutdown_requested.clear()

    def test_dashboard_close_hides_it_but_quit_allows_shutdown(self):
        window = Mock()
        dockt_desktop.shutdown_requested.clear()
        self.assertFalse(dockt_desktop.hide_dashboard_on_close(window))
        window.hide.assert_called_once_with()
        dockt_desktop.shutdown_requested.set()
        self.assertTrue(dockt_desktop.hide_dashboard_on_close(window))
        window.hide.assert_called_once_with()
        dockt_desktop.shutdown_requested.clear()

    def test_quickchat_receives_the_resolved_desktop_service_url(self):
        with patch.dict(quickchat.os.environ,
                        {"DOCKT_PORT": "8123", "DOCKT_URL": "http://127.0.0.1:8123"}), \
             patch.object(dockt_desktop, "spawn") as spawn:
            dockt_desktop.start_quickchat()
        args, kwargs = spawn.call_args
        self.assertEqual(args[:2], ("quickchat", ["agent/quickchat.py"]))
        self.assertEqual(args[2]["DOCKT_URL"], "http://127.0.0.1:8123")
        self.assertEqual(args[2]["DOCKT_PORT"], "8123")

    def test_reusing_running_backend_still_restarts_gx10_tunnel(self):
        with patch.dict(dockt_desktop.os.environ,
                        {"DOCKT_GX10": "asus@169.233.72.21"}, clear=True), \
             patch.object(dockt_desktop, "answers",
                          side_effect=lambda url, timeout=1.0: url.endswith("/api/stats")), \
             patch.object(dockt_desktop, "start_tunnel") as start_tunnel:
            self.assertIsNone(dockt_desktop.start_backend(mock=False, capture=False))
        start_tunnel.assert_called_once_with("asus@169.233.72.21")

    def test_primary_launcher_starts_one_native_app_with_quick_chat(self):
        launcher = (ROOT / "start-dockt.bat").read_text(encoding="utf-8")
        desktop = (ROOT / "desktop" / "dockt_desktop.py").read_text(encoding="utf-8")
        self.assertIn('call "desktop\\Dockt.bat" %*', launcher)
        self.assertNotIn("msedge --app", launcher.lower())
        self.assertIn('spawn("quickchat", ["agent/quickchat.py"]', desktop)

    def test_quickchat_palette_matches_dashboard_tokens(self):
        self.assertEqual(quickchat.PALETTES["light"]["bg"], "#FAF9F5")
        self.assertEqual(quickchat.PALETTES["light"]["accent"], "#76B900")
        self.assertEqual(quickchat.PALETTES["light"]["text"], "#1F1E1D")
        self.assertEqual(quickchat.PALETTES["dark"]["bg"], "#262624")
        self.assertEqual(quickchat.PALETTES["dark"]["accent_text"], "#A3E635")

    def test_windows_dpi_awareness_prefers_per_monitor_v2(self):
        user32 = Mock()
        user32.SetProcessDpiAwarenessContext.return_value = 1
        self.assertTrue(quickchat._enable_windows_dpi_awareness(user32=user32))
        context = user32.SetProcessDpiAwarenessContext.call_args.args[0]
        self.assertEqual(context.value, ctypes.c_void_p(-4).value)


if __name__ == "__main__":
    unittest.main()
