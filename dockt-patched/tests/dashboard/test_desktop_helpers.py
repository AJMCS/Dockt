"""Unit tests for desktop/dockt_desktop.py helpers (no window is opened)."""
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))
import dockt_desktop as d  # noqa: E402


@pytest.mark.parametrize("given,want", [
    ("#/capture/3", "#/capture/3"), ("/#/capture/3", "#/capture/3"), ("capture/3", "#/capture/3"),
    ("#/chat?q=hello%20there", "#/chat?q=hello%20there"), ("", ""), ("#/settings", ""),
    ("javascript:alert(1)", ""), ("https://evil.example", ""),
])
def test_clean_hash_only_allows_dashboard_routes(given, want):
    assert d.clean_hash(given) == want


def test_model_env_mock_points_at_local_mock(monkeypatch):
    monkeypatch.delenv("VLM_URL", raising=False)
    assert d.model_env(mock=True)["VLM_URL"] == "http://127.0.0.1:9000/v1"


def test_model_env_uses_gx10_address(monkeypatch):
    monkeypatch.delenv("VLM_URL", raising=False)
    monkeypatch.setenv("DOCKT_GX10", "192.168.8.10")
    env = d.model_env(mock=False)
    assert env["VLM_URL"] == "http://192.168.8.10:11434/v1"
    assert env["LLM_MODEL"] == "nemotron3:33b" and env["VLM_MODEL"] == "gemma4:26b"


def test_model_env_keeps_explicit_vlm_url(monkeypatch):
    monkeypatch.setenv("DOCKT_GX10", "192.168.8.10")
    monkeypatch.setenv("VLM_URL", "http://10.0.0.5:11434/v1")
    assert d.model_env(mock=False)["VLM_URL"] == "http://10.0.0.5:11434/v1"


def test_resolve_port_respects_dockt_port(monkeypatch):
    monkeypatch.setenv("DOCKT_PORT", "8042")
    monkeypatch.setattr(d, "PORT", 8042)
    monkeypatch.setattr(d, "BASE", "http://127.0.0.1:8042")
    d.resolve_port()
    assert d.BASE == "http://127.0.0.1:8042" and d.os.environ["DOCKT_URL"] == d.BASE


def test_port_free():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert not d.port_free(s.getsockname()[1])


def test_model_env_ssh_target_goes_through_local_tunnel(monkeypatch):
    monkeypatch.delenv("VLM_URL", raising=False)
    monkeypatch.setenv("DOCKT_GX10", "asus@169.233.72.21")
    assert d.model_env(mock=False)["VLM_URL"] == f"http://127.0.0.1:{d.TUNNEL_PORT}/v1"
    assert d.tunnel_target(mock=False, env=d.os.environ) == "asus@169.233.72.21"


@pytest.mark.parametrize("gx10,mock,vlm_url", [
    ("192.168.8.10", False, None),                       # plain address: direct, no tunnel
    ("asus@169.233.72.21", True, None),                  # mock never tunnels
    ("asus@169.233.72.21", False, "http://10.0.0.5:11434/v1"),   # explicit VLM_URL wins
])
def test_no_tunnel_unless_needed(monkeypatch, gx10, mock, vlm_url):
    monkeypatch.setenv("DOCKT_GX10", gx10)
    if vlm_url:
        monkeypatch.setenv("VLM_URL", vlm_url)
    else:
        monkeypatch.delenv("VLM_URL", raising=False)
    assert d.tunnel_target(mock=mock, env=d.os.environ) == ""


def test_ssh_tunnel_args_never_prompt_and_stay_local():
    args = d.ssh_tunnel_args("asus@169.233.72.21", 11435)
    assert args[-1] == "asus@169.233.72.21"
    assert "127.0.0.1:11435:127.0.0.1:11434" in args   # bound to loopback only, not the laptop's LAN address
    assert "BatchMode=yes" in args and "ExitOnForwardFailure=yes" in args


# ---------- local Whisper server for voice input ----------
@pytest.fixture
def whisper_launch(monkeypatch):
    spawned = []
    monkeypatch.setattr(d, "spawn", lambda name, args, env, program=None: spawned.append((name, args, env)) or name)
    monkeypatch.setattr(d, "listening", lambda port: False)
    monkeypatch.setattr(d.importlib.util, "find_spec", lambda name: object() if name == "faster_whisper" else None)
    monkeypatch.delenv("STT_URL", raising=False)
    return spawned


def test_whisper_starts_on_the_default_stt_port_in_english(whisper_launch):
    assert d.start_whisper(mock=False, env={"PATH": "x"}) == "whisper"
    [(name, args, env)] = whisper_launch
    assert args[-4:] == ["--host", "127.0.0.1", "--port", "8001"] and "whisper_server:app" in args
    assert env["WHISPER_LANGUAGE"] == "en" and env["PATH"] == "x"


def test_whisper_keeps_an_explicit_language(whisper_launch):
    d.start_whisper(mock=False, env={"WHISPER_LANGUAGE": "es"})
    assert whisper_launch[0][2]["WHISPER_LANGUAGE"] == "es"


@pytest.mark.parametrize("reason", ["mock", "stt_url_set", "already_running", "not_installed"])
def test_whisper_is_not_started_when_not_needed(whisper_launch, monkeypatch, reason):
    if reason == "stt_url_set":
        monkeypatch.setenv("STT_URL", "http://gx10:8001/v1")
    elif reason == "already_running":
        monkeypatch.setattr(d, "listening", lambda port: port == d.WHISPER_PORT)
    elif reason == "not_installed":
        monkeypatch.setattr(d.importlib.util, "find_spec", lambda name: None)
    assert d.start_whisper(mock=reason == "mock", env={}) is None
    assert whisper_launch == []


def test_mock_mode_sends_voice_input_to_the_mock(monkeypatch):
    monkeypatch.delenv("STT_URL", raising=False)
    assert d.model_env(mock=True)["STT_URL"] == "http://127.0.0.1:9000/v1"


def test_reusing_service_still_starts_missing_whisper(monkeypatch):
    calls = []
    monkeypatch.setattr(d, "model_env", lambda mock: {})
    monkeypatch.setattr(d, "tunnel_target", lambda *args: "")
    monkeypatch.setattr(d, "answers", lambda url: True)
    monkeypatch.setattr(d, "start_whisper", lambda mock, env: calls.append((mock, env)))
    assert d.start_backend(mock=False, capture=False) is None
    assert calls == [(False, {})]
