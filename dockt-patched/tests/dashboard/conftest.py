"""
Shared fixtures for the dashboard regression tests (Lane 2).

The suite starts its own isolated stack, so it never touches your real index:
  tools/mock_models.py on a free port  ->  server/app.py on a free port with a temp DATA_DIR
  -> tools/replay.py --synthetic 24  -> waits until all 24 are indexed and promises are extracted.

Browser: DOCKT_TEST_BROWSER = msedge | chrome | chromium (default: msedge on Windows, chrome
elsewhere, falling back to Playwright's bundled chromium). Set DOCKT_TEST_HEADED=1 to watch.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]   # dockt-patched/
N_CAPTURES = 24


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_json(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def post_json(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def delete(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, method="DELETE"), timeout=5) as r:
        return r.status


def wait_until(check, timeout: float, what: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"timed out waiting for {what}")


@pytest.fixture(scope="session")
def dockt(tmp_path_factory):
    """A running mock pipeline with 24 synthetic captures. Yields .base, .get(path), .post(path, body)."""
    data = tmp_path_factory.mktemp("dockt-data")
    logs = tmp_path_factory.mktemp("dockt-logs")
    mock_port, port = free_port(), free_port()
    base = f"http://127.0.0.1:{port}"
    # pin everything the stack depends on: other test modules (e.g. tests/test_dashboard_flow.py) change
    # os.environ at import time, and the promises tests need the extractor on
    env = {**os.environ, "DATA_DIR": str(data), "VLM_URL": f"http://127.0.0.1:{mock_port}/v1",
           "DOCKT_PORT": str(port), "DOCKT_HOST": "127.0.0.1", "EXTRACT_ENABLED": "1"}
    for k in ("EMBED_URL", "LLM_URL", "VLM_MODEL", "LLM_MODEL", "EMBED_MODEL", "DOCKT_TOKEN"):
        env.pop(k, None)
    procs = []

    def start(name, args):
        log = open(logs / f"{name}.log", "w", encoding="utf-8")
        procs.append(subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))

    try:
        start("mock_models", ["-m", "uvicorn", "--app-dir", "tools", "mock_models:app", "--port", str(mock_port)])
        wait_until(lambda: get_json(f"http://127.0.0.1:{mock_port}/v1/models"), 30, "mock models")
        start("server", ["server/app.py"])
        wait_until(lambda: get_json(base + "/api/stats"), 30, "the Dockt service")
        subprocess.run([sys.executable, "tools/replay.py", "--server", base, "--synthetic", str(N_CAPTURES)],
                       cwd=ROOT, env=env, check=True, capture_output=True)
        wait_until(lambda: get_json(base + "/api/stats")["done"] == N_CAPTURES, 120, "indexing")
        wait_until(lambda: len(get_json(base + "/api/commitments?hours=48")["items"]) >= 3, 120, "promise extraction")
        yield SimpleNamespace(base=base, logs=logs, data=data,
                              get=lambda path: get_json(base + path),
                              post=lambda path, body: post_json(base + path, body),
                              delete=lambda path: delete(base + path))
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.fixture(scope="session")
def browser():
    wanted = os.environ.get("DOCKT_TEST_BROWSER") or ("msedge" if os.name == "nt" else "chrome")
    headless = os.environ.get("DOCKT_TEST_HEADED") != "1"
    with sync_playwright() as p:
        b = None
        for channel in dict.fromkeys([wanted, "chromium"]):
            try:
                b = p.chromium.launch(headless=headless, **({} if channel == "chromium" else {"channel": channel}))
                break
            except PlaywrightError:
                continue
        if b is None:
            pytest.skip("no browser: install Chrome/Edge or run `python -m playwright install chromium`")
        yield b
        b.close()


class Dash:
    """One dashboard tab plus helpers. `errors` collects uncaught JS exceptions; `external` any non-local request."""

    def __init__(self, page, base):
        self.page, self.base = page, base
        self.errors, self.external = [], []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("request", lambda r: r.url.startswith(("http://", "https://")) and not r.url.startswith(base)
                and self.external.append(r.url))

    def open(self, path: str = "/"):
        self.page.goto(self.base + path)
        self.page.wait_for_function("document.querySelectorAll('#dev option').length > 1", timeout=10000)
        return self

    def js(self, expr: str):
        return self.page.evaluate(expr)

    def ask(self, q: str, timeout: float = 15000):
        """Type a question, press Enter, wait until the turn finished (answer, draft or error)."""
        self.page.fill("#q", q)
        self.page.keyboard.press("Enter")
        self.page.wait_for_function("!busy", timeout=timeout)
        self.page.wait_for_timeout(50)

    def last_bot_text(self) -> str:
        return self.page.locator("#thread .msg.bot").last.locator(".text").inner_text()


@pytest.fixture
def context(browser, request):
    marker = request.node.get_closest_marker("viewport")
    width, height = marker.args if marker else (1366, 768)
    scheme = getattr(request, "param", "light")
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    yield ctx
    ctx.close()


@pytest.fixture
def dash(context, dockt):
    # chat history is shared through the local service now (/api/conversations), so start each test empty
    for c in dockt.get("/api/conversations"):
        dockt.delete("/api/conversations/" + urllib.parse.quote(c["id"], safe=""))
    (dockt.data / "theme").unlink(missing_ok=True)   # and with no saved theme (it lives in DATA_DIR)
    d = Dash(context.new_page(), dockt.base)
    yield d
    assert not d.errors, f"uncaught JS errors: {d.errors}"
    assert not d.external, f"requests left the laptop: {d.external}"


def sse(*events) -> str:
    """Build a /api/chat event-stream body (TEAM.md §3.1)."""
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


def pytest_configure(config):
    config.addinivalue_line("markers", "viewport(width, height): browser viewport for this test")
