"""
Check the commitment extractor's retraction rules against a real chat model (Nemotron on the GX10): who owns a
commitment vs who says "nevermind, I'll do it". Mock models can't judge this. Uses the same prompt (EXTRACT_SYSTEM),
request settings and JSON parser as the server, and drops repeats the way the server does (store.is_repeat);
touches no database.

  python tools/check_retraction.py --url http://127.0.0.1:11435/v1        # through the SSH tunnel
  python tools/check_retraction.py --url http://<gx10-ip>:11434/v1 --model nemotron3:33b --runs 3
"""
import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

# (name, open item: (kind, owner, text), screen text, expected: (status of the open item, owner of a new item or None))
CASES = [
    ("they asked me, then THEY take it back",
     ("request", "me", "Send the deck to Sarah by Friday"),
     "Alex: can you send the deck to Sarah by Friday?\nAlex: actually nevermind, I'll do it",
     ("cancelled", None)),
    ("I asked them, then I take it back",
     ("request", "them", "Alex to send the deck to Sarah by Friday"),
     "Me: Alex, can you send the deck to Sarah by Friday?\nMe: actually nevermind, I'll do it",
     ("cancelled", "me")),
    ("I promised, then THEY say they'll do it",
     ("promise", "me", "Send the deck to Sarah by Friday"),
     "Me: I'll send the deck to Sarah by Friday\nAlex: nevermind, I'll do it",
     ("cancelled", None)),
    ("they promised, then I say I'll do it",
     ("promise", "them", "Alex sends the deck to Sarah by Friday"),
     "Alex: I'll send the deck to Sarah by Friday\nMe: nevermind, I'll do it",
     ("cancelled", "me")),
    ("new: they ask me (no open items)",
     None,
     "Alex: can you send the deck to Sarah by Friday?",
     ("active", "me")),
    ("new: I promise (no open items)",
     None,
     "Jordan: did you get the NDA?\nMe: I'll send the revised NDA by Monday",
     ("active", "me")),
    ("new: they promise me (no open items)",
     None,
     "Me: can you review the redline?\nPriya: sure, I'll get it back to you by Wednesday",
     ("active", "them")),
    ("new: a deadline on screen (email, not a chat)",
     None,
     "Invoice #4471 from ASUS\nAmount due: $1,250.00\nDue date: Oct 30",
     ("active", "me")),
    ("control: nothing retracted",
     ("request", "me", "Send the deck to Sarah by Friday"),
     "Alex: can you send the deck to Sarah by Friday?\nAlex: thanks!",
     ("active", None)),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("LLM_URL") or os.environ.get("VLM_URL"),
                    help="OpenAI-compatible base URL with /v1")
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "nemotron3:33b"))
    ap.add_argument("--runs", type=int, default=1, help="repeat each case (temperature is 0, but check stability)")
    a = ap.parse_args()
    if not a.url:
        sys.exit("give --url, e.g. http://127.0.0.1:11435/v1")
    os.environ.update(VLM_URL=a.url, LLM_URL=a.url, LLM_MODEL=a.model,
                      DATA_DIR=tempfile.mkdtemp(prefix="dockt-check-"))   # brain imports memory; keep it out of ~/.dockt
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
    import brain
    import models
    import store

    async def run():
        ok = total = 0
        for name, open_item, screen, (want_status, want_new_owner) in CASES:
            kind, owner, text = open_item or (None, None, None)
            item = {"id": 7, "kind": kind, "who": owner, "text": text, "due": "Friday", "status": "active"} if kind else None
            user = (f"Conversation: Slack — #design\nSeen: Sat 14:05\n\n"
                    f"OPEN ITEMS\n{brain._item_line(item) if item else '(none)'}\n\n"
                    f"CLOSED ITEMS\n(none)\n\nSummary: A Slack conversation.\n\nSCREEN TEXT\n{screen}")
            for _ in range(a.runs):
                total += 1
                out = await models.chat(brain.EXTRACT_SYSTEM, user, max_tokens=400, temperature=0)
                try:
                    new, updates = brain._parse_extract(out, {7}, screen)
                except ValueError as e:
                    print(f"FAIL  {name}: unparseable output\n      {out!r}")
                    continue
                status = next((u["status"] for u in updates if u["id"] == 7), "active")
                new = [n for n in new if not store.is_repeat(n, [item] if item else [])]   # the server drops these too
                new_owners = [n["who"] for n in new]
                good = status == want_status and (want_new_owner in new_owners if want_new_owner else not new)
                ok += good
                print(f"{'PASS' if good else 'FAIL'}  {name}\n      status {status} (want {want_status}); "
                      f"new {[(n['who'], n['text']) for n in new]} (want owner {want_new_owner or 'none'})\n"
                      f"      reason: {next((u['reason'] for u in updates), '-')}")
        print(f"\n{ok}/{total} as expected  ({a.model} at {a.url})")

    asyncio.run(run())


if __name__ == "__main__":
    main()
