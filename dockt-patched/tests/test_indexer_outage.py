"""A capture taken while the GX10 is unreachable must wait in the queue, not error out,
and index as soon as the models come back. Same stack as tests/dashboard/conftest.py:
server/app.py with a temp DATA_DIR, then tools/mock_models.py on the port it points at."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # dockt-patched/


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_json(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


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


def test_capture_survives_model_outage(tmp_path):
    mock_port, port = free_port(), free_port()   # nothing listens on mock_port yet
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "DATA_DIR": str(tmp_path / "data"), "VLM_URL": f"http://127.0.0.1:{mock_port}/v1",
           "DOCKT_PORT": str(port), "DOCKT_HOST": "127.0.0.1", "EXTRACT_ENABLED": "0"}
    for k in ("EMBED_URL", "LLM_URL", "VLM_MODEL", "LLM_MODEL", "EMBED_MODEL", "DOCKT_TOKEN"):
        env.pop(k, None)
    procs = []

    def start(name, args):
        log = open(tmp_path / f"{name}.log", "w", encoding="utf-8")
        procs.append(subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))

    try:
        start("server", ["server/app.py"])
        wait_until(lambda: get_json(base + "/api/stats"), 30, "the Dockt service")
        subprocess.run([sys.executable, "tools/replay.py", "--server", base, "--synthetic", "1"],
                       cwd=ROOT, env=env, check=True, capture_output=True)
        time.sleep(4)   # the old code burned all 3 attempts within ~1 s of connection refused
        (cid,) = [r["id"] for r in get_json(base + "/api/browse")]
        row = get_json(f"{base}/api/capture/{cid}")
        assert row["status"] == "pending", row   # backing off between retries, not given up

        start("mock_models", ["-m", "uvicorn", "--app-dir", "tools", "mock_models:app", "--port", str(mock_port)])
        wait_until(lambda: get_json(base + "/api/stats")["done"] == 1, 30, "indexing after the models came back")
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
