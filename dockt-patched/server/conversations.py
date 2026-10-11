"""Shared laptop chat history for the dashboard and desktop quick chat."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from memory import DATA_DIR

router = APIRouter()


@contextmanager
def database():
    db = sqlite3.connect(DATA_DIR / "conversations.db", timeout=10)
    try:
        with db:
            db.row_factory = sqlite3.Row
            db.execute("""CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, ts INTEGER NOT NULL,
                messages TEXT NOT NULL)""")
            yield db
    finally:
        db.close()


def save(messages: list[dict], conversation_id: str | None = None) -> str:
    cid = conversation_id or str(uuid.uuid4())
    title = next((m["content"] for m in messages if m.get("role") == "user"), "Chat")[:80]
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT ts FROM conversations WHERE id=?", (cid,)).fetchone()
        timestamp = max(time.time_ns() // 1_000_000, old["ts"] + 1 if old else 0)
        db.execute("""INSERT INTO conversations VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title, ts=excluded.ts,
            messages=excluded.messages""",
            (cid, title, timestamp, json.dumps(messages, ensure_ascii=False)))
    return cid


def get(cid: str) -> dict | None:
    with database() as db:
        row = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
    if row is None:
        return None
    return {**dict(row), "messages": json.loads(row["messages"])}


class ConversationReq(BaseModel):
    messages: list[dict] = Field(max_length=500)


@router.get("/api/conversations")
def list_conversations():
    with database() as db:
        rows = db.execute("SELECT * FROM conversations ORDER BY ts DESC LIMIT 30").fetchall()
    return [{**dict(row), "messages": json.loads(row["messages"])} for row in rows]


@router.get("/api/conversations/{cid}")
def read_conversation(cid: str):
    row = get(cid)
    if row is None:
        raise HTTPException(404, "conversation not found")
    return row


@router.put("/api/conversations/{cid}")
def write_conversation(cid: str, req: ConversationReq):
    if any(m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str)
           for m in req.messages):
        raise HTTPException(422, "messages need a role and text content")
    save(req.messages, cid)
    return get(cid)


@router.delete("/api/conversations/{cid}")
def delete_conversation(cid: str):
    with database() as db:
        db.execute("DELETE FROM conversations WHERE id=?", (cid,))
    return {"ok": True}
