import sys
import threading
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import quickchat


def test_local_speech_sends_clean_text_to_local_kokoro(monkeypatch):
    process = Mock()
    process.returncode = 0
    process.poll.return_value = 0
    spawn = Mock(return_value=process)
    monkeypatch.setattr(quickchat.subprocess, "Popen", spawn)
    finished = threading.Event()
    errors = []
    speech = quickchat.LocalSpeech()
    speech.speak("**Hello** Priya. [#3] `$(secret)`", lambda error: (errors.append(error), finished.set()))
    assert finished.wait(2)
    arguments = spawn.call_args.args[0]
    assert arguments == [sys.executable, "-m", "agent.kokoro_speech"]
    assert process.communicate.call_args.args[0] == b"Hello Priya.  $(secret)"
    assert spawn.call_args.kwargs["creationflags"] == quickchat.subprocess.CREATE_NO_WINDOW
    assert spawn.call_args.kwargs["stdin"] == quickchat.subprocess.PIPE
    assert errors == [None]


def test_stopping_speech_terminates_kokoro_child():
    speech = quickchat.LocalSpeech()
    process = Mock()
    process.poll.return_value = None
    speech.process = process
    speech.cancel()
    process.terminate.assert_called_once_with()
    assert speech.process is None
