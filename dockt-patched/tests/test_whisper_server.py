"""tools/whisper_server.py with a stand-in model: no download, no CPU-heavy transcription."""
import io
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import whisper_server as ws  # noqa: E402


def wav(seconds=0.5, rate=16000, channels=1, value=1000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.full(int(seconds * rate) * channels, value, dtype="<i2").tobytes())
    return buffer.getvalue()


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **options):
        self.calls.append((audio, options))
        return iter([SimpleNamespace(text=" Where is dinner"), SimpleNamespace(text=" tonight? ")]), None


@pytest.fixture
def client(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(ws, "_model", lambda name: model)
    with TestClient(ws.app) as c:   # the startup warm-up calls the patched _model, so nothing loads
        c.model = model
        yield c


def test_transcribes_a_wav_upload_like_the_openai_api(client):
    r = client.post("/v1/audio/transcriptions", files={"file": ("speech.wav", wav(), "audio/wav")},
                    data={"model": "small"})
    assert r.status_code == 200 and r.json() == {"text": "Where is dinner tonight?"}
    audio, options = client.model.calls[0]
    assert isinstance(audio, np.ndarray) and audio.dtype == np.float32 and len(audio) == 8000
    assert options["beam_size"] == 1 and options["vad_filter"] is True


def test_plain_text_response_format(client):
    r = client.post("/v1/audio/transcriptions", files={"file": ("speech.wav", wav(), "audio/wav")},
                    data={"model": "small", "response_format": "text"})
    assert r.status_code == 200 and r.text == "Where is dinner tonight?"


def test_rejects_unknown_models_and_empty_audio(client):
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", wav(), "audio/wav")},
                    data={"model": "someone/arbitrary-repo"})
    assert r.status_code == 400 and "unknown model" in r.json()["detail"]
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"", "audio/wav")}, data={"model": "small"})
    assert r.status_code == 400
    assert client.model.calls == []


def test_models_lists_the_supported_sizes(client):
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert "small" in ids and "base.en" in ids


def test_decode_resamples_and_downmixes_wav_to_16k_mono():
    audio = ws._decode(wav(seconds=1.0, rate=22050, channels=2, value=16384))
    assert isinstance(audio, np.ndarray) and abs(len(audio) - 16000) <= 1
    assert np.allclose(audio, 0.5, atol=1e-3)


def test_decode_passes_other_formats_to_faster_whisper():
    assert isinstance(ws._decode(b"OggS-not-a-wav"), io.BytesIO)
