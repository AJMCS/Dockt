"""Suggestion generation uses local models and fails independently of chat."""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="dockt-suggestions-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import brain
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()
app.include_router(brain.router)
client = TestClient(app)


def test_uses_chat_context_and_local_model(monkeypatch):
    calls = []

    async def model(system, user, **kwargs):
        calls.append(json.loads(user))
        topic = json.loads(user)[0]["content"]
        return json.dumps({"suggestions": [{"label": "Follow up on " + topic,
                                            "prompt": "Draft a follow-up about " + topic}]})

    monkeypatch.setattr(brain.models, "chat", model)
    for topic in ("Priya's NDA", "Sam's dinner"):
        history = [{"role": "user", "content": topic}, {"role": "assistant", "content": "Due Friday."}]
        response = client.post("/api/chat/suggestions", json={"messages": history})
        assert response.status_code == 200
        assert topic in response.json()["suggestions"][0]["prompt"]
        assert calls[-1] == history


def test_invalid_and_duplicate_suggestions_are_removed():
    items = [None, {"label": "", "prompt": "x"}, {"label": "x", "prompt": 7},
             {"label": "Review NDA", "prompt": "Review the NDA"},
             {"label": "review nda", "prompt": "Another request"},
             {"label": "Again", "prompt": "review the nda"}]
    assert brain._parse_suggestions(json.dumps({"suggestions": items})) == [
        {"label": "Review NDA", "prompt": "Review the NDA"}]
    for raw in ("garbage", "[]", '{"suggestions": null}'):
        assert brain._parse_suggestions(raw) == []
    valid = '{"suggestions":[{"label":"Draft NDA","prompt":"Draft the NDA"}]}'
    assert brain._parse_suggestions(valid + "\n}") == [{"label": "Draft NDA", "prompt": "Draft the NDA"}]
    assert brain._parse_suggestions(valid[:-1]) == [{"label": "Draft NDA", "prompt": "Draft the NDA"}]
    assert brain._parse_suggestions(valid[:-5]) == []
    assert brain._parse_suggestions(valid + "unrelated prose") == []


def test_model_outage_and_malformed_output_are_optional(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(brain.models, "chat", broken)
    history = [{"role": "user", "content": "NDA"}, {"role": "assistant", "content": "Friday"}]
    assert client.post("/api/chat/suggestions", json={"messages": history}).json() == {"suggestions": []}

    async def malformed(*args, **kwargs):
        return "not JSON"

    monkeypatch.setattr(brain.models, "chat", malformed)
    assert client.post("/api/chat/suggestions", json={"messages": history}).json() == {"suggestions": []}
