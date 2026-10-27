import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class FakeApi:
    """An OpenAI-compatible transcription server that records what it receives."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.text = "hello world"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(("GET", self.path, self.headers.get("Authorization"), b""))
                self._reply(200 if outer.status == 200 else outer.status, {"data": []})

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
                self._reply(outer.status, {"text": outer.text} if outer.status == 200 else {"error": {"message": "nope"}})

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def api():
    fake = FakeApi()
    yield fake
    fake.server.shutdown()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLD_DICTATE_CONFIG_DIR", str(tmp_path / "cfg"))
    for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "HOLD_DICTATE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
