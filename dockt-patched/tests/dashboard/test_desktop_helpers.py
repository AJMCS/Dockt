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
