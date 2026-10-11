"""
Local speech-to-text for Dockt's voice input: faster-whisper on this machine's CPU, behind the same
OpenAI-compatible endpoint a GPU Whisper server would offer. Audio never leaves the machine.

  python -m uvicorn --app-dir tools whisper_server:app --port 8001     # what desktop/dockt_desktop.py starts

server/models.py calls it at STT_URL (default http://localhost:8001/v1) with STT_MODEL (default "small").
The first request for a model downloads it once (~480 MB for small) into the Hugging Face cache.

  WHISPER_MODEL     model to load at startup (default: small); requests may ask for another listed size
  WHISPER_LANGUAGE  e.g. "en" to skip language detection (default: detect)
  WHISPER_THREADS   CPU threads (default: CTranslate2 picks)
"""
import io
import os
import threading
import wave
from contextlib import asynccontextmanager

import numpy as np

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "small")
LANGUAGE = os.environ.get("WHISPER_LANGUAGE") or None
THREADS = int(os.environ.get("WHISPER_THREADS", "0"))
# Only standard sizes: a request can't make this server download arbitrary repositories.
MODELS = {"tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en",
          "large-v3", "large-v3-turbo", "turbo", "distil-large-v3"}

_models: dict = {}
_load_lock = threading.Lock()
_run_lock = threading.Lock()   # one transcription at a time: they already use every core


def _model(name: str):
    with _load_lock:
        if name not in _models:
            from faster_whisper import WhisperModel   # imported here so /v1/models answers while it loads
            _models[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=THREADS)
        return _models[name]


def _decode(audio: bytes):
    """16 kHz mono float32 samples for a PCM WAV, which is what Dockt's mic buttons send. Anything else goes
    to faster-whisper's own decoder (PyAV), so the common path doesn't depend on PyAV's API."""
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(audio)) as w:
                width, rate, channels = w.getsampwidth(), w.getframerate(), w.getnchannels()
                frames = w.readframes(w.getnframes())
            if width == 2 and frames:
                samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768
                if channels > 1:
                    samples = samples.reshape(-1, channels).mean(axis=1)
                if rate != 16000:
                    count = max(1, round(len(samples) * 16000 / rate))
                    samples = np.interp(np.linspace(0, len(samples) - 1, count),
                                        np.arange(len(samples)), samples).astype(np.float32)
                return samples
        except (wave.Error, EOFError):
            pass
    return io.BytesIO(audio)


def _transcribe(name: str, audio: bytes, language: str | None) -> str:
    with _run_lock:
        segments, _info = _model(name).transcribe(_decode(audio), language=language or LANGUAGE,
                                                  beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


@asynccontextmanager
async def _lifespan(_app):
    # Load (and on first run download) the default model in the background, so the first question is quick.
    threading.Thread(target=_model, args=(DEFAULT_MODEL,), name="whisper-warmup", daemon=True).start()
    yield


app = FastAPI(title="Dockt local Whisper", lifespan=_lifespan)


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": name, "object": "model", "owned_by": "local",
                                        "ready": name in _models} for name in sorted(MODELS)]}


@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile = File(...), model: str = Form(DEFAULT_MODEL),
                         language: str | None = Form(None), response_format: str = Form("json")):
    if model not in MODELS:
        raise HTTPException(400, f"unknown model {model!r}; use one of: {', '.join(sorted(MODELS))}")
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "empty audio")
    try:
        text = await run_in_threadpool(_transcribe, model, audio, language)
    except Exception as e:   # undecodable audio and the like: a clear 422 instead of a bare 500
        raise HTTPException(422, f"couldn't transcribe: {e}") from e
    return PlainTextResponse(text) if response_format == "text" else {"text": text}
