"""
Storage + hybrid search. SQLite (with FTS5 keyword index) + an in-memory
numpy matrix for vector search. 15k captures x 1024-dim is ~60MB and a full
brute-force cosine search takes a few ms — no vector DB server needed, and
no ARM64 wheel surprises on the GX10.

Search = reciprocal-rank fusion of semantic (embeddings) and keyword (BM25)
results, so both "when was I looking at flights" and exact strings like a
name, email, or error code work.
"""
import re
import sqlite3
import threading
import time

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures(
  id INTEGER PRIMARY KEY,
  device TEXT NOT NULL,
  ts REAL NOT NULL,
  app TEXT DEFAULT '',
  title TEXT DEFAULT '',
  path TEXT NOT NULL,
  status TEXT DEFAULT 'pending',      -- pending | processing | done | error
  attempts INTEGER DEFAULT 0,
  summary TEXT DEFAULT '',
  ocr TEXT DEFAULT '',
  error TEXT DEFAULT '',
  embedding BLOB,
  processed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ts ON captures(ts);
CREATE INDEX IF NOT EXISTS idx_status ON captures(status);
CREATE TABLE IF NOT EXISTS agent_stats(
  device TEXT, session TEXT, checks INTEGER, sent INTEGER, last_seen REAL,
  PRIMARY KEY(device, session)
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, app, summary, ocr, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS commitments(
  id INTEGER PRIMARY KEY,
  capture_id INTEGER NOT NULL,
  device TEXT NOT NULL,
  ts REAL NOT NULL,                   -- when it was seen on screen
  kind TEXT DEFAULT 'promise',        -- promise | deadline | request
  text TEXT NOT NULL,
  who TEXT DEFAULT '',
  due TEXT DEFAULT '',
  done INTEGER DEFAULT 0,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_commit_ts ON commitments(ts);
CREATE TABLE IF NOT EXISTS threads(            -- last text the commitment pass read per conversation
  thread_key TEXT PRIMARY KEY,
  last_text TEXT DEFAULT '',
  last_capture_id INTEGER,
  last_ts REAL
);
"""

# commitments columns added after the first release; ALTER TABLE so existing databases keep their rows
COMMIT_COLUMNS = [
    ("thread_key", "TEXT DEFAULT ''"),          # app + normalized window title: one chat / email thread
    ("status", "TEXT DEFAULT 'active'"),        # active | cancelled | changed | done
    ("status_reason", "TEXT DEFAULT ''"),
    ("status_capture_id", "INTEGER"),           # the capture whose message cancelled / changed it
    ("status_at", "REAL"),                      # when that capture was seen
]


def thread_key(app, title) -> str:
    """One conversation across frames: app + window title without unread counts, badges and browser suffixes."""
    t = (title or "").lower()
    t = re.sub(r"\(\d+\+?\)|\[\d+\+?\]|\d+ new (?:items?|messages?)", " ", t)
    t = re.sub(r"\s[-|—–]\s(?:google chrome|microsoft edge|mozilla firefox|safari|brave)$", "", t.strip())
    t = re.sub(r"^[^\w#@]+", "", t)               # leading bullets / unread dots
    t = re.sub(r"\s+", " ", t).strip()
    return f"{(app or '').strip().lower()}|{t}"[:200]


def _words(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _similar(a, b, thresh=0.6):
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= thresh

FIELDS = "id, device, ts, app, title, summary, ocr, status"


def is_repeat(item, thread_items, open_items=()):
    """An extracted item repeats one we have: same text and owner as anything in its thread (whatever the status:
    a cancelled request still visible on screen must not come back), or same text as an open item elsewhere.
    A different owner is a different commitment ("nevermind, I'll do it" moves the task to the speaker)."""
    text, who = item.get("text") or "", item.get("who") or "me"
    return (any(_similar(text, t["text"]) and (t["who"] or "me") == who for t in thread_items)
            or any(_similar(text, t["text"]) for t in open_items))


def _absorb_glances(spans, max_glance_s=120):
    """A-B-A where B is a quick glance (<=2 captures, <2 min) on the same device:
    merge the two A spans into one, and keep B as its own line."""
    out = []
    i = 0
    while i < len(spans):
        sp = spans[i]
        if (out and i + 1 < len(spans)
                and out[-1]["key"] == spans[i + 1]["key"]
                and sp["key"][0] == out[-1]["key"][0]
                and sp["n"] <= 2 and sp["end"] - sp["start"] <= max_glance_s):
            a, c = out[-1], spans[i + 1]
            a["end"], a["n"], a["rows"] = c["end"], a["n"] + c["n"], a["rows"] + c["rows"]
            out.insert(len(out) - 1, sp)  # glance listed just before the merged span's tail
            i += 2
            continue
        out.append(sp)
        i += 1
    return out


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            # one-time migration for DBs created before the commitments pass existed
            try:
                self.db.execute("ALTER TABLE captures ADD COLUMN extracted INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already there
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_extracted ON captures(status, extracted)")
            try:
                self.db.execute("ALTER TABLE captures ADD COLUMN extract_attempts INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            for col, decl in COMMIT_COLUMNS:
                try:
                    self.db.execute(f"ALTER TABLE commitments ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError:
                    pass  # column already there
            self.db.execute("UPDATE commitments SET status='done' WHERE done=1 AND status='active'")
            for r in self.db.execute(  # give rows from before threads existed their thread
                    "SELECT m.id, c.app, c.title FROM commitments m JOIN captures c ON c.id = m.capture_id "
                    "WHERE m.thread_key IS NULL OR m.thread_key = ''").fetchall():
                self.db.execute("UPDATE commitments SET thread_key=? WHERE id=?",
                                (thread_key(r["app"], r["title"]), r["id"]))
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_commit_thread ON commitments(thread_key, status)")
            # anything mid-flight when we last stopped goes back in the queue
            self.db.execute("UPDATE captures SET status='pending' WHERE status='processing'")
            # and give up-on rows another go (e.g. errored out during a GX10 outage)
            self.db.execute("UPDATE captures SET status='pending', attempts=0 WHERE status='error'")
            self.db.execute("UPDATE captures SET extracted=0 WHERE extracted=1")  # 1 = claimed, 2 = done
            self.db.commit()
        self._ids = np.zeros(0, dtype=np.int64)
        self._mat = None
        self._dirty = True

    # ---------- writes ----------
    def add(self, device, ts, app, title, path) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO captures(device, ts, app, title, path) VALUES (?,?,?,?,?)",
                (device, ts, app, title, path))
            self.db.commit()
            return cur.lastrowid

    def claim(self, n: int):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, device, ts, app, title, path, attempts FROM captures "
                "WHERE status='pending' ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
            # newest first: the demo cares about what just happened
            if rows:
                self.db.executemany("UPDATE captures SET status='processing' WHERE id=?",
                                    [(r["id"],) for r in rows])
                self.db.commit()
            return [dict(r) for r in rows]

    def complete(self, cid, summary, ocr, emb):
        vec = None
        if emb is not None:
            v = np.asarray(emb, dtype=np.float32)
            v /= (np.linalg.norm(v) + 1e-9)
            vec = v.tobytes()
        with self.lock:
            row = self.db.execute("SELECT title, app FROM captures WHERE id=?", (cid,)).fetchone()
            self.db.execute(
                "UPDATE captures SET status='done', summary=?, ocr=?, embedding=?, "
                "processed_at=?, error='' WHERE id=?",
                (summary, ocr, vec, time.time(), cid))
            self.db.execute("DELETE FROM fts WHERE rowid=?", (cid,))
            self.db.execute("INSERT INTO fts(rowid, title, app, summary, ocr) VALUES (?,?,?,?,?)",
                            (cid, row["title"], row["app"], summary, ocr))
            self.db.commit()
            self._dirty = True

    def fail(self, cid, err, max_attempts=3):
        with self.lock:
            self.db.execute(
                "UPDATE captures SET attempts=attempts+1, error=?, "
                "status=CASE WHEN attempts+1>=? THEN 'error' ELSE 'pending' END WHERE id=?",
                (str(err)[:500], max_attempts, cid))
            self.db.commit()

    def requeue(self, cid, err):
        """Models unreachable: back in the queue without using up an attempt."""
        with self.lock:
            self.db.execute("UPDATE captures SET status='pending', error=? WHERE id=?",
                            (str(err)[:500], cid))
            self.db.commit()

    # ---------- commitments ("things you promised") ----------
    # captures.extracted: 0 = to do, 1 = claimed, 2 = done or skipped, 3 = gave up after repeated failures
    def claim_extract(self, n: int, min_text=40, settle_s=8.0):
        """Indexed captures the commitment pass hasn't read yet, OLDEST first (a retraction has to be read after
        the promise it retracts). Only settled frames: one replaced by the next capture on the same device within
        settle_s was mid-typing or mid-scroll, so it's skipped without an LLM call; the newest frame waits until it
        has stayed on screen settle_s. Captures with almost no screen text are skipped too."""
        with self.lock:
            self.db.execute(
                "UPDATE captures SET extracted=2 WHERE status='done' AND extracted=0 "
                "AND length(ocr) + length(summary) < ?", (min_text,))
            self.db.execute(
                "UPDATE captures SET extracted=2 WHERE status='done' AND extracted=0 AND EXISTS ("
                "SELECT 1 FROM captures n WHERE n.device = captures.device AND n.ts > captures.ts "
                "AND n.ts < captures.ts + ?)", (settle_s,))
            # indexing runs newest-first, so also wait until every earlier frame of the same app on this device
            # is indexed: otherwise a retraction can be read before the request it retracts
            rows = self.db.execute(
                "SELECT id, device, ts, app, title, summary, ocr FROM captures "
                "WHERE status='done' AND extracted=0 AND ts <= ? AND NOT EXISTS ("
                "SELECT 1 FROM captures p WHERE p.device = captures.device AND p.app = captures.app "
                "AND p.ts < captures.ts AND p.status IN ('pending', 'processing')) ORDER BY ts ASC LIMIT ?",
                (time.time() - settle_s, n)).fetchall()
            if rows:
                self.db.executemany("UPDATE captures SET extracted=1 WHERE id=?",
                                    [(r["id"],) for r in rows])
            self.db.commit()
            return [dict(r) for r in rows]

    def mark_extracted(self, cid, ok=True):
        with self.lock:
            self.db.execute("UPDATE captures SET extracted=? WHERE id=?", (2 if ok else 0, cid))
            self.db.commit()

    def extract_failed(self, cid, err, max_attempts=3, count=True):
        """Back in the queue to try again; after max_attempts real failures, give up (extracted=3).
        count=False (models unreachable) retries without using up an attempt."""
        with self.lock:
            if count:
                self.db.execute(
                    "UPDATE captures SET extract_attempts = extract_attempts + 1, "
                    "extracted = CASE WHEN extract_attempts + 1 >= ? THEN 3 ELSE 0 END WHERE id=?",
                    (max_attempts, cid))
            else:
                self.db.execute("UPDATE captures SET extracted=0 WHERE id=?", (cid,))
            self.db.commit()
        print(f"extract failed id={cid} ({'counted' if count else 'models down'}): {str(err)[:200]}")

    def thread_text(self, key):
        with self.lock:
            r = self.db.execute("SELECT last_text FROM threads WHERE thread_key=?", (key,)).fetchone()
        return r["last_text"] if r else None

    def set_thread_text(self, key, text, capture_id, ts):
        with self.lock:
            self.db.execute(
                "INSERT INTO threads(thread_key, last_text, last_capture_id, last_ts) VALUES (?,?,?,?) "
                "ON CONFLICT(thread_key) DO UPDATE SET last_text=excluded.last_text, "
                "last_capture_id=excluded.last_capture_id, last_ts=excluded.last_ts",
                (key, text[:4000], capture_id, ts))
            self.db.commit()

    def thread_commitments(self, key, since, statuses=("active",), limit=20):
        q = ",".join("?" * len(statuses))
        with self.lock:
            rows = self.db.execute(
                f"SELECT id, capture_id, ts, kind, text, who, due, status, status_reason FROM commitments "
                f"WHERE thread_key=? AND ts >= ? AND status IN ({q}) ORDER BY ts LIMIT ?",
                (key, since, *statuses, limit)).fetchall()
        return [dict(r) for r in rows]

    def add_commitments(self, row, items) -> list[dict]:
        """Insert extracted items, skipping repeats (is_repeat) from the last 24h: the same message is captured in
        many frames and sometimes several windows."""
        key = thread_key(row.get("app"), row.get("title"))
        added = []
        with self.lock:
            since = row["ts"] - 86400
            in_thread = [dict(r) for r in self.db.execute(
                "SELECT text, who FROM commitments WHERE ts > ? AND thread_key = ?", (since, key))]
            open_elsewhere = [dict(r) for r in self.db.execute(
                "SELECT text, who FROM commitments WHERE ts > ? AND thread_key != ? AND status = 'active' "
                "ORDER BY id DESC LIMIT 300", (since, key))]
            for it in items:
                text = (it.get("text") or "").strip()[:240]
                if not text or is_repeat({**it, "text": text}, in_thread, open_elsewhere):
                    continue
                cur = self.db.execute(
                    "INSERT INTO commitments(capture_id, device, ts, kind, text, who, due, created_at, thread_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (row["id"], row["device"], row["ts"], (it.get("kind") or "promise")[:20],
                     text, (it.get("who") or "")[:60], (it.get("due") or "")[:60], time.time(), key))
                in_thread.append({"text": text, "who": it.get("who") or "me"})
                added.append({"id": cur.lastrowid, "text": text})
            self.db.commit()
        return added

    def set_commitment_status(self, cid, status, reason, capture_id, ts) -> bool:
        """Close an open item (cancelled / changed / done) because of what capture_id showed. Only active items."""
        with self.lock:
            cur = self.db.execute(
                "UPDATE commitments SET status=?, status_reason=?, status_capture_id=?, status_at=?, "
                "done = CASE WHEN ?='done' THEN 1 ELSE done END WHERE id=? AND status='active'",
                (status, (reason or "")[:200], capture_id, ts, status, cid))
            self.db.commit()
            return cur.rowcount > 0

    def commitments(self, start=None, end=None, device=None, include_done=True, limit=200):
        where, args = ["1=1"], []
        if start is not None:
            where.append("ts >= ?"); args.append(start)
        if end is not None:
            where.append("ts < ?"); args.append(end)
        if device:
            where.append("device = ?"); args.append(device)
        if not include_done:  # open items only
            where.append("done = 0 AND status = 'active'")
        with self.lock:
            rows = self.db.execute(
                f"SELECT id, capture_id, device, ts, kind, text, who, due, done, created_at, "
                f"thread_key, status, status_reason, status_capture_id, status_at "
                f"FROM commitments WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?",
                args + [limit]).fetchall()
        return [dict(r) for r in rows]

    def set_commitment_done(self, cid, done):
        """The checkbox. Cancelled / changed items keep their status; done <-> active otherwise."""
        with self.lock:
            self.db.execute(
                "UPDATE commitments SET done=?, status = CASE WHEN status IN ('active','done') "
                "THEN ? ELSE status END WHERE id=?", (1 if done else 0, "done" if done else "active", cid))
            self.db.commit()

    def agent_ping(self, device, session, checks, sent):
        with self.lock:
            self.db.execute(
                "INSERT INTO agent_stats(device, session, checks, sent, last_seen) VALUES (?,?,?,?,?) "
                "ON CONFLICT(device, session) DO UPDATE SET checks=excluded.checks, "
                "sent=excluded.sent, last_seen=excluded.last_seen",
                (device, session, int(checks), int(sent), time.time()))
            self.db.commit()

    # ---------- reads ----------
    def get(self, cid):
        with self.lock:
            r = self.db.execute(f"SELECT {FIELDS}, path, error FROM captures WHERE id=?",
                                (cid,)).fetchone()
        return dict(r) if r else None

    def many(self, ids):
        if not ids:
            return []
        with self.lock:
            q = ",".join("?" * len(ids))
            rows = self.db.execute(f"SELECT {FIELDS} FROM captures WHERE id IN ({q})",
                                   list(ids)).fetchall()
        by = {r["id"]: dict(r) for r in rows}
        return [by[i] for i in ids if i in by]

    def stats(self):
        with self.lock:
            rows = self.db.execute("SELECT status, COUNT(*) n FROM captures GROUP BY status").fetchall()
            recent = self.db.execute("SELECT COUNT(*) FROM captures WHERE processed_at > ?",
                                     (time.time() - 300,)).fetchone()[0]
            ingest = self.db.execute("SELECT COUNT(*) FROM captures WHERE ts > ?",
                                     (time.time() - 300,)).fetchone()[0]
            devices = [r[0] for r in self.db.execute("SELECT DISTINCT device FROM captures")]
            checks, kept = self.db.execute(
                "SELECT COALESCE(SUM(checks),0), COALESCE(SUM(sent),0) FROM agent_stats").fetchone()
        s = {r["status"]: r["n"] for r in rows}
        return {"total": sum(s.values()), "done": s.get("done", 0),
                "queued": s.get("pending", 0) + s.get("processing", 0),
                "errors": s.get("error", 0),
                "indexed_per_min": round(recent / 5, 1),
                "captured_per_min": round(ingest / 5, 1),
                "devices": devices,
                "checks": checks, "kept": kept,
                "filtered_pct": round(100 * (1 - kept / checks), 1) if checks else None}

    def timeline(self, bucket_s=600, device=None):
        with self.lock:
            sql = ("SELECT device, CAST(ts / ? AS INTEGER) * ? AS b, COUNT(*) n, MIN(id) first_id "
                   "FROM captures {} GROUP BY device, b ORDER BY b")
            if device:
                rows = self.db.execute(sql.format("WHERE device=?"),
                                       (bucket_s, bucket_s, device)).fetchall()
            else:
                rows = self.db.execute(sql.format(""), (bucket_s, bucket_s)).fetchall()
        return [dict(r) for r in rows]

    def browse(self, start=None, end=None, device=None, limit=60):
        where, args = ["1=1"], []
        if start is not None:
            where.append("ts >= ?"); args.append(start)
        if end is not None:
            where.append("ts < ?"); args.append(end)
        if device:
            where.append("device = ?"); args.append(device)
        with self.lock:
            rows = self.db.execute(
                f"SELECT {FIELDS} FROM captures WHERE {' AND '.join(where)} "
                f"ORDER BY ts DESC LIMIT ?", args + [limit]).fetchall()
        return [dict(r) for r in rows]

    def activity(self, start, end, device=None, max_spans=120, gap_s=300):
        """Compress captures into spans of 'same app + window on one device'.
        Hundreds of captures become a few dozen lines the LLM can read at once."""
        where, args = ["ts >= ? AND ts < ?"], [start, end]
        if device:
            where.append("device = ?"); args.append(device)
        with self.lock:
            rows = self.db.execute(
                f"SELECT id, device, ts, app, title, summary FROM captures "
                f"WHERE {' AND '.join(where)} ORDER BY device, ts", args).fetchall()
        spans, cur = [], None
        for r in rows:
            key = (r["device"], r["app"], r["title"])
            if cur and cur["key"] == key and r["ts"] - cur["end"] <= gap_s:
                cur["end"] = r["ts"]; cur["n"] += 1; cur["rows"].append(r)
            else:
                cur = {"key": key, "start": r["ts"], "end": r["ts"], "n": 1, "rows": [r]}
                spans.append(cur)
        spans = _absorb_glances(spans)
        omitted = 0
        if len(spans) > max_spans:  # keep the longest spans, drop brief blips
            omitted = len(spans) - max_spans
            spans = sorted(spans, key=lambda x: x["end"] - x["start"] + x["n"], reverse=True)[:max_spans]
        spans.sort(key=lambda x: x["start"])
        out = []
        for sp in spans:
            mid = sp["rows"][len(sp["rows"]) // 2]
            out.append({"device": sp["key"][0], "app": sp["key"][1], "title": sp["key"][2],
                        "start": sp["start"], "end": sp["end"], "n": sp["n"],
                        "id": mid["id"], "summary": (mid["summary"] or "")[:120]})
        return out, omitted, len(rows)

    # ---------- search ----------
    def _matrix(self):
        if self._dirty:
            with self.lock:
                rows = self.db.execute(
                    "SELECT id, embedding FROM captures WHERE embedding IS NOT NULL").fetchall()
            if rows:
                self._ids = np.array([r["id"] for r in rows], dtype=np.int64)
                self._mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
            self._dirty = False
        return self._ids, self._mat

    def vector_search(self, qvec, n):
        ids, mat = self._matrix()
        if mat is None or not len(ids):
            return []
        q = np.asarray(qvec, dtype=np.float32)
        q /= (np.linalg.norm(q) + 1e-9)
        if q.shape[0] != mat.shape[1]:
            return []  # embedding model changed; re-index needed
        scores = mat @ q
        top = np.argsort(-scores)[:n]
        return [int(ids[i]) for i in top]

    def keyword_search(self, query, n):
        words = re.findall(r"[\w@.\-']+", query.lower())
        words = [w.strip(".'-") for w in words if len(w.strip(".'-")) > 1]
        if not words:
            return []
        match = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
        with self.lock:
            try:
                rows = self.db.execute(
                    "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, 2.0, 1.0, 1.0, 1.5) LIMIT ?",
                    (match, n)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [r[0] for r in rows]

    def hybrid(self, query, qvec, k=10, device=None, start=None, end=None):
        pool = k * 8
        vec = self.vector_search(qvec, pool) if qvec is not None else []
        kw = self.keyword_search(query, pool)
        scores = {}
        for rank, cid in enumerate(vec):
            scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
        for rank, cid in enumerate(kw):
            scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
        ranked = sorted(scores, key=scores.get, reverse=True)
        rows = self.many(ranked)
        out = []
        for r in rows:
            if device and r["device"] != device:
                continue
            if start is not None and r["ts"] < start:
                continue
            if end is not None and r["ts"] >= end:
                continue
            r["score"] = round(scores[r["id"]], 4)
            out.append(r)
            if len(out) >= k:
                break
        return out
