"""agent/capture.py must skip Dockt's own windows but still capture an editor with the repo open."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.capture import DEFAULT_EXCLUDE, is_dockt_window, is_excluded  # noqa: E402


@pytest.mark.parametrize("title,skip", [
    ("Dockt", True),                           # dashboard + pet
    ("  dockt ", True),
    ("Dockt Quick Chat", True),
    ("Dockt Quick Chat — answering", True),
    ("server.py - dockt-patched - Visual Studio Code", False),
    ("Dockt — your workday, remembered", False),
])
def test_is_dockt_window(title, skip):
    assert is_dockt_window(title) is skip


def test_repo_in_editor_is_not_excluded():
    assert not is_excluded("Code", "README.md - Dockt - Visual Studio Code", DEFAULT_EXCLUDE)
