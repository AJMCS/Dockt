"""Pet -> local desktop HTTP bridge -> WebView command, without microphone access."""
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import quickchat
from desktop import dockt_desktop as desktop


@pytest.fixture
def bridge(monkeypatch):
    class Window:
        def __init__(self):
            self.calls = []

        def evaluate_js(self, script):
            action, token = json.loads("[" + script[len("petVoiceCommand("):-1] + "]")
            self.calls.append((action, token))
            return {"id": token, "state": {"start": "recording", "stop": "working",
                    "status": "done", "cancel": "cancelled"}[action], "text": "Dinner at seven."}

    window = Window()
    monkeypatch.setattr(desktop.Control, "window", window)
    ready = threading.Event()
    ready.set()
    monkeypatch.setattr(desktop, "window_ready", ready)
    server = ThreadingHTTPServer(("127.0.0.1", 0), desktop.Control)
    monkeypatch.setattr(quickchat, "DESKTOP_CONTROL_PORT", server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield window
    server.shutdown()
    server.server_close()
    thread.join()


def test_recorder_uses_local_desktop_bridge(bridge):
    recorder = quickchat.MicRecorder()
    recorder.start()
    assert recorder.stop() == "Dinner at seven."
    assert [action for action, _ in bridge.calls] == ["start", "stop", "status"]
    assert all(token == recorder.id for _, token in bridge.calls)


def test_cancel_reaches_browser_recorder(bridge):
    recorder = quickchat.MicRecorder()
    recorder.start()
    recorder.cancel()
    assert bridge.calls[-1] == ("cancel", recorder.id)


def test_bridge_rejects_script_input(bridge):
    recorder = quickchat.MicRecorder()
    recorder.id = "'); malicious_code(); ('"
    with pytest.raises(OSError):
        recorder.start()
    assert bridge.calls == []
