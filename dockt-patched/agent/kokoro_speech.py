"""Generate and play a local Kokoro reply. Reads speech text as UTF-8 from stdin."""
from __future__ import annotations

import os
import sys
import tempfile
import urllib.request
import wave
from pathlib import Path

MODEL_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin",
}


def ensure_models(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, url in MODEL_FILES.items():
        target = folder / name
        if target.is_file() and target.stat().st_size > 1024:
            continue
        partial = target.with_suffix(target.suffix + ".download")
        try:
            urllib.request.urlretrieve(url, partial)
            if partial.stat().st_size < 1024:
                raise OSError("Kokoro model download was incomplete")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)


def main() -> None:
    text = sys.stdin.read().strip()
    if not text:
        return
    folder = Path(os.environ.get("DOCKT_KOKORO_MODELS", Path.home() / ".dockt" / "kokoro"))
    voice = os.environ.get("DOCKT_KOKORO_VOICE", "af_heart")
    ensure_models(folder)

    from kokoro_onnx import Kokoro
    import numpy as np
    import winsound

    audio, sample_rate = Kokoro(str(folder / "kokoro-v1.0.onnx"), str(folder / "voices-v1.0.bin")).create(
        text, voice=voice, speed=1.0, lang="en-us")
    if not len(audio):
        raise RuntimeError("Kokoro returned no speech audio")
    samples = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    playback = Path(tempfile.gettempdir()) / f"dockt-speech-{os.getpid()}.wav"
    try:
        with wave.open(str(playback), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(samples)
        winsound.PlaySound(str(playback), winsound.SND_FILENAME)
    finally:
        playback.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
