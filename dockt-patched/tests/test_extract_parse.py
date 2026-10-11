"""The extractor's JSON parser, on outputs real Nemotron produced (tools/check_retraction.py). The mock model can't
reproduce these: it always answers owner "me" and never adds stray brackets."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # dockt-patched/

# brain imports memory, which opens DATA_DIR at import time: keep it out of ~/.dockt
_previous = os.environ.get("DATA_DIR")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="dockt-parse-")
sys.path.insert(0, str(ROOT / "server"))
import brain  # noqa: E402
if _previous is None:
    os.environ.pop("DATA_DIR")
else:
    os.environ["DATA_DIR"] = _previous

ASKED_PRIYA = "Me: can you review the redline?\nPriya: sure, I'll get it back to you by Wednesday"


def test_a_request_the_user_made_belongs_to_whoever_was_asked():
    # Nemotron's answer every time, despite this exact example in EXTRACT_SYSTEM
    out = '{"new": [{"kind": "request", "text": "review the redline", "owner": "me", "due": ""}], "updates": []}'
    new, _ = brain._parse_extract(out, set(), ASKED_PRIYA)
    assert [(n["text"], n["who"]) for n in new] == [("review the redline", "them")]


def test_a_request_made_to_the_user_stays_theirs():
    out = '{"new": [{"kind": "request", "text": "Send the deck to Sarah", "owner": "me", "due": "Friday"}], "updates": []}'
    new, _ = brain._parse_extract(out, set(), "Alex: can you send the deck to Sarah by Friday?")
    assert new[0]["who"] == "me"


def test_promises_and_deadlines_keep_the_models_owner():
    out = ('{"new": [{"kind": "promise", "text": "send the revised NDA", "owner": "me", "due": "Monday"},'
           ' {"kind": "deadline", "text": "Pay ASUS invoice #4471", "owner": "me", "due": "Oct 30"}], "updates": []}')
    new, _ = brain._parse_extract(out, set(), "Me: I'll send the revised NDA by Monday\nInvoice #4471 due Oct 30")
    assert [n["who"] for n in new] == ["me", "me"]


def test_a_stray_closing_brace_after_the_json_still_parses():
    out = ('{"new": [{"kind": "request", "text": "Review the redline", "owner": "them", "due": ""}],'
           ' "updates": []}\n}')
    new, updates = brain._parse_extract(out, set(), ASKED_PRIYA)
    assert [n["text"] for n in new] == ["Review the redline"] and updates == []


def test_output_cut_off_before_the_last_brackets_still_parses():
    new, _ = brain._parse_extract('{"new": [{"kind": "promise", "text": "y", "owner": "them"}]', set())
    assert [n["text"] for n in new] == ["y"]
