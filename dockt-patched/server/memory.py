"""
Memory (Lane 4, laptop half): where this laptop's screenshots + index live, and
the functions the rest of the service uses to read them (TEAM.md §3.2).

  DATA_DIR   screenshots + SQLite DB (default ~/.dockt/data on every OS)

Everything here is a thin wrapper over store.Store; Lane 3 imports *this*
module, never store.py directly.
"""
import os
from pathlib import Path

import models
from store import Store, thread_key
from store import _similar as store_similar

DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path.home() / ".dockt" / "data"))).resolve()
IMG_DIR = DATA_DIR / "img"
IMG_DIR.mkdir(parents=True, exist_ok=True)

store = Store(str(DATA_DIR / "dockt.db"))


async def search(q: str, k: int = 24, device=None, start=None, end=None) -> list[dict]:
    """Hybrid (vector + keyword) search. Embeds the query itself; falls back to
    keyword-only when the embedding model is unreachable."""
    try:
        (qvec,) = await models.embed([q])
    except Exception as e:
        print(f"embed failed, keyword-only search: {e!r}")
        qvec = None
    return store.hybrid(q, qvec, k, device, start, end)


# synchronous reads/writes, re-exported so callers don't touch the Store object
get = store.get
many = store.many
activity = store.activity
commitments = store.commitments
add_commitments = store.add_commitments
set_commitment_done = store.set_commitment_done
set_commitment_status = store.set_commitment_status
thread_commitments = store.thread_commitments
