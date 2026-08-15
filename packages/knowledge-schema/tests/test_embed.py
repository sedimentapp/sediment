"""embed() endpoint-URL resolution and Bearer auth against a live local server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from knowledge_schema import embed


class _Recorder(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": body,
        })
        payload = json.dumps(
            {"data": [{"embedding": [0.1, 0.2]} for _ in body["input"]]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        pass


@pytest.fixture
def server():
    _Recorder.requests = []
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_base_url_gets_v1_embeddings_appended(server):
    vectors = embed(["hello"], server, "test-model")
    assert vectors == [[0.1, 0.2]]
    (req,) = _Recorder.requests
    assert req["path"] == "/v1/embeddings"
    assert req["body"] == {"model": "test-model", "input": ["hello"]}


def test_url_ending_in_embeddings_used_verbatim(server):
    embed(["hello"], f"{server}/v1beta/openai/embeddings", "test-model")
    (req,) = _Recorder.requests
    assert req["path"] == "/v1beta/openai/embeddings"


def test_api_key_sent_as_bearer(server):
    embed(["hello"], server, "test-model", api_key="sk-test-123")
    (req,) = _Recorder.requests
    assert req["authorization"] == "Bearer sk-test-123"


def test_no_api_key_no_auth_header(server):
    embed(["hello"], server, "test-model")
    (req,) = _Recorder.requests
    assert req["authorization"] is None
