"""Real HTTP preflight contracts for benchmark endpoints."""

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
from serving_verdict.preflight import EndpointPreflightError, preflight_endpoint


class ProbeHandler(BaseHTTPRequestHandler):
    mode = "ok"
    seen_authorization = ""
    seen_chat_body: dict[str, object] = {}

    def log_message(self, *args: object) -> None:
        return

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen_authorization = self.headers.get("Authorization", "")
        if self.path == "/v1/models":
            if type(self).mode == "models-404":
                self._json(404, {"error": "not found"})
            elif type(self).mode == "missing-model":
                self._json(200, {"object": "list", "data": [{"id": "other-model"}]})
            elif type(self).mode == "malformed-models":
                body = b"not-json"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif type(self).mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "http://example.com/v1/models")
                self.end_headers()
            else:
                self._json(200, {"object": "list", "data": [{"id": "test-model"}]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        type(self).seen_authorization = self.headers.get("Authorization", "")
        size = int(self.headers.get("Content-Length", "0"))
        type(self).seen_chat_body = json.loads(self.rfile.read(size))
        if type(self).mode == "chat-500":
            self._json(500, {"error": "server exploded with super-secret-body"})
        elif type(self).mode == "malformed-chat":
            self._json(200, {"model": "test-model", "choices": []})
        else:
            self._json(
                200,
                {
                    "id": "chatcmpl-probe",
                    "model": "served-test-model",
                    "choices": [
                        {"message": {"role": "assistant", "content": "READY"}}
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 1},
                },
            )


@contextmanager
def probe_server(mode: str = "ok") -> Iterator[str]:
    ProbeHandler.mode = mode
    ProbeHandler.seen_authorization = ""
    ProbeHandler.seen_chat_body = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
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
                "id": "probe-target",
                "base_url": base_url,
                "model": "test-model",
                "api_key_env": "SERVING_VERDICT_API_KEY_PROBE",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_real_preflight_checks_models_and_chat_without_persisting_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with probe_server() as base_url:
        config = load_endpoint_config(endpoint_file(tmp_path, base_url))
        monkeypatch.setenv(config.api_key_env, "probe-secret")
        result = preflight_endpoint(config, timeout_s=3)
    assert result.endpoint_id == "probe-target"
    assert result.requested_model == "test-model"
    assert result.served_model == "served-test-model"
    assert result.models_probe == "matched"
    assert result.chat_probe == "ready"
    assert result.model_ids == ("test-model",)
    assert ProbeHandler.seen_authorization == "Bearer probe-secret"
    assert ProbeHandler.seen_chat_body["model"] == "test-model"
    public = result.public_payload()
    assert "probe-secret" not in json.dumps(public)
    assert "probe-secret" not in repr(result)


def test_models_404_falls_back_to_real_chat_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with probe_server("models-404") as base_url:
        config = load_endpoint_config(endpoint_file(tmp_path, base_url))
        monkeypatch.setenv(config.api_key_env, "dummy")
        result = preflight_endpoint(config, timeout_s=3)
    assert result.models_probe == "unavailable"
    assert result.chat_probe == "ready"


def test_listed_models_must_include_requested_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with probe_server("missing-model") as base_url:
        config = load_endpoint_config(endpoint_file(tmp_path, base_url))
        monkeypatch.setenv(config.api_key_env, "dummy")
        with pytest.raises(EndpointPreflightError, match="requested model is not listed"):
            preflight_endpoint(config, timeout_s=3)


def test_redirect_is_rejected_before_credentials_can_leave_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with probe_server("redirect") as base_url:
        config = load_endpoint_config(endpoint_file(tmp_path, base_url))
        monkeypatch.setenv(config.api_key_env, "redirect-secret")
        with pytest.raises(EndpointPreflightError, match="redirect") as exc:
            preflight_endpoint(config, timeout_s=3)
    assert "redirect-secret" not in str(exc.value)


@pytest.mark.parametrize("mode", ["malformed-models", "malformed-chat"])
def test_malformed_endpoint_responses_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    with probe_server(mode) as base_url:
        config = load_endpoint_config(endpoint_file(tmp_path, base_url))
        monkeypatch.setenv(config.api_key_env, "dummy")
        with pytest.raises(EndpointPreflightError):
            preflight_endpoint(config, timeout_s=3)


def test_http_error_does_not_include_remote_body_or_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with probe_server("chat-500") as base_url:
        config = load_endpoint_config(endpoint_file(tmp_path, base_url))
        monkeypatch.setenv(config.api_key_env, "api-secret")
        with pytest.raises(EndpointPreflightError) as exc:
            preflight_endpoint(config, timeout_s=3)
    text = str(exc.value)
    assert "super-secret-body" not in text
    assert "api-secret" not in text
    assert "HTTP 500" in text
