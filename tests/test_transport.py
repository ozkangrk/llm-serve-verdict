"""Transport-layer HTTP contracts for streaming benchmark requests.

The transport is the only component that touches the network. It must:
- send the fixed bearer key and nothing else secret;
- reject redirects (a redirect must never carry the key to another host);
- classify timeout / http_error / connection failure cleanly;
- never leak the secret or remote bodies into raised messages.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from serving_verdict.endpoint import load_endpoint_config
from serving_verdict.transport import EndpointTransportError, stream_chat_completions


class RedirectHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # type: ignore[override]
        return

    def do_POST(self) -> None:  # noqa: N802
        self.send_response(302)
        self.send_header("Location", "http://example.com/elsewhere")
        self.end_headers()


@contextmanager
def redirect_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def endpoint_file(tmp_path: Path, base_url: str) -> Path:
    path = tmp_path / "endpoint.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "serving-verdict.endpoint.v1",
                "id": "transport-target",
                "base_url": base_url,
                "model": "test-model",
                "api_key_env": "SERVING_VERDICT_API_KEY_TRANSPORT",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_redirect_is_rejected_without_leaking_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with redirect_server() as base_url:
        config = load_endpoint_config(endpoint_file(tmp_path, base_url))
        monkeypatch.setenv(config.api_key_env, "transport-secret")
        payload = {"model": "test-model", "messages": [], "stream": True}
        with pytest.raises(EndpointTransportError) as exc:
            list(stream_chat_completions(config, "transport-secret", payload, request_timeout_s=2.0))
    text = str(exc.value)
    assert "redirect" in text.lower()
    assert "transport-secret" not in text


def test_connection_refused_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing listens on this port: connection failure, not a crash.
    config = load_endpoint_config(
        endpoint_file(tmp_path, "http://127.0.0.1:9/v1"),
    )
    monkeypatch.setenv(config.api_key_env, "unused")
    payload = {"model": "test-model", "messages": [], "stream": True}
    with pytest.raises(EndpointTransportError) as exc:
        list(stream_chat_completions(config, "unused", payload, request_timeout_s=1.0))
    text = str(exc.value)
    assert "connection failure" in text
    assert "unused" not in text


def test_timeout_is_classified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A server that accepts the connection but never sends a header line.
    class HangHandler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            import time

            time.sleep(5)
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), HangHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = load_endpoint_config(
            endpoint_file(tmp_path, f"http://127.0.0.1:{server.server_port}/v1")
        )
        monkeypatch.setenv(config.api_key_env, "hang-secret")
        payload = {"model": "test-model", "messages": [], "stream": True}
        with pytest.raises(EndpointTransportError) as exc:
            list(stream_chat_completions(config, "hang-secret", payload, request_timeout_s=0.3))
        assert "timeout" in str(exc.value).lower()
        assert "hang-secret" not in str(exc.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_transport_never_echoes_secret_or_remote_body() -> None:
    # Even when a remote 500 body contains a "secret-looking" string, the
    # raised message must not carry it.
    class ErrorBodyHandler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            body = json.dumps({"error": "boom internal-secret-token"}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ErrorBodyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = load_endpoint_config(
                endpoint_file(Path(tmp), f"http://127.0.0.1:{server.server_port}/v1")
            )
            payload = {"model": "test-model", "messages": [], "stream": True}
            # A non-2xx status returns (http_status, lines) instead of raising,
            # so callers classify it. The status must be surfaced...
            status, lines = stream_chat_completions(
                config, "bearer-secret", payload, request_timeout_s=2.0
            )
            assert status == 500
            # ...and no remote body text is embedded in the line iterator.
            assert "internal-secret-token" not in "".join(lines)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
