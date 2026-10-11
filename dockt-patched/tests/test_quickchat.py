"""Regression tests for the Dockt popup's input, handoff, and API contract."""
from __future__ import annotations

import json
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

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
        app.messages = []
        app._render = Mock()
        app._resize_composer = Mock()
        thread = Mock()

        with patch.object(quickchat.threading, "Thread", return_value=thread):
            result = app.submit()

        self.assertEqual(result, "break")
        self.assertEqual(app.latest_question, "first line\nsecond line")
        app.entry.delete.assert_called_once_with("1.0", "end")
        app._resize_composer.assert_called_once_with()
        thread.start.assert_called_once_with()

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
        app.entry.count.return_value = (8,)
        app._resize_composer()
        app.entry.configure.assert_called_once_with(height=5)

    def test_paste_or_other_programmatic_edits_resize_the_composer(self):
        app = quickchat.QuickChat.__new__(quickchat.QuickChat)
        app.entry = Mock()
        app.entry.edit_modified.return_value = True
        app._resize_composer = Mock()
        app._composer_modified()
        app.entry.edit_modified.assert_any_call(False)
        self.assertEqual(app.entry.edit_modified.call_count, 2)
        app._resize_composer.assert_called_once_with()

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
        app.entry.focus_set.assert_called_once_with()
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

    def test_quickchat_receives_the_resolved_desktop_service_url(self):
        with patch.dict(quickchat.os.environ,
                        {"DOCKT_PORT": "8123", "DOCKT_URL": "http://127.0.0.1:8123"}), \
             patch.object(dockt_desktop, "spawn") as spawn:
            dockt_desktop.start_quickchat()
        args, kwargs = spawn.call_args
        self.assertEqual(args[:2], ("quickchat", ["agent/quickchat.py"]))
        self.assertEqual(args[2]["DOCKT_URL"], "http://127.0.0.1:8123")
        self.assertEqual(args[2]["DOCKT_PORT"], "8123")

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


if __name__ == "__main__":
    unittest.main()
