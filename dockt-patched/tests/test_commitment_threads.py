"""Commitments follow the conversation: a later message retracts an earlier request, an unsent draft never becomes
a promise, repeated frames cost no extraction call, it works on an existing database, and Ask isn't starved.

Each test starts tools/mock_models.py + server/app.py on free ports with a throwaway DATA_DIR and posts
`tools/replay.py --scenario all` (the mock reads each frame's messages from the JPEG comment)."""
import json
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def wait(cond, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


class Stack:
    def __init__(self, data: Path, **env):
        self.data, self.mock_port, self.port = data, free_port(), free_port()
        self.base, self.mock = f"http://127.0.0.1:{self.port}", f"http://127.0.0.1:{self.mock_port}"
        import os
        self.env = {**os.environ, "DATA_DIR": str(data), "VLM_URL": self.mock + "/v1", "DOCKT_PORT": str(self.port),
                    "DOCKT_HOST": "127.0.0.1", "EXTRACT_ENABLED": "1", **{k: str(v) for k, v in env.items()}}
        for k in ("EMBED_URL", "LLM_URL", "VLM_MODEL", "LLM_MODEL", "EMBED_MODEL", "DOCKT_TOKEN"):
            self.env.pop(k, None)
        self.log = open(data.parent / f"{data.name}.log", "w")
        self.procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "--app-dir", "tools", "mock_models:app",
                                        "--port", str(self.mock_port)], cwd=ROOT, env=self.env,
                                       stdout=self.log, stderr=subprocess.STDOUT)]
        wait(lambda: get(self.mock + "/v1/models"), 30, "mock models")
        self.procs.append(subprocess.Popen([sys.executable, "server/app.py"], cwd=ROOT, env=self.env,
                                           stdout=self.log, stderr=subprocess.STDOUT))
        wait(lambda: get(self.base + "/api/stats"), 30, "the Dockt service")

    def replay(self, *args):
        out = subprocess.run([sys.executable, "tools/replay.py", "--server", self.base, *args], cwd=ROOT,
                             env=self.env, check=True, capture_output=True, text=True).stdout
        return out

    def db(self):
        con = sqlite3.connect(self.data / "dockt.db")
        con.row_factory = sqlite3.Row
        return con

    def settled(self):
        """Everything indexed and the commitment pass has nothing claimed or waiting."""
        with self.db() as con:
            return con.execute("SELECT COUNT(*) FROM captures WHERE status != 'done' OR extracted IN (0, 1)"
                               ).fetchone()[0] == 0

    def stop(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            p.wait(10)
        self.log.close()


def check_scenarios(stack):
    """The retraction and deleted-draft threads end up right (shared by the fresh- and existing-DB tests)."""
    calls0 = get(stack.mock + "/mock/calls")["extract"]
    stack.replay("--scenario", "all", "--name", "laptop")
    wait(stack.settled, 60, "indexing and extraction to finish")
    items = get(stack.base + "/api/commitments?hours=48")["items"]

    deck = [c for c in items if "deck" in c["text"].lower()]
    assert len(deck) == 1, f"one deck request, no duplicates across repeated frames: {deck}"
    deck = deck[0]
    assert deck["status"] == "cancelled", deck
    assert "nevermind" in deck["status_reason"].lower(), deck
    with stack.db() as con:
        cause = con.execute("SELECT ts, ocr FROM captures WHERE id=?", (deck["status_capture_id"],)).fetchone()
    assert cause and "nevermind" in cause["ocr"] and cause["ts"] > deck["ts"], "links the capture that retracted it"
    assert deck["status_at"] == pytest.approx(cause["ts"])

    assert not [c for c in items if "launch" in c["text"].lower() or "checklist" in c["text"].lower()], \
        f"a promise typed and deleted without sending is not a commitment: {items}"

    # 11 frames, 3 extraction calls: mid-typing frames (< 8 s before the next) are skipped, repeats cost nothing.
    # retraction: +3 (request), +65 (retraction); draft: +200 (question, with the full draft in the box)
    assert get(stack.mock + "/mock/calls")["extract"] - calls0 == 3
    with stack.db() as con:
        draft_frame = con.execute("SELECT ocr FROM captures WHERE ocr LIKE '%[Draft, not sent] I''ll send the launch "
                                  "checklist to Jordan by tomorrow%' AND extracted = 2").fetchall()
    assert draft_frame, "the settled frame with the draft in the box was read, and the draft ignored"

    # the UI shows it struck through with the reason, the time and a link to that capture
    return deck


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


def test_retraction_and_draft_on_a_fresh_database(data_dir):
    data_dir.mkdir()
    stack = Stack(data_dir)
    try:
        check_scenarios(stack)
        stack.replay("--scenario", "all", "--name", "laptop")   # the same frames again: still no duplicates
        wait(stack.settled, 60, "second pass")
        items = get(stack.base + "/api/commitments?hours=48")["items"]
        assert len([c for c in items if "deck" in c["text"].lower()]) == 1
    finally:
        stack.stop()


OLD_SCHEMA = """
CREATE TABLE captures(id INTEGER PRIMARY KEY, device TEXT NOT NULL, ts REAL NOT NULL, app TEXT DEFAULT '',
  title TEXT DEFAULT '', path TEXT NOT NULL, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
  summary TEXT DEFAULT '', ocr TEXT DEFAULT '', error TEXT DEFAULT '', embedding BLOB, processed_at REAL,
  extracted INTEGER DEFAULT 0);
CREATE TABLE agent_stats(device TEXT, session TEXT, checks INTEGER, sent INTEGER, last_seen REAL,
  PRIMARY KEY(device, session));
CREATE VIRTUAL TABLE fts USING fts5(title, app, summary, ocr, tokenize='porter unicode61');
CREATE TABLE commitments(id INTEGER PRIMARY KEY, capture_id INTEGER NOT NULL, device TEXT NOT NULL, ts REAL NOT NULL,
  kind TEXT DEFAULT 'promise', text TEXT NOT NULL, who TEXT DEFAULT '', due TEXT DEFAULT '', done INTEGER DEFAULT 0,
  created_at REAL);
"""


def test_existing_database_is_migrated_and_keeps_working(data_dir):
    """A DB from before threads/status existed: old rows survive, get a thread, and the new behaviour works."""
    data_dir.mkdir()
    now = time.time()
    con = sqlite3.connect(data_dir / "dockt.db")
    con.executescript(OLD_SCHEMA)
    con.execute("INSERT INTO captures(id, device, ts, app, title, path, status, summary, ocr, extracted) "
                "VALUES (1, 'laptop', ?, 'Slack', '#design | Acme — Slack', 'gone.jpg', 'done', 'Slack', "
                "'Me: I''ll review the Q3 budget today', 2)", (now - 3600,))
    con.execute("INSERT INTO commitments(capture_id, device, ts, kind, text, who, done, created_at) "
                "VALUES (1, 'laptop', ?, 'promise', 'Review the Q3 budget today', 'me', 0, ?)", (now - 3600, now))
    con.execute("INSERT INTO commitments(capture_id, device, ts, kind, text, who, done, created_at) "
                "VALUES (1, 'laptop', ?, 'deadline', 'Expense report due Friday', 'me', 1, ?)", (now - 3600, now))
    con.commit()
    con.close()

    stack = Stack(data_dir)
    try:
        with stack.db() as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info(commitments)")}
            old = [dict(r) for r in con.execute("SELECT text, thread_key, status FROM commitments ORDER BY id")]
        assert {"thread_key", "status", "status_reason", "status_capture_id", "status_at"} <= cols
        assert old == [{"text": "Review the Q3 budget today", "thread_key": "slack|#design | acme — slack",
                        "status": "active"},
                       {"text": "Expense report due Friday", "thread_key": "slack|#design | acme — slack",
                        "status": "done"}]
        check_scenarios(stack)   # the deck request joins the same thread as the old open item
        items = {c["text"]: c for c in get(stack.base + "/api/commitments?hours=48")["items"]}
        assert items["Review the Q3 budget today"]["status"] == "active", "the retraction only touches the deck"
    finally:
        stack.stop()


def test_ask_is_not_starved_while_extraction_runs(data_dir):
    """A slow GX10 with 2 parallel slots: while extraction is busy, an Ask still answers within about two model
    calls, and no new extraction call starts while it's running."""
    data_dir.mkdir()
    stack = Stack(data_dir, MOCK_LLM_DELAY=1.0, MOCK_LLM_PARALLEL=2, EXTRACT_CONCURRENCY=2)
    try:
        stack.replay("--synthetic", "24", "--name", "laptop")
        stack.replay("--scenario", "all", "--name", "laptop")
        wait(lambda: get(stack.mock + "/mock/calls")["extract_inflight"] > 0, 60, "extraction to be running")
        before = get(stack.mock + "/mock/calls")
        t0 = time.time()
        r = post(stack.base + "/api/ask", {"q": "where is dinner tonight"})
        took = time.time() - t0
        after = get(stack.mock + "/mock/calls")
        assert r["answer"], r
        assert took < 3.5, f"Ask took {took:.1f}s with extraction running"
        assert after["extract"] == before["extract"], "no extraction call started while Ask was running"
        wait(stack.settled, 120, "extraction to finish after Ask")   # and the extractor carries on afterwards
        assert get(stack.mock + "/mock/calls")["extract"] > after["extract"]
    finally:
        stack.stop()
