"""Local API and dashboard smoke tests; all data stays in a temporary test directory."""
from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import unittest
import warnings
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError

from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=StarletteDeprecationWarning,
                            message="Using `httpx` with `starlette.testclient` is deprecated")
    from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
_test_data = tempfile.TemporaryDirectory(prefix="dockt-tests-")
os.environ["DATA_DIR"] = _test_data.name
os.environ["EXTRACT_ENABLED"] = "0"

import app as dockt_app  # noqa: E402
sys.path.insert(0, str(ROOT))
from agent import quickchat


class DashboardFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addClassCleanup(dockt_app.store.db.close)

    def setUp(self):
        self.capture = {
            "id": 42,
            "device": "test-laptop",
            "ts": 1791658800,
            "app": "Slack",
            "title": "#hackathon",
            "summary": "Sam booked dinner at Nopalito for 8 PM.",
            "ocr": "Sam booked dinner at Nopalito for 8 PM.",
            "status": "done",
            "path": "",
        }
        self.patches = [
            patch.object(dockt_app.memory, "search", new=AsyncMock(return_value=[self.capture])),
            patch.object(dockt_app.memory, "activity", return_value=([], 0, 0)),
            patch.object(dockt_app.models, "chat",
                         new=AsyncMock(return_value="Sam booked dinner at Nopalito for 8 PM [#42].")),
            # Lane 3's /api/chat agent calls models.chat_messages: search first, then answer from the result
            patch.object(dockt_app.models, "chat_messages", new=self._fake_chat_messages),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self._stop_patches)
        self.client_context = TestClient(dockt_app.app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)

    @staticmethod
    async def _fake_chat_messages(messages, tools=None, **_):
        if tools and not any(m["role"] == "tool" for m in messages):
            return {"content": "", "tool_calls": [
                {"id": "call_0", "name": "search_screens", "arguments": {"q": "dinner"}}]}
        return {"content": "Sam booked dinner at Nopalito for 8 PM [#42].", "tool_calls": []}

    def _stop_patches(self):
        for p in reversed(self.patches):
            p.stop()

    def test_dashboard_serves_editable_chat_composer(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('<textarea id="q"', response.text)
        self.assertNotIn('<textarea id="q" disabled', response.text)

    def test_non_stream_chat_returns_answer_and_citation(self):
        response = self.client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "Who booked dinner?"}],
            "stream": False,
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("Nopalito", body["answer"])
        self.assertEqual(body["cited"], [42])
        self.assertEqual(body["sources"][0]["id"], 42)

    def test_streamed_chat_emits_complete_dashboard_event_sequence(self):
        response = self.client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "Who booked dinner?"}],
            "stream": True,
        })
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ")]
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[0], "status")
        self.assertIn("token", kinds)
        self.assertEqual(kinds[-2:], ["sources", "done"])
        self.assertEqual(events[-1]["cited"], [42])

    def popup_urlopen(self, request, **_kwargs):
        """Run the popup's actual HTTP payloads against this isolated FastAPI app."""
        if isinstance(request, str):
            path = request.split("http://127.0.0.1:8000", 1)[-1]
            response = self.client.get(path)
        else:
            path = request.full_url.split("http://127.0.0.1:8000", 1)[-1]
            response = self.client.post(path, json=json.loads(request.data))
        if not response.is_success:
            raise HTTPError(path, response.status_code, "request failed", {}, io.BytesIO(response.content))
        return io.BytesIO(response.content)

    def test_pet_and_dashboard_share_one_saved_conversation_with_citations(self):
        cid = str(uuid.uuid4())
        first = [{"role": "user", "content": "Who booked dinner?"}]
        with patch.object(quickchat, "BASE_URL", "http://127.0.0.1:8000"), \
             patch.object(quickchat, "urlopen", side_effect=self.popup_urlopen):
            result = quickchat.chat_request(first, cid)
            saved = self.client.get(f"/api/conversations/{cid}").json()
            self.assertEqual(result["conversation_id"], cid)
            self.assertEqual(len(saved["messages"]), 2)
            self.assertEqual(saved["messages"][1]["cited"], [42])
            self.assertTrue(any(c["id"] == cid for c in self.client.get("/api/conversations").json()))

            response = self.client.post("/api/chat", json={"messages": saved["messages"] +
                [{"role": "user", "content": "What time?"}], "conversation_id": cid, "stream": True})
            self.assertEqual(response.status_code, 200)
            resumed = quickchat.load_conversation(cid)
            self.assertEqual(len(resumed["messages"]), 4)
            self.assertGreater(resumed["ts"], saved["ts"])
            self.assertEqual(resumed["messages"][1]["cited"], [42])
            self.assertEqual(resumed["messages"][1]["sources"][0]["id"], 42)
            self.assertEqual(resumed["messages"][2]["content"], "What time?")

    def test_failed_model_reply_still_saves_the_user_message(self):
        from fastapi import HTTPException
        cid = str(uuid.uuid4())
        with patch.object(dockt_app.brain, "_chat_messages",
                          new=AsyncMock(side_effect=HTTPException(503, "offline"))):
            response = self.client.post("/api/chat", json={"messages":
                [{"role": "user", "content": "Keep this question"}], "conversation_id": cid})
        self.assertEqual(response.status_code, 503)
        saved = self.client.get(f"/api/conversations/{cid}").json()
        self.assertEqual(saved["messages"], [{"role": "user", "content": "Keep this question"}])

    def test_conversation_save_restore_delete_and_validation(self):
        cid = str(uuid.uuid4())
        payload = {"messages": [{"role": "user", "content": "From dashboard"}]}
        self.assertEqual(self.client.put(f"/api/conversations/{cid}", json=payload).status_code, 200)
        self.assertEqual(self.client.get(f"/api/conversations/{cid}").json()["messages"], payload["messages"])
        self.assertEqual(self.client.put(f"/api/conversations/{cid}", json={"messages":
            [{"role": "invalid", "content": "bad"}]}).status_code, 422)
        self.assertEqual(self.client.delete(f"/api/conversations/{cid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/conversations/{cid}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
