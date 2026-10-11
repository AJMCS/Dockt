# Dashboard regression tests

Covers every dashboard feature: streaming chat and citation rules, failures, drafts, the screenshot modal, deep links, promises, standup/timesheet, timeline, recent chats (including with the GX10 down), the on-device badge, themes and contrast, layout at four sizes, and the phone drawer. It also has unit tests for the desktop app's helpers.

The suite starts its own mock models + Dockt service on free ports with a temporary `DATA_DIR`, so it never touches your real index or a running Dockt.

```powershell
.venv\Scripts\pip install -r requirements-test.txt
.venv\Scripts\python -m pytest tests\dashboard -q
```

macOS/Linux: same with `.venv/bin/`. It uses Edge on Windows or Chrome elsewhere. Set `DOCKT_TEST_BROWSER=chromium` and run `python -m playwright install chromium` if neither is installed. Set `DOCKT_TEST_HEADED=1` to watch the browser.

Every test fails on an uncaught JS error, or on any request that leaves 127.0.0.1. When you change `server/static/index.html`, run the suite before pushing.
