"""Local API and dashboard smoke tests; all data stays in a temporary test directory."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self._stop_patches)
        self.client_context = TestClient(dockt_app.app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)

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


if __name__ == "__main__":
    unittest.main()
